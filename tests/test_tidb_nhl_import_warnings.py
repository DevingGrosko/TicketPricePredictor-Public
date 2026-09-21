"""Regression coverage for TiDB's SHOW WARNINGS syntax; synthetic values only."""
import unittest
from unittest.mock import MagicMock

from tools import tidb_nhl_import as imp


def fixture():
    return imp.Table('analytics_dirty_venue', [
        ('venue', 'varchar(300)', 'NO', '', 'utf8_general_ci'),
        ('revision', 'int', 'NO', '', ''),
        ('dirty', 'tinyint(1)', 'NO', '', ''),
        ('updated_at', 'datetime(6)', 'NO', '', ''),
    ], [('PRIMARY', 0, 1, 'venue')], [], None, [])


class WarningSyntaxTests(unittest.TestCase):
    def connection(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.executemany.return_value = 1
        cursor.fetchone.return_value = None
        # Do not let a permissive mock accept unsupported SQL again.
        def supported_statement(sql):
            self.assertEqual(sql, 'SHOW WARNINGS')
        cursor.execute.side_effect = supported_statement
        return connection, cursor

    def test_uses_plain_show_warnings_before_commit(self):
        connection, cursor = self.connection()
        imp.insert_batch(connection, fixture(), [('synthetic', 1, 0, '2026-09-20 00:00:00')])
        cursor.execute.assert_called_once_with('SHOW WARNINGS')
        cursor.fetchone.assert_called_once_with()
        connection.commit.assert_called_once_with()
        connection.rollback.assert_not_called()

    def test_supported_statement_does_not_suppress_conversion_warning(self):
        connection, cursor = self.connection()
        cursor.fetchone.return_value = ('Warning', 1264, 'synthetic warning')
        with self.assertRaisesRegex(imp.Stop, 'Server warning'):
            imp.insert_batch(connection, fixture(), [('synthetic', 1, 0, '2026-09-20 00:00:00')])
        connection.rollback.assert_called_once_with()
        connection.commit.assert_not_called()

    def test_warning_query_failure_still_rolls_back(self):
        connection, cursor = self.connection()
        cursor.execute.side_effect = RuntimeError('synthetic driver failure')
        with self.assertRaises(RuntimeError):
            imp.insert_batch(connection, fixture(), [('synthetic', 1, 0, '2026-09-20 00:00:00')])
        connection.rollback.assert_called_once_with()
        connection.commit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
