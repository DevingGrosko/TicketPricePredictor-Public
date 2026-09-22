"""Copy the pinned, previously audited MLB backup into EMPTY TiDB staging only.

Default is a checksum/schema audit with no database connection. --apply copies
rows but does NOT declare full destination verification. Run the separate
read-only all-field verification after copying. Never resume this command over
populated tables; never delete or overwrite to make it pass.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

HELPER_SHA = '58b40b0ca5a23cc1dcdbc10fae79f7744727262fdf45c5bff62a5ee8168d21c6'
SPORT = 'mlb'
SCHEMA = 'ticketsignal_staging_mlb'
# Exact counts from the checksum-verified source audit, not live SQL estimates.
EXPECTED_ROWS = {
    'analytics_dirty_venue': 11,
    'event': 310,
    'iterations': 39879,
    'section_bucket_summary': 286418,
    'section_summary_state': 303,
    'team_report_summary': 10,
    'tickets': 5735834,
}


def load_helper():
    path = Path(__file__).with_name('tidb_mlb_nfl_import.py')
    if hashlib.sha256(path.read_bytes()).hexdigest() != HELPER_SHA:
        raise RuntimeError('Pinned original helper checksum mismatch.')
    spec = importlib.util.spec_from_file_location('_pinned_mlb_copy_helper', path)
    if spec is None or spec.loader is None:
        raise RuntimeError('Could not load the pinned helper.')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def source_signature(path):
    st = path.stat()
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def prepare_source(helper, path):
    """Verify both complete byte hashes, then reuse the audited source identity.

    This intentionally does not rebuild the million-row SQLite fingerprint
    index: the exact same bytes were already audited, and the destination must
    be empty. Constraint checks remain on during copying. A later all-field
    readback remains mandatory rather than claiming verification here.
    """
    signature = source_signature(path)
    helper.verify_gzip(path, SPORT)
    tables = []
    for kind, value in helper.read_dump(path, SPORT):
        if kind == 'table':
            tables.append(helper.parse_table(value))
            print('Source definitions checked: ' + tables[-1].name, flush=True)
    spec = helper.spec_for(SPORT)
    helper.require(tuple(t.name for t in tables) == tuple(EXPECTED_ROWS) == spec['tables'],
                   'Unexpected MLB source table sequence.')
    helper.require(sum(len(t.columns) for t in tables) == spec['column_count'],
                   'Unexpected MLB source columns.')
    ddl = '\n\n'.join(t.original for t in tables) + '\n'
    helper.require(hashlib.sha256(ddl.encode()).hexdigest() == spec['ddl_sha'],
                   'Source definitions differ from the audited snapshot.')
    helper.require(source_signature(path) == signature, 'Source changed during validation.')
    print('VERIFIED: both MLB source checksums and the complete reviewed schema.', flush=True)
    return tables, signature


def close_quietly(connection):
    if connection is not None:
        try:
            connection.close()
        except Exception:
            # Closing a dead socket must not replace the actual failure.
            pass


class CopySession:
    """Renew only BETWEEN committed batches. Never retry an INSERT or COMMIT."""
    def __init__(self, helper, tables, clock=time.monotonic):
        self.helper, self.tables, self.clock = helper, tables, clock
        self.connection = None
        self.last_activity = 0.0

    def open(self):
        close_quietly(self.connection)
        self.connection = None
        c = self.helper.connect_staging(SPORT)
        try:
            self.helper.check_target(c, SPORT, self.tables)
            with c.cursor() as cursor:
                cursor.execute("SET SESSION sql_mode='STRICT_TRANS_TABLES,NO_AUTO_VALUE_ON_ZERO,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION'")
                cursor.execute("SET SESSION time_zone='+00:00'")
                cursor.execute("SET SESSION tidb_txn_mode='pessimistic'")
            c.commit()
        except BaseException:
            close_quietly(c)
            raise
        self.connection = c
        self.last_activity = self.clock()

    def require_empty(self):
        self.open()
        # Check EVERY destination before the first INSERT.
        for table in self.tables:
            found = self.helper.query(self.connection, 'SELECT 1 FROM `' + table.name + '` LIMIT 1')
            self.helper.require(not found, 'MLB staging is not empty. Stop for verification; do not delete or rerun the old resume command.')
        self.connection.rollback()
        # Source processing starts with no open database connection.
        self.close()

    def insert(self, table, rows):
        if self.connection is None or self.clock() - self.last_activity >= 15:
            self.open()
        # A disconnect during this operation is reported without replay. The
        # original helper rolls back when possible; committed batches remain.
        self.helper.insert_batch(self.connection, table, rows)
        self.last_activity = self.clock()

    def close(self):
        close_quietly(self.connection)
        self.connection = None


def copy_rows(helper, path, tables, signature, session):
    helper.require(source_signature(path) == signature, 'Source changed before copying.')
    helper.require(tuple(t.name for t in tables) == tuple(EXPECTED_ROWS), 'Unexpected copy targets.')
    counts = {name: 0 for name in EXPECTED_ROWS}
    by_name = {t.name: t for t in tables}
    session.require_empty()
    print('PASS: all seven MLB staging tables are empty. Starting copy.', flush=True)
    last_name = None
    last_progress = time.monotonic()
    next_progress = 100000
    try:
        for kind, value in helper.read_dump(path, SPORT):
            if kind == 'table':
                table = helper.parse_table(value)
                helper.require(table.name in by_name and table.original == by_name[table.name].original,
                               'Source schema changed before copying.')
                continue
            name, values = value
            helper.require(name in by_name, 'Unexpected source table.')
            table = by_name[name]
            if name != last_name:
                if last_name is not None:
                    helper.require(counts[last_name] == EXPECTED_ROWS[last_name], 'Copied count mismatch: ' + last_name)
                    print('COPIED ' + last_name + ': ' + format(counts[last_name], ',') + ' rows', flush=True)
                session.close()
                print('Copying ' + name + '...', flush=True)
                last_name = name
                next_progress = 100000
            typed = (helper.typed_row(table, raw) for raw in helper.literal_rows(values))
            for rows in helper.row_batches(typed):
                helper.require(counts[name] + len(rows) <= EXPECTED_ROWS[name], 'Too many rows in source: ' + name)
                session.insert(table, rows)
                counts[name] += len(rows)
                now = time.monotonic()
                if counts[name] >= next_progress or now - last_progress >= 15:
                    print(name + ': ' + format(counts[name], ',') + '/' + format(EXPECTED_ROWS[name], ',') + ' rows committed', flush=True)
                    next_progress = (counts[name] // 100000 + 1) * 100000
                    last_progress = now
        # read_dump has now checked the complete decoded hash again.
        helper.require(counts == EXPECTED_ROWS, 'Copied row counts differ from the audited snapshot.')
        helper.require(source_signature(path) == signature, 'Source changed during copying.')
        if last_name is not None:
            print('COPIED ' + last_name + ': ' + format(counts[last_name], ',') + ' rows', flush=True)
        return counts
    finally:
        session.close()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--report', type=Path)
    a = p.parse_args(argv)
    helper = None
    added_password = False
    try:
        helper = load_helper()
        helper.require(helper.schema_for(SPORT) == SCHEMA, 'Unexpected destination configuration.')
        if a.report:
            helper.require(a.report.parent.is_dir() and not a.report.exists(), 'Report requires a new file in an existing folder.')
        print('Checking the pinned MLB backup locally; no database connection.', flush=True)
        tables, signature = prepare_source(helper, a.source)
        if not a.apply:
            print('PASS: source byte hashes/schema checked only. No database connection.', flush=True)
            return 0
        # Cache once in this process only, so renewal does not ask again. The
        # password is never written into files, command arguments, or reports.
        if not os.getenv('TIDB_STAGING_PASSWORD'):
            password = getpass.getpass('TiDB STAGING password (not PythonAnywhere): ')
            helper.require(bool(password), 'Empty staging password.')
            os.environ['TIDB_STAGING_PASSWORD'] = password
            added_password = True
        print('Destination: TiDB / ticketsignal_staging_mlb ONLY. No production, NFL or NHL access.', flush=True)
        session = CopySession(helper, tables)
        try:
            counts = copy_rows(helper, a.source, tables, signature, session)
        finally:
            session.close()
        result = {'sport': SPORT, 'schema': SCHEMA, 'mode': 'initial-copy',
                  'snapshot_sha256': helper.spec_for(SPORT)['gzip_sha'],
                  'tables': counts, 'rows_inserted_this_run': sum(counts.values()),
                  'copy_completed': True, 'target_full_comparison_passed': False,
                  'next_step': 'Independent read-only all-field verification is required.'}
        if a.report:
            with a.report.open('x') as stream:
                json.dump(result, stream, indent=2)
                stream.write('\n')
        print('COPY COMPLETE: MLB rows committed. Full destination verification is still required.', flush=True)
        return 0
    except Exception as error:
        code = error.args[0] if error.args and isinstance(error.args[0], int) else None
        detail = str(error) if helper is not None and isinstance(error, helper.Stop) else type(error).__name__
        print('STOP: ' + detail + (f' (database code {code})' if code is not None else ''), file=sys.stderr)
        print('Earlier staging batches may remain. No rows were deleted or overwritten. Do not rerun; send this result.', file=sys.stderr)
        return 1
    finally:
        if added_password:
            os.environ.pop('TIDB_STAGING_PASSWORD', None)


if __name__ == '__main__':
    raise SystemExit(main())
