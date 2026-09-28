"""Regression tests for complete, resumable capture rather than timed batches."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import copy
import tempfile
import unittest

from test_free_live_reliability import ParallelTests
from tools import free_live_collect as live
from tools import free_live_mlb as mlb_live
from tools.free_live_map_cache import cached_map_matching


class DrainTests(ParallelTests):
    def test_all_games_processed_even_after_old_fourteen_minute_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            nfl, stack, games, capture = self.fixture(directory, 6)
            clock = iter(range(0, 100000, 1000))
            stack.enter_context(patch.object(live, 'time', SimpleNamespace(monotonic=lambda: next(clock))))
            collect = stack.enter_context(patch.object(live, 'capture_one', side_effect=capture))
            stack.enter_context(patch.object(nfl, 'post_snapshot_with_retry',
                                       return_value={'status': 'stored', 'iteration_id': 1}))
            root = Path(directory)
            self.assertEqual(live.run_parallel_nfl('', '', True, 1, root/'health.json', root/'pending'), 0)
            self.assertEqual(collect.call_count, 6)
            report = live.read_json(root/'health.json')
            self.assertGreater(report['seconds'], 14 * 60)
            self.assertEqual((report['committed'], report['deferred'], report['unfinished_games']), (6, 0, []))

    def test_failed_game_has_priority_across_hour_even_when_not_currently_due(self):
        with tempfile.TemporaryDirectory() as directory:
            nfl, stack, games, captured = self.fixture(directory, 3)
            initial = datetime.now(timezone.utc).replace(minute=5, second=0, microsecond=0)
            class Clock(datetime):
                current = initial
                @classmethod
                def now(cls, tz=None):
                    return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)
            stack.enter_context(patch.object(live, 'datetime', Clock))
            attempts = []
            def capture(resolution, *args):
                sid = resolution.game.schedule_id
                attempts.append(sid)
                if sid == '0' and Clock.current == initial:
                    raise TimeoutError('injected')
                return captured(resolution)
            stack.enter_context(patch.object(live, 'capture_one', side_effect=capture))
            stack.enter_context(patch.object(nfl, 'post_snapshot_with_retry',
                                       return_value={'status': 'stored', 'iteration_id': 1}))
            root = Path(directory)
            args = ('', '', True, 1, root/'health.json', root/'pending')
            self.assertEqual(live.run_parallel_nfl(*args, workers=1), 1)
            self.assertEqual(list(live.read_json(root/'nfl-backlog.json')['pending']), ['0'])
            Clock.current += timedelta(hours=1)
            attempts.clear()
            with patch.object(nfl, 'schedule_games_due', return_value=games[1:]):
                self.assertEqual(live.run_parallel_nfl(*args, workers=1), 0)
            self.assertEqual(attempts, ['0', '1', '2'])
            report = live.read_json(root/'health.json')
            self.assertEqual(report['carried_forward'], 1)
            self.assertEqual(report['current_cadence_due'], 2)
            self.assertEqual(report['scheduled_due'], 3)
            # Newly captured prices use this hour, not the missed hour.
            self.assertTrue(all(nfl.hourly_capture_slot(datetime.fromisoformat(row['captured_at']))
                                == nfl.hourly_capture_slot(Clock.current) for row in report['uploads']))

    def test_old_receipts_recover_previously_deferred_games(self):
        with tempfile.TemporaryDirectory() as directory:
            nfl, stack, games, capture = self.fixture(directory, 3)
            root = Path(directory)
            prior = nfl.hourly_capture_slot(datetime.now(timezone.utc)) - timedelta(hours=1)
            live.write_json(root/'nfl-committed.json', {'slot': prior.isoformat(), 'completed': {
                '0|' + games[0].event_date.isoformat(): {'iteration_id': 1, 'url': 'unused'}}})
            stack.enter_context(patch.object(nfl, 'schedule_games_due', side_effect=lambda items, slot:
                games if slot == prior else [games[0]]))
            attempts = []
            def record(resolution, *args):
                attempts.append(resolution.game.schedule_id)
                return capture(resolution)
            stack.enter_context(patch.object(live, 'capture_one', side_effect=record))
            stack.enter_context(patch.object(nfl, 'post_snapshot_with_retry',
                                       return_value={'status': 'stored', 'iteration_id': 2}))
            self.assertEqual(live.run_parallel_nfl('', '', True, 1, root/'health.json', root/'pending', workers=1), 0)
            self.assertEqual(attempts, ['1', '2', '0'])

    def test_resolver_failure_keeps_all_games_checkpointed(self):
        with tempfile.TemporaryDirectory() as directory:
            nfl, stack, games, captured = self.fixture(directory, 3)
            stack.enter_context(patch.object(nfl, 'resolve_schedule_games', side_effect=RuntimeError('network')))
            root = Path(directory)
            with self.assertRaises(RuntimeError):
                live.run_parallel_nfl('', '', True, 1, root/'health.json', root/'pending')
            self.assertEqual(set(live.read_json(root/'nfl-backlog.json')['pending']), {'0', '1', '2'})


class MLBMetadataTests(unittest.TestCase):
    def test_started_game_is_identified_before_waiting_for_listings(self):
        browser = object.__new__(mlb_live.UpcomingMLBBrowser)
        event_date = datetime.now(timezone.utc) - timedelta(hours=2)
        with patch.object(mlb_live.mlb.VividBrowser, '_event_datetime', return_value=event_date):
            with self.assertRaises(mlb_live.OutsideCaptureWindow) as caught:
                browser._event_datetime('unused')
        self.assertEqual(caught.exception.reason, 'event-has-started')
        self.assertFalse(mlb_live.mlb.event_metadata_is_still_rendering(caught.exception))

    def test_upcoming_today_is_not_filtered_out(self):
        browser = object.__new__(mlb_live.UpcomingMLBBrowser)
        event_date = datetime.now(timezone.utc) + timedelta(minutes=30)
        with patch.object(mlb_live.mlb.VividBrowser, '_event_datetime', return_value=event_date):
            self.assertEqual(browser._event_datetime('unused'), event_date)

    def test_unknown_metadata_remains_a_real_failure_not_a_fake_skip(self):
        browser = object.__new__(mlb_live.UpcomingMLBBrowser)
        with patch.object(mlb_live.mlb.VividBrowser, '_event_datetime', side_effect=ValueError('not loaded')):
            with self.assertRaises(ValueError):
                browser._event_datetime('unused')


class MapCacheTests(unittest.TestCase):
    def test_matching_rules_and_geometry_unchanged(self):
        import nfl_metadata as maps
        labels = ['101', 'Section 102', 'C136', '136', 'Upper Deck 3']
        candidates = [None, 101, 'Sec 102', 'C136', '136', 'Deck 3', 'missing', {'strange': True}]
        expected = [maps.match_section_name(c, labels) for c in candidates]
        source = {'view_box':[0,0,100,100], 'sections':[
            {'name': label, 'path': 'M0 0 L10 0 L10 10 Z'} for label in labels]}
        geometry = maps.sanitize_map_geometry(copy.deepcopy(source), labels)
        original = maps.match_section_name
        with cached_map_matching() as memo:
            for _ in range(3):
                self.assertEqual([maps.match_section_name(c, labels) for c in candidates], expected)
                self.assertEqual(maps.sanitize_map_geometry(copy.deepcopy(source), labels), geometry)
            self.assertGreater(memo.cache_info().hits, 0)
        self.assertIs(maps.match_section_name, original)

    def test_no_custom_batch_timeout_in_collector_template(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        flow = yaml.load((root/'docs/free-live-collect.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertNotIn('timeout-minutes', flow['jobs']['capture'])
        self.assertFalse(any(step.get('run', '').startswith('timeout ') for step in flow['jobs']['capture']['steps']))


class MLBDeliveryTests(unittest.TestCase):
    def test_one_failed_delivery_never_blocks_other_games(self):
        from contextlib import ExitStack
        import json
        import collector as mlb
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            urls = ['https://www.vividseats.com/game-9-29-2026--sports-mlb-baseball/production/'+str(i) for i in range(3)]
            at = datetime.now(timezone.utc) + timedelta(hours=2)
            attempts = []
            class Browser:
                def __init__(self, **kw): pass
                def discover_event_urls(self, _url): return set(urls)
                def capture(self, url): return {'url': url}, at
                def close(self): pass
            stack.enter_context(patch.object(mlb, 'VENUE_FEEDS', {'test': 'unused'}))
            stack.enter_context(patch.object(mlb, 'VividBrowser', Browser))
            stack.enter_context(patch.object(mlb_live, 'UpcomingMLBBrowser', Browser))
            stack.enter_context(patch.object(mlb, 'event_date_from_url', return_value=at))
            stack.enter_context(patch.object(mlb, 'replay_pending_snapshots', return_value=(0, True, [])))
            stack.enter_context(patch.object(mlb.SnapshotParser, 'parse', return_value=SimpleNamespace(title='Game', sections=[1,2])))
            stack.enter_context(patch.object(mlb, 'snapshot_to_payload', side_effect=lambda url,*a: {'url':url}))
            def queue(payload, folder):
                folder.mkdir(parents=True, exist_ok=True)
                path = folder/(payload['url'].split('/')[-1]+'.json')
                path.write_text(json.dumps(payload))
                return path
            stack.enter_context(patch.object(mlb, 'queue_snapshot', side_effect=queue))
            def deliver(_endpoint, _token, payload):
                attempts.append(payload['url'])
                if payload['url'] == urls[0]: raise RuntimeError('injected delivery error')
                return {'status':'stored'}
            stack.enter_context(patch.object(mlb, 'post_snapshot_with_retry', side_effect=deliver))
            result = mlb_live.run_remote_mlb('', '', True, 1, root/'health.json', root/'pending')
            report = live.read_json(root/'health.json')
            self.assertEqual(result, 1)
            self.assertEqual(attempts, urls)
            self.assertEqual((report['captured'], report['uploaded'], report['pending']), (3,2,1))
            self.assertEqual(report['unfinished_games'], [urls[0]])
