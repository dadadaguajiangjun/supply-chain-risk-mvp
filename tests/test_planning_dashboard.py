from datetime import date
import io

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import database, main
from app.operational_service import persist_operational_data
from app.excel_service import ParsedWorkbook
from app.planning_service import planning_dashboard, operational_history


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(database, 'DB_PATH', tmp_path / 'test.db')
    monkeypatch.setattr(database, 'UPLOAD_DIR', tmp_path / 'uploads')
    monkeypatch.setattr(main, 'UPLOAD_DIR', tmp_path / 'uploads')
    with TestClient(main.app) as client:
        for name in ['A仓库', 'B仓库']:
            client.post('/api/master-data/warehouses', json={'warehouse_english': name[0], 'warehouse_name': name})
        yield client


def load(kind, rows, run='r1'):
    persist_operational_data(run, date(2026, 9, 16), {
        kind: ParsedWorkbook(kind, kind, kind+'.xlsx', pd.DataFrame(rows), [])})


def board(warehouse='B仓库'):
    return planning_dashboard(date(2026, 9, 16), warehouse=warehouse)['rows'][0]


def test_dashboard_typed_search_and_exact_selection(client):
    for code, name in [('M1', '鲜牛奶'), ('M10', '燕麦奶'), ('T1', '红茶')]:
        client.post('/api/master-data/materials', json={'material_code': code, 'material_name': name, 'material_category': '短保'})
        client.post('/api/master-data/material-controls', json={'material_owner': '张三', 'purchase_owner': '李四', 'material_code': code, 'material_name': name})
    params = {'business_date': '2026-09-16', 'material_code': '奶', 'search_fields': ['material_code']}
    result = client.get('/api/planning-dashboard', params=params).json()
    assert result['total'] == 4
    assert {r['material_code'] for r in result['rows']} == {'M1', 'M10'}
    params.update(material_owner='张', purchase_owner='李', warehouse='b', material_category='短',
                  search_fields=['material_code', 'material_owner', 'purchase_owner', 'warehouse', 'material_category'])
    assert client.get('/api/planning-dashboard', params=params).json()['total'] == 2
    assert client.get('/api/planning-dashboard', params={'business_date':'2026-09-16', 'material_code':'M1'}).json()['total'] == 2
    assert client.get('/api/planning-dashboard', params={'business_date':'2026-09-16', 'material_code':'m1', 'search_fields':'material_code'}).json()['total'] == 4
    assert client.get('/api/planning-dashboard', params={'business_date':'2026-09-16', 'material_code':'不存在', 'search_fields':'material_code'}).json()['total'] == 0
    assert client.post('/api/master-data/suppliers', json={'supplier_code':'S1', 'supplier_name':'杭州奶业', 'material_code':'M1', 'material_name':'鲜牛奶', 'warehouse':'B仓库'}).status_code == 200
    assert client.post('/api/master-data/transfer-routes', json={'from_warehouse':'A仓库', 'to_warehouse':'B仓库'}).status_code == 200
    filtered = client.get('/api/planning-dashboard', params={'business_date':'2026-09-16', 'supplier':'奶业', 'transfer_area':'a仓', 'search_fields':['supplier','transfer_area']}).json()
    assert filtered['total'] == 1 and filtered['rows'][0]['material_code'] == 'M1'
    assert client.get('/api/planning-dashboard', params={'business_date':'2026-09-16', 'supplier':'奶业'}).json()['total'] == 0


def test_purchase_reconciliation_text_filters_pagination_and_remark(client):
    load('purchase_order', [
        {'order_no': 'PO-01', 'source_date': '2026-09-15', 'supplier_name': '杭州奶业', 'warehouse': 'B仓库', 'material_code': 'M1', 'material_name': '鲜牛奶', 'purchase_qty': 100, 'remaining_qty': 80, 'delivery_date': '2026-09-16'},
        {'order_no': 'PO-02', 'source_date': '2026-09-15', 'supplier_name': '杭州奶业', 'warehouse': 'B仓库', 'material_code': 'M2', 'material_name': '燕麦奶', 'purchase_qty': 90, 'remaining_qty': 90, 'delivery_date': '2026-09-17'},
        {'order_no': 'OTHER', 'source_date': '2026-09-15', 'supplier_name': '茶供应商', 'warehouse': 'A仓库', 'material_code': 'T1', 'material_name': '红茶', 'purchase_qty': 50, 'remaining_qty': 50, 'delivery_date': '2026-09-16'},
    ])
    load('inbound_daily', [{'transaction_no':'REC1', 'reference_no3':'PO-01', 'warehouse':'B仓库', 'material_code':'M1', 'inbound_qty':20, 'source_date':'2026-09-16'}])
    params = {'business_date':'2026-09-16', 'search':'po-', 'warehouse':'b', 'supplier':' 奶业 ', 'material':'奶', 'page':1, 'page_size':1}
    payload = client.get('/api/purchase-reconciliation', params=params).json()
    assert payload['total'] == 2 and len(payload['rows']) == 1
    first = payload['rows'][0]
    assert first['order_no'] == 'PO-01' and first['flux_received_qty'] == 20
    params['page'] = 2
    assert client.get('/api/purchase-reconciliation', params=params).json()['rows'][0]['order_no'] == 'PO-02'
    assert client.put(f"/api/purchase-records/{first['id']}/remark", json={'remark':'核对说明'}).status_code == 200
    exact = client.get('/api/purchase-reconciliation', params={'business_date':'2026-09-16', 'search':'PO-01', 'material':'m1'}).json()
    assert exact['rows'][0]['remark'] == '核对说明'
    assert len(client.get('/api/purchase-reconciliation', params={'business_date':'2026-09-16'}).json()['rows']) == 3
    assert client.get('/api/purchase-reconciliation', params={'business_date':'2026-09-16','search':'不存在'}).json()['total'] == 0
    assert client.get('/api/purchase-reconciliation', params={'business_date':'2026-09-16','days':0}).json()['total'] == 2


def test_search_ui_and_cache_revalidation(client):
    for url in ['/', '/purchase-reconciliation', '/static/planning.js', '/static/purchase-reconciliation.js']:
        response = client.get(url)
        assert response.status_code == 200
        assert 'no-cache' in response.headers['cache-control']
        if not url.startswith('/static/'):
            assert 'no-store' in response.headers['cache-control']
    html = client.get('/').text
    assert 'id="planningMaterial" type="search"' in html and '<datalist id="materialOptions"' in html
    html = client.get('/purchase-reconciliation').text
    for name in ['reconcileKeyword', 'reconcileSupplier', 'reconcileWarehouse', 'reconcileMaterial']:
        assert f'id="{name}" type="search"' in html
    assert 'purchase-reconciliation.js?v=' in html


def test_owner_all_ten_materials_all_warehouses_without_facts(client):
    csv = '物料负责人,采购负责人,物料编码,物料名称,启用\n' + ''.join(f'A,采购甲,M{i:02d},物料{i},1\n' for i in range(10))
    result = client.post('/api/master-data/material-controls/import', files={'file': ('control.csv', csv.encode('utf-8-sig'), 'text/csv')})
    assert result.json()['inserted'] == 10
    result = client.get('/api/planning-dashboard', params={'business_date':'2026-09-16', 'material_owner':'A'}).json()
    assert len(result['rows']) == 20
    assert len({r['material_code'] for r in result['rows']}) == 10
    assert {r['warehouse'] for r in result['rows']} == {'A仓库', 'B仓库'}
    assert all(r['inventory'] == 0 and r['purchase_owner'] == '采购甲' for r in result['rows'])
    page = client.get('/api/planning-dashboard', params={'business_date':'2026-09-16','material_owner':'A','page':2,'page_size':3}).json()
    assert page['total'] == 20 and len(page['rows']) == 3
    assert page['rows'] == result['rows'][3:6]
    rows = client.get('/api/master-data/material-controls?search=A').json()['rows']
    changed = {**rows[0], 'enabled':False}
    assert client.put(f"/api/master-data/material-controls/{rows[0]['id']}", json=changed).status_code == 200
    assert len(client.get('/api/planning-dashboard', params={'business_date':'2026-09-16','material_owner':'A'}).json()['rows']) == 18


def test_control_excel_leading_zero_and_template(client):
    data = io.BytesIO()
    pd.DataFrame([{'物料负责人':'A','采购负责人':'B','物料编码':'00123','物料名称':'原料','启用':1}]).to_excel(data,index=False)
    response=client.post('/api/master-data/material-controls/import',files={'file':('control.xlsx',data.getvalue())})
    assert response.json()['inserted']==1
    assert client.get('/api/master-data/material-controls').json()['rows'][0]['material_code']=='00123'
    assert '物料负责人' in client.get('/api/master-data/material-controls/template?format=csv').content.decode('utf-8-sig')


def test_purchase_wms_partial_multiple_receipts_and_manual_replacement(client):
    plan={'supplier_name':'S', 'material_code':'M1', 'order_qty':100, 'arrival_warehouse':'B仓库',
          'order_time':'2026-09-15T10:00:00','expected_arrival_time':'2026-09-18T10:00:00'}
    client.post('/api/master-data/materials',json={'material_code':'M1','material_name':'物料一'})
    created=client.post('/api/purchase-plans',json=plan).json()
    assert board()['purchase_data_future_7d']==100
    load('purchase_order',[{'order_no':'PO1','source_date':'2026-09-15','supplier_name':'S', 'material_code':'M1',
        'warehouse':'B仓库','purchase_qty':80,'remaining_qty':10,'delivery_date':'2026-09-18'}])
    load('inbound_daily',[
        {'transaction_no':'W1','reference_no3':'PO1','warehouse':'B仓库','material_code':'M1','inbound_qty':20},
        {'transaction_no':'W2','reference_no3':'PO1','warehouse':'B仓库','material_code':'M1','inbound_qty':15},
        {'transaction_no':'W3','reference_no3':'PO1','warehouse':'A仓库','material_code':'M1','inbound_qty':999},
        {'transaction_no':'W4','reference_no3':'PO1','warehouse':'B仓库','material_code':'M2','inbound_qty':999}])
    assert board()['purchase_data_future_7d']==45
    history=operational_history('purchase')
    manual=next(r for r in history if r['source']=='manual')
    assert manual['resolved_order_no']=='PO1'
    assert manual['quantity_difference']==20
    assert manual['counts_as_manual'] is False
    formal=next(r for r in history if r['source']=='formal')
    assert len(formal['wms_records'])==2
    # Formal quantity isn't removed just by a human clicking completed.
    response=client.put(f"/api/operations/purchase/formal/{formal['id']}/execution",json={'execution_status':'已完成','operator':'甲'})
    assert response.status_code==200
    assert board()['purchase_data_future_7d']==45


def test_transfer_actual_signs_boundaries_manual_and_wms_not_added(client):
    load('transfer',[
        {'order_no':'T1','material_code':'M1','from_warehouse':'A仓库','to_warehouse':'B仓库','transfer_qty':50,
         'outbound_qty':40,'inbound_qty':30,'actual_departure_date':'2026-09-13','actual_arrival_date':'2026-09-15'},
        {'order_no':'T2','material_code':'M1','from_warehouse':'A仓库','to_warehouse':'B仓库','transfer_qty':500,
         'outbound_qty':500,'inbound_qty':500,'actual_departure_date':'2026-09-16','actual_arrival_date':'2026-09-16'},
        {'order_no':'T3','material_code':'M1','from_warehouse':'A仓库','to_warehouse':'B仓库','transfer_qty':500,
         'outbound_qty':500,'inbound_qty':500,'actual_departure_date':'2026-09-12','actual_arrival_date':'2026-09-12'}])
    load('inbound_daily',[{'transaction_no':'WT','reference_no3':'T1','material_code':'M1','warehouse':'B仓库','inbound_qty':35}])
    assert board()['transfer_summary_3d']==30
    assert board('A仓库')['transfer_summary_3d']==-40
    plan={'request_no':'T1','from_warehouse':'A仓库','to_warehouse':'B仓库','material_code':'M1','planned_qty':60,
          'expected_departure_time':'2026-09-14T08:00:00','expected_arrival_time':'2026-09-15T12:00:00'}
    client.post('/api/transfer-plans',json=plan)
    assert board()['transfer_summary_3d']==30
    plan['request_no']='T-NEW'
    added=client.post('/api/transfer-plans',json=plan).json()
    assert board()['transfer_summary_3d']==90
    assert board('A仓库')['transfer_summary_3d']==-100
    client.put(f"/api/operations/transfer/manual/{added['id']}/execution",json={'execution_status':'已完成','operator':'甲'})
    assert board()['transfer_summary_3d']==30
    formal=next(r for r in operational_history('transfer') if r['source']=='formal' and r['request_no']=='T1')
    assert formal['received_difference']==5


def test_history_uses_outbound_today_uses_booking_and_snapshot_not_cumulative(client):
    load('stock',[{'warehouse':'B仓库','material_code':'M1','stock_qty':100,'allocated_qty':20,'frozen_qty':5}])
    # Different filename/run on same date is another snapshot, not extra stock.
    persist_operational_data('r2',date(2026,9,16),{'stock':ParsedWorkbook('stock','stock','renamed.xlsx',pd.DataFrame([
        {'warehouse':'B仓库','material_code':'M1','stock_qty':90,'allocated_qty':10,'frozen_qty':5}]),[])})
    load('outbound',[{'warehouse':'B仓库','material_code':'M1','stat_date':d,'allocated_qty':q,'picked_qty':q-1} for d,q in
                     [('2026-09-08',1000),('2026-09-09',14),('2026-09-13',21),('2026-09-15',35),('2026-09-16',1000)]])
    load('booking_order',[{'warehouse':'B仓库','material_code':'M1','delivery_date':d,'ordered_qty':q,'demand_qty':q-2} for d,q in
                          [('2026-09-15',900),('2026-09-16',40),('2026-09-17',900)]])
    row=board()
    assert row['inventory']==90 and row['available_stock']==75
    assert row['booking_avg_7d']==10 and row['booking_3d']==56
    assert row['saleable_booking_avg_7d']==67/7 and row['saleable_booking_3d']==54
    assert row['booking_today']==40 and row['saleable_booking_today']==38


def test_execution_validation_audit_survives_import_and_history_filters(client):
    original=[{'order_no':'PO','warehouse':'B仓库','material_code':'M1','purchase_qty':50,'delivery_date':'2026-09-18'}]
    load('purchase_order',original)
    row=client.get('/api/operations/purchase').json()['rows'][0]
    url=f"/api/operations/purchase/formal/{row['id']}/execution"
    assert client.put(url,json={'execution_status':'延误','operator':'甲'}).status_code==422
    assert client.put(url,json={'execution_status':'不支持','operator':'甲','execution_note':'说明'}).status_code==422
    assert client.put(url,json={'execution_status':'延误','operator':'甲','execution_note':'供应商延迟两天'}).status_code==200
    load('purchase_order',original,'r2')
    rows=client.get('/api/operations/purchase?execution_status=延误&search=PO').json()['rows']
    assert len(rows)==1 and rows[0]['execution_note']=='供应商延迟两天'
    audit=client.get(f"/api/operations/purchase/formal/{row['id']}/audit").json()['rows']
    assert len(audit)==1 and audit[0]['operator']=='甲'


def test_same_order_two_lines_wms_only_deducted_once(client):
    client.post('/api/master-data/materials',json={'material_code':'M1','material_name':'物料'})
    with database.db_connection() as conn:
        for i in range(2):
            conn.execute('INSERT INTO purchase_record (source_key,last_run_id,order_no,material_code,warehouse,purchase_qty,delivery_date) VALUES (?,?,?,?,?,?,?)',
                         (f'p{i}','r','PO','M1','B仓库',50,'2026-09-18'))
    load('inbound_daily',[{'transaction_no':'W','reference_no3':'PO','material_code':'M1','warehouse':'B仓库','inbound_qty':60}])
    assert board()['purchase_data_future_7d']==40


def test_business_import_does_not_overwrite_manual_material(client):
    client.post('/api/master-data/materials',json={'material_code':'M1','material_name':'维护名称','material_category':'短保'})
    load('stock',[{'warehouse':'B仓库','material_code':'M1','material_name':'外部名称','stock_qty':1}])
    assert board()['material_name']=='维护名称'


def test_purchase_date_boundaries_cancelled_overreceipt_and_invalid_manual(client):
    load('purchase_order', [
        {'order_no':key,'warehouse':'B仓库','material_code':'M1','purchase_qty':100,'delivery_date':d,'close_status':status}
        for key,d,status in [('TODAY','2026-09-16',''),('T1','2026-09-17','未关闭'),('T7','2026-09-23','未关闭'),('T8','2026-09-24',''),('CLOSED','2026-09-20','已关闭')]])
    load('inbound_daily',[{'transaction_no':'W1','reference_no3':'T1','warehouse':'B仓库','material_code':'M1','inbound_qty':120}])
    assert board()['purchase_data_future_7d']==100
    record=next(r for r in operational_history('purchase') if r['display_no']=='T1')
    assert record['flux_received_qty']==120 and record['remaining_wms_qty']==0
    assert client.post('/api/purchase-plans',json={'supplier_name':'S','material_code':'M1','arrival_warehouse':'B仓库','order_qty':-1}).status_code==422


def test_ambiguous_manual_candidate_requires_binding(client):
    load('purchase_order', [
        {'order_no':key,'warehouse':'B仓库','material_code':'M1','supplier_name':'S','purchase_qty':50,
         'source_date':'2026-09-15','delivery_date':'2026-09-18'} for key in ['P1','P2']])
    plan={'supplier_name':'S','material_code':'M1','arrival_warehouse':'B仓库','order_qty':50,
          'order_time':'2026-09-15T00:00:00','expected_arrival_time':'2026-09-18T00:00:00'}
    response=client.post('/api/purchase-plans',json=plan).json()
    manual=next(r for r in operational_history('purchase') if r['source']=='manual')
    assert manual['link_status']=='多个候选，请绑定单号'
    assert board()['purchase_data_future_7d']==100
    plan['linked_order_no']='P1'
    client.put(f"/api/purchase-plans/{response['id']}",json=plan)
    manual=next(r for r in operational_history('purchase') if r['source']=='manual')
    assert manual['resolved_order_no']=='P1' and manual['counts_as_manual'] is False


def test_dashboard_reuses_aggregates_and_invalidates_after_commit(client, monkeypatch):
    import os
    import app.planning_service as service
    load('stock',[{'warehouse':'B仓库','material_code':'M1','stock_qty':100}])
    calls = []
    original = service._prepare_dashboard
    def counted(day):
        calls.append(day)
        return original(day)
    monkeypatch.setattr(service, '_prepare_dashboard', counted)
    first = service.planning_dashboard(date(2026,9,16), page=1, page_size=1)
    second = service.planning_dashboard(date(2026,9,16), page=2, page_size=1, filters_version=first['filters_version'])
    assert len(calls)==1 and second['filters'] is None
    assert second['rows'][0]['inventory']==100
    stat = database.DB_PATH.stat()
    with database.db_connection() as conn:
        conn.execute('UPDATE stock_snapshot SET stock_qty=150')
    # Exercise coalesced Windows timestamps: a commit must still invalidate.
    os.utime(database.DB_PATH, ns=(stat.st_atime_ns,stat.st_mtime_ns))
    changed = service.planning_dashboard(date(2026,9,16), page=2,page_size=1,filters_version=first['filters_version'])
    assert len(calls)==2 and changed['rows'][0]['inventory']==150
    assert changed['filters'] is not None


def test_master_and_mapping_server_pagination_filters(client):
    for i in range(7):
        client.post('/api/master-data/materials',json={'material_code':f'M{i}','material_name':f'物料{i}'})
    result=client.get('/api/master-data/materials?page=2&page_size=3').json()
    assert result['total']==7 and [r['material_code'] for r in result['rows']]==['M3','M4','M5']
    for i in range(5):
        client.post('/api/warehouse-mappings',json={'source_system':'stock','raw_warehouse_name':f'原仓{i}',
            'standard_warehouse_code':f'W{i}','standard_warehouse_name':f'仓库{i}','enabled':i%2==0})
    mapping=client.get('/api/warehouse-mappings?page=2&page_size=1&enabled=1&source=stock&search=原仓').json()
    assert mapping['total']==3 and len(mapping['rows'])==1
    assert mapping['rows'][0]['standard_warehouse_code']=='W2'


def test_separate_pages_and_preserved_mapping_and_import_controls(client):
    home=client.get('/').text
    importer=client.get('/data-import').text
    master=client.get('/master-data').text
    assert 'id="planningRows"' in home
    assert 'id="uploadForm"' not in home and 'id="mappingForm"' not in home
    assert 'id="uploadForm"' in importer and 'id="qualityRows"' in importer
    assert 'id="mappingForm"' in master and 'data-master="warehouse-mappings"' in master
    assert 'id="mappingForm"' not in importer
    assert 'workspace-topbar' in master and 'href="#mapping"' in master
    for html in [home,importer,client.get('/data-edit').text,client.get('/purchase-reconciliation').text]:
        assert 'workspace-topbar' in html and 'href="/data-import?ui=' in html
        nav=html.split('<nav>')[1].split('</nav>')[0]
        assert 'href="/master-data' not in nav and '#mapping' not in nav
