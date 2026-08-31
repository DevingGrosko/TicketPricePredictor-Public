from datetime import datetime, timedelta, timezone
import unittest

from nfl_collector import DiscoveredNFLGame
from nfl_schedule_collector import (
    EVENT_TIME_TOLERANCE_HOURS,
    ScheduledNFLGame,
    is_us_venue_country,
    candidates_for_schedule_game,
    matchup_key_from_title,
    ordered_matchup_from_title,
    parse_schedule_payload,
    schedule_cadence_summary,
    schedule_games_due,
    schedule_url,
    validate_captured_match,
)


class NFLScheduleParsingTests(unittest.TestCase):
    def _event(
        self,
        event_id: str,
        start: datetime,
        away: str,
        home: str,
        *,
        completed: bool = False,
        country: str = "USA",
    ):
        return {
            "id": event_id,
            "date": start.isoformat().replace("+00:00", "Z"),
            "name": f"{away} at {home}",
            "competitions": [
                {
                    "id": event_id,
                    "venue": {
                        "fullName": "Test Stadium",
                        "address": {"country": country},
                    },
                    "status": {"type": {"completed": completed}},
                    "competitors": [
                        {
                            "homeAway": "away",
                            "team": {"displayName": away},
                        },
                        {
                            "homeAway": "home",
                            "team": {"displayName": home},
                        },
                    ],
                }
            ],
        }

    def test_parses_every_due_nfl_game_and_excludes_outside_window(self):
        now = datetime(2026, 9, 6, 12, tzinfo=timezone.utc)
        payload = {
            "events": [
                self._event(
                    "1",
                    now + timedelta(hours=72),
                    "New England Patriots",
                    "Seattle Seahawks",
                ),
                self._event(
                    "2",
                    now + timedelta(hours=167),
                    "Buffalo Bills",
                    "Houston Texans",
                ),
                self._event(
                    "3",
                    now + timedelta(hours=400),
                    "Dallas Cowboys",
                    "New York Giants",
                ),
                self._event(
                    "6",
                    now + timedelta(hours=719),
                    "Baltimore Ravens",
                    "Pittsburgh Steelers",
                ),
                self._event(
                    "7",
                    now + timedelta(hours=721),
                    "Miami Dolphins",
                    "New York Jets",
                ),
                self._event(
                    "8",
                    now + timedelta(hours=100),
                    "San Francisco 49ers",
                    "Los Angeles Rams",
                    country="Australia",
                ),
                self._event(
                    "4",
                    now + timedelta(hours=24),
                    "Detroit Lions",
                    "Indianapolis Colts",
                    completed=True,
                ),
                self._event(
                    "5",
                    now + timedelta(hours=48),
                    "McGill Redbirds",
                    "Georgia Bulldogs",
                ),
            ]
        }

        games = parse_schedule_payload(payload, now)

        self.assertEqual([game.schedule_id for game in games], ["1", "2", "3", "6"])
        self.assertEqual(games[0].away_team, "New England Patriots")
        self.assertEqual(games[0].home_team, "Seattle Seahawks")
        self.assertEqual(games[0].venue, "Test Stadium")

    def test_exact_720_hour_boundary_is_included(self):
        now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        payload = {
            "events": [
                self._event(
                    "boundary",
                    now + timedelta(hours=720),
                    "Baltimore Ravens",
                    "Pittsburgh Steelers",
                )
            ]
        }
        games = parse_schedule_payload(payload, now)
        self.assertEqual(len(games), 1)

    def test_schedule_url_requests_a_month_range_and_large_limit(self):
        now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        url = schedule_url(now, 720)
        self.assertIn("dates=20260901-20261002", url)
        self.assertIn("limit=1000", url)

    def test_schedule_due_filter_staggers_longer_window_games(self):
        slot = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        games = [
            ScheduledNFLGame(
                schedule_id="final-week",
                event_date=slot + timedelta(hours=100),
                away_team="Buffalo Bills",
                home_team="Houston Texans",
                venue="Test Stadium",
                name="Buffalo Bills at Houston Texans",
            )
        ] + [
            ScheduledNFLGame(
                schedule_id=f"early-{index}",
                event_date=slot + timedelta(hours=500),
                away_team="Dallas Cowboys",
                home_team="New York Giants",
                venue="Test Stadium",
                name="Dallas Cowboys at New York Giants",
            )
            for index in range(12)
        ]
        due = schedule_games_due(games, slot)
        self.assertIn("final-week", {game.schedule_id for game in due})
        self.assertLess(len(due), len(games))
        summary = schedule_cadence_summary(games, slot)
        self.assertEqual(summary["in_window"]["1h"], 1)
        self.assertEqual(summary["in_window"]["6h"], 12)
        self.assertEqual(sum(summary["due_now"].values()), len(due))

    def test_explicit_non_us_venues_are_out_of_scope(self):
        self.assertTrue(is_us_venue_country("USA"))
        self.assertTrue(is_us_venue_country("United States"))
        self.assertTrue(is_us_venue_country(""))
        self.assertFalse(is_us_venue_country("Australia"))
        self.assertFalse(is_us_venue_country("Brazil"))


class NFLVividResolutionTests(unittest.TestCase):
    def setUp(self):
        self.game = ScheduledNFLGame(
            schedule_id="401000001",
            event_date=datetime(2026, 9, 13, 17, tzinfo=timezone.utc),
            away_team="Buffalo Bills",
            home_team="Houston Texans",
            venue="Reliant Stadium",
            name="Buffalo Bills at Houston Texans",
        )

    def test_matchup_detection_ignores_promotional_prefixes(self):
        title = "Deals Available NFL Week 1 - Buffalo Bills at Houston Texans"
        self.assertEqual(matchup_key_from_title(title), self.game.matchup_key)
        self.assertEqual(
            ordered_matchup_from_title(title),
            ("Buffalo Bills", "Houston Texans"),
        )

    def test_reverse_home_away_matchup_is_rejected(self):
        scheduled = ScheduledNFLGame(
            schedule_id="rams-home",
            event_date=datetime(2026, 9, 17, 0, 15, tzinfo=timezone.utc),
            away_team="San Francisco 49ers",
            home_team="Los Angeles Rams",
            venue="SoFi Stadium",
            name="San Francisco 49ers at Los Angeles Rams",
        )
        reverse = DiscoveredNFLGame(
            url=(
                "https://www.vividseats.com/san-francisco-49ers-tickets-"
                "santa-clara-levis-stadium/production/6495873"
            ),
            title="Los Angeles Rams at San Francisco 49ers",
            date_hint=datetime(2026, 12, 13).date(),
        )
        correct = DiscoveredNFLGame(
            url=(
                "https://www.vividseats.com/los-angeles-rams-tickets-"
                "inglewood-sofi-stadium/production/7000001"
            ),
            title="San Francisco 49ers at Los Angeles Rams",
            date_hint=datetime(2026, 9, 16).date(),
        )

        candidates = candidates_for_schedule_game(scheduled, [reverse, correct])
        self.assertEqual([row.url for row in candidates], [correct.url])
        self.assertEqual(candidates_for_schedule_game(scheduled, [reverse]), ())

    def test_candidate_matching_does_not_trust_vivid_slug_date(self):
        rows = [
            DiscoveredNFLGame(
                url=(
                    "https://www.vividseats.com/houston-texans-tickets-houston-"
                    "nrg-stadium-3-6-2026/production/6490070"
                ),
                title="Buffalo Bills at Houston Texans",
                date_hint=datetime(2026, 3, 6).date(),
            )
        ]
        candidates = candidates_for_schedule_game(self.game, rows)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].url, rows[0].url)

    def test_same_date_candidate_is_preferred_when_duplicate_matchups_exist(self):
        wrong_date = DiscoveredNFLGame(
            url="https://www.vividseats.com/a/production/1",
            title="Buffalo Bills at Houston Texans",
            date_hint=datetime(2026, 3, 6).date(),
        )
        correct_date = DiscoveredNFLGame(
            url="https://www.vividseats.com/b/production/2",
            title="Buffalo Bills at Houston Texans",
            date_hint=datetime(2026, 9, 13).date(),
        )
        candidates = candidates_for_schedule_game(
            self.game,
            [wrong_date, correct_date],
        )
        self.assertEqual([row.url for row in candidates], [correct_date.url])

    def test_captured_event_must_match_teams_and_reasonable_kickoff(self):
        validate_captured_match(
            self.game,
            self.game.event_date + timedelta(hours=2),
            "Buffalo Bills at Houston Texans",
        )

        with self.assertRaisesRegex(ValueError, "scheduled teams"):
            validate_captured_match(
                self.game,
                self.game.event_date,
                "Dallas Cowboys at New York Giants",
            )

        with self.assertRaisesRegex(ValueError, "away/home order"):
            validate_captured_match(
                self.game,
                self.game.event_date,
                "Houston Texans at Buffalo Bills",
            )

        with self.assertRaisesRegex(ValueError, "differs"):
            validate_captured_match(
                self.game,
                self.game.event_date
                + timedelta(hours=EVENT_TIME_TOLERANCE_HOURS + 1),
                "Buffalo Bills at Houston Texans",
            )


if __name__ == "__main__":
    unittest.main()
