from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.data_quality import add_unmatched_material_counts, clean_and_assess
import app.database as database
from app.database import db_connection, initialize_database
from app.excel_service import parse_workbook
from app.operational_service import persist_operational_data
from app.warehouse_service import apply_warehouse_mappings


QUANTITY_FIELDS = {
    "purchase_order": ["purchase_qty", "remaining_qty"],
    "inbound_daily": ["expected_qty", "inbound_qty"],
    "stock": ["stock_qty", "allocated_qty", "frozen_qty", "transfer_in_transit_qty", "purchase_in_transit_qty"],
    "transfer": ["demand_qty", "transfer_qty", "outbound_qty", "inbound_qty"],
    "outbound": ["allocated_qty", "picked_qty", "allocated_amount", "picked_amount"],
    "booking_order": ["ordered_qty", "demand_qty"],
}


def quantity_totals(frame: pd.DataFrame, file_type: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for field in QUANTITY_FIELDS.get(file_type, []):
        if field in frame.columns:
            values = pd.to_numeric(frame[field].astype(str).str.replace(",", "", regex=False), errors="coerce")
            result[field] = round(float(values.fillna(0).sum()), 4)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay existing real Excel files without producing business conclusions.")
    parser.add_argument("folder", type=Path)
    parser.add_argument("business_date", type=date.fromisoformat)
    parser.add_argument('--database', type=Path, help='可选独立回放数据库，避免覆盖当前维护数据')
    parser.add_argument('--report', type=Path, help='保存清洗前后对比JSON')
    parser.add_argument(
        "--remove-replay-id", action="append", default=[],
        help="Remove rows created by a specific earlier replay id before importing again.",
    )
    args = parser.parse_args()

    if args.database:
        args.database.parent.mkdir(parents=True, exist_ok=True)
        database.DB_PATH = args.database.resolve()

    initialize_database()
    with db_connection() as conn:
        mappings = [dict(row) for row in conn.execute("SELECT * FROM warehouse_mapping ORDER BY id").fetchall()]

    workbooks = {}
    reports = {}
    unmatched_items = []
    before_totals = {}
    for path in sorted(args.folder.glob("*.xlsx")):
        parsed = parse_workbook(path)
        if parsed.file_type in workbooks:
            raise ValueError(f"目录中存在重复文件类型：{parsed.file_type}")
        before_totals[parsed.file_type] = quantity_totals(parsed.dataframe, parsed.file_type)
        unmatched = apply_warehouse_mappings(parsed, mappings, args.business_date)
        unmatched_items.extend(unmatched)
        reports[parsed.file_type] = clean_and_assess(
            parsed, args.business_date,
            unmatched_warehouse_count=sum(item["record_count"] for item in unmatched),
        )
        workbooks[parsed.file_type] = parsed

    add_unmatched_material_counts(workbooks, reports)
    replay_id = f"replay-{uuid.uuid4().hex[:12]}"
    if args.remove_replay_id:
        placeholders = ",".join("?" for _ in args.remove_replay_id)
        with db_connection() as conn:
            conn.execute(f"DELETE FROM stock_snapshot WHERE run_id IN ({placeholders})", args.remove_replay_id)
            for table in ("purchase_record", "inbound_record", "transfer_record", "demand_history", "booking_demand"):
                conn.execute(
                    f"DELETE FROM {table} WHERE last_run_id IN ({placeholders})",
                    args.remove_replay_id,
                )
    persist_operational_data(replay_id, args.business_date, workbooks)

    payload = {
        "replay_id": replay_id,
        "business_date": args.business_date.isoformat(),
        "folder": str(args.folder.resolve()),
        "files": [],
    }
    for file_type, parsed in workbooks.items():
        report = reports[file_type].to_dict()
        after = quantity_totals(parsed.dataframe, file_type)
        fields = sorted(set(before_totals[file_type]) | set(after))
        payload["files"].append({
            "file_type": file_type,
            "filename": parsed.filename,
            "sheet_name": parsed.sheet_name,
            "original_rows": report["original_rows"],
            "valid_rows": report["valid_rows"],
            "deleted_rows": report["deleted_rows"],
            "required_missing_count": report['required_missing_count'],
            "duplicate_count": report['duplicate_count'],
            "date_parse_failure_count": report['date_parse_failure_count'],
            "numeric_parse_failure_count": report['numeric_parse_failure_count'],
            "file_date": report["file_date"],
            "file_date_status": report["file_date_status"],
            "calculation_allowed": report["calculation_allowed"],
            "quantity_comparison": {
                field: {
                    "before": before_totals[file_type].get(field, 0),
                    "after": after.get(field, 0),
                    "difference": round(after.get(field, 0) - before_totals[file_type].get(field, 0), 4),
                }
                for field in fields
            },
        })
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


if __name__ == "__main__":
    main()
