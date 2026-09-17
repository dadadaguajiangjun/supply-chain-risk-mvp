CREATE TABLE IF NOT EXISTS purchase_record (
    id BIGSERIAL PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    last_run_id TEXT NOT NULL,
    order_no TEXT,
    purchase_date TIMESTAMP,
    supplier_name TEXT,
    order_status TEXT,
    purchase_org TEXT,
    close_status TEXT,
    material_code TEXT,
    material_name TEXT,
    purchase_qty NUMERIC NOT NULL DEFAULT 0,
    delivery_date TIMESTAMP,
    remaining_qty NUMERIC NOT NULL DEFAULT 0,
    warehouse TEXT,
    remark TEXT,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_purchase_record_reconcile
    ON purchase_record(order_no, material_code, delivery_date);

CREATE TABLE IF NOT EXISTS inbound_record (
    id BIGSERIAL PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    last_run_id TEXT NOT NULL,
    warehouse_code TEXT,
    warehouse_name TEXT,
    production_date TIMESTAMP,
    expected_arrival_time TIMESTAMP,
    inbound_date TIMESTAMP,
    product_status TEXT,
    asn_no TEXT,
    asn_type TEXT,
    notice_type TEXT,
    external_order_no TEXT,
    reference_no3 TEXT,
    expected_qty NUMERIC NOT NULL DEFAULT 0,
    received_qty NUMERIC NOT NULL DEFAULT 0,
    tracking_no TEXT,
    transaction_no TEXT,
    supplier_code TEXT,
    supplier_name TEXT,
    upstream_warehouse TEXT,
    location TEXT,
    zone TEXT,
    expiry_date TIMESTAMP,
    packaging_code TEXT,
    shipment_remark TEXT,
    material_code TEXT,
    material_name TEXT,
    source_system TEXT,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_inbound_record_reconcile
    ON inbound_record(reference_no3, material_code, inbound_date);
