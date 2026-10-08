from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nhl_collector import ordered_matchup_from_title
from tests.test_vivid_webkit import Browser, Clock, payload, session
from vivid_inventory import VividCaptureError
from vivid_performer_routes import configure_schedule_navigation
from vivid_webkit import verified_event_date


FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'nhl_official_provider_identity.json').read_text())


def capture_fields(record):
    public, context = record['provider'], deepcopy(record['official'])
    pid = str(public['id'])
    expected = datetime.fromisoformat(context['event_date'])
    metadata = dict(id=pid, page_id=pid, query_id=pid, utc_date=public['utcDate'],
                    title=public['name'], venue=public['venue']['name'], venue_id=public['venue']['id'])
    # The fixture preserves the real public event metadata; inventory rows are
    # synthetic, using the existing native contract and matching identity fields.
    body = payload(pid)
    body['global'][0].update(productionName=metadata['title'], mapTitle=metadata['venue'], venueId=metadata['venue_id'])
    return pid, expected, context, metadata, body


class OfficialNHLIdentityTests(unittest.TestCase):
    def test_three_actual_disagreements_use_official_utc_and_record_provider_evidence(self):
        differences = []
        for record in FIXTURE['games']:
            with self.subTest(schedule_id=record['official']['schedule_id']):
                pid, expected, context, metadata, body = capture_fields(record)
                diagnostics = {}
                stamp = verified_event_date(metadata, body, pid, expected,
                    official_game=context, diagnostics=diagnostics)
                self.assertEqual(stamp, expected)
                self.assertEqual(datetime.fromisoformat(record['official_fields']['startTimeUTC']), expected)
                # Provider raw UTC and its explicit local offset agree: this is
                # source disagreement, rather than our timestamp parser shifting it.
                local = record['provider']['localDate'].split('[')[0]
                self.assertEqual(datetime.fromisoformat(local).astimezone(timezone.utc),
                                 datetime.fromisoformat(metadata['utc_date']))
                evidence = diagnostics['event_time_validation']
                self.assertEqual(evidence['schedule_id'], context['schedule_id'])
                self.assertEqual(evidence['provider_utc'], datetime.fromisoformat(metadata['utc_date']).isoformat())
                self.assertEqual(evidence['official_utc'], expected.isoformat())
                differences.append(evidence['difference_seconds'])
        self.assertEqual(differences, [3600, 3600, -3600])

    def test_ordered_teams_official_venue_and_calendar_date_must_all_match(self):
        pid, expected, context, original, original_body = capture_fields(FIXTURE['games'][0])
        cases = [('home team', 'Vancouver Canucks at Edmonton Oilers', None, None),
                 ('reversed teams', 'Winnipeg Jets at Vancouver Canucks', None, None),
                 ('venue', None, 'Rogers Place', None),
                 ('calendar date', None, None, (expected + timedelta(days=1)).isoformat())]
        for name, title, venue, date in cases:
            with self.subTest(name=name):
                metadata, body = deepcopy(original), deepcopy(original_body)
                if title:
                    metadata['title'] = body['global'][0]['productionName'] = title
                if venue:
                    metadata['venue'] = body['global'][0]['mapTitle'] = venue
                if date:
                    metadata['utc_date'] = date
                with self.assertRaises(VividCaptureError):
                    verified_event_date(metadata, body, pid, expected, official_game=context)

    def test_one_hour_difference_crossing_venue_calendar_date_is_rejected(self):
        pid, _, context, metadata, body = capture_fields(FIXTURE['games'][0])
        expected = datetime(2026, 11, 6, 5, 30, tzinfo=timezone.utc)  # Winnipeg Nov 5, 23:30.
        context['event_date'] = expected.isoformat()
        metadata['utc_date'] = (expected + timedelta(hours=1)).isoformat()  # Nov 6, 00:30.
        with self.assertRaises(VividCaptureError) as error:
            verified_event_date(metadata, body, pid, expected, official_game=context)
        self.assertEqual(error.exception.category, 'event-metadata-time-mismatch')

    def test_missing_or_unknown_official_context_does_not_relax_exact_utc(self):
        pid, expected, context, metadata, body = capture_fields(FIXTURE['games'][0])
        for supplied in (None, {}, {**context, 'sport':'nfl'}, {**context, 'away_team':'Unknown Team'},
                         {**context, 'venue':''}, {**context, 'venue_timezone':'Unknown/Zone'},
                         {**context, 'event_date':expected.replace(tzinfo=None).isoformat()}):
            with self.subTest(context=supplied), self.assertRaises(VividCaptureError):
                verified_event_date(metadata, body, pid, expected, official_game=supplied)

    def test_nfl_and_standalone_capture_keep_exact_utc_requirement(self):
        pid, expected, _, metadata, body = capture_fields(FIXTURE['games'][0])
        metadata['title'] = body['global'][0]['productionName'] = 'Minnesota Vikings at New Orleans Saints'
        with self.assertRaises(VividCaptureError):
            verified_event_date(metadata, body, pid, expected)
        metadata['utc_date'] = expected.isoformat()
        self.assertEqual(verified_event_date(metadata, body, pid, expected), expected)

    def test_only_two_observed_exact_venue_aliases_are_equivalent(self):
        pid, expected, context, metadata, body = capture_fields(FIXTURE['games'][0])
        for provider, official in [('SAP Center','SAP Center at San Jose'), ('Bell Centre','Centre Bell')]:
            with self.subTest(provider=provider):
                metadata['venue'] = body['global'][0]['mapTitle'] = provider
                context['venue'] = official
                self.assertEqual(verified_event_date(metadata,body,pid,expected,official_game=context), expected)
                metadata['venue'] = body['global'][0]['mapTitle'] = provider + ' Annex'
                with self.assertRaises(VividCaptureError):
                    verified_event_date(metadata,body,pid,expected,official_game=context)

    def test_native_page_identity_and_venue_id_guards_still_precede_canonicalization(self):
        pid, expected, context, original, body = capture_fields(FIXTURE['games'][0])
        for key, value in [('id','999'), ('page_id','999'), ('query_id','999'), ('venue_id','999'), ('utc_date','not-a-date')]:
            with self.subTest(key=key), self.assertRaises(VividCaptureError):
                verified_event_date({**original,key:value},body,pid,expected,official_game=context)

    def test_schedule_configuration_capture_and_cleanup_preserve_official_anchor(self):
        pid, expected, context, metadata, body = capture_fields(FIXTURE['games'][2])
        clock = Clock()
        browser = Browser(clock,pid=pid,body=body,metadata=metadata)
        with patch('vivid_webkit._blocked_category',None), patch('vivid_webkit.time.monotonic',clock.monotonic), \
             patch('vivid_webkit.time.time',clock.time):
            adapter = session(clock,[browser])
            owner = SimpleNamespace(_webkit_session=adapter)
            game = SimpleNamespace(**{**context,'event_date':expected})
            url = 'https://www.vividseats.com/provider-slug/production/'+pid
            configure_schedule_navigation(owner,'nhl',game,url)
            clean, stamp = adapter.capture(url)
            self.assertEqual(stamp,expected)
            self.assertEqual(len(clean['tickets']),12)
            self.assertEqual(adapter.owner.capture_diagnostics['event_time_validation']['difference_seconds'],-3600)
            self.assertEqual(browser.context.body_reads,[pid])
            self.assertEqual(browser.context.clicks,1)
            adapter.close()
            self.assertTrue(browser.closed)
            self.assertEqual(set(browser.context.removed),{'response','requestfinished'})

    def test_invalid_reconfiguration_cannot_replace_valid_trusted_context(self):
        pid, expected, context, _, _ = capture_fields(FIXTURE['games'][0])
        adapter = session(Clock(),[])
        routes = {pid:'https://www.vividseats.com/winnipeg-jets-tickets--sports-nhl-hockey/performer/1707'}
        adapter.configure_normal_navigation(routes,{pid:expected},official_games={pid:context})
        for contexts in ({}, {'999':context}, {pid:{**context,'schedule_id':None}}, {pid:{**context,'unexpected':'field'}}):
            with self.subTest(contexts=contexts), self.assertRaises(ValueError):
                adapter.configure_normal_navigation(routes,{pid:expected},official_games=contexts)
            self.assertEqual(adapter.official_games,{pid:context})
        adapter.playwright.webkit.launch.assert_not_called()
        adapter.close()


if __name__ == '__main__':
    unittest.main()
