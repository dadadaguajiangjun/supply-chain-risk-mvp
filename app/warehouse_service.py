from __future__ import annotations

import re
from datetime import date
from typing import Iterable

import pandas as pd

from .excel_service import ParsedWorkbook, normalize_warehouse_name, text_series


WAREHOUSE_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "purchase_order": (("warehouse", "warehouse_code"),),
    "stock": (("warehouse", "warehouse_code"),),
    "inbound_daily": (("warehouse", "warehouse_code"),),
    "booking_order": (("warehouse", "warehouse_code"),),
    "outbound": (("warehouse", "warehouse_code"),),
    "transfer": (
        ("from_warehouse", "from_warehouse_code"),
        ("to_warehouse", "to_warehouse_code"),
    ),
}


def _key(value: object) -> str:
    return re.sub(r"[\s\u3000]+", "", str(value or "").strip()).lower()


def _mapping_is_active(mapping: dict, business_date: date) -> bool:
    if not bool(mapping.get("enabled", 1)):
        return False
    start = mapping.get("effective_start_date")
    end = mapping.get("effective_end_date")
    iso = business_date.isoformat()
    return (not start or start <= iso) and (not end or end >= iso)


def _mapping_indexes(
    mappings: Iterable[dict], source_system: str, business_date: date
) -> tuple[dict[str, dict], dict[str, dict]]:
    code_map: dict[str, dict] = {}
    name_map: dict[str, dict] = {}
    for mapping in mappings:
        if mapping.get("source_system") not in {source_system, "*"}:
            continue
        if not _mapping_is_active(mapping, business_date):
            continue
        code = _key(mapping.get("raw_warehouse_code"))
        name = _key(mapping.get("raw_warehouse_name"))
        if code:
            code_map[code] = mapping
        if name:
            name_map[name] = mapping
        for alias in re.split(r"[,，;；|\n]+", str(mapping.get("special_alias") or "")):
            alias_key = _key(alias)
            if alias_key:
                name_map[alias_key] = mapping
    return code_map, name_map


def apply_warehouse_mappings(
    workbook: ParsedWorkbook,
    mappings: Iterable[dict],
    business_date: date,
) -> list[dict]:
    """Apply confirmed mappings and return aggregated unmatched raw warehouses.

    Automatic suffix normalization is recorded only as a suggestion. It is not
    inserted into warehouse_mapping by this function.
    """
    frame = workbook.dataframe
    code_map, name_map = _mapping_indexes(mappings, workbook.file_type, business_date)
    unmatched: dict[tuple[str, str], dict] = {}

    for name_column, code_column in WAREHOUSE_FIELDS.get(workbook.file_type, ()):
        if name_column not in frame.columns and code_column not in frame.columns:
            continue
        names = text_series(frame, name_column)
        codes = text_series(frame, code_column)
        resolved: list[str] = []
        for raw_name, raw_code in zip(names, codes):
            mapping = code_map.get(_key(raw_code)) if raw_code else None
            if mapping is None and raw_name:
                mapping = name_map.get(_key(raw_name))
            if mapping is not None:
                resolved.append(str(mapping["standard_warehouse_name"]).strip())
                continue
            resolved.append(raw_name)
            if raw_name or raw_code:
                key = (raw_code, raw_name)
                item = unmatched.setdefault(key, {
                    "source_system": workbook.file_type,
                    "source_file": workbook.filename,
                    "raw_warehouse_code": raw_code,
                    "raw_warehouse_name": raw_name,
                    "record_count": 0,
                    "suggested_standard_name": normalize_warehouse_name(raw_name),
                })
                item["record_count"] += 1
        frame[name_column] = pd.Series(resolved, index=frame.index, dtype="object")
    return list(unmatched.values())
