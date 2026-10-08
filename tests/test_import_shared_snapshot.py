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

    def prepare(self, sport='nhl'):
        raw = json.loads(json.dumps(self.raw))
        production_id = '7302493' if sport == 'nhl' else '6493143'
        if sport == 'nfl':
            raw['global'][0].update(productionId=production_id,
                productionName='Dallas Cowboys at New York Giants', mapTitle='MetLife Stadium')
        inventory = self.inventory if sport == 'nhl' else self.root / 'inventory-nfl.json'
        output = self.output if sport == 'nhl' else self.root / 'snapshot-nfl.json'
        data = json.dumps(raw).encode(); inventory.write_bytes(data)
        proof = prepare(inventory, hashlib.sha256(data).hexdigest(),
            'https://www.vividseats.com/game/production/' + production_id, production_id,
            self.now.isoformat(), self.event.isoformat(), output, sport=sport)
        return json.loads(output.read_text()), proof

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

    def test_every_raw_listing_must_be_valid_even_when_other_sections_exceed_minimum(self):
        original = json.loads(json.dumps(self.raw))
        original['tickets'].extend([
            {'l': 'Lower 110', 'p': '80', 'q': '2'}, {'l': 'Lower 111', 'p': '81', 'q': '2'}])
        original['global'][0]['listingCount'] = '12'
        malformed = [{'l': ''}, {'l': '  '}, {'l': 123}, {'p': 'invalid'}, {'p': None},
                     {'p': True}, {'p': 'NaN'}, {'p': 'Infinity'}, {'p': '-1'}, {'p': '1e1000'},
                     {'q': '0'}, {'q': True}, {'q': '2.5'}, {'q': None}, 'not-a-listing']
        for sport in ('nhl', 'nfl'):
            for change in malformed:
                self.raw = json.loads(json.dumps(original))
                if isinstance(change, dict):
                    self.raw['tickets'][-1].update(change)
                else:
                    self.raw['tickets'][-1] = change
                with self.subTest(sport=sport, change=change), self.assertRaisesRegex(ValueError, 'invalid listing'):
                    self.prepare(sport)
        self.assertFalse(self.output.exists())
        self.assertFalse((self.root / 'snapshot-nfl.json').exists())

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
        from Flask_App.nhl_blueprint import nhl_blueprint
        from Flask_App.nfl_blueprint import nfl_blueprint
        for sport, blueprint in (('nhl', nhl_blueprint), ('nfl', nfl_blueprint)):
            with self.subTest(sport=sport):
                value, proof = self.prepare(sport)
                self.assertEqual(value['event_type'], sport)
                output = self.output if sport == 'nhl' else self.root / 'snapshot-nfl.json'
                self.assertEqual(load_payload(output, proof['payload_sha256']), value)
                pa_db = self.root / ('pythonanywhere-' + sport + '.db')
                tidb_db = self.root / ('tidb-' + sport + '.db')
                Event, Iteration, Ticket = models_for(sport)
                engine = create_engine('sqlite:///' + str(tidb_db))
                Event.metadata.create_all(engine); engine.dispose()
                def writer(requested_sport):
                    self.assertEqual(requested_sport, sport)
                    return create_engine('sqlite:///' + str(tidb_db))
                app = Flask(__name__); app.register_blueprint(blueprint)
                client = app.test_client()
                captures = []
                def post(endpoint, token, observation, **kwargs):
                    self.assertEqual(endpoint, 'https://bunnyjeff.pythonanywhere.com/api/' + sport + '/snapshot')
                    captures.append(observation['captured_at'])
                    response = client.post('/api/' + sport + '/snapshot', json=observation,
                                           headers={'Authorization': 'Bearer ' + token})
                    self.assertIn(response.status_code, (200, 201))
                    return response.get_json()
                env = {'COLLECTOR_INGEST_TOKEN': 'private-test', sport.upper() + '_DATABASE_PATH': str(pa_db),
                       'TICKETSIGNAL_DATABASE_BACKEND': 'sqlite'}
                with patch.dict(os.environ, env, clear=True), \
                     patch('Flask_App.' + sport + '_blueprint.create_' + sport + '_daily_backup'), \
                     patch('Flask_App.' + sport + '_blueprint.write_' + sport + '_audit'):
                    first_pa = deliver_pythonanywhere(value, send=post)
                    again_pa = deliver_pythonanywhere(value, send=post)
                first_tidb = deliver_tidb(value, writer=writer)
                again_tidb = deliver_tidb(value, writer=writer)
                self.assertEqual((first_pa['status'], first_tidb['status']), ('stored', 'stored'))
                self.assertEqual((again_pa['status'], again_tidb['status']), ('duplicate', 'duplicate'))
                self.assertTrue(first_tidb['price_readback_verified'])
                self.assertTrue(first_tidb['identity_readback_verified'])
                self.assertEqual(captures, [value['captured_at'], value['captured_at']])
                self.assertEqual(first_pa['captured_at'], first_tidb['captured_at'])
                self.assertEqual((first_pa['event_type'], first_tidb['event_type']), (sport, sport))
                expected = sorted((r['section'], r['price'], r['listing_count']) for r in value['sections'])
                for path in (pa_db, tidb_db):
                    engine = create_engine('sqlite:///' + str(path))
                    try:
                        with Session(engine) as session:
                            self.assertEqual(session.scalar(select(func.count()).select_from(Iteration)), 1)
                            event = session.scalars(select(Event)).one()
                            self.assertEqual((event.source_id, event.source_url),
                                             (value['source_id'], value['source_url']))
                            rows = session.scalars(select(Ticket)).all()
                            self.assertEqual(sorted((r.section, r.price, r.listing_count) for r in rows), expected)
                    finally:
                        engine.dispose()

    def test_unknown_sport_mislabeled_matchup_and_source_identity_fail_before_delivery(self):
        for sport in ('nhl', 'nfl'):
            value, _ = self.prepare(sport)
            changes = ({'event_type': 'mlb'}, {'event_type': 'nba'}, {'event_type': None},
                       {'event_type': 'nfl' if sport == 'nhl' else 'nhl'}, {'source_id': '9999999'})
            for change in changes:
                with self.subTest(sport=sport, change=change):
                    malformed = {**value, **change}
                    data = json.dumps(malformed).encode()
                    path = self.root / 'bad-snapshot.json'; path.write_bytes(data)
                    with self.assertRaises(ValueError):
                        load_payload(path, hashlib.sha256(data).hexdigest())
                    send, writer = Mock(), Mock()
                    with patch.dict(os.environ, {'COLLECTOR_INGEST_TOKEN': 'private-test'}, clear=True):
                        with self.assertRaises(ValueError):
                            deliver_pythonanywhere(malformed, send=send)
                        with self.assertRaises(ValueError):
                            deliver_tidb(malformed, writer=writer)
                    send.assert_not_called(); writer.assert_not_called()

    def test_receipts_must_identify_the_matching_sport_and_slot(self):
        from Flask_App.collection_cadence import half_hour_capture_slot
        for sport in ('nhl', 'nfl'):
            value, _ = self.prepare(sport)
            for destination, field in (('pythonanywhere', 'event_type'), ('tidb', 'sport')):
                response = {'status': 'stored', 'event_id': 1, 'iteration_id': 1,
                    'sections': value['section_count'], 'captured_at': half_hour_capture_slot(self.now).isoformat(),
                    field: sport}
                self.assertEqual(verify_receipt(value, response, destination)['event_type'], sport)
                for change in ({field: 'nfl' if sport == 'nhl' else 'nhl'}, {field: None},
                               {'captured_at': (self.now + timedelta(hours=1)).isoformat()}):
                    with self.subTest(sport=sport, destination=destination, change=change), self.assertRaises(ValueError):
                        verify_receipt(value, {**response, **change}, destination)

    def test_tidb_readback_rejects_wrong_prices_or_event_identity_for_both_sports(self):
        from tools.free_refresh_capture import store_payload
        for sport in ('nhl', 'nfl'):
            value, _ = self.prepare(sport)
            Event, _, Ticket = models_for(sport)
            for mutation in ('price', 'source_id'):
                with self.subTest(sport=sport, mutation=mutation):
                    engine = create_engine('sqlite:///:memory:')
                    Event.metadata.create_all(engine)
                    def corrupted_store(target, requested_sport, payload):
                        self.assertEqual(requested_sport, sport)
                        response = store_payload(target, requested_sport, payload)
                        with Session(target) as session, session.begin():
                            if mutation == 'price':
                                session.scalars(select(Ticket)).first().price += 1
                            else:
                                session.get(Event, response['event_id']).source_id = '9999999'
                        return response
                    with patch('tools.free_refresh_capture.store_payload', side_effect=corrupted_store):
                        with self.assertRaisesRegex(ValueError, 'differ|different observation'):
                            deliver_tidb(value, writer=lambda requested: engine)

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
        pa_env = next(step['env'] for step in pa['steps'] if 'env' in step)
        tidb_env = next(step['env'] for step in tidb['steps'] if 'env' in step)
        self.assertEqual(set(pa_env), {'COLLECTOR_INGEST_TOKEN'})
        self.assertNotIn('COLLECTOR_INGEST_TOKEN', tidb_env)
        import re
        saved = re.search(r'cp bridge/(docs/\S+\.json) source/import-input.json', path.read_text()).group(1)
        sport = json.loads((path.parents[2] / saved).read_text())['event_type']
        self.assertEqual(tidb['concurrency']['group'], f'free-refresh-staging-{sport}-writer')
        self.assertEqual(workflow['env']['SNAPSHOT_SHA256'],
            hashlib.sha256((path.parents[2] / saved).read_bytes()).hexdigest())


if __name__ == '__main__':
    unittest.main()
