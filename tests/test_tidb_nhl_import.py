"""Synthetic-only importer tests. No exports, credentials, or network required."""
from datetime import datetime
from decimal import Decimal
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import ssl
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

from tools import tidb_nhl_import as imp


def table():
    return imp.Table('nhl_tickets', [
        ('id','int','NO','auto_increment',''),
        ('section','varchar(300)','NO','','utf8_general_ci'),
        ('price','int','NO','',''),
        ('listing_count','int','NO','',''),
        ('iteration_id','int','NO','','')], [('PRIMARY',0,1,'id')], [], 10, [])


class LiteralTests(unittest.TestCase):
    def test_literals_and_scientific_numbers(self):
        self.assertEqual(list(imp.literal_rows("(1,NULL,'text',-3.5,1e3),(2,'',0,4,5)")),
                         [(1,None,'text',Decimal('-3.5'),Decimal('1e3')),(2,'',0,4,5)])

    def test_unicode_and_special_sql_string_characters(self):
        text = r"(1,'Café l\'arène; DROP TABLE t; \\ slash, (test) \n new')"
        self.assertEqual(list(imp.literal_rows(text))[0][1], "Café l'arène; DROP TABLE t; \\ slash, (test) \n new")

    def test_double_quote_escape_and_control_bytes(self):
        token = r"'a\0\b\r\t\Z\"\\z'"
        self.assertEqual(imp.quoted_value(token), 'a\0\b\r\t\x1a"\\z')

    def test_doubled_apostrophe(self):
        self.assertEqual(list(imp.literal_rows("('it''s')")), [("it's",)])

    def test_mysql_unknown_escapes_and_like_escapes(self):
        self.assertEqual(imp.quoted_value(r"'\q\%\_'"), 'q\\%\\_')

    def test_sql_expressions_and_extra_statements_rejected(self):
        for text in ['', '(SLEEP(1))', '(NULL); DROP TABLE t', '(1+2)', '(1),', "('unterminated)",
                     '(1) UNION SELECT 2', '(1,)', '(DEFAULT)', '(0xAB)', '(1))']:
            with self.subTest(text=text), self.assertRaises(imp.Stop):
                list(imp.literal_rows(text))

    def test_escaped_json_is_decoded_once(self):
        original = json.dumps({'value': "é' and \\ backslash", 'array': [1,None]})
        escaped = original.replace('\\', '\\\\').replace("'", "\\'")
        self.assertEqual(list(imp.literal_rows("('" + escaped + "')"))[0][0], original)


class IntegrityTests(unittest.TestCase):
    def test_exact_integer_and_text_differences_are_detected(self):
        t = table(); row = (1, 'Section é', 125, 2, 3)
        self.assertNotEqual(imp.row_fingerprint(t,row), imp.row_fingerprint(t,(1,'SECTION E',125,2,3)))
        self.assertNotEqual(imp.row_fingerprint(t,row), imp.row_fingerprint(t,(1,'Section é',126,2,3)))

    def test_target_subsets_can_be_identified_without_overwriting(self):
        t=table();t.rows=[(1,'a',2,3,4),(2,'b',3,4,5)]
        self.assertEqual(imp.matched_keys(t,[t.rows[0]]), {(1,)})

    def test_unknown_target_rows_stop(self):
        t=table();t.rows=[(1,'a',2,3,4)]
        with self.assertRaises(imp.Stop): imp.matched_keys(t, [(2,'a',2,3,4)])

    def test_changed_existing_row_stops(self):
        t=table();t.rows=[(1,'a',2,3,4)]
        with self.assertRaises(imp.Stop): imp.matched_keys(t, [(1,'a',999,3,4)])

    def test_duplicate_source_and_target_keys_stop(self):
        t=table();t.rows=[(1,'a',2,3,4)]
        with self.assertRaises(imp.Stop): imp.matched_keys(t,t.rows*2)
        t.rows *= 2
        with self.assertRaises(imp.Stop): imp.source_map(t)

    def test_null_and_empty_string_are_distinct(self):
        t=imp.Table('x',[('value','varchar(20)','YES','','utf8_general_ci')],[],[],None,[])
        self.assertNotEqual(imp.row_fingerprint(t,(None,)), imp.row_fingerprint(t,('',)))

    def test_timestamp_microseconds_and_nullability(self):
        t=imp.Table('x',[('date','datetime(6)','NO','','')],[],[],None,[])
        self.assertEqual(imp.typed_row(t,('2026-09-20 12:34:56.123456',)), (datetime(2026,9,20,12,34,56,123456),))
        for value in (None,'0000-00-00 00:00:00','2026-09-20','2026-09-20 12:34:56.1234567'):
            with self.subTest(value=value), self.assertRaises((imp.Stop,ValueError)):
                imp.typed_row(t,(value,))

    def test_integer_overflow_and_four_byte_text_stop(self):
        for row in [(2**31,'a',1,1,1),(1,'\U0001F600',1,1,1),(1,'a'*301,1,1,1),(1,'a',1.5,1,1)]:
            with self.subTest(row=row), self.assertRaises(imp.Stop): imp.typed_row(table(),row)

    def test_float_compares_storage_bits(self):
        t=imp.Table('x',[('f','float','NO','','')],[],[],None,[])
        self.assertEqual(imp.row_fingerprint(t,(Decimal('1.1'),)),imp.row_fingerprint(t,(1.100000023841858,)))
        self.assertNotEqual(imp.row_fingerprint(t,(Decimal('1.1'),)),imp.row_fingerprint(t,(Decimal('1.2'),)))

    def test_json_key_order_and_numeric_spelling(self):
        self.assertEqual(imp.json_tree('{"b":1.00,"a":[true,null]}'), imp.json_tree('{"a":[true,null],"b":1}'))
        self.assertNotEqual(imp.json_tree('{"b":1}'),imp.json_tree('{"b":"1"}'))

    def test_json_numbers_do_not_silently_round(self):
        a = '0.1234567890123456789012345678901';b = '0.1234567890123456789012345678902'
        self.assertNotEqual(imp.json_tree(a),imp.json_tree(b))

    def test_invalid_json_and_duplicate_keys_stop(self):
        for text in ['{"a":1,"a":2}','NaN','Infinity']:
            with self.subTest(text=text),self.assertRaises(imp.Stop):imp.json_tree(text)

    def test_gzip_checksum_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'bad.gz';p.write_bytes(gzip.compress(b'not the NHL backup'))
            with self.assertRaises(imp.Stop):imp.load_source(p)

    def test_uncompressed_checksum_rejected(self):
        with self.assertRaises(imp.Stop):imp.parse_dump(b'not the pinned SQL snapshot')

    def test_bounded_batches(self):
        rows=[(x,'a',1,1,1) for x in range(501)]
        self.assertEqual([len(b) for b in imp.batches(rows)],[250,250,1])
        self.assertEqual(list(imp.batches([])),[])
        self.assertEqual(len(list(imp.batches([(1,'a'*400000),(2,'a'*400000)]))),2)
        with self.assertRaises(imp.Stop):list(imp.batches([(1,'a'*(2*1024*1024))]))

    def test_audit_is_order_independent(self):
        t=table();t.rows=[(2,'b',1,1,1),(1,'a',1,1,1)]
        before=imp.audit([t]);t.rows.reverse()
        self.assertEqual(before,imp.audit([t]))


class DatabaseBoundaryTests(unittest.TestCase):
    def fake_driver(self):
        return types.SimpleNamespace(connect=MagicMock(),cursors=types.SimpleNamespace(SSCursor=object()))

    def test_no_production_setting_fallback(self):
        driver=self.fake_driver()
        with patch.dict('sys.modules',{'pymysql':driver}), patch.dict(os.environ,{'MYSQL_HOST':'production','MYSQL_PASSWORD':'production'},clear=True):
            with self.assertRaises(imp.Stop):imp.connect_staging()
        driver.connect.assert_not_called()

    def test_host_lookalikes_and_pythonanywhere_rejected(self):
        driver=self.fake_driver()
        for host in ['localhost','127.0.0.1','example.mysql.pythonanywhere-services.com','tidbcloud.com.attacker.example','evil-tidbcloud.com']:
            with patch.dict('sys.modules',{'pymysql':driver}),patch.dict(os.environ,{'TIDB_STAGING_HOST':host,'TIDB_STAGING_USERNAME':'synthetic'},clear=True),self.assertRaises(imp.Stop):
                imp.connect_staging()
        driver.connect.assert_not_called()

    def test_connection_is_explicit_and_verified_tls(self):
        driver=self.fake_driver()
        env={'TIDB_STAGING_HOST':'gateway.example.tidbcloud.com','TIDB_STAGING_USERNAME':'synthetic','TIDB_STAGING_PASSWORD':'test-only'}
        with patch.dict('sys.modules',{'pymysql':driver}),patch.dict(os.environ,env,clear=True):imp.connect_staging()
        args=driver.connect.call_args.kwargs
        self.assertEqual(args['database'],'ticketsignal_staging_nhl');self.assertEqual(args['port'],4000)
        self.assertTrue(args['ssl'].check_hostname);self.assertEqual(args['ssl'].verify_mode,ssl.CERT_REQUIRED)
        self.assertFalse(args['local_infile']);self.assertIsNone(args['read_default_file']);self.assertFalse(args['autocommit'])

    def test_values_are_parameters_not_executable_sql(self):
        connection=MagicMock();cur=connection.cursor.return_value.__enter__.return_value
        cur.executemany.return_value=1;cur.fetchone.return_value=None
        data=[(1,"'; DROP TABLE x; --",2,3,4)]
        imp.insert_batch(connection,table(),data)
        sql,params=cur.executemany.call_args.args
        self.assertNotIn('DROP TABLE',sql);self.assertEqual(params,data)
        connection.commit.assert_called_once();connection.rollback.assert_not_called()

    def test_error_rolls_back_batch(self):
        connection=MagicMock();cur=connection.cursor.return_value.__enter__.return_value
        cur.executemany.side_effect=RuntimeError('synthetic failure')
        with self.assertRaises(RuntimeError):imp.insert_batch(connection,table(),[(1,'a',2,3,4)])
        connection.rollback.assert_called_once();connection.commit.assert_not_called()

    def test_conversion_warning_rolls_back_batch(self):
        connection=MagicMock();cur=connection.cursor.return_value.__enter__.return_value
        cur.executemany.return_value=1;cur.fetchone.return_value=('Warning',1265,'test only')
        with self.assertRaises(imp.Stop):imp.insert_batch(connection,table(),[(1,'a',2,3,4)])
        connection.rollback.assert_called_once();connection.commit.assert_not_called()

    def test_apply_refuses_nonempty_target_before_inserting(self):
        t=table();t.rows=[(1,'a',2,3,4)];c=MagicMock()
        with patch.object(imp,'connect_staging',return_value=c),patch.object(imp,'check_target'),patch.object(imp,'existing_keys',return_value={(1,)}),patch.object(imp,'insert_batch') as insert:
            with self.assertRaises(imp.Stop):imp.run_import([t],'apply')
        insert.assert_not_called();c.close.assert_called_once()

    def test_verify_does_not_insert(self):
        t=table();t.rows=[(1,'a',2,3,4)];c=MagicMock()
        with patch.object(imp,'connect_staging',return_value=c),patch.object(imp,'check_target'),patch.object(imp,'existing_keys',return_value={(1,)}),patch.object(imp,'insert_batch') as insert,patch('sys.stdout',new=io.StringIO()):
            result=imp.run_import([t],'verify')
        insert.assert_not_called();self.assertTrue(result['target_full_comparison_passed'])

    def test_resume_only_inserts_missing_rows(self):
        t=table();t.rows=[(1,'a',2,3,4),(2,'b',2,3,4)];c=MagicMock()
        with patch.object(imp,'connect_staging',return_value=c),patch.object(imp,'check_target'),patch.object(imp,'existing_keys',side_effect=[{(1,)},{(1,),(2,)}]),patch.object(imp,'insert_batch') as insert,patch('sys.stdout',new=io.StringIO()):
            imp.run_import([t],'resume')
        self.assertEqual(insert.call_args.args[2],[t.rows[1]])

    def test_partial_import_does_not_report_success(self):
        t=table();t.rows=[(1,'a',2,3,4)];c=MagicMock()
        with patch.object(imp,'connect_staging',return_value=c),patch.object(imp,'check_target'),patch.object(imp,'existing_keys',return_value=set()),patch.object(imp,'insert_batch'),patch('sys.stdout',new=io.StringIO()):
            with self.assertRaises(imp.Stop):imp.run_import([t],'apply')


if __name__=='__main__':unittest.main()
