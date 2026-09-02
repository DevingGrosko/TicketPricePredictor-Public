from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nhl_collector import (
    DiscoveredNHLGame,
    NHLInventoryIncompleteError,
    NHLSnapshotParser,
)
from nhl_schedule_collector import (
    NHLProviderGapError,
    ScheduleResolution,
    ScheduledNHLGame,
    _capture_resolution,
    candidates_for_schedule_game,
    nhl_collection_should_fail,
    nhl_should_skip_for_trigger,
    run_schedule_collector,
    validate_captured_match,
)


class NHLResilienceTests(unittest.TestCase):
    def _scheduled_game(self):
        return ScheduledNHLGame(
            schedule_id="2026010001",
            event_date=datetime(2026, 9, 12, 23, tzinfo=timezone.utc),
            away_team="Toronto Maple Leafs",
            home_team="Ottawa Senators",
            venue="Canadian Tire Centre",
            name="Toronto Maple Leafs at Ottawa Senators",
            venue_timezone="America/Toronto",
            country="Canada",
            game_type=1,
            season=20262027,
        )

    def _thin_payload(self):
        return {
            "global": [
                {
                    "productionName": "Toronto Maple Leafs at Ottawa Senators",
                    "mapTitle": "Centre Slush Puppie",
                    "productionId": "7227372",
                    "currencyCode": "USD",
                }
            ],
            "tickets": [
                {
                    "l": "General Admission",
                    "p": "45",
                    "r": "GA",
                    "q": "2",
                    "tags": [],
                }
            ],
        }

    def test_thin_inventory_has_a_specific_provider_gap_exception(self):
        with self.assertRaises(NHLInventoryIncompleteError):
            NHLSnapshotParser.parse(self._thin_payload())

    def test_explicitly_wrong_dates_are_never_used_as_fallbacks(self):
        game = self._scheduled_game()
        wrong_date = DiscoveredNHLGame(
            url="https://www.vividseats.com/wrong/production/1",
            title=game.name,
            date_hint=datetime(2026, 10, 28).date(),
        )
        undated = DiscoveredNHLGame(
            url="https://www.vividseats.com/undated/production/2",
            title=game.name,
            date_hint=None,
        )
        self.assertEqual(candidates_for_schedule_game(game, [wrong_date]), ())
        self.assertEqual(
            candidates_for_schedule_game(game, [wrong_date, undated]),
            (undated,),
        )

    def test_date_only_provider_metadata_uses_official_puck_drop(self):
        game = self._scheduled_game()
        provider_midnight = datetime(2026, 9, 12, 4, tzinfo=timezone.utc)
        self.assertEqual(
            validate_captured_match(game, provider_midnight, game.name),
            game.event_date,
        )
        with self.assertRaisesRegex(ValueError, "does not match"):
            validate_captured_match(
                game,
                datetime(2026, 10, 28, 23, tzinfo=timezone.utc),
                game.name,
            )

    def test_capture_classifies_thin_inventory_as_provider_gap(self):
        game = self._scheduled_game()
        candidate = DiscoveredNHLGame(
            url="https://www.vividseats.com/game/production/7227372",
            title=game.name,
            date_hint=game.local_date,
        )
        resolution = ScheduleResolution(game, (candidate,), "test")
        payload = self._thin_payload()

        class FakeBrowser:
            def __init__(self, **kwargs):
                pass

            def capture(self, url):
                return (
                    payload,
                    datetime(2026, 9, 12, 4, tzinfo=timezone.utc),
                )

            def close(self):
                pass

        with patch("nhl_schedule_collector.VividNFLBrowser", FakeBrowser):
            with self.assertRaises(NHLProviderGapError):
                _capture_resolution(
                    resolution,
                    headless=True,
                    timeout=1,
                )

    def test_provider_gaps_do_not_define_an_operational_failure(self):
        self.assertFalse(nhl_collection_should_fail([], []))
        self.assertTrue(
            nhl_collection_should_fail(["browser crashed"], [])
        )
        self.assertTrue(
            nhl_collection_should_fail([], ["search failed"])
        )

    def test_scheduled_fallback_exits_before_network_or_browser_work(self):
        self.assertTrue(nhl_should_skip_for_trigger("schedule"))
        self.assertFalse(nhl_should_skip_for_trigger("workflow_dispatch"))
        with tempfile.TemporaryDirectory() as directory:
            health = Path(directory) / "nhl-health.json"
            pending = Path(directory) / "pending"
            with patch.dict(
                os.environ,
                {"GITHUB_EVENT_NAME": "schedule"},
                clear=False,
            ), patch(
                "nhl_schedule_collector.fetch_schedule_games"
            ) as fetch_schedule:
                code = run_schedule_collector(
                    "https://example.test/api/nhl/snapshot",
                    "token",
                    True,
                    1,
                    health,
                    pending,
                )
            self.assertEqual(code, 0)
            fetch_schedule.assert_not_called()
            report = json.loads(health.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
