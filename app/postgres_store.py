from __future__ import annotations

import os
from pathlib import Path
from typing import Any


POSTGRES_DSN = os.getenv("POSTGRES_DSN", "").strip()
SCHEMA_PATH = Path(__file__).with_name("postgres_schema.sql")


def is_enabled() -> bool:
    return bool(POSTGRES_DSN)


def _connect():
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - only used when PostgreSQL is configured
        raise RuntimeError("已配置 POSTGRES_DSN，但未安装 psycopg。请重新运行 install.bat。") from exc
    return psycopg.connect(POSTGRES_DSN)


def initialize_postgres_storage() -> None:
    """Create the cumulative Kingdee/FLUX tables when PostgreSQL is configured."""
    if not is_enabled():
        return
    with _connect() as conn:
        conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))


def mirror_rows(table: str, columns: list[str], rows: list[tuple[Any, ...]], conflict: str = "source_key") -> None:
    if not is_enabled() or not rows or table not in {"purchase_record", "inbound_record"}:
        return
    update_columns = [column for column in columns if column != conflict]
    placeholders = ",".join(["%s"] * len(columns))
    sql = (
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict}) DO UPDATE SET "
        + ",".join(f"{column}=EXCLUDED.{column}" for column in update_columns)
        + ",updated_at=CURRENT_TIMESTAMP"
    )
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.executemany(sql, rows)


def update_purchase_remark(source_key: str, remark: str | None) -> None:
    if not is_enabled():
        return
    with _connect() as conn:
        conn.execute(
            "UPDATE purchase_record SET remark=%s,updated_at=CURRENT_TIMESTAMP WHERE source_key=%s",
            (remark, source_key),
        )
