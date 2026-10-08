from datetime import datetime, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tools.nhl_official_schedule_audit import audit


NOW = datetime(2026, 10, 8, 5, 30, tzinfo=timezone.utc)


def fixture():
    return {'gameWeek': [{'games': [
        dict(id=identity, startTimeUTC=stamp, venue={'default': venue}, venueTimezone=zone,
             venueUTCOffset=offset, easternUTCOffset='-05:00', gameType=2, gameState='FUT',
             awayTeam={'abbrev':away}, homeTeam={'abbrev':home}, unrelated_token='never exported')
        for identity, stamp, venue, zone, offset, away, home in (
            (2026020264,'2026-11-07T01:00:00Z','Scotiabank Saddledome','America/Edmonton','-07:00','ANA','CGY'),
            (2026020259,'2026-11-06T00:00:00Z','Canada Life Centre','America/Winnipeg','-06:00','VAN','WPG'),
            (2026020260,'2026-11-06T01:00:00Z','Rogers Place','Canada/Mountain','-07:00','ANA','EDM'),
        )]}]}


class OfficialScheduleAuditTests(unittest.TestCase):
    def test_fixed_ids_use_real_parser_and_only_public_whitelisted_fields_without_browser(self):
        with patch('nhl_schedule_collector.VividNFLBrowser', side_effect=AssertionError('No browser')):
            result = audit(fetcher=lambda url,timeout:fixture(), now=NOW)
        self.assertEqual(result['status'],'success')
        self.assertEqual([row['schedule_id'] for row in result['games']],['2026020259','2026020260','2026020264'])
        self.assertEqual([row['event_date'] for row in result['games']],
                         ['2026-11-06T00:00:00+00:00','2026-11-06T01:00:00+00:00','2026-11-07T01:00:00+00:00'])
        self.assertEqual(result['games'][0]['local_datetime'],'2026-11-05T18:00:00-06:00')
        self.assertEqual((result['browser_calls'],result['vivid_requests'],result['database_calls']),(0,0,0))
        self.assertNotIn('unrelated_token',json.dumps(result))
        self.assertNotIn('never exported',json.dumps(result))

    def test_missing_official_identity_fails_instead_of_inventing_expected_time(self):
        data=fixture();data['gameWeek'][0]['games'].pop()
        with self.assertRaisesRegex(ValueError,'all three exact'):
            audit(fetcher=lambda url,timeout:data,now=NOW)
        with self.assertRaises(ValueError):
            audit(fetcher=lambda url,timeout:fixture(),now=NOW.replace(tzinfo=None))

    def test_only_schedule_job_runs_for_new_manual_mode_without_secrets_or_browser_commands(self):
        text=(Path(__file__).resolve().parents[1]/'.github/workflows/nhl-smoke-test.yml').read_text()
        self.assertIn("inputs.mode != 'webkit_canary' && inputs.mode != 'official_schedule'",text)
        section=text.split('\n  official-schedule:\n',1)[1].split('\n  firefox-canary:\n',1)[0]
        self.assertIn("if: github.event_name == 'workflow_dispatch' && inputs.mode == 'official_schedule'",section)
        self.assertIn('python -m tools.nhl_official_schedule_audit',section)
        self.assertIn('retention-days: 1',section)
        for forbidden in ('secrets.','environment:','xvfb-run','chromedriver --version','playwright install','remote-run'):
            self.assertNotIn(forbidden,section)


if __name__=='__main__':unittest.main()
