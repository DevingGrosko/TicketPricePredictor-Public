from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from tools import tidb_schema_rehearsal as r

FIXTURE = Path(__file__).resolve().parents[1] / 'tools' / 'tidb_staging_schema.sql'


class SchemaRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.content = FIXTURE.read_bytes()
        self.defs = r.parse_definitions(self.content)

    def test_rejects_modified_fixture(self):
        with self.assertRaises(r.RehearsalError):
            r.parse_definitions(self.content + b'\n')

    def test_rejects_extra_statement(self):
        with self.assertRaises(r.RehearsalError):
            r.parse_definitions(self.content + b'DROP DATABASE sys;')

    def test_counts_all_tables_columns_foreign_keys(self):
        self.assertEqual([len(x) for x in self.defs.values()], [7, 6, 6])
        self.assertEqual(sum(len(d.columns) for ds in self.defs.values() for d in ds), 139)
        self.assertEqual(sum(len(d.foreign_keys) for ds in self.defs.values() for d in ds), 6)

    def test_table_names_are_fixed(self):
        for s, ds in self.defs.items():
            self.assertEqual([d.name for d in ds], list(r.EXPECTED[s]))

    def test_exactly_reversible_table_options_change(self):
        for ds in self.defs.values():
            for d in ds:
                self.assertEqual(d.ddl.replace('DEFAULT CHARSET=utf8 COLLATE=utf8_general_ci;', 'DEFAULT CHARSET=utf8mb3;'), d.original)
                self.assertNotIn('DROP ', d.ddl)
                self.assertNotIn('INSERT ', d.ddl)
                self.assertNotIn('IF NOT EXISTS', d.ddl)

    def test_every_table_has_primary_key(self):
        for ds in self.defs.values():
            for d in ds:
                self.assertTrue(any(i[0] == 'PRIMARY' for i in d.indexes))

    def test_parent_tables_precede_children(self):
        for ds in self.defs.values():
            seen = set()
            for d in ds:
                for fk in d.foreign_keys:
                    self.assertIn(fk[2], seen)
                seen.add(d.name)

    def test_no_auto_increment_floor_is_lost(self):
        floors = {(s, d.name): d.auto_increment for s,ds in self.defs.items() for d in ds if d.auto_increment}
        self.assertEqual(floors[('mlb','tickets')], 5759850)
        self.assertEqual(floors[('nfl','nfl_tickets')], 1562357)
        self.assertEqual(floors[('nhl','nhl_tickets')], 143182)
        self.assertEqual(len(floors), 12)

    def test_sql_insert_uses_parameters_not_literal_values(self):
        conn = MagicMock()
        d = self.defs['mlb'][0]
        r.sql_insert(conn, d, {'venue': "not SQL: '); DROP TABLE x;--"})
        sql, values = conn.execute.call_args.args
        self.assertNotIn('DROP', str(sql))
        self.assertEqual(values, {'p0': "not SQL: '); DROP TABLE x;--"})

    def test_sql_insert_rejects_unexpected_column(self):
        with self.assertRaises(r.RehearsalError):
            r.sql_insert(MagicMock(), self.defs['mlb'][0], {'not_a_column': 1})

    def test_probe_rolls_back_on_failure(self):
        c = MagicMock()
        with patch.object(r, 'sql_insert', side_effect=r.RehearsalError('synthetic failure')):
            with self.assertRaises(r.RehearsalError):
                r.probe(c, 'mlb', self.defs['mlb'])
        c.begin.return_value.rollback.assert_called_once()

    def test_synthetic_inserts_use_only_negative_ids(self):
        # The first probe error short circuits after capturing an event insert.
        c = MagicMock()
        with patch.object(r, 'sql_insert', side_effect=r.RehearsalError('stop')) as insert:
            with self.assertRaises(r.RehearsalError):
                r.probe(c, 'nhl', self.defs['nhl'])
        self.assertEqual(insert.call_args.args[2]['id'], -1)
        self.assertEqual(insert.call_args.args[2]['currency'], 'USD')

    def test_required_constraint_rejection_is_not_assumed(self):
        with patch.object(r, 'sql_insert'), self.assertRaises(r.RehearsalError):
            r.must_reject(MagicMock(), self.defs['mlb'][0], {}, 1062)

    def test_int_display_width_normalizes_only_int(self):
        self.assertEqual(r.normal_type('int(11)'), 'int')
        self.assertEqual(r.normal_type('tinyint(1)'), 'tinyint(1)')
        self.assertEqual(r.normal_type('datetime(6)'), 'datetime(6)')

    def test_utf8_alias_does_not_hide_other_collation_changes(self):
        self.assertEqual(r.normal_collation('utf8mb3_general_ci'), 'utf8_general_ci')
        self.assertNotEqual(r.normal_collation('utf8_bin'), 'utf8_general_ci')


if __name__ == '__main__':
    unittest.main()
