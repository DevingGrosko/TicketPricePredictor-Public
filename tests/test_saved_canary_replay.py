"""Replay reviewed real canary originals without recapture or slot replacement."""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import collector
import nfl_schedule_collector as nfl
import nhl_schedule_collector as nhl
from tests.test_observation_receipt import proof
from tests.test_shared_capture import acknowledgment
from tools.shared_capture import (MirrorQueue, SAVED_BUNDLES, deliver_tidb,
    export_observations, run_legacy, saved_bundle_observations)

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = 'canary-20261012-2200'
EXPECTED = {
    '7301418': ('2026-10-12T22:05:51.617637+00:00', 91,
        '828ba83b7692ec92415ac236a612edc3e147a50bfecaf958617c93d80b22d751'),
    '7302299': ('2026-10-12T22:06:00.265848+00:00', 21,
        '484030fb13ca9f53df223f0a16b5f74e53fdecf827e0dbf07b07b44be7f440b7'),
    '6491666': ('2026-10-12T22:06:08.636137+00:00', 217,
        'dc94fcdba5b0af3c5dcb5f9a3444aa0696a82eef4b4820ee78a38ca1906bd480'),
}


class SavedCanaryReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {'COLLECTOR_INGEST_TOKEN': 'test'}, clear=True))
        self.stack.enter_context(patch.object(socket.socket, 'connect', side_effect=AssertionError('No network')))
        self.stack.enter_context(patch('nfl_collector.VividNFLBrowser.__init__', side_effect=AssertionError('No browser')))
        for module in (nfl, nhl):
            self.stack.enter_context(patch.object(module, 'run_schedule_collector', side_effect=AssertionError('No schedule')))
        self.stack.enter_context(patch('sys.stdout', StringIO()))

    def test_pinned_original_bytes_and_exact_observation_times(self):
        manifest, digest = SAVED_BUNDLES[BUNDLE]
        data = (ROOT / manifest).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
        for row in json.loads(data)['observations']:
            original = (ROOT / row['file']).read_bytes()
            value = json.loads(original)
            captured, count, expected = EXPECTED[value['source_id']]
            self.assertEqual(hashlib.sha256(original).hexdigest(), expected)
            self.assertEqual(row['sha256'], expected)
            self.assertEqual((value['captured_at'], value['section_count']), (captured, count))
        with self.assertRaises(ValueError): saved_bundle_observations('../unreviewed', 'nhl')
        with patch.dict(SAVED_BUNDLES, {BUNDLE: (manifest, '0' * 64)}):
            with self.assertRaisesRegex(ValueError, 'integrity mismatch'):
                saved_bundle_observations(BUNDLE, 'nhl')

    def test_real_originals_replay_to_both_stores_without_new_capture_or_time_changes(self):
        for sport in ('nfl', 'nhl'):
            originals = saved_bundle_observations(BUNDLE, sport)
            before = deepcopy(originals)
            posted = []
            def post(endpoint, token, value, **kwargs):
                posted.append(deepcopy(value)); return proof(value)
            directory = self.root / sport
            health = self.root / (sport + '-health.json')
            with patch.object(collector, 'post_snapshot_with_retry', post):
                self.assertEqual(run_legacy(sport, directory, self.root / (sport + '-pending'),
                    health, saved=originals), 0)
            self.assertEqual(posted, before)
            self.assertEqual(originals, before)
            report = json.loads(health.read_text())
            self.assertEqual((report['mode'], report['captured'], report['replayed']),
                             ('delivery-only', 0, len(originals)))
            export = self.root / (sport + '-export')
            export_observations(sport, directory, export)
            delivered = []
            def send(value):
                delivered.append(deepcopy(value)); return acknowledgment(value, 'tidb')
            receiver = self.root / (sport + '-tidb')
            self.assertEqual(deliver_tidb(sport, receiver, export, sender=send), 0)
            self.assertEqual(sorted(delivered, key=lambda v: v['source_id']),
                             sorted(before, key=lambda v: v['source_id']))
            for _path, value in MirrorQueue(receiver, sport).records():
                self.assertTrue(all(value['acknowledged'].values()))
                self.assertEqual(value['acknowledged']['pythonanywhere']['stored_observation_sha256'],
                                 value['observation_sha256'])

    def test_any_different_owned_slot_rejects_entire_sport_before_inserting_or_posting(self):
        originals = saved_bundle_observations(BUNDLE, 'nhl')
        existing = deepcopy(originals[-1])
        existing['sections'][0]['price'] += 1
        queue = MirrorQueue(self.root / 'nhl', 'nhl')
        path = queue.enqueue(existing)
        before = path.read_bytes()
        with patch.object(collector, 'post_snapshot_with_retry', side_effect=AssertionError('No delivery')):
            with self.assertRaisesRegex(ValueError, 'different original'):
                run_legacy('nhl', queue.root, self.root / 'pending', self.root / 'health.json', saved=originals)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(queue.records()), 1)
        self.assertEqual(queue.records()[0][1]['payload'], existing)

    def test_raw_pending_collision_without_mirror_rejects_before_any_insert_or_post(self):
        originals = saved_bundle_observations(BUNDLE, 'nhl')
        existing = deepcopy(originals[-1])
        existing['sections'][0]['price'] += 1
        for suffix in ('.json', '.rejected', '.archived-conflict'):
            with self.subTest(suffix=suffix):
                root = self.root / suffix[1:]
                pending = root / 'pending'; pending.mkdir(parents=True)
                path = pending / ('saved-original' + suffix)
                data = json.dumps(existing, sort_keys=True).encode()
                path.write_bytes(data)
                with patch.object(collector, 'post_snapshot_with_retry', side_effect=AssertionError('No delivery')):
                    with self.assertRaisesRegex(ValueError, 'different pending original'):
                        run_legacy('nhl', root / 'mirror', pending, root / 'health.json', saved=originals)
                self.assertEqual(list((root / 'mirror').glob('*.json')), [])
                self.assertEqual(path.read_bytes(), data)


if __name__ == '__main__':
    unittest.main()
