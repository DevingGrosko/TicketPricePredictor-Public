"""Cache correctness and byte-for-byte original-static output regression tests."""
from pathlib import Path
import importlib.util
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools.build_original_fast import IdentityMemo, cached_original_analysis


class MemoTests(unittest.TestCase):
    def test_same_identity_labels_threshold_reuses_validation(self):
        fn=Mock(return_value=True);memo=IdentityMemo(fn);geometry={}
        for _ in range(8):self.assertTrue(memo(geometry,['101','102'],minimum_ratio=.6))
        self.assertEqual(fn.call_count,1)
        self.assertEqual((memo.hits,memo.misses),(7,1))

    def test_distinct_objects_labels_order_and_threshold_do_not_collide(self):
        fn=Mock(return_value=None);memo=IdentityMemo(fn);a={};b={}
        memo(a,['101','102']);memo(b,['101','102']);memo(a,['102','101'])
        memo(a,['101','102'],minimum_ratio=.7)
        self.assertEqual(fn.call_count,4)
        self.assertTrue(any(value[0] is a for value in memo.entries.values()))

    def test_bound_and_clear(self):
        memo=IdentityMemo(lambda geometry,names:len(names),limit=2)
        objects=[{} for _ in range(3)]
        for obj in objects:memo(obj,['101'])
        self.assertEqual(len(memo.entries),2)
        memo.clear();self.assertFalse(memo.entries)

    def test_failure_is_not_cached(self):
        fn=Mock(side_effect=ValueError('invalid'));memo=IdentityMemo(fn);obj={}
        for _ in range(2):
            with self.assertRaises(ValueError):memo(obj,['101'])
        self.assertEqual(fn.call_count,2);self.assertFalse(memo.entries)


class CacheParityTests(unittest.TestCase):
    def test_original_validators_and_aliases_are_preserved(self):
        from Flask_App import nfl_blueprint as nfl, nfl_stadium_blueprint as api, nhl_blueprint as nhl
        from copy import deepcopy
        names=['Section 101','Section 102','Section 103','Section 104','Club 101','Parking']
        cases=[None,{}, {'sections':[]},
            {'view_box':'0 0 100 100','sections':[{'name':n,'shapes':[{'path':f'M {i} 0 L {i+1} 0 L {i+1} 5 Z','transform':''}]} for i,n in enumerate(names)]},
            {'sections':[{'name':'101','shapes':[{'path':'<script>alert(1)</script>'}]}]},
            {'view_box':'0 0 100 100','sections':[{'name':'101','shapes':[{'path':'M0 0 L1 1 Z','transform':'javascript:bad'}]}]}]
        original_sanitize,original_usable=nfl.sanitize_map_geometry,nfl.geometry_is_usable
        originals={(id(module),name):getattr(module,name) for module in (nfl,api,nhl) for name in ('sanitize_map_geometry','geometry_is_usable') if hasattr(module,name)}
        snapshots=deepcopy(cases)
        expected=[(original_sanitize(g,names),[original_usable(g,names,minimum_ratio=t) for t in (.3,.6,.9)]) for g in cases]
        with cached_original_analysis():
            for _ in range(3):
                for g,(sanitized,usable) in zip(cases,expected):
                    self.assertEqual(api.sanitize_map_geometry(g,names),sanitized)
                    self.assertEqual([api.geometry_is_usable(g,names,minimum_ratio=t) for t in (.3,.6,.9)],usable)
            first=api._public_sections(names);first.clear()
            self.assertTrue(api._public_sections(names))
        for module in (nfl,api,nhl):
            for name in ('sanitize_map_geometry','geometry_is_usable'):
                if hasattr(module,name):self.assertIs(getattr(module,name),originals[(id(module),name)])
        self.assertEqual(cases,snapshots)

    def test_restored_after_exception(self):
        from Flask_App import nfl_blueprint as nfl
        original=nfl.sanitize_map_geometry
        with self.assertRaises(RuntimeError):
            with cached_original_analysis():raise RuntimeError('stop')
        self.assertIs(nfl.sanitize_map_geometry,original)

    def test_same_full_fixture_output_including_provider_geometry(self):
        from tools import build_static_preview as source
        from tools.build_original_static import ROOT
        spec=importlib.util.spec_from_file_location('cache_fixture',ROOT/'tests/test_original_static.py')
        fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
        original_build=source.build_sport
        def with_maps(sport,spool,events,*args,**kwargs):
            if sport!='mlb':
                for event in events.values():
                    event.sections=[f'Section {n}' for n in range(101,109)]
                    event.map_geometry={'view_box':'0 0 100 100','sections':[
                        {'name':name,'shapes':[{'path':f'M {i*10} 0 L {i*10+5} 0 L {i*10+5} 10 Z','transform':''}]} for i,name in enumerate(event.sections)]}
            return original_build(sport,spool,events,*args,**kwargs)
        with tempfile.TemporaryDirectory() as work,patch.object(source,'build_sport',with_maps):
            slow=Path(work)/'slow';fast=Path(work)/'fast'
            fixture.fixture(slow)
            with cached_original_analysis():fixture.fixture(fast)
            old={str(p.relative_to(slow)):p.read_bytes() for p in slow.rglob('*') if p.is_file()}
            new={str(p.relative_to(fast)):p.read_bytes() for p in fast.rglob('*') if p.is_file()}
            self.assertEqual(set(old),set(new))
            for path in old:self.assertEqual(old[path],new[path],path)


if __name__=='__main__':unittest.main()
