"""存销事实看板。手工计划、正式单据、收货记录分别保存，只计一次业务量。"""
from __future__ import annotations

from collections import defaultdict, OrderedDict
from functools import lru_cache
from threading import RLock
from pathlib import Path
import hashlib

from . import database
from datetime import date, timedelta
import re
from typing import Any

from .database import db_connection
from .excel_service import normalize_warehouse_name


def wh(value: Any) -> str:
    return _warehouse_name(str(value or ''))


@lru_cache(maxsize=4096)
def _warehouse_name(value: str) -> str:
    return normalize_warehouse_name(value)


def num(value: Any) -> float:
    return float(value or 0)


def day(value: Any) -> str:
    return str(value or '')[:10]


def active(row: dict) -> bool:
    # 金蝶的“未关闭”和“未取消”是有效状态，不能按“关闭”子串排除。
    return not any(re.search(r'(?<!未)(关闭|取消|作废)', str(row.get(field) or ''))
                   or str(row.get(field) or '').strip().lower() in {'closed','cancelled','canceled','void'}
                   for field in ('order_status','close_status','document_status','transport_status','status'))


def _read(conn, table: str) -> list[dict]:
    return [dict(row) for row in conn.execute(f'SELECT * FROM {table}')]


def _business_data(conn, kind: str, inbound_rows=None) -> tuple[list[dict], list[dict]]:
    records = _read(conn, f'{kind}_record')
    plans = _read(conn, f'{kind}_plan')
    number_field = 'order_no' if kind == 'purchase' else 'request_no'
    receipts = defaultdict(list)
    for receipt in (inbound_rows if inbound_rows is not None else _read(conn, 'inbound_record')):
        if receipt.get('reference_no3'):
            receipts[(receipt['reference_no3'], receipt.get('material_code'), wh(receipt.get('warehouse_name')))].append(receipt)
    groups = defaultdict(list)
    for record in records:
        warehouse = wh(record.get('warehouse') if kind == 'purchase' else record.get('to_warehouse'))
        key = (record.get(number_field), record.get('material_code'), warehouse)
        groups[key].append(record)
    # One receipt pool per order/material/destination. Do not subtract the same
    # WMS receipt repeatedly when a formal order contains multiple material lines.
    for key, group in groups.items():
        matched = receipts.get(key, []) if key[0] else []
        remaining_received = sum(num(r.get('received_qty')) for r in matched)
        sorted_group = sorted(group, key=lambda r: (str(r.get('delivery_date') or ''), r['id']))
        for index, record in enumerate(sorted_group):
            quantity = num(record.get('purchase_qty') if kind == 'purchase' else record.get('planned_qty'))
            allocated = min(max(quantity, 0), max(remaining_received, 0))
            if index == len(sorted_group)-1:
                allocated = max(remaining_received, 0)
            remaining_received -= allocated
            record.update(flux_received_qty=allocated, flux_order_received_qty=sum(num(r.get('received_qty')) for r in matched),
                          flux_expected_qty=sum(num(r.get('expected_qty')) for r in matched),
                          flux_record_count=len(matched), wms_records=matched,
                          wms_last_inbound_date=max((str(r.get('inbound_date') or '') for r in matched), default=''),
                          remaining_wms_qty=max(quantity - allocated, 0), manual_records=[])
            system_received = num(record.get('purchase_qty')) - num(record.get('remaining_qty')) if kind == 'purchase' else num(record.get('inbound_qty'))
            record['received_difference'] = allocated - system_received
            record['reconciliation_status'] = '未匹配WMS' if not matched else ('一致' if abs(record['received_difference']) < 1e-6 else '数量差异')
            planned_date = day(record.get('delivery_date') or record.get('expected_arrival_time'))
            actual_date = day(record.get('wms_last_inbound_date'))
            record['arrival_delay_days'] = (date.fromisoformat(actual_date) - date.fromisoformat(planned_date)).days if actual_date and planned_date else None

    formal_index = defaultdict(list)
    composite_index = defaultdict(list)
    for record in records:
        material = record.get('material_code')
        dest = wh(record.get('warehouse') if kind == 'purchase' else record.get('to_warehouse'))
        formal_index[(record.get(number_field), material, dest)].append(record)
        if kind == 'purchase':
            business_key = (material, dest, record.get('supplier_name'), day(record.get('purchase_date')))
        else:
            business_key = (material, dest, wh(record.get('from_warehouse')), day(record.get('expected_departure_time')), day(record.get('expected_arrival_time')))
        composite_index[business_key].append(record)
    for plan in plans:
        dest = wh(plan.get('arrival_warehouse') if kind == 'purchase' else plan.get('to_warehouse'))
        explicit = plan.get('linked_order_no') or (plan.get('request_no') if kind == 'transfer' else None)
        if explicit:
            candidates = formal_index.get((explicit, plan['material_code'], dest), [])
            if kind == 'transfer':
                candidates = [r for r in candidates if wh(r.get('from_warehouse')) == wh(plan.get('from_warehouse'))]
        elif kind == 'purchase' and plan.get('order_time'):
            candidates = composite_index.get((plan['material_code'], dest, plan.get('supplier_name'), day(plan.get('order_time'))), [])
        elif kind == 'transfer' and plan.get('expected_departure_time') and plan.get('expected_arrival_time'):
            candidates = composite_index.get((plan['material_code'], dest, wh(plan.get('from_warehouse')), day(plan.get('expected_departure_time')), day(plan.get('expected_arrival_time'))), [])
        else:
            candidates = []
        order_numbers = {r.get(number_field) for r in candidates if r.get(number_field)}
        matched_no = next(iter(order_numbers)) if len(order_numbers) == 1 else None
        plan['resolved_order_no'] = matched_no
        plan['temporary_no'] = f"{'MP' if kind == 'purchase' else 'MT'}-{plan['id']:06d}"
        plan['link_status'] = '已关联正式单据' if matched_no else ('多个候选，请绑定单号' if len(order_numbers) > 1 else '尚无匹配正式单据，临时计入')
        receipt_no = matched_no or explicit or plan['temporary_no']
        matched_receipts = receipts.get((receipt_no, plan['material_code'], dest), [])
        plan['wms_records'] = matched_receipts
        plan['flux_received_qty'] = sum(num(r.get('received_qty')) for r in matched_receipts)
        plan['wms_last_inbound_date'] = max((str(r.get('inbound_date') or '') for r in matched_receipts), default='')
        qty = num(plan.get('order_qty') if kind == 'purchase' else plan.get('planned_qty'))
        plan['remaining_wms_qty'] = max(qty - plan['flux_received_qty'], 0)
        plan['formal_qty'] = sum(num(r.get('purchase_qty') if kind == 'purchase' else r.get('planned_qty')) for r in candidates) if matched_no else None
        plan['quantity_difference'] = qty - plan['formal_qty'] if plan['formal_qty'] is not None else None
        plan['counts_as_manual'] = not matched_no and len(order_numbers) <= 1 and active(plan) and plan.get('execution_status') != '已完成'
        for record in candidates if matched_no else []:
            record['manual_records'].append({k: plan[k] for k in ('id', 'temporary_no', 'formal_qty', 'quantity_difference', 'execution_status')})
    return records, plans


def operational_history(kind: str) -> list[dict]:
    with db_connection() as conn:
        records, plans = _business_data(conn, kind)
    number_field = 'order_no' if kind == 'purchase' else 'request_no'
    result = []
    for source, rows in (('formal', records), ('manual', plans)):
        for row in rows:
            result.append({**row, 'source': source,
                           'display_no': row.get(number_field) if source == 'formal' else row['temporary_no'],
                           'quantity': row.get('purchase_qty' if kind == 'purchase' else 'planned_qty') if source == 'formal' else row.get('order_qty' if kind == 'purchase' else 'planned_qty'),
                           'warehouse_display': row.get('warehouse') or row.get('arrival_warehouse') or f"{row.get('from_warehouse', '')}→{row.get('to_warehouse', '')}",
                           'business_time': row.get('delivery_date') or row.get('expected_arrival_time'),
                           'original_status': row.get('order_status') or row.get('transport_status') or row.get('status') or row.get('document_status')})
    return sorted(result, key=lambda r: (str(r.get('business_time') or ''), r['id']), reverse=True)


def _prepare_dashboard(business_date: date) -> dict:
    current = business_date.isoformat()
    past7 = (business_date - timedelta(days=7)).isoformat()
    past3 = (business_date - timedelta(days=3)).isoformat()
    yesterday = (business_date - timedelta(days=1)).isoformat()
    future7 = (business_date + timedelta(days=7)).isoformat()
    with db_connection() as conn:
        conn.execute("BEGIN")
        materials = {r['material_code']: r for r in _read(conn, 'material_master')}
        controls = [r for r in _read(conn, 'material_control') if r['enabled']]
        warehouse_rows = _read(conn, 'warehouse_master')
        warehouses = sorted({r['warehouse_name'] for r in warehouse_rows if r['warehouse_name']})
        routes = [r for r in _read(conn, 'transfer_route') if r['enabled']]
        suppliers = _read(conn, 'supplier_master')
        snapshot_date = conn.execute('SELECT MAX(business_date) FROM stock_snapshot WHERE business_date<=?', (current,)).fetchone()[0]
        # One snapshot batch per warehouse/date: re-uploading another filename
        # must not multiply stock. Older batches remain stored for traceability.
        stock_rows = [dict(r) for r in conn.execute('''
            WITH latest AS (
                SELECT warehouse_name,run_id,ROW_NUMBER() OVER (
                    PARTITION BY warehouse_name ORDER BY MAX(updated_at) DESC,MAX(id) DESC) AS rn
                FROM stock_snapshot WHERE business_date=? GROUP BY warehouse_name,run_id
            ) SELECT s.* FROM stock_snapshot s JOIN latest l
                ON l.warehouse_name=s.warehouse_name AND l.run_id=s.run_id AND l.rn=1
                WHERE s.business_date=?''', (snapshot_date, snapshot_date))]
        history = [dict(r) for r in conn.execute('SELECT * FROM demand_history WHERE date(stat_date) BETWEEN ? AND ?', (past7, yesterday))]
        previous_end = business_date.replace(day=1) - timedelta(days=1)
        previous_start = previous_end.replace(day=1)
        last_year_start = previous_start.replace(year=previous_start.year-1)
        import calendar
        last_year_end = last_year_start.replace(day=calendar.monthrange(last_year_start.year,last_year_start.month)[1])
        legacy_history = [dict(r) for r in conn.execute('''SELECT warehouse_name,material_code,
            SUM(CASE WHEN date(stat_date) BETWEEN ? AND ? THEN allocated_qty ELSE 0 END) AS allocated_14d,
            SUM(CASE WHEN date(stat_date) BETWEEN ? AND ? THEN picked_qty ELSE 0 END) AS picked_14d,
            SUM(CASE WHEN date(stat_date) BETWEEN ? AND ? THEN picked_qty ELSE 0 END) AS picked_last_month,
            SUM(CASE WHEN date(stat_date) BETWEEN ? AND ? THEN picked_qty ELSE 0 END) AS picked_last_year_month
            FROM demand_history GROUP BY warehouse_name,material_code''',
            ((business_date-timedelta(days=13)).isoformat(),current,(business_date-timedelta(days=13)).isoformat(),current,
             previous_start.isoformat(),previous_end.isoformat(),last_year_start.isoformat(),last_year_end.isoformat()))]
        bookings = [dict(r) for r in conn.execute('SELECT * FROM booking_demand WHERE date(demand_date)=?', (current,))]
        inbound = _read(conn, 'inbound_record')
        purchases, purchase_plans = _business_data(conn, 'purchase', inbound)
        transfers, transfer_plans = _business_data(conn, 'transfer', inbound)

    owners = defaultdict(list)
    for item in controls:
        owners[item['material_code']].append(item)
        materials.setdefault(item['material_code'], dict(item))
    values = defaultdict(lambda: defaultdict(float))
    def bucket(w, m):
        return values[(wh(w), m)]
    for row in stock_rows:
        out = bucket(row['warehouse_name'], row['material_code'])
        for field in ('stock_qty', 'allocated_qty', 'frozen_qty', 'transfer_in_transit_qty', 'purchase_in_transit_qty'):
            out[field] += num(row.get(field))
        out['has_stock'] = 1
    for row in history:
        out = bucket(row['warehouse_name'], row['material_code'])
        out['allocated_7d'] += num(row.get('allocated_qty'))
        out['picked_7d'] += num(row.get('picked_qty'))
        if past3 <= day(row['stat_date']) <= yesterday:
            out['booking_3d'] += num(row.get('allocated_qty'))
            out['saleable_booking_3d'] += num(row.get('picked_qty'))
    for row in legacy_history:
        out = bucket(row['warehouse_name'], row['material_code'])
        for field in ('allocated_14d','picked_14d','picked_last_month','picked_last_year_month'):
            out[field] += num(row.get(field))
    for row in bookings:
        out = bucket(row['warehouse_name'], row['material_code'])
        out['booking_today'] += num(row.get('ordered_qty'))
        out['saleable_booking_today'] += num(row.get('saleable_qty'))
    for row in purchases:
        if active(row) and current < day(row.get('delivery_date')) <= future7:
            out = bucket(row['warehouse'], row['material_code'])
            out['purchase_data_future_7d'] += row['remaining_wms_qty']
            out['purchase_qty_7d'] += num(row.get('purchase_qty'))
            out['purchase_remaining_qty_7d'] += num(row.get('remaining_qty'))
    for row in purchase_plans:
        if row['counts_as_manual'] and current < day(row.get('expected_arrival_time')) <= future7:
            out = bucket(row['arrival_warehouse'], row['material_code'])
            out['purchase_data_future_7d'] += row['remaining_wms_qty']
            out['manual_purchase_qty'] += row['remaining_wms_qty']
    for row in inbound:
        if past7 <= day(row.get('inbound_date')) <= future7:
            bucket(row['warehouse_name'], row['material_code'])['purchase_received_qty_7d'] += num(row.get('received_qty'))
    for row in transfers:
        if not active(row):
            continue
        incoming = bucket(row['to_warehouse'], row['material_code'])
        outgoing = bucket(row['from_warehouse'], row['material_code'])
        incoming['transfer_planned_in'] += num(row['planned_qty'])
        incoming['transfer_in_transit_in'] += max(num(row['outbound_qty']) - num(row['inbound_qty']), 0)
        incoming['transfer_received_in'] += num(row['inbound_qty'])
        outgoing['transfer_planned_out'] += num(row['planned_qty'])
        outgoing['transfer_outbound_qty'] += num(row['outbound_qty'])
        # Actual quantity uses its actual event date; expected date is only a
        # visible fallback when OMS hasn't populated the actual timestamp.
        departure = day(row.get('actual_departure_time') or row.get('expected_departure_time'))
        arrival = day(row.get('actual_arrival_time') or row.get('expected_arrival_time'))
        if past3 <= departure <= yesterday:
            outgoing['transfer_summary_3d'] -= num(row['outbound_qty'])
            outgoing['transfer_out_3d'] += num(row['outbound_qty'])
        if past3 <= arrival <= yesterday:
            incoming['transfer_summary_3d'] += num(row['inbound_qty'])
            incoming['transfer_in_3d'] += num(row['inbound_qty'])
    for row in transfer_plans:
        if not row['counts_as_manual']:
            continue
        qty = num(row['planned_qty'])
        if past3 <= day(row.get('expected_departure_time')) <= yesterday:
            out = bucket(row['from_warehouse'], row['material_code'])
            out['transfer_summary_3d'] -= qty
            out['manual_transfer_out'] += qty
        if past3 <= day(row.get('expected_arrival_time')) <= yesterday:
            out = bucket(row['to_warehouse'], row['material_code'])
            out['transfer_summary_3d'] += qty
            out['manual_transfer_in'] += qty

    route_map = defaultdict(set)
    for route in routes:
        label = f"{route['from_warehouse']}→{route['to_warehouse']}"
        for w in (route['from_warehouse'], route['to_warehouse']):
            route_map[wh(w)].add(label)
    supplier_map = defaultdict(list)
    for item in suppliers:
        supplier_map[(wh(item.get('warehouse')), item.get('material_code'))].append(item)
    known_dates = sorted({day(r.get('snapshot_date')) for r in stock_rows if r.get('snapshot_date')})
    unknown_stock_date = any(not r.get('snapshot_date') for r in stock_rows)

    return dict(materials=materials, controls=controls, warehouses=warehouses, owners=owners,
                values=values, supplier_map=supplier_map, route_map=route_map,
                snapshot_date=snapshot_date, known_dates=known_dates, unknown_stock_date=unknown_stock_date,
                filters={'warehouses': warehouses, 'materials': [{'material_code': k, 'material_name': v.get('material_name')} for k, v in sorted(materials.items())],
                        'material_owners': sorted({r['material_owner'] for r in controls}),
                        'purchase_owners': sorted({r['purchase_owner'] for r in controls if r['purchase_owner']}),
                        'suppliers': sorted({r['supplier_name'] for r in suppliers}),
                        'material_categories': sorted({r['material_category'] for r in materials.values() if r.get('material_category')}),
                        'transfer_areas': sorted({label for labels in route_map.values() for label in labels})},
                windows={'transfer': [past3, yesterday], 'history': [past7, yesterday],
                        'purchase': [(business_date + timedelta(days=1)).isoformat(), future7]})


_PREPARED_CACHE = OrderedDict()
_CACHE_LOCK = RLock()


def _database_signature():
    path = Path(database.DB_PATH).resolve()
    stamps = []
    for candidate in (path, Path(str(path) + '-wal')):
        try:
            stat = candidate.stat()
            # Windows can coalesce file mtimes for quick successive commits.
            # SQLite's change counter and WAL frame checksums track the commit
            # even when size and modification time do not change.
            with candidate.open('rb') as handle:
                header = handle.read(100 if candidate == path else 32)
                tail = b''
                if candidate != path and len(header) == 32:
                    page_size = int.from_bytes(header[8:12], 'big')
                    if page_size and stat.st_size >= 32 + 24 + page_size:
                        handle.seek(32 + ((stat.st_size-32)//(24+page_size)-1)*(24+page_size))
                        tail = handle.read(24)
                stamps.append((stat.st_mtime_ns, stat.st_size, header, tail))
        except FileNotFoundError:
            stamps.append(None)
    return str(path), tuple(stamps)


def _prepared_dashboard(business_date):
    # Reuse only derived data. Every committed DB/WAL file change invalidates
    # the key, including imports, manual edits and changes from other processes.
    with _CACHE_LOCK:
        signature = _database_signature()
        key = (signature, business_date)
        if key not in _PREPARED_CACHE:
            prepared = _prepare_dashboard(business_date)
            if signature != _database_signature():
                return prepared, ''  # A concurrent writer committed: don't cache.
            _PREPARED_CACHE[key] = prepared
            while len(_PREPARED_CACHE) > 2:
                _PREPARED_CACHE.popitem(last=False)
        _PREPARED_CACHE.move_to_end(key)
        return _PREPARED_CACHE[key], hashlib.sha256(repr(signature).encode()).hexdigest()[:20]


def planning_dashboard(business_date: date, warehouse=None, material_code=None,
                       material_owner=None, purchase_owner=None, supplier=None,
                       material_category=None, transfer_area=None, page=None, page_size=100,
                       filters_version=None, search_fields=()) -> dict:
    prepared, version = _prepared_dashboard(business_date)
    materials, owners = prepared['materials'], prepared['owners']
    values, route_map = prepared['values'], prepared['route_map']
    supplier_map = prepared['supplier_map']
    search_fields = set(search_fields)
    def matches(field, value, term):
        if not term:
            return True
        if field in search_fields:
            return str(term).strip().casefold() in str(value or '').casefold()
        return value == term
    eligible = [code for code, material in sorted(materials.items())
                if (matches('material_code', code, material_code) or ('material_code' in search_fields and matches('material_code', material.get('material_name'), material_code)))
                and (not material_owner or any(matches('material_owner', c['material_owner'], material_owner) for c in owners.get(code, [])))
                and (not purchase_owner or any(matches('purchase_owner', c['purchase_owner'], purchase_owner) for c in owners.get(code, [])))
                and matches('material_category', material.get('material_category'), material_category)]
    selection = []
    total = 0
    offset = (page-1)*page_size if page is not None else 0
    limit = page_size if page is not None else None
    for w in prepared['warehouses']:
        normalized = wh(w)
        if warehouse and not (matches('warehouse', w, warehouse) if 'warehouse' in search_fields else normalized == wh(warehouse)):
            continue
        areas = sorted(route_map.get(normalized, set()))
        if transfer_area and not any(matches('transfer_area', area, transfer_area) for area in areas):
            continue
        codes = eligible
        if supplier:
            codes = [code for code in eligible if any(matches('supplier', r['supplier_name'], supplier) for r in
                     supplier_map.get((normalized, code), []) + supplier_map.get(('', code), []))]
        group_size = len(codes)
        first = max(0, offset-total)
        last = group_size if limit is None else min(group_size, offset+limit-total)
        if first < last:
            selection.extend((w, code, areas) for code in codes[first:last])
        total += group_size
    rows = []
    numeric_fields = ('stock_qty', 'allocated_qty', 'frozen_qty', 'transfer_summary_3d', 'purchase_data_future_7d',
                      'booking_3d', 'booking_today', 'saleable_booking_3d', 'saleable_booking_today',
                      'allocated_7d', 'picked_7d', 'purchase_qty_7d', 'purchase_remaining_qty_7d',
                      'purchase_received_qty_7d', 'transfer_in_transit_in', 'transfer_received_in',
                      'transfer_planned_in', 'transfer_planned_out', 'transfer_outbound_qty',
                      'manual_purchase_qty', 'manual_transfer_in', 'manual_transfer_out', 'transfer_in_3d', 'transfer_out_3d',
                      'allocated_14d','picked_14d','picked_last_month','picked_last_year_month')
    for w, code, areas in selection:
        material, assigned = materials[code], owners.get(code, [])
        supply = supplier_map.get((wh(w), code), []) + supplier_map.get(('', code), [])
        facts = values.get((wh(w), code), {})
        row = {field: num(facts.get(field)) for field in numeric_fields}
        row.update(warehouse=w, material_code=code, material_name=material.get('material_name') or '',
                   material_owner='；'.join(sorted({c['material_owner'] for c in assigned})),
                   purchase_owner='；'.join(sorted({c['purchase_owner'] for c in assigned if c['purchase_owner']})),
                   material_category=material.get('material_category') or '',
                   supplier='；'.join(sorted({c['supplier_name'] for c in supply})),
                   arrival_mode='；'.join(sorted({c['arrival_mode'] for c in supply if c.get('arrival_mode')})),
                   transfer_area='；'.join(areas), has_stock=bool(facts.get('has_stock')))
        row['inventory'] = row['stock_qty']
        row['stock_transfer_in_transit_qty'] = num(facts.get('transfer_in_transit_qty'))
        row['stock_purchase_in_transit_qty'] = num(facts.get('purchase_in_transit_qty'))
        row['available_stock'] = row['stock_qty'] - row['allocated_qty'] - row['frozen_qty']
        row['inventory_plus_transfer'] = row['inventory'] + row['transfer_summary_3d']
        row['inventory_plus_transfer_purchase'] = row['inventory_plus_transfer'] + row['purchase_data_future_7d']
        row['booking_avg_7d'] = row['allocated_7d'] / 7
        row['saleable_booking_avg_7d'] = row['picked_7d'] / 7
        rows.append(row)

    return {'business_date': business_date.isoformat(), 'snapshot_date': prepared['snapshot_date'],
            'stock_file_dates': prepared['known_dates'], 'stock_date_unknown': prepared['unknown_stock_date'],
            'rows': rows, 'total': total, 'page': page or 1, 'filters_version': version,
            'filters': prepared['filters'] if not version or filters_version != version else None,
            'windows': prepared['windows']}
