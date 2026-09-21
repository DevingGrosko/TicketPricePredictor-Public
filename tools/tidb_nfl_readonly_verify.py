"""Read-only NFL snapshot verification; never call an import or resume path.

Uses a small checksum-pinned manifest independently recomputed from the uploaded
source backup. Reads each target table once, closes that connection, then hashes
all fields locally. Designed for a GitHub runner, not a long-lived PA console.
No raw history, source credentials, DDL, INSERT, UPDATE, DELETE, or deployment.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time
from datetime import datetime, timezone

from tools import tidb_mlb_nfl_import as imp

HELPER_SHA256 = '58b40b0ca5a23cc1dcdbc10fae79f7744727262fdf45c5bff62a5ee8168d21c6'
MANIFEST_SHA256 = '05201ae0f497585e526fc2288e0d4bdff5d0f72f2a9ba5f53141786c6b7678af'
MANIFEST_PATH = Path(__file__).with_name('tidb_nfl_expected_snapshot.json')
SCHEMA = 'ticketsignal_staging_nfl'
COUNTS = {'analytics_dirty_venue': 30, 'nfl_event': 90, 'nfl_iterations': 10367,
          'nfl_tickets': 1562356, 'section_bucket_summary': 56466,
          'section_summary_state': 90}


def require(value, message):
    imp.require(value, message)


class ReadOnlyCursor:
    """Defense in depth: deny writes even through a reused metadata helper."""
    def __init__(self, cursor):
        self.cursor = cursor

    def __enter__(self):
        self.cursor.__enter__()
        return self

    def __exit__(self, *args):
        return self.cursor.__exit__(*args)

    def execute(self, sql, args=None):
        require(isinstance(sql, str) and ';' not in sql and
                bool(re.match(r'\A(?:SELECT|SHOW)\s', sql)),
                'Read-only verifier rejected a non-read SQL statement.')
        return self.cursor.execute(sql, args)

    def fetchall(self):
        return self.cursor.fetchall()


class ReadOnlyConnection:
    def __init__(self, connection):
        self.connection = connection

    def cursor(self):
        return ReadOnlyCursor(self.connection.cursor())

    def rollback(self):
        return self.connection.rollback()

    def close(self):
        return self.connection.close()


def close_readonly(connection):
    # No remote writes are possible here. Do not let failed cleanup replace the
    # original read error (the old importer's finally/rollback could do that).
    try:
        connection.close()
    except Exception:
        print('NOTE: closing the read-only connection failed; no writes were issued.', flush=True)


def connect():
    return ReadOnlyConnection(imp.connect_staging('nfl'))


def load_manifest():
    require(hashlib.sha256(Path(imp.__file__).read_bytes()).hexdigest() == HELPER_SHA256,
            'Pinned canonicalization helper changed; review required.')
    raw = MANIFEST_PATH.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == MANIFEST_SHA256,
            'Expected-snapshot manifest checksum mismatch.')
    manifest = json.loads(raw)
    spec = imp.spec_for('nfl')
    require(manifest['sport'] == 'nfl' and manifest['schema'] == SCHEMA,
            'Manifest must target NFL staging only.')
    require(manifest['gzip_sha256'] == spec['gzip_sha'] and
            manifest['sql_sha256'] == spec['sql_sha'] and
            manifest['ddl_sha256'] == spec['ddl_sha'], 'Source fingerprint mismatch.')
    entries = manifest['tables']
    require(tuple(e['name'] for e in entries) == tuple(COUNTS) == spec['tables'],
            'Unexpected manifest tables or order.')
    tables = []
    for e in entries:
        require(type(e['rows']) is int and e['rows'] == COUNTS[e['name']],
                'Unexpected expected row count.')
        require(bool(re.fullmatch('[0-9a-f]{64}', e['canonical_sha256'])),
                'Invalid expected table fingerprint.')
        table = imp.parse_table(e['ddl'])
        require(table.name == e['name'], 'Manifest table definition mismatch.')
        table.count = e['rows']
        tables.append(table)
    ddl = '\n\n'.join(t.original for t in tables) + '\n'
    require(hashlib.sha256(ddl.encode()).hexdigest() == spec['ddl_sha'],
            'Expected table definitions changed.')
    require(manifest['total_rows'] == sum(COUNTS.values()), 'Unexpected snapshot size.')
    return manifest, tables


def table_digest(table, rows):
    """Same field encoding and bytewise primary-key order as the source index."""
    require(len(rows) == table.count, 'Row-count mismatch: ' + table.name)
    entries = []
    last_progress = time.monotonic()
    for i, raw in enumerate(rows, 1):
        row = imp.typed_row(table, raw)
        entries.append((table.key(row), imp.row_fingerprint(table, row)))
        now = time.monotonic()
        if i % 100000 == 0 or now - last_progress >= 30:
            print(f'Hashing {table.name}: {i:,}/{table.count:,} rows', flush=True)
            last_progress = now
    entries.sort(key=lambda e: e[0])
    digest = hashlib.sha256()
    previous = None
    for key, fingerprint in entries:
        require(key != previous, 'Duplicate destination primary key: ' + table.name)
        digest.update(fingerprint)
        previous = key
    return digest.hexdigest()


def fetch_table(table):
    require(table.name in COUNTS and table.count == COUNTS[table.name],
            'Unreviewed target table or size.')
    connection = connect()
    try:
        selected, version, fk = imp.query(connection, 'SELECT DATABASE(),VERSION(),@@foreign_key_checks')[0]
        require(selected == SCHEMA and 'tidb' in str(version).lower() and int(fk) == 1,
                'Wrong destination or disabled constraints.')
        fields = ','.join('`' + c[0] + '`' for c in table.columns)
        # Buffer the bounded result quickly, then close BEFORE CPU-heavy hashing.
        # An extra row makes unexpected growth fail rather than pass as a subset.
        sql = f'SELECT {fields} FROM `{SCHEMA}`.`{table.name}` LIMIT {table.count + 1}'
        return imp.query(connection, sql)
    finally:
        close_readonly(connection)


def verify():
    manifest, tables = load_manifest()
    started = datetime.now(timezone.utc).isoformat()
    connection = connect()
    try:
        imp.check_target(connection, 'nfl', tables)
        print('PASS: full NFL staging schema preflight. No writes permitted.', flush=True)
    finally:
        close_readonly(connection)
    result = {'sport': 'nfl', 'schema': SCHEMA, 'mode': 'read-only',
              'snapshot_sha256': manifest['gzip_sha256'],
              'source_manifest_sha256': MANIFEST_SHA256,
              'started_utc': started, 'tables': {}, 'rows_written': 0}
    for table, expected in zip(tables, manifest['tables']):
        print(f'Reading {table.name}: expecting {table.count:,} rows', flush=True)
        rows = fetch_table(table)
        digest = table_digest(table, rows)
        del rows
        require(digest == expected['canonical_sha256'], 'All-field fingerprint mismatch: ' + table.name)
        result['tables'][table.name] = {'rows': table.count, 'canonical_sha256': digest}
        print(f'PASS {table.name}: {table.count:,} rows; complete all-field fingerprint matched.', flush=True)
    result.update(total_rows=sum(t.count for t in tables), target_full_comparison_passed=True,
                  finished_utc=datetime.now(timezone.utc).isoformat())
    print('VERIFICATION_REPORT ' + json.dumps(result, sort_keys=True), flush=True)
    print('PASS: NFL exported snapshot independently verified; zero writes. No PythonAnywhere, NHL, MLB, import or deployment access.', flush=True)
    return result


def main():
    try:
        verify()
        return 0
    except Exception as error:
        code = error.args[0] if error.args and isinstance(error.args[0], int) else None
        detail = str(error) if isinstance(error, imp.Stop) else type(error).__name__
        print(f'STOP: read-only verification incomplete: {detail}; database_code={code}', flush=True)
        print('No imports, retries of writes, deletes or overwrites were attempted.', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
