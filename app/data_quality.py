from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date

import pandas as pd

from .config import FILE_SPECS
from .excel_service import ParsedWorkbook, clean_column_name, text_series


DATE_COLUMNS: dict[str, tuple[str, ...]] = {
    "purchase_order": ("source_date", "delivery_date"),
    "stock": ("snapshot_date",),
    "inbound_daily": ("inbound_date",),
    "transfer": ("departure_date", "arrival_date", "actual_departure_date", "actual_arrival_date"),
    "booking_order": ("delivery_date",),
    "outbound": ("stat_date",),
}

NUMERIC_COLUMNS: dict[str, tuple[str, ...]] = {
    "purchase_order": ("remaining_qty", "purchase_qty"),
    "stock": ("stock_qty", "allocated_qty", "frozen_qty", "transfer_in_transit_qty"),
    "inbound_daily": ("inbound_qty", "expected_qty"),
    "transfer": ("transfer_qty", "outbound_qty", "inbound_qty", "demand_qty"),
    "booking_order": ("ordered_qty", "demand_qty"),
    "outbound": ("picked_qty", "allocated_qty"),
}

PREFERRED_FILE_DATE: dict[str, tuple[str, ...]] = {
    "stock": ("snapshot_date",),
    "purchase_order": ("source_date", "delivery_date"),
    "booking_order": ("delivery_date",),
    "transfer": ("departure_date", "arrival_date"),
    "outbound": ("stat_date",),
    "inbound_daily": ("inbound_date",),
}


@dataclass
class QualityReport:
    file_type: str
    filename: str
    sheet_name: str
    original_rows: int
    valid_rows: int = 0
    deleted_rows: int = 0
    required_missing_count: int = 0
    date_parse_failure_count: int = 0
    numeric_parse_failure_count: int = 0
    duplicate_count: int = 0
    unmatched_warehouse_count: int = 0
    unmatched_material_count: int = 0
    negative_count: int = 0
    file_date: str | None = None
    file_date_source: str | None = None
    file_date_status: str = "未知"
    calculation_allowed: bool = True
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        result = asdict(self)
        result["details_json"] = result.pop("details")
        return result


def _nonblank(series: pd.Series) -> pd.Series:
    return series.notna() & series.astype(str).str.strip().ne("") & ~series.astype(str).isin({"nan", "None"})


def _filename_date(filename: str) -> date | None:
    for token in re.findall(r"(?<!\d)(20\d{6})(?!\d)", filename):
        parsed = pd.to_datetime(token, format="%Y%m%d", errors="coerce")
        if pd.notna(parsed):
            return parsed.date()
    return None


def infer_file_date(workbook: ParsedWorkbook) -> tuple[date | None, str | None, dict]:
    frame = workbook.dataframe
    ranges: dict[str, dict[str, str]] = {}
    for column in PREFERRED_FILE_DATE.get(workbook.file_type, ()):
        if column not in frame.columns:
            continue
        parsed = pd.to_datetime(frame[column], errors="coerce").dropna().dt.normalize()
        if parsed.empty:
            continue
        ranges[column] = {
            "min": parsed.min().date().isoformat(),
            "max": parsed.max().date().isoformat(),
        }
        # Snapshot/source dates describe the file itself. Event dates describe
        # coverage, so use their most frequent date as the representative date.
        mode = parsed.mode()
        representative = (mode.iloc[0] if not mode.empty else parsed.max()).date()
        return representative, f"Excel字段:{column}", {"date_ranges": ranges}
    fallback = _filename_date(workbook.filename)
    if fallback:
        return fallback, "文件名回退", {"date_ranges": ranges}
    return None, None, {"date_ranges": ranges}


def _date_status(file_type: str, file_date: date | None, business_date: date) -> str:
    if file_date is None:
        return "未知"
    lag = (business_date - file_date).days
    if file_type == "stock":
        if lag == 0:
            return "库存当日"
        if lag == 1:
            return "库存滞后1天"
        if lag > 1:
            return f"库存滞后{lag}天"
        return "库存日期晚于基准日"
    if lag == 0:
        return "包含基准日"
    if lag > 0:
        return f"代表日期早于基准日{lag}天"
    return f"代表日期晚于基准日{abs(lag)}天"


def clean_and_assess(
    workbook: ParsedWorkbook,
    business_date: date,
    unmatched_warehouse_count: int = 0,
) -> QualityReport:
    frame = workbook.dataframe.copy()
    spec = FILE_SPECS[workbook.file_type]
    report = QualityReport(
        file_type=workbook.file_type,
        filename=workbook.filename,
        sheet_name=str(workbook.sheet_name),
        original_rows=workbook.original_row_count or len(frame),
        unmatched_warehouse_count=unmatched_warehouse_count,
    )

    invalid = pd.Series(False, index=frame.index)
    required_missing = pd.Series(False, index=frame.index)
    for column in spec.required_fields:
        if column not in frame.columns:
            required_missing |= True
        else:
            required_missing |= ~_nonblank(frame[column])
    report.required_missing_count = int(required_missing.sum())
    invalid |= required_missing

    date_failures = 0
    invalid_date_rows = pd.Series(False, index=frame.index)
    for column in DATE_COLUMNS.get(workbook.file_type, ()):
        if column not in frame.columns:
            continue
        present = _nonblank(frame[column])
        parsed = pd.to_datetime(frame[column], errors="coerce")
        failed = present & parsed.isna()
        date_failures += int(failed.sum())
        if column in spec.required_fields:
            invalid_date_rows |= failed
    report.date_parse_failure_count = date_failures
    invalid |= invalid_date_rows

    numeric_failures = 0
    negative_count = 0
    invalid_numeric_rows = pd.Series(False, index=frame.index)
    for column in NUMERIC_COLUMNS.get(workbook.file_type, ()):
        if column not in frame.columns:
            continue
        raw = frame[column].astype(str).str.replace(",", "", regex=False).str.strip()
        present = _nonblank(frame[column])
        parsed = pd.to_numeric(raw, errors="coerce")
        failed = present & parsed.isna()
        numeric_failures += int(failed.sum())
        negative_count += int((parsed < 0).sum())
        if column in spec.required_fields:
            invalid_numeric_rows |= failed
    report.numeric_parse_failure_count = numeric_failures
    report.negative_count = negative_count
    invalid |= invalid_numeric_rows

    valid = frame.loc[~invalid].copy()
    # Only remove exact duplicate source rows. Repeated warehouse/material keys
    # can legitimately represent different lots, suppliers or orders.
    duplicate_mask = valid.duplicated(keep="first")
    report.duplicate_count = int(duplicate_mask.sum())
    valid = valid.loc[~duplicate_mask].copy()
    valid["_source_row"] = [int(index) + workbook.header_row + 2 for index in valid.index]
    valid = valid.reset_index(drop=True)
    report.valid_rows = int(len(valid))
    report.deleted_rows = report.original_rows - report.valid_rows

    file_date, source, details = infer_file_date(workbook)
    report.file_date = file_date.isoformat() if file_date else None
    report.file_date_source = source
    report.file_date_status = _date_status(workbook.file_type, file_date, business_date)
    report.details.update(details)

    if workbook.file_type == "stock":
        lag = (business_date - file_date).days if file_date else None
        report.calculation_allowed = lag is not None and 0 <= lag <= 1

    workbook.dataframe = valid
    return report


def add_unmatched_material_counts(
    workbooks: dict[str, ParsedWorkbook], reports: dict[str, QualityReport]
) -> None:
    stock = workbooks.get("stock")
    if stock is None or "material_code" not in stock.dataframe.columns:
        return
    stock_codes = set(text_series(stock.dataframe, "material_code")) - {""}
    for file_type, workbook in workbooks.items():
        if file_type == "stock" or "material_code" not in workbook.dataframe.columns:
            continue
        codes = text_series(workbook.dataframe, "material_code")
        reports[file_type].unmatched_material_count = int((codes.ne("") & ~codes.isin(stock_codes)).sum())
