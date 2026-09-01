from datetime import datetime, timedelta, timezone
import unittest

from nhl_collector import DiscoveredNHLGame
from nhl_schedule_collector import (
    ScheduledNHLGame,
    candidates_for_schedule_game,
    fetch_schedule_games,
    game_type_label,
    parse_schedule_payload,
    schedule_cadence_summary,
    schedule_games_due,
    venue_timezone_is_supported,
)


class NHLScheduleCollectorTests(unittest.TestCase):
    def _game(
        self,
        game_id,
        start,
        away,
        home,
        *,
        venue="Test Arena",
        venue_timezone="America/New_York",
        game_type=2,
        game_state="FUT",
        neutral=False,
    ):
        return {
            "id": game_id,
            "season": 20262027,
            "gameType": game_type,
            "venue": {"default": venue},
            "neutralSite": neutral,
            "startTimeUTC": start.isoformat().replace("+00:00", "Z"),
            "venueTimezone": venue_timezone,
            "gameState": game_state,
            "awayTeam": {"abbrev": away},
            "homeTeam": {"abbrev": home},
        }

    def test_schedule_includes_us_and_canada_but_excludes_global_series(self):
        now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
        payload = {
            "gameWeek": [
                {
                    "date": "2026-09-21",
                    "games": [
                        self._game(
                            1,
                            now + timedelta(hours=24),
                            "BOS",
                            "TOR",
                            venue="Scotiabank Arena",
                            venue_timezone="America/Toronto",
                        ),
                        self._game(
                            2,
                            now + timedelta(hours=48),
                            "NYR",
                            "BOS",
                            venue="TD Garden",
                            venue_timezone="America/New_York",
                        ),
                        self._game(
                            3,
                            now + timedelta(hours=72),
                            "CAR",
                            "SEA",
                            venue="Helsinki Ice Hall",
                            venue_timezone="Europe/Helsinki",
                            neutral=True,
                        ),
                        self._game(
                            4,
                            now + timedelta(hours=169),
                            "DAL",
                            "STL",
                        ),
                    ],
                }
            ]
        }

        games = parse_schedule_payload(payload, now)
        self.assertEqual([game.schedule_id for game in games], ["1", "2"])
        self.assertEqual(games[0].country, "Canada")
        self.assertEqual(games[1].country, "USA")
        self.assertTrue(venue_timezone_is_supported("America/Toronto"))
        self.assertFalse(venue_timezone_is_supported("Europe/Helsinki"))

    def test_official_schedule_pagination_is_deduplicated(self):
        now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
        calls = []

        def fetcher(url, timeout):
            calls.append(url)
            if url.endswith("2026-09-20"):
                return {
                    "nextStartDate": "2026-09-27",
                    "gameWeek": [
                        {
                            "date": "2026-09-21",
                            "games": [
                                self._game(
                                    1,
                                    now + timedelta(hours=24),
                                    "BOS",
                                    "TOR",
                                    venue_timezone="America/Toronto",
                                )
                            ],
                        }
                    ],
                }
            return {
                "nextStartDate": "2026-10-04",
                "gameWeek": [
                    {
                        "date": "2026-09-27",
                        "games": [
                            self._game(
                                2,
                                now + timedelta(hours=160),
                                "NYR",
                                "BOS",
                            )
                        ],
                    }
                ],
            }

        games, sources = fetch_schedule_games(
            now,
            horizon_hours=168,
            fetcher=fetcher,
        )
        self.assertEqual([game.schedule_id for game in games], ["1", "2"])
        self.assertEqual(len(sources), 2)
        self.assertEqual(len(calls), 2)

    def test_due_filter_and_summary_apply_agreed_cadence(self):
        slot = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
        games = [
            ScheduledNHLGame(
                schedule_id="hourly",
                event_date=slot + timedelta(hours=48),
                away_team="Boston Bruins",
                home_team="Toronto Maple Leafs",
                venue="Scotiabank Arena",
                name="Boston Bruins at Toronto Maple Leafs",
                venue_timezone="America/Toronto",
                country="Canada",
            )
        ] + [
            ScheduledNHLGame(
                schedule_id=f"early-{index}",
                event_date=slot + timedelta(hours=120),
                away_team="New York Rangers",
                home_team="Boston Bruins",
                venue="TD Garden",
                name="New York Rangers at Boston Bruins",
                venue_timezone="America/New_York",
                country="USA",
            )
            for index in range(12)
        ]
        due = schedule_games_due(games, slot)
        self.assertIn("hourly", {game.schedule_id for game in due})
        self.assertLess(len(due), len(games))
        summary = schedule_cadence_summary(games, slot)
        self.assertEqual(summary["in_window"]["1h"], 1)
        self.assertEqual(summary["in_window"]["6h"], 12)
        self.assertEqual(sum(summary["due_now"].values()), len(due))

    def test_vivid_candidates_require_away_home_order_and_prefer_local_date(self):
        game = ScheduledNHLGame(
            schedule_id="2026020001",
            event_date=datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc),
            away_team="New York Islanders",
            home_team="Toronto Maple Leafs",
            venue="Scotiabank Arena",
            name="New York Islanders at Toronto Maple Leafs",
            venue_timezone="America/Toronto",
            country="Canada",
        )
        reverse = DiscoveredNHLGame(
            url="https://www.vividseats.com/a/production/1",
            title="Toronto Maple Leafs at New York Islanders",
            date_hint=datetime(2026, 9, 30).date(),
        )
        wrong_date = DiscoveredNHLGame(
            url="https://www.vividseats.com/b/production/2",
            title="New York Islanders at Toronto Maple Leafs",
            date_hint=datetime(2026, 10, 1).date(),
        )
        correct = DiscoveredNHLGame(
            url="https://www.vividseats.com/c/production/3",
            title="New York Islanders at Toronto Maple Leafs",
            date_hint=datetime(2026, 9, 30).date(),
        )

        candidates = candidates_for_schedule_game(
            game,
            [reverse, wrong_date, correct],
        )
        self.assertEqual([row.url for row in candidates], [correct.url])

    def test_game_type_labels(self):
        self.assertEqual(game_type_label(1), "Preseason")
        self.assertEqual(game_type_label(2), "Regular season")
        self.assertEqual(game_type_label(3), "Playoffs")


if __name__ == "__main__":
    unittest.main()
