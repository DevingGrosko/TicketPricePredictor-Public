"""Full checkpoints recover without discarding unconfirmed observations."""
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_shared_capture import payload, acknowledgment
from tools.shared_capture import MirrorQueue, deliver_tidb, export_observations, run_legacy


class RetentionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old = datetime.now(timezone.utc) - timedelta(days=8)

    def fill(self, queue):
        with queue._batch_writes():
            for index in range(8):
                value = payload(pid=str(9000000 + index), captured=self.old)
                queue.acknowledge(value, 'pythonanywhere', acknowledgment(value))
                queue.acknowledge(value, 'tidb', acknowledgment(value, 'tidb'))
        original = payload(pid='9999999', captured=self.old)
        queue.enqueue(original)
        return original

    def test_capture_prunes_both_restored_checkpoints_before_budgeted_merge(self):
        capture = MirrorQueue(self.root/'capture', 'nfl')
        acks = MirrorQueue(self.root/'acks', 'nfl')
        original = self.fill(capture); self.fill(acks)
        new = payload(pid='8888888'); acks.enqueue(new)
        limit = capture._budget_snapshot()[1] + 1
        capture.byte_limit = limit
        with self.assertRaisesRegex(ValueError, 'budget exhausted'):
            capture.merge(acks.root)
        health = self.root/'health.json'
        def owner(*args, **kwargs):
            current = MirrorQueue(capture.root, 'nfl').records()
            self.assertEqual({value['payload_sha256'] for _, value in current},
                             {capture.read(capture.root/path.name)['payload_sha256']
                              for path, _ in MirrorQueue(acks.root, 'nfl').records()})
            self.assertEqual(len(current), 2)
            self.assertIn(original, [value['payload'] for _, value in current])
            health.write_text(json.dumps({'status': 'healthy'}))
            return 0
        with patch.dict(os.environ, {'COLLECTOR_INGEST_TOKEN': 'test'}, clear=True), \
                patch('tools.shared_capture_policy.run_owner', owner), patch('sys.stdout', StringIO()):
            run_legacy('nfl', capture.root, self.root/'pending', health, acknowledgments=acks.root)
        self.assertEqual(len(MirrorQueue(acks.root, 'nfl').records()), 2)

    def test_tidb_prunes_confirmed_receipts_but_keeps_old_undelivered_payload(self):
        target = MirrorQueue(self.root/'target', 'nfl')
        original = self.fill(target)
        incoming = self.root/'export'
        export_observations('nfl', self.root/'empty', incoming)
        with patch('sys.stdout', StringIO()):
            deliver_tidb('nfl', target.root, incoming, budget=0, sender=lambda _: self.fail('No delivery'))
        self.assertEqual([value['payload'] for _, value in target.records()], [original])

