"""Empty exports remain explicit while missing/corrupt artifacts fail closed."""
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.test_shared_capture import acknowledgment, payload
from tools.shared_capture import EXPORT_MANIFEST, MirrorQueue, deliver_tidb, export_observations, run_legacy, validate_export


class SharedCaptureExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.output, self.destination = self.root/'capture', self.root/'export', self.root/'tidb'

    def test_genuinely_empty_first_capture_downloads_and_delivers_without_record_json(self):
        health = self.root/'health.json'
        def healthy_empty(_endpoint, _token, _headless, _timeout, path, _pending):
            path.write_text(json.dumps({'status': 'healthy', 'scheduled_due': 0, 'captured': 0}))
            return 0
        with patch.dict(os.environ, {'COLLECTOR_INGEST_TOKEN': 'test'}, clear=True), patch('sys.stdout', StringIO()):
            self.assertEqual(run_legacy('nfl', self.source, self.root/'pending', health, runner=healthy_empty), 0)
        report = export_observations('nfl', self.source, self.output)
        self.assertEqual(report['records'], 0)
        self.assertEqual({p.name for p in self.output.iterdir()}, {EXPORT_MANIFEST})
        self.assertEqual(validate_export('nfl', self.output), report)
        sender = Mock(side_effect=AssertionError('No payload in empty capture'))
        with patch('sys.stdout', StringIO()):
            self.assertEqual(deliver_tidb('nfl', self.destination, self.output, sender=sender), 0)
        sender.assert_not_called()
        self.assertEqual(MirrorQueue(self.destination, 'nfl').records(), [])

    def test_receipts_aged_out_still_produce_an_explicit_empty_export(self):
        observed = datetime.now(timezone.utc)-timedelta(days=8)
        value = payload(captured=observed); queue = MirrorQueue(self.source, 'nfl')
        queue.acknowledge(value, 'pythonanywhere', acknowledgment(value))
        queue.acknowledge(value, 'tidb', acknowledgment(value, 'tidb'))
        queue.prune_receipts(datetime.now(timezone.utc))
        self.assertEqual(queue.records(), [])
        self.assertEqual(export_observations('nfl', self.source, self.output)['records'], 0)
        self.assertEqual(validate_export('nfl', self.output)['records'], 0)

    def test_nonempty_export_preserves_validated_payload_and_excludes_nested_recovery(self):
        value = payload(); queue = MirrorQueue(self.source, 'nfl'); path = queue.enqueue(value)
        nested = self.source/'recovery/state.json'; nested.parent.mkdir(); nested.write_text('{"pending":{"unfinished":{}}}')
        report = export_observations('nfl', self.source, self.output)
        self.assertEqual(report['records'], 1)
        self.assertEqual({p.name for p in self.output.iterdir()}, {EXPORT_MANIFEST, path.name})
        self.assertEqual(MirrorQueue(self.output, 'nfl').records()[0][1]['payload'], value)
        self.assertTrue(nested.exists())
        calls = []
        def sender(original):
            calls.append(original); return acknowledgment(original, 'tidb')
        with patch('sys.stdout', StringIO()):
            self.assertEqual(deliver_tidb('nfl', self.destination, self.output, sender=sender), 0)
        self.assertEqual(calls, [value])
        self.assertIsNotNone(MirrorQueue(self.destination, 'nfl').records()[0][1]['acknowledged']['tidb'])

    def test_failed_empty_capture_remains_red_and_existing_pending_still_replays(self):
        health = self.root/'health.json'
        def failed_empty(_endpoint, _token, _headless, _timeout, path, _pending):
            path.write_text(json.dumps({'status': 'degraded', 'captured': 0, 'failed': 1}))
            return 1
        with patch.dict(os.environ, {'COLLECTOR_INGEST_TOKEN': 'test'}, clear=True), patch('sys.stdout', StringIO()):
            self.assertEqual(run_legacy('nfl', self.source, self.root/'pending', health, runner=failed_empty), 1)
        original_health = health.read_bytes()
        export_observations('nfl', self.source, self.output)
        value = payload(); MirrorQueue(self.destination, 'nfl').enqueue(value)
        calls = []
        def sender(original):
            calls.append(original); return acknowledgment(original, 'tidb')
        legacy = self.root/'free-pending'
        with patch('tools.shared_capture_policy.replay_free_pending', return_value={'delivered': 1, 'errors': [], 'pending': 0}) as replay, \
             patch('sys.stdout', StringIO()):
            self.assertEqual(deliver_tidb('nfl', self.destination, self.output, sender=sender, legacy_free_state=legacy), 0)
        self.assertEqual(calls, [value])
        replay.assert_called_once_with('nfl', legacy, sender=sender)
        self.assertEqual(health.read_bytes(), original_health)
        self.assertEqual(json.loads(original_health)['status'], 'degraded')

    def test_missing_or_unmarked_artifact_is_a_real_failure_and_never_merges(self):
        sender = Mock()
        for exists in (False, True):
            with self.subTest(directory_exists=exists):
                if exists: self.output.mkdir()
                with patch('sys.stdout', StringIO()) as output:
                    self.assertEqual(deliver_tidb('nfl', self.destination, self.output, sender=sender), 1)
                self.assertIn('incoming-export-ValueError', output.getvalue())
        sender.assert_not_called()
        self.assertEqual(MirrorQueue(self.destination, 'nfl').records(), [])

    def test_missing_artifact_stays_red_while_cached_validated_pending_can_finish(self):
        value = payload(); queue = MirrorQueue(self.destination, 'nfl'); queue.enqueue(value)
        calls = []
        def sender(original):
            calls.append(original); return acknowledgment(original, 'tidb')
        legacy = self.root/'free-pending'
        with patch('tools.shared_capture_policy.replay_free_pending', return_value={'delivered': 1, 'errors': [], 'pending': 0}) as replay, \
             patch('sys.stdout', StringIO()) as output:
            self.assertEqual(deliver_tidb('nfl', self.destination, self.output, sender=sender, legacy_free_state=legacy), 1)
        self.assertEqual(calls, [value]); self.assertEqual(queue.pending('tidb'), [])
        replay.assert_called_once_with('nfl', legacy, sender=sender)
        self.assertIn('incoming-export-ValueError', output.getvalue())

    def test_changed_record_count_digest_wrong_sport_or_nested_download_fails(self):
        value = payload(); MirrorQueue(self.source, 'nfl').enqueue(value)
        export_observations('nfl', self.source, self.output)
        record = next(self.output.glob('*.json')); original = record.read_bytes()
        with self.assertRaisesRegex(ValueError, 'sport'):
            validate_export('nhl', self.output)
        record.write_bytes(original+b'\n')
        with self.assertRaisesRegex(ValueError, 'incomplete or changed'):
            validate_export('nfl', self.output)
        record.unlink()
        with self.assertRaisesRegex(ValueError, 'incomplete or changed'):
            validate_export('nfl', self.output)
        record.write_bytes(original)
        (self.output/'recovery').mkdir()
        with self.assertRaisesRegex(ValueError, 'unexpected state'):
            validate_export('nfl', self.output)

    def test_source_corruption_and_export_budget_preserve_durable_original(self):
        value = payload(); queue = MirrorQueue(self.source, 'nfl'); path = queue.enqueue(value)
        original = path.read_bytes()
        with patch('tools.shared_capture.LIMIT', 256), self.assertRaisesRegex(ValueError, 'byte budget'):
            export_observations('nfl', self.source, self.output)
        self.assertEqual(path.read_bytes(), original); self.assertFalse(self.output.exists())
        corrupt = json.loads(original); corrupt['payload']['sections'][0]['price'] += 1
        path.write_text(json.dumps(corrupt))
        with self.assertRaisesRegex(ValueError, 'Immutable'):
            export_observations('nfl', self.source, self.output)
        self.assertFalse(self.output.exists()); self.assertTrue(path.exists())

    def test_workflow_uploads_prepared_directory_even_after_capture_failure(self):
        import yaml
        project = Path(__file__).resolve().parents[1]
        owner = yaml.load((project/'.github/workflows/collect-ticket-prices.yml').read_text(), Loader=yaml.BaseLoader)
        consumer = yaml.load((project/'.github/workflows/shared-snapshot-mirror.yml').read_text(), Loader=yaml.BaseLoader)
        for sport in ('nfl', 'nhl'):
            with self.subTest(sport=sport):
                steps = owner['jobs']['collect-'+sport]['steps']
                export = next(row for row in steps if row.get('id') == 'shared-export')
                self.assertEqual(export['if'], "always() && steps.shared-budget.outcome == 'success'")
                self.assertIn('--output shared-export/'+sport, export['run'])
                upload = next(row for row in steps if row.get('name', '').startswith('Export only public'))
                self.assertEqual(upload['with']['path'], 'shared-export/'+sport+'/')
                self.assertEqual(upload['with']['if-no-files-found'], 'error')
                self.assertEqual(upload['if'], "always() && steps.shared-export.outcome == 'success'")
        steps = consumer['jobs']['mirror']['steps']
        download = next(row for row in steps if row.get('id') == 'download')
        self.assertEqual(download['with']['path'], 'incoming')
        self.assertEqual(download['continue-on-error'], 'true')
        missing = next(row for row in steps if row.get('name', '').startswith('Report missing'))
        self.assertIn("steps.download.outcome != 'success'", missing['if'])
        self.assertEqual(missing['run'], 'exit 1')


if __name__ == '__main__':
    unittest.main()
