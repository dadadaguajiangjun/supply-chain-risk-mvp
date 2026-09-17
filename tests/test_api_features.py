from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

import app.database as database
import app.main as main


def xlsx_bytes(frame: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False)
    return output.getvalue()


def configure_temp_database(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(database, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(main, "UPLOAD_DIR", upload_dir)
    database.initialize_database()


def test_stale_stock_blocks_official_and_force_creates_simulation_with_trace(monkeypatch, tmp_path: Path) -> None:
    configure_temp_database(monkeypatch, tmp_path)
    stock = xlsx_bytes(pd.DataFrame([{
        "库存日期": "2026-09-14", "仓库名称": "湖州DC", "产品编码": "A",
        "产品名称": "物料A", "库存状态": "ZP", "库存数量": 100,
    }]))
    booking = xlsx_bytes(pd.DataFrame([{
        "送货日期": "2026-09-17", "发货仓库": "湖州仓库", "物料编码": "A",
        "物料名称": "物料A", "计划发货数量": 120,
    }]))

    with TestClient(main.app) as client:
        blocked = client.post(
            "/api/runs",
            data={"business_date": "2026-09-16", "safety_days": "1"},
            files=[
                ("files", ("库存余量_20260914.xlsx", stock, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
                ("files", ("报货订单实时_20260916.xlsx", booking, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
            ],
        )
        assert blocked.status_code == 422
        assert blocked.json()["status"] == "blocked"
        assert blocked.json()["requires_force_calculation"] is True

        forced = client.post(
            "/api/runs",
            data={"business_date": "2026-09-16", "safety_days": "1", "force_calculation": "true"},
            files=[
                ("files", ("库存余量_20260914.xlsx", stock, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
                ("files", ("报货订单实时_20260916.xlsx", booking, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
            ],
        )
        payload = forced.json()
        assert forced.status_code == 200
        assert payload["result_mode"] == "simulation"
        assert payload["quality_reports"][0]["original_rows"] == 1
        risk = payload["rows"][0]

        detail = client.get(f"/api/runs/{payload['run_id']}/risks/{risk['id']}")
        assert detail.status_code == 200
        assert detail.json()["run"]["result_mode"] == "simulation"
        assert detail.json()["source_records"]["demand"][0]["demand_qty"] == 120


def test_warehouse_mapping_api_supports_create_update_and_disable(monkeypatch, tmp_path: Path) -> None:
    configure_temp_database(monkeypatch, tmp_path)
    item = {
        "source_system": "stock", "raw_warehouse_code": "W1", "raw_warehouse_name": "湖州DC",
        "standard_warehouse_code": "WH001", "standard_warehouse_name": "湖州仓库",
        "special_alias": "湖州RDC", "enabled": True,
        "effective_start_date": "2026-01-01", "effective_end_date": None, "remark": "测试",
    }
    with TestClient(main.app) as client:
        created = client.post("/api/warehouse-mappings", json=item)
        assert created.status_code == 200
        mapping_id = created.json()["id"]
        item["enabled"] = False
        item["remark"] = "停用"
        updated = client.put(f"/api/warehouse-mappings/{mapping_id}", json=item)
        assert updated.status_code == 200
        assert updated.json()["enabled"] == 0
        assert client.get("/api/warehouse-mappings").json()["rows"][0]["remark"] == "停用"


def test_dashboard_reports_data_readiness_without_risk_interpretation(monkeypatch, tmp_path: Path) -> None:
    configure_temp_database(monkeypatch, tmp_path)
    with database.db_connection() as conn:
        conn.execute(
            """INSERT INTO upload_run
               (id,business_date,created_at,status,result_mode,stock_snapshot_date,stock_date_status)
               VALUES (?,?,?,?,?,?,?)""",
            ("run-dashboard", "2026-09-16", "2026-09-16T08:00:00+00:00", "completed", "official", "2026-09-16", "current"),
        )
        conn.executemany(
            """INSERT INTO uploaded_file
               (run_id,original_name,detected_type,row_count,status,message)
               VALUES (?,?,?,?,?,?)""",
            [
                ("run-dashboard", "库存余量.xlsx", "stock", 10, "success", "读取成功"),
                ("run-dashboard", "报货订单实时.xlsx", "booking_order", 8, "success", "读取成功"),
            ],
        )
        conn.execute(
            """INSERT INTO data_quality_report
               (run_id,file_type,filename,original_rows,valid_rows,deleted_rows,
                duplicate_count,file_date,file_date_status,calculation_allowed)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("run-dashboard", "stock", "库存余量.xlsx", 10, 9, 1, 1, "2026-09-16", "current", 1),
        )
        conn.execute(
            """INSERT INTO warehouse_mapping
               (source_system,raw_warehouse_code,standard_warehouse_code,standard_warehouse_name,enabled)
               VALUES (?,?,?,?,?)""",
            ("stock", "W1", "WH001", "中心仓", 1),
        )

    with TestClient(main.app) as client:
        response = client.get("/api/dashboard")
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["data_ready"] is True
    assert payload["summary"]["successful_file_count"] == 2
    assert payload["quality_totals"]["valid_rows"] == 9
    assert payload["mapping"]["enabled"] == 1
    assert {item["file_type"] for item in payload["file_readiness"] if item["present"]} == {"stock", "booking_order"}
    assert "risk" not in payload["summary"]


def test_quality_only_upload_stops_before_risk_calculation(monkeypatch, tmp_path: Path) -> None:
    configure_temp_database(monkeypatch, tmp_path)
    stock = xlsx_bytes(pd.DataFrame([{
        "库存日期": "2026-09-10", "仓库名称": "湖州DC", "产品编码": "A",
        "产品名称": "物料A", "库存状态": "ZP", "库存数量": 100,
    }]))
    booking = xlsx_bytes(pd.DataFrame([{
        "送货日期": "2026-09-17", "发货仓库": "湖州仓库", "物料编码": "A",
        "物料名称": "物料A", "计划发货数量": 120,
    }]))
    with TestClient(main.app) as client:
        response = client.post(
            "/api/runs",
            data={"business_date": "2026-09-16", "quality_only": "true"},
            files=[
                ("files", ("库存余量.xlsx", stock, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
                ("files", ("报货订单实时.xlsx", booking, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
            ],
        )
        dashboard_response = client.get("/api/dashboard?business_date=2026-09-16&run_status=validated")
    assert response.status_code == 200
    assert response.json()["status"] == "validated"
    assert response.json()["result_mode"] == "quality_only"
    assert response.json()["rows"] == []
    assert dashboard_response.status_code == 200
    assert dashboard_response.json()["latest_run"]["status"] == "validated"
