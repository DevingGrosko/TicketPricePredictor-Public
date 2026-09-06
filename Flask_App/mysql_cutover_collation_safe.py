"""Run the MySQL cutover with database-collation-independent verification.

SQLite compares text primary keys bytewise by default, while MySQL commonly
uses a case-insensitive database collation.  SQL ``MIN``, ``MAX``, and
``ORDER BY`` can therefore select different rows even when the copied values
are identical.  This entry point keeps the existing guarded cutover workflow
but orders text keys by their encoded bytes on both databases.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import LargeBinary, String, cast, select
from sqlalchemy.engine import Connection
from sqlalchemy.sql.schema import Table

from Flask_App import mysql_cutover as _base


def _stable_order_expression(column: Any) -> Any:
    """Return an ordering expression with the same text semantics on both DBs."""

    if isinstance(column.type, String):
        # SQLAlchemy compiles this as CAST(... AS BLOB) for SQLite and
        # CAST(... AS BINARY) for MySQL, avoiding each database's collation.
        return cast(column, LargeBinary)
    return column


def _pk_signature(connection: Connection, table: Table) -> dict[str, Any]:
    """Calculate primary-key endpoints using stable bytewise text ordering."""

    columns = list(table.primary_key.columns)
    if not columns:
        return {}

    signature: dict[str, Any] = {}
    for column in columns:
        order = _stable_order_expression(column)
        minimum = connection.execute(
            select(column).order_by(order.asc()).limit(1)
        ).scalar_one_or_none()
        maximum = connection.execute(
            select(column).order_by(order.desc()).limit(1)
        ).scalar_one_or_none()
        signature[column.name] = {
            "min": _base._normalize_column_value(column, minimum),
            "max": _base._normalize_column_value(column, maximum),
        }
    return signature


def _sample_rows(connection: Connection, table: Table) -> list[dict[str, Any]]:
    """Select deterministic row samples without relying on DB text collation."""

    count = _base._count(connection, table)
    if not count:
        return []

    keys = list(table.primary_key.columns)
    order_columns = keys or list(table.columns)[:1]
    order = [_stable_order_expression(column) for column in order_columns]
    rows: list[dict[str, Any]] = []

    for offset in sorted({0, count // 2, count - 1}):
        row = connection.execute(
            select(table).order_by(*order).offset(offset).limit(1)
        ).mappings().one()
        normalized = _base._normalized_row(table, dict(row))
        rows.append(
            {
                "offset": offset,
                "digest": hashlib.sha256(
                    json.dumps(
                        normalized,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        default=_base._json_default,
                    ).encode("utf-8")
                ).hexdigest(),
                "row": normalized,
            }
        )
    return rows


def install() -> None:
    """Install the stable verification functions into the guarded cutover."""

    _base._pk_signature = _pk_signature
    _base._sample_rows = _sample_rows


def main() -> int:
    install()
    return _base.main()


if __name__ == "__main__":
    raise SystemExit(main())
