"""Scope regressions: original MLB home venues only; no network or database."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import tempfile
import unittest

from tools import free_live_mlb_schedule as mlb
from tools.free_live_mlb_scope import (
    quarantine_out_of_scope, retain_scoped_events, tracked_venues, venue_in_scope,
)

NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)


def official(identity, venue, away='Colorado Rockies', home='Washington Nationals'):
    return dict(gamePk=identity, gameType='R', gameDate=(NOW + timedelta(hours=8)).isoformat(),
                status={'abstractGameState': 'Preview'}, venue={'name': venue},
                teams={'away': {'team': {'name': away}}, 'home': {'team': {'name': home}}})


class ScopeTests(unittest.TestCase):
    def test_original_ten_venues_are_the_only_source_of_scope(self):
        self.assertEqual(set(tracked_venues()), {
            'Nationals Park', 'Citi Field', 'Citizens Bank Park', 'Truist Park',
            'Wrigley Field', 'Dodger Stadium', 'Busch Stadium', 'Yankee Stadium',
            'Fenway Park', 'Oriole Park at Camden Yards',
        })
        import collector
        with patch.dict(collector.VENUE_FEEDS, {'Nationals Park': 'fixture'}, clear=True):
            self.assertTrue(venue_in_scope('Nationals Park'))
            self.assertFalse(venue_in_scope('Yankee Stadium'))

    def test_same_building_aliases_work_without_accepting_parking_or_extra_venues(self):
        for venue in (*tracked_venues(), 'Uniqlo Field at Dodger Stadium', 'Camden Yards', 'citi field'):
            with self.subTest(venue=venue):
                self.assertTrue(venue_in_scope(venue))
        for venue in ('Coors Field', 'Petco Park', 'Daikin Park', 'Steinbrenner Field',
                      'Truist Park Parking', '', None):
            with self.subTest(venue=venue):
                self.assertFalse(venue_in_scope(venue))

    def test_tracked_away_team_does_not_expand_home_venue_scope(self):
        rows = [official(1, 'Nationals Park'),
                official(2, 'Coors Field', 'Washington Nationals', 'Colorado Rockies'),
                official(3, 'Petco Park', 'Chicago Cubs', 'San Diego Padres')]
        result = mlb.schedule_games({'dates': [{'games': rows}]}, NOW)
        self.assertEqual([g['schedule_id'] for g in result], ['1'])

    def test_provider_venue_must_match_official_configured_venue(self):
        import collector
        game = dict(away_team='Colorado Rockies', home_team='Washington Nationals',
                    venue='Nationals Park', event_date=(NOW + timedelta(hours=8)).isoformat())
        snapshot = SimpleNamespace(source_id='123', title='Colorado Rockies at Washington Nationals',
                                   venue='Coors Field')
        with patch.object(collector.SnapshotParser, 'parse', return_value=snapshot):
            with self.assertRaises(ValueError):
                mlb.validate_match(game, 'https://www.vividseats.com/production/123', {},
                                   NOW + timedelta(hours=8))

    def test_outside_pending_payloads_are_preserved_but_not_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            pending = Path(directory) / 'pending'; pending.mkdir()
            outside = json.dumps({'venue': 'Petco Park', 'id': 'outside'}).encode()
            (pending / 'outside.json').write_bytes(outside)
            (pending / 'duplicate.rejected').write_bytes(outside)
            (pending / 'inside.json').write_text(json.dumps({'venue': 'Nationals Park'}))
            (pending / 'invalid.json').write_text('not json')
            self.assertEqual(quarantine_out_of_scope(pending), ['duplicate.rejected', 'outside.json'])
            self.assertEqual(sorted(p.name for p in pending.iterdir()), ['inside.json', 'invalid.json'])
            saved = list((Path(directory) / 'out-of-scope-payloads').glob('*.json'))
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].read_bytes(), outside)
            self.assertEqual(quarantine_out_of_scope(pending), [])

    def test_restored_outside_backlog_cannot_bypass_new_schedule_filter(self):
        import collector
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory); pending = root / 'pending'; pending.mkdir()
            outside = dict(schedule_id='2', venue='Petco Park',
                           event_date=(datetime.now(timezone.utc) + timedelta(hours=8)).isoformat())
            (root / 'mlb-schedule-progress.json').write_text(json.dumps({'pending': {'2': outside}}))
            (pending / 'outside.json').write_text(json.dumps({'venue': 'Petco Park'}))
            def replay(*args):
                self.assertFalse((pending / 'outside.json').exists())
                return 0, True, []
            stack.enter_context(patch.object(collector, 'replay_pending_snapshots', side_effect=replay))
            stack.enter_context(patch.object(mlb, 'fetch_schedule', return_value=([outside], 'fixture')))
            capture = stack.enter_context(patch.object(mlb, 'capture_game'))
            self.assertEqual(mlb.run_mlb('', '', True, 1, root / 'health.json', pending), 0)
            capture.assert_not_called()
            health = json.loads((root / 'health.json').read_text())
            self.assertEqual(health['out_of_scope_games'], ['2'])
            self.assertEqual(health['scheduled_due'], 0)
            self.assertEqual(health['unfinished_games'], [])
            self.assertEqual(health['coverage_mode'], 'configured-MLB-venues')

    def test_publication_filters_games_and_freshness_without_mutating_history(self):
        events = {1: SimpleNamespace(Place='Nationals Park'),
                  2: SimpleNamespace(Place='Daikin Park'),
                  3: SimpleNamespace(Place='Uniqlo Field at Dodger Stadium')}
        latest = {1: NOW, 2: NOW + timedelta(hours=1), 3: NOW}
        captures = {1: 3, 2: 1, 3: 4}
        counts = {'games': 3, 'captures': 8, 'tickets': 80}
        original = (events, latest, captures, counts)
        actual = retain_scoped_events('mlb', original)
        for mapping in actual[:3]:
            self.assertEqual(set(mapping), {1, 3})
        self.assertEqual(max(actual[1].values()), NOW)
        self.assertEqual(set(events), {1, 2, 3})
        self.assertIs(actual[3], counts)
        for sport in ('nfl', 'nhl'):
            self.assertIs(retain_scoped_events(sport, original), original)

    def test_publication_reader_actually_applies_scope(self):
        from tools.free_live_publication import ScopedSnapshotCache
        from tools.free_refresh_cache import SnapshotCache
        events = {1: SimpleNamespace(Place='Nationals Park'), 2: SimpleNamespace(Place='Daikin Park')}
        result = (events, {1: NOW, 2: NOW}, {1: 1, 2: 1}, {'games': 2})
        def read(reader, sport, *args, **kwargs):
            reader.metrics[sport] = {}
            return result
        with tempfile.TemporaryDirectory() as directory, patch.object(SnapshotCache, 'read_sport', read):
            reader = ScopedSnapshotCache(directory)
            self.assertEqual(set(reader.read_sport('mlb')[0]), {1})
            self.assertEqual(reader.metrics['mlb']['out_of_scope_events_hidden'], 1)
