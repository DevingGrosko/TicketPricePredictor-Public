"""Bounded TiDB-only rehearsal of the verified source DDL, never a row import.

Only fixed staging schemas are allowed. Existing tables must match the reviewed
DDL and contain no rows. Never drops, truncates, alters, or overwrites a table.
Synthetic rows use explicit negative IDs inside a transaction rolled back in a
finally block. A failed run may leave newly CREATED EMPTY tables for inspection.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

FIXTURE_HASH = "989f549b31b4f626a2d2bd893267150f77e4253837d28bf2e0a8fd8cd8a6e605"
ARCHIVE_HASH = "02a1616ba73f6a4b67100c28049f519273cac469a83119bc1c3c7cb4360a8560"
EXPECTED = {
    "mlb": ("analytics_dirty_venue", "event", "iterations", "section_bucket_summary", "section_summary_state", "team_report_summary", "tickets"),
    "nfl": ("analytics_dirty_venue", "nfl_event", "nfl_iterations", "nfl_tickets", "section_bucket_summary", "section_summary_state"),
    "nhl": ("analytics_dirty_venue", "nhl_event", "nhl_iterations", "nhl_tickets", "section_bucket_summary", "section_summary_state"),
}
CREATE = re.compile(r"CREATE TABLE `([a-z_]+)` \(\n(.*?)\n\) ENGINE=InnoDB(?: AUTO_INCREMENT=(\d+))? DEFAULT CHARSET=utf8mb3;", re.S)
COLUMN = re.compile(r"  `([A-Za-z_]+)` (int|tinyint\(1\)|varchar\(\d+\)|datetime(?:\(6\))?|float|json) (NOT NULL|DEFAULT NULL)( AUTO_INCREMENT)?[,]?$")
KEY = re.compile(r"  (?:(PRIMARY) KEY|(?:(UNIQUE) )?KEY `([a-z_]+)`) \(([^)]+)\)[,]?$")
FK = re.compile(r"  CONSTRAINT `([a-z_0-9]+)` FOREIGN KEY \(`([a-z_]+)`\) REFERENCES `([a-z_]+)` \(`([a-z_]+)`\)[,]?$")


class RehearsalError(RuntimeError):
    """Contains only a locally defined diagnostic, never driver or credential text."""


def need(condition, message):
    if not condition:
        raise RehearsalError(message)


def normal_type(value):
    return re.sub(r"^int\(11\)$", "int", str(value).lower())


def normal_index(row):
    # TiDB returns NON_UNIQUE as text in information_schema.statistics.
    return (row[0], int(row[1]), int(row[2]), row[3])


def normal_collation(value):
    return str(value or "").lower().replace("utf8mb3_", "utf8_")


@dataclass
class Definition:
    name: str
    original: str
    ddl: str
    columns: list
    indexes: list
    foreign_keys: list
    auto_increment: int | None


def parse_definitions(content: bytes):
    need(hashlib.sha256(content).hexdigest() == FIXTURE_HASH, "Reviewed schema fixture checksum mismatch.")
    sections = re.split(r"^-- sport: (mlb|nfl|nhl)\n", content.decode(), flags=re.M)
    need(sections[1::2] == list(EXPECTED), "Schema sections differ from the approved sports.")
    result = {}
    for sport, section in zip(sections[1::2], sections[2::2]):
        found = list(CREATE.finditer(section))
        need([m[1] for m in found] == list(EXPECTED[sport]), "Unexpected source table names or counts.")
        need(not CREATE.sub("", section).strip(), "Unexpected SQL outside CREATE TABLE statements.")
        definitions = []
        available = set()
        for match in found:
            name, body, floor = match.groups()
            columns, indexes, fks = [], [], []
            for line in body.splitlines():
                c, k, f = COLUMN.fullmatch(line), KEY.fullmatch(line), FK.fullmatch(line)
                if c:
                    column, kind, null, auto = c.groups()
                    columns.append((column, kind, "NO" if null == "NOT NULL" else "YES", "auto_increment" if auto else "", "utf8_general_ci" if kind.startswith("varchar") else ""))
                elif k:
                    primary, unique, key_name, fields = k.groups()
                    key_name = "PRIMARY" if primary else key_name
                    for pos, field in enumerate(fields.split(","), 1):
                        need(bool(re.fullmatch(r"`[A-Za-z_]+`", field)), "Unsupported index expression.")
                        indexes.append((key_name, 0 if primary or unique else 1, pos, field.strip("`")))
                elif f:
                    constraint, col, parent, parent_col = f.groups()
                    need(parent in available, "Foreign-key creation order is unsafe.")
                    fks.append((constraint, col, parent, parent_col))
                else:
                    raise RehearsalError("Unreviewed table definition syntax.")
            old = match[0]
            ddl = old.removesuffix("DEFAULT CHARSET=utf8mb3;") + "DEFAULT CHARSET=utf8 COLLATE=utf8_general_ci;"
            need(ddl.replace("DEFAULT CHARSET=utf8 COLLATE=utf8_general_ci;", "DEFAULT CHARSET=utf8mb3;") == old, "Target changed more than table text options.")
            definitions.append(Definition(name, old, ddl, columns, sorted(indexes), sorted(fks), int(floor) if floor else None))
            available.add(name)
        result[sport] = definitions
    need(sum(len(d.columns) for ds in result.values() for d in ds) == 139, "Unexpected total column count.")
    need(sum(len(d.foreign_keys) for ds in result.values() for d in ds) == 6, "Unexpected foreign-key count.")
    return result


def inspect_table(connection, schema, d):
    params = {"schema": schema, "table": d.name}
    meta = connection.execute(text("SELECT TABLE_TYPE, TABLE_COLLATION, AUTO_INCREMENT FROM information_schema.tables WHERE table_schema=:schema AND table_name=:table"), params).one()
    need(meta[0] == "BASE TABLE" and normal_collation(meta[1]) == "utf8_general_ci", "Unexpected target table type or collation.")
    if d.auto_increment is not None:
        need(meta[2] is not None and int(meta[2]) >= d.auto_increment, "Target auto-increment floor is below the exported floor.")
    columns = connection.execute(text("SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, EXTRA, COLLATION_NAME FROM information_schema.columns WHERE table_schema=:schema AND table_name=:table ORDER BY ORDINAL_POSITION"), params)
    got = [(r[0], normal_type(r[1]), r[2], r[3] or "", normal_collation(r[4])) for r in columns]
    # TiDB may describe native JSON using a binary utf8mb4 collation; that is not
    # a source VARCHAR collation change. JSON values are checked in the probe.
    got = [(a,b,c,d_, "" if b == "json" else e) for a,b,c,d_,e in got]
    need(got == d.columns, "Target columns differ from the reviewed source definition.")
    indexes = connection.execute(text("SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME FROM information_schema.statistics WHERE table_schema=:schema AND table_name=:table"), params)
    need(sorted(normal_index(r) for r in indexes) == d.indexes, "Target indexes differ from the reviewed source definition.")
    fks = connection.execute(text("SELECT CONSTRAINT_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME, REFERENCED_TABLE_SCHEMA FROM information_schema.key_column_usage WHERE table_schema=:schema AND table_name=:table AND REFERENCED_TABLE_NAME IS NOT NULL"), params).all()
    need(all(r[4] == schema for r in fks), "Cross-schema foreign key detected.")
    need(sorted(tuple(r[:4]) for r in fks) == d.foreign_keys, "Target foreign keys differ from the reviewed source definition.")
    need(connection.exec_driver_sql(f"SELECT 1 FROM `{d.name}` LIMIT 1").first() is None, "Target contains rows; rehearsal refuses to proceed.")


def assert_target(connection, schema):
    need(connection.execute(text("SELECT DATABASE()")).scalar_one() == schema, "Wrong selected staging database.")
    version = connection.execute(text("SELECT VERSION()")).scalar_one()
    need("tidb" in str(version).lower(), "Connected server is not TiDB.")
    need(int(connection.execute(text("SELECT @@foreign_key_checks")).scalar_one()) == 1, "Foreign-key checks must be enabled.")


def sql_insert(connection, definition, values):
    allowed = {c[0] for c in definition.columns}
    need(set(values) <= allowed, "Invalid probe column name.")
    keys = list(values)
    sql = f"INSERT INTO `{definition.name}` (" + ",".join(f"`{k}`" for k in keys) + ") VALUES (" + ",".join(f":p{i}" for i in range(len(keys))) + ")"
    connection.execute(text(sql), {f"p{i}": values[k] for i, k in enumerate(keys)})


def must_reject(connection, definition, values, code):
    try:
        sql_insert(connection, definition, values)
    except IntegrityError as error:
        need(error.orig.args and error.orig.args[0] == code, "Probe was rejected for an unexpected reason.")
    else:
        raise RehearsalError("Expected uniqueness or foreign-key rejection did not occur.")


def probe(connection, sport, definitions):
    tables = {d.name: d for d in definitions}
    event = "event" if sport == "mlb" else f"{sport}_event"
    iterations = "iterations" if sport == "mlb" else f"{sport}_iterations"
    tickets = "tickets" if sport == "mlb" else f"{sport}_tickets"
    when = datetime(2026, 9, 20, 12, 34, 56, 123456)
    payload = {"section": "Balcon éA", "escaped": "quoted \" and slash \\"}
    e = {"id": -1, "title": "STAGING ONLY", "event_date": when}
    if sport == "mlb":
        e.update(event_sections=json.dumps(payload), URL="https://example.invalid/probe", Place="StagingCafé")
    else:
        e.update(sections=json.dumps(payload), source_id="staging-probe", source_url="https://example.invalid/probe", venue="StagingCafé")
        if sport == "nhl":
            e["currency"] = "USD"
    iteration = {"id": -1, "event_id": -1, "captured_at": when}
    ticket = {"id": -1, "section": "Balcon éA", "price": 137, "iteration_id": -1,
              ("ticketsPerSection" if sport == "mlb" else "listing_count"): 2}
    # Session-only pessimistic transactions give immediate constraint errors.
    connection.exec_driver_sql("SET SESSION tidb_txn_mode = 'pessimistic'")
    connection.commit()
    transaction = connection.begin()
    try:
        sql_insert(connection, tables[event], e)
        sql_insert(connection, tables[iterations], iteration)
        sql_insert(connection, tables[tickets], ticket)
        dirty = dict(venue="StagingCafé", revision=1, dirty=1, updated_at=when)
        sql_insert(connection, tables["analytics_dirty_venue"], dirty)
        must_reject(connection, tables["analytics_dirty_venue"], {**dirty, "venue": "STAGINGCAFE "}, 1062)
        must_reject(connection, tables[iterations], {**iteration, "id": -2, "event_id": -2}, 1452)
        must_reject(connection, tables[tickets], {**ticket, "id": -2, "iteration_id": -2}, 1452)
        if sport != "mlb":
            must_reject(connection, tables[iterations], {**iteration, "id": -2}, 1062)
            must_reject(connection, tables[event], {**e, "id": -2, "source_id": "STAGING-PROBE", "source_url": "https://example.invalid/other"}, 1062)
        sql_insert(connection, tables["section_bucket_summary"], dict(event_id=-1, section_key="probe", bucket_slot=0, section_name="Balcon éA", median_price=137.5, observation_count=1, first_captured_at=when, last_captured_at=when, refreshed_at=when))
        sql_insert(connection, tables["section_summary_state"], dict(event_id=-1, summary_version=1, event_signature="probe", source_iteration_id=-1, source_iteration_count=1, complete=1, refreshed_at=when))
        if sport == "mlb":
            sql_insert(connection, tables["team_report_summary"], dict(sport="mlb", venue="StagingCafé", season=2026, summary_version=1, source_revision=1, payload=json.dumps(payload), refreshed_at=when.replace(microsecond=0)))
        field = "event_sections" if sport == "mlb" else "sections"
        row = connection.exec_driver_sql(f"SELECT event_date, `{field}` FROM `{event}` WHERE id=-1").one()
        need(row[0] == when and json.loads(row[1]) == payload, "Datetime/JSON round-trip mismatch.")
        row = connection.exec_driver_sql(f"SELECT section, price FROM `{tickets}` WHERE id=-1").one()
        need(tuple(row) == ("Balcon éA", 137), "Ticket text/price round-trip mismatch.")
        value = connection.exec_driver_sql("SELECT median_price FROM section_bucket_summary WHERE event_id=-1").scalar_one()
        need(value == 137.5, "Floating-point summary round-trip mismatch.")
    finally:
        transaction.rollback()
    for d in definitions:
        need(connection.exec_driver_sql(f"SELECT 1 FROM `{d.name}` LIMIT 1").first() is None, "Probe rollback did not leave the tables empty.")
    connection.rollback()


def run():
    from Flask_App.tidb_staging import SCHEMAS, StagingTiDBConfig, create_staging_engine
    defs = parse_definitions(Path(__file__).with_name("tidb_staging_schema.sql").read_bytes())
    need(SCHEMAS == {s: f"ticketsignal_staging_{s}" for s in EXPECTED}, "Staging database map changed.")
    config = StagingTiDBConfig.from_environment()
    engines = {}
    try:
        engines = {s: create_staging_engine(s, config=config) for s in EXPECTED}
        # Inspect ALL destinations before writing any DDL. Resume only a matching,
        # row-empty partial rehearsal; never assume IF NOT EXISTS implies parity.
        existing = {}
        for s, engine in engines.items():
            with engine.connect() as c:
                assert_target(c, SCHEMAS[s])
                names = set(c.execute(text("SELECT TABLE_NAME FROM information_schema.tables WHERE table_schema=:schema"), {"schema": SCHEMAS[s]}).scalars())
                need(names <= set(EXPECTED[s]), "Unexpected target tables; stopped before schema creation.")
                for d in defs[s]:
                    if d.name in names:
                        inspect_table(c, SCHEMAS[s], d)
                existing[s] = names
        for s, engine in engines.items():
            with engine.connect() as c:
                assert_target(c, SCHEMAS[s])
                for d in defs[s]:
                    if d.name not in existing[s]:
                        c.exec_driver_sql(d.ddl)
                        c.commit()  # DDL persists; never promise DDL rollback.
                    inspect_table(c, SCHEMAS[s], d)
                c.rollback()
                probe(c, s, defs[s])
                print(f"PASS {s}: {len(defs[s])} table definitions, columns, indexes, FKs, text and typed-value probes; synthetic rows rolled back.", flush=True)
        print("PASS: 19 empty tables verified. No historical rows imported. No PythonAnywhere access or deployment.", flush=True)
    finally:
        for e in engines.values():
            e.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply-empty-staging", action="store_true", required=True)
    parser.parse_args()
    try:
        run()
    except Exception as exc:
        if isinstance(exc, RehearsalError):
            print("STOP: " + str(exc), file=sys.stderr)
        else:
            args = getattr(getattr(exc, "orig", None), "args", ())
            code = args[0] if args else None
            print(f"STOP: {type(exc).__name__}; driver code {code if isinstance(code, int) else 'not available'}. Credentials and raw SQL errors omitted.", file=sys.stderr)
        print("Previously created empty tables may remain; do not drop or import over them. Production was not accessed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
