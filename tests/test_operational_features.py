from __future__ import annotations

from datetime import date
import io
from pathlib import Path
from urllib.parse import quote

import pandas as pd
from fastapi.testclient import TestClient

import app.database as database
import app.main as main
from app.excel_service import ParsedWorkbook
from app.operational_service import persist_operational_data


def configure_temp_database(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "operational.db"
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(database, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(main, "UPLOAD_DIR", upload_dir)
    database.initialize_database()


def workbook(file_type: str, frame: pd.DataFrame) -> ParsedWorkbook:
    return ParsedWorkbook(file_type, file_type, f"{file_type}.xlsx", frame, [])


def test_operational_persistence_dashboard_reconciliation_and_master_data(monkeypatch, tmp_path: Path) -> None:
    configure_temp_database(monkeypatch, tmp_path)
    workbooks = {
        "stock": workbook("stock", pd.DataFrame([{
            "warehouse_code": "W1", "warehouse": "湖州DC", "material_code": "M1",
            "material_name": "测试物料", "stock_qty": 100, "allocated_qty": 10,
            "frozen_qty": 5, "transfer_in_transit_qty": 7, "purchase_in_transit_qty": 9,
        }])),
        "purchase_order": workbook("purchase_order", pd.DataFrame([{
            "order_no": "PO001", "source_date": "2026-09-15", "supplier_name": "供应商甲",
            "material_code": "M1", "material_name": "测试物料", "purchase_qty": 80,
            "delivery_date": "2026-09-17", "remaining_qty": 20, "warehouse": "湖州仓库",
        }])),
        "inbound_daily": workbook("inbound_daily", pd.DataFrame([{
            "reference_no3": "PO001", "transaction_no": "TX001", "warehouse_code": "W1",
            "warehouse": "湖州仓库", "material_code": "M1", "material_name": "测试物料",
            "expected_qty": 80, "inbound_qty": 55, "inbound_date": "2026-09-17",
            "supplier_name": "供应商甲",
        }])),
        "transfer": workbook("transfer", pd.DataFrame([{
            "order_no": "TR001", "from_warehouse": "杭州DC", "to_warehouse": "湖州DC",
            "material_code": "M1", "material_name": "测试物料", "transfer_qty": 30,
            "outbound_qty": 25, "inbound_qty": 10, "transportation_mode": "干线",
        }])),
        "outbound": workbook("outbound", pd.DataFrame([{
            "stat_date": "2026-09-15", "warehouse": "湖州DC", "material_code": "M1",
            "material_name": "测试物料", "material_category": "原料", "allocated_qty": 12,
            "picked_qty": 11,
        }])),
        "booking_order": workbook("booking_order", pd.DataFrame([
            {"delivery_date": "2026-09-15", "warehouse": "湖州DC", "material_code": "M1", "material_name": "测试物料", "ordered_qty": 70, "demand_qty": 63},
            {"delivery_date": "2026-09-16", "warehouse": "湖州DC", "material_code": "M1", "material_name": "测试物料", "ordered_qty": 30, "demand_qty": 27},
        ])),
    }
    persist_operational_data("run-1", date(2026, 9, 16), workbooks)
    with database.db_connection() as conn:
        conn.execute(
            """INSERT INTO supplier_master
               (supplier_key,supplier_code,supplier_name,material_code,material_name,arrival_mode,warehouse,source_system)
               VALUES (?,?,?,?,?,?,?,'manual')""",
            ("S1|M1|湖州仓库", "S1", "供应商甲", "M1", "测试物料", "含运", "湖州仓库"),
        )

    with TestClient(main.app) as client:
        dashboard = client.get("/api/planning-dashboard?business_date=2026-09-16").json()
        reconciliation = client.get("/api/purchase-reconciliation?business_date=2026-09-16&days=7").json()
        materials = client.get("/api/master-data/materials").json()["rows"]
        suppliers = client.get("/api/master-data/suppliers").json()["rows"]
        routes = client.get("/api/master-data/transfer-routes").json()["rows"]

    row = next(r for r in dashboard["rows"] if r['warehouse'] == '湖州仓库')
    assert row["available_stock"] == 85
    assert row["picked_7d"] == 11
    assert row["purchase_remaining_qty_7d"] == 20
    assert row["purchase_received_qty_7d"] == 55
    assert row["transfer_in_transit_in"] == 15
    assert row["supplier"] == "供应商甲"
    assert row["arrival_mode"] == "含运"
    assert row["purchase_data_future_7d"] == 25  # 80采购 - 55 WMS收货
    assert row["inventory"] == 100
    assert row["inventory_plus_transfer_purchase"] == 125
    assert row["booking_avg_7d"] == 12 / 7  # 历史需求改用日出库
    assert row["booking_3d"] == 12
    assert row["booking_today"] == 30
    assert row["saleable_booking_avg_7d"] == 11 / 7
    assert row["saleable_booking_3d"] == 11
    assert row["saleable_booking_today"] == 27
    assert "杭州仓库→湖州仓库" in row["transfer_area"]
    assert reconciliation["rows"][0]["order_no"] == "PO001"
    assert reconciliation["rows"][0]["received_difference"] == -5
    assert materials[0]["material_code"] == "M1"
    assert suppliers[0]["supplier_name"] == "供应商甲"
    assert routes[0]["from_warehouse"] == "杭州仓库"

    persist_operational_data("run-2", date(2026, 9, 16), workbooks)
    with database.db_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM stock_snapshot").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM demand_history").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM booking_demand").fetchone()[0] == 2


def test_purchase_and_transfer_plan_create_and_update(monkeypatch, tmp_path: Path) -> None:
    configure_temp_database(monkeypatch, tmp_path)
    purchase = {
        "supplier_name": "供应商甲", "material_code": "M1", "material_name": "物料一",
        "order_qty": 100, "arrival_warehouse": "湖州仓库", "supplier_stock_qty": 20,
        "supplier_producible_qty": 40, "supplier_estimated_qty": 60,
    }
    transfer = {
        "request_no": "TR001", "from_warehouse": "杭州仓库", "to_warehouse": "湖州仓库",
        "material_code": "M1", "material_name": "物料一", "demand_qty": 50, "planned_qty": 40,
    }
    with TestClient(main.app) as client:
        created_purchase = client.post("/api/purchase-plans", json=purchase)
        created_transfer = client.post("/api/transfer-plans", json=transfer)
        purchase["order_qty"] = 120
        transfer["planned_qty"] = 45
        updated_purchase = client.put(f"/api/purchase-plans/{created_purchase.json()['id']}", json=purchase)
        updated_transfer = client.put(f"/api/transfer-plans/{created_transfer.json()['id']}", json=transfer)

    assert created_purchase.status_code == 200
    assert created_transfer.status_code == 200
    assert updated_purchase.json()["order_qty"] == 120
    assert updated_transfer.json()["planned_qty"] == 45


def test_stock_in_transit_only_and_completed_purchase_rows_are_preserved() -> None:
    from app.data_quality import clean_and_assess

    stock = workbook("stock", pd.DataFrame([{
        "warehouse": "湖州仓库", "material_code": "M1", "stock_qty": None,
        "transfer_in_transit_qty": 30, "purchase_in_transit_qty": 20,
    }]))
    purchase = workbook("purchase_order", pd.DataFrame([{
        "order_no": "PO-DONE", "warehouse": "湖州仓库", "material_code": "M1",
        "delivery_date": "2026-09-16", "remaining_qty": None,
    }]))

    stock_report = clean_and_assess(stock, date(2026, 9, 16))
    purchase_report = clean_and_assess(purchase, date(2026, 9, 16))

    assert stock_report.valid_rows == 1
    assert purchase_report.valid_rows == 1


def test_master_data_manual_edit_and_excel_csv_import(monkeypatch, tmp_path: Path) -> None:
    configure_temp_database(monkeypatch, tmp_path)
    material = {
        "material_code": "M100", "material_name": "测试原料", "material_category": "短保",
        "supply_time": "2天", "unit": "包", "minimum_package": "1包",
        "package_content": "500克", "case_pack": "20包/箱", "volume": 0.03,
        "weight": 10, "safety_stock": 100,
    }
    supplier = {
        "supplier_code": "S100", "supplier_name": "测试供应商", "material_code": "M100",
        "material_name": "测试原料", "arrival_mode": "含运", "warehouse": "湖州仓库",
        "vehicle_type": "9.6米", "loading_qty": 500, "loading_package": "500箱",
    }
    warehouse_csv = "仓库英文,仓库名称,仓库库容\nHuzhou,湖州仓库,大型\n".encode("utf-8-sig")
    route_output = io.BytesIO()
    with pd.ExcelWriter(route_output, engine="openpyxl") as writer:
        pd.DataFrame([{
            "调出仓库": "杭州仓库", "调入仓库": "湖州仓库", "可用时间": "全年",
            "理论发车时间": "08:00", "理论到货时间": "14:00", "时间要求": "当日到达",
        }]).to_excel(writer, index=False)

    with TestClient(main.app) as client:
        created_material = client.post("/api/master-data/materials", json=material)
        material["safety_stock"] = 120
        updated_material = client.put("/api/master-data/materials/M100", json=material)
        created_supplier = client.post("/api/master-data/suppliers", json=supplier)
        supplier["loading_qty"] = 550
        updated_supplier = client.put(
            f"/api/master-data/suppliers/{quote(created_supplier.json()['supplier_key'], safe='')}", json=supplier,
        )
        imported_warehouse = client.post(
            "/api/master-data/warehouses/import",
            files={"file": ("warehouses.csv", warehouse_csv, "text/csv")},
        )
        imported_route = client.post(
            "/api/master-data/transfer-routes/import",
            files={"file": ("routes.xlsx", route_output.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        csv_template = client.get("/api/master-data/materials/template?format=csv")
        xlsx_template = client.get("/api/master-data/suppliers/template?format=xlsx")

    assert created_material.status_code == 200
    assert updated_material.json()["safety_stock"] == 120
    assert updated_supplier.json()["loading_qty"] == 550
    assert imported_warehouse.json()["inserted"] == 1
    assert imported_route.json()["inserted"] == 1
    assert "物料编码" in csv_template.content.decode("utf-8-sig")
    assert xlsx_template.headers["content-type"].startswith("application/vnd.openxmlformats")


def test_master_data_rejects_invalid_category_and_arrival_mode(monkeypatch, tmp_path: Path) -> None:
    configure_temp_database(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        material = client.post("/api/master-data/materials", json={
            "material_code": "M1", "material_name": "物料", "material_category": "其他",
        })
        supplier = client.post("/api/master-data/suppliers", json={
            "supplier_code": "S1", "supplier_name": "供应商", "material_code": "M1",
            "material_name": "物料", "arrival_mode": "未知",
        })
    assert material.status_code == 422
    assert supplier.status_code == 422
