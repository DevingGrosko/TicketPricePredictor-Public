import unittest
from tools.tidb_schema_rehearsal import normal_index


class IndexMetadataTests(unittest.TestCase):
    def test_tidb_text_flag_matches_mysql_numeric_flag(self):
        self.assertEqual(normal_index(('PRIMARY', '0', 1, 'venue')), ('PRIMARY', 0, 1, 'venue'))
        self.assertEqual(normal_index(('secondary', '1', 2, 'updated_at')), ('secondary', 1, 2, 'updated_at'))

    def test_unique_and_nonunique_remain_distinct(self):
        self.assertNotEqual(normal_index(('k', '0', 1, 'x')), normal_index(('k', '1', 1, 'x')))

    def test_invalid_flag_is_rejected(self):
        with self.assertRaises(ValueError):
            normal_index(('k', 'unknown', 1, 'x'))
