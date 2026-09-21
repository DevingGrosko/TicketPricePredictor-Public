import unittest
from tools.tidb_schema_rehearsal import normal_index, declared_auto_increment, RehearsalError


class IndexMetadataTests(unittest.TestCase):
    def test_tidb_text_flag_matches_mysql_numeric_flag(self):
        self.assertEqual(normal_index(('PRIMARY', '0', 1, 'venue')), ('PRIMARY', 0, 1, 'venue'))
        self.assertEqual(normal_index(('secondary', '1', 2, 'updated_at')), ('secondary', 1, 2, 'updated_at'))

    def test_unique_and_nonunique_remain_distinct(self):
        self.assertNotEqual(normal_index(('k', '0', 1, 'x')), normal_index(('k', '1', 1, 'x')))

    def test_invalid_flag_is_rejected(self):
        with self.assertRaises(ValueError):
            normal_index(('k', 'unknown', 1, 'x'))


class AutoIncrementMetadataTests(unittest.TestCase):
    def test_reads_tidb_table_option_not_column_attribute(self):
        ddl = "CREATE TABLE `event` (\n  `id` int NOT NULL AUTO_INCREMENT\n) ENGINE=InnoDB DEFAULT CHARSET=utf8 COLLATE=utf8_general_ci AUTO_INCREMENT=313"
        self.assertEqual(declared_auto_increment(ddl), 313)

    def test_reordered_options_preserve_the_base(self):
        self.assertEqual(declared_auto_increment("CREATE TABLE `x` (\n `id` int\n) ENGINE=InnoDB AUTO_INCREMENT=5759850 DEFAULT CHARSET=utf8"), 5759850)

    def test_missing_table_option_is_rejected(self):
        with self.assertRaises(RehearsalError):
            declared_auto_increment("CREATE TABLE `x` (\n `id` int AUTO_INCREMENT\n) ENGINE=InnoDB DEFAULT CHARSET=utf8")

    def test_does_not_read_an_auto_increment_number_from_a_column_default(self):
        with self.assertRaises(RehearsalError):
            declared_auto_increment("CREATE TABLE `x` (\n `note` varchar(30) DEFAULT 'AUTO_INCREMENT=999'\n) ENGINE=InnoDB")
