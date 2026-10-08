"""Actual free monkeypatch integration, without browser, credentials or database."""
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import nfl_collector
from tools.browser_capture_canary import events_from_json
from tools.free_browser_capture_canary import event_dates_from_json, run_free_canary
from tools import free_live_provider_recovery as recovery

EVENTS = [{'sport': 'nfl', 'url': 'https://www.vividseats.com/game/production/1234567'},
          {'sport': 'nhl', 'url': 'https://www.vividseats.com/game/production/7302493'}]


def dates_for(events, hours=12):
    stamp = datetime.now(timezone.utc) + timedelta(hours=hours)
    return {event['production_id']: stamp for event in events}


class FreeCanaryTests(unittest.TestCase):
    def test_actual_free_capture_method_wrapper_with_native_firefox_diagnostics_and_no_db(self):
        calls, browsers = [], []
        original = nfl_collector.VividNFLBrowser.capture
        def shared(browser, url, *, reload_page=False):
            self.assertEqual(browser.driver.capabilities['browserName'], 'firefox')
            pid = url.rsplit('/', 1)[-1]
            calls.append(pid)
            browser.capture_diagnostics = {'engine': 'firefox', 'production_id': pid,
                'document_status': 200, 'responses': [{'path': '/hermes/api/v1/listings', 'status': 200}]}
            name = 'Dallas Cowboys at New York Giants' if pid == '1234567' else 'Utah Mammoth at Boston Bruins'
            return {'global': [{'productionId': pid, 'productionName': name, 'mapTitle': 'Arena', 'listingCount': 10}],
                    'tickets': [{'l': f'Section {100+i}', 'p': str(70+i), 'q': '2'} for i in range(10)]}, \
                    datetime.now(timezone.utc) + timedelta(hours=12)
        def factory(**kwargs):
            # This uses the real class method that free recovery monkeypatches.
            browser = nfl_collector.VividNFLBrowser.__new__(nfl_collector.VividNFLBrowser)
            browser.driver = SimpleNamespace(capabilities={'browserName': 'firefox', 'browserVersion': '156.0'})
            browser.capture_diagnostics = {}
            browser.close = lambda: None
            browsers.append(browser)
            return browser
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(recovery, '_SHARED_CAPTURE', shared), \
             patch('sqlalchemy.create_engine', side_effect=AssertionError('No database in read-only canary')), \
             patch('sys.stdout', StringIO()):
            report = run_free_canary(events_from_json(json.dumps(EVENTS)), directory,
                                    event_dates=dates_for(events_from_json(json.dumps(EVENTS))), factory=factory)
            saved = json.loads((Path(directory) / 'report.json').read_text())
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(calls, ['1234567', '7302493', '1234567', '1234567'])
        self.assertEqual(len(browsers), 2)
        self.assertEqual(saved['free_integration']['delivery_enabled'], False)
        self.assertEqual((saved['database_calls'], saved['upload_calls']), (0, 0))
        self.assertTrue(saved['free_integration']['current_inventory_recovery'])
        self.assertTrue(all(row['eligible_at_start'] and not row['reload_used']
                            for row in report['recovery_observations']))
        self.assertIs(nfl_collector.VividNFLBrowser.capture, original)

    def test_free_wrappers_restore_class_after_unexpected_canary_failure(self):
        original = nfl_collector.VividNFLBrowser.capture
        def failed(*args, **kwargs):
            self.assertIsNot(nfl_collector.VividNFLBrowser.capture, original)
            raise RuntimeError('Unexpected canary failure')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                events = events_from_json(json.dumps(EVENTS))
                run_free_canary(events, directory, event_dates=dates_for(events), runner=failed)
        self.assertIs(nfl_collector.VividNFLBrowser.capture, original)

    def recovery_case(self, *, persistent=False, status=404, hours=12):
        from vivid_inventory import VividCaptureError
        events = events_from_json(json.dumps(EVENTS))
        calls, browsers, delays = [], [], []
        dates = dates_for(events, hours)
        def shared(browser, url, *, reload_page=False):
            pid = url.rsplit('/', 1)[-1]
            calls.append((pid, reload_page, browser.driver))
            browser.capture_diagnostics = {'engine': 'firefox', 'production_id': pid,
                'document_status': 200, 'responses': [{'path': '/hermes/api/v1/listings',
                'status': status if persistent or not reload_page else 200}], 'cookies': 'private'}
            if persistent or not reload_page:
                category = 'provider-inventory-not-found' if status == 404 else 'provider-access-denied'
                raise VividCaptureError(category, browser.capture_diagnostics)
            name = 'Dallas Cowboys at New York Giants' if pid == '1234567' else 'Utah Mammoth at Boston Bruins'
            return {'global': [{'productionId': pid, 'productionName': name, 'mapTitle': 'Arena', 'listingCount': 10}],
                    'tickets': [{'l': f'Section {100+i}', 'p': str(70+i), 'q': '2'} for i in range(10)]}, dates[pid]
        def factory(**kwargs):
            browser = nfl_collector.VividNFLBrowser.__new__(nfl_collector.VividNFLBrowser)
            browser.driver = SimpleNamespace(capabilities={'browserName': 'firefox', 'browserVersion': '157.0'})
            browser.capture_diagnostics = {}
            browser.closed = False
            browser.close = lambda: setattr(browser, 'closed', True)
            browsers.append(browser)
            return browser
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(recovery, '_SHARED_CAPTURE', shared), \
             patch('vivid_inventory.time.sleep', side_effect=delays.append), \
             patch('sqlalchemy.create_engine', side_effect=AssertionError('No database in read-only canary')), \
             patch('sys.stdout', StringIO()):
            report = run_free_canary(events, directory, event_dates=dates, factory=factory)
        self.assertTrue(all(browser.closed for browser in browsers))
        self.assertNotIn('private', json.dumps(report))
        return report, calls, delays

    def test_true_current_inventory_recovery_waits_then_reloads_same_inner_driver(self):
        report, calls, delays = self.recovery_case()
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(delays, [15] * 4)
        self.assertEqual(len(calls), 8)
        for first, second in zip(calls[::2], calls[1::2]):
            self.assertEqual(first[:2], (second[0], False))
            self.assertTrue(second[1])
            self.assertIs(first[2], second[2])
        self.assertTrue(all(row['reload_used'] and row['recovered'] and row['cooldown_seconds'] == 15
                            and [attempt['status'] for attempt in row['attempts']] == ['failed', 'captured']
                            for row in report['recovery_observations']))

    def test_persistent_404_is_red_after_one_reload_without_broad_retry(self):
        report, calls, delays = self.recovery_case(persistent=True)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(len(calls), 8)
        self.assertEqual(delays, [15] * 4)
        self.assertTrue(all(row['category'] == 'provider-inventory-not-found' and row['retryable'] is False
                            for row in report['observations']))
        self.assertTrue(all(row['reload_used'] and not row['recovered'] and len(row['attempts']) == 2
                            for row in report['recovery_observations']))

    def test_denial_and_noncurrent_event_do_not_reload(self):
        for kwargs in ({'persistent': True, 'status': 403}, {'persistent': True, 'hours': 8 * 24}):
            with self.subTest(kwargs=kwargs):
                report, calls, delays = self.recovery_case(**kwargs)
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(len(calls), 4)
                self.assertEqual(delays, [])
                self.assertTrue(all(not row['reload_used'] for row in report['recovery_observations']))

    def test_event_dates_require_exact_explicit_utc_mapping(self):
        events = events_from_json(json.dumps(EVENTS))
        mapping = {'1234567': '2026-10-11T17:00:00Z', '7302493': '2026-10-08T23:00:00Z'}
        dates = event_dates_from_json(json.dumps(mapping), events)
        self.assertEqual(dates['7302493'], datetime(2026, 10, 8, 23, tzinfo=timezone.utc))
        cases = [{}, {**mapping, '999': mapping['7302493']},
                 {**mapping, '7302493': '2026-10-08T23:00:00'},
                 {**mapping, '7302493': '2026-10-08T19:00:00-04:00'},
                 {**mapping, '7302493': 0}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                event_dates_from_json(json.dumps(value), events)

    def test_manual_workflow_has_no_delivery_credentials_or_scheduling(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / '.github/workflows/nhl-smoke-test.yml').read_text()
        self.assertIn('workflow_dispatch:', source)
        self.assertNotIn('schedule:', source)
        self.assertNotIn('push:', source)
        self.assertNotIn('pull_request:', source)
        self.assertNotIn('secrets.', source)
        self.assertNotIn('TIDB_STAGING_', source)
        self.assertNotIn('COLLECTOR_INGEST_TOKEN', source)
        self.assertIn('python -m tools.free_browser_capture_canary', source)
        self.assertIn('TICKETSIGNAL_BROWSER_ENGINE: firefox', source)
        self.assertIn('EVENT_DATES: ${{ inputs.event_dates }}', source)
        self.assertIn('--event-dates "$EVENT_DATES"', source)


if __name__ == '__main__':
    unittest.main()
