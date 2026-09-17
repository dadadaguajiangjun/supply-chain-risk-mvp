"""Read-only checks of an isolated replay, including rows rejected by cleaning."""
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.database as database
from app.data_quality import clean_and_assess
from app.excel_service import parse_workbook
from app.planning_service import planning_dashboard


if __name__ == '__main__':
    report_path = Path(sys.argv[1])
    database.DB_PATH = Path(sys.argv[2])
    database.initialize_database()
    report = json.loads(report_path.read_text(encoding='utf-8'))
    result = {'cleaning_details': [], 'persisted': {}}
    for item in report['files']:
        if item['deleted_rows'] == 0:
            continue
        parsed = parse_workbook(Path(report['folder']) / item['filename'])
        raw = parsed.dataframe.copy()
        quality = clean_and_assess(parsed, date.fromisoformat(report['business_date']))
        valid_source_rows = set(parsed.dataframe['_source_row'])
        removed = raw.loc[[int(i)+parsed.header_row+2 not in valid_source_rows for i in raw.index]]
        columns = [c for c in ['order_no','warehouse','from_warehouse','to_warehouse','material_code','purchase_qty','remaining_qty','demand_qty','transfer_qty','delivery_date','arrival_date'] if c in removed.columns]
        result['cleaning_details'].append({'type':item['file_type'], 'quality':quality.to_dict(),
            'removed_rows':json.loads(removed[columns].to_json(orient='records',force_ascii=False))})
    with database.db_connection() as conn:
        for table, qty in [('purchase_record','purchase_qty'),('inbound_record','received_qty'),('stock_snapshot','stock_qty'),('transfer_record','planned_qty'),('demand_history','allocated_qty'),('booking_demand','ordered_qty')]:
            result['persisted'][table] = dict(conn.execute(f'SELECT COUNT(*) AS rows,SUM({qty}) AS quantity FROM {table}').fetchone())
    page = planning_dashboard(date.fromisoformat(report['business_date']), page=1,page_size=100)
    result['dashboard'] = {'total':page['total'],'page_rows':len(page['rows']), 'stock_date_unknown':page['stock_date_unknown']}
    output = report_path.with_name(report_path.stem+'_verification.json')
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='cleaning_details'},ensure_ascii=False))
    for item in result['cleaning_details']:
        print(json.dumps({'type':item['type'],'quality':item['quality'],'removed_sample':item['removed_rows'][:3]},ensure_ascii=False,default=str))
