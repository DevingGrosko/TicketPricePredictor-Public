"""Disposable CI MySQL tests with synthetic values only, never TiDB or exports."""
from datetime import datetime
import os
import unittest
from tools import tidb_nhl_import as imp


@unittest.skipUnless(os.getenv('NHL_IMPORT_TEST_MYSQL') == '1', 'Requires disposable CI MySQL')
class DriverTests(unittest.TestCase):
    def setUp(self):
        import pymysql
        self.c = pymysql.connect(host='127.0.0.1', port=3306, user='root',
            password='ephemeral-test-only', database='importer_test', charset='utf8mb4',
            autocommit=False, read_default_file=None)

    def tearDown(self):
        self.c.close()

    def test_native_mysql_literal_decoding(self):
        literals = [r"'Café l\'arène'", r"'comma,(), semi;colon'", r"'\\ slash \n newline'",
                    r"'\q\%\_'", r"'it''s'", r"'a\0\b\r\t\Z\"z'", 'NULL', '-42', '1.25']
        with self.c.cursor() as cursor:
            for literal in literals:
                with self.subTest(literal=literal):
                    # This is a fixed synthetic test literal, never data from an export.
                    cursor.execute('SELECT ' + literal)
                    got = cursor.fetchone()[0]
                    expected = list(imp.literal_rows('(' + literal + ')'))[0][0]
                    self.assertEqual(got, expected)

    def test_bound_values_and_streamed_readback(self):
        name = 'nhl_tickets'
        with self.c.cursor() as cursor:
            cursor.execute('CREATE TABLE nhl_tickets (id INT PRIMARY KEY, section VARCHAR(300) NOT NULL, price INT NOT NULL) DEFAULT CHARSET=utf8mb3 COLLATE=utf8mb3_general_ci')
        t = imp.Table(name, [('id','int','NO','',''),('section','varchar(300)','NO','','utf8_general_ci'),('price','int','NO','','')], [('PRIMARY',0,1,'id')], [], None, [(1,"é'; DROP TABLE x; --",125),(2,'slash \\ and newline\n',200)])
        imp.insert_batch(self.c,t,t.rows)
        self.assertEqual(imp.existing_keys(self.c,t),{(1,),(2,)})
        with self.assertRaises(Exception):
            imp.insert_batch(self.c,t,[(3,'too long'*100,1)])
        self.assertEqual(imp.existing_keys(self.c,t),{(1,),(2,)})

    def test_json_float_and_microsecond_roundtrip(self):
        with self.c.cursor() as cursor:
            cursor.execute('CREATE TABLE typed_values (id INT PRIMARY KEY, payload JSON NOT NULL, price FLOAT NOT NULL, captured_at DATETIME(6) NOT NULL)')
        columns=[('id','int','NO','',''),('payload','json','NO','',''),('price','float','NO','',''),('captured_at','datetime(6)','NO','','')]
        rows=[(1,'{"b": [true, null, "Café"], "a": 34.103123}',1.1,datetime(2026,9,20,12,34,56,123456))]
        t=imp.Table('typed_values',columns,[('PRIMARY',0,1,'id')],[],None,rows)
        imp.insert_batch(self.c,t,t.rows)
        self.assertEqual(imp.existing_keys(self.c,t),{(1,)})

    def test_real_non_tidb_connection_is_rejected(self):
        with self.assertRaises(imp.Stop):
            imp.check_target(self.c,[])


if __name__ == '__main__':
    unittest.main()
