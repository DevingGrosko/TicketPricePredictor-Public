"""Offline checks of the single owner's clocks, gates, and credential boundaries."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'e68c46746b50c5c1b529318940085492bcbe3ceb'


def workflow(name):
    return (ROOT / '.github/workflows' / (name + '.yml')).read_text()


def job(text, name):
    value = text.split('\n  ' + name + ':\n', 1)[1]
    return re.split(r'(?m)^  [a-z][a-z-]*:\n', value, maxsplit=1)[0]


def step(text, name):
    value = text.split('      - name: ' + name + '\n', 1)[1]
    return re.split(r'(?m)^      - ', value, maxsplit=1)[0]


class CaptureCutoverWorkflowTests(unittest.TestCase):
    def test_recurring_defaults_retain_manual_engines_mlb_pause_and_existing_clocks(self):
        owner = workflow('collect-ticket-prices')
        header = owner.split('\njobs:\n', 1)[0]
        self.assertNotIn('\n  push:', header)
        self.assertIn('cron: "53 */6 * * *"', header)
        self.assertIn('options: [chrome, firefox, webkit]\n        default: webkit', header)
        self.assertRegex(header, r'shared_capture:\n(?:        .*\n)*?        default: true\n')
        self.assertRegex(header, r'manual_repair:\n(?:        .*\n)*?        default: false\n')
        self.assertIn('if: ${{ false }}', job(owner, 'collect-baseball'))
        self.assertIn("cron: '7,37 * * * *'", workflow('free-ticket-collect'))
        self.assertIn("cron: '17,47 * * * *'", workflow('free-ticket-site'))

    def test_slot_checks_run_after_checkout_inside_each_existing_writer_lock(self):
        owner = workflow('collect-ticket-prices')
        for sport in ('nfl', 'nhl'):
            with self.subTest(sport=sport):
                capture = job(owner, 'collect-' + sport)
                self.assertLess(capture.index('uses: actions/checkout'), capture.index('id: cadence'))
                self.assertIn('persist-credentials: false', capture)
                self.assertIn('group: ' + sport + '-ticket-price-collector', capture)
                self.assertIn('cancel-in-progress: false', capture)
                self.assertIn('actions: read', capture)
                self.assertNotIn('actions: write', capture)
                self.assertIn("SHARED_CAPTURE: ${{ github.event_name != 'workflow_dispatch' || inputs.shared_capture }}", capture)
                self.assertIn("TICKETSIGNAL_BROWSER_ENGINE: ${{ inputs.browser_engine || 'webkit' }}", capture)
                self.assertIn("env.TICKETSIGNAL_BROWSER_ENGINE == 'webkit'", capture)
                self.assertNotIn('TIDB_STAGING_PASSWORD', capture)
                self.assertNotIn('continue-on-error:', capture)

    def test_actual_gate_script_constrains_bypass_to_explicit_manual_repair(self):
        owner = workflow('collect-ticket-prices')
        for sport in ('nfl', 'nhl'):
            gate = step(job(owner, 'collect-' + sport), 'Select the half-hour ' + sport.upper() + ' slot')
            script = textwrap.dedent(gate.split('        run: |\n', 1)[1])
            for event, repair, delivery in [('schedule', 'true', 'false'),
                                             ('schedule', 'true', 'true'),
                                             ('workflow_dispatch', 'false', 'false'),
                                             ('workflow_dispatch', 'true', 'false'),
                                             ('workflow_dispatch', 'false', 'true')]:
                with self.subTest(sport=sport, event=event, repair=repair, delivery=delivery), \
                     tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    command = root / 'python'
                    command.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$CALL_LOG"\necho run=false >> "$GITHUB_OUTPUT"\n')
                    command.chmod(0o700)
                    output, calls = root / 'output', root / 'calls'
                    env = {**os.environ, 'PATH': str(root) + os.pathsep + os.environ.get('PATH', ''),
                           'EVENT_NAME': event, 'MANUAL_REPAIR': repair, 'DELIVERY_ONLY': delivery,
                           'GITHUB_OUTPUT': str(output), 'CALL_LOG': str(calls)}
                    subprocess.run(['bash', '-e', '-c', script], env=env, check=True,
                                   capture_output=True, text=True, timeout=5)
                    if delivery == 'true' and event == 'workflow_dispatch':
                        self.assertFalse(calls.exists())
                        self.assertEqual(output.read_text(), 'run=true\n')
                    else:
                        args = calls.read_text().splitlines()
                        self.assertEqual(args[:5], ['-m', 'tools.single_capture_owner', 'slot-gate', '--sport', sport])
                        self.assertEqual('--manual-repair' in args, event == 'workflow_dispatch' and repair == 'true')
                        self.assertEqual(output.read_text(), 'run=false\n')

    def test_failed_provider_command_keeps_attempt_marker_and_original_failure_code(self):
        owner = workflow('collect-ticket-prices')
        for sport in ('nfl', 'nhl'):
            capture = step(job(owner, 'collect-' + sport), 'Attempt shared ' + sport.upper() + ' capture for the half-hour')
            script = textwrap.dedent(capture.split('        run: |\n', 1)[1])
            with self.subTest(sport=sport), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                command = root / 'xvfb-run'
                command.write_text('#!/bin/bash\nexit 17\n')
                command.chmod(0o700)
                output = root / 'output'
                env = {**os.environ, 'PATH': str(root) + os.pathsep + os.environ.get('PATH', ''),
                       'GITHUB_OUTPUT': str(output)}
                result = subprocess.run(['bash', '-e', '-c', script], env=env,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 17)
                self.assertEqual(output.read_text(), 'attempted=true\n')

    def test_actual_capture_is_separate_from_zero_browser_saved_delivery_and_failures_still_mirror(self):
        owner = workflow('collect-ticket-prices')
        for sport in ('nfl', 'nhl'):
            with self.subTest(sport=sport):
                collect = job(owner, 'collect-' + sport)
                capture = step(collect, 'Attempt shared ' + sport.upper() + ' capture for the half-hour')
                replay = step(collect, 'Deliver saved ' + sport.upper() + ' observations to both stores')
                self.assertIn("!inputs.delivery_only", capture)
                self.assertIn('timeout-minutes: 26', capture)
                self.assertIn('timeout-minutes: 60', collect)
                self.assertNotIn('--saved-manifest', capture)
                self.assertLess(capture.index('echo "attempted=true"'), capture.index('xvfb-run'))
                self.assertIn("github.event_name == 'workflow_dispatch' && inputs.delivery_only", replay)
                self.assertIn('--saved-manifest docs/shared-observations/manifest-0400.json', replay)
                self.assertNotIn('xvfb-run', replay)
                mirror = job(owner, 'mirror-' + sport)
                self.assertIn("if: always() && needs.collect-" + sport + ".outputs.mirror == 'true'", mirror)
                self.assertIn('name: mirror-' + sport + '-staging', mirror)
                self.assertNotIn('workflow_dispatch', mirror)
                self.assertNotIn('success()', mirror)
                self.assertNotIn('secrets: inherit', mirror)
                self.assertNotIn('COLLECTOR_INGEST_TOKEN', mirror)
                self.assertIn('source_ref: ' + SOURCE, mirror)
                self.assertIn('permissions:\n      contents: read\n      actions: read', mirror)
                self.assertNotIn('actions: write', mirror)

    def test_backup_dispatches_only_owner_and_preserves_old_free_state(self):
        backup = workflow('free-ticket-collect')
        self.assertIn('python -m tools.single_capture_owner dispatch-backup', backup)
        self.assertIn('actions: write', job(backup, 'backup'))
        for forbidden in ('free_live_http_diagnostics', 'free_live_storage', 'TIDB_STAGING_',
                          'xvfb-run', 'secrets.', 'actions/cache/', 'environment: tidb-staging', '\n  capture:'):
            self.assertNotIn(forbidden, backup)
        self.assertNotIn('\n  push:', backup)

    def test_old_free_pending_state_is_restored_for_one_owner_and_replayed_under_the_tidb_lock(self):
        owner = workflow('collect-ticket-prices')
        mirror = job(workflow('shared-snapshot-mirror'), 'mirror')
        for sport in ('nfl', 'nhl'):
            with self.subTest(sport=sport):
                capture = job(owner, 'collect-' + sport)
                restore = step(capture, 'Restore old free ' + sport.upper() + ' pending state for the same owner')
                self.assertIn('path: free-capture/' + sport, restore)
                self.assertIn('restore-keys: ticketsignal-free-v1-state-' + sport + '-', restore)
                self.assertIn('actions/cache/restore', restore)
                self.assertNotIn('actions/cache/save', capture.split('      - name: Restore old free ', 1)[1].split('      - name: Restore independent', 1)[0])
                self.assertEqual(capture.count('--legacy-free-state free-capture/' + sport), 2)
                self.assertIn('path: shared-capture/' + sport + '/*.json', capture)
                self.assertNotIn('path: shared-capture/' + sport + '/**', capture)
        self.assertIn('cp bridge/tools/shared_capture_policy.py source/tools/shared_capture_policy.py', mirror)
        self.assertIn('--legacy-free-state "../free-capture/$SPORT"', mirror)
        self.assertIn('restore-keys: ticketsignal-free-v1-state-${{ inputs.sport }}-', mirror)
        checkpoint = step(mirror, 'Checkpoint old free delivery replay under the same TiDB writer lock')
        self.assertIn("if: always() && steps.legacy-budget.outcome == 'success'", checkpoint)
        self.assertIn('path: free-capture/${{ inputs.sport }}', checkpoint)
        self.assertIn('key: ticketsignal-free-v1-state-${{ inputs.sport }}-shared-${{ github.run_id }}-${{ github.run_attempt }}', checkpoint)

    def test_publication_tracks_owner_completion_and_preserves_freshness_verification(self):
        publisher = workflow('free-ticket-site')
        self.assertIn('workflows: [Collect ticket prices]', publisher)
        self.assertIn('FREE_SOURCE_REF: ' + SOURCE, publisher)
        ready = job(publisher, 'ready')
        decision = step(ready, 'Publish after a canonical capture or real delivery attempt')
        self.assertIn('OWNER_RUN_ID: ${{ github.event.workflow_run.id }}', decision)
        self.assertIn('publication-gate --owner-run-id "$OWNER_RUN_ID"', decision)
        self.assertNotIn('COLLECTION_RUN_ID', decision)
        self.assertNotIn('secrets.', decision)
        self.assertIn('python -m tools.free_live_watchdog', ready)
        self.assertIn('FREE_LIVE_VERIFICATION_REPORT: publication-verification.json', publisher)
        self.assertIn("if: always() && steps.verification.outputs.version_verified == 'true'", publisher)

    def test_queue_budgets_and_scoped_postrun_pruning_never_remove_old_free_cache_namespaces(self):
        owner = workflow('collect-ticket-prices')
        for sport in ('nfl', 'nhl'):
            capture = job(owner, 'collect-' + sport)
            self.assertIn('queue-budget --directory shared-capture/' + sport, capture)
            self.assertIn('queue-budget --directory ' + sport + '_pending', capture)
            checkpoint = step(capture, 'Checkpoint the ' + sport.upper() + ' shared queue even after a failed capture')
            self.assertIn("if: always() && steps.shared-budget.outcome == 'success'", checkpoint)
            self.assertIn('github.run_attempt', checkpoint)
        cleanup = job(owner, 'preserve-shared-cache-budget')
        self.assertIn('needs: [collect-nfl, collect-nhl, mirror-nfl, mirror-nhl]', cleanup)
        self.assertIn('actions: write', cleanup)
        self.assertIn('tools.shared_capture_storage prune-caches', cleanup)
        self.assertNotIn('secrets.', cleanup)
        self.assertNotIn('free_live_storage', cleanup)
        mirror = job(workflow('shared-snapshot-mirror'), 'mirror')
        self.assertIn('queue-budget --directory "../shared-acknowledgments/$SPORT"', mirror)
        self.assertIn('group: free-refresh-staging-${{ inputs.sport }}-writer', mirror)
        self.assertIn('actions: read', mirror)
        self.assertNotIn('actions: write', mirror)
        self.assertNotIn('xvfb-run', mirror)


if __name__ == '__main__':
    unittest.main()
