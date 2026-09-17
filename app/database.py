from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "risk_mvp.db"


def initialize_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS upload_run (
                id TEXT PRIMARY KEY,
                business_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT
            );

            CREATE TABLE IF NOT EXISTS uploaded_file (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                original_name TEXT NOT NULL,
                detected_type TEXT,
                row_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                message TEXT,
                FOREIGN KEY(run_id) REFERENCES upload_run(id)
            );

            CREATE TABLE IF NOT EXISTS risk_result (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                warehouse TEXT NOT NULL,
                material_code TEXT NOT NULL,
                material_name TEXT,
                current_stock REAL NOT NULL,
                allocated_qty REAL NOT NULL,
                frozen_qty REAL NOT NULL,
                available_stock REAL NOT NULL,
                t1_demand REAL NOT NULL,
                t1_purchase REAL NOT NULL,
                t1_transfer_in REAL NOT NULL,
                t1_transfer_out REAL NOT NULL,
                t1_projected_stock REAL NOT NULL,
                t2_demand REAL NOT NULL,
                t2_purchase REAL NOT NULL,
                t2_transfer_in REAL NOT NULL,
                t2_transfer_out REAL NOT NULL,
                t2_projected_stock REAL NOT NULL,
                safety_stock REAL NOT NULL,
                shortage_date TEXT,
                risk_level TEXT NOT NULL,
                risk_reason TEXT NOT NULL,
                details_json TEXT,
                FOREIGN KEY(run_id) REFERENCES upload_run(id)
            );

            CREATE INDEX IF NOT EXISTS idx_risk_run ON risk_result(run_id);
            CREATE INDEX IF NOT EXISTS idx_risk_level ON risk_result(risk_level);

            CREATE TABLE IF NOT EXISTS warehouse_mapping (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_system TEXT NOT NULL,
                raw_warehouse_code TEXT,
                raw_warehouse_name TEXT,
                standard_warehouse_code TEXT NOT NULL,
                standard_warehouse_name TEXT NOT NULL,
                special_alias TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                effective_start_date TEXT,
                effective_end_date TEXT,
                remark TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_warehouse_mapping_code
                ON warehouse_mapping(source_system,raw_warehouse_code,enabled);
            CREATE INDEX IF NOT EXISTS idx_warehouse_mapping_name
                ON warehouse_mapping(source_system,raw_warehouse_name,enabled);

            CREATE TABLE IF NOT EXISTS data_quality_report (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                file_type TEXT,
                filename TEXT NOT NULL,
                sheet_name TEXT,
                original_rows INTEGER NOT NULL DEFAULT 0,
                valid_rows INTEGER NOT NULL DEFAULT 0,
                deleted_rows INTEGER NOT NULL DEFAULT 0,
                required_missing_count INTEGER NOT NULL DEFAULT 0,
                date_parse_failure_count INTEGER NOT NULL DEFAULT 0,
                numeric_parse_failure_count INTEGER NOT NULL DEFAULT 0,
                duplicate_count INTEGER NOT NULL DEFAULT 0,
                unmatched_warehouse_count INTEGER NOT NULL DEFAULT 0,
                unmatched_material_count INTEGER NOT NULL DEFAULT 0,
                negative_count INTEGER NOT NULL DEFAULT 0,
                file_date TEXT,
                file_date_source TEXT,
                file_date_status TEXT,
                calculation_allowed INTEGER NOT NULL DEFAULT 1,
                details_json TEXT,
                FOREIGN KEY(run_id) REFERENCES upload_run(id)
            );

            CREATE TABLE IF NOT EXISTS unmatched_warehouse (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                source_system TEXT NOT NULL,
                source_file TEXT NOT NULL,
                raw_warehouse_code TEXT,
                raw_warehouse_name TEXT,
                record_count INTEGER NOT NULL DEFAULT 0,
                suggested_standard_name TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                UNIQUE(run_id,source_system,source_file,raw_warehouse_code,raw_warehouse_name)
            );

            CREATE TABLE IF NOT EXISTS material_master (
                material_code TEXT PRIMARY KEY,
                material_name TEXT,
                material_category TEXT,
                unit TEXT,
                packaging_code TEXT,
                latest_pack_qty REAL,
                volume REAL,
                weight REAL,
                source_system TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS material_control (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                material_owner TEXT NOT NULL,
                purchase_owner TEXT,
                material_code TEXT NOT NULL,
                material_name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(material_owner,material_code)
            );
            CREATE TABLE IF NOT EXISTS execution_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity TEXT NOT NULL,
                record_id INTEGER NOT NULL,
                execution_status TEXT NOT NULL,
                execution_note TEXT,
                operator TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS supplier_master (
                supplier_key TEXT PRIMARY KEY,
                supplier_code TEXT,
                supplier_name TEXT NOT NULL,
                arrival_mode TEXT,
                loading_qty REAL,
                vehicle_type TEXT,
                packaging_basis TEXT,
                transportation_mode TEXT,
                remark TEXT,
                source_system TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS warehouse_master (
                warehouse_key TEXT PRIMARY KEY,
                warehouse_code TEXT,
                warehouse_name TEXT NOT NULL,
                owner TEXT,
                source_system TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS transfer_route (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_warehouse TEXT NOT NULL,
                to_warehouse TEXT NOT NULL,
                transportation_mode TEXT,
                business_type TEXT,
                planned_departure_time TEXT,
                planned_arrival_time TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                remark TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(from_warehouse,to_warehouse,transportation_mode,business_type)
            );

            CREATE TABLE IF NOT EXISTS purchase_record (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                last_run_id TEXT NOT NULL,
                order_no TEXT,
                purchase_date TEXT,
                supplier_name TEXT,
                order_status TEXT,
                purchase_org TEXT,
                close_status TEXT,
                material_code TEXT,
                material_name TEXT,
                purchase_qty REAL NOT NULL DEFAULT 0,
                delivery_date TEXT,
                remaining_qty REAL NOT NULL DEFAULT 0,
                warehouse TEXT,
                remark TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS inbound_record (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                last_run_id TEXT NOT NULL,
                warehouse_code TEXT,
                warehouse_name TEXT,
                production_date TEXT,
                expected_arrival_time TEXT,
                inbound_date TEXT,
                product_status TEXT,
                asn_no TEXT,
                asn_type TEXT,
                notice_type TEXT,
                external_order_no TEXT,
                reference_no3 TEXT,
                expected_qty REAL NOT NULL DEFAULT 0,
                received_qty REAL NOT NULL DEFAULT 0,
                tracking_no TEXT,
                transaction_no TEXT,
                supplier_code TEXT,
                supplier_name TEXT,
                upstream_warehouse TEXT,
                location TEXT,
                zone TEXT,
                expiry_date TEXT,
                packaging_code TEXT,
                shipment_remark TEXT,
                material_code TEXT,
                material_name TEXT,
                source_system TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS stock_snapshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                business_date TEXT NOT NULL,
                warehouse_code TEXT,
                warehouse_name TEXT,
                stock_status TEXT,
                owner TEXT,
                unit TEXT,
                freeze_type TEXT,
                freeze_reason TEXT,
                allocated_qty REAL NOT NULL DEFAULT 0,
                frozen_qty REAL NOT NULL DEFAULT 0,
                qa_status TEXT,
                material_name TEXT,
                material_code TEXT,
                transfer_in_transit_qty REAL NOT NULL DEFAULT 0,
                purchase_in_transit_qty REAL NOT NULL DEFAULT 0,
                stock_qty REAL NOT NULL DEFAULT 0,
                supplier_id TEXT,
                purchase_inbound_date TEXT,
                expiry_date TEXT,
                production_date TEXT,
                sale_status TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_stock_snapshot_lookup
                ON stock_snapshot(business_date,warehouse_name,material_code);

            CREATE TABLE IF NOT EXISTS transfer_record (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                last_run_id TEXT NOT NULL,
                request_no TEXT,
                document_type TEXT,
                original_transfer_no TEXT,
                trunk_plan_no TEXT,
                document_status TEXT,
                transport_status TEXT,
                wms_outbound_no TEXT,
                wms_inbound_no TEXT,
                business_type TEXT,
                transportation_mode TEXT,
                from_warehouse TEXT,
                to_warehouse TEXT,
                material_code TEXT,
                material_name TEXT,
                minimum_unit_demand_qty REAL NOT NULL DEFAULT 0,
                demand_qty REAL NOT NULL DEFAULT 0,
                converted_demand_qty REAL NOT NULL DEFAULT 0,
                planned_qty REAL NOT NULL DEFAULT 0,
                converted_planned_qty REAL NOT NULL DEFAULT 0,
                outbound_qty REAL NOT NULL DEFAULT 0,
                inbound_qty REAL NOT NULL DEFAULT 0,
                request_time TEXT,
                expected_departure_time TEXT,
                actual_departure_time TEXT,
                expected_arrival_time TEXT,
                actual_arrival_time TEXT,
                line_remark TEXT,
                material_line_id TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS demand_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                last_run_id TEXT NOT NULL,
                year_month TEXT,
                stat_date TEXT,
                stat_week TEXT,
                period_range TEXT,
                warehouse_name TEXT,
                material_category TEXT,
                material_name TEXT,
                material_code TEXT,
                outbound_type TEXT,
                allocated_qty REAL NOT NULL DEFAULT 0,
                picked_qty REAL NOT NULL DEFAULT 0,
                allocated_amount REAL NOT NULL DEFAULT 0,
                picked_amount REAL NOT NULL DEFAULT 0,
                delivery_method TEXT,
                material_group TEXT,
                latest_pack_qty REAL,
                volume REAL,
                weight REAL,
                owner_name TEXT,
                order_source TEXT,
                order_count REAL NOT NULL DEFAULT 0,
                required_delivery_time TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_demand_history_lookup
                ON demand_history(stat_date,warehouse_name,material_code);

            CREATE TABLE IF NOT EXISTS booking_demand (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                last_run_id TEXT NOT NULL,
                demand_date TEXT,
                warehouse_code TEXT,
                warehouse_name TEXT,
                material_code TEXT,
                material_name TEXT,
                ordered_qty REAL NOT NULL DEFAULT 0,
                saleable_qty REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_booking_demand_lookup
                ON booking_demand(demand_date,warehouse_name,material_code);

            CREATE TABLE IF NOT EXISTS purchase_plan (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_name TEXT NOT NULL,
                material_code TEXT NOT NULL,
                material_name TEXT,
                order_qty REAL NOT NULL DEFAULT 0,
                arrival_warehouse TEXT NOT NULL,
                order_time TEXT,
                expected_arrival_time TEXT,
                supplier_stock_qty REAL NOT NULL DEFAULT 0,
                supplier_producible_qty REAL NOT NULL DEFAULT 0,
                supplier_estimated_qty REAL NOT NULL DEFAULT 0,
                status TEXT,
                remark TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS transfer_plan (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_no TEXT,
                from_warehouse TEXT NOT NULL,
                to_warehouse TEXT NOT NULL,
                material_code TEXT NOT NULL,
                material_name TEXT,
                demand_qty REAL NOT NULL DEFAULT 0,
                planned_qty REAL NOT NULL DEFAULT 0,
                document_status TEXT,
                transport_status TEXT,
                expected_departure_time TEXT,
                expected_arrival_time TEXT,
                remark TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        _ensure_column(conn, "upload_run", "result_mode", "TEXT NOT NULL DEFAULT 'official'")
        for table in ("purchase_plan", "transfer_plan", "purchase_record", "transfer_record"):
            _ensure_column(conn, table, "execution_status", "TEXT NOT NULL DEFAULT '未完成'")
            _ensure_column(conn, table, "execution_note", "TEXT")
            _ensure_column(conn, table, "operator", "TEXT")
            _ensure_column(conn, table, "status_updated_at", "TEXT")
        for table in ("purchase_plan", "transfer_plan"):
            _ensure_column(conn, table, "linked_order_no", "TEXT")
        _ensure_column(conn, "upload_run", "forced_calculation", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "upload_run", "stock_snapshot_date", "TEXT")
        _ensure_column(conn, "upload_run", "stock_date_status", "TEXT")
        _ensure_column(conn, "stock_snapshot", "snapshot_date", "TEXT")
        _ensure_column(conn, "material_master", "supply_time", "TEXT")
        _ensure_column(conn, "material_master", "minimum_package", "TEXT")
        _ensure_column(conn, "material_master", "package_content", "TEXT")
        _ensure_column(conn, "material_master", "case_pack", "TEXT")
        _ensure_column(conn, "material_master", "safety_stock", "REAL")
        _ensure_column(conn, "supplier_master", "material_code", "TEXT")
        _ensure_column(conn, "supplier_master", "material_name", "TEXT")
        _ensure_column(conn, "supplier_master", "warehouse", "TEXT")
        _ensure_column(conn, "supplier_master", "loading_package", "TEXT")
        _ensure_column(conn, "warehouse_master", "warehouse_english", "TEXT")
        _ensure_column(conn, "warehouse_master", "capacity", "TEXT")
        _ensure_column(conn, "transfer_route", "available_time", "TEXT")
        _ensure_column(conn, "transfer_route", "theoretical_departure_time", "TEXT")
        _ensure_column(conn, "transfer_route", "theoretical_arrival_time", "TEXT")
        _ensure_column(conn, "transfer_route", "time_requirement", "TEXT")
        conn.execute(
            "UPDATE warehouse_master SET warehouse_english=warehouse_code "
            "WHERE (warehouse_english IS NULL OR warehouse_english='') AND warehouse_code<>''"
        )
        conn.execute(
            "UPDATE supplier_master SET loading_package=packaging_basis "
            "WHERE (loading_package IS NULL OR loading_package='') AND packaging_basis<>''"
        )
        conn.execute(
            "UPDATE transfer_route SET theoretical_departure_time=planned_departure_time "
            "WHERE (theoretical_departure_time IS NULL OR theoretical_departure_time='') AND planned_departure_time<>''"
        )
        conn.execute(
            "UPDATE transfer_route SET theoretical_arrival_time=planned_arrival_time "
            "WHERE (theoretical_arrival_time IS NULL OR theoretical_arrival_time='') AND planned_arrival_time<>''"
        )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


@contextmanager
def db_connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_risk_rows(run_id: str, rows: list[dict]) -> None:
    if not rows:
        return
    columns = [
        "warehouse", "material_code", "material_name", "current_stock",
        "allocated_qty", "frozen_qty", "available_stock", "t1_demand",
        "t1_purchase", "t1_transfer_in", "t1_transfer_out",
        "t1_projected_stock", "t2_demand", "t2_purchase",
        "t2_transfer_in", "t2_transfer_out", "t2_projected_stock",
        "safety_stock", "shortage_date", "risk_level", "risk_reason",
    ]
    placeholders = ",".join("?" for _ in range(len(columns) + 2))
    sql = f"INSERT INTO risk_result (run_id,{','.join(columns)},details_json) VALUES ({placeholders})"
    payload = []
    for row in rows:
        details = {k: v for k, v in row.items() if k not in columns}
        payload.append([run_id, *[row.get(c) for c in columns], json.dumps(details, ensure_ascii=False, default=str)])
    with db_connection() as conn:
        conn.executemany(sql, payload)
