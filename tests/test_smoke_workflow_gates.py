"""PR checks are offline; live Chrome and native canaries require manual dispatch."""
from pathlib import Path
import unittest

import yaml


class SmokeWorkflowGateTests(unittest.TestCase):
    def workflow(self, sport):
        path = Path(__file__).resolve().parents[1]/'.github/workflows'/f'{sport}-smoke-test.yml'
        return yaml.load(path.read_text(), Loader=yaml.BaseLoader)

    def test_named_pr_checks_validate_saved_payloads_and_native_lifecycle_offline(self):
        for sport in ('nfl', 'nhl'):
            with self.subTest(sport=sport):
                workflow = self.workflow(sport)
                self.assertIn('pull_request', workflow['on'])
                job = workflow['jobs']['capture-'+sport]
                steps = job['steps']
                install = next(step for step in steps if step.get('name') == 'Install offline collector test dependencies')
                self.assertEqual(install['if'], "github.event_name == 'pull_request'")
                self.assertIn('-r requirements.txt -r requirements-test.txt', install['run'])
                offline = next(step for step in steps if step.get('name', '').startswith('Validate saved'))
                self.assertEqual(offline['if'], "github.event_name == 'pull_request'")
                for required in ('tests.test_saved_collector_payloads.SavedCollectorPayloadTests.test_'+sport+'_original_observations',
                                 'tests.test_vivid_webkit', 'tests.test_'+sport+'_collector',
                                 'tests.test_'+sport+'_schedule_collector', 'tests.test_smoke_workflow_gates'):
                    self.assertIn(required, offline['run'])
                for forbidden in ('xvfb-run', ' remote-run', ' smoke', ' schedule ', 'tools.browser_capture_canary'):
                    self.assertNotIn(forbidden, offline['run'])
                self.assertNotIn('continue-on-error', offline)
                paths = workflow['on']['pull_request']['paths']
                self.assertIn('vivid_webkit.py', paths); self.assertIn('tests/test_vivid_webkit.py', paths)

    def test_legacy_live_chrome_and_schedule_calls_require_explicit_dispatch(self):
        for sport in ('nfl', 'nhl'):
            with self.subTest(sport=sport):
                steps = self.workflow(sport)['jobs']['capture-'+sport]['steps']
                for step in steps:
                    name = step.get('name', '')
                    if (name in ('Show browser versions', 'Install legacy Chrome diagnostic dependencies')
                            or 'schedule source' in name or name.startswith('Capture one')
                            or name.startswith('Require usable') or name.startswith('Validate NHL section')):
                        self.assertEqual(step['if'], "github.event_name == 'workflow_dispatch'", name)
                    if name == 'Upload diagnostic results':
                        self.assertEqual(step['if'], "always() && github.event_name == 'workflow_dispatch'")
                capture = next(step for step in steps if step.get('name', '').startswith('Capture one'))
                self.assertIn('legacy headed Chrome', capture['name'])
                self.assertIn('timeout --signal=TERM --kill-after=10s 120s', capture['run'])

    def test_manual_native_webkit_canary_is_retained_with_an_explicit_event_gate(self):
        workflow = self.workflow('nhl')
        job = workflow['jobs']['capture-nhl']
        self.assertIn("github.event_name == 'pull_request'", job['if'])
        self.assertIn("github.event_name == 'workflow_dispatch'", job['if'])
        canary = workflow['jobs']['firefox-canary']
        self.assertEqual(canary['if'], "github.event_name == 'workflow_dispatch' && (inputs.mode == 'firefox_canary' || inputs.mode == 'webkit_canary')")
        self.assertIn('webkit_canary', workflow['on']['workflow_dispatch']['inputs']['mode']['options'])
        install = next(step for step in canary['steps'] if step.get('name') == 'Install stock WebKit for the explicit WebKit canary')
        self.assertEqual(install['if'], "inputs.mode == 'webkit_canary'")
        self.assertIn('playwright install --with-deps webkit', install['run'])
        capture = next(step for step in canary['steps'] if step.get('name', '').startswith('Capture distinct'))
        self.assertIn('tools.browser_capture_canary --engine', capture['run'])


if __name__ == '__main__':
    unittest.main()
