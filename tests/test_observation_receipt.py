"""Actual stored-row receipts and immutable duplicate delivery, without live calls."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

import collector
from Flask_App.collection_cadence import half_hour_capture_slot
from Flask_App.nfl_blueprint import nfl_blueprint
from Flask_App.nhl_blueprint import nhl_blueprint
from Flask_App.observation_receipt import observation_sha256, verify_stored_receipt
from tests.test_shared_capture import acknowledgment, payload
from tools.import_shared_snapshot import verify_receipt
from tools.shared_capture import MirrorQueue, run_legacy


def proof(value, status='stored'):
    result = acknowledgment(value, status=status)
    result.update(stored_observation_version=1, stored_source_id=value['source_id'],
        stored_capture_slot=half_hour_capture_slot(datetime.fromisoformat(value['captured_at'])).isoformat(),
        stored_section_count=value['section_count'], stored_observation_sha256=observation_sha256(
            value['event_type'], value['source_id'], value['captured_at'], value['sections']))
    return result


class ObservationReceiptTests(unittest.TestCase):
    def test_digest_order_timezone_and_every_committed_identity_field(self):
        value = payload(captured=datetime(2026, 10, 8, 4, 4, tzinfo=timezone.utc))
        args = ('nfl', value['source_id'], value['captured_at'], value['sections'])
        expected = observation_sha256(*args)
        self.assertEqual(expected, observation_sha256('nfl', value['source_id'],
            datetime(2026, 10, 8, 0, 28, tzinfo=timezone(timedelta(hours=-4))),
            list(reversed(value['sections']))))
        changes = [dict(sport='nhl'), dict(source_id='6491666'),
                   dict(captured_at='2026-10-08T04:30:00+00:00')]
        for change in changes:
            kwargs = dict(sport='nfl', source_id=value['source_id'],
                captured_at=value['captured_at'], sections=value['sections'])
            kwargs.update(change)
            self.assertNotEqual(expected, observation_sha256(**kwargs))
        for key in ('section', 'price', 'listing_count'):
            rows = deepcopy(value['sections'])
            rows[0][key] = 'Different section' if key == 'section' else rows[0][key] + 1
            self.assertNotEqual(expected, observation_sha256('nfl', value['source_id'], value['captured_at'], rows))
        for bad in (None, '', 'Section 1'):
            rows = deepcopy(value['sections']); rows[0]['section'] = bad
            with self.subTest(bad_section=bad), self.assertRaises((TypeError, ValueError)):
                observation_sha256('nfl', value['source_id'], value['captured_at'], rows)

    def test_actual_both_sport_post_receipts_prove_stored_rows_and_reject_changed_duplicate(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'TICKET_DB_BACKEND': 'sqlite', 'COLLECTOR_INGEST_TOKEN': 'test-token',
            'NFL_DATABASE_PATH': str(Path(directory)/'nfl.db'),
            'NHL_DATABASE_PATH': str(Path(directory)/'nhl.db'),
        }, clear=True), \
             patch('Flask_App.nfl_blueprint.create_nfl_daily_backup'), \
             patch('Flask_App.nfl_blueprint.write_nfl_audit'), \
             patch('Flask_App.nhl_blueprint.create_nhl_daily_backup'), \
             patch('Flask_App.nhl_blueprint.write_nhl_audit'), \
             patch('Flask_App.nhl_blueprint.compact_completed_nhl_games', return_value={}):
            app = Flask(__name__); app.register_blueprint(nfl_blueprint); app.register_blueprint(nhl_blueprint)
            app.config['TESTING'] = True
            client = app.test_client()
            for sport, pid in (('nfl', '6493143'), ('nhl', '7302493')):
                with self.subTest(sport=sport):
                    value = payload(sport, pid)
                    endpoint = f'/api/{sport}/snapshot'
                    headers = {'Authorization': 'Bearer test-token'}
                    stored = client.post(endpoint, json=value, headers=headers)
                    self.assertEqual(stored.status_code, 201, stored.get_data(as_text=True))
                    response = stored.get_json()
                    self.assertEqual(response['stored_source_id'], pid)
                    self.assertEqual(response['stored_section_count'], 10)
                    self.assertEqual(response['stored_capture_slot'], half_hour_capture_slot(
                        datetime.fromisoformat(value['captured_at'])).isoformat())
                    self.assertEqual(response['stored_observation_sha256'], proof(value)['stored_observation_sha256'])
                    self.assertTrue(verify_receipt(value, response, 'pythonanywhere')['price_readback_verified'])
                    duplicate = client.post(endpoint, json=value, headers=headers).get_json()
                    self.assertEqual(duplicate['status'], 'duplicate')
                    self.assertEqual(duplicate['stored_observation_sha256'], response['stored_observation_sha256'])
                    self.assertTrue(verify_receipt(value, duplicate, 'pythonanywhere')['identity_readback_verified'])

                    for key in ('price', 'listing_count'):
                        different = deepcopy(value); different['sections'][0][key] += 1
                        actual = client.post(endpoint, json=different, headers=headers).get_json()
                        self.assertEqual(actual['status'], 'duplicate')
                        self.assertEqual(actual['stored_observation_sha256'], response['stored_observation_sha256'])
                        with self.assertRaisesRegex(ValueError, 'differs'):
                            verify_receipt(different, actual, 'pythonanywhere')
                    more = deepcopy(value); extra = dict(more['sections'][0]); extra['section'] = 'Extra section'
                    more['sections'].append(extra); more['section_count'] += 1
                    actual = client.post(endpoint, json=more, headers=headers).get_json()
                    self.assertEqual(actual['sections'], 11)  # Existing response format stays compatible.
                    self.assertEqual(actual['stored_section_count'], 10)  # Actual committed rows are separate.
                    with self.assertRaisesRegex(ValueError, 'differs'):
                        verify_receipt(more, actual, 'pythonanywhere')

    def test_staged_old_stored_compatibility_but_duplicates_and_wrong_new_proofs_reject(self):
        for sport, pid in (('nfl', '6493143'), ('nhl', '7302493')):
            value = payload(sport, pid)
            old = verify_receipt(value, acknowledgment(value), 'pythonanywhere')
            self.assertNotIn('price_readback_verified', old)
            with self.assertRaisesRegex(ValueError, 'requires actual stored readback'):
                verify_receipt(value, acknowledgment(value, status='duplicate'), 'pythonanywhere')
            response = proof(value)
            for key, wrong in (('stored_source_id', '999'), ('stored_capture_slot', '2020-01-01T00:00:00+00:00'),
                ('stored_section_count', 9), ('stored_section_count', True),
                ('stored_observation_version', 2), ('stored_observation_version', True),
                ('stored_observation_sha256', '0'*64)):
                changed = dict(response); changed[key] = wrong
                with self.subTest(sport=sport, key=key), self.assertRaises(ValueError):
                    verify_receipt(value, changed, 'pythonanywhere')

    def test_verified_duplicate_ack_survives_release_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            value = payload(); queue = MirrorQueue(directory, 'nfl')
            queue.acknowledge(value, 'pythonanywhere', proof(value, status='duplicate'))
            queue.acknowledge(value, 'tidb', acknowledgment(value, 'tidb'))
            restarted = MirrorQueue(directory, 'nfl').records()[0][1]
            self.assertNotIn('payload', restarted)
            self.assertEqual(restarted['acknowledged']['pythonanywhere']['stored_observation_sha256'],
                proof(value)['stored_observation_sha256'])
            self.assertEqual(MirrorQueue(directory, 'nfl').pending('pythonanywhere'), [])

    def test_conflicting_or_unverified_pa_duplicate_kept_but_other_game_delivers(self):
        for supplied_proof in (False, True):
            with self.subTest(supplied_proof=supplied_proof), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); conflict = payload(pid='6493143'); unrelated = payload(pid='6489565')
                calls = []
                def post(endpoint, token, value, **kwargs):
                    calls.append(value['source_id'])
                    if value['source_id'] == unrelated['source_id']:
                        return proof(value)
                    response = proof(value, status='duplicate') if supplied_proof else acknowledgment(value, status='duplicate')
                    if supplied_proof:
                        response['stored_observation_sha256'] = '0'*64
                    return response
                with patch.dict(os.environ, {'COLLECTOR_INGEST_TOKEN': 'test-token'}, clear=True), \
                     patch.object(collector, 'post_snapshot_with_retry', post), \
                     patch('sys.stdout', StringIO()), patch('sys.stderr', StringIO()):
                    code = run_legacy('nfl', root/'mirror', root/'pending', root/'health.json',
                        saved=[conflict, unrelated])
                self.assertEqual(code, 1)
                self.assertEqual(set(calls), {conflict['source_id'], unrelated['source_id']})
                by_id = {record['source_id']: record for _, record in MirrorQueue(root/'mirror', 'nfl').records()}
                self.assertIsNone(by_id[conflict['source_id']]['acknowledged']['pythonanywhere'])
                self.assertEqual(by_id[conflict['source_id']]['payload'], conflict)
                self.assertIsNotNone(by_id[unrelated['source_id']]['acknowledged']['pythonanywhere'])
                self.assertEqual(len(list((root/'pending').glob('*.rejected'))), 1)
                report = json.loads((root/'health.json').read_text())
                self.assertEqual((report['captured'], report['replayed'], report['pending']), (0, 1, 1))
                self.assertIn('observation conflict or unverified receipt', report['errors'][0])


if __name__ == '__main__':
    unittest.main()
