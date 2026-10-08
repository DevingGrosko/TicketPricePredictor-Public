"""Offline one-capture import: actual receiver and raw-store parity."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from flask import Flask
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from tools.import_shared_snapshot import (
    deliver_pythonanywhere, deliver_tidb, load_payload, prepare, verify_receipt,
)
from tools.free_refresh_capture import models_for
from vivid_inventory import VividCaptureError


class ManualImportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = datetime.now(timezone.utc).replace(microsecond=123456)
        self.event = self.now + timedelta(hours=12)
        self.raw = {'global': [{'productionId': '7302493', 'productionName': 'Utah Mammoth at Boston Bruins',
            'mapTitle': 'TD Garden', 'listingCount': '10', 'venueCountry': 'US',
            'venueTimeZone': 'America/New_York', 'dte': '0', 'Authorization': 'never-copy'}],
            'tickets': [{'l': f'Lower {100+i}', 'p': str(70+i), 'aip': str(90+i),
                         'r': 'A', 'q': '2', 'cookie': 'never-copy'} for i in range(10)]}
        self.inventory = self.root / 'inventory.json'
        self.output = self.root / 'snapshot.json'

    def prepare(self):
        data = json.dumps(self.raw).encode(); self.inventory.write_bytes(data)
        proof = prepare(self.inventory, hashlib.sha256(data).hexdigest(),
            'https://www.vividseats.com/game/production/7302493', '7302493',
            self.now.isoformat(), self.event.isoformat(), self.output)
        return json.loads(self.output.read_text()), proof

    def test_complete_public_inventory_has_original_capture_time_and_no_transport_fields(self):
        value, proof = self.prepare()
        self.assertEqual(value['captured_at'], self.now.isoformat())
        self.assertEqual(value['event_date'], self.event.isoformat())
        self.assertEqual((proof['inventory_count'], proof['section_count']), (10, 10))
        self.assertNotIn('never-copy', self.output.read_text())
        self.assertEqual(value['schedule']['country'], 'US')
        self.assertEqual(value['sections'][0]['price'], 70)
        self.assertEqual(value['sections'][0]['alternate_price'], '90')
        self.assertEqual(load_payload(self.output, proof['payload_sha256']), value)

    def test_wrong_hash_identity_incomplete_inventory_and_naive_time_are_rejected(self):
        self.prepare()
        data = self.inventory.read_bytes()
        args = [self.inventory, hashlib.sha256(data).hexdigest(),
            'https://www.vividseats.com/game/production/7302493', '7302493',
            self.now.isoformat(), self.event.isoformat(), self.output]
        for index, change in ((1, '0'*64), (3, '9999999'), (4, self.now.replace(tzinfo=None).isoformat())):
            mutated = args.copy(); mutated[index] = change
            with self.subTest(change=change), self.assertRaises((ValueError, VividCaptureError)):
                prepare(*mutated)
        self.raw['global'][0]['listingCount'] = '11'
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.prepare()

    def test_separate_credential_scopes_are_required(self):
        value, _ = self.prepare()
        send = Mock()
        with patch.dict(os.environ, {'COLLECTOR_INGEST_TOKEN': 'private', 'TIDB_STAGING_PASSWORD': 'separate'}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'separate credential'):
                deliver_pythonanywhere(value, send=send)
        send.assert_not_called()
        with patch.dict(os.environ, {'TICKETSIGNAL_STAGING_SITE': '1',
                'COLLECTOR_INGEST_TOKEN': 'private'}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'COLLECTOR_INGEST_TOKEN'):
                deliver_tidb(value)

    def test_actual_pythonanywhere_receiver_and_tidb_adapter_store_identical_prices_once(self):
        from Flask_App.nhl_blueprint import nhl_blueprint, CreateNHLModel
        value, _ = self.prepare()
        pa_db = self.root / 'pythonanywhere.db'
        tidb_db = self.root / 'tidb.db'
        Event, Iteration, Ticket = models_for('nhl')
        engine = create_engine('sqlite:///' + str(tidb_db))
        Event.metadata.create_all(engine); engine.dispose()
        def writer(sport):
            self.assertEqual(sport, 'nhl')
            return create_engine('sqlite:///' + str(tidb_db))
        app = Flask(__name__); app.register_blueprint(nhl_blueprint)
        client = app.test_client()
        captures = []
        def post(endpoint, token, observation, **kwargs):
            self.assertEqual(endpoint, 'https://bunnyjeff.pythonanywhere.com/api/nhl/snapshot')
            captures.append(observation['captured_at'])
            response = client.post('/api/nhl/snapshot', json=observation,
                                   headers={'Authorization': 'Bearer ' + token})
            self.assertIn(response.status_code, (200, 201))
            return response.get_json()
        env = {'COLLECTOR_INGEST_TOKEN': 'private-test', 'NHL_DATABASE_PATH': str(pa_db),
               'NHL_AUDIT_DIR': str(self.root/'audit'), 'NHL_BACKUP_DIR': str(self.root/'backup'),
               'TICKETSIGNAL_DATABASE_BACKEND': 'sqlite'}
        with patch.dict(os.environ, env, clear=True), \
             patch('Flask_App.nhl_blueprint.create_nhl_daily_backup'), \
             patch('Flask_App.nhl_blueprint.write_nhl_audit'):
            first_pa = deliver_pythonanywhere(value, send=post)
            again_pa = deliver_pythonanywhere(value, send=post)
        first_tidb = deliver_tidb(value, writer=writer)
        again_tidb = deliver_tidb(value, writer=writer)
        self.assertEqual((first_pa['status'], first_tidb['status']), ('stored', 'stored'))
        self.assertEqual((again_pa['status'], again_tidb['status']), ('duplicate', 'duplicate'))
        self.assertEqual(captures, [value['captured_at'], value['captured_at']])
        self.assertEqual(first_pa['captured_at'], first_tidb['captured_at'])
        expected = sorted((r['section'], r['price'], r['listing_count']) for r in value['sections'])
        for path in (pa_db, tidb_db):
            engine = create_engine('sqlite:///' + str(path))
            try:
                with Session(engine) as session:
                    self.assertEqual(session.scalar(select(func.count()).select_from(Iteration)), 1)
                    rows = session.scalars(select(Ticket)).all()
                    self.assertEqual(sorted((r.section, r.price, r.listing_count) for r in rows), expected)
            finally:
                engine.dispose()

    def test_real_prepared_observation_has_exact_completion_time_and_expected_section_count(self):
        path = Path(__file__).resolve().parents[1] / 'docs/manual-capture-7302493-20261008.json'
        value = json.loads(path.read_text())
        self.assertEqual(value['captured_at'], '2026-10-08T02:59:13.786678+00:00')
        self.assertEqual(value['event_date'], '2026-10-08T23:00:00+00:00')
        self.assertEqual(value['source_id'], '7302493')
        self.assertEqual(value['section_count'], 60)

    def test_workflow_is_manual_only_and_separates_existing_secrets(self):
        import yaml
        path = Path(__file__).resolve().parents[1] / '.github/workflows/nhl-smoke-test.yml'
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow['on']), {'workflow_dispatch'})
        pa = workflow['jobs']['pythonanywhere']
        tidb = workflow['jobs']['tidb']
        self.assertIn("inputs.destination == 'both'", pa['if'])
        self.assertEqual(tidb['environment'], 'tidb-staging')
        self.assertEqual(tidb['concurrency']['group'], 'free-refresh-staging-nhl-writer')
        pa_env = next(step['env'] for step in pa['steps'] if 'env' in step)
        tidb_env = next(step['env'] for step in tidb['steps'] if 'env' in step)
        self.assertEqual(set(pa_env), {'COLLECTOR_INGEST_TOKEN'})
        self.assertNotIn('COLLECTOR_INGEST_TOKEN', tidb_env)
        self.assertEqual(workflow['env']['SNAPSHOT_SHA256'],
            '5376f1333dd4fbd7881ca79c721cc9a5eb60d4b7dc858a3c4e3fa8864c82dba5')


if __name__ == '__main__':
    unittest.main()
