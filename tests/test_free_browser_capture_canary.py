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
from tools.browser_capture_canary import events_from_json, run_canary
from tools.free_browser_capture_canary import run_free_canary
from tools import free_live_provider_recovery as recovery

EVENTS = [{'sport': 'nfl', 'url': 'https://www.vividseats.com/game/production/1234567'},
          {'sport': 'nhl', 'url': 'https://www.vividseats.com/game/production/7302493'}]


class FreeCanaryTests(unittest.TestCase):
    def test_actual_free_capture_method_wrapper_with_native_firefox_diagnostics_and_no_db(self):
        from functools import partial
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
                                    runner=partial(run_canary, factory=factory))
            saved = json.loads((Path(directory) / 'report.json').read_text())
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(calls, ['1234567', '7302493', '1234567', '1234567'])
        self.assertEqual(len(browsers), 2)
        self.assertEqual(saved['free_integration']['delivery_enabled'], False)
        self.assertEqual((saved['database_calls'], saved['upload_calls']), (0, 0))
        self.assertIs(nfl_collector.VividNFLBrowser.capture, original)

    def test_free_wrappers_restore_class_after_unexpected_canary_failure(self):
        original = nfl_collector.VividNFLBrowser.capture
        def failed(*args, **kwargs):
            self.assertIsNot(nfl_collector.VividNFLBrowser.capture, original)
            raise RuntimeError('Unexpected canary failure')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                run_free_canary(EVENTS, directory, runner=failed)
        self.assertIs(nfl_collector.VividNFLBrowser.capture, original)

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


if __name__ == '__main__':
    unittest.main()
