from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.browser_capture_canary import events_from_json, run_canary, safe_diagnostics

EVENTS = [{'sport': 'nfl', 'url': 'https://www.vividseats.com/game/production/1234567'},
          {'sport': 'nhl', 'url': 'https://www.vividseats.com/game/production/7302493'}]


class Browser:
    def __init__(self, failures=None):
        self.driver = SimpleNamespace(capabilities={'browserName': 'firefox', 'browserVersion': '156.0'})
        self.urls, self.closed, self.failures = [], False, failures or set()
        self.capture_diagnostics = {}

    def capture(self, url):
        self.urls.append(url)
        if url in self.failures:
            raise TimeoutError('private-error-text-must-not-enter-report')
        pid = url.rsplit('/', 1)[-1]
        name = ('Dallas Cowboys at New York Giants' if pid == '1234567' else
                'Minnesota Vikings at New Orleans Saints' if pid == '6493143' else 'Utah Mammoth at Boston Bruins')
        raw = {'global': [{'productionId': pid, 'productionName': name, 'mapTitle': 'Test arena', 'listingCount': '10'}],
               'tickets': [{'l': f'Section {100+i}', 'p': str(70+i), 'q': '2'} for i in range(10)]}
        return raw, datetime.now(timezone.utc) + timedelta(hours=24)

    def close(self):
        self.closed = True


class CanaryTests(unittest.TestCase):
    def test_normal_navigation_uses_only_explicit_known_routes_and_keeps_capture_wrapper(self):
        known = [{**EVENTS[0], 'url': 'https://www.vividseats.com/wrong-date-slug/production/6493143'}, EVENTS[1]]
        sessions = []
        def factory(**_kwargs):
            browser = Browser(); sessions.append(browser); return browser
        with tempfile.TemporaryDirectory() as directory, patch('vivid_firefox.configure_normal_navigation') as configure:
            report = run_canary(events_from_json(json.dumps(known)), directory, factory=factory, normal_navigation=True)
        self.assertEqual(report['status'], 'passed')
        self.assertTrue(report['normal_navigation'])
        self.assertEqual(configure.call_count, 2)
        for call, browser in zip(configure.call_args_list, sessions):
            self.assertIs(call.args[0], browser)
            self.assertEqual(set(call.args[1]), {'6493143', '7302493'} if browser is sessions[0] else {'6493143'})
            self.assertTrue(call.args[1]['6493143'].endswith('/performer/597'))
            self.assertEqual(call.args[2]['6493143'], datetime(2026, 10, 11, 17, tzinfo=timezone.utc))
        self.assertEqual(len(sessions[0].urls), 3)
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            run_canary(events_from_json(json.dumps(EVENTS)), directory, normal_navigation=True,
                       factory=lambda **_kwargs: self.fail('Browser started for unknown route'))

    def test_isolated_events_each_have_one_capture_and_one_closed_session(self):
        values = [
            {**EVENTS[0], 'home_team': 'New York Giants', 'event_date': '2026-10-11T17:00:00Z'},
            {**EVENTS[1], 'home_team': 'Boston Bruins', 'event_date': '2026-10-08T23:00:00Z'},
            {'sport': 'nfl', 'url': 'https://www.vividseats.com/game/production/6493143', 'home_team': 'New Orleans Saints', 'event_date': '2026-10-11T17:00:00+00:00'},
            {'sport': 'nhl', 'url': 'https://www.vividseats.com/game/production/7301789', 'home_team': 'Pittsburgh Penguins', 'event_date': '2026-10-10T17:00:00+00:00'},
        ]
        events = events_from_json(json.dumps(values)); sessions = []
        dates = {event['production_id']: datetime.fromisoformat(event['event_date']) for event in events}
        class Scheduled(Browser):
            def capture(self, url):
                raw, _ = super().capture(url)
                return raw, dates[url.rsplit('/', 1)[-1]]
        def factory(**_kwargs):
            browser = Scheduled(); sessions.append(browser); return browser
        with tempfile.TemporaryDirectory() as directory, patch('vivid_firefox.configure_normal_navigation') as configure:
            report = run_canary(events, directory, factory=factory, normal_navigation=True, isolated_events=True)
        self.assertEqual(report['status'], 'passed')
        self.assertTrue(report['isolated_events'])
        self.assertEqual(len(report['observations']), 4)
        self.assertEqual([browser.urls for browser in sessions], [[event['url']] for event in events])
        self.assertTrue(all(browser.closed for browser in sessions))
        for call, event in zip(configure.call_args_list, events):
            self.assertEqual(set(call.args[1]), {event['production_id']})
            self.assertEqual(call.args[2][event['production_id']], dates[event['production_id']])

    def test_schedule_fields_require_valid_observed_team_and_aware_paired_date(self):
        cases = [
            {**EVENTS[0], 'home_team': 'New York Giants'},
            {**EVENTS[0], 'event_date': '2026-10-11T17:00:00Z'},
            {**EVENTS[0], 'home_team': 'Unknown Team', 'event_date': '2026-10-11T17:00:00Z'},
            {**EVENTS[0], 'home_team': 'New York Giants', 'event_date': '2026-10-11T17:00:00'},
            {**EVENTS[0], 'home_team': 'New York Giants', 'event_date': 123},
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                events_from_json(json.dumps([value, EVENTS[1]]))

    def test_pacing_is_between_every_observation_including_restarted_session(self):
        trace = []
        class Paced(Browser):
            def capture(self, url):
                trace.append(('capture', url))
                return super().capture(url)
            def close(self):
                trace.append(('close', None))
                super().close()
        with tempfile.TemporaryDirectory() as directory, \
                patch('tools.browser_capture_canary.time.sleep', side_effect=lambda value: trace.append(('wait', value))):
            report = run_canary(events_from_json(json.dumps(EVENTS)), directory, pace_seconds=60,
                                factory=lambda **kwargs: Paced())
        self.assertEqual(report['pace_seconds'], 60)
        self.assertEqual([kind for kind, _ in trace],
                         ['capture', 'wait', 'capture', 'wait', 'capture', 'close', 'wait', 'capture', 'close'])
        self.assertEqual([value for kind, value in trace if kind == 'wait'], [60, 60, 60])
        self.assertEqual(report['status'], 'passed')
        self.assertEqual((report['database_calls'], report['upload_calls']), (0, 0))
        self.assertTrue(all('started_at' in row and 'finished_at' in row for row in report['observations']))

    def test_default_pacing_adds_no_sleep_or_capture_and_invalid_values_fail_before_startup(self):
        with tempfile.TemporaryDirectory() as directory, patch('tools.browser_capture_canary.time.sleep') as sleep:
            report = run_canary(events_from_json(json.dumps(EVENTS)), directory, factory=lambda **kwargs: Browser())
        sleep.assert_not_called()
        self.assertEqual(report['pace_seconds'], 0)
        self.assertEqual(len(report['observations']), 4)
        for pace in (-1, 91, True, 0.5, '60'):
            with self.subTest(pace=pace), tempfile.TemporaryDirectory() as directory, \
                    patch('tools.browser_capture_canary.time.sleep') as sleep:
                with self.assertRaises(ValueError):
                    run_canary(EVENTS, directory, pace_seconds=pace, factory=lambda **kwargs: self.fail('Browser started'))
                sleep.assert_not_called()

    def test_multiple_sports_repeat_and_restart_use_correct_context_and_close_browsers(self):
        with tempfile.TemporaryDirectory() as directory:
            sessions = []
            def factory(**kwargs):
                self.assertEqual(kwargs, {'headless': False, 'timeout': 45})
                browser = Browser(); sessions.append(browser); return browser
            report = run_canary(events_from_json(json.dumps(EVENTS)), directory, factory=factory)
            self.assertEqual(report['status'], 'passed')
            self.assertEqual(len(sessions), 2)
            self.assertEqual(sessions[0].urls, [EVENTS[0]['url'], EVENTS[1]['url'], EVENTS[0]['url']])
            self.assertEqual(sessions[1].urls, [EVENTS[0]['url']])
            self.assertTrue(all(browser.closed for browser in sessions))
            self.assertEqual((report['database_calls'], report['upload_calls']), (0, 0))
            self.assertEqual(len(list(Path(directory).glob('observation-*.json'))), 4)

    def test_one_failure_remains_red_without_blocking_other_event_or_cleanup(self):
        with tempfile.TemporaryDirectory() as directory, patch('tools.browser_capture_canary.time.sleep') as sleep:
            sessions = []
            def factory(**kwargs):
                browser = Browser({EVENTS[0]['url']}); sessions.append(browser); return browser
            report = run_canary(events_from_json(json.dumps(EVENTS)), directory, pace_seconds=90, factory=factory)
            self.assertEqual(report['status'], 'failed')
            self.assertEqual([row['status'] for row in report['observations']], ['failed', 'captured', 'failed', 'failed'])
            self.assertEqual(sleep.call_args_list, [unittest.mock.call(90)] * 3)
            self.assertTrue(all(browser.closed for browser in sessions))
            self.assertNotIn('private-error-text', json.dumps(report))

    def test_requires_distinct_public_ids_and_both_active_sports(self):
        cases = [EVENTS[:1], [{'sport': 'mlb', 'url': EVENTS[0]['url']}, EVENTS[1]],
                 [EVENTS[0], EVENTS[0]], [{**EVENTS[0], 'url': EVENTS[0]['url']+'?token=private'}, EVENTS[1]]]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                events_from_json(json.dumps(value))

    def test_workflow_keeps_explicit_firefox_override_and_existing_recurring_schedule(self):
        root = Path(__file__).resolve().parents[1]
        smoke = (root / '.github/workflows/nhl-smoke-test.yml').read_text()
        collect = (root / '.github/workflows/collect-ticket-prices.yml').read_text()
        self.assertIn("if: github.event_name == 'workflow_dispatch' && (inputs.mode == 'firefox_canary' || inputs.mode == 'webkit_canary')", smoke)
        self.assertIn('default: smoke', smoke)
        self.assertIn('default: webkit', collect)
        self.assertEqual(collect.count("TICKETSIGNAL_BROWSER_ENGINE: ${{ inputs.browser_engine || 'webkit' }}"), 2)
        self.assertIn('cron: "53 */6 * * *"', collect)
        self.assertIn('group: nfl-ticket-price-collector', collect)
        self.assertIn('group: nhl-ticket-price-collector', collect)
        self.assertIn('if: ${{ false }}', collect)  # MLB stays paused.
        self.assertIn('selenium==4.26.1', (root / 'requirements.txt').read_text())
        self.assertIn('selenium==4.50.0', (root / 'requirements-collector.txt').read_text())
        self.assertEqual(collect.count('Verify stock Firefox and geckodriver for a requested Firefox capture'), 2)
        self.assertIn('canary_pace_seconds:', smoke)
        self.assertIn("CANARY_PACE_SECONDS: ${{ inputs.canary_pace_seconds || '0' }}", smoke)
        self.assertIn('print(360 + 5 * pace)', smoke)
        self.assertIn('--pace-seconds "$CANARY_PACE_SECONDS"', smoke)

    def test_webkit_uses_native_delegate_and_stops_all_sessions_after_denial(self):
        from unittest.mock import Mock
        from vivid_inventory import VividCaptureError
        known = events_from_json(json.dumps([
            {'sport':'nfl','url':'https://www.vividseats.com/game/production/6493143'}, EVENTS[1]]))
        for denied in (False, True):
            sessions=[]
            class StockWebKit(Browser):
                def __init__(self):
                    super().__init__()
                    self._webkit_session=SimpleNamespace(configure_normal_navigation=Mock())
                    self.driver=SimpleNamespace()  # The adapter has no Selenium capabilities.
                def capture(self, url):
                    if denied:
                        raise VividCaptureError('provider-rate-limited', {})
                    return super().capture(url)
            def factory(**kwargs):
                browser=StockWebKit();sessions.append(browser);return browser
            with tempfile.TemporaryDirectory() as directory, patch('vivid_firefox.configure_normal_navigation') as firefox:
                report=run_canary(known,directory,engine='webkit',isolated_events=True,factory=factory)
            firefox.assert_not_called()
            self.assertEqual(report['status'],'failed' if denied else 'passed')
            self.assertEqual(len(sessions),1 if denied else 2)
            self.assertTrue(all(browser.closed for browser in sessions))
            self.assertTrue(report['normal_navigation'])
            for browser in sessions:
                browser._webkit_session.configure_normal_navigation.assert_called_once()
            if denied:self.assertTrue(report['stopped_after_access_denial'])

    def test_failure_report_keeps_category_and_safe_native_response_evidence(self):
        from vivid_inventory import VividCaptureError
        class Unavailable(Browser):
            def capture(self, url):
                self.capture_diagnostics = {'engine': 'firefox', 'production_id': url.rsplit('/', 1)[-1],
                    'document_status': 200, 'responses': [{'path': '/hermes/api/v1/listings', 'status': 404,
                    'protocol': 'h3', 'query': {'priceGroupId': '21', 'token': 'private'},
                    'request_header_names': ['accept', 'authorization']}], 'cookies': 'private'}
                raise VividCaptureError('provider-inventory-not-found', self.capture_diagnostics)
        with tempfile.TemporaryDirectory() as directory:
            report = run_canary(events_from_json(json.dumps(EVENTS)), directory,
                                factory=lambda **kw: Unavailable())
        row = report['observations'][0]
        self.assertEqual(row['category'], 'provider-inventory-not-found')
        self.assertFalse(row['retryable'])
        self.assertEqual(row['diagnostics']['responses'][0]['status'], 404)
        self.assertEqual(row['diagnostics']['responses'][0]['protocol'], 'h3')
        self.assertNotIn('private', json.dumps(report))
        self.assertNotIn('authorization', json.dumps(report))


if __name__ == '__main__':
    unittest.main()
