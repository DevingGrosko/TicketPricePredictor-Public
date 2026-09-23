"""Offline tests for publication integrity and snapshot extraction boundaries."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import build_static_preview as m


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle = m.Bundle(Path(self.temp.name) / 'new')

    def test_content_hash_and_deterministic_payload(self):
        a = self.bundle.blob('index', {'b':2,'a':1})
        b = self.bundle.blob('index', {'a':1,'b':2})
        self.assertEqual(a,b)
        self.assertEqual(len(self.bundle.files),1)
        self.assertIn(hashlib.sha256((self.bundle.root/a).read_bytes()).hexdigest(),a)

    def test_invalid_category_never_becomes_path(self):
        for name in ('../secret','/etc/passwd','../series',''):
            with self.subTest(name=name),self.assertRaises(m.BuildError):
                self.bundle.blob(name,{})

    def test_nonfinite_values_rejected(self):
        for value in (float('nan'),float('inf'),float('-inf')):
            with self.assertRaises(ValueError): self.bundle.blob('index',{'value':value})

    def test_size_budget_fails_closed(self):
        with patch.object(m,'FILE_LIMIT',10),self.assertRaises(m.BuildError):
            self.bundle.blob('index',{'abc':'too long'})
        self.assertFalse(list((self.bundle.root/'data').iterdir()))

    def test_existing_output_not_overwritten(self):
        with self.assertRaises(FileExistsError): m.Bundle(self.bundle.root)

    def test_duplicate_times_prices_and_zero_prices_preserved(self):
        chart={'x':[3,3,1],'y':[0,0,10]}
        refs=self.bundle.series({'Club 1':chart})
        read=json.loads((self.bundle.root/refs[0]['file']).read_bytes())
        self.assertEqual(read['sections'][refs[0]['key']],chart)
        self.assertEqual(refs[0]['points'],3)

    def test_raw_labels_never_used_as_filenames_or_html(self):
        label='../../<script>alert(1)</script>'
        refs=self.bundle.series({label:{'x':[2,1],'y':[4,2]}})
        self.assertEqual(refs[0]['name'],label)
        self.assertNotIn('<',refs[0]['file'])
        self.assertNotIn('..',refs[0]['file'])

    def test_large_section_requires_explicit_sharding_instead_of_huge_payload(self):
        with patch.object(m,'SHARD_LIMIT',100),self.assertRaises(m.BuildError):
            self.bundle.series({'A':{'x':list(reversed(range(100))),'y':[1]*100}})

    def test_small_shards_split_and_references_resolve(self):
        series={str(i):{'x':[3,2,1],'y':[10,9,8]} for i in range(20)}
        with patch.object(m,'SHARD_LIMIT',250): refs=self.bundle.series(series)
        self.assertGreater(len({r['file'] for r in refs}),1)
        for r in refs:
            blob=json.loads((self.bundle.root/r['file']).read_bytes())
            self.assertEqual(blob['sections'][r['key']],series[r['name']])

    def test_malformed_or_backward_chart_rejected(self):
        for chart in ({'x':[],'y':[]},{'x':[1],'y':[2,3]}, {'x':[1,2],'y':[2,3]},
                      {'x':[1],'y':[True]}, {'x':[1],'y':['2']}):
            with self.subTest(chart=chart),self.assertRaises(m.BuildError):m.valid_chart(chart)

    def test_manifest_does_not_include_large_checksum_catalogue(self):
        assets=Path(self.temp.name)/'ui';assets.mkdir()
        for name in ('index.html','app.js','styles.css'): (assets/name).write_text('test')
        self.bundle.blob('index',{'abc':1})
        result=self.bundle.finish({'version':1},assets)
        self.assertEqual(json.loads((self.bundle.root/'manifest.json').read_bytes()),{'version':1})
        self.assertTrue((self.bundle.root/'checksums.json').is_file())
        self.assertEqual(result['json_files'],1)


class Result:
    def __init__(self, rows=(), scalar=None):self.rows=rows;self.scalar=scalar
    def mappings(self):return self
    def all(self):return self.rows
    def scalar_one(self):return self.scalar
    def __iter__(self):return iter(self.rows)
    def close(self):pass


class Source:
    def __init__(self,orphan=False,isolation='REPEATABLE-READ'):
        self.sql=[];self.closed=False;self.orphan=orphan;self.isolation=isolation
    def __enter__(self):return self
    def __exit__(self,*args):self.closed=True
    def execution_options(self,**kw):return self
    def exec_driver_sql(self,sql):
        self.sql.append(sql)
        if sql=='SELECT @@transaction_isolation':return Result(scalar=self.isolation)
        if 'COUNT(*)' in sql:return Result(scalar=2)
        if 'FROM `iterations`' in sql:return Result(rows=[(3,1,datetime(2026,9,19,12))])
        if 'FROM `tickets`' in sql:return Result(rows=[(1,999 if self.orphan else 3,'A',5),(2,3,'A',0)])
        return Result(rows=[{'id':1,'title':'A at B','event_date':datetime(2026,9,20,12),
                            'event_sections':'["A"]','URL':'test','Place':'B'}])


class ReaderTests(unittest.TestCase):
    def read(self,source):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        settings=SimpleNamespace(engine_for=lambda sport:SimpleNamespace(connect=lambda:source))
        result=m.read_sport('mlb',db,settings,lambda d:d.replace(tzinfo=timezone.utc))
        return db,result
    def test_only_select_and_closes_source_before_build(self):
        source=Source();db,result=self.read(source)
        self.assertTrue(all(sql.startswith('SELECT ') for sql in source.sql))
        self.assertTrue(source.closed)
        self.assertEqual(result[-1],{'games':1,'captures':1,'tickets':2})
        self.assertEqual(db.execute('SELECT price,hours FROM raw ORDER BY id').fetchall(),[(5,24.0),(0,24.0)])
    def test_orphan_stops_instead_of_silent_drop(self):
        with self.assertRaises(m.BuildError):self.read(Source(orphan=True))
    def test_weak_source_isolation_rejected(self):
        with self.assertRaises(m.BuildError):self.read(Source(isolation='READ-COMMITTED'))
    def test_unknown_sport_cannot_interpolate_sql(self):
        with self.assertRaises(KeyError):m.SPORTS['sys; DROP TABLE t']
    def test_datetime_labels_are_explicit_utc(self):
        self.assertTrue(m.utc_iso(datetime(2026,9,21)).endswith('+00:00'))


if __name__=='__main__':unittest.main()
