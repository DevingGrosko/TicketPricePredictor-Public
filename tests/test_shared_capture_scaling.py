"""Large restored queues must not consume the next capture's entire runtime."""
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.test_shared_capture import acknowledgment, payload
from tools.shared_capture import MirrorQueue, deliver_tidb, encoded, export_observations, run_legacy


class QueueScalingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def queue(self, name, count, *, captured=None):
        queue = MirrorQueue(self.root / name, 'nfl')
        with queue._batch_writes():
            for index in range(count):
                queue.enqueue(payload(pid=str(9000000 + index), captured=captured))
        return queue

    def test_thousands_of_unchanged_records_are_validated_without_rewrites(self):
        count = 2048
        queue = self.queue('capture', count)
        incoming = self.root / 'incoming'
        shutil.copytree(queue.root, incoming)
        before = {path.name: path.read_bytes() for path in queue.root.glob('*.json')}
        reads = 0
        native = MirrorQueue.read
        def read(owner, path):
            nonlocal reads
            reads += 1
            return native(owner, path)
        with patch.object(MirrorQueue, 'read', read), \
                patch.object(queue, '_save', side_effect=AssertionError('Unchanged originals must not be rewritten')):
            queue.merge(incoming)
        self.assertLessEqual(reads, 3 * count)
        self.assertEqual(before, {path.name: path.read_bytes() for path in queue.root.glob('*.json')})

    def test_changed_batch_is_linear_and_retains_unconfirmed_originals(self):
        count = 1024
        old = datetime.now(timezone.utc) - timedelta(days=8)
        queue = self.queue('capture', count, captured=old)
        incoming_root = self.root / 'incoming'
        shutil.copytree(queue.root, incoming_root)
        incoming = MirrorQueue(incoming_root, 'nfl')
        with incoming._batch_writes():
            for _, record in incoming.records():
                value = record['payload']
                incoming.acknowledge(value, 'tidb', acknowledgment(value, 'tidb'))
        original_payloads = {path.name: record['payload'] for path, record in queue.records()}
        reads = 0
        native = MirrorQueue.read
        def read(owner, path):
            nonlocal reads
            reads += 1
            return native(owner, path)
        with patch.object(MirrorQueue, 'read', read):
            queue.merge(incoming_root)
        self.assertLessEqual(reads, 4 * count)
        self.assertEqual(original_payloads, {path.name: record['payload'] for path, record in queue.records()})
        # Receipt retention must never remove even very old unconfirmed originals.
        queue.prune_receipts(datetime.now(timezone.utc))
        self.assertEqual(len(queue.records()), count)
        self.assertEqual(len(queue.pending('pythonanywhere')), count)
        self.assertEqual(queue.pending('tidb'), [])

    def test_each_batch_revalidates_disk_and_counts_auxiliary_files(self):
        queue = self.queue('capture', 1)
        first_path, first = queue.records()[0]
        second = payload(pid='9000002')
        with queue._batch_writes():
            queue.acknowledge(first['payload'], 'tidb', acknowledgment(first['payload'], 'tidb'))
        # A recovery checkpoint written between batches is part of the budget.
        auxiliary = queue.root / 'recovery' / 'state.json'
        auxiliary.parent.mkdir()
        auxiliary.write_bytes(b'x' * 10000)
        queue.byte_limit = first_path.stat().st_size + 10000 + 1024 + 1
        with queue._batch_writes(), self.assertRaisesRegex(ValueError, 'budget exhausted'):
            queue.enqueue(second)
        self.assertEqual(len(queue.records()), 1)
        self.assertEqual(auxiliary.read_bytes(), b'x' * 10000)
        # Validation is never memoized across batches.
        broken = json.loads(first_path.read_bytes())
        broken['payload']['sections'][0]['price'] += 1
        first_path.write_bytes(encoded(broken))
        with queue._batch_writes(), self.assertRaisesRegex(ValueError, 'Immutable'):
            queue.enqueue(second)

    def test_mid_write_interruption_reconciles_budget_before_next_write(self):
        queue = self.queue('capture', 1)
        first_path, first = queue.records()[0]
        second = payload(pid='9000002')
        native = os.fsync
        calls = 0
        def fail_after_replace(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('interrupted after atomic replace')
            return native(fd)
        with queue._batch_writes():
            with patch('tools.shared_capture.os.fsync', fail_after_replace), self.assertRaises(OSError):
                queue.acknowledge(first['payload'], 'tidb', acknowledgment(first['payload'], 'tidb'))
            self.assertIsNone(queue._write_budget)
            queue.enqueue(second)
        self.assertEqual(len(queue.records()), 2)
        self.assertIsNotNone(queue.read(first_path)['acknowledged']['tidb'])
        self.assertFalse(list(queue.root.glob('.shared-*')))

    def test_bootstrap_failure_is_reported_before_any_capture(self):
        health = self.root / 'health.json'
        def interrupted(*args):
            current = json.loads(health.read_text())
            self.assertEqual(current['phase'], 'merge-acknowledgments')
            raise ValueError('private error detail must not enter health')
        with patch.dict(os.environ, {'COLLECTOR_INGEST_TOKEN': 'test'}, clear=True), \
                patch.object(MirrorQueue, 'merge', interrupted), \
                patch('tools.shared_capture_policy.run_owner', side_effect=AssertionError('No capture during bootstrap')), \
                patch('sys.stdout', StringIO()), self.assertRaises(ValueError):
            run_legacy('nfl', self.root / 'capture', self.root / 'pending', health,
                       acknowledgments=self.root / 'acks')
        current = json.loads(health.read_text())
        self.assertEqual(current['status'], 'degraded')
        self.assertEqual(current['phase'], 'failed-merge-acknowledgments')
        self.assertEqual(current['captured'], 0)
        self.assertEqual(current['errors'], ['Bootstrap: ValueError'])
        self.assertNotIn('private error detail', health.read_text())

    def test_bootstrap_elapsed_time_reduces_capture_budget(self):
        health = self.root / 'health.json'
        clock = [100.0]
        def merge(*args):
            clock[0] += 125
        def owner(*args, **kwargs):
            self.assertEqual(kwargs['deadline_seconds'], 24 * 60 - 125)
            health.write_text(json.dumps({'status': 'healthy'}))
            return 0
        with patch.dict(os.environ, {'COLLECTOR_INGEST_TOKEN': 'test'}, clear=True), \
                patch('tools.shared_capture.time.monotonic', lambda: clock[0]), \
                patch.object(MirrorQueue, 'merge', merge), \
                patch('tools.shared_capture_policy.run_owner', owner), patch('sys.stdout', StringIO()):
            self.assertEqual(run_legacy('nfl', self.root / 'capture', self.root / 'pending', health,
                                       acknowledgments=self.root / 'acks'), 0)
        self.assertEqual(json.loads(health.read_text())['bootstrap_seconds'], 125)

    def test_delivery_budget_prioritizes_fresh_data_and_preserves_every_original(self):
        now = datetime.now(timezone.utc)
        queue = MirrorQueue(self.root / 'capture', 'nfl')
        originals = [payload(pid=str(9000000 + index), captured=now - timedelta(hours=3-index))
                     for index in range(3)]
        for value in originals:
            queue.acknowledge(value, 'pythonanywhere', acknowledgment(value))
        exported = self.root / 'export'
        export_observations('nfl', queue.root, exported)
        delivered = []
        clock = [0]
        def sender(value):
            delivered.append(value)
            clock[0] += 11
            return acknowledgment(value, 'tidb')
        target = self.root / 'target'
        with patch('sys.stdout', StringIO()):
            self.assertEqual(deliver_tidb('nfl', target, exported, sender=sender,
                                         clock=lambda: clock[0], budget=10), 1)
        self.assertEqual(delivered, [originals[-1]])
        restored = MirrorQueue(target, 'nfl')
        self.assertEqual(len(restored.records()), 3)
        self.assertEqual({row['source_id'] for _, row in restored.pending('tidb')},
                         {value['source_id'] for value in originals[:-1]})
        for _, row in restored.pending('tidb'):
            self.assertIn(row['payload'], originals)
        # A later run retries remaining originals without recapturing or replacing times.
        with patch('sys.stdout', StringIO()):
            self.assertEqual(deliver_tidb('nfl', target, exported, sender=sender,
                                         clock=lambda: clock[0], budget=30), 0)
        self.assertEqual(delivered, list(reversed(originals)))
        self.assertEqual(len(restored.records()), 3)
        self.assertTrue(all('payload' not in row for _, row in restored.records()))


if __name__ == '__main__':
    unittest.main()
