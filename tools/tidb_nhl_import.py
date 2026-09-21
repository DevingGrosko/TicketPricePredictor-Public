"""Import ONLY the checksum-pinned NHL snapshot into pre-created TiDB staging.

Default is an offline audit. --apply requires all target tables to be empty.
--resume inserts only missing rows after checking EVERY existing row matches.
--verify reads only. No dump SQL is executed; INSERT values are decoded and bound.
No .env, .my.cnf, source database, production service or other sport is accessed.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import getpass
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import ssl
import struct
import sys
from typing import Any, Iterable

SOURCE_SHA256 = '3e4a611b3021b32a06dc3c32c5ff87e799aec1eb290aac7bf63851b8fc1e2413'
SQL_SHA256 = '11a40edb7194328f187e4216c9cb8fd74065450353b0b7b3743f54b121a8225b'
SCHEMA = 'ticketsignal_staging_nhl'
TABLES = ('analytics_dirty_venue', 'nhl_event', 'nhl_iterations', 'nhl_tickets',
          'section_bucket_summary', 'section_summary_state')
MAX_SOURCE = 4 * 1024 * 1024
MAX_SQL = 20 * 1024 * 1024
MAX_ROWS = 500000
HOST = re.compile(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+tidbcloud\.com\Z')
CREATE = re.compile(r'CREATE TABLE `([a-z_]+)` \(\n(.*?)\n\) ENGINE=InnoDB(?: AUTO_INCREMENT=(\d+))? DEFAULT CHARSET=utf8mb3;', re.S)
COLUMN = re.compile(r'  `([A-Za-z_]+)` (int|tinyint\(1\)|varchar\(\d+\)|datetime(?:\(6\))?|float|json) (NOT NULL|DEFAULT NULL)( AUTO_INCREMENT)?[,]?$')
KEY = re.compile(r'  (?:(PRIMARY) KEY|(?:(UNIQUE) )?KEY `([a-z_]+)`) \(([^)]+)\)[,]?$')
FK = re.compile(r'  CONSTRAINT `([a-z_0-9]+)` FOREIGN KEY \(`([a-z_]+)`\) REFERENCES `([a-z_]+)` \(`([a-z_]+)`\)[,]?$')
VALUE = re.compile(r"'(?:[^'\\]|\\.|'')*'|NULL|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
INSERT = re.compile(r'INSERT INTO `([a-z_]+)` VALUES (.*);\Z', re.S)
ESCAPES = {'0': '\0', 'b': '\b', 'n': '\n', 'r': '\r', 't': '\t',
           'Z': '\x1a', "'": "'", '"': '"', '\\': '\\', '%': '\\%', '_': '\\_'}


class Stop(RuntimeError):
    """Locally authored message without credentials, raw SQL, or row values."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Stop(message)


def quoted_value(token: str) -> str:
    text = token[1:-1]
    return re.sub(r"\\(.)|''", lambda m: ESCAPES.get(m[1], m[1]) if m[1] is not None else "'", text, flags=re.S)


def literal_rows(text: str) -> Iterable[tuple]:
    """Strict literal-only MySQL VALUES grammar; no eval or executable SQL."""
    i, n = 0, len(text)
    while i < n:
        require(text[i] == '(', 'Expected start of a literal row.')
        i += 1
        values = []
        while True:
            m = VALUE.match(text, i)
            require(m is not None, 'Unsupported or incomplete SQL value.')
            token = m[0]
            if token.startswith("'"):
                value = quoted_value(token)
            elif token == 'NULL':
                value = None
            else:
                value = Decimal(token) if any(c in token for c in '.eE') else int(token)
            values.append(value)
            i = m.end()
            require(i < n and text[i] in ',)', 'Unexpected SQL after a literal value.')
            delimiter = text[i]
            i += 1
            if delimiter == ')':
                break
        yield tuple(values)
        if i < n:
            require(text[i] == ',' and i + 1 < n, 'Unexpected SQL after a row.')
            i += 1
    require(i == n and n > 0, 'Empty or incomplete VALUES list.')


def reject_constant(value):
    raise Stop('Non-finite JSON number.')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'Duplicate JSON object key.')
        result[key] = value
    return result


def json_tree(value: str) -> Any:
    parsed = json.loads(value, parse_float=Decimal, parse_int=Decimal,
                        parse_constant=reject_constant, object_pairs_hook=unique_object)
    def canonical(item):
        if isinstance(item, dict):
            return ['object', [[k, canonical(v)] for k, v in sorted(item.items())]]
        if isinstance(item, list):
            return ['array', [canonical(v) for v in item]]
        if isinstance(item, Decimal):
            # Decimal tuple normalization without the decimal context's rounding.
            sign, digits, exponent = item.as_tuple()
            digits = list(digits)
            if not any(digits):
                return ['number', 0, '0', 0]
            while digits[-1] == 0:
                digits.pop(); exponent += 1
            return ['number', sign, ''.join(map(str, digits)), exponent]
        return ['atom', item]
    return canonical(parsed)


@dataclass
class Table:
    name: str
    columns: list
    indexes: list
    foreign_keys: list
    auto_increment: int | None
    rows: list

    @property
    def pk_positions(self):
        names = [c[0] for c in self.columns]
        return tuple(names.index(r[3]) for r in sorted(self.indexes) if r[0] == 'PRIMARY')

    def key(self, row):
        return tuple(row[i] for i in self.pk_positions)


def typed_row(table: Table, row: tuple) -> tuple:
    require(len(row) == len(table.columns), 'Column count mismatch in ' + table.name)
    result = []
    for (name, kind, nullable, _auto, _collation), value in zip(table.columns, row):
        if value is None:
            require(nullable == 'YES', 'NULL in required column of ' + table.name)
        elif kind in ('int', 'tinyint(1)'):
            require(isinstance(value, int) and not isinstance(value, bool), 'Invalid integer in ' + table.name)
            low, high = (-128, 127) if kind == 'tinyint(1)' else (-2147483648, 2147483647)
            require(low <= value <= high, 'Integer overflow in ' + table.name)
        elif kind.startswith('varchar'):
            require(isinstance(value, str), 'Invalid text in ' + table.name)
            require(len(value) <= int(re.search(r'\d+', kind)[0]) and all(ord(c) <= 65535 for c in value),
                    'Text outside reviewed column capacity in ' + table.name)
        elif kind.startswith('datetime'):
            if isinstance(value, str):
                require(bool(re.fullmatch(r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d{1,6})?', value)), 'Invalid datetime syntax.')
                value = datetime.fromisoformat(value)
            require(isinstance(value, datetime) and value.tzinfo is None and value.year >= 1000, 'Invalid datetime value.')
            require(kind != 'datetime' or value.microsecond == 0, 'Unexpected timestamp precision.')
        elif kind == 'float':
            require(isinstance(value, (int, float, Decimal)), 'Invalid float value.')
            value = struct.unpack('!f', struct.pack('!f', float(value)))[0]
            require(math.isfinite(value), 'Non-finite float value.')
        elif kind == 'json':
            require(isinstance(value, str), 'Invalid JSON storage value.')
            json_tree(value)  # Validate without rewriting the original JSON text.
        else:
            raise Stop('Unreviewed data type.')
        result.append(value)
    return tuple(result)


def row_fingerprint(table: Table, row: tuple) -> bytes:
    fields = []
    for column, value in zip(table.columns, typed_row(table, row)):
        kind = column[1]
        if value is None:
            fields.append(['null'])
        elif kind == 'json':
            fields.append(['json', json_tree(value)])
        elif kind.startswith('datetime'):
            fields.append(['datetime', value.isoformat(timespec='microseconds')])
        elif kind == 'float':
            fields.append(['float32', struct.pack('!f', value).hex()])
        else:
            fields.append([kind, value])
    encoded = json.dumps(fields, ensure_ascii=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).digest()


def source_map(table: Table) -> dict:
    output = {}
    for row in table.rows:
        key = table.key(row)
        require(key not in output, 'Duplicate primary key in ' + table.name)
        output[key] = row_fingerprint(table, row)
    return output


def parse_dump(content: bytes) -> list[Table]:
    require(hashlib.sha256(content).hexdigest() == SQL_SHA256, 'Uncompressed NHL snapshot checksum mismatch.')
    text = content.decode('utf-8', errors='strict')
    require(text.endswith('-- Dump completed on 2026-09-21  0:03:51\n'), 'Missing expected dump completion marker.')
    found = list(CREATE.finditer(text))
    require(tuple(m[1] for m in found) == TABLES, 'Unreviewed table set in dump.')
    tables = []
    for m in found:
        columns, indexes, fks = [], [], []
        for line in m[2].splitlines():
            c, k, f = COLUMN.fullmatch(line), KEY.fullmatch(line), FK.fullmatch(line)
            if c:
                name, kind, null, auto = c.groups()
                columns.append((name, kind, 'NO' if null == 'NOT NULL' else 'YES', 'auto_increment' if auto else '',
                                'utf8_general_ci' if kind.startswith('varchar') else ''))
            elif k:
                primary, unique, key, fields = k.groups()
                for position, field in enumerate(fields.split(','), 1):
                    require(bool(re.fullmatch(r'`[A-Za-z_]+`', field)), 'Unreviewed index expression.')
                    indexes.append(('PRIMARY' if primary else key, 0 if primary or unique else 1, position, field[1:-1]))
            elif f:
                fks.append(f.groups())
            else:
                raise Stop('Unreviewed column definition.')
        tables.append(Table(m[1], columns, sorted(indexes), sorted(fks), int(m[3]) if m[3] else None, []))
    by_name = {t.name: t for t in tables}
    for line in CREATE.sub('', text).splitlines():
        if not line or line.startswith('--'):
            continue
        if re.fullmatch(r'/\*!\d+ SET [^\r\n]* \*/;', line):
            continue  # Never execute session/constraint-changing dump commands.
        match = INSERT.fullmatch(line)
        require(match is not None and match[1] in by_name, 'Unexpected non-data SQL in dump.')
        table = by_name[match[1]]
        table.rows.extend(typed_row(table, row) for row in literal_rows(match[2]))
        require(len(table.rows) <= MAX_ROWS, 'Snapshot row safety limit exceeded.')
    require(sum(len(t.columns) for t in tables) == 52, 'Unexpected column total.')
    require(sum(len(t.foreign_keys) for t in tables) == 2, 'Unexpected foreign-key total.')
    for table in tables:
        require(bool(table.pk_positions), 'Missing primary key.')
        source_map(table)  # Verify uniqueness and all canonical representations.
        for _constraint, col, parent, parent_col in table.foreign_keys:
            target = by_name[parent]
            parent_position = [c[0] for c in target.columns].index(parent_col)
            child_position = [c[0] for c in table.columns].index(col)
            parent_values = {r[parent_position] for r in target.rows}
            require(all(r[child_position] in parent_values for r in table.rows), 'Orphaned source foreign key in ' + table.name)
    return tables


def load_source(path: Path) -> list[Table]:
    require(path.is_file() and path.stat().st_size <= MAX_SOURCE, 'Expected the original small NHL .sql.gz file.')
    data = path.read_bytes()
    require(hashlib.sha256(data).hexdigest() == SOURCE_SHA256, 'NHL gzip checksum mismatch; no connection attempted.')
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
        content = stream.read(MAX_SQL + 1)
        require(len(content) <= MAX_SQL and not stream.read(1), 'Decompressed snapshot exceeds safety limit.')
    return parse_dump(content)


def audit(tables: list[Table]) -> dict:
    result = {'snapshot_sha256': SOURCE_SHA256, 'sql_sha256': SQL_SHA256,
              'schema': SCHEMA, 'tables': {}, 'historical_import_performed': False}
    for table in tables:
        fingerprints = source_map(table)
        digest = hashlib.sha256(b''.join(fingerprints[k] for k in sorted(fingerprints))).hexdigest()
        result['tables'][table.name] = {'rows': len(table.rows), 'canonical_sha256': digest}
    result['total_rows'] = sum(len(t.rows) for t in tables)
    return result


def connect_staging():
    import pymysql
    host = os.getenv('TIDB_STAGING_HOST', '').strip().lower()
    user = os.getenv('TIDB_STAGING_USERNAME', '').strip()
    require(bool(HOST.fullmatch(host)) and len(host) <= 253, 'Only a valid *.tidbcloud.com staging host is allowed.')
    require(bool(user), 'TIDB_STAGING_USERNAME is missing.')
    password = os.getenv('TIDB_STAGING_PASSWORD')
    if not password:
        password = getpass.getpass('TiDB STAGING password (not PythonAnywhere): ')
    require(bool(password), 'Empty staging password.')
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    require(context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED, 'TLS verification is required.')
    return pymysql.connect(host=host, port=4000, user=user, password=password, database=SCHEMA,
                           charset='utf8mb4', ssl=context, connect_timeout=15,
                           read_timeout=90, write_timeout=90, autocommit=False,
                           local_infile=False, read_default_file=None)


def all_rows(connection, sql, parameters=None):
    with connection.cursor() as cur:
        cur.execute(sql, parameters)
        return cur.fetchall()


def check_target(connection, tables: list[Table]) -> None:
    selected, version, fk = all_rows(connection, 'SELECT DATABASE(), VERSION(), @@foreign_key_checks')[0]
    require(selected == SCHEMA and 'tidb' in str(version).lower() and int(fk) == 1, 'Wrong database/server or foreign-key checks disabled.')
    names = all_rows(connection, 'SELECT TABLE_NAME FROM information_schema.tables WHERE table_schema=%s', (SCHEMA,))
    require({r[0] for r in names} == set(TABLES), 'Target table set differs from the reviewed NHL schema.')
    for table in tables:
        parameters = (SCHEMA, table.name)
        meta = all_rows(connection, 'SELECT TABLE_TYPE,TABLE_COLLATION FROM information_schema.tables WHERE table_schema=%s AND table_name=%s', parameters)[0]
        require(meta[0] == 'BASE TABLE' and str(meta[1]).replace('utf8mb3_', 'utf8_') == 'utf8_general_ci', 'Target type/collation mismatch: ' + table.name)
        raw = all_rows(connection, 'SELECT COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,EXTRA,COLLATION_NAME FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ORDINAL_POSITION', parameters)
        cols = []
        for a, b, c, d, e in raw:
            kind = re.sub(r'^int\(11\)$', 'int', b.lower())
            cols.append((a, kind, c, d or '', '' if kind == 'json' else str(e or '').replace('utf8mb3_', 'utf8_')))
        require(cols == table.columns, 'Target column mismatch: ' + table.name)
        raw = all_rows(connection, 'SELECT INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME FROM information_schema.statistics WHERE table_schema=%s AND table_name=%s', parameters)
        require(sorted((a, int(b), int(c), d) for a, b, c, d in raw) == table.indexes, 'Target index mismatch: ' + table.name)
        raw = all_rows(connection, 'SELECT CONSTRAINT_NAME,COLUMN_NAME,REFERENCED_TABLE_NAME,REFERENCED_COLUMN_NAME,REFERENCED_TABLE_SCHEMA FROM information_schema.key_column_usage WHERE table_schema=%s AND table_name=%s AND REFERENCED_TABLE_NAME IS NOT NULL', parameters)
        require(all(r[4] == SCHEMA for r in raw) and sorted(tuple(r[:4]) for r in raw) == table.foreign_keys, 'Target foreign-key mismatch: ' + table.name)
        if table.auto_increment is not None:
            ddl = all_rows(connection, f'SHOW CREATE TABLE `{table.name}`')[0][1]
            line = re.search(r'\n\) ENGINE=InnoDB\b([^\n]*)\Z', ddl)
            base = re.search(r'(?:^|\s)AUTO_INCREMENT=(\d+)(?:\s|$)', line[1]) if line else None
            require(base is not None and int(base[1]) >= table.auto_increment, 'Target auto-increment base below source: ' + table.name)


def matched_keys(table: Table, rows: Iterable[tuple]) -> set:
    expected = source_map(table)
    seen = set()
    for row in rows:
        converted = typed_row(table, tuple(row))
        key = table.key(converted)
        require(key in expected and key not in seen, 'Unexpected or duplicate target primary key in ' + table.name)
        require(row_fingerprint(table, converted) == expected[key], 'Existing target values differ in ' + table.name)
        seen.add(key)
    return seen


def existing_keys(connection, table: Table) -> set:
    import pymysql
    fields = ','.join('`' + c[0] + '`' for c in table.columns)
    with connection.cursor(pymysql.cursors.SSCursor) as cursor:
        cursor.execute(f'SELECT {fields} FROM `{table.name}`')
        return matched_keys(table, cursor)


def batches(rows, size=250):
    batch, approximate_bytes = [], 0
    for row in rows:
        weight = sum(len(v.encode('utf-8')) if isinstance(v, str) else 32 for v in row)
        require(weight <= 1024 * 1024, 'Single row exceeds the bounded importer limit.')
        if batch and (len(batch) >= size or approximate_bytes + weight > 512 * 1024):
            yield batch
            batch, approximate_bytes = [], 0
        batch.append(row); approximate_bytes += weight
    if batch:
        yield batch


def insert_batch(connection, table: Table, rows: list) -> None:
    fields = ','.join('`' + c[0] + '`' for c in table.columns)
    sql = f'INSERT INTO `{table.name}` ({fields}) VALUES (' + ','.join(['%s'] * len(table.columns)) + ')'
    try:
        connection.begin()
        with connection.cursor() as cursor:
            count = cursor.executemany(sql, rows)
            require(count == len(rows), 'Inserted row count mismatch.')
            cursor.execute('SHOW WARNINGS LIMIT 1')
            require(cursor.fetchone() is None, 'Server warning: stopped rather than accepting a conversion.')
        connection.commit()
    except BaseException:
        try:
            connection.rollback()
        except Exception:
            pass  # Commit outcome will be resolved by full comparison on resume.
        raise


def run_import(tables: list[Table], mode: str) -> dict:
    require(mode in ('apply', 'resume', 'verify'), 'Unsupported operation mode.')
    result = audit(tables)
    connection = connect_staging()
    total_inserted = 0
    try:
        check_target(connection, tables)
        present = {t.name: existing_keys(connection, t) for t in tables}
        if mode == 'apply':
            require(not any(present.values()), 'Target is not empty. Use --verify, or reviewed --resume; never delete it.')
        if mode == 'verify':
            require(all(len(present[t.name]) == len(t.rows) for t in tables), 'Target is incomplete; no data was changed.')
        connection.rollback()
        if mode != 'verify':
            # Only connection-local settings. Constraints remain enabled throughout.
            with connection.cursor() as c:
                c.execute("SET SESSION sql_mode='STRICT_TRANS_TABLES,NO_AUTO_VALUE_ON_ZERO,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION'")
                c.execute("SET SESSION time_zone='+00:00'")
                c.execute("SET SESSION tidb_txn_mode='pessimistic'")
            connection.commit()
            for table in tables:
                missing = (r for r in table.rows if table.key(r) not in present[table.name])
                inserted = 0
                for batch in batches(missing):
                    insert_batch(connection, table, batch)
                    inserted += len(batch)
                    total_inserted += len(batch)
                    if inserted % 10000 == 0:
                        print(f'{table.name}: {inserted:,} new rows committed', flush=True)
                print(f'{table.name}: inserted {inserted:,}; previously matching {len(present[table.name]):,}', flush=True)
        # Independent post-commit read-back of every column of every target row.
        connection.rollback()
        for table in tables:
            matched = existing_keys(connection, table)
            require(len(matched) == len(table.rows), 'Final table completeness check failed: ' + table.name)
            print(f'VERIFIED {table.name}: {len(matched):,} rows, every field matched', flush=True)
        result['historical_import_performed'] = total_inserted > 0
        result['rows_inserted_this_run'] = total_inserted
        result['target_full_comparison_passed'] = True
        result['mode'] = mode
        return result
    finally:
        connection.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--apply', action='store_true')
    actions.add_argument('--resume', action='store_true')
    actions.add_argument('--verify', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.report:
            require(not args.report.exists(), 'Report output already exists; choose a new report path.')
            require(args.report.parent.is_dir(), 'Report output directory does not exist.')
        tables = load_source(args.source)
        print('VERIFIED: original NHL gzip and decompressed SQL checksums.', flush=True)
        result = audit(tables)
        for name, info in result['tables'].items():
            print(f"Source {name}: {info['rows']:,} rows", flush=True)
        mode = 'apply' if args.apply else 'resume' if args.resume else 'verify' if args.verify else 'audit'
        if mode != 'audit':
            print('Destination: TiDB / ticketsignal_staging_nhl only. Keep staging collectors stopped.', flush=True)
            result = run_import(tables, mode)
        if args.report:
            with args.report.open('x') as stream:
                json.dump(result, stream, indent=2); stream.write('\n')
        print('PASS: offline audit only; no network connection.' if mode == 'audit' else
              'PASS: NHL snapshot fully verified in TiDB staging. PythonAnywhere was not accessed.', flush=True)
        return 0
    except Exception as error:
        code = error.args[0] if error.args and isinstance(error.args[0], int) else None
        detail = str(error) if isinstance(error, Stop) else type(error).__name__ + (f' (database code {code})' if code is not None else '')
        print('STOP: ' + detail, file=sys.stderr)
        if args.apply or args.resume:
            print('Earlier committed staging batches may remain. No data was deleted or overwritten. Do not run the import again blindly; report this result.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
