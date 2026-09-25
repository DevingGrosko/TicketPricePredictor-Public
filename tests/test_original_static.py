"""Original template restoration tests: synthetic data, no network or credentials."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import build_static_preview as source
from tools.build_original_static import OriginalPages, market_history, ROOT


def fixture(output):
    from Flask_App import nfl_stadium_blueprint as api
    from models import event_datetime_utc
    now = datetime(2026,9,25,tzinfo=timezone.utc)
    bundle=source.Bundle(output); renderer=OriginalPages(bundle,now)
    manifest={'version':1,'mode':'historical-snapshot-preview','presentation':'original-templates',
              'live_updates_enabled':False,'generated_at':now.isoformat(),'sports':[]}
    try:
        for sport,venue,team,title in [
            ('mlb','Nationals Park','Washington Nationals','New York Mets at Washington Nationals'),
            ('nfl','Northwest Stadium','Washington Commanders','Detroit Lions at Washington Commanders'),
            ('nhl','Bell Centre','Montreal Canadiens','Toronto Maple Leafs at Montreal Canadiens')]:
            spool=sqlite3.connect(':memory:')
            spool.execute('CREATE TABLE raw(id INTEGER PRIMARY KEY,event_id INTEGER,section TEXT,price INTEGER,hours REAL,captured TEXT,listing_count INTEGER)')
            events={};latest={};captures={}
            for eid in range(1,4):
                values={k:None for k in set(source.COMMON+source.COLUMNS['mlb']+source.COLUMNS['nhl'])}
                values.update(id=eid,title=title,event_date=datetime(2026,9,10+eid,19),
                    Place=venue,venue=venue,canonical_venue=venue,home_team=team,away_team=title.split(' at ')[0],
                    sections=['Section 101','Section 102'],event_sections=['Section 101','Section 102'],
                    URL='https://www.vividseats.com/--sports-mlb-baseball/test-'+str(eid),
                    source_url='https://www.vividseats.com/test-'+str(eid), source_id=str(eid),country='Canada' if sport=='nhl' else 'US',
                    currency='CAD' if sport=='nhl' else 'USD',game_type=2,map_geometry=None,city='')
                e=SimpleNamespace(**values);events[eid]=e
                captured=[]
                for j,lead in enumerate([72,47.75,36,24,18,12,6,3,1]):
                    cap=event_datetime_utc(e.event_date)-timedelta(hours=lead)
                    captured.append(cap)
                    for k,section in enumerate(values['sections']):
                        spool.execute('INSERT INTO raw VALUES(?,?,?,?,?,?,?)',(eid*100+j*2+k,eid,section,100-j*3+k*15,lead,cap.isoformat(),5+k))
                latest[eid]=max(captured);captures[eid]=len(captured)
            item=source.build_sport(sport,spool,events,latest,captures,bundle,now,api,page_builder=renderer.render_sport)
            manifest['sports'].append(item);spool.close()
        bundle.finish(manifest,ROOT/'static_preview');stats=renderer.finish()
    finally: renderer.close()
    return stats


class MarketParityTests(unittest.TestCase):
    def test_original_bin_algorithm_dollars_percentages_and_zero(self):
        # A private import avoids other legacy tests' graph_builder stubs.
        spec=importlib.util.spec_from_file_location('_native_graph_reference',ROOT/'graph_builder.py')
        graph=importlib.util.module_from_spec(spec);spec.loader.exec_module(graph)
        rng=random.Random(121)
        for case in range(12):
            histories=[(sorted([rng.uniform(-.1,72) for _ in range(100)],reverse=True),
                         [rng.choice([0,41,50,100,105,220]) for _ in range(100)]) for _ in range(case%4+1)]
            events=[SimpleNamespace(id=i,event_date=datetime(2026,9,12),URL='--sports-mlb-baseball/',Place='Nationals Park') for i in range(len(histories))]
            class Session:
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def query(self,*args):return self
                def filter(self,*args):return self
                def all(self):return events
            model=SimpleNamespace(getSession=lambda: Session)
            reader=graph.GraphBuilder();reader.eachEventGraphList=lambda section,eid:histories[eid]
            with patch.object(graph,'CreateModel',return_value=model), patch.object(graph,'event_has_complete_public_data',return_value=True), patch.dict('sys.modules', {'graph_builder':graph}):
                for mode in ['money','percentage']:
                    y,x,total=reader.allEventsForStadium('Nationals Park','Section 101',48,mode)
                    self.assertEqual(market_history(histories,mode),{'x':x,'y':y,'total':total})

    def test_no_overlap_does_not_invent_points(self):
        self.assertEqual(market_history([([60],[100])]),{'x':[],'y':[],'total':0})


class TemplateRestorationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)/'site';cls.stats=fixture(cls.root)
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def test_all_css_and_native_js_are_byte_identical(self):
        for p in (ROOT/'Flask_App/static').rglob('*'):
            if p.is_file(): self.assertEqual(p.read_bytes(),(self.root/'static'/p.relative_to(ROOT/'Flask_App/static')).read_bytes())
    def test_home_uses_original_headline_tabs_and_forms(self):
        text=(self.root/'index.html').read_text()
        for snippet in ['Start with a team.','See where prices soften.','market-panel','game-panel','timing-panel','game-place','site-nav']:
            self.assertIn(snippet,text)
        self.assertNotIn('static_preview/app.js',text)
    def test_templates_are_not_modified(self):
        self.assertIn('{% extends "base.html" %}',(ROOT/'Flask_App/templates/HomeScreen.html').read_text())
    def test_original_reports_and_section_evidence_exist(self):
        self.assertEqual(len(list((self.root/'reports').glob('*.html'))),3)
        self.assertEqual(len(list((self.root/'sections').glob('*.html'))),6)
        text=next((self.root/'sections').glob('*.html')).read_text()
        self.assertIn('Games used for this section.',text)
        self.assertIn('data-static-json',text)
    def test_maps_retain_listing_counts_and_null_prices(self):
        files=list((self.root/'maps').glob('*.html'));self.assertEqual(len(files),6)
        import re
        text=files[0].read_text();path=re.search(r'id="(?:nhl|nfl)-map-data"[^>]*data-static-json="([^"]+)"',text).group(1)
        data=json.loads((self.root/path.lstrip('/')).read_bytes())
        self.assertEqual([s['listing_count'] for s in data['sections']],[5,6])
    def test_published_output_contains_no_credentials_or_work_database(self):
        for p in self.root.rglob('*'):
            if p.is_file():
                self.assertNotIn(p.suffix,{'.db','.sqlite','.sql','.py','.gz','.whl'})
        manifest=json.loads((self.root/'original-manifest.json').read_bytes())
        self.assertFalse(manifest['live_updates_enabled'])
    def test_script_data_are_externalized_not_inline_executable(self):
        import re
        for p in self.root.rglob('*.html'):
            for attrs,body in re.findall(r'<script\b([^>]*)>(.*?)</script>',p.read_text(),re.S):
                self.assertTrue('src=' in attrs or 'type="text/plain"' in attrs or 'application/json' in attrs)
    def test_catalogs_split_by_sport_and_hashed(self):
        m=json.loads((self.root/'original-manifest.json').read_bytes())
        self.assertEqual(set(m['sports']),{'mlb','nfl','nhl'})
        for path in m['sports'].values():
            raw=(self.root/path.lstrip('/')).read_bytes();self.assertIn(hashlib.sha256(raw).hexdigest(),path)

if __name__=='__main__':unittest.main()
