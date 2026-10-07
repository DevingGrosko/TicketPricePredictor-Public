"""Equivalent-slot fallback decisions preserve real and partial collections."""
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools.free_live_watchdog import (REPO, TARGET, collection_timer_decision,
                                      publication_needed, timer_gate)

NOW = datetime(2026, 10, 7, 19, 29, tzinfo=timezone.utc)


def row(identity=1, started='2026-10-07T19:00:20Z', status='completed', **extra):
    return dict(id=identity, path=TARGET, head_branch='main', run_started_at=started,
                status=status, **extra)


def capture(conclusion='failure', name='capture (nfl)'):
    return dict(name=name, status='completed', conclusion=conclusion)


def skipped_timer():
    return [capture('success', 'timer'), capture('skipped', 'capture')]


class TimerDecisionTests(unittest.TestCase):
    def decision(self, rows, jobs=None, now=NOW):
        lookup = Mock(return_value=[capture()] if jobs is None else jobs)
        result = collection_timer_decision(rows, now, 99, lookup)
        return result, lookup

    def test_failed_same_slot_attempt_blocks_only_redundant_fallback(self):
        (run, reason), lookup = self.decision([row()])
        self.assertFalse(run)
        self.assertIn('failures remain reported', reason)
        lookup.assert_called_once_with(1)

    def test_actual_start_controls_slot_even_when_dispatch_was_queued(self):
        record = row(started='2026-10-07T19:02:00Z', created_at='2026-10-07T18:30:00Z')
        self.assertFalse(self.decision([record])[0][0])
        self.assertTrue(self.decision([record], now=NOW.replace(minute=31))[0][0])

    def test_missed_slot_runs_and_prior_slot_does_not_block(self):
        for rows in ([], [row(started='2026-10-07T18:59:59Z')]):
            (run, _), lookup = self.decision(rows)
            self.assertTrue(run)
            lookup.assert_not_called()

    def test_active_or_queued_collection_blocks_duplicate_without_touching_jobs(self):
        for status in ('in_progress', 'queued', 'pending', 'waiting'):
            (run, _), lookup = self.decision([row(status=status)])
            self.assertFalse(run)
            lookup.assert_not_called()

    def test_current_run_other_branch_and_workflow_are_ignored(self):
        records = [row(identity=99, status='in_progress'),
                   {**row(status='queued'), 'head_branch': 'other'},
                   {**row(status='queued'), 'path': '.github/workflows/collect-ticket-prices.yml'}]
        self.assertTrue(self.decision(records)[0][0])

    def test_skipped_or_uncertain_capture_does_not_claim_slot_attempted(self):
        for jobs in ([capture('skipped', 'capture')], [], [capture(name='timer')], [capture('cancelled')]):
            self.assertTrue(self.decision([row()], jobs=jobs)[0][0])

    def test_publish_partial_failed_successful_and_uncertain_captures(self):
        for jobs in ([capture('failure')], [capture('success')],
                     [capture('skipped'), capture('failure', 'capture (nhl)')],
                     [capture('cancelled')], [], [capture(name='timer')],
                     [capture('failure', 'timer'), capture('skipped', 'capture')],
                     [capture('skipped', 'capture')]):
            self.assertTrue(publication_needed(jobs)[0])
        self.assertFalse(publication_needed(skipped_timer())[0])

    def test_external_manual_push_and_publisher_schedule_never_query_gate_history(self):
        for kind, events in [('collection', ('workflow_dispatch', 'push')),
                             ('publication', ('workflow_dispatch', 'push', 'schedule'))]:
            for event in events:
                with tempfile.TemporaryDirectory() as directory, \
                     patch.dict('os.environ', {'GITHUB_REPOSITORY': REPO, 'GITHUB_REF': 'refs/heads/main',
                         'GITHUB_EVENT_NAME': event, 'GITHUB_OUTPUT': str(Path(directory)/'output')}), \
                     patch('tools.free_refresh_storage.api') as api, redirect_stdout(io.StringIO()):
                    timer_gate(kind)
                    self.assertEqual((Path(directory)/'output').read_text(), 'run=true\n')
                    api.assert_not_called()

    def test_completed_skipped_capture_emits_explicit_publication_skip(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict('os.environ', {'GITHUB_REPOSITORY': REPO, 'GITHUB_REF': 'refs/heads/main',
                 'GITHUB_EVENT_NAME': 'workflow_run', 'COLLECTION_RUN_ID': '12',
                 'GITHUB_OUTPUT': str(Path(directory)/'output')}), \
             patch('tools.free_refresh_storage.api', return_value={'jobs': skipped_timer()}), \
             redirect_stdout(io.StringIO()) as output:
            timer_gate('publication')
            self.assertEqual((Path(directory)/'output').read_text(), 'run=false\n')
            self.assertIn('Duplicate collection timer skipped capture', output.getvalue())

    def test_workflow_templates_wire_gates_before_capture_build_and_preserve_health(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        collect = yaml.load((root/'docs/free-live-collect.yml').read_text(), Loader=yaml.BaseLoader)
        publish = yaml.load((root/'docs/free-live-publish.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(collect['jobs']['capture']['needs'], 'timer')
        self.assertEqual(collect['jobs']['capture']['if'], "needs.timer.outputs.run == 'true'")
        self.assertIn('--collection-gate', str(collect['jobs']['timer']))
        self.assertIn('--publication-gate', str(publish['jobs']['ready']))
        self.assertEqual(publish['jobs']['build']['if'], "needs.ready.outputs.run == 'true'")
        self.assertNotIn('conclusion', publish['jobs']['ready']['if'])
        self.assertIn('tools.free_live_verify', str(publish['jobs']['deploy']))
        self.assertNotIn('continue-on-error', str(publish['jobs']['deploy']))


if __name__ == '__main__':
    unittest.main()
