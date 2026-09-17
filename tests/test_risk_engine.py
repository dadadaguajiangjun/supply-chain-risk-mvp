from __future__ import annotations

from datetime import date

import pandas as pd

from app.excel_service import ParsedWorkbook
from app.risk_engine import calculate_risk


BASE_DATE = date(2026, 9, 16)


def workbook(key: str, frame: pd.DataFrame) -> ParsedWorkbook:
    return ParsedWorkbook(key, key, f"{key}.xlsx", frame, [])


def test_risk_calculation_covers_red_yellow_and_green() -> None:
    stock = pd.DataFrame([
        {"warehouse": "A仓", "material_code": "RED", "material_name": "红色物料", "stock_qty": 100, "allocated_qty": 0, "frozen_qty": 0},
        {"warehouse": "A仓", "material_code": "YELLOW", "material_name": "黄色物料", "stock_qty": 180, "allocated_qty": 0, "frozen_qty": 0},
        {"warehouse": "A仓", "material_code": "GREEN", "material_name": "绿色物料", "stock_qty": 1000, "allocated_qty": 0, "frozen_qty": 0},
    ])
    booking = pd.DataFrame([
        {"warehouse": "A仓", "material_code": code, "material_name": name, "delivery_date": day, "demand_qty": qty}
        for code, name, day, qty in [
            ("RED", "红色物料", "2026-09-17", 80), ("RED", "红色物料", "2026-09-18", 80),
            ("YELLOW", "黄色物料", "2026-09-17", 80), ("YELLOW", "黄色物料", "2026-09-18", 80),
            ("GREEN", "绿色物料", "2026-09-17", 80), ("GREEN", "绿色物料", "2026-09-18", 80),
        ]
    ])
    rows = calculate_risk({"stock": workbook("stock", stock), "booking_order": workbook("booking_order", booking)}, BASE_DATE)
    levels = {row["material_code"]: row["risk_level"] for row in rows}
    assert levels == {"RED": "红色", "YELLOW": "黄色", "GREEN": "绿色"}


def test_purchase_and_transfer_are_included() -> None:
    stock = pd.DataFrame([{"warehouse": "杭州仓", "material_code": "A", "material_name": "物料A", "stock_qty": 100, "allocated_qty": 0, "frozen_qty": 0}])
    booking = pd.DataFrame([
        {"warehouse": "杭州仓", "material_code": "A", "material_name": "物料A", "delivery_date": "2026-09-17", "demand_qty": 120},
        {"warehouse": "杭州仓", "material_code": "A", "material_name": "物料A", "delivery_date": "2026-09-18", "demand_qty": 120},
    ])
    purchase = pd.DataFrame([{"warehouse": "杭州仓", "material_code": "A", "delivery_date": "2026-09-18", "remaining_qty": 100}])
    transfer = pd.DataFrame([{"from_warehouse": "湖州仓", "to_warehouse": "杭州仓", "material_code": "A", "arrival_date": "2026-09-17", "transfer_qty": 100}])
    rows = calculate_risk({
        "stock": workbook("stock", stock), "booking_order": workbook("booking_order", booking),
        "purchase_order": workbook("purchase_order", purchase), "transfer": workbook("transfer", transfer),
    }, BASE_DATE)
    row = next(item for item in rows if item["warehouse"] == "杭州仓库")
    assert row["t1_projected_stock"] == 80
    assert row["t2_projected_stock"] == 60
    assert row["t1_transfer_in"] == 100
    assert row["t2_purchase"] == 100


def test_risk_result_contains_source_trace() -> None:
    stock = pd.DataFrame([{
        "warehouse": "湖州DC", "material_code": "A", "material_name": "物料A",
        "stock_qty": 100, "allocated_qty": 0, "frozen_qty": 0,
    }])
    booking = pd.DataFrame([{
        "order_no": "SO1", "warehouse": "湖州仓库", "material_code": "A",
        "material_name": "物料A", "delivery_date": "2026-09-17", "demand_qty": 120,
    }])
    purchase = pd.DataFrame([{
        "order_no": "PO1", "warehouse": "湖州仓库", "material_code": "A",
        "delivery_date": "2026-09-17", "remaining_qty": 30,
    }])

    row = calculate_risk({
        "stock": workbook("stock", stock),
        "booking_order": workbook("booking_order", booking),
        "purchase_order": workbook("purchase_order", purchase),
    }, BASE_DATE)[0]

    assert row["_trace"]["stock_components"][0]["source_file"] == "stock.xlsx"
    assert row["_trace"]["demand"][0]["order_no"] == "SO1"
    assert row["_trace"]["purchase"][0]["order_no"] == "PO1"
