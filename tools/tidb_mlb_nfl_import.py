"""Checksum-pinned MLB/NFL imports into existing TiDB staging tables only.

Default: offline audit. --resume validates ALL existing rows before adding only
missing rows; --apply requires empty targets; --verify never inserts. No dump
SQL or DDL is executed. The old NHL importer and its verified data are untouched.
Rows are streamed; a disposable, disk-backed SQLite index bounds memory usage.
Keep ALL staging writers stopped until the snapshot verification is complete.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import getpass
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import ssl
import struct
import sys
import tempfile

SPECS = {
    'nfl': {
        'gzip_sha': '98c4a65de897d290b2233c0c07ee4939b0b3c2888ec23b01c8d12fa4c73cd860',
        'sql_sha': '08e1e4eb3dea0bc0073c75ae290767ef6ac7bb313efe16297ac9d5b7a7a19dec',
        'ddl_sha': '4781d3364c62b2d9ccf3930c5f36d58d2aa0256ee324939f8c568f9c0a0e8a06',
        'gzip_bytes': 14123278, 'sql_bytes': 70483912,
        'ending': '-- Dump completed on 2026-09-21  0:03:49\n',
        'tables': ('analytics_dirty_venue', 'nfl_event', 'nfl_iterations', 'nfl_tickets',
                   'section_bucket_summary', 'section_summary_state'),
        'column_count': 46,
    },
    'mlb': {
        'gzip_sha': '06c22d54401a78e078d3aa5472815e070a48f4b135f8523400eec33830a1389f',
        'sql_sha': 'f7621c8a692de8ea7736e1f00619917623ed076557f312f1aa76908372445fea',
        'ddl_sha': 'fffc0abaa69caecffcfc32c68644b5da4c2e339c12e05dba96a4a06c35abaad3',
        'gzip_bytes': 46256829, 'sql_bytes': 283171520,
        'ending': '-- Dump completed on 2026-09-21  0:03:41\n',
        'tables': ('analytics_dirty_venue', 'event', 'iterations', 'section_bucket_summary',
                   'section_summary_state', 'team_report_summary', 'tickets'),
        'column_count': 41,
    },
}
HOST = re.compile(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+tidbcloud\.com\Z')
CREATE = re.compile(r'CREATE TABLE `([a-z_]+)` \(\n(.*?)\n\) ENGINE=InnoDB(?: AUTO_INCREMENT=(\d+))? DEFAULT CHARSET=utf8mb3;', re.S)
COLUMN = re.compile(r'  `([A-Za-z_]+)` (int|tinyint\(1\)|varchar\(\d+\)|datetime(?:\(6\))?|float|json) (NOT NULL|DEFAULT NULL)( AUTO_INCREMENT)?[,]?$')
KEY = re.compile(r'  (?:(PRIMARY) KEY|(?:(UNIQUE) )?KEY `([a-z_]+)`) \(([^)]+)\)[,]?$')
FK = re.compile(r'  CONSTRAINT `([a-z_0-9]+)` FOREIGN KEY \(`([a-z_]+)`\) REFERENCES `([a-z_]+)` \(`([a-z_]+)`\)[,]?$')
VALUE = re.compile(r"'(?:[^'\\]|\\.|'')*'|NULL|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
INSERT = re.compile(r'INSERT INTO `([a-z_]+)` VALUES (.*);\Z', re.S)
ESCAPES = {'0': '\0', 'b': '\b', 'n': '\n', 'r': '\r', 't': '\t',
           'Z': '\x1a', "'": "'", '"': '"', '\\': '\\', '%': '\\%', '_': '\\_'}
MAX_LINE = 4 * 1024 * 1024
BATCH_ROWS = 1000
BATCH_BYTES = 256 * 1024


class Stop(RuntimeError):
    """Only locally authored messages: never secrets, row data or raw SQL."""


def require(condition, message):
    if not condition:
        raise Stop(message)


def spec_for(sport):
    require(sport in SPECS, 'Only the reviewed mlb and nfl snapshots are allowed.')
    return SPECS[sport]


def schema_for(sport):
    spec_for(sport)
    return 'ticketsignal_staging_' + sport


def quoted_value(token):
    return re.sub(r"\\(.)|''", lambda m: ESCAPES.get(m[1], m[1]) if m[1] is not None else "'", token[1:-1], flags=re.S)


def literal_rows(text):
    """The same literal-only grammar used by the verified NHL importer."""
    i, n = 0, len(text)
    require(n > 0, 'Empty VALUES list.')
    while i < n:
        require(text[i] == '(', 'Expected a literal row.')
        i += 1
        values = []
        while True:
            m = VALUE.match(text, i)
            require(m is not None, 'Unsupported or incomplete SQL literal.')
            token = m[0]
            if token.startswith("'"):
                value = quoted_value(token)
            elif token == 'NULL':
                value = None
            else:
                value = Decimal(token) if any(c in token for c in '.eE') else int(token)
            values.append(value)
            i = m.end()
            require(i < n and text[i] in ',)', 'Unexpected SQL after a literal.')
            delimiter = text[i]
            i += 1
            if delimiter == ')':
                break
        yield tuple(values)
        if i < n:
            require(text[i] == ',' and i + 1 < n, 'Unexpected SQL after a row.')
            i += 1


def unique_object(pairs):
    result = {}
    for k, v in pairs:
        require(k not in result, 'Duplicate JSON key.')
        result[k] = v
    return result


def reject_constant(_):
    raise Stop('Non-finite JSON value.')


def json_tree(value):
    item = json.loads(value, parse_float=Decimal, parse_int=Decimal,
                      parse_constant=reject_constant, object_pairs_hook=unique_object)
    def walk(v):
        if isinstance(v, dict):
            return ['object', [[k, walk(x)] for k, x in sorted(v.items())]]
        if isinstance(v, list):
            return ['array', [walk(x) for x in v]]
        if isinstance(v, Decimal):
            sign, digits, exp = v.as_tuple()
            digits = list(digits)
            if not any(digits):
                return ['number', 0, '0', 0]
            while digits[-1] == 0:
                digits.pop(); exp += 1
            return ['number', sign, ''.join(map(str, digits)), exp]
        return ['atom', v]
    return walk(item)


@dataclass
class Table:
    name: str
    columns: list
    indexes: list
    foreign_keys: list
    auto_increment: int | None
    original: str = ''
    count: int = 0
    pk_positions: tuple = field(init=False)

    def __post_init__(self):
        names = [c[0] for c in self.columns]
        self.pk_positions = tuple(names.index(x[3]) for x in sorted(self.indexes) if x[0] == 'PRIMARY')
        require(bool(self.pk_positions), 'Every source table must have a primary key.')

    def key(self, row):
        return encode_key(tuple(row[i] for i in self.pk_positions))


def encode_key(values):
    return json.dumps(values, ensure_ascii=True, separators=(',', ':')).encode()


def parse_table(sql):
    m = CREATE.fullmatch(sql)
    require(m is not None, 'Unreviewed CREATE TABLE syntax.')
    cols, keys, fks = [], [], []
    for line in m[2].splitlines():
        c, k, f = COLUMN.fullmatch(line), KEY.fullmatch(line), FK.fullmatch(line)
        if c:
            name, kind, null, auto = c.groups()
            cols.append((name, kind, 'NO' if null == 'NOT NULL' else 'YES',
                         'auto_increment' if auto else '', 'utf8_general_ci' if kind.startswith('varchar') else ''))
        elif k:
            primary, unique, name, fields = k.groups()
            for pos, value in enumerate(fields.split(','), 1):
                require(bool(re.fullmatch(r'`[A-Za-z_]+`', value)), 'Unreviewed index expression.')
                keys.append(('PRIMARY' if primary else name, 0 if primary or unique else 1, pos, value[1:-1]))
        elif f:
            fks.append(f.groups())
        else:
            raise Stop('Unreviewed schema line.')
    return Table(m[1], cols, sorted(keys), sorted(fks), int(m[3]) if m[3] else None, sql)


def typed_row(table, row):
    require(len(row) == len(table.columns), 'Column count mismatch: ' + table.name)
    result = []
    for (name, kind, null, _auto, _coll), value in zip(table.columns, row):
        if value is None:
            require(null == 'YES', 'NULL in a required column: ' + table.name)
        elif kind in ('int', 'tinyint(1)'):
            require(isinstance(value, int) and not isinstance(value, bool), 'Invalid integer.')
            lo, hi = (-128, 127) if kind == 'tinyint(1)' else (-2147483648, 2147483647)
            require(lo <= value <= hi, 'Integer overflow.')
        elif kind.startswith('varchar'):
            require(isinstance(value, str), 'Invalid text.')
            require(len(value) <= int(re.search(r'\d+', kind)[0]) and all(ord(c) <= 65535 for c in value), 'Text exceeds reviewed capacity.')
        elif kind.startswith('datetime'):
            if isinstance(value, str):
                require(bool(re.fullmatch(r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d{1,6})?', value)), 'Invalid datetime syntax.')
                value = datetime.fromisoformat(value)
            require(isinstance(value, datetime) and value.tzinfo is None and value.year >= 1000, 'Invalid datetime.')
            require(kind != 'datetime' or value.microsecond == 0, 'Unexpected datetime precision.')
        elif kind == 'float':
            require(isinstance(value, (int, float, Decimal)), 'Invalid FLOAT.')
            value = struct.unpack('!f', struct.pack('!f', float(value)))[0]
            require(math.isfinite(value), 'Non-finite FLOAT.')
        elif kind == 'json':
            require(isinstance(value, str), 'Invalid JSON storage.')
            json_tree(value)
        else:
            raise Stop('Unreviewed column type.')
        result.append(value)
    return tuple(result)


def row_fingerprint(table, row):
    fields = []
    for column, value in zip(table.columns, row):
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
    return hashlib.sha256(json.dumps(fields, ensure_ascii=True, separators=(',', ':')).encode()).digest()


def read_dump(path, sport):
    """Yield DDL or literal row groups, never executing any supplied SQL."""
    spec = spec_for(sport)
    h, total, ddl, last = hashlib.sha256(), 0, [], ''
    with gzip.open(path, 'rb') as stream:
        while True:
            raw = stream.readline(MAX_LINE + 1)
            if not raw:
                break
            require(len(raw) <= MAX_LINE, 'Source statement exceeds reviewed size limit.')
            total += len(raw); h.update(raw)
            require(total <= spec['sql_bytes'], 'Decompressed source exceeds pinned size.')
            line = raw.decode('utf-8', errors='strict'); last = line
            if line.startswith('CREATE TABLE '):
                require(not ddl, 'Nested source DDL.')
                ddl = [line]
                continue
            if ddl:
                ddl.append(line)
                require(sum(map(len, ddl)) <= 20000, 'Oversized source DDL.')
                if line.startswith(') ENGINE=InnoDB'):
                    yield 'table', ''.join(ddl).rstrip('\n')
                    ddl = []
                continue
            text = line.rstrip('\n')
            if not text or text.startswith('--'):
                continue
            if re.fullmatch(r'/\*!\d+ SET [^\r\n]* \*/;', text):
                continue
            match = INSERT.fullmatch(text)
            require(match is not None, 'Unexpected non-data SQL in pinned source.')
            yield 'rows', (match[1], match[2])
    require(not ddl and total == spec['sql_bytes'] and h.hexdigest() == spec['sql_sha'] and last == spec['ending'],
            'Decompressed checksum, length or completion marker mismatch.')


def verify_gzip(path, sport):
    spec = spec_for(sport)
    require(path.is_file() and path.stat().st_size == spec['gzip_bytes'], 'Wrong source file size for ' + sport)
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for b in iter(lambda: stream.read(1024 * 1024), b''): h.update(b)
    require(h.hexdigest() == spec['gzip_sha'], 'Compressed source checksum mismatch; no connection attempted.')


class DiskIndex:
    """Fresh scratch index, not an application database or reusable checkpoint."""
    def __init__(self, path):
        require(not path.exists(), 'Scratch index already exists.')
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA cache_size=-16384')
        self.db.execute('PRAGMA journal_mode=OFF')
        self.db.execute('PRAGMA synchronous=OFF')
        self.db.execute('PRAGMA temp_store=FILE')
        self.names = set()

    def add_table(self, table):
        require(table.name not in self.names and bool(re.fullmatch('[a-z_]+', table.name)), 'Unexpected or duplicate table.')
        self.db.execute(f'CREATE TABLE "{table.name}" (k BLOB PRIMARY KEY, h BLOB NOT NULL, parent BLOB, seen INTEGER NOT NULL DEFAULT 0) WITHOUT ROWID')
        self.names.add(table.name)

    def add_rows(self, table, rows):
        require(len(table.foreign_keys) <= 1, 'Unreviewed multi-parent source table.')
        fk_position = [c[0] for c in table.columns].index(table.foreign_keys[0][1]) if table.foreign_keys else None
        def records():
            for row in rows:
                yield (table.key(row), row_fingerprint(table, row), encode_key((row[fk_position],)) if fk_position is not None else None)
        try:
            self.db.executemany(f'INSERT INTO "{table.name}" (k,h,parent) VALUES (?,?,?)', records())
        except sqlite3.IntegrityError:
            raise Stop('Duplicate source primary key: ' + table.name) from None

    def check_foreign_keys(self, tables):
        by_name = {t.name: t for t in tables}
        for t in tables:
            for _, col, parent, parent_col in t.foreign_keys:
                p = by_name[parent]
                require([p.columns[i][0] for i in p.pk_positions] == [parent_col], 'Unreviewed parent key.')
                missing = self.db.execute(f'SELECT 1 FROM "{t.name}" c LEFT JOIN "{parent}" p ON c.parent=p.k WHERE p.k IS NULL LIMIT 1').fetchone()
                require(missing is None, 'Orphaned source foreign key: ' + t.name)

    def reset_seen(self):
        for name in self.names:
            self.db.execute(f'UPDATE "{name}" SET seen=0 WHERE seen<>0')
        self.db.commit()

    def match(self, table, rows):
        count = 0
        for row in rows:
            row = typed_row(table, tuple(row)); key = table.key(row)
            old = self.db.execute(f'SELECT h,seen FROM "{table.name}" WHERE k=?', (key,)).fetchone()
            require(old is not None and old[1] == 0, 'Unknown or duplicate target primary key: ' + table.name)
            require(old[0] == row_fingerprint(table, row), 'Existing target values differ: ' + table.name)
            self.db.execute(f'UPDATE "{table.name}" SET seen=1 WHERE k=?', (key,))
            count += 1
        self.db.commit()
        return count

    def missing(self, table, row):
        got = self.db.execute(f'SELECT h,seen FROM "{table.name}" WHERE k=?', (table.key(row),)).fetchone()
        require(got is not None and got[0] == row_fingerprint(table, row), 'Source changed after offline audit.')
        return not got[1]

    def fingerprint(self, table):
        h = hashlib.sha256()
        for (digest,) in self.db.execute(f'SELECT h FROM "{table.name}" ORDER BY k'):
            h.update(digest)
        return h.hexdigest()

    def close(self):
        self.db.close()


def audit_source(path, sport, index):
    verify_gzip(path, sport)
    tables, by_name = [], {}
    for kind, value in read_dump(path, sport):
        if kind == 'table':
            t = parse_table(value)
            require(t.name in spec_for(sport)['tables'], 'Wrong source table.')
            require(all(f[2] in by_name for f in t.foreign_keys), 'Unsafe source parent order.')
            index.add_table(t); tables.append(t); by_name[t.name] = t
        else:
            name, values = value
            require(name in by_name, 'Data appeared before its table definition.')
            t = by_name[name]
            batch = []
            for row in literal_rows(values):
                batch.append(typed_row(t, row)); t.count += 1
                require(t.count <= 10000000, 'Source row safety limit exceeded.')
                if len(batch) == 1000:
                    index.add_rows(t, batch); batch = []
            if batch: index.add_rows(t, batch)
            index.db.commit()
    spec = spec_for(sport)
    require(tuple(t.name for t in tables) == spec['tables'], 'Wrong source table sequence.')
    require(sum(len(t.columns) for t in tables) == spec['column_count'], 'Wrong source column total.')
    ddl = '\n\n'.join(t.original for t in tables) + '\n'
    require(hashlib.sha256(ddl.encode()).hexdigest() == spec['ddl_sha'], 'Actual dump DDL differs from reviewed DDL.')
    index.check_foreign_keys(tables)
    result = {'sport': sport, 'schema': schema_for(sport), 'snapshot_sha256': spec['gzip_sha'],
              'sql_sha256': spec['sql_sha'], 'total_rows': sum(t.count for t in tables),
              'fingerprint_order': 'JSON-primary-key-bytes', 'tables': {}, 'historical_import_performed': False}
    for t in tables:
        result['tables'][t.name] = {'rows': t.count, 'canonical_sha256': index.fingerprint(t)}
        print(f'VERIFIED source {sport}/{t.name}: {t.count:,} rows', flush=True)
    print(f'VERIFIED {sport}: both source checksums, schema, types, primary keys and foreign keys.', flush=True)
    return tables, result


def connect_staging(sport):
    import pymysql
    schema = schema_for(sport)
    host = os.getenv('TIDB_STAGING_HOST', '').strip().lower()
    user = os.getenv('TIDB_STAGING_USERNAME', '').strip()
    require(bool(HOST.fullmatch(host)) and len(host) <= 253, 'Only a valid *.tidbcloud.com staging host is allowed.')
    require(bool(user), 'Missing TIDB_STAGING_USERNAME.')
    password = os.getenv('TIDB_STAGING_PASSWORD') or getpass.getpass('TiDB STAGING password (not PythonAnywhere): ')
    require(bool(password), 'Empty staging password.')
    context = ssl.create_default_context(); context.minimum_version = ssl.TLSVersion.TLSv1_2
    require(context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED, 'TLS verification required.')
    return pymysql.connect(host=host, port=4000, user=user, password=password, database=schema,
                           charset='utf8mb4', ssl=context, connect_timeout=15, read_timeout=90,
                           write_timeout=90, autocommit=False, local_infile=False, read_default_file=None)


def query(connection, sql, args=None):
    with connection.cursor() as c:
        c.execute(sql, args)
        return c.fetchall()


def check_target(connection, sport, tables):
    schema = schema_for(sport)
    selected, version, fk = query(connection, 'SELECT DATABASE(),VERSION(),@@foreign_key_checks')[0]
    require(selected == schema and 'tidb' in str(version).lower() and int(fk) == 1, 'Wrong destination/server or disabled constraints.')
    names = query(connection, 'SELECT TABLE_NAME FROM information_schema.tables WHERE table_schema=%s', (schema,))
    require({r[0] for r in names} == set(spec_for(sport)['tables']), 'Destination table set mismatch.')
    for t in tables:
        args = (schema, t.name)
        meta = query(connection, 'SELECT TABLE_TYPE,TABLE_COLLATION FROM information_schema.tables WHERE table_schema=%s AND table_name=%s', args)[0]
        require(meta[0] == 'BASE TABLE' and str(meta[1]).replace('utf8mb3_', 'utf8_') == 'utf8_general_ci', 'Destination collation/type mismatch: ' + t.name)
        cols = query(connection, 'SELECT COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,EXTRA,COLLATION_NAME FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ORDINAL_POSITION', args)
        normalized = []
        for a, b, c, d, e in cols:
            kind = re.sub(r'^int\(11\)$', 'int', b.lower())
            normalized.append((a, kind, c, d or '', '' if kind == 'json' else str(e or '').replace('utf8mb3_', 'utf8_')))
        require(normalized == t.columns, 'Destination column mismatch: ' + t.name)
        keys = query(connection, 'SELECT INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME FROM information_schema.statistics WHERE table_schema=%s AND table_name=%s', args)
        require(sorted((a, int(b), int(c), d) for a,b,c,d in keys) == t.indexes, 'Destination index mismatch: ' + t.name)
        fks = query(connection, 'SELECT CONSTRAINT_NAME,COLUMN_NAME,REFERENCED_TABLE_NAME,REFERENCED_COLUMN_NAME,REFERENCED_TABLE_SCHEMA FROM information_schema.key_column_usage WHERE table_schema=%s AND table_name=%s AND REFERENCED_TABLE_NAME IS NOT NULL', args)
        require(all(r[4] == schema for r in fks) and sorted(tuple(r[:4]) for r in fks) == t.foreign_keys, 'Destination foreign-key mismatch: ' + t.name)
        if t.auto_increment is not None:
            ddl = query(connection, f'SHOW CREATE TABLE `{t.name}`')[0][1]
            line = re.search(r'\n\) ENGINE=InnoDB\b([^\n]*)\Z', ddl)
            base = re.search(r'(?:^|\s)AUTO_INCREMENT=(\d+)(?:\s|$)', line[1]) if line else None
            require(base is not None and int(base[1]) >= t.auto_increment, 'Destination auto-increment floor too low: ' + t.name)
    connection.rollback()


def compare_target(connection, tables, index, complete=False):
    import pymysql
    index.reset_seen()
    counts = {}
    for t in tables:
        fields = ','.join('`' + c[0] + '`' for c in t.columns)
        with connection.cursor(pymysql.cursors.SSCursor) as cur:
            cur.execute(f'SELECT {fields} FROM `{t.name}`')
            count = index.match(t, cur)
        connection.rollback()
        require(not complete or count == t.count, 'Destination is incomplete: ' + t.name)
        counts[t.name] = count
        print(f'{"VERIFIED" if complete else "Existing matching"} {t.name}: {count:,} rows', flush=True)
    return counts


def row_batches(rows):
    batch, weight = [], 0
    for row in rows:
        size = sum(2 * len(v.encode('utf8')) + 8 if isinstance(v, str) else 64 for v in row)
        require(size <= 2 * 1024 * 1024, 'Single row exceeds the reviewed import limit.')
        if batch and (len(batch) >= BATCH_ROWS or weight + size > BATCH_BYTES):
            yield batch
            batch, weight = [], 0
        batch.append(row); weight += size
    if batch: yield batch


def insert_batch(connection, table, rows):
    require(bool(rows) and len(rows) <= BATCH_ROWS, 'Invalid insert batch size.')
    fields = ','.join('`' + c[0] + '`' for c in table.columns)
    group = '(' + ','.join(['%s'] * len(table.columns)) + ')'
    sql = f'INSERT INTO `{table.name}` ({fields}) VALUES ' + ','.join([group] * len(rows))
    parameters = tuple(v for row in rows for v in row)
    try:
        connection.begin()
        with connection.cursor() as c:
            # One statement per batch: warning information cannot be lost between
            # automatic executemany splits. Dump SQL is never executed.
            count = c.execute(sql, parameters)
            require(count == len(rows), 'Inserted row count mismatch.')
            c.execute('SHOW WARNINGS')
            require(c.fetchone() is None, 'Server conversion warning; batch rejected.')
        connection.commit()
    except BaseException:
        try: connection.rollback()
        except Exception: pass
        raise


def run_import(path, sport, tables, index, result, mode):
    require(mode in ('apply', 'resume', 'verify'), 'Invalid import mode.')
    connection = connect_staging(sport)
    inserted = 0
    try:
        check_target(connection, sport, tables)
        present = compare_target(connection, tables, index, complete=(mode == 'verify'))
        require(mode != 'apply' or not any(present.values()), 'Destination is populated; use reviewed resume, never delete it.')
        if mode != 'verify':
            with connection.cursor() as c:
                c.execute("SET SESSION sql_mode='STRICT_TRANS_TABLES,NO_AUTO_VALUE_ON_ZERO,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION'")
                c.execute("SET SESSION time_zone='+00:00'")
                c.execute("SET SESSION tidb_txn_mode='pessimistic'")
            connection.commit()
            by_name = {t.name: t for t in tables}
            last_table = None; table_inserted = 0; next_progress = 100000
            for kind, value in read_dump(path, sport):
                if kind == 'table': continue
                name, values = value; t = by_name[name]
                if name != last_table:
                    if last_table is not None: print(f'{last_table}: {table_inserted:,} new rows committed', flush=True)
                    last_table = name; table_inserted = 0; next_progress = 100000
                def missing_rows():
                    for raw in literal_rows(values):
                        row = typed_row(t, raw)
                        if index.missing(t, row): yield row
                for batch in row_batches(missing_rows()):
                    insert_batch(connection, t, batch)
                    inserted += len(batch); table_inserted += len(batch)
                    if table_inserted >= next_progress:
                        print(f'{name}: {table_inserted:,} new rows committed', flush=True)
                        next_progress += 100000
            if last_table is not None: print(f'{last_table}: {table_inserted:,} new rows committed', flush=True)
            compare_target(connection, tables, index, complete=True)
        result.update(mode=mode, historical_import_performed=inserted > 0,
                      rows_inserted_this_run=inserted, target_full_comparison_passed=True)
        return result
    finally:
        try: connection.rollback()
        finally: connection.close()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sport', choices=tuple(SPECS), required=True)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--work-dir', type=Path, required=True)
    p.add_argument('--report', type=Path)
    actions = p.add_mutually_exclusive_group()
    for name in ('apply', 'resume', 'verify'): actions.add_argument('--' + name, action='store_true')
    a = p.parse_args(argv)
    mode = 'apply' if a.apply else 'resume' if a.resume else 'verify' if a.verify else 'audit'
    stage = 'offline validation'
    try:
        require(a.work_dir.is_dir(), 'Working directory does not exist.')
        if a.report:
            require(not a.report.exists() and a.report.parent.is_dir(), 'Report must be a new file in an existing directory.')
        needed = 700 * 1024**2 if a.sport == 'mlb' else 250 * 1024**2
        require(shutil.disk_usage(a.work_dir).free >= needed, 'Insufficient disk headroom for temporary audit index.')
        with tempfile.TemporaryDirectory(prefix='audit-' + a.sport + '-', dir=a.work_dir) as tmp:
            index = DiskIndex(Path(tmp) / 'fingerprints.sqlite')
            try:
                print('Auditing ' + a.sport + ' locally; no database connection yet.', flush=True)
                tables, result = audit_source(a.source, a.sport, index)
                result['scratch_bytes'] = index.path.stat().st_size
                if mode != 'audit':
                    stage = 'TiDB staging preflight/import/verification'
                    print('Destination: TiDB / ' + schema_for(a.sport) + ' ONLY. Keep staging writers stopped.', flush=True)
                    run_import(a.source, a.sport, tables, index, result, mode)
                if a.report:
                    with a.report.open('x') as f: json.dump(result, f, indent=2); f.write('\n')
                print('PASS: offline audit only; no connection.' if mode == 'audit' else
                      'PASS: ' + a.sport.upper() + ' snapshot fully verified in TiDB staging. Production was not accessed.', flush=True)
            finally: index.close()
        return 0
    except Exception as e:
        code = e.args[0] if e.args and isinstance(e.args[0], int) else None
        detail = str(e) if isinstance(e, Stop) else type(e).__name__ + (f' (database code {code})' if code is not None else '')
        print(f'STOP during {stage}: {detail}', file=sys.stderr)
        if mode in ('apply', 'resume'):
            print('Earlier committed staging batches may remain. No rows were deleted or overwritten. Report this result before resuming.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
