"""Synthetic orchestration tests: no credentials or network connections."""
import contextlib
import hashlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

try:
    from tools import tidb_mlb_initial_copy as copy
except ImportError:
    import tidb_mlb_initial_copy as copy


class LocalStop(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise LocalStop(message)


def fake_helper():
    return SimpleNamespace(require=require, Stop=LocalStop,
                           connect_staging=MagicMock(), check_target=MagicMock(),
                           query=MagicMock(return_value=()), insert_batch=MagicMock())


class SessionTests(unittest.TestCase):
    def test_all_empty_checks_precede_first_insert_and_connection_closes(self):
        h = fake_helper()
        tables = [SimpleNamespace(name='event'), SimpleNamespace(name='tickets')]
        c = h.connect_staging.return_value
        session = copy.CopySession(h, tables, clock=lambda: 0)
        session.require_empty()
        self.assertEqual(h.query.call_count, 2)
        self.assertTrue(all(call.args[1].startswith('SELECT 1 FROM `') for call in h.query.call_args_list))
        h.insert_batch.assert_not_called()
        c.close.assert_called_once()
        self.assertIsNone(session.connection)
        h.connect_staging.assert_called_once_with('mlb')

    def test_nonempty_destination_refused_without_insert(self):
        h = fake_helper()
        h.query.return_value = ((1,),)
        session = copy.CopySession(h, [SimpleNamespace(name='event')])
        try:
            with self.assertRaisesRegex(LocalStop, 'not empty'):
                session.require_empty()
        finally:
            session.close()
        h.insert_batch.assert_not_called()

    def test_renewal_only_between_batches(self):
        h = fake_helper()
        c1, c2 = MagicMock(), MagicMock()
        h.connect_staging.side_effect = [c1, c2]
        now = [0]
        session = copy.CopySession(h, [], clock=lambda: now[0])
        session.insert('table', [(1,)])
        now[0] = 1
        session.insert('table', [(2,)])
        now[0] = 17
        session.insert('table', [(3,)])
        self.assertEqual(h.connect_staging.call_count, 2)
        c1.close.assert_called_once()
        self.assertEqual([x.args[0] for x in h.insert_batch.call_args_list], [c1, c1, c2])
        session.close()

    def test_ambiguous_write_is_not_retried(self):
        h = fake_helper()
        h.insert_batch.side_effect = ConnectionError('synthetic lost commit response')
        session = copy.CopySession(h, [])
        try:
            with self.assertRaises(ConnectionError):
                session.insert('table', [(1,)])
        finally:
            session.close()
        h.insert_batch.assert_called_once()
        h.connect_staging.assert_called_once()

    def test_full_target_preflight_runs_on_each_connection(self):
        h = fake_helper()
        session = copy.CopySession(h, ['reviewed-tables'])
        session.open()
        session.close()
        session.open()
        self.assertEqual(h.check_target.call_count, 2)
        for c in h.check_target.call_args_list:
            self.assertEqual(c.args[1:], ('mlb', ['reviewed-tables']))
        session.close()

    def test_failed_preflight_closes_without_writes(self):
        h = fake_helper()
        h.check_target.side_effect = LocalStop('wrong target')
        session = copy.CopySession(h, [])
        with self.assertRaises(LocalStop):
            session.open()
        h.insert_batch.assert_not_called()
        h.connect_staging.return_value.close.assert_called_once()
        self.assertIsNone(session.connection)

    def test_cleanup_does_not_replace_original_error(self):
        h = fake_helper()
        h.check_target.side_effect = LocalStop('original failure')
        h.connect_staging.return_value.close.side_effect = ConnectionError('cleanup')
        with self.assertRaisesRegex(LocalStop, 'original failure'):
            copy.CopySession(h, []).open()


class CopyTests(unittest.TestCase):
    def setup_copy(self):
        h = fake_helper()
        table = SimpleNamespace(name='tickets', original='reviewed')
        h.parse_table = MagicMock(return_value=table)
        h.read_dump = MagicMock(return_value=iter([('table', 'ddl'), ('rows', ('tickets', 'literal-data'))]))
        h.literal_rows = MagicMock(return_value=iter([(1,), (2,)]))
        h.typed_row = lambda table, row: row
        h.row_batches = lambda rows: ([r] for r in rows)
        return h, table, MagicMock()

    def test_checks_emptiness_before_each_source_insert_and_copies_once(self):
        h, table, session = self.setup_copy()
        events = []
        session.require_empty.side_effect = lambda: events.append('empty')
        session.insert.side_effect = lambda *_: events.append('insert')
        with patch.object(copy, 'EXPECTED_ROWS', {'tickets': 2}), patch.object(copy, 'source_signature', return_value='sig'), contextlib.redirect_stdout(io.StringIO()):
            counts = copy.copy_rows(h, Path('unused'), [table], 'sig', session)
        self.assertEqual(events, ['empty', 'insert', 'insert'])
        self.assertEqual(counts, {'tickets': 2})
        session.close.assert_called()

    def test_failure_closes_and_never_replays_batch(self):
        h, table, session = self.setup_copy()
        session.insert.side_effect = ConnectionError('write failed')
        with patch.object(copy, 'EXPECTED_ROWS', {'tickets': 2}), patch.object(copy, 'source_signature', return_value='sig'), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ConnectionError):
            copy.copy_rows(h, Path('unused'), [table], 'sig', session)
        session.insert.assert_called_once()
        session.close.assert_called()

    def test_excess_rows_rejected_before_excess_insert(self):
        h, table, session = self.setup_copy()
        with patch.object(copy, 'EXPECTED_ROWS', {'tickets': 1}), patch.object(copy, 'source_signature', return_value='sig'), contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(LocalStop, 'Too many'):
            copy.copy_rows(h, Path('unused'), [table], 'sig', session)
        session.insert.assert_called_once()

    def test_missing_rows_do_not_report_completion(self):
        h, table, session = self.setup_copy()
        with patch.object(copy, 'EXPECTED_ROWS', {'tickets': 3}), patch.object(copy, 'source_signature', return_value='sig'), contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(LocalStop, 'counts differ'):
            copy.copy_rows(h, Path('unused'), [table], 'sig', session)

    def test_source_change_before_copy_stops_before_connection(self):
        h, table, session = self.setup_copy()
        with patch.object(copy, 'source_signature', return_value='changed'), self.assertRaisesRegex(LocalStop, 'Source changed'):
            copy.copy_rows(h, Path('unused'), [table], 'original', session)
        session.require_empty.assert_not_called()
        session.insert.assert_not_called()

    def test_source_schema_change_stops_before_insert(self):
        h, table, session = self.setup_copy()
        h.parse_table.return_value = SimpleNamespace(name='tickets', original='changed')
        with patch.object(copy, 'EXPECTED_ROWS', {'tickets': 2}), patch.object(copy, 'source_signature', return_value='sig'), contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(LocalStop, 'schema changed'):
            copy.copy_rows(h, Path('unused'), [table], 'sig', session)
        session.insert.assert_not_called()


class EntryTests(unittest.TestCase):
    def test_source_preflight_finishes_checksum_generator(self):
        h = fake_helper()
        table = SimpleNamespace(name='tickets', original='source ddl', columns=[1])
        h.verify_gzip = MagicMock()
        h.parse_table = lambda _: table
        h.spec_for = lambda _: {'tables': ('tickets',), 'column_count': 1,
                               'ddl_sha': hashlib.sha256(b'source ddl\n').hexdigest()}
        reached_end = []
        def stream(*_):
            yield 'table', 'source ddl'
            yield 'rows', ('tickets', 'not executed')
            reached_end.append(True)
        h.read_dump = stream
        with patch.object(copy, 'EXPECTED_ROWS', {'tickets': 2}), patch.object(copy, 'source_signature', return_value='sig'), contextlib.redirect_stdout(io.StringIO()):
            tables, sig = copy.prepare_source(h, Path('unused'))
        self.assertEqual(reached_end, [True])
        h.connect_staging.assert_not_called()
        self.assertEqual(tables, [table])

    def test_default_audit_does_not_connect_or_request_password(self):
        h = fake_helper()
        h.schema_for = lambda _: copy.SCHEMA
        with patch.object(copy, 'load_helper', return_value=h), patch.object(copy, 'prepare_source', return_value=([], 'sig')), patch.object(copy.getpass, 'getpass') as password, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(copy.main(['--source', '/unused']), 0)
        h.connect_staging.assert_not_called()
        password.assert_not_called()

    def test_report_distinguishes_copy_from_full_verification(self):
        h = fake_helper()
        h.schema_for = lambda _: copy.SCHEMA
        h.spec_for = lambda _: {'gzip_sha': 'source hash'}
        with tempfile.TemporaryDirectory() as root:
            report = Path(root) / 'copy.json'
            with patch.object(copy, 'load_helper', return_value=h), patch.object(copy, 'prepare_source', return_value=([], 'sig')), patch.object(copy, 'copy_rows', return_value={'tickets': 2}), patch.dict(copy.os.environ, {'TIDB_STAGING_PASSWORD': 'synthetic-test-only'}), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(copy.main(['--source', '/unused', '--apply', '--report', str(report)]), 0)
            result = copy.json.loads(report.read_text())
            self.assertTrue(result['copy_completed'])
            self.assertFalse(result['target_full_comparison_passed'])
            self.assertEqual(result['rows_inserted_this_run'], 2)

    def test_error_output_omits_raw_driver_error(self):
        h = fake_helper()
        h.schema_for = lambda _: copy.SCHEMA
        stream = io.StringIO()
        with patch.object(copy, 'load_helper', return_value=h), patch.object(copy, 'prepare_source', side_effect=ValueError('sensitive row or password')), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stream):
            self.assertEqual(copy.main(['--source', '/unused']), 1)
        self.assertNotIn('sensitive row', stream.getvalue())
        self.assertIn('STOP: ValueError', stream.getvalue())


if __name__ == '__main__':
    unittest.main()
