from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import yaml

from nhl_schedule_collector import ScheduledNHLGame
from tests.test_nhl_official_identity import FIXTURE, DALLAS_FIXTURE, capture_fields
from tools.nhl_time_identity_canary import run, TARGETS, DALLAS_TARGETS
from tools.shared_capture import identity
from vivid_inventory import VividCaptureError
from vivid_webkit import WebKitInventorySession, verified_event_date


AT = datetime(2026, 10, 8, 6, tzinfo=timezone.utc)


class FakeProductionBrowser:
    def __init__(self, *, deny=False):
        self._webkit_session = WebKitInventorySession.__new__(WebKitInventorySession)
        self.capture_diagnostics = dict(engine='webkit', responses=[])
        self.closed = False
        self.deny = deny

    def capture(self, url, **_options):
        pid = url.rsplit('/',1)[-1]
        if self.deny:
            raise VividCaptureError('provider-access-denied', self.capture_diagnostics)
        record = next(row for row in FIXTURE['games']+DALLAS_FIXTURE['games'] if str(row['provider']['id']) == pid)
        _, expected, _, metadata, body = capture_fields(record)
        stamp = verified_event_date(metadata, body, pid, self._webkit_session.expected_dates[pid],
            official_game=self._webkit_session.official_games[pid], diagnostics=self.capture_diagnostics)
        self.capture_diagnostics.update(production_id=pid,
            responses=[dict(path='/hermes/api/v1/listings',status=200)])
        return body, stamp

    def close(self):
        self.closed = True


def official_games(records=None):
    result = []
    for row in records or FIXTURE['games']:
        context = row['official']
        result.append(ScheduledNHLGame(schedule_id=context['schedule_id'],
            event_date=datetime.fromisoformat(context['event_date']), away_team=context['away_team'],
            home_team=context['home_team'], venue=context['venue'], venue_timezone=context['venue_timezone'],
            name=context['away_team']+' at '+context['home_team']))
    return result


class NHLTimeCanaryTests(unittest.TestCase):
    def test_real_production_resolution_parser_and_official_config_export_three_public_observations(self):
        created=[]
        def factory(**options):
            self.assertEqual(options,dict(headless=False,timeout=45))
            browser=FakeProductionBrowser();created.append(browser);return browser
        fetch=Mock(return_value=(official_games(),['https://api-web.nhle.com/v1/schedule/2026-11-05']))
        with TemporaryDirectory() as directory, patch('collector.post_snapshot_with_retry') as upload, \
             patch('models.create_ticket_engine',side_effect=AssertionError('No database connection')) as engine:
            report=run(directory,fetcher=fetch,factory=factory,now=AT)
            self.assertEqual(report['status'],'passed')
            fetch.assert_called_once_with(AT)
            self.assertEqual(len(created),3);self.assertTrue(all(browser.closed for browser in created))
            self.assertTrue(all(row['closed'] for row in report['browser_sessions']))
            self.assertEqual([row['source_id'] for row in report['observations']],[t[1] for t in TARGETS])
            for row in report['observations']:
                blob=Path(directory,row['payload_file']).read_bytes()
                self.assertEqual(hashlib.sha256(blob).hexdigest(),row['payload_sha256'])
                payload=json.loads(blob)
                self.assertEqual(identity('nhl',payload)[0],row['source_id'])
                self.assertEqual(payload['event_date'],row['official_event_date'])
                self.assertEqual(payload['schedule']['schedule_id'],row['schedule_id'])
                self.assertEqual(row['inventory_listing_count'],12)
                self.assertEqual(row['section_count'],12)
                self.assertEqual(row['diagnostics']['event_time_validation']['official_utc'],row['official_event_date'])
                raw=Path(directory,row['inventory_file']).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(),row['inventory_sha256'])
                self.assertEqual(len(json.loads(raw)['tickets']),12)
            upload.assert_not_called();engine.assert_not_called()

    def test_missing_official_identity_stops_before_any_browser(self):
        factory=Mock()
        with TemporaryDirectory() as directory:
            report=run(directory,fetcher=lambda _: (official_games()[:-1],[]),factory=factory,now=AT)
        self.assertEqual(report['status'],'failed');self.assertEqual(report['observations'],[])
        factory.assert_not_called()

    def test_denial_preserves_first_payload_and_stops_remaining_capture(self):
        first,second=FakeProductionBrowser(),FakeProductionBrowser(deny=True)
        factory=Mock(side_effect=[first,second])
        with TemporaryDirectory() as directory:
            report=run(directory,fetcher=lambda _: (official_games(),[]),factory=factory,now=AT)
            self.assertEqual(report['status'],'failed');self.assertTrue(report['stopped_after_access_denial'])
            self.assertEqual(len(report['observations']),2)
            self.assertEqual(report['observations'][0]['status'],'captured')
            self.assertEqual(report['observations'][1]['category'],'provider-access-denied')
            self.assertTrue(Path(directory,report['observations'][0]['payload_file']).exists())
            self.assertTrue(first.closed and second.closed)
        self.assertEqual(factory.call_count,2)

    def test_previous_observation_files_cannot_be_overwritten(self):
        with TemporaryDirectory() as directory:
            path=Path(directory,'old-observation.json');path.write_text('original')
            with self.assertRaises(ValueError):
                run(directory,fetcher=Mock(),factory=Mock(),now=AT)
            self.assertEqual(path.read_text(),'original')

    def test_dallas_option_fetches_only_its_two_fixed_official_identities_and_preserves_payloads(self):
        created=[]
        def factory(**_options):
            browser=FakeProductionBrowser();created.append(browser);return browser
        fetch=Mock(return_value=(official_games(DALLAS_FIXTURE['games']),['official-source']))
        with TemporaryDirectory() as directory:
            report=run(directory,fetcher=fetch,factory=factory,now=AT,cohort='dallas')
            self.assertEqual(report['status'],'passed');self.assertEqual(report['cohort'],'dallas')
            self.assertEqual([row['source_id'] for row in report['observations']],[t[1] for t in DALLAS_TARGETS])
            self.assertEqual(len(created),2);self.assertTrue(all(browser.closed for browser in created))
            fetch.assert_called_once_with(AT)
            for row in report['observations']:
                saved=json.loads(Path(directory,row['payload_file']).read_text())
                self.assertEqual(saved['venue'],'American Airlines Center - TX')
                self.assertEqual(saved['schedule']['canonical_venue'],'American Airlines Center')
                self.assertEqual(saved['event_date'],row['official_event_date'])
                self.assertEqual(identity('nhl',saved)[0],row['source_id'])
                self.assertTrue(Path(directory,row['inventory_file']).exists())
        with TemporaryDirectory() as directory,self.assertRaises(ValueError):
            run(directory,fetcher=Mock(),factory=Mock(),now=AT,cohort='arbitrary')

    def test_workflow_manual_mode_has_no_secrets_stores_or_other_browser_jobs(self):
        root=Path(__file__).resolve().parents[1]
        workflow=yaml.load((root/'.github/workflows/nhl-smoke-test.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertIn('nhl_time_canary',workflow['on']['workflow_dispatch']['inputs']['mode']['options'])
        job=workflow['jobs']['nhl-time-canary']
        self.assertEqual(job['if'],"github.event_name == 'workflow_dispatch' && inputs.mode == 'nhl_time_canary'")
        self.assertIn("inputs.mode != 'nhl_time_canary'",workflow['jobs']['capture-nhl']['if'])
        self.assertNotIn('nhl_time_canary',workflow['jobs']['firefox-canary']['if'])
        self.assertNotIn('secrets.',json.dumps(job))
        capture=next(s for s in job['steps'] if s.get('name','').startswith('Validate the fixed NHL'))
        self.assertEqual(capture['env']['TICKETSIGNAL_BROWSER_ENGINE'],'webkit')
        self.assertIn('inputs.nhl_identity_cohort',capture['env']['NHL_IDENTITY_COHORT'])
        self.assertEqual(workflow['on']['workflow_dispatch']['inputs']['nhl_identity_cohort']['options'],['canada','dallas'])
        self.assertIn('300s',capture['run']);self.assertIn('tools.nhl_time_identity_canary',capture['run'])
        offline=next(s for s in workflow['jobs']['capture-nhl']['steps'] if s.get('name','').startswith('Validate saved'))
        for module in ('tests.test_nhl_official_identity','tests.test_nhl_time_identity_canary'):
            self.assertIn(module,offline['run'])
        self.assertIn('tools/nhl_time_identity_canary.py',workflow['on']['pull_request']['paths'])


if __name__=='__main__':
    unittest.main()
