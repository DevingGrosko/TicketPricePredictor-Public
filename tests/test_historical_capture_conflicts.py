"""Exact archived originals retain truthful delivery state and cannot broaden scope."""
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

import collector
from tests.test_observation_receipt import proof
from tests.test_shared_capture import acknowledgment, payload
from tools import historical_capture_conflicts as historical
from tools.shared_capture import (MirrorQueue, encoded, export_observations, run_legacy,
                                  validate_export, verify_delivery_checkpoint)


class HistoricalCaptureConflictTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        manifest = json.loads((historical.PROJECT / historical.MANIFEST).read_text())
        self.entries = {row['sport']: row for row in manifest['observations']}

    def original(self, sport='nfl'):
        return json.loads((historical.PROJECT / self.entries[sport]['file']).read_text())

    def receipt(self, value):
        return {**proof(value, status='duplicate'),
                'stored_section_count': self.entries[value['event_type']]['canonical_section_count'],
                'stored_observation_sha256': 'a' * 64}

    def queue(self, name='queue', sport='nfl'):
        return MirrorQueue(self.root / name, sport)

    def marked(self, queue, value):
        self.assertTrue(queue.archive_conflict(value, self.receipt(value)))
        return queue.read(queue.enqueue(value))

    def test_both_exact_originals_require_actual_different_duplicate_proof(self):
        for sport in ('nfl', 'nhl'):
            value = self.original(sport); queue = self.queue(sport, sport)
            record = self.marked(queue, value)
            self.assertEqual(record['payload'], value)
            self.assertIsNone(record['acknowledged']['pythonanywhere'])
            self.assertEqual(len(queue.pending('pythonanywhere')), 1)
            self.assertEqual(queue.retryable_pending('pythonanywhere'), [])
            self.assertEqual(len(queue.retryable_pending('tidb')), 1)
            self.assertEqual(record['archived_conflicts']['pythonanywhere']['archive_payload_sha256'],
                             self.entries[sport]['sha256'])
            with self.assertRaisesRegex(ValueError, 'not a destination acknowledgment'):
                queue.acknowledge(value, 'pythonanywhere', proof(value))
            queue.acknowledge(value, 'tidb', acknowledgment(value, 'tidb'))
            stored = queue.read(queue.enqueue(value))
            self.assertEqual(stored['payload'], value)
            self.assertIsNotNone(stored['acknowledged']['tidb'])
            self.assertIsNone(stored['acknowledged']['pythonanywhere'])

    def test_unlisted_or_changed_original_is_never_terminal(self):
        ordinary = payload(); queue = self.queue()
        self.assertFalse(queue.archive_conflict(ordinary, self.receipt(ordinary)))
        value = self.original(); value['sections'][0]['price'] += 1
        other = self.queue('changed')
        self.assertFalse(other.archive_conflict(value, self.receipt(value)))
        self.assertEqual(len(other.retryable_pending('pythonanywhere')), 1)

    def test_missing_hostile_wrong_identity_and_malformed_receipts_do_not_qualify(self):
        value = self.original(); queue = self.queue(); record = queue.read(queue.enqueue(value))
        response = self.receipt(value)
        invalid = [None, [], {'status': 'duplicate'},
            {**response, 'status': 'stored'}, {**response, 'status': 'HTTP409'},
            {**response, 'event_type': 'nhl'}, {**response, 'stored_source_id': '6493302'},
            {**response, 'stored_source_id': 6493301}, {**response, 'stored_section_count': 180},
            {**response, 'stored_section_count': True}, {**response, 'stored_observation_version': True},
            {**response, 'stored_observation_version': 2}, {**response, 'event_id': 0},
            {**response, 'iteration_id': True}, {**response, 'sections': 1},
            {**response, 'stored_capture_slot': '2026-10-10T19:00:00-04:00'},
            {**response, 'stored_capture_slot': '2026-10-10T23:00:00'},
            {**response, 'captured_at': '2026-10-10T23:30:00+00:00'},
            {**response, 'captured_at': '2026-10-10T23:00:00'},
            {**response, 'stored_observation_sha256': record['observation_sha256']},
            {**response, 'stored_observation_sha256': 'SECRET\n' + 'a' * 64}]
        for key in historical.RECEIPT_FIELDS:
            invalid.append({k: v for k, v in response.items() if k != key})
            invalid.append({**response, key: {'hostile': 'credential-secret'}})
        for index, candidate in enumerate(invalid):
            with self.subTest(index=index):
                self.assertIsNone(historical.classify(record, candidate))
        self.assertNotIn('archived_conflicts', queue.read(queue.enqueue(value)))

    def test_corrupt_missing_and_symlink_archives_fail_closed_without_marker(self):
        value = self.original(); queue = self.queue(); record = queue.read(queue.enqueue(value))
        project = self.root / 'project'
        shutil.copytree(historical.PROJECT / 'docs/shared-observations/conflicts',
                        project / 'docs/shared-observations/conflicts')
        path = project / self.entries['nfl']['file']; data = path.read_bytes()
        with patch.object(historical, 'PROJECT', project):
            path.write_bytes(data + b' ')
            with self.assertRaisesRegex(ValueError, 'integrity'):
                historical.classify(record, self.receipt(value))
            path.unlink()
            with self.assertRaisesRegex(ValueError, 'archive path'):
                historical.classify(record, self.receipt(value))
            path.symlink_to(historical.__file__)
            with self.assertRaisesRegex(ValueError, 'archive path'):
                historical.classify(record, self.receipt(value))
            path.unlink(); path.write_bytes(data)
            manifest = project / historical.MANIFEST
            manifest.write_bytes(manifest.read_bytes() + b' ')
            with self.assertRaisesRegex(ValueError, 'integrity'):
                historical.classify(record, self.receipt(value))
        self.assertNotIn('archived_conflicts', queue.read(queue.enqueue(value)))

    def test_marker_survives_old_merges_and_exports_with_independent_tidb_ack(self):
        value = self.original(); stale = self.queue('stale'); stale.enqueue(value)
        producer = self.queue('producer'); marked = self.marked(producer, value)
        consumer = self.queue('consumer'); consumer.merge(producer.root)
        consumer.acknowledge(value, 'tidb', acknowledgment(value, 'tidb'))
        producer.merge(consumer.root); producer.merge(stale.root)
        result = producer.read(producer.enqueue(value))
        self.assertEqual(result['archived_conflicts'], marked['archived_conflicts'])
        self.assertIsNone(result['acknowledged']['pythonanywhere'])
        self.assertIsNotNone(result['acknowledged']['tidb'])
        self.assertEqual(result['payload'], value)
        output = self.root / 'export'; export_observations('nfl', producer.root, output)
        self.assertEqual(validate_export('nfl', output)['records'], 1)
        verify_delivery_checkpoint('nfl', consumer.root, output)
        with self.assertRaisesRegex(ValueError, 'lost the archived'):
            verify_delivery_checkpoint('nfl', stale.root, output)
        other = self.queue('different-proof'); response = self.receipt(value)
        response['stored_observation_sha256'] = 'b' * 64
        other.archive_conflict(value, response)
        before = producer.read(producer.enqueue(value))
        with self.assertRaisesRegex(ValueError, 'proof changed'):
            producer.merge(other.root)
        self.assertEqual(producer.read(producer.enqueue(value)), before)

    def test_forged_marker_destinations_receipts_and_missing_payload_are_rejected_on_read(self):
        value = self.original(); queue = self.queue(); record = self.marked(queue, value)
        path = queue.enqueue(value)
        variants = []
        for key, wrong in [('archive_manifest_sha256', '0' * 64), ('archive_file', '../elsewhere'),
                           ('archive_payload_sha256', '0' * 64), ('version', 2), ('version', True)]:
            altered = json.loads(json.dumps(record)); altered['archived_conflicts']['pythonanywhere'][key] = wrong
            variants.append(altered)
        altered = json.loads(json.dumps(record)); altered['archived_conflicts']['tidb'] = altered['archived_conflicts']['pythonanywhere']; variants.append(altered)
        altered = json.loads(json.dumps(record)); altered.pop('payload'); variants.append(altered)
        altered = json.loads(json.dumps(record)); altered['archived_conflicts']['pythonanywhere']['receipt']['stored_source_id'] = '0'; variants.append(altered)
        for altered in variants:
            path.write_bytes(encoded(altered))
            with self.assertRaises(ValueError): queue.read(path)
        path.write_bytes(encoded(record))
        self.assertEqual(queue.read(path), record)

    def test_replay_archives_without_false_success_and_next_run_never_posts(self):
        value = self.original(); pending = self.root / 'pending'; health = self.root / 'health.json'
        queue = self.queue()
        sender = Mock(return_value=self.receipt(value))
        with patch.dict(os.environ, {'COLLECTOR_INGEST_TOKEN': 'test'}, clear=True), \
             patch.object(collector, 'post_snapshot_with_retry', sender), patch('sys.stdout', StringIO()):
            self.assertEqual(run_legacy('nfl', queue.root, pending, health, saved=[value]), 0)
        sender.assert_called_once()
        report = json.loads(health.read_text())
        self.assertEqual((report['replayed'], report['pending'], report['pending_pythonanywhere'],
                          report['retryable_pending_pythonanywhere'], report['archived_pythonanywhere_conflicts']),
                         (0, 0, 1, 0, 1))
        self.assertEqual(len(list(pending.glob('*.archived-conflict'))), 1)
        self.assertEqual(json.loads(next(pending.glob('*.archived-conflict')).read_text()), value)
        with patch.dict(os.environ, {'COLLECTOR_INGEST_TOKEN': 'test'}, clear=True), \
             patch.object(collector, 'post_snapshot_with_retry', side_effect=AssertionError('No repeat POST')), \
             patch('sys.stdout', StringIO()):
            self.assertEqual(run_legacy('nfl', queue.root, pending, health, saved=[value]), 0)

    def test_suffix_without_valid_proof_retries_and_new_conflict_still_fails(self):
        value = self.original(); pending = self.root / 'pending'; health = self.root / 'health.json'
        path = collector.queue_snapshot(value, pending); path.rename(path.with_suffix('.archived-conflict'))
        sender = Mock(return_value=self.receipt(value))
        with patch.dict(os.environ, {'COLLECTOR_INGEST_TOKEN': 'test'}, clear=True), \
             patch.object(collector, 'post_snapshot_with_retry', sender), patch('sys.stdout', StringIO()):
            self.assertEqual(run_legacy('nfl', self.root / 'queue', pending, health, saved=[value]), 0)
        sender.assert_called_once()
        ordinary = payload(); sender = Mock(return_value=self.receipt(ordinary))
        with patch.dict(os.environ, {'COLLECTOR_INGEST_TOKEN': 'test'}, clear=True), \
             patch.object(collector, 'post_snapshot_with_retry', sender), patch('sys.stdout', StringIO()):
            self.assertEqual(run_legacy('nfl', self.root / 'queue', pending, health, saved=[ordinary]), 1)
        report = json.loads(health.read_text())
        self.assertEqual((report['archived_pythonanywhere_conflicts'], report['retryable_pending_pythonanywhere']), (1, 1))
        self.assertTrue(report['errors'])
        self.assertEqual(len(list(pending.glob('*.rejected'))), 1)

    def test_http_conflict_without_actual_duplicate_receipt_remains_failure(self):
        value = self.original(); queue = self.queue(); pending = self.root / 'pending'
        health = self.root / 'health.json'
        failure = collector.SnapshotUploadError('HTTP409 is not a readback receipt',
                                                retryable=False, status_code=409)
        with patch.dict(os.environ, {'COLLECTOR_INGEST_TOKEN': 'test'}, clear=True), \
             patch.object(collector, 'post_snapshot_with_retry', side_effect=failure), \
             patch('sys.stdout', StringIO()):
            self.assertEqual(run_legacy('nfl', queue.root, pending, health, saved=[value]), 1)
        report = json.loads(health.read_text())
        self.assertEqual((report['archived_pythonanywhere_conflicts'], report['retryable_pending_pythonanywhere']), (0, 1))
        self.assertNotIn('archived_conflicts', queue.read(queue.enqueue(value)))

    def test_real_owner_replay_reports_archive_separately_without_upload_or_failure(self):
        import nfl_schedule_collector as nfl
        value = self.original(); queue = self.queue(); queue.enqueue(value)
        pending = self.root / 'pending'; health = self.root / 'health.json'
        with patch.dict(os.environ, {'COLLECTOR_INGEST_TOKEN': 'test'}, clear=True), \
             patch.object(collector, 'post_snapshot_with_retry', return_value=self.receipt(value)), \
             patch.object(nfl, 'fetch_schedule_games', return_value=([], 'official')), \
             patch.object(nfl, 'discover_nfl_games', side_effect=AssertionError('No new games')), \
             patch('sys.stdout', StringIO()):
            self.assertEqual(run_legacy('nfl', queue.root, pending, health), 0)
        report = json.loads(health.read_text())
        self.assertEqual((report['uploaded'], report['duplicates'], report['replayed']), (0, 0, 0))
        self.assertEqual(report['archived_pythonanywhere_conflicts'], 1)
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['status'], 'healthy')

    def test_mirror_workflow_overlays_validator_and_archive_before_delivery(self):
        source = (historical.PROJECT / '.github/workflows/shared-snapshot-mirror.yml').read_text()
        self.assertIn('cp bridge/tools/historical_capture_conflicts.py source/tools/historical_capture_conflicts.py', source)
        self.assertIn('cp bridge/docs/shared-observations/conflicts/*.json source/docs/shared-observations/conflicts/', source)
        self.assertLess(source.index('cp bridge/docs/shared-observations/conflicts/*.json'),
                        source.index('python -m tools.shared_capture deliver'))


if __name__ == '__main__':
    unittest.main()
