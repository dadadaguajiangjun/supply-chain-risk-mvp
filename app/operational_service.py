from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import Any

import pandas as pd

from .database import db_connection
from .excel_service import ParsedWorkbook, normalize_warehouse_name
from .postgres_store import mirror_rows
from .planning_service import planning_dashboard


def _text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _number(value: Any) -> float:
    if value is None or pd.isna(value):
        return 0.0
    parsed = pd.to_numeric(str(value).replace(",", "").strip(), errors="coerce")
    return 0.0 if pd.isna(parsed) else float(parsed)


def _datetime_text(value: Any) -> str | None:
    if value is None or pd.isna(value) or _text(value) == "":
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    if parsed.hour or parsed.minute or parsed.second:
        return parsed.isoformat(sep=" ", timespec="seconds")
    return parsed.date().isoformat()


def _key(*parts: Any) -> str:
    normalized = "|".join(_text(part) for part in parts)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _upsert_many(table: str, columns: list[str], rows: list[tuple[Any, ...]], conflict: str = "source_key") -> None:
    if not rows:
        return
    update_columns = [column for column in columns if column != conflict]
    sql = (
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
        f"ON CONFLICT({conflict}) DO UPDATE SET "
        + ",".join(f"{column}=excluded.{column}" for column in update_columns)
        + ",updated_at=CURRENT_TIMESTAMP"
    )
    with db_connection() as conn:
        conn.executemany(sql, rows)
    mirror_rows(table, columns, rows, conflict)


def _upsert_materials(items: dict[str, dict[str, Any]]) -> None:
    if not items:
        return
    columns = [
        "material_code", "material_name", "material_category", "unit", "packaging_code",
        "latest_pack_qty", "volume", "weight", "source_system",
    ]
    sql = """INSERT INTO material_master
             (material_code,material_name,material_category,unit,packaging_code,
              latest_pack_qty,volume,weight,source_system)
             VALUES (?,?,?,?,?,?,?,?,?)
             ON CONFLICT(material_code) DO UPDATE SET
               material_name=COALESCE(NULLIF(excluded.material_name,''),material_master.material_name),
               material_category=COALESCE(NULLIF(excluded.material_category,''),material_master.material_category),
               unit=COALESCE(NULLIF(excluded.unit,''),material_master.unit),
               packaging_code=COALESCE(NULLIF(excluded.packaging_code,''),material_master.packaging_code),
               latest_pack_qty=COALESCE(excluded.latest_pack_qty,material_master.latest_pack_qty),
               volume=COALESCE(excluded.volume,material_master.volume),
               weight=COALESCE(excluded.weight,material_master.weight),
               source_system=excluded.source_system,updated_at=CURRENT_TIMESTAMP
             WHERE material_master.source_system NOT IN ('manual','master_import')
                OR material_master.source_system IS NULL"""
    rows = [tuple(item.get(column) for column in columns) for item in items.values()]
    with db_connection() as conn:
        conn.executemany(sql, rows)


def _upsert_warehouses(items: dict[str, dict[str, Any]]) -> None:
    if not items:
        return
    sql = """INSERT INTO warehouse_master
             (warehouse_key,warehouse_code,warehouse_name,owner,source_system)
             VALUES (?,?,?,?,?)
             ON CONFLICT(warehouse_key) DO UPDATE SET
               warehouse_code=COALESCE(NULLIF(excluded.warehouse_code,''),warehouse_master.warehouse_code),
               warehouse_name=COALESCE(NULLIF(excluded.warehouse_name,''),warehouse_master.warehouse_name),
               owner=COALESCE(NULLIF(excluded.owner,''),warehouse_master.owner),
               source_system=excluded.source_system,updated_at=CURRENT_TIMESTAMP
             WHERE warehouse_master.source_system NOT IN ('manual','master_import')
                OR warehouse_master.source_system IS NULL"""
    with db_connection() as conn:
        conn.executemany(sql, [
            (key, item["warehouse_code"], item["warehouse_name"], item["owner"], item["source_system"])
            for key, item in items.items()
        ])


def _upsert_suppliers(items: dict[str, dict[str, Any]]) -> None:
    if not items:
        return
    sql = """INSERT INTO supplier_master
             (supplier_key,supplier_code,supplier_name,source_system)
             VALUES (?,?,?,?)
             ON CONFLICT(supplier_key) DO UPDATE SET
               supplier_code=COALESCE(NULLIF(excluded.supplier_code,''),supplier_master.supplier_code),
               supplier_name=COALESCE(NULLIF(excluded.supplier_name,''),supplier_master.supplier_name),
               source_system=excluded.source_system,updated_at=CURRENT_TIMESTAMP
             WHERE supplier_master.source_system NOT IN ('manual','master_import')
                OR supplier_master.source_system IS NULL"""
    with db_connection() as conn:
        conn.executemany(sql, [
            (key, item["supplier_code"], item["supplier_name"], item["source_system"])
            for key, item in items.items()
        ])


def persist_operational_data(
    run_id: str,
    business_date: date,
    workbooks: dict[str, ParsedWorkbook],
) -> None:
    materials: dict[str, dict[str, Any]] = {}
    warehouses: dict[str, dict[str, Any]] = {}
    suppliers: dict[str, dict[str, Any]] = {}

    def collect_master(frame: pd.DataFrame, source: str) -> None:
        for _, row in frame.iterrows():
            material_code = _text(row.get("material_code"))
            if material_code:
                materials[material_code] = {
                    "material_code": material_code,
                    "material_name": _text(row.get("material_name")),
                    "material_category": _text(row.get("material_category")),
                    "unit": _text(row.get("unit")),
                    "packaging_code": _text(row.get("packaging_code")),
                    "latest_pack_qty": _number(row.get("latest_pack_qty")) if _text(row.get("latest_pack_qty")) else None,
                    "volume": _number(row.get("volume")) if _text(row.get("volume")) else None,
                    "weight": _number(row.get("weight")) if _text(row.get("weight")) else None,
                    "source_system": source,
                }
            for name_column, code_column in (
                ("warehouse", "warehouse_code"), ("from_warehouse", "from_warehouse_code"),
                ("to_warehouse", "to_warehouse_code"),
            ):
                name = _text(row.get(name_column))
                code = _text(row.get(code_column))
                if name or code:
                    normalized_name = normalize_warehouse_name(name) if name else code
                    warehouse_key = code or normalized_name
                    warehouses[warehouse_key] = {
                        "warehouse_code": code, "warehouse_name": normalized_name,
                        "owner": _text(row.get("owner")), "source_system": source,
                    }
            supplier_name = _text(row.get("supplier_name"))
            supplier_code = _text(row.get("supplier_code"))
            if supplier_name or supplier_code:
                supplier_key = supplier_code or supplier_name
                suppliers[supplier_key] = {
                    "supplier_code": supplier_code,
                    "supplier_name": supplier_name or supplier_code,
                    "source_system": source,
                }

    for file_type, workbook in workbooks.items():
        frame = workbook.dataframe
        collect_master(frame, file_type)

        if file_type == "purchase_order":
            columns = [
                "source_key", "last_run_id", "order_no", "purchase_date", "supplier_name",
                "order_status", "purchase_org", "close_status", "material_code", "material_name",
                "purchase_qty", "delivery_date", "remaining_qty", "warehouse", "remark",
            ]
            rows = []
            for _, row in frame.iterrows():
                order_no = _text(row.get("order_no"))
                purchase_date = _datetime_text(row.get("source_date"))
                supplier = _text(row.get("supplier_name"))
                warehouse = normalize_warehouse_name(row.get("warehouse", ""))
                material = _text(row.get("material_code"))
                source_key = _key(order_no or purchase_date, supplier, warehouse, material)
                rows.append((
                    source_key, run_id, order_no, purchase_date, supplier,
                    _text(row.get("order_status")), _text(row.get("purchase_org")),
                    _text(row.get("close_status")), material, _text(row.get("material_name")),
                    _number(row.get("purchase_qty")), _datetime_text(row.get("delivery_date")),
                    _number(row.get("remaining_qty")), warehouse, "",
                ))
            _upsert_many("purchase_record", columns, rows)

        elif file_type == "inbound_daily":
            columns = [
                "source_key", "last_run_id", "warehouse_code", "warehouse_name", "production_date",
                "expected_arrival_time", "inbound_date", "product_status", "asn_no", "asn_type",
                "notice_type", "external_order_no", "reference_no3", "expected_qty", "received_qty",
                "tracking_no", "transaction_no", "supplier_code", "supplier_name", "upstream_warehouse",
                "location", "zone", "expiry_date", "packaging_code", "shipment_remark",
                "material_code", "material_name", "source_system",
            ]
            rows = []
            for index, row in frame.iterrows():
                transaction = _text(row.get("transaction_no"))
                reference = _text(row.get("reference_no3"))
                material = _text(row.get("material_code"))
                source_row = row.get("_source_row", index)
                source_key = _key(transaction or _text(row.get("tracking_no")) or source_row, reference, material)
                rows.append((
                    source_key, run_id, _text(row.get("warehouse_code")), normalize_warehouse_name(row.get("warehouse", "")),
                    _datetime_text(row.get("production_date")), _datetime_text(row.get("expected_arrival_date")),
                    _datetime_text(row.get("inbound_date")), _text(row.get("product_status")),
                    _text(row.get("asn_no")), _text(row.get("asn_type")), _text(row.get("notice_type")),
                    _text(row.get("external_order_no")), reference, _number(row.get("expected_qty")),
                    _number(row.get("inbound_qty")), _text(row.get("tracking_no")), transaction,
                    _text(row.get("supplier_code")), _text(row.get("supplier_name")),
                    normalize_warehouse_name(row.get("upstream_warehouse", "")), _text(row.get("location")),
                    _text(row.get("zone")), _datetime_text(row.get("expiry_date")),
                    _text(row.get("packaging_code")), _text(row.get("shipment_remark")), material,
                    _text(row.get("material_name")), "FLUX",
                ))
            _upsert_many("inbound_record", columns, rows)

        elif file_type == "stock":
            from .data_quality import infer_file_date
            file_snapshot_date, _, _ = infer_file_date(workbook)
            columns = [
                "source_key", "run_id", "business_date", "warehouse_code", "warehouse_name", "stock_status",
                "owner", "unit", "freeze_type", "freeze_reason", "allocated_qty", "frozen_qty", "qa_status",
                "material_name", "material_code", "transfer_in_transit_qty", "purchase_in_transit_qty",
                "stock_qty", "supplier_id", "purchase_inbound_date", "expiry_date", "production_date", "sale_status", "snapshot_date",
            ]
            rows = []
            for index, row in frame.iterrows():
                warehouse_code = _text(row.get("warehouse_code"))
                warehouse = normalize_warehouse_name(row.get("warehouse", ""))
                material = _text(row.get("material_code"))
                status = _text(row.get("stock_status"))
                supplier = _text(row.get("supplier_id"))
                source_row = row.get("_source_row", index)
                source_key = _key(business_date.isoformat(), workbook.filename, source_row, warehouse_code or warehouse, material, status, supplier)
                rows.append((
                    source_key, run_id, business_date.isoformat(), warehouse_code, warehouse, status,
                    _text(row.get("owner")), _text(row.get("unit")), _text(row.get("freeze_type")),
                    _text(row.get("freeze_reason")), _number(row.get("allocated_qty")), _number(row.get("frozen_qty")),
                    _text(row.get("qa_status")), _text(row.get("material_name")), material,
                    _number(row.get("transfer_in_transit_qty")), _number(row.get("purchase_in_transit_qty")),
                    _number(row.get("stock_qty")), supplier, _datetime_text(row.get("purchase_inbound_date")),
                    _datetime_text(row.get("expiry_date")), _datetime_text(row.get("production_date")),
                    _text(row.get("sale_status")),
                    _datetime_text(row.get('snapshot_date')) or (file_snapshot_date.isoformat() if file_snapshot_date else None),
                ))
            _upsert_many("stock_snapshot", columns, rows)

        elif file_type == "transfer":
            columns = [
                "source_key", "last_run_id", "request_no", "document_type", "original_transfer_no",
                "trunk_plan_no", "document_status", "transport_status", "wms_outbound_no", "wms_inbound_no",
                "business_type", "transportation_mode", "from_warehouse", "to_warehouse", "material_code",
                "material_name", "minimum_unit_demand_qty", "demand_qty", "converted_demand_qty", "planned_qty",
                "converted_planned_qty", "outbound_qty", "inbound_qty", "request_time", "expected_departure_time",
                "actual_departure_time", "expected_arrival_time", "actual_arrival_time", "line_remark", "material_line_id",
            ]
            rows = []
            route_rows = []
            for index, row in frame.iterrows():
                request_no = _text(row.get("order_no"))
                material_line_id = _text(row.get("material_line_id"))
                material = _text(row.get("material_code"))
                source_key = _key(material_line_id or request_no or row.get("_source_row", index), material)
                from_warehouse = normalize_warehouse_name(row.get("from_warehouse", ""))
                to_warehouse = normalize_warehouse_name(row.get("to_warehouse", ""))
                transportation_mode = _text(row.get("transportation_mode"))
                business_type = _text(row.get("business_type"))
                rows.append((
                    source_key, run_id, request_no, _text(row.get("document_type")),
                    _text(row.get("original_transfer_no")), _text(row.get("trunk_plan_no")),
                    _text(row.get("document_status")), _text(row.get("transport_status") or row.get("transfer_status")),
                    _text(row.get("wms_outbound_no")), _text(row.get("wms_inbound_no")), business_type,
                    transportation_mode, from_warehouse, to_warehouse, material, _text(row.get("material_name")),
                    _number(row.get("minimum_unit_demand_qty")), _number(row.get("demand_qty")),
                    _number(row.get("converted_demand_qty")), _number(row.get("transfer_qty")),
                    _number(row.get("converted_planned_qty")), _number(row.get("outbound_qty")),
                    _number(row.get("inbound_qty")), _datetime_text(row.get("request_time")),
                    _datetime_text(row.get("departure_date")), _datetime_text(row.get("actual_departure_date")),
                    _datetime_text(row.get("arrival_date")), _datetime_text(row.get("actual_arrival_date")),
                    _text(row.get("line_remark")), material_line_id,
                ))
                if from_warehouse and to_warehouse:
                    route_rows.append((
                        from_warehouse, to_warehouse, transportation_mode, business_type,
                        _datetime_text(row.get("departure_date")), _datetime_text(row.get("arrival_date")),
                    ))
            _upsert_many("transfer_record", columns, rows)
            with db_connection() as conn:
                conn.executemany(
                    """INSERT INTO transfer_route
                       (from_warehouse,to_warehouse,transportation_mode,business_type,planned_departure_time,planned_arrival_time)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(from_warehouse,to_warehouse,transportation_mode,business_type) DO UPDATE SET
                         planned_departure_time=excluded.planned_departure_time,
                         planned_arrival_time=excluded.planned_arrival_time,updated_at=CURRENT_TIMESTAMP""",
                    route_rows,
                )

        elif file_type == "booking_order":
            columns = [
                "source_key", "last_run_id", "demand_date", "warehouse_code", "warehouse_name",
                "material_code", "material_name", "ordered_qty", "saleable_qty",
            ]
            rows = []
            for index, row in frame.iterrows():
                demand_date = _datetime_text(row.get("delivery_date"))
                warehouse = normalize_warehouse_name(row.get("warehouse", ""))
                material = _text(row.get("material_code"))
                source_key = _key(workbook.filename, row.get("_source_row", index), demand_date, warehouse, material)
                rows.append((
                    source_key, run_id, demand_date, _text(row.get("warehouse_code")), warehouse,
                    material, _text(row.get("material_name")), _number(row.get("ordered_qty")),
                    _number(row.get("demand_qty")),
                ))
            _upsert_many("booking_demand", columns, rows)

        elif file_type == "outbound":
            columns = [
                "source_key", "last_run_id", "year_month", "stat_date", "stat_week", "period_range",
                "warehouse_name", "material_category", "material_name", "material_code", "outbound_type",
                "allocated_qty", "picked_qty", "allocated_amount", "picked_amount", "delivery_method",
                "material_group", "latest_pack_qty", "volume", "weight", "owner_name", "order_source",
                "order_count", "required_delivery_time",
            ]
            rows = []
            for index, row in frame.iterrows():
                stat_date = _datetime_text(row.get("stat_date"))
                warehouse = normalize_warehouse_name(row.get("warehouse", ""))
                material = _text(row.get("material_code"))
                source_key = _key(
                    workbook.filename, row.get("_source_row", index), stat_date, warehouse, material,
                    _text(row.get("outbound_type")), _text(row.get("order_source")),
                )
                rows.append((
                    source_key, run_id, _text(row.get("year_month")), stat_date, _text(row.get("stat_week")),
                    _text(row.get("period_range")), warehouse, _text(row.get("material_category")),
                    _text(row.get("material_name")), material, _text(row.get("outbound_type")),
                    _number(row.get("allocated_qty")), _number(row.get("picked_qty")),
                    _number(row.get("allocated_amount")), _number(row.get("picked_amount")),
                    _text(row.get("delivery_method")), _text(row.get("material_group")),
                    _number(row.get("latest_pack_qty")), _number(row.get("volume")), _number(row.get("weight")),
                    _text(row.get("owner_name")), _text(row.get("order_source")), _number(row.get("order_count")),
                    _datetime_text(row.get("required_delivery_time")),
                ))
            _upsert_many("demand_history", columns, rows)

    _upsert_materials(materials)
    _upsert_warehouses(warehouses)
    _upsert_suppliers(suppliers)




def purchase_reconciliation(base_date: date, days: int = 7) -> list[dict[str, Any]]:
    from .planning_service import operational_history
    start = (base_date - timedelta(days=days)).isoformat()
    end = (base_date + timedelta(days=days)).isoformat()
    rows = [r for r in operational_history('purchase') if r['source'] == 'formal'
            and start <= str(r.get('delivery_date') or '')[:10] <= end]
    for row in rows:
        row['kingdee_received_qty'] = float(row.get('purchase_qty') or 0) - float(row.get('remaining_qty') or 0)
        row['quantity_matched'] = abs(row['received_difference']) < 0.000001
        dates = [r['inbound_date'] for r in row['wms_records'] if r.get('inbound_date')]
        row['first_inbound_date'] = min(dates, default=None)
        row['last_inbound_date'] = max(dates, default=None)
        row['arrival_on_time'] = bool(row['last_inbound_date'] and row.get('delivery_date')
                                      and str(row['last_inbound_date'])[:10] <= str(row['delivery_date'])[:10])
    return rows
