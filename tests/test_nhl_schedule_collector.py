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
                            now + timedelta(hours=500),
                            "DAL",
                            "STL",
                        ),
                        self._game(
                            5,
                            now + timedelta(hours=721),
                            "COL",
                            "MIN",
                        ),
                    ],
                }
            ]
        }

        games = parse_schedule_payload(payload, now)
        self.assertEqual(
            [game.schedule_id for game in games],
            ["1", "2", "4"],
        )
        self.assertEqual(games[0].country, "Canada")
        self.assertEqual(games[1].country, "USA")
        self.assertTrue(venue_timezone_is_supported("America/Toronto"))
        self.assertFalse(venue_timezone_is_supported("Europe/Helsinki"))

    def test_official_schedule_pagination_covers_the_full_month(self):
        now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
        calls = []
        page_starts = [
            "2026-09-20",
            "2026-09-27",
            "2026-10-04",
            "2026-10-11",
            "2026-10-18",
        ]
        lead_hours = [24, 180, 348, 516, 684]

        def fetcher(url, timeout):
            calls.append(url)
            current = url.rsplit("/", 1)[-1]
            index = page_starts.index(current)
            next_start = (
                page_starts[index + 1]
                if index + 1 < len(page_starts)
                else "2026-10-25"
            )
            return {
                "nextStartDate": next_start,
                "gameWeek": [
                    {
                        "date": current,
                        "games": [
                            self._game(
                                index + 1,
                                now + timedelta(hours=lead_hours[index]),
                                "BOS" if index % 2 == 0 else "NYR",
                                "TOR" if index % 2 == 0 else "BOS",
                                venue_timezone=(
                                    "America/Toronto"
                                    if index % 2 == 0
                                    else "America/New_York"
                                ),
                            )
                        ],
                    }
                ],
            }

        games, sources = fetch_schedule_games(
            now,
            horizon_hours=720,
            fetcher=fetcher,
        )
        self.assertEqual(
            [game.schedule_id for game in games],
            ["1", "2", "3", "4", "5"],
        )
        self.assertEqual(len(sources), 5)
        self.assertEqual(len(calls), 5)

    def test_due_filter_and_summary_apply_agreed_cadence(self):
        slot = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
        tier_specs = (
            ("hourly", 48, 1),
            ("six-hour", 120, 12),
            ("twelve-hour", 240, 12),
            ("daily", 500, 24),
        )
        games = []
        for prefix, lead, count in tier_specs:
            for index in range(count):
                games.append(
                    ScheduledNHLGame(
                        schedule_id=f"{prefix}-{index}",
                        event_date=slot + timedelta(hours=lead),
                        away_team="New York Rangers",
                        home_team="Boston Bruins",
                        venue="TD Garden",
                        name="New York Rangers at Boston Bruins",
                        venue_timezone="America/New_York",
                        country="USA",
                    )
                )

        due = schedule_games_due(games, slot)
        self.assertIn("hourly-0", {game.schedule_id for game in due})
        self.assertLess(len(due), len(games))
        summary = schedule_cadence_summary(games, slot)
        self.assertEqual(
            summary["in_window"],
            {"1h": 1, "6h": 12, "12h": 12, "24h": 24},
        )
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
