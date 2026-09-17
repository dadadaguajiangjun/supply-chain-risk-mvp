from __future__ import annotations

import io
import json
import sqlite3
import shutil
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
from fastapi import Body, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal

from .config import FILE_SPECS, TEMPLATE_COLUMNS
from .database import UPLOAD_DIR, db_connection, initialize_database, save_risk_rows
from .data_quality import QualityReport, add_unmatched_material_counts, clean_and_assess
from .excel_service import ParsedWorkbook, WorkbookValidationError, clean_column_name, parse_workbook
from .operational_service import persist_operational_data, planning_dashboard, purchase_reconciliation
from .planning_service import operational_history
from .postgres_store import initialize_postgres_storage, update_purchase_remark as update_postgres_purchase_remark
from .risk_engine import calculate_risk
from .warehouse_service import apply_warehouse_mappings
from .ui_pages import ui_version, workspace_page


ROOT_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_FILE_BYTES = 40 * 1024 * 1024
ALLOWED_SUFFIXES = {".xlsx", ".xls"}

app = FastAPI(title="供应链计划数据看板", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def revalidate_workspace_assets(request, call_next):
    response = await call_next(request)
    # Local deployments update in place; always validate cached UI files.
    if response.headers.get('content-type', '').startswith('text/html'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    elif request.url.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'no-cache, must-revalidate'
    return response


WAREHOUSE_MAPPING_COLUMNS = [
    "source_system", "raw_warehouse_code", "raw_warehouse_name",
    "standard_warehouse_code", "standard_warehouse_name", "special_alias",
    "enabled", "effective_start_date", "effective_end_date", "remark",
]


class WarehouseMappingInput(BaseModel):
    source_system: str
    raw_warehouse_code: str | None = None
    raw_warehouse_name: str | None = None
    standard_warehouse_code: str
    standard_warehouse_name: str
    special_alias: str | None = None
    enabled: bool = True
    effective_start_date: date | None = None
    effective_end_date: date | None = None
    remark: str | None = None


class PurchasePlanInput(BaseModel):
    linked_order_no: str | None = None
    supplier_name: str
    material_code: str
    material_name: str | None = None
    order_qty: float = Field(default=0, ge=0, allow_inf_nan=False)
    arrival_warehouse: str
    order_time: datetime | None = None
    expected_arrival_time: datetime | None = None
    supplier_stock_qty: float = Field(default=0, ge=0, allow_inf_nan=False)
    supplier_producible_qty: float = Field(default=0, ge=0, allow_inf_nan=False)
    supplier_estimated_qty: float = Field(default=0, ge=0, allow_inf_nan=False)
    status: str | None = None
    remark: str | None = None


class TransferPlanInput(BaseModel):
    linked_order_no: str | None = None
    request_no: str | None = None
    from_warehouse: str
    to_warehouse: str
    material_code: str
    material_name: str | None = None
    demand_qty: float = Field(default=0, ge=0, allow_inf_nan=False)
    planned_qty: float = Field(default=0, ge=0, allow_inf_nan=False)
    document_status: str | None = None
    transport_status: str | None = None
    expected_departure_time: datetime | None = None
    expected_arrival_time: datetime | None = None
    remark: str | None = None


class PurchaseRemarkInput(BaseModel):
    remark: str | None = None


class MaterialMasterInput(BaseModel):
    material_code: str
    material_name: str
    material_category: str | None = None
    supply_time: str | None = None
    unit: str | None = None
    minimum_package: str | None = None
    package_content: str | None = None
    case_pack: str | None = None
    volume: float | None = None
    weight: float | None = None
    safety_stock: float | None = None


class MaterialControlInput(BaseModel):
    material_owner: str
    purchase_owner: str | None = None
    material_code: str
    material_name: str
    enabled: bool = True


class ExecutionInput(BaseModel):
    execution_status: Literal['已完成', '延误', '未完成']
    execution_note: str = ''
    operator: str


class SupplierMasterInput(BaseModel):
    supplier_code: str
    supplier_name: str
    material_code: str
    material_name: str
    arrival_mode: str | None = None
    warehouse: str | None = None
    vehicle_type: str | None = None
    loading_qty: float | None = None
    loading_package: str | None = None


class WarehouseMasterInput(BaseModel):
    warehouse_english: str
    warehouse_name: str
    capacity: str | None = None


class TransferRouteInput(BaseModel):
    from_warehouse: str
    to_warehouse: str
    available_time: str | None = None
    theoretical_departure_time: str | None = None
    theoretical_arrival_time: str | None = None
    time_requirement: str | None = None


MASTER_DATA_DEFINITIONS = {
    "material-controls": {
        "columns": ["material_owner", "purchase_owner", "material_code", "material_name", "enabled"],
        "headers": ["物料负责人", "采购负责人", "物料编码", "物料名称", "启用"],
        "required": ["material_owner", "material_code", "material_name"],
    },
    "materials": {
        "columns": ["material_code", "material_name", "material_category", "supply_time", "unit", "minimum_package", "package_content", "case_pack", "volume", "weight", "safety_stock"],
        "headers": ["物料编码", "物料名称", "物料分类", "供应时间", "单位", "最小包装", "最小包装多少克/多少毫升", "箱规", "体积", "重量", "安全库存"],
        "required": ["material_code", "material_name"],
    },
    "suppliers": {
        "columns": ["supplier_code", "supplier_name", "material_code", "material_name", "arrival_mode", "warehouse", "vehicle_type", "loading_qty", "loading_package"],
        "headers": ["供应商编码", "名称", "物料编码", "物料名称", "到仓方式", "对应仓库", "车型", "装载数量", "装载箱/包"],
        "required": ["supplier_code", "supplier_name", "material_code", "material_name"],
    },
    "warehouses": {
        "columns": ["warehouse_english", "warehouse_name", "capacity"],
        "headers": ["仓库英文", "仓库名称", "仓库库容"],
        "required": ["warehouse_english", "warehouse_name"],
    },
    "transfer-routes": {
        "columns": ["from_warehouse", "to_warehouse", "available_time", "theoretical_departure_time", "theoretical_arrival_time", "time_requirement"],
        "headers": ["调出仓库", "调入仓库", "可用时间", "理论发车时间", "理论到货时间", "时间要求"],
        "required": ["from_warehouse", "to_warehouse"],
    },
}


def _model_values(item: WarehouseMappingInput) -> dict:
    values = item.model_dump() if hasattr(item, "model_dump") else item.dict()
    for key in ("effective_start_date", "effective_end_date"):
        if values.get(key):
            values[key] = values[key].isoformat()
    return values


def _load_mappings() -> list[dict]:
    with db_connection() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM warehouse_mapping ORDER BY id").fetchall()]


def _persist_quality(run_id: str, reports: list[QualityReport]) -> None:
    columns = [
        "file_type", "filename", "sheet_name", "original_rows", "valid_rows", "deleted_rows",
        "required_missing_count", "date_parse_failure_count", "numeric_parse_failure_count",
        "duplicate_count", "unmatched_warehouse_count", "unmatched_material_count", "negative_count",
        "file_date", "file_date_source", "file_date_status", "calculation_allowed", "details_json",
    ]
    sql = f"INSERT INTO data_quality_report (run_id,{','.join(columns)}) VALUES ({','.join('?' for _ in range(len(columns) + 1))})"
    payload = []
    for report in reports:
        item = report.to_dict()
        item["calculation_allowed"] = int(bool(item["calculation_allowed"]))
        item["details_json"] = json.dumps(item["details_json"], ensure_ascii=False, default=str)
        payload.append([run_id, *[item.get(column) for column in columns]])
    with db_connection() as conn:
        conn.executemany(sql, payload)


def _persist_unmatched(run_id: str, items: list[dict]) -> None:
    if not items:
        return
    with db_connection() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO unmatched_warehouse
               (run_id,source_system,source_file,raw_warehouse_code,raw_warehouse_name,
                record_count,suggested_standard_name,status)
               VALUES (?,?,?,?,?,?,?,'pending')""",
            [(
                run_id, item["source_system"], item["source_file"], item.get("raw_warehouse_code", ""),
                item.get("raw_warehouse_name", ""), item["record_count"],
                item.get("suggested_standard_name", ""),
            ) for item in items],
        )


@app.on_event("startup")
def on_startup() -> None:
    initialize_database()
    initialize_postgres_storage()


@app.get("/")
def home() -> HTMLResponse:
    return workspace_page("index.html")


@app.get("/master-data")
def master_data_page() -> HTMLResponse:
    return workspace_page("master-data.html")


@app.get('/data-import')
def data_import_page() -> HTMLResponse:
    return workspace_page('data-import.html')


@app.get("/purchase-reconciliation")
def purchase_reconciliation_page() -> HTMLResponse:
    return workspace_page("purchase-reconciliation.html")


@app.get("/data-edit")
def data_edit_page() -> HTMLResponse:
    return workspace_page("data-edit.html")


@app.get('/api/ui-version')
def get_ui_version() -> JSONResponse:
    return JSONResponse({'version': ui_version()}, headers={'Cache-Control': 'no-store'})


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/dashboard")
def dashboard(
    business_date: date | None = Query(default=None),
    run_status: str | None = Query(default=None),
) -> dict:
    """Return operational data-readiness metrics without risk interpretation."""
    quality_columns = [
        "original_rows", "valid_rows", "deleted_rows", "required_missing_count",
        "date_parse_failure_count", "numeric_parse_failure_count", "duplicate_count",
        "unmatched_warehouse_count", "unmatched_material_count", "negative_count",
    ]
    with db_connection() as conn:
        where = []
        params: list[object] = []
        if business_date:
            where.append("business_date=?")
            params.append(business_date.isoformat())
        if run_status:
            where.append("status=?")
            params.append(run_status)
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""
        latest_row = conn.execute(
            f"SELECT * FROM upload_run{where_sql} ORDER BY created_at DESC LIMIT 1", params
        ).fetchone()
        latest = dict(latest_row) if latest_row else None
        files = []
        quality = []
        unmatched_records = 0
        unmatched_items = []
        if latest:
            files = [dict(row) for row in conn.execute(
                "SELECT * FROM uploaded_file WHERE run_id=? ORDER BY id", (latest["id"],)
            ).fetchall()]
            quality = [dict(row) for row in conn.execute(
                "SELECT * FROM data_quality_report WHERE run_id=? ORDER BY id", (latest["id"],)
            ).fetchall()]
            unmatched_records = int(conn.execute(
                "SELECT COALESCE(SUM(record_count),0) FROM unmatched_warehouse WHERE run_id=?",
                (latest["id"],),
            ).fetchone()[0])
            unmatched_items = [dict(row) for row in conn.execute(
                """SELECT source_system,source_file,raw_warehouse_code,raw_warehouse_name,
                          record_count,suggested_standard_name,status
                   FROM unmatched_warehouse WHERE run_id=?
                   ORDER BY record_count DESC,id LIMIT 8""",
                (latest["id"],),
            ).fetchall()]

        mapping_counts = dict(conn.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END),0) AS enabled,
                      COALESCE(SUM(CASE WHEN enabled=0 THEN 1 ELSE 0 END),0) AS disabled
               FROM warehouse_mapping"""
        ).fetchone())
        recent_runs = [dict(row) for row in conn.execute(
            f"""SELECT r.id,r.business_date,r.created_at,r.status,r.result_mode,
                      r.stock_snapshot_date,r.stock_date_status,r.error_message,
                      (SELECT COUNT(*) FROM uploaded_file f WHERE f.run_id=r.id) AS file_count,
                      (SELECT COALESCE(SUM(q.valid_rows),0) FROM data_quality_report q WHERE q.run_id=r.id) AS valid_rows,
                      (SELECT COALESCE(SUM(u.record_count),0) FROM unmatched_warehouse u WHERE u.run_id=r.id) AS unmatched_warehouse_count
               FROM upload_run r{where_sql} ORDER BY r.created_at DESC LIMIT 20""", params
        ).fetchall()]
        filter_dates = [row[0] for row in conn.execute(
            "SELECT DISTINCT business_date FROM upload_run ORDER BY business_date DESC LIMIT 60"
        ).fetchall()]

    file_by_type = {
        item["detected_type"]: item for item in files
        if item.get("detected_type")
    }
    quality_by_type = {
        item["file_type"]: item for item in quality
        if item.get("file_type")
    }
    file_readiness = []
    for key, spec in FILE_SPECS.items():
        file_item = file_by_type.get(key)
        quality_item = quality_by_type.get(key)
        file_readiness.append({
            "file_type": key,
            "label": spec.label,
            "required": not spec.optional and key in {"stock", "booking_order"},
            "present": file_item is not None,
            "filename": file_item.get("original_name") if file_item else None,
            "row_count": int(file_item.get("row_count") or 0) if file_item else 0,
            "status": file_item.get("status") if file_item else "missing",
            "file_date": quality_item.get("file_date") if quality_item else None,
            "file_date_status": quality_item.get("file_date_status") if quality_item else None,
        })

    quality_totals = {
        column: sum(int(item.get(column) or 0) for item in quality)
        for column in quality_columns
    }
    issue_fields = [
        "required_missing_count", "date_parse_failure_count", "numeric_parse_failure_count",
        "duplicate_count", "unmatched_warehouse_count", "unmatched_material_count", "negative_count",
    ]
    quality_totals["issue_count"] = sum(quality_totals[field] for field in issue_fields)
    required_ready = all(
        file_by_type.get(key, {}).get("status") == "success"
        for key in ("stock", "booking_order")
    )
    data_ready = bool(
        required_ready and latest and latest.get("status") in {"validated", "completed"}
    )
    return {
        "latest_run": latest,
        "summary": {
            "data_ready": data_ready,
            "required_files_ready": required_ready,
            "file_count": len(files),
            "successful_file_count": sum(item.get("status") == "success" for item in files),
            "unmatched_warehouse_records": unmatched_records,
        },
        "quality_totals": quality_totals,
        "mapping": mapping_counts,
        "file_readiness": file_readiness,
        "recent_runs": recent_runs,
        "unmatched_warehouses": unmatched_items,
        "filters": {
            "business_dates": filter_dates,
            "statuses": ["validated", "completed", "blocked", "failed", "processing"],
            "selected_business_date": business_date.isoformat() if business_date else None,
            "selected_status": run_status,
        },
    }


@app.get("/api/planning-dashboard")
def get_planning_dashboard(
    business_date: date = Query(...),
    warehouse: str | None = Query(default=None),
    material_code: str | None = Query(default=None),
    material_owner: str | None = Query(default=None),
    purchase_owner: str | None = Query(default=None),
    supplier: str | None = Query(default=None),
    material_category: str | None = Query(default=None),
    transfer_area: str | None = Query(default=None),
    page: int | None = Query(default=None, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
    filters_version: str | None = Query(default=None),
    search_fields: list[Literal['warehouse', 'material_code', 'material_owner', 'purchase_owner', 'supplier', 'material_category', 'transfer_area']] = Query(default=[]),
) -> dict:
    return planning_dashboard(business_date, warehouse, material_code, material_owner,
                              purchase_owner, supplier, material_category, transfer_area, page, page_size, filters_version, search_fields)


def _paged_rows(sql: str, params: list, page: int | None, page_size: int) -> dict:
    with db_connection() as conn:
        conn.execute('BEGIN')
        total = conn.execute(f'SELECT COUNT(*) FROM ({sql})', params).fetchone()[0]
        if page is not None:
            sql += ' LIMIT ? OFFSET ?'
            params = [*params, page_size, (page-1)*page_size]
        rows = [dict(r) for r in conn.execute(sql, params)]
    return {'rows': rows, 'total': total, 'page': page or 1}


@app.get("/api/master-data/materials")
def list_material_master(search: str | None = Query(default=None), page: int | None = Query(default=None, ge=1),
                        page_size: int = Query(default=100, ge=1, le=500)) -> dict:
    sql = "SELECT * FROM material_master"
    params: list[object] = []
    if search:
        sql += " WHERE material_code LIKE ? OR material_name LIKE ? OR material_category LIKE ?"
        token = f"%{search}%"
        params.extend([token, token, token])
    sql += " ORDER BY material_code"
    return _paged_rows(sql, params, page, page_size)


def _input_values(item: BaseModel) -> dict:
    values = item.model_dump() if hasattr(item, "model_dump") else item.dict()
    return {key: (value.strip() if isinstance(value, str) else value) for key, value in values.items()}


def _validate_master_values(entity: str, values: dict) -> None:
    missing = [column for column in MASTER_DATA_DEFINITIONS[entity]["required"] if values.get(column) in {None, ""}]
    if missing:
        raise ValueError(f"必填字段不能为空：{'、'.join(missing)}")
    if entity == "materials" and values.get("material_category") not in {None, "", "短保", "长保", "限定"}:
        raise ValueError("物料分类只能填写：短保、长保、限定。")
    if entity == "suppliers" and values.get("arrival_mode") not in {None, "", "自提", "含运"}:
        raise ValueError("到仓方式只能填写：自提、含运。")


def _save_master_record(entity: str, values: dict, record_key: str | int | None = None) -> tuple[dict, bool]:
    _validate_master_values(entity, values)
    created = False
    with db_connection() as conn:
        if entity == "material-controls":
            enabled = values.get("enabled")
            if enabled is None or enabled == '':
                enabled = 1
            elif str(enabled).lower() in {'0', 'false', '否', '停用'}:
                enabled = 0
            elif str(enabled).lower() in {'1', 'true', '是', '启用'}:
                enabled = 1
            else:
                raise ValueError('启用应填写1/0或启用/停用')
            values['enabled'] = enabled
            found = conn.execute('SELECT id FROM material_control WHERE id=?', (record_key,)).fetchone() if record_key is not None else conn.execute(
                'SELECT id FROM material_control WHERE material_owner=? AND material_code=?',
                (values['material_owner'], values['material_code']),
            ).fetchone()
            columns = MASTER_DATA_DEFINITIONS[entity]['columns']
            if record_key is not None and not found:
                raise HTTPException(404, '未找到物料管控记录')
            if found:
                key = found['id']
                conn.execute(f"UPDATE material_control SET {','.join(c+'=?' for c in columns)},updated_at=CURRENT_TIMESTAMP WHERE id=?",
                             [*[values.get(c) for c in columns], key])
            else:
                key = conn.execute(f"INSERT INTO material_control ({','.join(columns)}) VALUES ({','.join('?' for c in columns)})",
                                   [values.get(c) for c in columns]).lastrowid
                created = True
            row = conn.execute('SELECT * FROM material_control WHERE id=?', (key,)).fetchone()
        elif entity == "materials":
            old_key = str(record_key) if record_key is not None else values["material_code"]
            exists = conn.execute("SELECT 1 FROM material_master WHERE material_code=?", (old_key,)).fetchone()
            if exists:
                assignments = ",".join(f"{column}=?" for column in MASTER_DATA_DEFINITIONS[entity]["columns"])
                conn.execute(
                    f"UPDATE material_master SET {assignments},source_system='manual',updated_at=CURRENT_TIMESTAMP WHERE material_code=?",
                    [*[values.get(column) for column in MASTER_DATA_DEFINITIONS[entity]["columns"]], old_key],
                )
            else:
                columns = [*MASTER_DATA_DEFINITIONS[entity]["columns"], "source_system"]
                conn.execute(
                    f"INSERT INTO material_master ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    [*[values.get(column) for column in MASTER_DATA_DEFINITIONS[entity]["columns"]], "master_import"],
                )
                created = True
            row = conn.execute("SELECT * FROM material_master WHERE material_code=?", (values["material_code"],)).fetchone()
        elif entity == "suppliers":
            new_key = "|".join([values["supplier_code"], values["material_code"], values.get("warehouse") or ""])
            old_key = str(record_key) if record_key is not None else new_key
            exists = conn.execute("SELECT 1 FROM supplier_master WHERE supplier_key=?", (old_key,)).fetchone()
            if exists:
                assignments = ",".join(f"{column}=?" for column in MASTER_DATA_DEFINITIONS[entity]["columns"])
                conn.execute(
                    f"UPDATE supplier_master SET supplier_key=?,{assignments},source_system='manual',updated_at=CURRENT_TIMESTAMP WHERE supplier_key=?",
                    [new_key, *[values.get(column) for column in MASTER_DATA_DEFINITIONS[entity]["columns"]], old_key],
                )
            else:
                columns = ["supplier_key", *MASTER_DATA_DEFINITIONS[entity]["columns"], "source_system"]
                conn.execute(
                    f"INSERT INTO supplier_master ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    [new_key, *[values.get(column) for column in MASTER_DATA_DEFINITIONS[entity]["columns"]], "master_import"],
                )
                created = True
            row = conn.execute("SELECT * FROM supplier_master WHERE supplier_key=?", (new_key,)).fetchone()
        elif entity == "warehouses":
            new_key = values["warehouse_english"] or values["warehouse_name"]
            old_key = str(record_key) if record_key is not None else new_key
            exists = conn.execute("SELECT 1 FROM warehouse_master WHERE warehouse_key=?", (old_key,)).fetchone()
            if exists:
                conn.execute(
                    """UPDATE warehouse_master SET warehouse_key=?,warehouse_english=?,warehouse_code=?,
                       warehouse_name=?,capacity=?,source_system='manual',updated_at=CURRENT_TIMESTAMP
                       WHERE warehouse_key=?""",
                    (new_key, values["warehouse_english"], values["warehouse_english"], values["warehouse_name"], values.get("capacity"), old_key),
                )
            else:
                conn.execute(
                    """INSERT INTO warehouse_master
                       (warehouse_key,warehouse_english,warehouse_code,warehouse_name,capacity,source_system)
                       VALUES (?,?,?,?,?,'master_import')""",
                    (new_key, values["warehouse_english"], values["warehouse_english"], values["warehouse_name"], values.get("capacity")),
                )
                created = True
            row = conn.execute("SELECT * FROM warehouse_master WHERE warehouse_key=?", (new_key,)).fetchone()
        else:
            route_id = int(record_key) if record_key is not None else None
            exists = conn.execute("SELECT 1 FROM transfer_route WHERE id=?", (route_id,)).fetchone() if route_id else None
            if not exists and record_key is None:
                found = conn.execute(
                    "SELECT id FROM transfer_route WHERE from_warehouse=? AND to_warehouse=? ORDER BY id LIMIT 1",
                    (values["from_warehouse"], values["to_warehouse"]),
                ).fetchone()
                if found:
                    route_id, exists = found["id"], True
            if exists:
                conn.execute(
                    """UPDATE transfer_route SET from_warehouse=?,to_warehouse=?,available_time=?,
                       theoretical_departure_time=?,theoretical_arrival_time=?,time_requirement=?,
                       planned_departure_time=?,planned_arrival_time=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (
                        values["from_warehouse"], values["to_warehouse"], values.get("available_time"),
                        values.get("theoretical_departure_time"), values.get("theoretical_arrival_time"),
                        values.get("time_requirement"), values.get("theoretical_departure_time"),
                        values.get("theoretical_arrival_time"), route_id,
                    ),
                )
            else:
                cursor = conn.execute(
                    """INSERT INTO transfer_route
                       (from_warehouse,to_warehouse,available_time,theoretical_departure_time,
                        theoretical_arrival_time,time_requirement,planned_departure_time,planned_arrival_time)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        values["from_warehouse"], values["to_warehouse"], values.get("available_time"),
                        values.get("theoretical_departure_time"), values.get("theoretical_arrival_time"),
                        values.get("time_requirement"), values.get("theoretical_departure_time"),
                        values.get("theoretical_arrival_time"),
                    ),
                )
                route_id, created = cursor.lastrowid, True
            row = conn.execute("SELECT * FROM transfer_route WHERE id=?", (route_id,)).fetchone()
    return dict(row), created


@app.post("/api/master-data/materials")
def create_material_master(item: MaterialMasterInput) -> dict:
    try:
        return _save_master_record("materials", _input_values(item))[0]
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.put("/api/master-data/materials/{material_code}")
def update_material_master(material_code: str, item: MaterialMasterInput) -> dict:
    try:
        return _save_master_record("materials", _input_values(item), material_code)[0]
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/master-data/suppliers")
def list_supplier_master(search: str | None = Query(default=None), page: int | None = Query(default=None, ge=1),
                        page_size: int = Query(default=100, ge=1, le=500)) -> dict:
    sql = "SELECT * FROM supplier_master"
    params: list[object] = []
    if search:
        sql += " WHERE supplier_code LIKE ? OR supplier_name LIKE ? OR material_code LIKE ? OR material_name LIKE ? OR warehouse LIKE ?"
        token = f"%{search}%"
        params.extend([token, token, token, token, token])
    sql += " ORDER BY supplier_name"
    return _paged_rows(sql, params, page, page_size)


@app.post("/api/master-data/suppliers")
def create_supplier_master(item: SupplierMasterInput) -> dict:
    try:
        return _save_master_record("suppliers", _input_values(item))[0]
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.put("/api/master-data/suppliers/{supplier_key:path}")
def update_supplier_master(supplier_key: str, item: SupplierMasterInput) -> dict:
    try:
        return _save_master_record("suppliers", _input_values(item), supplier_key)[0]
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/master-data/warehouses")
def list_warehouse_master(search: str | None = Query(default=None), page: int | None = Query(default=None, ge=1),
                        page_size: int = Query(default=100, ge=1, le=500)) -> dict:
    sql = "SELECT * FROM warehouse_master"
    params: list[object] = []
    if search:
        sql += " WHERE warehouse_english LIKE ? OR warehouse_name LIKE ? OR capacity LIKE ?"
        token = f"%{search}%"
        params.extend([token, token, token])
    sql += " ORDER BY warehouse_name"
    return _paged_rows(sql, params, page, page_size)


@app.post("/api/master-data/warehouses")
def create_warehouse_master(item: WarehouseMasterInput) -> dict:
    try:
        return _save_master_record("warehouses", _input_values(item))[0]
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.put("/api/master-data/warehouses/{warehouse_key:path}")
def update_warehouse_master(warehouse_key: str, item: WarehouseMasterInput) -> dict:
    try:
        return _save_master_record("warehouses", _input_values(item), warehouse_key)[0]
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/master-data/transfer-routes")
def list_transfer_routes(search: str | None = Query(default=None), page: int | None = Query(default=None, ge=1),
                        page_size: int = Query(default=100, ge=1, le=500)) -> dict:
    sql = "SELECT * FROM transfer_route"
    params: list[object] = []
    if search:
        sql += " WHERE from_warehouse LIKE ? OR to_warehouse LIKE ? OR available_time LIKE ? OR time_requirement LIKE ?"
        token = f"%{search}%"
        params.extend([token, token, token, token])
    sql += " ORDER BY from_warehouse,to_warehouse"
    return _paged_rows(sql, params, page, page_size)


@app.post("/api/master-data/transfer-routes")
def create_transfer_route(item: TransferRouteInput) -> dict:
    try:
        return _save_master_record("transfer-routes", _input_values(item))[0]
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.put("/api/master-data/transfer-routes/{route_id}")
def update_transfer_route(route_id: int, item: TransferRouteInput) -> dict:
    try:
        return _save_master_record("transfer-routes", _input_values(item), route_id)[0]
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _read_master_import(content: bytes, filename: str) -> pd.DataFrame:
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        last_error: Exception | None = None
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                return pd.read_csv(io.BytesIO(content), dtype=object, encoding=encoding)
            except UnicodeDecodeError as exc:
                last_error = exc
        raise ValueError(f"CSV编码无法识别：{last_error}")
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(io.BytesIO(content), sheet_name=0, dtype=object)
    raise ValueError("仅支持 .xlsx、.xls 或 .csv 文件。")


@app.get("/api/master-data/{entity}/template")
def download_master_template(entity: str, format: str = Query(default="xlsx", pattern="^(xlsx|csv)$")) -> StreamingResponse:
    if entity not in MASTER_DATA_DEFINITIONS:
        raise HTTPException(status_code=404, detail="未找到该基础数据类型。")
    headers = MASTER_DATA_DEFINITIONS[entity]["headers"]
    frame = pd.DataFrame(columns=headers)
    if format == "csv":
        content = frame.to_csv(index=False).encode("utf-8-sig")
        return StreamingResponse(
            io.BytesIO(content), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{entity}_template.csv"'},
        )
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="基础数据")
        sheet = writer.book["基础数据"]
        sheet.freeze_panes = "A2"
        for index, header in enumerate(headers, start=1):
            sheet.column_dimensions[sheet.cell(1, index).column_letter].width = min(max(len(header) * 2 + 4, 14), 32)
    output.seek(0)
    return StreamingResponse(
        output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{entity}_template.xlsx"'},
    )


@app.get('/api/master-data/material-controls')
def list_material_controls(search: str = '', page: int | None = Query(default=None, ge=1),
                           page_size: int = Query(default=100, ge=1, le=500)) -> dict:
    return _paged_rows('''SELECT * FROM material_control
            WHERE material_owner LIKE ? OR purchase_owner LIKE ? OR material_code LIKE ? OR material_name LIKE ?
            ORDER BY material_owner,material_code''', [f'%{search}%']*4, page, page_size)


@app.post('/api/master-data/material-controls')
def create_material_control(item: MaterialControlInput) -> dict:
    try:
        return _save_master_record('material-controls', _input_values(item))[0]
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put('/api/master-data/material-controls/{record_id}')
def update_material_control(record_id: int, item: MaterialControlInput) -> dict:
    try:
        return _save_master_record('material-controls', _input_values(item), record_id)[0]
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get('/api/operations/{kind}')
def list_operations(kind: Literal['purchase', 'transfer'], search: str = '',
                    execution_status: str = '', source: str = '', start_date: date | None = None,
                    end_date: date | None = None, page: int = Query(1, ge=1),
                    page_size: int = Query(100, ge=1, le=500)) -> dict:
    rows = operational_history(kind)
    rows = [r for r in rows if (not execution_status or r['execution_status'] == execution_status)
            and (not source or r['source'] == source)
            and (not start_date or str(r.get('business_time') or '')[:10] >= start_date.isoformat())
            and (not end_date or str(r.get('business_time') or '')[:10] <= end_date.isoformat())
            and (not search or search.lower() in ' '.join(str(r.get(k) or '') for k in
                 ('display_no', 'material_code', 'material_name', 'supplier_name', 'warehouse_display', 'linked_order_no')).lower())]
    return {'rows': rows[(page-1)*page_size:page*page_size], 'total': len(rows), 'page': page}


@app.put('/api/operations/{kind}/{source}/{record_id}/execution')
def update_execution(kind: Literal['purchase', 'transfer'], source: Literal['manual', 'formal'],
                     record_id: int, item: ExecutionInput) -> dict:
    if not item.operator.strip():
        raise HTTPException(422, '请填写修改人员')
    if item.execution_status in {'延误', '未完成'} and not item.execution_note.strip():
        raise HTTPException(422, '延误或未完成必须填写情况说明')
    table = f"{kind}_{'plan' if source == 'manual' else 'record'}"
    with db_connection() as conn:
        if not conn.execute(f'SELECT id FROM {table} WHERE id=?', (record_id,)).fetchone():
            raise HTTPException(404, '未找到该记录')
        conn.execute(f'''UPDATE {table} SET execution_status=?,execution_note=?,operator=?,
            status_updated_at=CURRENT_TIMESTAMP WHERE id=?''',
            (item.execution_status, item.execution_note.strip(), item.operator.strip(), record_id))
        conn.execute('INSERT INTO execution_audit (entity,record_id,execution_status,execution_note,operator) VALUES (?,?,?,?,?)',
                     (table, record_id, item.execution_status, item.execution_note.strip(), item.operator.strip()))
        row = dict(conn.execute(f'SELECT * FROM {table} WHERE id=?', (record_id,)).fetchone())
    return row


@app.get('/api/operations/{kind}/{source}/{record_id}/audit')
def execution_audit(kind: Literal['purchase', 'transfer'], source: Literal['manual', 'formal'], record_id: int) -> dict:
    table = f"{kind}_{'plan' if source == 'manual' else 'record'}"
    with db_connection() as conn:
        return {'rows': [dict(r) for r in conn.execute('SELECT * FROM execution_audit WHERE entity=? AND record_id=? ORDER BY id DESC', (table, record_id))]}


@app.post("/api/master-data/{entity}/import")
async def import_master_data(entity: str, file: UploadFile = File(...)) -> dict:
    if entity not in MASTER_DATA_DEFINITIONS:
        raise HTTPException(status_code=404, detail="未找到该基础数据类型。")
    filename = _safe_filename(file.filename or "")
    content = await file.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(status_code=422, detail="文件不能超过40MB。")
    try:
        frame = _read_master_import(content, filename)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"无法读取基础数据文件：{exc}") from exc
    definition = MASTER_DATA_DEFINITIONS[entity]
    header_lookup = {clean_column_name(header).lower(): column for header, column in zip(definition["headers"], definition["columns"])}
    rename_map = {
        original: header_lookup[clean_column_name(original).lower()]
        for original in frame.columns if clean_column_name(original).lower() in header_lookup
    }
    frame = frame.rename(columns=rename_map).dropna(how="all")
    missing_columns = [header for header, column in zip(definition["headers"], definition["columns"]) if column not in frame.columns and column in definition["required"]]
    if missing_columns:
        raise HTTPException(status_code=422, detail=f"模板缺少必填列：{'、'.join(missing_columns)}")
    inserted = updated = skipped = 0
    errors = []
    for index, row in frame.iterrows():
        values = {}
        for column in definition["columns"]:
            value = row.get(column)
            values[column] = None if value is None or pd.isna(value) else (str(value).strip() if not isinstance(value, (int, float)) else value)
        for numeric_column in ({"volume", "weight", "safety_stock"} if entity == "materials" else {"loading_qty"} if entity == "suppliers" else set()):
            if values.get(numeric_column) not in {None, ""}:
                parsed = pd.to_numeric(str(values[numeric_column]).replace(",", ""), errors="coerce")
                if pd.isna(parsed):
                    errors.append({"row": int(index) + 2, "message": f"{numeric_column}不是有效数字"})
                    values = {}
                    break
                values[numeric_column] = float(parsed)
        if not values:
            skipped += 1
            continue
        try:
            _, created = _save_master_record(entity, values)
            inserted += int(created)
            updated += int(not created)
        except (ValueError, sqlite3.IntegrityError) as exc:
            skipped += 1
            errors.append({"row": int(index) + 2, "message": str(exc)})
    return {"filename": filename, "inserted": inserted, "updated": updated, "skipped": skipped, "errors": errors[:50]}


@app.get("/api/purchase-reconciliation")
def get_purchase_reconciliation(
    business_date: date = Query(...),
    days: int = Query(default=7, ge=0, le=31),
    search: str = Query(default=''),
    warehouse: str = Query(default=''),
    supplier: str = Query(default=''),
    material: str = Query(default=''),
    page: int | None = Query(default=None, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
) -> dict:
    rows = purchase_reconciliation(business_date, days)
    def contains(value, term):
        return term.strip().casefold() in str(value or '').casefold()
    rows = [r for r in rows
            if contains(r.get('warehouse'), warehouse)
            and contains(r.get('supplier_name'), supplier)
            and (contains(r.get('material_code'), material) or contains(r.get('material_name'), material))
            and any(contains(r.get(k), search) for k in ('order_no', 'supplier_name', 'warehouse', 'material_code', 'material_name'))]
    rows.sort(key=lambda r: (str(r.get('delivery_date') or ''), str(r.get('order_no') or ''), r['id']))
    total = len(rows)
    if page is not None:
        rows = rows[(page-1)*page_size:page*page_size]
    return {"business_date": business_date.isoformat(), "days": days, "rows": rows, "total": total, "page": page or 1}


@app.put("/api/purchase-records/{record_id}/remark")
def update_purchase_remark(record_id: int, item: PurchaseRemarkInput) -> dict:
    with db_connection() as conn:
        exists = conn.execute("SELECT id,source_key FROM purchase_record WHERE id=?", (record_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="未找到采购记录。")
        conn.execute(
            "UPDATE purchase_record SET remark=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (item.remark, record_id),
        )
        row = conn.execute("SELECT * FROM purchase_record WHERE id=?", (record_id,)).fetchone()
    update_postgres_purchase_remark(exists["source_key"], item.remark)
    return dict(row)


def _serialize_plan(item: BaseModel) -> dict:
    values = item.model_dump() if hasattr(item, "model_dump") else item.dict()
    for key in ('material_code', 'supplier_name', 'arrival_warehouse', 'from_warehouse', 'to_warehouse'):
        if key in values and not str(values[key] or '').strip():
            raise HTTPException(422, f'{key}不能为空')
    for key, value in list(values.items()):
        if isinstance(value, (datetime, date)):
            values[key] = value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
        elif isinstance(value, str):
            values[key] = value.strip()
    return values


@app.get("/api/purchase-plans")
def list_purchase_plans() -> dict:
    with db_connection() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM purchase_plan ORDER BY COALESCE(expected_arrival_time,''),id DESC"
        ).fetchall()]
    return {"rows": rows}


@app.post("/api/purchase-plans")
def create_purchase_plan(item: PurchasePlanInput) -> dict:
    values = _serialize_plan(item)
    columns = list(values)
    with db_connection() as conn:
        cursor = conn.execute(
            f"INSERT INTO purchase_plan ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            [values[column] for column in columns],
        )
        row = conn.execute("SELECT * FROM purchase_plan WHERE id=?", (cursor.lastrowid,)).fetchone()
    return dict(row)


@app.put("/api/purchase-plans/{plan_id}")
def update_purchase_plan(plan_id: int, item: PurchasePlanInput) -> dict:
    values = _serialize_plan(item)
    with db_connection() as conn:
        if not conn.execute("SELECT id FROM purchase_plan WHERE id=?", (plan_id,)).fetchone():
            raise HTTPException(status_code=404, detail="未找到采购信息。")
        conn.execute(
            f"UPDATE purchase_plan SET {','.join(f'{key}=?' for key in values)},updated_at=CURRENT_TIMESTAMP WHERE id=?",
            [*values.values(), plan_id],
        )
        row = conn.execute("SELECT * FROM purchase_plan WHERE id=?", (plan_id,)).fetchone()
    return dict(row)


@app.get("/api/transfer-plans")
def list_transfer_plans() -> dict:
    with db_connection() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM transfer_plan ORDER BY COALESCE(expected_departure_time,''),id DESC"
        ).fetchall()]
    return {"rows": rows}


@app.post("/api/transfer-plans")
def create_transfer_plan(item: TransferPlanInput) -> dict:
    values = _serialize_plan(item)
    columns = list(values)
    with db_connection() as conn:
        cursor = conn.execute(
            f"INSERT INTO transfer_plan ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            [values[column] for column in columns],
        )
        row = conn.execute("SELECT * FROM transfer_plan WHERE id=?", (cursor.lastrowid,)).fetchone()
    return dict(row)


@app.put("/api/transfer-plans/{plan_id}")
def update_transfer_plan(plan_id: int, item: TransferPlanInput) -> dict:
    values = _serialize_plan(item)
    with db_connection() as conn:
        if not conn.execute("SELECT id FROM transfer_plan WHERE id=?", (plan_id,)).fetchone():
            raise HTTPException(status_code=404, detail="未找到调拨物料信息。")
        conn.execute(
            f"UPDATE transfer_plan SET {','.join(f'{key}=?' for key in values)},updated_at=CURRENT_TIMESTAMP WHERE id=?",
            [*values.values(), plan_id],
        )
        row = conn.execute("SELECT * FROM transfer_plan WHERE id=?", (plan_id,)).fetchone()
    return dict(row)


def _safe_filename(name: str) -> str:
    cleaned = Path(name).name.replace("\x00", "")
    return cleaned or f"upload-{uuid.uuid4().hex}.xlsx"


async def _store_upload(upload: UploadFile, target_dir: Path) -> Path:
    filename = _safe_filename(upload.filename or "")
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise WorkbookValidationError(f"{filename}：仅支持 .xlsx 或 .xls 文件。")
    target = target_dir / filename
    size = 0
    with target.open("wb") as output:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                output.close()
                target.unlink(missing_ok=True)
                raise WorkbookValidationError(f"{filename}：单个文件不能超过40MB。")
            output.write(chunk)
    return target


@app.post("/api/runs")
async def create_run(
    business_date: date = Form(...),
    safety_days: float = Form(1.0),
    force_calculation: bool = Form(False),
    quality_only: bool = Form(False),
    files: list[UploadFile] = File(...),
) -> JSONResponse:
    if not files:
        raise HTTPException(status_code=400, detail="请至少上传一个Excel文件。")
    if safety_days < 0 or safety_days > 30:
        raise HTTPException(status_code=400, detail="安全库存天数应在0到30之间。")

    run_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    run_dir = UPLOAD_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    with db_connection() as conn:
        conn.execute(
            "INSERT INTO upload_run(id,business_date,created_at,status) VALUES (?,?,?,?)",
            (run_id, business_date.isoformat(), created_at, "processing"),
        )

    workbooks: dict[str, ParsedWorkbook] = {}
    file_results: list[dict] = []
    try:
        for upload in files:
            original_name = _safe_filename(upload.filename or "")
            try:
                path = await _store_upload(upload, run_dir)
                parsed = parse_workbook(path)
                if parsed.file_type in workbooks:
                    raise WorkbookValidationError(f"重复上传了“{parsed.label}”类型文件。")
                workbooks[parsed.file_type] = parsed
                message = "；".join(parsed.warnings) if parsed.warnings else "读取成功"
                file_result = {
                    "filename": original_name,
                    "file_type": parsed.file_type,
                    "label": parsed.label,
                    "row_count": int(len(parsed.dataframe)),
                    "status": "success",
                    "message": message,
                }
            except WorkbookValidationError as exc:
                file_result = {
                    "filename": original_name,
                    "file_type": None,
                    "label": "未识别",
                    "row_count": 0,
                    "status": "error",
                    "message": str(exc),
                }
            file_results.append(file_result)
            with db_connection() as conn:
                conn.execute(
                    """INSERT INTO uploaded_file
                       (run_id,original_name,detected_type,row_count,status,message)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        run_id, file_result["filename"], file_result["file_type"],
                        file_result["row_count"], file_result["status"], file_result["message"],
                    ),
                )

        errors = [item for item in file_results if item["status"] == "error"]
        if errors:
            raise WorkbookValidationError("部分文件校验失败，请根据文件明细修正后重新上传。")

        mappings = _load_mappings()
        reports: dict[str, QualityReport] = {}
        unmatched_items: list[dict] = []
        for file_type, workbook in workbooks.items():
            unmatched = apply_warehouse_mappings(workbook, mappings, business_date)
            unmatched_items.extend(unmatched)
            reports[file_type] = clean_and_assess(
                workbook, business_date,
                unmatched_warehouse_count=sum(item["record_count"] for item in unmatched),
            )
        add_unmatched_material_counts(workbooks, reports)
        _persist_quality(run_id, list(reports.values()))
        _persist_unmatched(run_id, unmatched_items)
        persist_operational_data(run_id, business_date, workbooks)

        stock_report = reports.get("stock")
        result_mode = "official"
        if stock_report is None:
            raise WorkbookValidationError("缺少库存余量文件，无法计算风险。")
        if quality_only:
            with db_connection() as conn:
                conn.execute(
                    """UPDATE upload_run SET status='validated',result_mode='quality_only',
                       stock_snapshot_date=?,stock_date_status=? WHERE id=?""",
                    (stock_report.file_date, stock_report.file_date_status, run_id),
                )
            return JSONResponse({
                "run_id": run_id,
                "business_date": business_date.isoformat(),
                "status": "validated",
                "result_mode": "quality_only",
                "forced_calculation": False,
                "files": file_results,
                "quality_reports": [report.to_dict() for report in reports.values()],
                "unmatched_warehouses": unmatched_items,
                "summary": {"total": 0, "红色": 0, "黄色": 0, "绿色": 0},
                "rows": [],
                "truncated": False,
            })
        if not stock_report.calculation_allowed:
            if not force_calculation:
                with db_connection() as conn:
                    conn.execute(
                        """UPDATE upload_run SET status='blocked', error_message=?,
                           stock_snapshot_date=?,stock_date_status=? WHERE id=?""",
                        (
                            "库存日期不满足正式计算要求；如需继续，请选择强制计算。",
                            stock_report.file_date, stock_report.file_date_status, run_id,
                        ),
                    )
                return JSONResponse(
                    status_code=422,
                    content={
                        "run_id": run_id,
                        "status": "blocked",
                        "detail": "库存滞后超过1天或无法识别库存日期，已禁止生成正式结果。",
                        "requires_force_calculation": True,
                        "files": file_results,
                        "quality_reports": [report.to_dict() for report in reports.values()],
                        "unmatched_warehouses": unmatched_items,
                    },
                )
            result_mode = "simulation"

        rows = calculate_risk(workbooks, business_date, safety_days=safety_days)
        save_risk_rows(run_id, rows)
        with db_connection() as conn:
            conn.execute(
                """UPDATE upload_run SET status='completed',result_mode=?,forced_calculation=?,
                   stock_snapshot_date=?,stock_date_status=? WHERE id=?""",
                (
                    result_mode, int(force_calculation), stock_report.file_date,
                    stock_report.file_date_status, run_id,
                ),
            )

        counts = {level: sum(r["risk_level"] == level for r in rows) for level in ("红色", "黄色", "绿色")}
        with db_connection() as conn:
            stored_rows = [dict(row) for row in conn.execute(
                """SELECT id,warehouse,material_code,material_name,current_stock,allocated_qty,
                   frozen_qty,available_stock,t1_demand,t1_purchase,t1_transfer_in,t1_transfer_out,
                   t1_projected_stock,t2_demand,t2_purchase,t2_transfer_in,t2_transfer_out,
                   t2_projected_stock,safety_stock,shortage_date,risk_level,risk_reason
                   FROM risk_result WHERE run_id=? ORDER BY
                   CASE risk_level WHEN '红色' THEN 1 WHEN '黄色' THEN 2 ELSE 3 END,
                   t2_projected_stock LIMIT 500""",
                (run_id,),
            ).fetchall()]
        return JSONResponse({
            "run_id": run_id,
            "business_date": business_date.isoformat(),
            "status": "completed",
            "result_mode": result_mode,
            "forced_calculation": force_calculation,
            "files": file_results,
            "quality_reports": [report.to_dict() for report in reports.values()],
            "unmatched_warehouses": unmatched_items,
            "summary": {"total": len(rows), **counts},
            "rows": stored_rows,
            "truncated": len(rows) > 500,
        })
    except (WorkbookValidationError, ValueError) as exc:
        with db_connection() as conn:
            conn.execute(
                "UPDATE upload_run SET status='failed', error_message=? WHERE id=?",
                (str(exc), run_id),
            )
        return JSONResponse(
            status_code=422,
            content={
                "run_id": run_id,
                "status": "failed",
                "detail": str(exc),
                "files": file_results,
            },
        )
    except Exception as exc:
        with db_connection() as conn:
            conn.execute(
                "UPDATE upload_run SET status='failed', error_message=? WHERE id=?",
                (f"系统错误：{exc}", run_id),
            )
        raise HTTPException(status_code=500, detail=f"处理失败：{exc}") from exc


def _run_payload(run_id: str, risk_level: str | None = None) -> dict:
    with db_connection() as conn:
        run = conn.execute("SELECT * FROM upload_run WHERE id=?", (run_id,)).fetchone()
        if not run:
            raise HTTPException(status_code=404, detail="未找到该批次。")
        sql = """SELECT id,run_id,warehouse,material_code,material_name,current_stock,allocated_qty,
                 frozen_qty,available_stock,t1_demand,t1_purchase,t1_transfer_in,t1_transfer_out,
                 t1_projected_stock,t2_demand,t2_purchase,t2_transfer_in,t2_transfer_out,
                 t2_projected_stock,safety_stock,shortage_date,risk_level,risk_reason
                 FROM risk_result WHERE run_id=?"""
        params: list[object] = [run_id]
        if risk_level:
            sql += " AND risk_level=?"
            params.append(risk_level)
        sql += " ORDER BY CASE risk_level WHEN '红色' THEN 1 WHEN '黄色' THEN 2 ELSE 3 END, t2_projected_stock"
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
        files = [dict(row) for row in conn.execute("SELECT * FROM uploaded_file WHERE run_id=? ORDER BY id", (run_id,)).fetchall()]
        quality = [dict(row) for row in conn.execute(
            "SELECT * FROM data_quality_report WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()]
        unmatched = [dict(row) for row in conn.execute(
            "SELECT * FROM unmatched_warehouse WHERE run_id=? ORDER BY record_count DESC,id", (run_id,)
        ).fetchall()]
    return {"run": dict(run), "files": files, "quality_reports": quality, "unmatched_warehouses": unmatched, "rows": rows}


@app.get("/api/runs/latest")
def latest_run() -> dict:
    with db_connection() as conn:
        row = conn.execute(
            "SELECT id FROM upload_run WHERE status='completed' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        return {"run": None, "files": [], "rows": []}
    return _run_payload(row["id"])


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, risk_level: str | None = Query(default=None)) -> dict:
    return _run_payload(run_id, risk_level)


@app.get("/api/runs/{run_id}/risks/{risk_id}")
def get_risk_detail(run_id: str, risk_id: int) -> dict:
    with db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM risk_result WHERE run_id=? AND id=?", (run_id, risk_id)
        ).fetchone()
        run = conn.execute("SELECT * FROM upload_run WHERE id=?", (run_id,)).fetchone()
    if not row or not run:
        raise HTTPException(status_code=404, detail="未找到该风险记录。")
    item = dict(row)
    details = json.loads(item.pop("details_json") or "{}")
    trace = details.pop("_trace", {})
    calculation = {
        "available_stock": "当前库存 - 分配数量 - 冻结数量",
        "t1": "可用库存 + T+1采购到货 + T+1调拨入 - T+1需求 - T+1调拨出",
        "t2": "T+1预计库存 + T+2采购到货 + T+2调拨入 - T+2需求 - T+2调拨出",
    }
    return {
        "run": dict(run),
        "risk": item,
        "calculation": calculation,
        "source_records": trace,
        "details": details,
    }


@app.get("/api/runs/{run_id}/export")
def export_run(run_id: str) -> StreamingResponse:
    payload = _run_payload(run_id)
    rows = payload["rows"]
    if not rows:
        raise HTTPException(status_code=404, detail="该批次没有可导出的风险结果。")
    export_columns = {
        "warehouse": "仓库", "material_code": "物料编码", "material_name": "物料名称",
        "current_stock": "当前库存", "allocated_qty": "分配数量", "frozen_qty": "冻结数量",
        "available_stock": "可用库存", "t1_demand": "T+1需求", "t1_purchase": "T+1计划采购到货",
        "t1_transfer_in": "T+1调拨入", "t1_transfer_out": "T+1调拨出",
        "t1_projected_stock": "T+1预计库存", "t2_demand": "T+2需求",
        "t2_purchase": "T+2计划采购到货", "t2_transfer_in": "T+2调拨入",
        "t2_transfer_out": "T+2调拨出", "t2_projected_stock": "T+2预计库存",
        "safety_stock": "安全库存", "shortage_date": "预计缺货日期",
        "risk_level": "风险等级", "risk_reason": "风险原因",
    }
    frame = pd.DataFrame(rows)
    frame = frame[[c for c in export_columns if c in frame.columns]].rename(columns=export_columns)
    frame.insert(0, "结果类型", "模拟结果" if payload["run"].get("result_mode") == "simulation" else "正式结果")
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="库存风险清单")
        worksheet = writer.book["库存风险清单"]
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
        for column in worksheet.columns:
            width = min(max(len(str(cell.value or "")) for cell in column) + 2, 35)
            worksheet.column_dimensions[column[0].column_letter].width = width
    output.seek(0)
    filename = f"inventory_risk_{payload['run']['business_date']}.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/templates")
def download_templates() -> StreamingResponse:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for key, columns in TEMPLATE_COLUMNS.items():
            sheet_name = FILE_SPECS[key].label[:31]
            pd.DataFrame(columns=columns).to_excel(writer, index=False, sheet_name=sheet_name)
            worksheet = writer.book[sheet_name]
            worksheet.freeze_panes = "A2"
            for index, column in enumerate(columns, start=1):
                worksheet.column_dimensions[worksheet.cell(1, index).column_letter].width = min(max(len(column) * 2 + 4, 14), 28)
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="daily_import_templates.xlsx"'},
    )


@app.get("/api/warehouse-mappings")
def list_warehouse_mappings(include_disabled: bool = Query(default=True),
                            page: int | None = Query(default=None, ge=1),
                            page_size: int = Query(default=100, ge=1, le=500),
                            source: str = '', enabled: int | None = Query(default=None, ge=0, le=1),
                            search: str = '') -> dict:
    sql = "SELECT * FROM warehouse_mapping"
    conditions, params = [], []
    if not include_disabled:
        conditions.append('enabled=1')
    if source:
        conditions.append('source_system=?'); params.append(source)
    if enabled is not None:
        conditions.append('enabled=?'); params.append(enabled)
    if search:
        fields = ('raw_warehouse_code','raw_warehouse_name','standard_warehouse_code','standard_warehouse_name','special_alias')
        conditions.append('('+' OR '.join(field+' LIKE ?' for field in fields)+')')
        params.extend([f'%{search}%']*len(fields))
    if conditions:
        sql += ' WHERE '+' AND '.join(conditions)
    sql += " ORDER BY source_system,standard_warehouse_code,id"
    payload = _paged_rows(sql, params, page, page_size)
    with db_connection() as conn:
        payload['sources'] = [r[0] for r in conn.execute('SELECT DISTINCT source_system FROM warehouse_mapping ORDER BY source_system')]
    return payload


@app.post("/api/warehouse-mappings")
def create_warehouse_mapping(item: WarehouseMappingInput) -> dict:
    values = _model_values(item)
    if not values.get("raw_warehouse_code") and not values.get("raw_warehouse_name"):
        raise HTTPException(status_code=422, detail="原始仓库编码和名称至少填写一项。")
    with db_connection() as conn:
        cursor = conn.execute(
            f"INSERT INTO warehouse_mapping ({','.join(WAREHOUSE_MAPPING_COLUMNS)}) VALUES ({','.join('?' for _ in WAREHOUSE_MAPPING_COLUMNS)})",
            [int(values[column]) if column == "enabled" else values.get(column) for column in WAREHOUSE_MAPPING_COLUMNS],
        )
        row = conn.execute("SELECT * FROM warehouse_mapping WHERE id=?", (cursor.lastrowid,)).fetchone()
    return dict(row)


@app.put("/api/warehouse-mappings/{mapping_id}")
def update_warehouse_mapping(mapping_id: int, item: WarehouseMappingInput) -> dict:
    values = _model_values(item)
    assignments = ",".join(f"{column}=?" for column in WAREHOUSE_MAPPING_COLUMNS)
    with db_connection() as conn:
        exists = conn.execute("SELECT id FROM warehouse_mapping WHERE id=?", (mapping_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="未找到该仓库映射。")
        conn.execute(
            f"UPDATE warehouse_mapping SET {assignments},updated_at=CURRENT_TIMESTAMP WHERE id=?",
            [*[int(values[column]) if column == "enabled" else values.get(column) for column in WAREHOUSE_MAPPING_COLUMNS], mapping_id],
        )
        row = conn.execute("SELECT * FROM warehouse_mapping WHERE id=?", (mapping_id,)).fetchone()
    return dict(row)


@app.get("/api/warehouse-mappings/template")
def warehouse_mapping_template() -> StreamingResponse:
    output = io.BytesIO()
    columns = ["id", *WAREHOUSE_MAPPING_COLUMNS]
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame(columns=columns).to_excel(writer, index=False, sheet_name="仓库映射")
        sheet = writer.book["仓库映射"]
        sheet.freeze_panes = "A2"
        for index, column in enumerate(columns, start=1):
            sheet.column_dimensions[sheet.cell(1, index).column_letter].width = min(max(len(column) + 4, 16), 28)
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="warehouse_mapping_template.xlsx"'},
    )


@app.post("/api/warehouse-mappings/import")
async def import_warehouse_mappings(file: UploadFile = File(...)) -> dict:
    filename = _safe_filename(file.filename or "")
    if Path(filename).suffix.lower() not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=422, detail="仅支持 .xlsx 或 .xls 文件。")
    content = await file.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(status_code=422, detail="文件不能超过40MB。")
    try:
        frame = pd.read_excel(io.BytesIO(content), sheet_name=0, dtype=object)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"无法读取映射文件：{exc}") from exc
    missing = [column for column in WAREHOUSE_MAPPING_COLUMNS if column not in frame.columns]
    if missing:
        raise HTTPException(status_code=422, detail=f"映射模板缺少字段：{'、'.join(missing)}")
    inserted = updated = skipped = 0
    with db_connection() as conn:
        for _, row in frame.dropna(how="all").iterrows():
            values = {column: (None if pd.isna(row.get(column)) else row.get(column)) for column in WAREHOUSE_MAPPING_COLUMNS}
            if not values.get("source_system") or not values.get("standard_warehouse_code") or not values.get("standard_warehouse_name"):
                skipped += 1
                continue
            values["enabled"] = int(str(values.get("enabled", "1")).strip().lower() not in {"0", "false", "否", "停用"})
            mapping_id = None if "id" not in frame.columns or pd.isna(row.get("id")) else int(row["id"])
            if mapping_id and conn.execute("SELECT id FROM warehouse_mapping WHERE id=?", (mapping_id,)).fetchone():
                assignments = ",".join(f"{column}=?" for column in WAREHOUSE_MAPPING_COLUMNS)
                conn.execute(
                    f"UPDATE warehouse_mapping SET {assignments},updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    [*[values[column] for column in WAREHOUSE_MAPPING_COLUMNS], mapping_id],
                )
                updated += 1
            else:
                conn.execute(
                    f"INSERT INTO warehouse_mapping ({','.join(WAREHOUSE_MAPPING_COLUMNS)}) VALUES ({','.join('?' for _ in WAREHOUSE_MAPPING_COLUMNS)})",
                    [values[column] for column in WAREHOUSE_MAPPING_COLUMNS],
                )
                inserted += 1
    return {"filename": filename, "inserted": inserted, "updated": updated, "skipped": skipped}
