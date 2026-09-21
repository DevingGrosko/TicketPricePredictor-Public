"""Synthetic tests; never need real export data or staging credentials."""
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
from tools import tidb_mlb_nfl_import as imp

DDL = '''CREATE TABLE `analytics_dirty_venue` (
  `venue` varchar(300) NOT NULL,
  `revision` int NOT NULL,
  `dirty` tinyint(1) NOT NULL,
  `updated_at` datetime(6) NOT NULL,
  PRIMARY KEY (`venue`),
  KEY `ix_analytics_dirty_venue_dirty` (`dirty`,`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;'''


def row(name='Café', revision=1):
    return (name, revision, 1, datetime(2026, 9, 20, 12, 34, 56, 123456))


class ValuesTests(unittest.TestCase):
    def test_literal_parser_and_doubled_apostrophe(self):
        self.assertEqual(list(imp.literal_rows("(1,NULL,'it''s',-3.5,1e3)")), [(1,None,"it's",Decimal('-3.5'),Decimal('1e3'))])

    def test_escapes_and_sql_like_text_remain_data(self):
        self.assertEqual(list(imp.literal_rows(r"('a\n\0\Z\'\\; DROP TABLE x;')")), [("a\n\0\x1a'\\; DROP TABLE x;",)])
        self.assertEqual(imp.quoted_value(r"'\q\%\_'"), 'q\\%\\_')

    def test_invalid_literal_sql_is_rejected(self):
        for x in ['', '(SLEEP(1))', '(1+2)', '(1),', '(1); DELETE FROM t', '(NULL))', "('no)"]:
            with self.subTest(x=x), self.assertRaises(imp.Stop): list(imp.literal_rows(x))

    def test_type_checks_and_mlb_nullable_column(self):
        t=imp.Table('tickets',[('id','int','NO','auto_increment',''),('ticketsPerSection','int','YES','','')],[('PRIMARY',0,1,'id')],[],10)
        self.assertEqual(imp.typed_row(t,(1,None)),(1,None))
        for value in [2**31, 1.5, True]:
            with self.subTest(value=value),self.assertRaises(imp.Stop):imp.typed_row(t,(value,None))

    def test_unicode_capacity_and_microseconds(self):
        t=imp.parse_table(DDL)
        self.assertEqual(imp.typed_row(t,('é',1,1,'2026-09-20 12:34:56.123456')),row('é'))
        for bad in ['x'*301,'\U0001f600']:
            with self.assertRaises(imp.Stop):imp.typed_row(t,row(bad))

    def test_nullability_and_datetime_precision(self):
        t=imp.Table('x',[('id','int','NO','',''),('d','datetime','NO','','')],[('PRIMARY',0,1,'id')],[],None)
        for value in [None,'0000-00-00 00:00:00','2026-09-20 00:00:00.000001']:
            with self.assertRaises((imp.Stop,ValueError)):imp.typed_row(t,(1,value))

    def test_json_numbers_and_structure(self):
        self.assertEqual(imp.json_tree('{"b":1.00,"a":[true,null]}'),imp.json_tree('{"a":[true,null],"b":1}'))
        self.assertNotEqual(imp.json_tree('0.1234567890123456789012345678901'),imp.json_tree('0.1234567890123456789012345678902'))
        for value in ['NaN','Infinity','{"a":1,"a":2}']:
            with self.assertRaises(imp.Stop):imp.json_tree(value)

    def test_float32_bits_and_exact_text(self):
        t=imp.Table('x',[('id','int','NO','',''),('v','float','NO','','')],[('PRIMARY',0,1,'id')],[],None)
        a=imp.typed_row(t,(1,Decimal('1.1')));b=imp.typed_row(t,(1,1.100000023841858))
        self.assertEqual(imp.row_fingerprint(t,a),imp.row_fingerprint(t,b))
        text=imp.parse_table(DDL)
        self.assertNotEqual(imp.row_fingerprint(text,row('é')),imp.row_fingerprint(text,row('E')))

    def test_keys_do_not_coerce_types_or_case(self):
        self.assertNotEqual(imp.encode_key((1,)),imp.encode_key(('1',)))
        self.assertNotEqual(imp.encode_key(('a',)),imp.encode_key(('A',)))

    def test_parse_full_structure_and_reject_unreviewed_ddl(self):
        t=imp.parse_table(DDL)
        self.assertEqual(t.pk_positions,(0,));self.assertEqual(len(t.columns),4)
        self.assertEqual(len(t.indexes),3)
        for text in [DDL+'\nDROP TABLE x;',DDL.replace('varchar(300)','text'),DDL.replace('PRIMARY KEY (`venue`)','PRIMARY KEY (LOWER(`venue`))')]:
            with self.assertRaises(imp.Stop):imp.parse_table(text)


class DiskTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.index=imp.DiskIndex(self.root/'fresh.sqlite');self.table=imp.parse_table(DDL)
        self.index.add_table(self.table)
    def tearDown(self):self.index.close();self.tmp.cleanup()

    def test_matches_all_fields_and_detects_missing(self):
        self.index.add_rows(self.table,[row('a'),row('b')])
        self.assertEqual(self.index.match(self.table,[row('a')]),1)
        self.assertFalse(self.index.missing(self.table,row('a')));self.assertTrue(self.index.missing(self.table,row('b')))
        self.index.reset_seen();self.assertTrue(self.index.missing(self.table,row('a')))

    def test_source_duplicates_rejected(self):
        with self.assertRaises(imp.Stop):self.index.add_rows(self.table,[row(),row()])

    def test_unknown_target_and_changed_rows_rejected(self):
        self.index.add_rows(self.table,[row()])
        for r in [row('unknown'),row(revision=99)]:
            with self.assertRaises(imp.Stop):self.index.match(self.table,[r])

    def test_duplicate_target_keys_rejected(self):
        self.index.add_rows(self.table,[row()])
        with self.assertRaises(imp.Stop):self.index.match(self.table,[row(),row()])

    def test_source_change_between_passes_rejected(self):
        self.index.add_rows(self.table,[row()])
        with self.assertRaises(imp.Stop):self.index.missing(self.table,row(revision=99))

    def test_fingerprint_is_order_independent(self):
        self.index.add_rows(self.table,[row('z'),row('a')]);a=self.index.fingerprint(self.table)
        self.index.reset_seen();self.index.match(self.table,[row('a'),row('z')])
        self.assertEqual(a,self.index.fingerprint(self.table))

    def test_foreign_key_orphan_rejected(self):
        parent=imp.Table('event',[('id','int','NO','','')],[('PRIMARY',0,1,'id')],[],1)
        child=imp.Table('iterations',[('id','int','NO','',''),('event_id','int','NO','','')],[('PRIMARY',0,1,'id')],[('fk','event_id','event','id')],1)
        for t in (parent,child):self.index.add_table(t)
        self.index.add_rows(parent,[(1,)]);self.index.add_rows(child,[(1,2)])
        with self.assertRaises(imp.Stop):self.index.check_foreign_keys([parent,child])

    def test_foreign_key_valid(self):
        parent=imp.Table('event',[('id','int','NO','','')],[('PRIMARY',0,1,'id')],[],1)
        child=imp.Table('iterations',[('id','int','NO','',''),('event_id','int','NO','','')],[('PRIMARY',0,1,'id')],[('fk','event_id','event','id')],1)
        for t in (parent,child):self.index.add_table(t)
        self.index.add_rows(parent,[(1,)]);self.index.add_rows(child,[(2,1)])
        self.index.check_foreign_keys([parent,child])

    def test_scratch_existing_file_not_overwritten(self):
        with self.assertRaises(imp.Stop):imp.DiskIndex(self.index.path)


class SourceTests(unittest.TestCase):
    def test_wrong_sport_or_source_rejected_before_network(self):
        for s in ['nhl','sys','main','nfl; DROP TABLE x']:
            with self.assertRaises(imp.Stop):imp.schema_for(s)
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'wrong.gz';path.write_bytes(gzip.compress(b'not source'))
            with patch.object(imp,'connect_staging') as connect, self.assertRaises(imp.Stop):imp.verify_gzip(path,'nfl')
            connect.assert_not_called()

    def test_stream_checks_full_plaintext_hash(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'data.gz';p.write_bytes(gzip.compress(b'-- not the snapshot\n'))
            with self.assertRaises(imp.Stop):list(imp.read_dump(p,'nfl'))

    def test_full_synthetic_stream_audit(self):
        content=(DDL+"\nINSERT INTO `analytics_dirty_venue` VALUES ('a',1,1,'2026-09-20 12:34:56.123456');\n-- done\n").encode()
        data=gzip.compress(content)
        spec=dict(gzip_sha=hashlib.sha256(data).hexdigest(),sql_sha=hashlib.sha256(content).hexdigest(),ddl_sha=hashlib.sha256((DDL+'\n').encode()).hexdigest(),gzip_bytes=len(data),sql_bytes=len(content),ending='-- done\n',tables=('analytics_dirty_venue',),column_count=4)
        with tempfile.TemporaryDirectory() as root,patch.dict(imp.SPECS,{'nfl':spec}):
            p=Path(root)/'input.gz';p.write_bytes(data);index=imp.DiskIndex(Path(root)/'index')
            try:
                with patch('sys.stdout',new=io.StringIO()):tables,report=imp.audit_source(p,'nfl',index)
                self.assertEqual(report['total_rows'],1);self.assertFalse(report['historical_import_performed'])
            finally:index.close()


class BoundaryTests(unittest.TestCase):
    def fake_driver(self):return types.SimpleNamespace(connect=MagicMock())

    def test_separate_schemas_and_tls_no_production_fallback(self):
        driver=self.fake_driver()
        with patch.dict('sys.modules',{'pymysql':driver}),patch.dict(os.environ,{'MYSQL_HOST':'production'},clear=True):
            with self.assertRaises(imp.Stop):imp.connect_staging('mlb')
        driver.connect.assert_not_called()
        env={'TIDB_STAGING_HOST':'gateway.example.tidbcloud.com','TIDB_STAGING_USERNAME':'synthetic','TIDB_STAGING_PASSWORD':'test-only'}
        for sport in ('mlb','nfl'):
            with patch.dict('sys.modules',{'pymysql':driver}),patch.dict(os.environ,env,clear=True):imp.connect_staging(sport)
            args=driver.connect.call_args.kwargs
            self.assertEqual(args['database'],'ticketsignal_staging_'+sport)
            self.assertEqual(args['port'],4000);self.assertTrue(args['ssl'].check_hostname)
            self.assertEqual(args['ssl'].verify_mode,ssl.CERT_REQUIRED)
            self.assertIsNone(args['read_default_file']);self.assertFalse(args['local_infile'])

    def test_reject_host_lookalikes(self):
        driver=self.fake_driver()
        for host in ['localhost','example.mysql.pythonanywhere-services.com','tidbcloud.com.evil.example','evil-tidbcloud.com']:
            with patch.dict('sys.modules',{'pymysql':driver}),patch.dict(os.environ,{'TIDB_STAGING_HOST':host,'TIDB_STAGING_USERNAME':'u'},clear=True),self.assertRaises(imp.Stop):imp.connect_staging('nfl')
        driver.connect.assert_not_called()

    def test_one_bound_insert_then_plain_warnings_before_commit(self):
        c=MagicMock();cur=c.cursor.return_value.__enter__.return_value
        cur.execute.side_effect=[2,0];cur.fetchone.return_value=None
        rows=[row("'; DROP TABLE x; --"),row('other')];imp.insert_batch(c,imp.parse_table(DDL),rows)
        self.assertEqual(cur.execute.call_count,2)
        sql,params=cur.execute.call_args_list[0].args
        self.assertNotIn('DROP TABLE',sql);self.assertEqual(params,rows[0]+rows[1])
        self.assertEqual(cur.execute.call_args_list[1].args,('SHOW WARNINGS',))
        cur.executemany.assert_not_called();c.commit.assert_called_once()

    def test_warning_or_query_failure_rolls_back(self):
        for warning in (True,False):
            c=MagicMock();cur=c.cursor.return_value.__enter__.return_value
            if warning:cur.execute.side_effect=[1,0];cur.fetchone.return_value=('Warning',1265,'test')
            else:cur.execute.side_effect=RuntimeError('synthetic')
            with self.assertRaises((imp.Stop,RuntimeError)):imp.insert_batch(c,imp.parse_table(DDL),[row()])
            c.rollback.assert_called_once();c.commit.assert_not_called()

    def test_ambiguous_commit_not_retried(self):
        c=MagicMock();cur=c.cursor.return_value.__enter__.return_value;cur.execute.side_effect=[1,0];cur.fetchone.return_value=None
        c.commit.side_effect=RuntimeError('lost acknowledgement')
        with self.assertRaises(RuntimeError):imp.insert_batch(c,imp.parse_table(DDL),[row()])
        c.commit.assert_called_once();self.assertEqual(cur.execute.call_count,2)

    def test_batch_count_and_payload_bounds(self):
        self.assertEqual([len(x) for x in imp.row_batches([(i,) for i in range(2001)])],[1000,1000,1])
        self.assertEqual(len(list(imp.row_batches([('a'*100000,),('b'*100000,)]))),2)
        with self.assertRaises(imp.Stop):list(imp.row_batches([('a'*2000000,)]))

    def test_verify_never_calls_insert_or_scans_source_again(self):
        c=MagicMock();result={}
        with patch.object(imp,'connect_staging',return_value=c),patch.object(imp,'check_target'),patch.object(imp,'compare_target',return_value={'x':1}),patch.object(imp,'insert_batch') as insert,patch.object(imp,'read_dump') as read:
            imp.run_import(Path('unused'),'nfl',[],MagicMock(),result,'verify')
        insert.assert_not_called();read.assert_not_called();self.assertTrue(result['target_full_comparison_passed']);c.close.assert_called_once()

    def test_apply_rejects_existing_data_before_insert(self):
        c=MagicMock()
        with patch.object(imp,'connect_staging',return_value=c),patch.object(imp,'check_target'),patch.object(imp,'compare_target',return_value={'x':1}),patch.object(imp,'insert_batch') as insert,self.assertRaises(imp.Stop):
            imp.run_import(Path('unused'),'nfl',[],MagicMock(),{},'apply')
        insert.assert_not_called()

    def test_resume_inserts_only_verified_missing_rows(self):
        t=imp.parse_table(DDL);t.count=2;c=MagicMock()
        with tempfile.TemporaryDirectory() as root:
            index=imp.DiskIndex(Path(root)/'index');index.add_table(t)
            index.add_rows(t,[row('a'),row('b')]);index.match(t,[row('a')])
            try:
                values="('a',1,1,'2026-09-20 12:34:56.123456'),('b',1,1,'2026-09-20 12:34:56.123456')"
                with patch.object(imp,'connect_staging',return_value=c),patch.object(imp,'check_target'),patch.object(imp,'compare_target',return_value={t.name:1}),patch.object(imp,'read_dump',return_value=iter([('rows',(t.name,values))])),patch.object(imp,'insert_batch') as insert,patch('sys.stdout',new=io.StringIO()):
                    result=imp.run_import(Path('unused'),'nfl',[t],index,{},'resume')
                self.assertEqual(insert.call_args.args[2],[row('b')]);self.assertEqual(result['rows_inserted_this_run'],1)
            finally:index.close()

    def test_final_incomplete_verification_cannot_report_success(self):
        c=MagicMock();report={}
        with patch.object(imp,'connect_staging',return_value=c),patch.object(imp,'check_target'),patch.object(imp,'compare_target',side_effect=[{},imp.Stop('incomplete')]),patch.object(imp,'read_dump',return_value=iter([])),self.assertRaises(imp.Stop):
            imp.run_import(Path('unused'),'nfl',[],MagicMock(),report,'resume')
        self.assertNotIn('target_full_comparison_passed',report)

    def test_wrong_server_stops_before_table_queries(self):
        for record in [('ticketsignal_staging_nfl','8.0.46',1),('production','8.0-TiDB',1),('ticketsignal_staging_nfl','8.0-TiDB',0)]:
            with patch.object(imp,'query',return_value=[record]) as q,self.assertRaises(imp.Stop):imp.check_target(MagicMock(),'nfl',[])
            self.assertEqual(q.call_count,1)


@unittest.skipUnless(os.getenv('SPORTS_IMPORT_TEST_MYSQL')=='1','Requires disposable CI MySQL')
class DriverTests(unittest.TestCase):
    def setUp(self):
        import pymysql
        self.c=pymysql.connect(host='127.0.0.1',port=3306,user='root',password='ephemeral-test-only',database='importer_test',charset='utf8mb4',autocommit=False)
        with self.c.cursor() as cur:cur.execute('CREATE TEMPORARY TABLE analytics_dirty_venue (venue varchar(300) PRIMARY KEY, revision int NOT NULL, dirty tinyint NOT NULL, updated_at datetime(6) NOT NULL)')
    def tearDown(self):self.c.close()

    def test_bound_multivalue_insert_and_streamed_disk_comparison(self):
        import pymysql
        t=imp.parse_table(DDL);rows=[row("a'; \\ é"),row('b')];imp.insert_batch(self.c,t,rows)
        with tempfile.TemporaryDirectory() as root:
            index=imp.DiskIndex(Path(root)/'index');index.add_table(t);index.add_rows(t,rows)
            try:
                with self.c.cursor(pymysql.cursors.SSCursor) as cur:
                    cur.execute('SELECT venue,revision,dirty,updated_at FROM analytics_dirty_venue')
                    self.assertEqual(index.match(t,cur),2)
            finally:index.close()

    def test_real_conversion_warning_not_committed(self):
        with self.c.cursor() as c:c.execute("SET SESSION sql_mode=''")
        self.c.commit()
        with self.assertRaises(imp.Stop):imp.insert_batch(self.c,imp.parse_table(DDL),[row(revision=2**31)])
        self.assertEqual(imp.query(self.c,'SELECT COUNT(*) FROM analytics_dirty_venue')[0][0],0)

    def test_real_non_tidb_server_rejected(self):
        with self.assertRaises(imp.Stop):imp.check_target(self.c,'nfl',[imp.parse_table(DDL)])


if __name__=='__main__':unittest.main()
