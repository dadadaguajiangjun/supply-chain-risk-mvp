from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "demo_files"
TODAY = date.today()


def write(name: str, rows: list[dict]) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_excel(OUTPUT / name, index=False)


write("库存余量_演示.xlsx", [
    {"仓库": "杭州仓", "物料编码": "YL00428", "物料名称": "鲜奶", "库存状态": "ZP", "库存数量": 1000, "分配数量": 200, "冻结数量": 0, "调拨在途数量": 0},
    {"仓库": "湖州仓", "物料编码": "YL00428", "物料名称": "鲜奶", "库存状态": "ZP", "库存数量": 9000, "分配数量": 500, "冻结数量": 0, "调拨在途数量": 0},
    {"仓库": "杭州仓", "物料编码": "BC001", "物料名称": "纸杯", "库存状态": "ZP", "库存数量": 3500, "分配数量": 0, "冻结数量": 0, "调拨在途数量": 0},
])

write("报货订单实时_演示.xlsx", [
    {"送货日期": TODAY + timedelta(days=1), "发货仓库": "杭州仓", "物料编码": "YL00428", "物料名称": "鲜奶", "下单数量": 1500, "计划发货数量": 1500},
    {"送货日期": TODAY + timedelta(days=2), "发货仓库": "杭州仓", "物料编码": "YL00428", "物料名称": "鲜奶", "下单数量": 1600, "计划发货数量": 1600},
    {"送货日期": TODAY + timedelta(days=1), "发货仓库": "湖州仓", "物料编码": "YL00428", "物料名称": "鲜奶", "下单数量": 1500, "计划发货数量": 1500},
    {"送货日期": TODAY + timedelta(days=2), "发货仓库": "湖州仓", "物料编码": "YL00428", "物料名称": "鲜奶", "下单数量": 1500, "计划发货数量": 1500},
    {"送货日期": TODAY + timedelta(days=1), "发货仓库": "杭州仓", "物料编码": "BC001", "物料名称": "纸杯", "下单数量": 1800, "计划发货数量": 1800},
    {"送货日期": TODAY + timedelta(days=2), "发货仓库": "杭州仓", "物料编码": "BC001", "物料名称": "纸杯", "下单数量": 1800, "计划发货数量": 1800},
])

write("采购订单_演示.xlsx", [
    {"单据编号": "PO001", "交货日期": TODAY + timedelta(days=2), "仓库": "杭州仓", "物料编码": "BC001", "物料名称": "纸杯", "剩余入库数量": 1000, "单据状态": "已审核", "关闭状态": "未关闭"},
])

write("transfer_演示.xlsx", [
    {"调出仓库": "湖州仓", "调入仓库": "杭州仓", "物料编码": "YL00428", "物料名称": "鲜奶", "调拨数量": 1000, "预计发车时间": TODAY, "预计送达时间": TODAY + timedelta(days=1), "调拨状态": "运输中"},
])

print(f"已生成演示文件：{OUTPUT}")
