"""Offline progress migration, phase budgets, and old-observation delivery checks."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

import collector
import nfl_schedule_collector as nfl
import nhl_schedule_collector as nhl
from Flask_App.collection_cadence import half_hour_capture_slot
from tests.test_observation_receipt import proof
from tests.test_shared_capture import acknowledgment, payload
from tools.shared_capture import MirrorQueue, run_legacy
from tools.shared_capture_policy import checkpoint, game_value, replay_free_pending, run_owner

NOW = datetime(2026, 10, 8, 5, tzinfo=timezone.utc)


class SharedCapturePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.mirror = MirrorQueue(self.root/'mirror', 'nfl')
        self.pending = self.root/'pending'; self.pending.mkdir(); self.health = self.root/'health.json'

    def game(self, sport, identity, days=1):
        module = nfl if sport == 'nfl' else nhl
        away, home = ('Minnesota Vikings', 'New Orleans Saints') if sport == 'nfl' else ('Utah Mammoth', 'Boston Bruins')
        return getattr(module, 'Scheduled'+sport.upper()+'Game')(identity, NOW+timedelta(days=days), away, home, 'Arena', away+' at '+home)

    def controlled(self, module, games, *, clock=None, advance=None, failure=None):
        calls = []; captured = []
        def resolve(rows, feed, **kwargs):
            if advance: advance('resolve')
            calls.append('resolve:'+str(rows[0].schedule_id))
            return ([module.ScheduleResolution(game, (type('Candidate', (), {'url':
                'https://www.vividseats.com/game/production/'+str(6500000+games.index(game))})(),), 'verified-feed') for game in rows], [])
        def capture(resolution, **kwargs):
            identity = str(resolution.game.schedule_id); calls.append('capture:'+identity)
            if advance: advance('capture')
            if failure and identity == failure:
                raise RuntimeError('Provider failed')
            value = payload('nfl' if module is nfl else 'nhl', resolution.candidates[0].url.split('/')[-1], NOW)
            _url, _event, _stamp, snapshot = collector.snapshot_from_payload(value)
            if module is nhl:
                from nhl_collector import NHLEventSnapshot
                snapshot = NHLEventSnapshot(snapshot.source_id, snapshot.title, snapshot.venue, snapshot.sections)
            return value['source_url'], resolution.game.event_date, snapshot
        def queue(value, pending):
            captured.append(value); self.mirror.enqueue(value)
            return collector.queue_snapshot(value, pending)
        def post(endpoint, token, value, **kwargs):
            response = proof(value); self.mirror.acknowledge(value, 'pythonanywhere', response); return response
        stack = ExitStack()
        for key, value in {
            'fetch_schedule_games': Mock(return_value=(games, 'official')),
            'discover_'+('nfl' if module is nfl else 'nhl')+'_games': Mock(return_value=([], [])),
            'resolve_schedule_games': resolve, '_capture_resolution': capture,
            'queue_snapshot': queue, 'post_snapshot_with_retry': post,
            ('nfl' if module is nfl else 'nhl')+'_capture_is_due': lambda event, slot, identity: str(identity).startswith('current'),
            'schedule_games_due': lambda rows, slot: [g for g in rows if str(g.schedule_id).startswith('current')],
        }.items():
            stack.enter_context(patch.object(module, key, value))
        return stack, calls, captured

    def test_imports_old_backlog_once_current_first_fair_twenty_and_original_observation_time(self):
        games = [self.game('nfl', 'current-1')] + [self.game('nfl', 'older-'+str(i), 10) for i in range(21)]
        legacy = self.root/'free'; legacy.mkdir()
        oldslot = NOW-timedelta(days=1)
        old = {'pending': {g.schedule_id: {'game': game_value(g), 'first_due': oldslot.isoformat()} for g in games[1:]}}
        (legacy/'nfl-backlog.json').write_text(json.dumps(old))
        (legacy/'nfl-committed.json').write_text(json.dumps({'slot': oldslot.isoformat()}))
        state, calls, captures = self.controlled(nfl, games)
        with state:
            self.assertEqual(run_owner('nfl', nfl, self.mirror, 'endpoint', 'token', 45, self.health,
                self.pending, legacy=legacy, now=lambda: NOW), 1)
        self.assertEqual(calls[0:2], ['resolve:current-1', 'capture:current-1'])
        self.assertEqual(len(captures), 21)
        self.assertTrue(all(value['captured_at'] == NOW.isoformat() for value in captures))
        report = json.loads(self.health.read_text())
        self.assertEqual((report['current_due_selected'], report['recovery_selected']), (1, 20))
        self.assertEqual((report['scheduled_due'], report['scheduled_selected']), (22, 21))
        self.assertLess(report['coverage_percent'], 100)
        self.assertEqual(report['current_coverage_percent'], 100)
        self.assertEqual(len(report['unfinished_games']), 1)
        progress = self.mirror.root/'recovery/state.json'
        self.assertTrue(json.loads(progress.read_text())['legacy_migrated'])
        # Mutating an already consumed legacy checkpoint cannot re-add completed work.
        old['pending']['obsolete-new-entry'] = old['pending']['older-0']
        (legacy/'nfl-backlog.json').write_text(json.dumps(old))
        state, calls, captures = self.controlled(nfl, games)
        with state:
            run_owner('nfl', nfl, self.mirror, 'endpoint', 'token', 45, self.health, self.pending,
                legacy=legacy, now=lambda: NOW)
        self.assertEqual(len(captures), 1)
        self.assertNotIn('obsolete-new-entry', json.loads(progress.read_text())['pending'])

    def test_resolution_deadline_defers_remaining_without_backdating_and_restart_recovers(self):
        games = [self.game('nfl', 'current-'+str(i)) for i in range(3)]
        elapsed = [0]
        def advance(phase): elapsed[0] += 100
        state, calls, captures = self.controlled(nfl, games, advance=advance)
        with state:
            code = run_owner('nfl', nfl, self.mirror, 'endpoint', 'token', 45, self.health, self.pending,
                now=lambda: NOW, clock=lambda: elapsed[0], deadline_seconds=500)
        self.assertEqual(code, 1); self.assertEqual(len(captures), 2)
        self.assertNotIn('resolve:current-2', calls)
        report = json.loads(self.health.read_text())
        self.assertEqual(report['deferred_games'], ['current-2']); self.assertTrue(report['deadline_reached'])
        state, calls, captures = self.controlled(nfl, games)
        with state:
            self.assertEqual(run_owner('nfl', nfl, self.mirror, 'endpoint', 'token', 45, self.health,
                self.pending, now=lambda: NOW), 0)
        self.assertEqual(len(captures), 1)
        self.assertEqual(json.loads(self.health.read_text())['already_observed_current'], 2)

    def test_discovery_budget_and_interrupt_leave_honest_health_and_durable_backlog(self):
        games = [self.game('nfl', 'current-1')]; elapsed = [0]
        state, calls, captures = self.controlled(nfl, games)
        with state, patch.object(nfl, 'discover_nfl_games', side_effect=lambda *args: (elapsed.__setitem__(0, 230) or [], [])):
            self.assertEqual(run_owner('nfl', nfl, self.mirror, 'endpoint', 'token', 45, self.health,
                self.pending, now=lambda: NOW, clock=lambda: elapsed[0], deadline_seconds=400), 1)
        self.assertEqual(calls, []); self.assertEqual(json.loads(self.health.read_text())['captured'], 0)
        state, calls, captures = self.controlled(nfl, games)
        with state, patch.object(nfl, '_capture_resolution', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_owner('nfl', nfl, self.mirror, 'endpoint', 'token', 45, self.health,
                    self.pending, now=lambda: NOW)
        self.assertEqual(json.loads(self.health.read_text())['status'], 'degraded')
        self.assertIn('current-1', json.loads((self.mirror.root/'recovery/state.json').read_text())['pending'])

    def test_precise_nhl_exemption_and_provider_gap_categories_remain_visible(self):
        exempt = nhl.ScheduledNHLGame('2026020182', NOW+timedelta(days=10), 'Montreal Canadiens',
            'Winnipeg Jets', 'Princess Auto Stadium', 'Montreal Canadiens at Winnipeg Jets')
        ordinary = self.game('nhl', 'current-2')
        self.mirror = MirrorQueue(self.root/'nhl', 'nhl')
        state, calls, captures = self.controlled(nhl, [exempt, ordinary])
        with state, patch.object(nhl, '_capture_resolution', side_effect=nhl.NHLProviderGapError('Incomplete inventory')):
            self.assertEqual(run_owner('nhl', nhl, self.mirror, 'endpoint', 'token', 45, self.health,
                self.pending, now=lambda: NOW), 1)
        report = json.loads(self.health.read_text())
        self.assertEqual([g['schedule_id'] for g in report['excluded_games']], ['2026020182'])
        self.assertEqual((report['provider_gap_count'], report['failed']), (1, 0))
        self.assertEqual(report['unfinished_games'], ['current-2'])

    def test_nested_progress_is_included_in_payload_admission_budget(self):
        value = payload(captured=NOW); mirror = MirrorQueue(self.root/'bounded', 'nfl', byte_limit=7000)
        checkpoint(mirror.root/'recovery/state.json', {'pending': {}, 'padding': 'x'*4000}, mirror)
        with self.assertRaisesRegex(ValueError, 'budget'):
            mirror.enqueue(value)
        self.assertEqual(mirror.records(), [])

    def test_invalid_restored_progress_gets_failure_health_without_overwrite(self):
        path = self.mirror.root/'recovery/state.json'; path.parent.mkdir()
        path.write_text('{"sport":"nhl"}')
        with patch.object(nfl, 'fetch_schedule_games') as fetch:
            self.assertEqual(run_owner('nfl', nfl, self.mirror, 'endpoint', 'token', 45,
                self.health, self.pending, now=lambda: NOW), 1)
        fetch.assert_not_called()
        self.assertEqual(json.loads(self.health.read_text())['status'], 'degraded')
        self.assertEqual(path.read_text(), '{"sport":"nhl"}')

    def test_nested_native_retry_guard_keeps_provider_category_and_stops_more_browser_starts(self):
        from vivid_inventory import VividCaptureError
        for exhausted in (False, True):
            with self.subTest(exhausted=exhausted):
                games = [self.game('nfl', 'current-native')]
                elapsed = [0]; starts = []; requests = []
                class Browser:
                    def __init__(self, **kwargs):
                        starts.append(1); self.capture_diagnostics = {}
                    def capture(self, url, **kwargs):
                        requests.append(url)
                        self.capture_diagnostics = {'production_id': url.split('/')[-1], 'responses': []}
                        if exhausted: elapsed[0] = 350
                        category = 'provider-inventory-timeout' if exhausted else 'provider-inventory-not-found'
                        raise VividCaptureError(category, self.capture_diagnostics, retryable=exhausted)
                    def close(self): pass
                real_capture = nfl._capture_resolution
                state, calls, captures = self.controlled(nfl, games)
                with state, patch.object(nfl, '_capture_resolution', real_capture), \
                     patch.object(nfl, 'VividNFLBrowser', Browser):
                    self.assertEqual(run_owner('nfl', nfl, self.mirror, 'endpoint', 'token', 45,
                        self.health, self.pending, now=lambda: NOW, clock=lambda: elapsed[0], deadline_seconds=500), 1)
                self.assertEqual((len(starts), len(requests)), (1, 1))
                report = json.loads(self.health.read_text())
                if exhausted:
                    self.assertTrue(report['deadline_reached']); self.assertEqual(report['failed'], 0)
                else:
                    self.assertEqual(report['capture_failures'][0]['category'], 'provider-inventory-not-found')
                self.assertEqual(report['unfinished_games'], ['current-native'])

    def test_old_free_pending_replays_original_age_independently_and_bounds_remaining(self):
        storage = ModuleType('tools.free_refresh_capture')
        calls = []
        def parse(sport, value, now=None):
            stamp = datetime.fromisoformat(value['captured_at'])
            if now-stamp > timedelta(days=7): raise ValueError('Too old')
            if value['event_type'] != sport: raise ValueError('Wrong sport')
            return collector.snapshot_from_payload(value)
        storage.parse_payload = parse
        pending = self.root/'free/pending'; pending.mkdir(parents=True)
        old = payload(captured=NOW-timedelta(days=10)); newer = payload(pid='6491666', captured=NOW-timedelta(days=9))
        # The old free collector allowed one valid section; only TiDB replay retains that scope.
        old['sections'] = old['sections'][:1]; old['section_count'] = 1
        (pending/'old.json').write_text(json.dumps(old)); (pending/'newer.json').write_text(json.dumps(newer))
        def sender(value):
            storage.parse_payload('nfl', value, now=NOW)
            calls.append(value['captured_at'])
            if value['source_id'] == newer['source_id']: raise RuntimeError('Temporary failure')
            return acknowledgment(value, 'tidb')
        with patch.dict(sys.modules, {'tools.free_refresh_capture': storage}), \
             patch.dict(os.environ, {}, clear=True):
            result = replay_free_pending('nfl', pending.parent, sender=sender, now=lambda: NOW)
        self.assertEqual((result['delivered'], result['pending']), (1, 1)); self.assertEqual(len(result['errors']), 1)
        self.assertFalse((pending/'old.json').exists()); self.assertEqual(json.loads((pending/'newer.json').read_text()), newer)
        self.assertIn(old['captured_at'], calls)
        with patch.dict(sys.modules, {'tools.free_refresh_capture': storage}), patch.dict(os.environ, {}, clear=True):
            result = replay_free_pending('nfl', pending.parent, sender=Mock(), now=lambda: NOW, budget=0)
        self.assertEqual(result['pending'], 1); self.assertFalse(result['changed'])
        with patch.dict(sys.modules, {'tools.free_refresh_capture': storage}), \
             patch.dict(os.environ, {'COLLECTOR_INGEST_TOKEN': 'no-mixed-scope'}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'separate TiDB'):
                replay_free_pending('nfl', pending.parent, sender=Mock())

    def test_rejected_pa_pending_is_not_duplicated_on_mirror_rehydration(self):
        value = payload(captured=NOW); self.mirror.enqueue(value)
        path = collector.queue_snapshot(value, self.pending); path.rename(path.with_suffix('.rejected'))
        def runner(endpoint, token, headless, timeout, health, pending):
            self.assertEqual(len(list(pending.glob('*.rejected'))), 1)
            self.assertEqual(len(list(pending.glob('*.json'))), 0)
            health.write_text(json.dumps({'status': 'queued'})); return 1
        with patch.dict(os.environ, {'COLLECTOR_INGEST_TOKEN': 'test'}, clear=True):
            self.assertEqual(run_legacy('nfl', self.mirror.root, self.pending, self.health, runner=runner), 1)


if __name__ == '__main__':
    unittest.main()
