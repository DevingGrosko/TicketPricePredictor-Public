"""Full checkpoints recover without discarding unconfirmed observations."""
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
import warnings
from unittest.mock import patch
import zipfile

from tests.test_shared_capture import payload, acknowledgment
from tests.test_shared_transport_retention import artifact, provenance, jobs
from tools.shared_capture import MirrorQueue, deliver_tidb, export_observations, run_legacy
from tools.shared_capture_storage import (export_covers, read_transport_archive,
                                          removable_transport_artifacts)


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

    def test_content_cleanup_needs_two_validated_copies_and_never_removes_latest_two(self):
        q = MirrorQueue(self.root/'q', 'nfl'); q.enqueue(payload())
        records = {path.name: value for path, value in q.records()}
        rows = [artifact(i) for i in range(1, 5)]
        for row in rows: row['size_in_bytes'] = 60 * 1024**2
        def select(copies):
            return removable_transport_artifacts(rows, [], provenance, jobs, current_run=99,
                                                  records_for=lambda row: copies[row['id']])
        self.assertEqual({row['id'] for row in select(dict.fromkeys(range(1, 5), records))}, {1, 2})
        self.assertEqual(select({1: records, 2: records, 3: {}, 4: records}), [])
        with patch('tools.shared_capture_storage.PROVENANCE_RUN_LIMIT', 1):
            self.assertEqual(select(dict.fromkeys(range(1, 5), records)), [])
        # An active artifact cannot supply either retained copy.
        def active(run): return provenance(run, status='in_progress' if run == 4 else 'completed')
        selected = removable_transport_artifacts(rows, [], active, jobs, current_run=99,
                                                 records_for=lambda row: records)
        self.assertEqual([row['id'] for row in selected], [1])

    def test_coverage_rejects_different_original_or_lost_archive_proof(self):
        q = MirrorQueue(self.root/'q', 'nfl'); q.enqueue(payload())
        name, value = [(path.name, value) for path, value in q.records()][0]
        self.assertFalse(export_covers({name: value}, {}))
        self.assertFalse(export_covers({name: value}, {name: {**value, 'payload_sha256': '0'*64}}))
        archived = {**value, 'archived_conflicts': {'pythonanywhere': {'proof': 'immutable'}}}
        self.assertFalse(export_covers({name: archived}, {name: value}))
        self.assertTrue(export_covers({name: archived}, {name: archived}))
        confirmed = {**value, 'acknowledged': dict(pythonanywhere={'confirmed': True}, tidb={'confirmed': True})}
        confirmed.pop('payload')
        self.assertTrue(export_covers({name: confirmed}, {}))

    def test_zip_validation_rejects_traversal_duplicate_names_and_changed_records(self):
        q = MirrorQueue(self.root/'q', 'nfl'); q.enqueue(payload())
        exported = self.root/'export'; export_observations('nfl', q.root, exported)
        archive = self.root/'export.zip'
        with zipfile.ZipFile(archive, 'w') as zipped:
            for path in exported.iterdir(): zipped.write(path, path.name)
        self.assertEqual(len(read_transport_archive(archive, 'nfl')), 1)
        with zipfile.ZipFile(archive, 'a') as zipped: zipped.writestr('../outside', 'x')
        with self.assertRaisesRegex(ValueError, 'Invalid'): read_transport_archive(archive, 'nfl')
        self.assertFalse((self.root/'outside').exists())
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(archive, 'w') as zipped:
                zipped.writestr('observations.manifest', 'first')
                zipped.writestr('observations.manifest', 'second')
        with self.assertRaisesRegex(ValueError, 'Invalid'): read_transport_archive(archive, 'nfl')
        with zipfile.ZipFile(archive, 'w') as zipped:
            for path in exported.iterdir():
                data = path.read_bytes()
                if path.suffix == '.json': data = data.replace(b'"section_count":10', b'"section_count":11')
                zipped.writestr(path.name, data)
        with self.assertRaises(ValueError): read_transport_archive(archive, 'nfl')
