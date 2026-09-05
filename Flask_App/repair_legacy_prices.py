"""Repair legacy fractional ticket prices before the MySQL cutover.

Historic SQLite databases can contain decimal values in columns that the current
application intentionally treats as whole-dollar integers. This command pauses
collection, creates SQLite backups, rounds only fractional ticket prices using
the collector's current ROUND_HALF_UP rule, invalidates and rebuilds affected
materialized summaries, and leaves collection paused for the verified cutover.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from Flask_App.database_config import (  # noqa: E402
    begin_migration_pause,
    configured_backend,
    migration_pause_active,
)
from Flask_App.mysql_cutover import SOURCE_PATHS  # noqa: E402


SPORT_SPECS = {
    "mlb": ("tickets", "iterations"),
    "nfl": ("nfl_tickets", "nfl_iterations"),
    "nhl": ("nhl_tickets", "nhl_iterations"),
}
AUDIT_DIR = PROJECT_DIR / "mysql_migration_audits"


def _quote_identifier(value: str) -> str:
    if not value.replace("_", "").isalnum():
        raise ValueError(f"Unsafe SQLite identifier: {value!r}")
    return '"' + value.replace('"', '""') + '"'


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _fractional_rows(
    connection: sqlite3.Connection,
    ticket_table: str,
    iteration_table: str,
) -> list[dict[str, Any]]:
    tickets = _quote_identifier(ticket_table)
    iterations = _quote_identifier(iteration_table)
    rows = connection.execute(
        f"""
        SELECT
            t.id AS ticket_id,
            i.event_id AS event_id,
            t.section AS section,
            t.price AS old_price
        FROM {tickets} AS t
        JOIN {iterations} AS i ON i.id = t.iteration_id
        WHERE t.price IS NOT NULL
          AND ABS(
                CAST(t.price AS REAL)
                - CAST(CAST(t.price AS REAL) AS INTEGER)
              ) > 0.000000001
        ORDER BY t.id
        """
    ).fetchall()

    result: list[dict[str, Any]] = []
    for ticket_id, event_id, section, old_price in rows:
        try:
            decimal_price = Decimal(str(old_price))
        except (InvalidOperation, ValueError) as exc:
            raise RuntimeError(
                f"{ticket_table}.price contains a non-numeric value: {old_price!r}"
            ) from exc
        repaired = int(
            decimal_price.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        if repaired <= 0:
            raise RuntimeError(
                f"Refusing to repair non-positive {ticket_table}.price "
                f"{old_price!r} for ticket {ticket_id}."
            )
        result.append(
            {
                "ticket_id": int(ticket_id),
                "event_id": int(event_id),
                "section": str(section or ""),
                "old_price": str(old_price),
                "new_price": repaired,
            }
        )
    return result


def _backup_database(
    source: sqlite3.Connection,
    source_path: Path,
    stamp: str,
) -> Path:
    backup_path = source_path.with_name(
        f"{source_path.name}.pre-mysql-price-repair-{stamp}.bak"
    )
    if backup_path.exists():
        raise FileExistsError(f"Backup already exists: {backup_path}")
    with closing(sqlite3.connect(backup_path)) as backup:
        source.backup(backup)
    return backup_path


def _delete_for_event_ids(
    connection: sqlite3.Connection,
    table_name: str,
    event_ids: list[int],
) -> None:
    if not event_ids or not _table_exists(connection, table_name):
        return
    quoted = _quote_identifier(table_name)
    for start in range(0, len(event_ids), 500):
        batch = event_ids[start : start + 500]
        placeholders = ",".join("?" for _ in batch)
        connection.execute(
            f"DELETE FROM {quoted} WHERE event_id IN ({placeholders})",
            batch,
        )


def _repair_sport(sport: str, stamp: str) -> dict[str, Any]:
    ticket_table, iteration_table = SPORT_SPECS[sport]
    source_path = Path(SOURCE_PATHS[sport]()).expanduser().resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"{sport.upper()} SQLite database is missing: {source_path}")

    with closing(sqlite3.connect(source_path, timeout=60)) as connection:
        connection.execute("PRAGMA busy_timeout = 60000")
        if not _table_exists(connection, ticket_table):
            raise RuntimeError(
                f"{sport.upper()} database is missing table {ticket_table!r}."
            )
        if not _table_exists(connection, iteration_table):
            raise RuntimeError(
                f"{sport.upper()} database is missing table {iteration_table!r}."
            )

        rows = _fractional_rows(connection, ticket_table, iteration_table)
        if not rows:
            return {
                "sport": sport,
                "database": str(source_path),
                "backup": None,
                "rows_repaired": 0,
                "event_ids": [],
                "sample": [],
            }

        backup_path = _backup_database(connection, source_path, stamp)
        event_ids = sorted({row["event_id"] for row in rows})
        quoted_tickets = _quote_identifier(ticket_table)

        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                f"UPDATE {quoted_tickets} SET price = ? WHERE id = ?",
                [(row["new_price"], row["ticket_id"]) for row in rows],
            )
            _delete_for_event_ids(
                connection,
                "section_bucket_summary",
                event_ids,
            )
            _delete_for_event_ids(
                connection,
                "section_summary_state",
                event_ids,
            )
            remaining = _fractional_rows(
                connection,
                ticket_table,
                iteration_table,
            )
            if remaining:
                raise RuntimeError(
                    f"{sport.upper()} still has {len(remaining)} fractional "
                    "ticket-price rows after repair."
                )
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if not integrity or str(integrity[0]).casefold() != "ok":
                raise RuntimeError(
                    f"{sport.upper()} SQLite integrity check failed: {integrity}"
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    return {
        "sport": sport,
        "database": str(source_path),
        "backup": str(backup_path),
        "rows_repaired": len(rows),
        "event_ids": event_ids,
        "sample": rows[:25],
    }


def _rebuild_summaries(sports: list[str]) -> None:
    if not sports:
        return

    from Flask_App.analytics_maintenance import backfill_sport

    for sport in sports:
        while True:
            result = backfill_sport(sport, limit=20)
            print(
                f"{sport.upper()} summaries: processed={result.processed}, "
                f"remaining={result.remaining}",
                flush=True,
            )
            if result.complete:
                break


def main() -> int:
    if configured_backend() != "sqlite":
        raise RuntimeError(
            "Legacy price repair is allowed only while production selects SQLite."
        )

    if not migration_pause_active():
        begin_migration_pause()
    print(
        "Collector writes are paused. Waiting for in-flight writes to finish...",
        flush=True,
    )
    time.sleep(10)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    reports: list[dict[str, Any]] = []
    try:
        for sport in SPORT_SPECS:
            report = _repair_sport(sport, stamp)
            reports.append(report)
            print(
                f"{sport.upper()}: repaired {report['rows_repaired']} "
                "fractional ticket-price row(s).",
                flush=True,
            )

        affected = [
            report["sport"]
            for report in reports
            if report["rows_repaired"]
        ]
        _rebuild_summaries(affected)

        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        audit_path = AUDIT_DIR / f"legacy_price_repair_{stamp}.json"
        audit_path.write_text(
            json.dumps(
                {
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "reports": reports,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        audit_path.chmod(0o600)
    except Exception:
        print(
            "Repair failed. Collector writes remain paused. Do not run the "
            "migration until the error is reviewed.",
            file=sys.stderr,
            flush=True,
        )
        raise

    print(f"Audit written: {audit_path}")
    for report in reports:
        if report["backup"]:
            print(f"{report['sport'].upper()} backup: {report['backup']}")
    print("Legacy price repair completed. Collector writes remain paused.")
    print("Next: python3 -m Flask_App.mysql_cutover migrate --replace")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
