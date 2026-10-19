"""Offline progress migration, phase budgets, and old-observation delivery checks."""
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta, timezone
import json
from io import StringIO
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
from tools.shared_capture_policy import capture_progress, checkpoint, game_value, replay_free_pending, replay_pa, run_owner

NOW = datetime(2026, 10, 8, 5, tzinfo=timezone.utc)


class SharedCapturePolicyTests(unittest.TestCase):
    def test_progress_overlay_actions_exclude_page_text_unknown_controls_and_extra_rows(self):
        output = StringIO()
        with redirect_stdout(output):
            capture_progress('nhl', '2026020090', 'stored', {}, 1, diagnostics={
                'phase': 'performer-overlay-controls', 'performer_overlay_actions': [
                    {'action': 'reject-optional-cookies', 'outcome': 'clicked', 'private': 'private'},
                    {'action': 'close-promotion', 'outcome': 'clicked'},
                    {'action': 'accept-all', 'outcome': 'private'}]})
        row = json.loads(output.getvalue().split(' ', 1)[1])
        self.assertEqual(row['diagnostics'], {'phase': 'performer-overlay-controls', 'performer_overlay_actions': [
            {'action': 'reject-optional-cookies', 'outcome': 'clicked'},
            {'action': 'close-promotion', 'outcome': 'clicked'}]})
        self.assertNotIn('private', output.getvalue())

    def test_progress_reports_only_native_iframe_escape_outcome(self):
        output = StringIO()
        with redirect_stdout(output):
            capture_progress('nfl', '401872997', 'failed', {}, 1, diagnostics={
                'performer_overlay_actions': [{'action': 'dismiss-html-promotion',
                    'outcome': 'escape-sent', 'frame_url': 'https://private/?token=secret'}]})
        self.assertEqual(json.loads(output.getvalue().split(' ', 1)[1])['diagnostics'], {
            'performer_overlay_actions': [{'action': 'dismiss-html-promotion', 'outcome': 'escape-sent'}]})
        self.assertNotIn('private', output.getvalue())

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

    def test_progress_redacts_untrusted_values_and_bounds_native_failure_evidence(self):
        output=StringIO()
        diagnostics={'production_id':'7301418','phase':'event-link-ready','timeout_phase':'event-link-ready',
            'performer_document_status':200,'document_status':True,
            'event_link_trial_timeout_reason':'pointer-intercepted','event_link_trial_attempts':2,
            'event_link_click_timeout_reason':'private-token',
            'event_link_actionability':{'attached':True,'has_box':1,'in_viewport':True,
                'receives_pointer':False,'obstructed_by_dialog':True,'text':'private'},
            'responses':[{'status':200,'url':'https://private.example/?token=private'},
                         {'status':404},{'status':True},{'status':999}]+[{'status':503}]*20,
            'cookies':'private','exception_text':'private'}
        with redirect_stdout(output):
            capture_progress('nhl','2026020088','failed',{'captured':0,'failed':1,'errors':['private']},
                12.34567,category='provider-inventory-timeout',diagnostics=diagnostics)
            capture_progress('nfl','private','failed',{'captured':True},float('nan'),
                category={'private':'private'},source_id='private',diagnostics={'event_link_trial_attempts':999})
        lines=output.getvalue().splitlines()
        self.assertTrue(all(line.startswith('SHARED_CAPTURE_PROGRESS ') for line in lines))
        self.assertNotIn('private',output.getvalue())
        row=json.loads(lines[0].split(' ',1)[1])
        self.assertEqual((row['schedule_id'],row['source_id']),('2026020088','7301418'))
        self.assertEqual(row['elapsed_seconds'],12.346)
        self.assertEqual(row['category'],'provider-inventory-timeout')
        self.assertEqual(row['diagnostics'],{
            'phase':'event-link-ready','timeout_phase':'event-link-ready','performer_document_status':200,
            'event_link_trial_timeout_reason':'pointer-intercepted','event_link_trial_attempts':2,
            'event_link_actionability':{'attached':True,'in_viewport':True,
                'receives_pointer':False,'obstructed_by_dialog':True},
            'inventory_http_statuses':[200,404]+[503]*6})
        self.assertEqual(json.loads(lines[1].split(' ',1)[1]),
            {'sport':'nfl','status':'failed','category':'other-failure'})

    def test_game_progress_emits_start_and_stored_counts_without_changing_captures(self):
        games=[self.game('nfl','current-1'),self.game('nfl','current-2')]
        state,calls,captures=self.controlled(nfl,games);output=StringIO()
        with state,redirect_stdout(output):
            code=run_owner('nfl',nfl,self.mirror,'endpoint','token',45,self.health,self.pending,now=lambda:NOW)
        rows=[json.loads(line.split(' ',1)[1]) for line in output.getvalue().splitlines()
              if line.startswith('SHARED_CAPTURE_PROGRESS ')]
        self.assertEqual(code,0);self.assertEqual(len(captures),2)
        self.assertEqual([row['status'] for row in rows],['started','stored','started','stored'])
        self.assertEqual([row['captured'] for row in rows],[0,1,1,2])
        self.assertEqual([row['uploaded'] for row in rows],[0,1,1,2])
        self.assertEqual(rows[-1]['captured_current'],2)
        self.assertEqual(rows[-1]['source_id'],'6500001')
        self.assertEqual(calls,['resolve:current-1','capture:current-1','resolve:current-2','capture:current-2'])

    def test_manual_diagnostic_stop_preserves_success_and_defers_remaining_for_normal_restart(self):
        games=[self.game('nfl','current-'+str(i)) for i in range(3)]
        state,calls,captures=self.controlled(nfl,games,failure='current-1')
        with state:
            code=run_owner('nfl',nfl,self.mirror,'endpoint','token',45,self.health,self.pending,
                now=lambda:NOW,blocker_diagnostics=True,stop_after_first_failure=True)
        report=json.loads(self.health.read_text())
        self.assertEqual(code,1);self.assertTrue(report['diagnostic_stopped'])
        self.assertFalse(report['deadline_reached']);self.assertEqual(report['status'],'degraded')
        self.assertEqual(report['deferred_games'],['current-2'])
        self.assertEqual(report['unfinished_games'],['current-1','current-2'])
        self.assertEqual(len(captures),1);self.assertNotIn('resolve:current-2',calls)
        original_path=self.mirror.records()[0][0];original=original_path.read_bytes()
        checkpoint=json.loads((self.mirror.root/'recovery/state.json').read_text())
        self.assertEqual(checkpoint['pending'], {})
        state,calls,captures=self.controlled(nfl,games)
        with state:
            self.assertEqual(run_owner('nfl',nfl,self.mirror,'endpoint','token',45,self.health,
                self.pending,now=lambda:NOW),0)
        self.assertEqual(len(captures),2);self.assertNotIn('capture:current-0',calls)
        self.assertEqual(original_path.read_bytes(),original)
        self.assertFalse(json.loads(self.health.read_text())['diagnostic_stopped'])

    def test_manual_diagnostic_stop_also_defers_after_unresolved_without_starting_browsers(self):
        games=[self.game('nfl','current-'+str(i)) for i in range(2)]
        state,calls,captures=self.controlled(nfl,games)
        with state,patch.object(nfl,'resolve_schedule_games',return_value=([],[])) as resolve:
            self.assertEqual(run_owner('nfl',nfl,self.mirror,'endpoint','token',45,self.health,self.pending,
                now=lambda:NOW,blocker_diagnostics=True,stop_after_first_failure=True),1)
        report=json.loads(self.health.read_text())
        self.assertEqual(resolve.call_count,1);self.assertEqual(captures,[])
        self.assertTrue(report['diagnostic_stopped']);self.assertEqual(report['deferred_games'],['current-1'])
        self.assertEqual(report['unfinished_games'],['current-0','current-1'])

    def test_diagnostic_options_reject_implicit_stop_before_any_work(self):
        with patch.object(nfl,'fetch_schedule_games') as fetch:
            with self.assertRaisesRegex(ValueError,'explicit blocker'):
                run_owner('nfl',nfl,self.mirror,'endpoint','token',45,self.health,self.pending,
                    stop_after_first_failure=True)
        fetch.assert_not_called();self.assertFalse(self.health.exists())

    def test_progress_includes_only_bounded_public_blocker_metadata(self):
        output=StringIO()
        with redirect_stdout(output):
            capture_progress('nfl','401872994','failed',{},1,diagnostics={
                'blocker_diagnostic_status':'metadata-only','blocker_element':{
                    'hit':{'tag':'IFRAME','id':'consent-overlay','src':'https://private?token=secret',
                        'classes':['overlay','https://private']},'hit_is_iframe':True,
                    'known_consent_present':True,'html':'secret','text':'private'},
                'blocker_screenshot_file':'/private/secret.png'})
        row=json.loads(output.getvalue().split(' ',1)[1])
        self.assertEqual(row['diagnostics'],{'blocker_diagnostic_status':'metadata-only',
            'blocker_element':{'hit':{'tag':'IFRAME','id':'consent-overlay','classes':['overlay']},
                'hit_is_iframe':True,'known_consent_present':True}})
        self.assertNotIn('private',output.getvalue());self.assertNotIn('secret',output.getvalue())

    def test_production_retries_share_one_screenshot_allowance(self):
        from vivid_inventory import VividCaptureError
        games=[self.game('nfl','current-1')];configured=[];starts=[]
        class Session:
            def configure_blocker_diagnostics(self,directory,*,screenshot_budget):
                configured.append((directory,screenshot_budget));self.budget=screenshot_budget
            def configure_normal_navigation(self,*args,**kwargs):pass
        class Browser:
            def __init__(self,**kwargs):
                starts.append(1);self._webkit_session=Session();self.capture_diagnostics={}
            def capture(self,url,**kwargs):
                self.capture_diagnostics={'phase':'event-link-ready'}
                self._webkit_session.budget.claim()
                raise VividCaptureError('provider-inventory-timeout',self.capture_diagnostics,retryable=True)
            def close(self):pass
        real_capture=nfl._capture_resolution
        state,calls,captures=self.controlled(nfl,games)
        with state,patch.object(nfl,'VividNFLBrowser',Browser),patch.object(nfl,'_capture_resolution',real_capture):
            self.assertEqual(run_owner('nfl',nfl,self.mirror,'endpoint','token',45,self.health,self.pending,
                now=lambda:NOW,blocker_diagnostics=True,stop_after_first_failure=True),1)
        self.assertGreaterEqual(len(starts),2)
        self.assertEqual(len({id(row[1]) for row in configured}),1)
        self.assertTrue(configured[0][1].attempted)
        self.assertTrue(all(row[0]==self.root/'nfl-blocker-diagnostics' for row in configured))

    def test_ignores_restored_and_legacy_scrape_backlogs_without_touching_observations(self):
        games = [self.game('nfl', 'current-1'), self.game('nfl', 'older-1', 10)]
        legacy = self.root/'free'; legacy.mkdir()
        oldslot = (NOW-timedelta(days=1)).isoformat()
        backlog = {'pending': {'older-1': {'game': game_value(games[1]), 'first_due': oldslot}}}
        oldfile = legacy/'nfl-backlog.json'; oldfile.write_text(json.dumps(backlog))
        oldbytes = oldfile.read_bytes()
        progress = self.mirror.root/'recovery/state.json'; progress.parent.mkdir()
        progress.write_text(json.dumps(dict(backlog, version=1, sport='nfl', last_slot=oldslot)))
        original = payload(pid='6491666', captured=NOW-timedelta(days=1))
        saved = self.mirror.enqueue(original); before = saved.read_bytes()
        state, calls, captures = self.controlled(nfl, games)
        with state:
            self.assertEqual(run_owner('nfl', nfl, self.mirror, 'endpoint', 'token', 45,
                self.health, self.pending, legacy=legacy, now=lambda: NOW), 0)
        self.assertEqual(calls, ['resolve:current-1', 'capture:current-1'])
        self.assertEqual(len(captures), 1)
        report = json.loads(self.health.read_text())
        self.assertEqual((report['recovery_selected'], report['discarded_capture_backlog']), (0, 1))
        self.assertEqual((report['scheduled_due'], report['scheduled_selected']), (1, 1))
        self.assertEqual(json.loads(progress.read_text())['pending'], {})
        self.assertEqual(saved.read_bytes(), before)
        self.assertEqual(oldfile.read_bytes(), oldbytes)

    def test_failed_game_waits_until_next_normal_due_slot_for_both_sports(self):
        for sport, module in [('nfl', nfl), ('nhl', nhl)]:
            with self.subTest(sport=sport):
                self.mirror = MirrorQueue(self.root/sport, sport)
                game = self.game(sport, 'current-1', 10)
                state, calls, captures = self.controlled(module, [game], failure='current-1')
                with state:
                    self.assertEqual(run_owner(sport, module, self.mirror, 'endpoint', 'token', 45,
                        self.health, self.pending, now=lambda: NOW), 1)
                self.assertEqual(json.loads(self.health.read_text())['unfinished_games'], ['current-1'])
                self.assertEqual(json.loads((self.mirror.root/'recovery/state.json').read_text())['pending'], {})
                state, calls, captures = self.controlled(module, [game])
                with state, patch.object(module, sport+'_capture_is_due', return_value=False):
                    self.assertEqual(run_owner(sport, module, self.mirror, 'endpoint', 'token', 45,
                        self.health, self.pending, now=lambda: NOW+timedelta(minutes=30)), 0)
                self.assertEqual(calls, []); self.assertEqual(captures, [])
                state, calls, captures = self.controlled(module, [game])
                with state:
                    self.assertEqual(run_owner(sport, module, self.mirror, 'endpoint', 'token', 45,
                        self.health, self.pending, now=lambda: NOW+timedelta(hours=6)), 0)
                self.assertEqual(len(captures), 1)
                self.assertEqual(captures[0]['captured_at'], (NOW+timedelta(hours=6)).isoformat())

    def test_missed_dispatch_does_not_backfill_old_slots(self):
        game = self.game('nfl', 'older-1', 10)
        progress = self.mirror.root/'recovery/state.json'; progress.parent.mkdir()
        progress.write_text(json.dumps({'version': 1, 'sport': 'nfl', 'pending': {},
            'last_slot': (NOW-timedelta(hours=12)).isoformat()}))
        state, calls, captures = self.controlled(nfl, [game])
        with state, patch.object(nfl, 'schedule_games_due', return_value=[game]):
            self.assertEqual(run_owner('nfl', nfl, self.mirror, 'endpoint', 'token', 45,
                self.health, self.pending, now=lambda: NOW), 0)
        self.assertEqual(calls, []); self.assertEqual(captures, [])
        self.assertEqual(json.loads(self.health.read_text())['recovery_selected'], 0)

    def test_game_no_longer_collectible_at_run_start_is_not_scraped(self):
        game = self.game('nfl', 'current-1')
        state, calls, captures = self.controlled(nfl, [game])
        with state, patch.object(nfl, 'nfl_is_within_capture_window', return_value=False):
            self.assertEqual(run_owner('nfl', nfl, self.mirror, 'endpoint', 'token', 45,
                self.health, self.pending, now=lambda: NOW), 0)
        self.assertEqual(calls, []); self.assertEqual(captures, [])
        self.assertEqual(json.loads(self.health.read_text())['no_longer_collectible'], ['current-1'])
        self.assertEqual(json.loads((self.mirror.root/'recovery/state.json').read_text())['pending'], {})

    def test_current_work_deadline_reports_misses_without_persisting_scrape_work(self):
        games = [self.game('nfl', 'current-'+str(i)) for i in range(45)]
        elapsed = [0]
        def advance(phase): elapsed[0] += 60
        state, calls, captures = self.controlled(nfl, games, advance=advance)
        with state:
            self.assertEqual(run_owner('nfl', nfl, self.mirror, 'endpoint', 'token', 45,
                self.health, self.pending, now=lambda: NOW, clock=lambda: elapsed[0]), 1)
        report = json.loads(self.health.read_text())
        self.assertEqual(report['recovery_selected'], 0)
        self.assertEqual(report['deadline_seconds'], 24*60)
        self.assertTrue(report['deadline_reached'])
        self.assertEqual(elapsed[0], 21*60)
        self.assertEqual(len(captures), 10)
        self.assertEqual(len(report['deferred_games']), 35)
        self.assertEqual(json.loads((self.mirror.root/'recovery/state.json').read_text())['pending'], {})

    def test_successful_capture_with_failed_upload_is_preserved_and_replayed_without_scraping(self):
        games = [self.game('nfl', 'current-1')]
        state, calls, captures = self.controlled(nfl, games)
        with state, patch.object(nfl, 'post_snapshot_with_retry', side_effect=RuntimeError('Temporary upload failure')):
            self.assertEqual(run_owner('nfl', nfl, self.mirror, 'endpoint', 'token', 45,
                self.health, self.pending, now=lambda: NOW), 1)
        original = captures[0]
        self.assertEqual(json.loads(self.health.read_text())['captured'], 1)
        self.assertEqual(len(list(self.pending.glob('*.json'))), 1)
        self.assertEqual(json.loads((self.mirror.root/'recovery/state.json').read_text())['pending'], {})
        state, calls, captures = self.controlled(nfl, games)
        with state, patch.object(nfl, 'nfl_capture_is_due', return_value=False):
            self.assertEqual(run_owner('nfl', nfl, self.mirror, 'endpoint', 'token', 45,
                self.health, self.pending, now=lambda: NOW+timedelta(minutes=30)), 0)
        self.assertEqual(calls, []); self.assertEqual(captures, [])
        self.assertEqual(json.loads(self.health.read_text())['replayed'], 1)
        self.assertEqual(list(self.pending.glob('*.json')), [])
        record = self.mirror.records()[0][1]
        self.assertEqual(record['payload'], original)
        self.assertIsNotNone(record['acknowledged']['pythonanywhere'])
        self.assertIsNone(record['acknowledged']['tidb'])

    def test_delivery_replay_remains_bounded_and_preserves_unattempted_payloads(self):
        values = [payload(pid=str(6500000+i), captured=NOW-timedelta(days=10)) for i in range(21)]
        for index, value in enumerate(values):
            (self.pending/f'{index:02}.json').write_text(json.dumps(value))
        with patch.object(nfl, 'post_snapshot_with_retry', side_effect=lambda endpoint, token, value, **kw: proof(value)) as post:
            count, errors, deferred = replay_pa(nfl, 'endpoint', 'token', self.pending, clock=lambda: 0)
        self.assertEqual((count, errors, deferred, post.call_count), (20, [], 1, 20))
        self.assertEqual(json.loads((self.pending/'20.json').read_text()), values[20])
        storage = ModuleType('tools.free_refresh_capture')
        storage.parse_payload = lambda sport, value, now=None: collector.snapshot_from_payload(value)
        pending = self.root/'free/pending'; pending.mkdir(parents=True)
        for index, value in enumerate(values):
            (pending/f'{index:02}.json').write_text(json.dumps(value))
        sender = Mock(side_effect=lambda value: acknowledgment(value, 'tidb'))
        with patch.dict(sys.modules, {'tools.free_refresh_capture': storage}), patch.dict(os.environ, {}, clear=True):
            result = replay_free_pending('nfl', pending.parent, sender=sender, now=lambda: NOW, clock=lambda: 0)
        self.assertEqual((result['delivered'], result['pending'], sender.call_count), (20, 1, 20))
        self.assertEqual(result['errors'], [])
        self.assertEqual(json.loads((pending/'20.json').read_text()), values[20])

    def test_two_capture_limit_spans_candidates_and_nested_same_browser_reloads(self):
        for sport, module in [('nfl', nfl), ('nhl', nhl)]:
            for fresh_browser in (False, True):
                with self.subTest(sport=sport, fresh_browser=fresh_browser):
                    self.mirror = MirrorQueue(self.root/f'{sport}-{fresh_browser}', sport)
                    games = [self.game(sport, 'current-1')]
                    requests, starts, closes = [], [], []
                    class Browser:
                        def __init__(self, **kwargs):
                            starts.append(1); self.capture_diagnostics = {}
                        def capture(self, url, **kwargs):
                            requests.append(url)
                            raise TimeoutError('Transient provider timeout')
                        def close(self): closes.append(1)
                    def nested_retry(resolution, **kwargs):
                        browser = None
                        try:
                            for attempt in range(5):
                                if browser is None or fresh_browser:
                                    if browser is not None: browser.close()
                                    browser = None
                                    browser = module.VividNFLBrowser()
                                try:
                                    browser.capture(resolution.candidates[0].url, reload_page=attempt > 0)
                                except TimeoutError:
                                    continue
                        finally:
                            if browser is not None: browser.close()
                        raise RuntimeError('Capture failed')
                    state, calls, captures = self.controlled(module, games)
                    with state, patch.object(module, 'VividNFLBrowser', Browser), \
                         patch.object(module, '_capture_resolution', nested_retry):
                        self.assertEqual(run_owner(sport, module, self.mirror, 'endpoint', 'token', 45,
                            self.health, self.pending, now=lambda: NOW), 1)
                    self.assertEqual(len(requests), 2)
                    self.assertEqual(len(starts), 2 if fresh_browser else 1)
                    self.assertEqual(len(closes), len(starts))
                    self.assertEqual(captures, [])
                    report = json.loads(self.health.read_text())
                    self.assertEqual(report['failed'], 1)
                    self.assertEqual(report['capture_attempt_limit'], 2)
                    self.assertEqual(report['capture_failures'][0]['category'], 'capture-attempt-limit')
                    self.assertEqual(json.loads((self.mirror.root/'recovery/state.json').read_text())['pending'], {})

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

    def test_discovery_budget_and_interrupt_leave_honest_health_without_scrape_backlog(self):
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
        self.assertEqual(json.loads((self.mirror.root/'recovery/state.json').read_text())['pending'], {})

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
                        self.capture_diagnostics = {'production_id': url.split('/')[-1], 'responses': [],
                            'phase':'event-link-ready','timeout_phase':'event-link-ready',
                            'event_link_trial_timeout_reason':'pointer-intercepted','event_link_trial_attempts':2,
                            'event_link_actionability':{'receives_pointer':False,'private':'secret'},
                            'exception_text':'private-secret'}
                        if exhausted: elapsed[0] = 350
                        category = 'provider-inventory-timeout' if exhausted else 'provider-inventory-not-found'
                        raise VividCaptureError(category, self.capture_diagnostics, retryable=exhausted)
                    def close(self): pass
                real_capture = nfl._capture_resolution
                state, calls, captures = self.controlled(nfl, games)
                output=StringIO()
                with state, patch.object(nfl, '_capture_resolution', real_capture), \
                     patch.object(nfl, 'VividNFLBrowser', Browser), redirect_stdout(output):
                    self.assertEqual(run_owner('nfl', nfl, self.mirror, 'endpoint', 'token', 45,
                        self.health, self.pending, now=lambda: NOW, clock=lambda: elapsed[0], deadline_seconds=500), 1)
                self.assertEqual((len(starts), len(requests)), (1, 1))
                report = json.loads(self.health.read_text())
                if exhausted:
                    self.assertTrue(report['deadline_reached']); self.assertEqual(report['failed'], 0)
                else:
                    self.assertEqual(report['capture_failures'][0]['category'], 'provider-inventory-not-found')
                self.assertEqual(report['unfinished_games'], ['current-native'])
                rows=[json.loads(line.split(' ',1)[1]) for line in output.getvalue().splitlines()
                      if line.startswith('SHARED_CAPTURE_PROGRESS ')]
                self.assertEqual([row['status'] for row in rows],['started','deferred' if exhausted else 'failed'])
                final=rows[-1]
                self.assertEqual(final['source_id'],'6500000')
                self.assertEqual(final['failed'],0 if exhausted else 1)
                self.assertEqual(final['category'],'capture-deferred-deadline' if exhausted else 'provider-inventory-not-found')
                self.assertEqual(final['diagnostics']['event_link_trial_timeout_reason'],'pointer-intercepted')
                self.assertEqual(final['diagnostics']['event_link_trial_attempts'],2)
                self.assertEqual(final['diagnostics']['event_link_actionability'],{'receives_pointer':False})
                self.assertNotIn('private',output.getvalue());self.assertNotIn('secret',output.getvalue())

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
