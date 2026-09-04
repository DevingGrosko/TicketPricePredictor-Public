#!/usr/bin/env python3
"""Migrate TicketSignal's active sport databases from SQLite to MySQL.

The tool deliberately separates data copy from activation:

1. ``migrate`` pauses collector writes, copies and verifies all three databases,
   changes the server-owned backend setting to MySQL, and leaves writes paused.
2. Reload the PythonAnywhere web app.
3. ``activate`` confirms the live worker reports MySQL before unpausing writes.

The source SQLite files are never modified and remain the rollback copy.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from sqlalchemy import JSON, MetaData, String, Table, create_engine, func, inspect, select, text
from sqlalchemy.engine import Connection, Engine


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from Flask_App.database_config import (  # noqa: E402
    ENV_PATH,
    MIGRATION_MANIFEST_PATH,
    begin_migration_pause,
    configured_backend,
    create_mysql_engine,
    end_migration_pause,
    migration_pause_active,
    update_backend_setting,
)
from Flask_App.materialized_analytics import (  # noqa: E402
    DIRTY_VENUE,
    SECTION_BUCKET_SUMMARY,
    SECTION_SUMMARY_STATE,
)
from Flask_App.nfl_blueprint import (  # noqa: E402
    NFLBase,
    nfl_database_path,
)
from Flask_App.nhl_blueprint import (  # noqa: E402
    NHLBase,
    nhl_database_path,
)
from models import Base, database_path  # noqa: E402


load_dotenv(ENV_PATH, override=True)

SPORTS = ("mlb", "nfl", "nhl")
BASE_METADATA = {
    "mlb": Base.metadata,
    "nfl": NFLBase.metadata,
    "nhl": NHLBase.metadata,
}
SOURCE_PATHS = {
    "mlb": database_path,
    "nfl": nfl_database_path,
    "nhl": nhl_database_path,
}
RAW_TABLE_ORDER = {
    "mlb": ("event", "iterations", "tickets"),
    "nfl": ("nfl_event", "nfl_iterations", "nfl_tickets"),
    "nhl": ("nhl_event", "nhl_iterations", "nhl_tickets"),
}
SUMMARY_TABLES = (
    SECTION_BUCKET_SUMMARY,
    SECTION_SUMMARY_STATE,
    DIRTY_VENUE,
)
DEFAULT_LIVE_BASE = "https://bunnyjeff.pythonanywhere.com"


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def _source_engine(path: Path) -> Engine:
    if not path.exists():
        raise FileNotFoundError(f"SQLite source database is missing: {path}")
    return create_engine(
        f"sqlite:///{path.expanduser().resolve()}",
        connect_args={"timeout": 60},
    )


def _column_length(table_name: str, column_name: str) -> int:
    name = column_name.casefold()
    if name in {"url", "source_url"}:
        return 700
    if name in {"title"}:
        return 700
    if name in {"section", "section_name", "section_key"}:
        return 600 if name == "section_key" else 300
    if name in {"venue", "canonical_venue", "provider_venue", "place"}:
        return 300
    if name in {"source_id", "schedule_id"}:
        return 191
    if name in {"away_team", "home_team"}:
        return 200
    if name in {"city", "country", "map_source"}:
        return 191
    return 255


def _mysql_metadata(sport: str) -> MetaData:
    """Clone current ORM metadata and make unbounded VARCHARs MySQL-safe."""

    target = MetaData()
    for table in BASE_METADATA[sport].sorted_tables:
        table.to_metadata(target)
    for table in SUMMARY_TABLES:
        if table.name not in target.tables:
            table.to_metadata(target)

    for table in target.tables.values():
        for column in table.columns:
            if isinstance(column.type, String) and column.type.length is None:
                column.type = String(_column_length(table.name, column.name))
    return target


def _source_tables(sport: str, engine: Engine) -> dict[str, Table]:
    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    tables = {
        table.name: table
        for table in BASE_METADATA[sport].sorted_tables
        if table.name in present
    }
    for table in SUMMARY_TABLES:
        if table.name in present:
            tables[table.name] = table
    return tables


def _ordered_table_names(sport: str, source_tables: dict[str, Table]) -> list[str]:
    preferred = [*RAW_TABLE_ORDER[sport], *(table.name for table in SUMMARY_TABLES)]
    ordered = [name for name in preferred if name in source_tables]
    ordered.extend(sorted(set(source_tables) - set(ordered)))
    return ordered


def _destination_has_rows(engine: Engine, table_names: Iterable[str]) -> bool:
    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    with engine.connect() as connection:
        for name in table_names:
            if name not in present:
                continue
            quoted = connection.dialect.identifier_preparer.quote(name)
            if int(connection.exec_driver_sql(f"SELECT COUNT(*) FROM {quoted}").scalar() or 0):
                return True
    return False


def _drop_and_create(engine: Engine, metadata: MetaData) -> None:
    with engine.connect() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=0")
        try:
            metadata.drop_all(connection, checkfirst=True)
            metadata.create_all(connection, checkfirst=True)
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=1")
        connection.commit()


def _create_empty_schema(engine: Engine, metadata: MetaData) -> None:
    metadata.create_all(engine, checkfirst=True)


def _normalize_for_digest(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {str(key): _normalize_for_digest(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normalize_for_digest(item) for item in value]
    if isinstance(value, float):
        return round(value, 10)
    return value


def _row_digest(mapping: dict[str, Any]) -> str:
    payload = json.dumps(
        _normalize_for_digest(mapping),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _copy_table(
    source: Connection,
    destination: Connection,
    source_table: Table,
    destination_table: Table,
    *,
    batch_size: int,
) -> int:
    primary_keys = list(source_table.primary_key.columns)
    statement = select(source_table)
    if primary_keys:
        statement = statement.order_by(*primary_keys)

    result = source.execution_options(stream_results=True).execute(statement)
    copied = 0
    while True:
        rows = result.fetchmany(batch_size)
        if not rows:
            break
        payload = [dict(row._mapping) for row in rows]
        destination.execute(destination_table.insert(), payload)
        copied += len(payload)
    return copied


def _count(connection: Connection, table: Table) -> int:
    return int(connection.execute(select(func.count()).select_from(table)).scalar_one())


def _pk_signature(connection: Connection, table: Table) -> dict[str, Any]:
    columns = list(table.primary_key.columns)
    if not columns:
        return {}
    signature: dict[str, Any] = {}
    for column in columns:
        values = connection.execute(
            select(
                func.min(column).label("minimum"),
                func.max(column).label("maximum"),
            )
        ).one()
        signature[column.name] = {
            "min": _normalize_for_digest(values.minimum),
            "max": _normalize_for_digest(values.maximum),
        }
    return signature


def _sample_digests(connection: Connection, table: Table) -> list[str]:
    count = _count(connection, table)
    if not count:
        return []
    keys = list(table.primary_key.columns)
    order = keys or list(table.columns)[:1]
    offsets = sorted({0, count // 2, count - 1})
    digests = []
    for offset in offsets:
        row = connection.execute(
            select(table).order_by(*order).offset(offset).limit(1)
        ).mappings().one()
        digests.append(_row_digest(dict(row)))
    return digests


def _foreign_key_orphans(connection: Connection, sport: str, tables: dict[str, Table]) -> dict[str, int]:
    parent, iterations, tickets = RAW_TABLE_ORDER[sport]
    if not {parent, iterations, tickets}.issubset(tables):
        return {}
    event_table = tables[parent]
    iteration_table = tables[iterations]
    ticket_table = tables[tickets]

    iteration_orphans = int(
        connection.execute(
            select(func.count())
            .select_from(iteration_table.outerjoin(event_table, iteration_table.c.event_id == event_table.c.id))
            .where(event_table.c.id.is_(None))
        ).scalar_one()
    )
    ticket_orphans = int(
        connection.execute(
            select(func.count())
            .select_from(ticket_table.outerjoin(iteration_table, ticket_table.c.iteration_id == iteration_table.c.id))
            .where(iteration_table.c.id.is_(None))
        ).scalar_one()
    )
    return {"iteration_orphans": iteration_orphans, "ticket_orphans": ticket_orphans}


def migrate_sport(sport: str, *, replace: bool, batch_size: int) -> dict[str, Any]:
    source_path = Path(SOURCE_PATHS[sport]()).expanduser().resolve()
    source_engine = _source_engine(source_path)
    destination_engine = create_mysql_engine(sport)
    destination_metadata = _mysql_metadata(sport)
    source_tables = _source_tables(sport, source_engine)
    table_names = _ordered_table_names(sport, source_tables)

    if not table_names:
        raise RuntimeError(f"No application tables were found in {source_path}")
    if _destination_has_rows(destination_engine, table_names) and not replace:
        raise RuntimeError(
            f"The {sport.upper()} MySQL database is not empty. Rerun with --replace "
            "only after confirming it contains no independent data."
        )

    if replace:
        _drop_and_create(destination_engine, destination_metadata)
    else:
        _create_empty_schema(destination_engine, destination_metadata)

    destination_tables = destination_metadata.tables
    copied: dict[str, int] = {}
    started = time.perf_counter()

    with source_engine.connect() as source, destination_engine.connect() as destination:
        source.exec_driver_sql("BEGIN")
        destination.exec_driver_sql("SET FOREIGN_KEY_CHECKS=0")
        try:
            for name in table_names:
                destination_table = destination_tables[name]
                copied[name] = _copy_table(
                    source,
                    destination,
                    source_tables[name],
                    destination_table,
                    batch_size=batch_size,
                )
                destination.commit()
        finally:
            destination.exec_driver_sql("SET FOREIGN_KEY_CHECKS=1")
            destination.commit()
            source.rollback()

    verification: dict[str, Any] = {}
    with source_engine.connect() as source, destination_engine.connect() as destination:
        destination_reflected = MetaData()
        destination_reflected.reflect(bind=destination, only=table_names)
        for name in table_names:
            source_table = source_tables[name]
            destination_table = destination_reflected.tables[name]
            source_count = _count(source, source_table)
            destination_count = _count(destination, destination_table)
            source_signature = _pk_signature(source, source_table)
            destination_signature = _pk_signature(destination, destination_table)
            source_samples = _sample_digests(source, source_table)
            destination_samples = _sample_digests(destination, destination_table)
            if source_count != destination_count:
                raise RuntimeError(
                    f"{sport}.{name}: row count mismatch "
                    f"({source_count} SQLite vs {destination_count} MySQL)"
                )
            if source_signature != destination_signature:
                raise RuntimeError(f"{sport}.{name}: primary-key range mismatch")
            if source_samples != destination_samples:
                raise RuntimeError(f"{sport}.{name}: deterministic sample mismatch")
            verification[name] = {
                "rows": source_count,
                "primary_keys": source_signature,
                "sample_digests": source_samples,
            }

        orphans = _foreign_key_orphans(destination, sport, destination_reflected.tables)
        if any(orphans.values()):
            raise RuntimeError(f"{sport}: foreign-key orphan verification failed: {orphans}")

    report = {
        "sport": sport,
        "source": str(source_path),
        "source_size_bytes": source_path.stat().st_size,
        "source_modified_at": datetime.fromtimestamp(
            source_path.stat().st_mtime, timezone.utc
        ).isoformat(),
        "tables": verification,
        "foreign_keys": orphans,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    source_engine.dispose()
    return report


def _write_manifest(reports: list[dict[str, Any]]) -> None:
    manifest = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "backend_after_reload": "mysql",
        "sports": {report["sport"]: report for report in reports},
    }
    temporary = MIGRATION_MANIFEST_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(MIGRATION_MANIFEST_PATH)


def _read_manifest() -> dict[str, Any]:
    if not MIGRATION_MANIFEST_PATH.exists():
        raise RuntimeError("No successful MySQL migration manifest exists.")
    return json.loads(MIGRATION_MANIFEST_PATH.read_text(encoding="utf-8"))


def verify_mysql_against_manifest() -> dict[str, Any]:
    manifest = _read_manifest()
    results: dict[str, Any] = {}
    for sport in SPORTS:
        expected = manifest["sports"][sport]["tables"]
        engine = create_mysql_engine(sport)
        metadata = MetaData()
        metadata.reflect(bind=engine, only=list(expected))
        with engine.connect() as connection:
            actual = {
                name: _count(connection, metadata.tables[name])
                for name in expected
            }
        mismatches = {
            name: {"expected": details["rows"], "actual": actual.get(name)}
            for name, details in expected.items()
            if actual.get(name) != details["rows"]
        }
        if mismatches:
            raise RuntimeError(f"{sport}: MySQL counts changed before activation: {mismatches}")
        results[sport] = actual
    return results


def _live_backend_status(base_url: str) -> dict[str, Any]:
    token = os.getenv("COLLECTOR_INGEST_TOKEN", "").strip()
    if not token:
        raise RuntimeError("COLLECTOR_INGEST_TOKEN is missing from .env")
    request = Request(
        base_url.rstrip("/") + "/api/database/status",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Live database status returned HTTP {exc.code}: {body}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Could not reach live database status: {exc}") from exc
    return payload


def command_migrate(args: argparse.Namespace) -> int:
    if configured_backend() != "sqlite":
        raise RuntimeError("Production must still be configured for SQLite before migration.")

    begin_migration_pause()
    print("Collector writes are paused. Waiting for in-flight writes to finish...", flush=True)
    time.sleep(max(0, args.quiesce_seconds))

    reports: list[dict[str, Any]] = []
    try:
        for sport in SPORTS:
            print(f"Migrating {sport.upper()}...", flush=True)
            report = migrate_sport(
                sport,
                replace=args.replace,
                batch_size=args.batch_size,
            )
            reports.append(report)
            total = sum(table["rows"] for table in report["tables"].values())
            print(
                f"{sport.upper()} verified: {total:,} rows across "
                f"{len(report['tables'])} tables in {report['elapsed_seconds']:.1f}s.",
                flush=True,
            )
        _write_manifest(reports)
        update_backend_setting("mysql")
    except Exception:
        print(
            "Migration failed. Collector writes remain paused so the source cannot "
            "change unnoticed. Resolve the error, then rerun with --replace, or run "
            "`python tools/mysql_cutover.py abort` to resume SQLite collection.",
            file=sys.stderr,
            flush=True,
        )
        raise

    print("\nSQLite-to-MySQL copy and verification completed.")
    print("The .env backend is now set to mysql, but the running web workers still need a reload.")
    print("Do NOT unpause collection yet.")
    print("Next: reload the web app in PythonAnywhere, then run:")
    print("  python tools/mysql_cutover.py activate")
    return 0


def command_activate(args: argparse.Namespace) -> int:
    if not migration_pause_active():
        raise RuntimeError("The migration pause marker is missing; refusing blind activation.")
    if configured_backend() != "mysql":
        raise RuntimeError("The server .env is not configured for MySQL.")

    counts = verify_mysql_against_manifest()
    live = _live_backend_status(args.base_url)
    if live.get("backend") != "mysql":
        raise RuntimeError(
            "The live web worker still reports a non-MySQL backend. Reload the web app "
            "from PythonAnywhere's Web tab, then rerun activate."
        )
    if not live.get("migration_paused"):
        raise RuntimeError("The live app does not see the migration pause marker.")

    end_migration_pause()
    print("MySQL cutover activated and collector writes resumed.")
    for sport, table_counts in counts.items():
        print(f"  {sport.upper()}: {sum(table_counts.values()):,} verified rows")
    print("The SQLite files remain unchanged as rollback copies.")
    return 0


def command_status(args: argparse.Namespace) -> int:
    status: dict[str, Any] = {
        "configured_backend": configured_backend(),
        "migration_paused": migration_pause_active(),
        "manifest_exists": MIGRATION_MANIFEST_PATH.exists(),
    }
    if args.live:
        status["live"] = _live_backend_status(args.base_url)
    print(json.dumps(status, indent=2))
    return 0


def command_abort(_args: argparse.Namespace) -> int:
    update_backend_setting("sqlite")
    end_migration_pause()
    print("Cutover aborted. SQLite remains selected and collector writes are unpaused.")
    print("Reload the PythonAnywhere web app if it had already been reloaded onto MySQL.")
    return 0


def command_prepare_rollback(_args: argparse.Namespace) -> int:
    begin_migration_pause()
    update_backend_setting("sqlite")
    print("Rollback prepared: writes are paused and .env now selects SQLite.")
    print("Reload the PythonAnywhere web app, verify the site, then run:")
    print("  python tools/mysql_cutover.py finish-rollback")
    return 0


def command_finish_rollback(args: argparse.Namespace) -> int:
    if configured_backend() != "sqlite":
        raise RuntimeError("The server .env does not select SQLite.")
    live = _live_backend_status(args.base_url)
    if live.get("backend") != "sqlite":
        raise RuntimeError("The live web worker has not reloaded onto SQLite yet.")
    end_migration_pause()
    print("SQLite rollback completed and collector writes resumed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate = subparsers.add_parser("migrate", help="copy and verify SQLite data in MySQL")
    migrate.add_argument("--replace", action="store_true", help="drop/recreate destination tables")
    migrate.add_argument("--batch-size", type=int, default=1000)
    migrate.add_argument("--quiesce-seconds", type=int, default=10)
    migrate.set_defaults(handler=command_migrate)

    activate = subparsers.add_parser("activate", help="verify the reloaded app and unpause writes")
    activate.add_argument("--base-url", default=DEFAULT_LIVE_BASE)
    activate.set_defaults(handler=command_activate)

    status = subparsers.add_parser("status", help="show local and optionally live cutover state")
    status.add_argument("--live", action="store_true")
    status.add_argument("--base-url", default=DEFAULT_LIVE_BASE)
    status.set_defaults(handler=command_status)

    abort = subparsers.add_parser("abort", help="cancel an unactivated migration and resume SQLite")
    abort.set_defaults(handler=command_abort)

    rollback = subparsers.add_parser("prepare-rollback", help="pause writes and select SQLite")
    rollback.set_defaults(handler=command_prepare_rollback)

    finish = subparsers.add_parser("finish-rollback", help="verify live SQLite and unpause writes")
    finish.add_argument("--base-url", default=DEFAULT_LIVE_BASE)
    finish.set_defaults(handler=command_finish_rollback)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "batch_size", 1) < 1:
        parser.error("--batch-size must be positive")
    try:
        return int(args.handler(args))
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
