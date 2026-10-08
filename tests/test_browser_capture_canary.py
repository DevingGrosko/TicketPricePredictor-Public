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
        name = 'Dallas Cowboys at New York Giants' if pid == '1234567' else 'Utah Mammoth at Boston Bruins'
        raw = {'global': [{'productionId': pid, 'productionName': name, 'mapTitle': 'Test arena', 'listingCount': '10'}],
               'tickets': [{'l': f'Section {100+i}', 'p': str(70+i), 'q': '2'} for i in range(10)]}
        return raw, datetime.now(timezone.utc) + timedelta(hours=24)

    def close(self):
        self.closed = True


class CanaryTests(unittest.TestCase):
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

    def test_workflow_keeps_firefox_manual_only_and_existing_recurring_schedule(self):
        root = Path(__file__).resolve().parents[1]
        smoke = (root / '.github/workflows/nhl-smoke-test.yml').read_text()
        collect = (root / '.github/workflows/collect-ticket-prices.yml').read_text()
        self.assertIn("if: github.event_name == 'workflow_dispatch' && inputs.mode == 'firefox_canary'", smoke)
        self.assertIn('default: smoke', smoke)
        self.assertIn('default: chrome', collect)
        self.assertEqual(collect.count("TICKETSIGNAL_BROWSER_ENGINE: ${{ inputs.browser_engine || 'chrome' }}"), 2)
        self.assertIn('cron: "53 */6 * * *"', collect)
        self.assertIn('group: nfl-ticket-price-collector', collect)
        self.assertIn('group: nhl-ticket-price-collector', collect)
        self.assertIn('if: ${{ false }}', collect)  # MLB stays paused.
        self.assertIn('selenium==4.26.1', (root / 'requirements.txt').read_text())
        self.assertIn('selenium==4.50.0', (root / 'requirements-collector.txt').read_text())
        self.assertEqual(collect.count('Verify stock Firefox and geckodriver for this manual trial'), 2)
        self.assertIn('canary_pace_seconds:', smoke)
        self.assertIn("CANARY_PACE_SECONDS: ${{ inputs.canary_pace_seconds || '0' }}", smoke)
        self.assertIn('print(360 + 5 * pace)', smoke)
        self.assertIn('--pace-seconds "$CANARY_PACE_SECONDS"', smoke)

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
