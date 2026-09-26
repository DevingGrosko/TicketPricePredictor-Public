"""Pure cache and synthetic Pages regression tests; no network or credentials."""
from datetime import datetime, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

from tools.free_refresh_cache import SnapshotCache, capture_diff
from tools.free_refresh_publish import mount_pages, PREFIX
from tools.build_static_preview import BuildError


class Result:
    def __init__(self, rows): self.rows = rows
    def __iter__(self): return iter(self.rows)
    def mappings(self): return self
    def all(self): return self.rows
    def scalar_one(self): return self.rows[0][0]
    def close(self): pass


class Source:
    def __init__(self):
        self.events = [{'id':1,'title':'New York Mets at Washington Nationals',
                        'event_date':datetime(2026,9,28,19),'event_sections':['101','102'],
                        'URL':'https://www.vividseats.com/--sports-mlb-baseball/game/production/1234567','Place':'Nationals Park'}]
        self.captures = [(10,1,datetime(2026,9,27,12))]
        self.tickets = [(1,10,'101',50),(2,10,'102',60)]
        self.reads = []; self.fail = False
    def connect(self): return self
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def execution_options(self,**kwargs): return self
    def engine_for(self,sport): return self
    def exec_driver_sql(self,sql,args=()):
        if sql == 'SELECT @@transaction_isolation': return Result([('REPEATABLE-READ',)])
        if 'FROM `event`' in sql: return Result(self.events)
        if 'FROM `iterations`' in sql: return Result(self.captures)
        if 'FROM `tickets`' in sql:
            self.reads.append((sql,args))
            if self.fail: raise RuntimeError('injected source failure')
            return Result([r for r in self.tickets if not args or r[1] in args])
        raise AssertionError(sql)


class DeltaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.source = Source(); self.cache = SnapshotCache(self.tmp.name)
    def read(self):
        spool = sqlite3.connect(':memory:'); self.addCleanup(spool.close)
        from models import event_datetime_utc
        output = self.cache.read_sport('mlb',spool,self.source,event_datetime_utc,include_maps=True)
        return spool, output
    def test_seed_then_no_ticket_reads_when_unchanged(self):
        spool,first = self.read()
        self.assertEqual(first[3]['tickets'],2)
        self.assertEqual(len(self.source.reads),1)
        self.read()
        self.assertEqual(len(self.source.reads),1)
        self.assertEqual(self.cache.metrics['mlb']['ticket_rows_read_from_tidb'],0)
    def test_late_commit_with_smaller_id_is_not_skipped(self):
        self.read()
        self.source.captures.append((5,1,datetime(2026,9,27,12,30)))
        self.source.tickets.extend([(3,5,'101',45),(4,5,'102',55)])
        spool,second = self.read()
        self.assertEqual(second[3]['tickets'],4)
        self.assertEqual(self.source.reads[-1][1],(5,))
        self.assertEqual(self.cache.metrics['mlb']['ticket_rows_read_from_tidb'],2)
    def test_rescheduling_updates_existing_lead_times_locally(self):
        spool,_ = self.read(); old = spool.execute('SELECT hours FROM raw WHERE id=1').fetchone()[0]
        self.source.events[0]['event_date'] += timedelta(hours=2)
        spool,_ = self.read()
        self.assertEqual(spool.execute('SELECT hours FROM raw WHERE id=1').fetchone()[0],old+2)
        self.assertEqual(len(self.source.reads),1)
    def test_failed_read_preserves_previous_cache(self):
        self.read(); path=Path(self.tmp.name)/'mlb.sqlite'; before=path.read_bytes()
        self.source.captures.append((11,1,datetime(2026,9,27,12,30)));self.source.fail=True
        with self.assertRaises(RuntimeError):self.read()
        self.assertEqual(path.read_bytes(),before)
    def test_changed_or_removed_capture_metadata_fails_closed(self):
        known={10:(1,'2026-09-27T12:00:00')}
        for changed in [{}, {10:(2,'2026-09-27T12:00:00')}, {10:(1,'2026-09-27T13:00:00')}]:
            with self.assertRaises(BuildError):capture_diff(known,changed)
    def test_wrong_sport_cache_fails_before_network(self):
        self.read(); path=Path(self.tmp.name)/'mlb.sqlite'
        with sqlite3.connect(path) as db:db.execute("UPDATE cache_meta SET value='nfl' WHERE key='sport'")
        with self.assertRaises(BuildError):self.read()


class PagesTests(unittest.TestCase):
    def test_project_routes_preserve_css_and_data_hashes(self):
        from tools.build_original_static import ROOT
        from tools.build_original_fast import cached_original_analysis
        spec=importlib.util.spec_from_file_location('free_fixture',ROOT/'tests/test_original_static.py')
        fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'site'
            with cached_original_analysis():fixture.fixture(root)
            data_before={p.relative_to(root):p.read_bytes() for p in (root/'data').glob('*.json')}
            css_before={p.relative_to(root):p.read_bytes() for p in (root/'static/css').glob('*.css')}
            stats=mount_pages(root)
            self.assertGreater(stats['public_files'],30)
            self.assertIn(PREFIX+'/native/bridge.js',(root/'index.html').read_text())
            self.assertIn('Start with a team.',(root/'index.html').read_text())
            for path,raw in {**data_before,**css_before}.items():self.assertEqual((root/path).read_bytes(),raw)
            for path in [root/'native/bridge.js',*list((root/'static/js').glob('*.js'))]:
                subprocess.run(['node','--check',str(path)],check=True,capture_output=True)
            manifest=json.loads((root/'original-manifest.json').read_text())
            self.assertFalse(manifest['live_updates_enabled'])
            self.assertEqual(manifest['base_path'],PREFIX)


if __name__ == '__main__':unittest.main()
