"""Superseded transport cleanup preserves unconfirmed originals in two exports."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

from tests.test_shared_capture import payload
from tests.test_shared_transport_retention import artifact, provenance, jobs
from tools.shared_capture import MirrorQueue, export_observations
from tools.shared_capture_storage import (export_covers, read_transport_archive,
                                          removable_transport_artifacts)


class TransportSupersessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

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
