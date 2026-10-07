from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock
from datetime import timedelta
from vivid_inventory import CurrentInventoryRecovery, VividCaptureError

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
    def test_current_404_uses_one_same_browser_reload_before_parsing(self):
        game = self._scheduled_game()
        candidate = DiscoveredNHLGame("https://www.vividseats.com/game/production/7227372",
                                      game.name, game.local_date)
        raw = self._thin_payload()
        raw["tickets"] = [{"l": f"Section {100 + index}", "p": "45", "q": "2"} for index in range(12)]
        browser = Mock()
        diagnostics = {"production_id": "7227372", "document_status": 200,
                       "responses": [{"path": "/hermes/api/v1/listings", "status": 404}]}
        browser.capture_diagnostics = diagnostics
        browser.capture.side_effect = [VividCaptureError("provider-inventory-not-found", diagnostics), (raw, game.event_date)]
        sleep = Mock()
        with patch("nhl_schedule_collector.CurrentInventoryRecovery", side_effect=lambda date, tier:
                   CurrentInventoryRecovery(date, tier, now=lambda: game.event_date - timedelta(days=2), sleep=sleep)), \
             patch("nhl_schedule_collector.VividNFLBrowser", return_value=browser) as factory:
            _url, _date, snapshot = _capture_resolution(
                ScheduleResolution(game, (candidate,), "test"), headless=False, timeout=45)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(browser.capture.call_count, 2)
        self.assertEqual(browser.capture.call_args.kwargs, {"reload_page": True})
        sleep.assert_called_once_with(15)
        browser.close.assert_called_once_with()
        self.assertTrue(snapshot.capture_diagnostics["inventory_recovery"]["recovered"])
        self.assertEqual(len(snapshot.capture_diagnostics["inventory_recovery"]["attempts"]), 2)

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

    def test_schedule_capture_preserves_inventory_and_map_diagnostics(self):
        game = self._scheduled_game()
        candidate = DiscoveredNHLGame(
            url="https://www.vividseats.com/game/production/7227372",
            title=game.name,
            date_hint=game.local_date,
        )
        payload = self._thin_payload()
        payload["tickets"] = [
            {"l": f"Section {100 + index}", "p": "45", "q": "2", "r": "A"}
            for index in range(12)
        ]
        payload["_map_geometry_diagnostics"] = {"status": "partial", "mapped_sections": 3}
        diagnostics = {"listing_responses": [{"status": 200}], "inventory_view": "unfiltered"}

        class FakeBrowser:
            capture_diagnostics = diagnostics

            def __init__(self, **kwargs):
                pass

            def capture(self, url):
                return payload, game.event_date

            def close(self):
                pass

        with patch("nhl_schedule_collector.VividNFLBrowser", FakeBrowser):
            _, _, snapshot = _capture_resolution(
                ScheduleResolution(game, (candidate,), "test"), headless=False, timeout=1,
            )
        self.assertEqual(snapshot.inventory_listing_count, 12)
        self.assertEqual(snapshot.capture_diagnostics, diagnostics)
        self.assertIsNot(snapshot.capture_diagnostics, diagnostics)
        self.assertEqual(snapshot.map_geometry_diagnostics, payload["_map_geometry_diagnostics"])

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
