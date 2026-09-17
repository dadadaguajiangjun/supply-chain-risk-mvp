from __future__ import annotations

from datetime import date

import pandas as pd

from app.data_quality import clean_and_assess
from app.excel_service import ParsedWorkbook
from app.warehouse_service import apply_warehouse_mappings


BASE_DATE = date(2026, 9, 16)


def workbook(key: str, frame: pd.DataFrame, filename: str = "test.xlsx") -> ParsedWorkbook:
    return ParsedWorkbook(key, key, filename, frame, [], "Sheet1", 0, len(frame))


def test_stock_date_one_day_warns_but_older_date_blocks() -> None:
    one_day = workbook("stock", pd.DataFrame([{
        "warehouse": "湖州DC", "material_code": "A", "stock_qty": 10,
        "snapshot_date": "2026-09-15",
    }]))
    expired = workbook("stock", pd.DataFrame([{
        "warehouse": "湖州DC", "material_code": "A", "stock_qty": 10,
        "snapshot_date": "2026-09-14",
    }]))

    warning_report = clean_and_assess(one_day, BASE_DATE)
    blocked_report = clean_and_assess(expired, BASE_DATE)

    assert warning_report.file_date_status == "库存滞后1天"
    assert warning_report.calculation_allowed is True
    assert blocked_report.file_date_status == "库存滞后2天"
    assert blocked_report.calculation_allowed is False


def test_quality_detects_duplicates_illegal_dates_and_quantities() -> None:
    frame = pd.DataFrame([
        {"warehouse": "湖州仓库", "material_code": "A", "delivery_date": "2026-09-17", "demand_qty": 10},
        {"warehouse": "湖州仓库", "material_code": "A", "delivery_date": "2026-09-17", "demand_qty": 10},
        {"warehouse": "湖州仓库", "material_code": "B", "delivery_date": "非法日期", "demand_qty": 8},
        {"warehouse": "湖州仓库", "material_code": "C", "delivery_date": "2026-09-17", "demand_qty": "非法数量"},
    ])
    parsed = workbook("booking_order", frame)

    report = clean_and_assess(parsed, BASE_DATE)

    assert report.duplicate_count == 1
    assert report.date_parse_failure_count == 1
    assert report.numeric_parse_failure_count == 1
    assert report.original_rows == 4
    assert report.valid_rows == 1
    assert report.deleted_rows == 3


def test_warehouse_code_has_priority_over_name_and_alias_matches() -> None:
    mappings = [
        {"source_system": "stock", "raw_warehouse_code": "W1", "raw_warehouse_name": "旧名称", "standard_warehouse_code": "S1", "standard_warehouse_name": "编码匹配仓库", "special_alias": "", "enabled": 1},
        {"source_system": "stock", "raw_warehouse_code": "", "raw_warehouse_name": "名称匹配", "standard_warehouse_code": "S2", "standard_warehouse_name": "名称匹配仓库", "special_alias": "别名仓", "enabled": 1},
    ]
    frame = pd.DataFrame([
        {"warehouse_code": "W1", "warehouse": "名称匹配", "material_code": "A", "stock_qty": 1},
        {"warehouse_code": "", "warehouse": "别名仓", "material_code": "B", "stock_qty": 1},
    ])
    parsed = workbook("stock", frame)

    unmatched = apply_warehouse_mappings(parsed, mappings, BASE_DATE)

    assert list(parsed.dataframe["warehouse"]) == ["编码匹配仓库", "名称匹配仓库"]
    assert unmatched == []


def test_unmatched_warehouse_returns_pending_candidate_only() -> None:
    parsed = workbook("stock", pd.DataFrame([{
        "warehouse_code": "UNKNOWN", "warehouse": "湖州DC", "material_code": "A", "stock_qty": 1,
    }]), "库存.xlsx")

    unmatched = apply_warehouse_mappings(parsed, [], BASE_DATE)

    assert parsed.dataframe.loc[0, "warehouse"] == "湖州DC"
    assert unmatched[0]["raw_warehouse_code"] == "UNKNOWN"
    assert unmatched[0]["suggested_standard_name"] == "湖州仓库"
    assert unmatched[0]["record_count"] == 1
