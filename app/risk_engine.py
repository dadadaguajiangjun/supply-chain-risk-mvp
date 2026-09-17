from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from .excel_service import (
    ParsedWorkbook,
    date_series,
    normalize_warehouse_name,
    numeric_series,
    text_series,
)


KEYS = ["warehouse", "material_code"]
TRACE_LIMIT_PER_KIND = 100


def _json_value(value: object) -> object:
    if value is None or (not isinstance(value, (dict, list)) and pd.isna(value)):
        return None
    if isinstance(value, (pd.Timestamp, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, TypeError):
            pass
    return value


def _source_record(workbook: ParsedWorkbook, index: object, columns: list[str]) -> dict:
    row = workbook.dataframe.loc[index]
    record = {
        "source_file": workbook.filename,
        "sheet_name": str(workbook.sheet_name),
        "source_row": int(row.get("_source_row")) if pd.notna(row.get("_source_row")) else (
            int(index) + workbook.header_row + 2 if isinstance(index, int) else str(index)
        ),
    }
    for column in columns:
        if column in row.index:
            record[column] = _json_value(row[column])
    return record


def _append_trace(store: dict, key: tuple[str, str], kind: str, record: dict) -> None:
    bucket = store.setdefault(key, {}).setdefault(kind, [])
    if len(bucket) < TRACE_LIMIT_PER_KIND:
        bucket.append(record)


def _build_trace_maps(
    workbooks: dict[str, ParsedWorkbook], business_date: date
) -> dict[tuple[str, str], dict]:
    traces: dict[tuple[str, str], dict] = {}
    stock = workbooks.get("stock")
    if stock is not None:
        for index, row in stock.dataframe.iterrows():
            key = (
                normalize_warehouse_name(row.get("warehouse", "")),
                str(row.get("material_code", "") or "").strip(),
            )
            if all(key):
                _append_trace(traces, key, "stock_components", _source_record(
                    stock, index,
                    ["warehouse", "warehouse_code", "material_code", "material_name", "stock_status",
                     "stock_qty", "allocated_qty", "frozen_qty", "transfer_in_transit_qty"],
                ))

    configurations = (
        ("booking_order", "warehouse", "delivery_date", "demand", ["order_no", "delivery_date", "demand_qty"]),
        ("purchase_order", "warehouse", "delivery_date", "purchase", ["order_no", "delivery_date", "remaining_qty", "order_status", "close_status"]),
    )
    valid_dates = {pd.Timestamp(business_date + timedelta(days=offset)) for offset in (1, 2)}
    for file_type, warehouse_column, date_column, kind, columns in configurations:
        workbook = workbooks.get(file_type)
        if workbook is None:
            continue
        dates = date_series(workbook.dataframe, date_column)
        for index, row in workbook.dataframe.loc[dates.isin(valid_dates)].iterrows():
            key = (
                normalize_warehouse_name(row.get(warehouse_column, "")),
                str(row.get("material_code", "") or "").strip(),
            )
            if all(key):
                _append_trace(traces, key, kind, _source_record(
                    workbook, index, [warehouse_column, "material_code", "material_name", *columns],
                ))

    transfer = workbooks.get("transfer")
    if transfer is not None:
        dates = date_series(transfer.dataframe, "arrival_date")
        for index, row in transfer.dataframe.loc[dates.isin(valid_dates)].iterrows():
            material = str(row.get("material_code", "") or "").strip()
            record = _source_record(
                transfer, index,
                ["order_no", "from_warehouse", "to_warehouse", "material_code", "material_name",
                 "transfer_qty", "departure_date", "arrival_date", "transfer_status"],
            )
            for warehouse_column, kind in (("to_warehouse", "transfer_in"), ("from_warehouse", "transfer_out")):
                key = (normalize_warehouse_name(row.get(warehouse_column, "")), material)
                if all(key):
                    _append_trace(traces, key, kind, record)
    return traces


def _base_columns(frame: pd.DataFrame) -> pd.DataFrame:
    result = pd.DataFrame(index=frame.index)
    result["warehouse"] = text_series(frame, "warehouse").map(normalize_warehouse_name)
    result["material_code"] = text_series(frame, "material_code")
    result["material_name"] = text_series(frame, "material_name")
    return result


def _clean_keys(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[(frame["warehouse"] != "") & (frame["material_code"] != "")].copy()


def aggregate_stock(frame: pd.DataFrame) -> pd.DataFrame:
    base = _base_columns(frame)
    if "stock_status" in frame.columns:
        status = text_series(frame, "stock_status").str.upper()
        keep = status.eq("") | status.eq("ZP")
        frame, base = frame.loc[keep].copy(), base.loc[keep].copy()
    base["current_stock"] = numeric_series(frame, "stock_qty")
    base["allocated_qty"] = numeric_series(frame, "allocated_qty")
    base["frozen_qty"] = numeric_series(frame, "frozen_qty")
    base = _clean_keys(base)
    return base.groupby(KEYS, as_index=False).agg(
        material_name=("material_name", "first"),
        current_stock=("current_stock", "sum"),
        allocated_qty=("allocated_qty", "sum"),
        frozen_qty=("frozen_qty", "sum"),
    )


def aggregate_by_date(
    frame: pd.DataFrame,
    business_date: date,
    date_column: str,
    qty_column: str,
    prefix: str,
) -> pd.DataFrame:
    base = _base_columns(frame)
    base["event_date"] = date_series(frame, date_column)
    base["qty"] = numeric_series(frame, qty_column)
    base = _clean_keys(base)
    records = []
    for offset in (1, 2):
        target = pd.Timestamp(business_date + timedelta(days=offset))
        group = (
            base.loc[base["event_date"].eq(target)]
            .groupby(KEYS, as_index=False)["qty"]
            .sum()
            .rename(columns={"qty": f"t{offset}_{prefix}"})
        )
        records.append(group)
    if not records:
        return pd.DataFrame(columns=KEYS)
    return records[0].merge(records[1], how="outer", on=KEYS)


def aggregate_purchase(frame: pd.DataFrame, business_date: date) -> pd.DataFrame:
    keep = pd.Series(True, index=frame.index)
    if "close_status" in frame.columns:
        closed = text_series(frame, "close_status").str.lower()
        keep &= ~closed.isin({"已关闭", "关闭", "是", "true", "1"})
    if "order_status" in frame.columns:
        status = text_series(frame, "order_status").str.lower()
        keep &= ~status.isin({"作废", "已取消", "取消"})
    return aggregate_by_date(frame.loc[keep], business_date, "delivery_date", "remaining_qty", "purchase")


def aggregate_transfer(frame: pd.DataFrame, business_date: date) -> tuple[pd.DataFrame, pd.DataFrame]:
    work = frame.copy()
    if "transfer_status" in work.columns:
        status = text_series(work, "transfer_status").str.lower()
        work = work.loc[~status.isin({"已取消", "取消", "作废"})].copy()
    dates = date_series(work, "arrival_date")
    quantities = numeric_series(work, "transfer_qty")
    material_codes = text_series(work, "material_code")
    material_names = text_series(work, "material_name")

    def build(warehouse_column: str, prefix: str) -> pd.DataFrame:
        base = pd.DataFrame({
            "warehouse": text_series(work, warehouse_column).map(normalize_warehouse_name),
            "material_code": material_codes,
            "material_name": material_names,
            "event_date": dates,
            "qty": quantities,
        })
        base = _clean_keys(base)
        parts = []
        for offset in (1, 2):
            target = pd.Timestamp(business_date + timedelta(days=offset))
            grouped = (
                base.loc[base["event_date"].eq(target)]
                .groupby(KEYS, as_index=False)["qty"]
                .sum()
                .rename(columns={"qty": f"t{offset}_{prefix}"})
            )
            parts.append(grouped)
        return parts[0].merge(parts[1], how="outer", on=KEYS)

    return build("to_warehouse", "transfer_in"), build("from_warehouse", "transfer_out")


def _merge_metric(base: pd.DataFrame, metric: pd.DataFrame) -> pd.DataFrame:
    if metric.empty:
        return base
    return base.merge(metric, how="outer", on=KEYS)


def calculate_risk(
    workbooks: dict[str, ParsedWorkbook],
    business_date: date,
    safety_days: float = 1.0,
) -> list[dict]:
    if "stock" not in workbooks:
        raise ValueError("缺少库存余量文件，无法计算风险。")
    if "booking_order" not in workbooks:
        raise ValueError("缺少报货订单实时文件，无法计算T+1/T+2需求。")

    trace_maps = _build_trace_maps(workbooks, business_date)
    result = aggregate_stock(workbooks["stock"].dataframe)

    demand = aggregate_by_date(
        workbooks["booking_order"].dataframe,
        business_date,
        "delivery_date",
        "demand_qty",
        "demand",
    )
    result = _merge_metric(result, demand)

    if "purchase_order" in workbooks:
        result = _merge_metric(result, aggregate_purchase(workbooks["purchase_order"].dataframe, business_date))

    if "transfer" in workbooks:
        transfer_in, transfer_out = aggregate_transfer(workbooks["transfer"].dataframe, business_date)
        result = _merge_metric(result, transfer_in)
        result = _merge_metric(result, transfer_out)

    # Recover a readable material name from any input if stock did not contain it.
    name_candidates: list[pd.DataFrame] = []
    for workbook in workbooks.values():
        frame = workbook.dataframe
        if not {"material_code", "material_name"}.issubset(frame.columns):
            continue
        candidate = pd.DataFrame({
            "material_code": text_series(frame, "material_code"),
            "material_name": text_series(frame, "material_name"),
        })
        candidate = candidate[candidate["material_code"].ne("") & candidate["material_name"].ne("")]
        name_candidates.append(candidate)
    if name_candidates:
        name_map = (
            pd.concat(name_candidates, ignore_index=True)
            .drop_duplicates("material_code")
            .set_index("material_code")["material_name"]
        )
        if "material_name" not in result.columns:
            result["material_name"] = ""
        result["material_name"] = result["material_name"].fillna("")
        result["material_name"] = result["material_name"].mask(
            result["material_name"].eq(""), result["material_code"].map(name_map).fillna("")
        )
    elif "material_name" not in result.columns:
        result["material_name"] = ""

    numeric_columns = [
        "current_stock", "allocated_qty", "frozen_qty", "t1_demand", "t2_demand",
        "t1_purchase", "t2_purchase", "t1_transfer_in", "t2_transfer_in",
        "t1_transfer_out", "t2_transfer_out",
    ]
    for column in numeric_columns:
        if column not in result.columns:
            result[column] = 0.0
        result[column] = pd.to_numeric(result[column], errors="coerce").fillna(0.0)

    result["available_stock"] = (result["current_stock"] - result["allocated_qty"] - result["frozen_qty"]).clip(lower=0)
    result["t1_projected_stock"] = (
        result["available_stock"] + result["t1_purchase"] + result["t1_transfer_in"]
        - result["t1_demand"] - result["t1_transfer_out"]
    )
    result["t2_projected_stock"] = (
        result["t1_projected_stock"] + result["t2_purchase"] + result["t2_transfer_in"]
        - result["t2_demand"] - result["t2_transfer_out"]
    )
    result["safety_stock"] = result[["t1_demand", "t2_demand"]].mean(axis=1) * float(safety_days)

    def classify(row: pd.Series) -> tuple[str, str | None, str]:
        t1_date = (business_date + timedelta(days=1)).isoformat()
        t2_date = (business_date + timedelta(days=2)).isoformat()
        if row["t1_projected_stock"] < 0:
            return "红色", t1_date, f"T+1预计缺口{abs(row['t1_projected_stock']):.2f}"
        if row["t2_projected_stock"] < 0:
            return "红色", t2_date, f"T+2预计缺口{abs(row['t2_projected_stock']):.2f}"
        if min(row["t1_projected_stock"], row["t2_projected_stock"]) < row["safety_stock"]:
            return "黄色", None, "预计库存未缺货，但将跌破1天安全库存"
        return "绿色", None, "T+1/T+2预计库存高于安全库存"

    classifications = result.apply(classify, axis=1, result_type="expand")
    result[["risk_level", "shortage_date", "risk_reason"]] = classifications
    order = pd.Categorical(result["risk_level"], categories=["红色", "黄色", "绿色"], ordered=True)
    result = result.assign(_risk_order=order).sort_values(["_risk_order", "t2_projected_stock", "warehouse", "material_code"])
    result = result.drop(columns=["_risk_order"])

    float_columns = numeric_columns + ["available_stock", "t1_projected_stock", "t2_projected_stock", "safety_stock"]
    result[float_columns] = result[float_columns].round(2)
    result["_trace"] = [
        trace_maps.get((row["warehouse"], row["material_code"]), {})
        for _, row in result.iterrows()
    ]
    clean_result = result.astype(object).where(pd.notna(result), None)
    return clean_result.to_dict(orient="records")
