"""Synthetic-only tests; no credentials or live databases are used."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tools import tidb_mlb_readonly_verify as v
from tools import tidb_mlb_nfl_import as imp


def table():
    return imp.Table('tickets', [('id', 'int', 'NO', '', ''),
        ('section', 'varchar(300)', 'NO', '', 'utf8_general_ci'),
        ('price', 'int', 'NO', '', '')], [('PRIMARY', 0, 1, 'id')], [], None, count=2)


class ReadOnlyVerifierTests(unittest.TestCase):
    def test_pinned_manifest_and_original_helper_load(self):
        manifest, tables = v.load_manifest()
        self.assertEqual(sum(t.count for t in tables), 6062765)
        self.assertEqual(sum(len(t.columns) for t in tables), 41)
        self.assertEqual([t.name for t in tables], list(v.COUNTS))
        self.assertEqual(manifest['schema'], 'ticketsignal_staging_mlb')

    def test_modified_manifest_stops(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'bad.json'
            path.write_text('{}')
            with patch.object(v, 'MANIFEST_PATH', path), self.assertRaises(imp.Stop):
                v.load_manifest()

    def test_digest_matches_original_disk_fingerprint_and_order(self):
        t = table()
        rows = [(2, 'Café', 125), (10, 'Section 10', 200)]
        with tempfile.TemporaryDirectory() as root:
            index = imp.DiskIndex(Path(root) / 'test.sqlite')
            try:
                index.add_table(t)
                index.add_rows(t, rows)
                index.db.commit()
                self.assertEqual(v.table_digest(t, rows), index.fingerprint(t))
                self.assertEqual(v.table_digest(t, rows[::-1]), index.fingerprint(t))
            finally:
                index.close()

    def test_changed_values_do_not_match(self):
        t = table()
        rows = [(1, 'Café', 125), (2, 'Other', 200)]
        original = v.table_digest(t, rows)
        self.assertNotEqual(original, v.table_digest(t, [(1, 'Café', 126), rows[1]]))
        self.assertNotEqual(original, v.table_digest(t, [(1, 'CAFE', 125), rows[1]]))

    def test_missing_extra_and_duplicate_rows_fail(self):
        for rows in [[(1, 'a', 1)], [(1, 'a', 1)] * 3, [(1, 'a', 1)] * 2]:
            with self.subTest(rows=rows), self.assertRaises(imp.Stop):
                v.table_digest(table(), rows)

    def test_proxy_rejects_writes_and_side_effect_reads(self):
        raw = MagicMock()
        cursor = v.ReadOnlyCursor(raw)
        for sql in ['INSERT INTO x VALUES (1)', 'DELETE FROM x', 'DROP TABLE x',
                    'UPDATE x SET a=1', 'ALTER TABLE x ADD a INT', 'TRUNCATE x',
                    'SET GLOBAL x=1', 'SELECT 1; DELETE FROM x',
                    "SELECT 1 INTO OUTFILE '/tmp/x'", 'SELECT * FROM x FOR UPDATE',
                    'SELECT @x:=1', 'SELECT SLEEP(99)', "SELECT GET_LOCK('x',99)"]:
            with self.subTest(sql=sql), self.assertRaises(imp.Stop):
                cursor.execute(sql)
        raw.execute.assert_not_called()

    def test_proxy_permits_required_read_statements(self):
        raw = MagicMock()
        cursor = v.ReadOnlyCursor(raw)
        cursor.execute('SELECT DATABASE()')
        cursor.execute('SHOW CREATE TABLE `event`')
        self.assertEqual(raw.execute.call_count, 2)
        self.assertFalse(hasattr(v.ReadOnlyConnection(raw), 'commit'))

    def test_connect_only_requests_mlb(self):
        with patch.object(imp, 'connect_staging') as connect:
            result = v.connect()
        connect.assert_called_once_with('mlb')
        self.assertIsInstance(result, v.ReadOnlyConnection)

    def test_fetch_closes_before_return_and_bounds_extra_rows(self):
        c = MagicMock()
        rows = [(1, 'a', 1), (2, 'b', 2)]
        with patch.object(v, 'COUNTS', {'tickets': 2}), patch.object(v, 'connect', return_value=c), \
             patch.object(imp, 'query', side_effect=[[(v.SCHEMA, '8.0-TiDB', 1)], rows]) as query:
            self.assertEqual(v.fetch_table(table()), rows)
        c.close.assert_called_once()
        self.assertIn('`ticketsignal_staging_mlb`.`tickets` LIMIT 3', query.call_args.args[1])

    def test_unreviewed_target_rejected_before_connect(self):
        t = table()
        t.name = 'nfl_tickets'
        with patch.object(v, 'connect') as connect, self.assertRaises(imp.Stop):
            v.fetch_table(t)
        connect.assert_not_called()

    def test_read_failure_not_masked_by_cleanup(self):
        c = MagicMock()
        c.close.side_effect = RuntimeError('cleanup failure')
        original = RuntimeError('original read failure')
        with patch.object(v, 'COUNTS', {'tickets': 2}), patch.object(v, 'connect', return_value=c), \
             patch.object(imp, 'query', side_effect=original), patch('sys.stdout', new=io.StringIO()):
            with self.assertRaises(RuntimeError) as caught:
                v.fetch_table(table())
        self.assertIs(caught.exception, original)

    def test_wrong_database_fails_before_reading_rows(self):
        c = MagicMock()
        with patch.object(v, 'COUNTS', {'tickets': 2}), patch.object(v, 'connect', return_value=c), \
             patch.object(imp, 'query', return_value=[('production', '8.0-TiDB', 1)]) as query:
            with self.assertRaises(imp.Stop):
                v.fetch_table(table())
        self.assertEqual(query.call_count, 1)
        c.close.assert_called_once()

    def test_readonly_verification_never_calls_import(self):
        t = table()
        rows = [(1, 'a', 1), (2, 'b', 2)]
        manifest = {'gzip_sha256': 'test', 'tables': [{'canonical_sha256': v.table_digest(t, rows)}]}
        with patch.object(v, 'load_manifest', return_value=(manifest, [t])), \
             patch.object(v, 'connect'), patch.object(imp, 'check_target'), \
             patch.object(v, 'fetch_table', return_value=rows), \
             patch.object(imp, 'run_import') as imports, patch.object(imp, 'insert_batch') as writes, \
             patch('sys.stdout', new=io.StringIO()):
            result = v.verify()
        self.assertTrue(result['target_full_comparison_passed'])
        self.assertEqual(result['rows_written'], 0)
        imports.assert_not_called()
        writes.assert_not_called()

    def test_fingerprint_mismatch_does_not_report_completion(self):
        t = table()
        manifest = {'gzip_sha256': 'test', 'tables': [{'canonical_sha256': '0' * 64}]}
        with patch.object(v, 'load_manifest', return_value=(manifest, [t])), \
             patch.object(v, 'connect'), patch.object(imp, 'check_target'), \
             patch.object(v, 'fetch_table', return_value=[(1, 'a', 1), (2, 'b', 2)]), \
             patch('sys.stdout', new=io.StringIO()), self.assertRaises(imp.Stop):
            v.verify()

    def test_failure_does_not_report_pass_or_leak_raw_error(self):
        output = io.StringIO()
        with patch.object(v, 'verify', side_effect=RuntimeError('private raw details')), patch('sys.stdout', new=output):
            self.assertEqual(v.main(), 1)
        self.assertNotIn('private raw details', output.getvalue())
        self.assertNotIn('PASS:', output.getvalue())


if __name__ == '__main__':
    unittest.main()
