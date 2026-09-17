from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.excel_service import normalize_warehouse_name, parse_workbook, standardize_columns
from app.config import FILE_SPECS


def test_real_transfer_headers_are_mapped() -> None:
    frame = pd.DataFrame(columns=[
        "调出点", "调入点", "物料编码", "物料名称", "需求数量",
        "预计送达时间", "单据状态",
    ])

    standardized, warnings = standardize_columns(frame, FILE_SPECS["transfer"])

    assert warnings == []
    assert {
        "from_warehouse", "to_warehouse", "material_code", "material_name",
        "transfer_qty", "arrival_date", "transfer_status",
    }.issubset(standardized.columns)


def test_real_stock_and_outbound_headers_are_mapped() -> None:
    stock = pd.DataFrame(columns=[
        "仓库名称", "产品编码", "产品名称", "库存状态", "库存数量",
        "分配数量", "冻结数量", "调拨在途数量",
    ])
    outbound = pd.DataFrame(columns=[
        "统计日期", "仓库名称（维表）", "物料编码", "物料名称",
        "仓库拣货_物料数量",
    ])

    standardized_stock, _ = standardize_columns(stock, FILE_SPECS["stock"])
    standardized_outbound, _ = standardize_columns(outbound, FILE_SPECS["outbound"])

    assert {"warehouse", "material_code", "material_name", "stock_qty"}.issubset(
        standardized_stock.columns
    )
    assert {"warehouse", "material_code", "material_name", "picked_qty"}.issubset(
        standardized_outbound.columns
    )


def test_full_purchase_inbound_transfer_and_demand_headers_are_mapped() -> None:
    purchase = pd.DataFrame(columns=[
        "单据编号", "采购日期", "供应商", "单据状态", "采购组织", "关闭状态",
        "物料编码", "物料名称", "采购数量", "交货日期", "剩余入库数量", "仓库",
    ])
    inbound = pd.DataFrame(columns=[
        "仓库编码", "仓库名称", "预期到货时间", "入库日期", "参考编号3",
        "预期数量", "收货数量（EA）", "供应商", "供应商名称", "产品代码", "产品描述",
    ])
    transfer = pd.DataFrame(columns=[
        "需求单号", "运输状态", "调出点", "调入点", "物料编码", "物料名称",
        "需求数量", "计划安排量", "出库量", "入库量", "预计发车时间", "预计送达时间", "实际送达时间",
    ])
    demand = pd.DataFrame(columns=[
        "统计日期", "统计周", "仓库名称（维表）", "物料一级分类", "物料编码", "物料名称",
        "仓库分配_物料数量", "仓库拣货_物料数量", "分配金额", "拣货金额",
    ])

    p, _ = standardize_columns(purchase, FILE_SPECS["purchase_order"])
    i, _ = standardize_columns(inbound, FILE_SPECS["inbound_daily"])
    t, _ = standardize_columns(transfer, FILE_SPECS["transfer"])
    d, _ = standardize_columns(demand, FILE_SPECS["outbound"])

    assert {"order_no", "purchase_qty", "remaining_qty", "delivery_date"}.issubset(p.columns)
    assert {"reference_no3", "expected_qty", "inbound_qty", "material_code"}.issubset(i.columns)
    assert {"order_no", "transfer_qty", "outbound_qty", "inbound_qty"}.issubset(t.columns)
    assert {"stat_date", "stat_week", "allocated_qty", "picked_qty", "allocated_amount", "picked_amount"}.issubset(d.columns)


def test_booking_order_separates_ordered_and_saleable_quantities() -> None:
    booking = pd.DataFrame(columns=["送货时间", "发货仓库", "物料编码", "物料名称", "下单数量", "计划发货数量"])
    standardized, _ = standardize_columns(booking, FILE_SPECS["booking_order"])
    assert {"delivery_date", "warehouse", "material_code", "ordered_qty", "demand_qty"}.issubset(standardized.columns)


def test_warehouse_suffixes_normalize_to_same_business_key() -> None:
    assert normalize_warehouse_name("湖州DC") == "湖州仓库"
    assert normalize_warehouse_name("湖州 RDC") == "湖州仓库"
    assert normalize_warehouse_name("湖州仓") == "湖州仓库"
    assert normalize_warehouse_name("湖州仓库") == "湖州仓库"


def test_parser_finds_stock_sheet_and_header(tmp_path: Path) -> None:
    path = tmp_path / "原料存销表-存销调拨.xlsx"
    cover = pd.DataFrame([["说明", "这不是导入数据"]])
    transfer = pd.DataFrame([
        {
            "调出点": "湖州DC",
            "调入点": "杭州DC",
            "物料编码": "01.01.001",
            "需求数量": 20,
            "预计送达时间": "2026-09-17",
        }
    ])
    stock = pd.DataFrame([
        {
            "仓库名称": "湖州DC",
            "产品编码": "01.01.001",
            "产品名称": "测试物料",
            "库存状态": "ZP",
            "库存数量": 100,
        }
    ])
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        cover.to_excel(writer, sheet_name="说明", index=False, header=False)
        transfer.to_excel(writer, sheet_name="oms", index=False)
        stock.to_excel(writer, sheet_name="库存", index=False, startrow=2)

    parsed = parse_workbook(path)

    assert parsed.file_type == "stock"
    assert parsed.dataframe.loc[0, "warehouse"] == "湖州DC"
    assert parsed.dataframe.loc[0, "material_code"] == "01.01.001"
    assert "工作表“库存”第3行" in parsed.warnings[0]
