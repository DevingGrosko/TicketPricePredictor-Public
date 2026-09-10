from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from nfl_collector import DiscoveredNFLGame
from nfl_schedule_collector import (
    ScheduleResolution,
    ScheduledNFLGame,
    _capture_resolution,
)


class NFLScheduleCaptureResilienceTests(unittest.TestCase):
    def setUp(self):
        self.game = ScheduledNFLGame(
            schedule_id="seattle-live",
            event_date=datetime(2026, 9, 10, 0, 20, tzinfo=timezone.utc),
            away_team="New England Patriots",
            home_team="Seattle Seahawks",
            venue="Lumen Field",
            name="New England Patriots at Seattle Seahawks",
        )
        self.candidate = DiscoveredNFLGame(
            url="https://www.vividseats.com/seattle-seahawks-tickets/production/6493039",
            title="New England Patriots at Seattle Seahawks",
            date_hint=datetime(2026, 9, 9).date(),
        )
        self.resolution = ScheduleResolution(
            game=self.game,
            candidates=(self.candidate,),
            source="vivid-nfl-feed",
        )
        self.snapshot = SimpleNamespace(title="New England Patriots at Seattle Seahawks")

    def test_schedule_kickoff_is_authoritative_after_provider_time_validation(self):
        # This mirrors the production Seattle failure: Vivid rendered 5:20 PM
        # without a reliable timezone, which the generic parser could interpret
        # as Eastern (21:20 UTC) even though kickoff is 00:20 UTC.
        provider_event_date = self.game.event_date - timedelta(hours=3)
        browser = Mock()
        browser.capture.return_value = ({}, provider_event_date)

        with patch(
            "nfl_schedule_collector.VividNFLBrowser",
            return_value=browser,
        ), patch(
            "nfl_schedule_collector.NFLSnapshotParser.parse",
            return_value=self.snapshot,
        ):
            url, event_date, snapshot = _capture_resolution(
                self.resolution,
                headless=True,
                timeout=45,
            )

        self.assertEqual(url, self.candidate.url)
        self.assertEqual(event_date, self.game.event_date)
        self.assertIs(snapshot, self.snapshot)
        browser.close.assert_called_once_with()

    def test_timeout_retries_same_candidate_once_with_fresh_browser(self):
        provider_event_date = self.game.event_date - timedelta(hours=3)
        first_browser = Mock()
        first_browser.capture.side_effect = TimeoutError(
            "No Vivid listings response appeared within 45 seconds."
        )
        second_browser = Mock()
        second_browser.capture.return_value = ({}, provider_event_date)

        with patch(
            "nfl_schedule_collector.VividNFLBrowser",
            side_effect=[first_browser, second_browser],
        ) as browser_class, patch(
            "nfl_schedule_collector.NFLSnapshotParser.parse",
            return_value=self.snapshot,
        ):
            _url, event_date, snapshot = _capture_resolution(
                self.resolution,
                headless=True,
                timeout=45,
            )

        self.assertEqual(browser_class.call_count, 2)
        self.assertEqual(event_date, self.game.event_date)
        self.assertIs(snapshot, self.snapshot)
        first_browser.close.assert_called_once_with()
        second_browser.close.assert_called_once_with()

    def test_validation_error_does_not_retry_same_candidate(self):
        browser = Mock()
        browser.capture.return_value = ({}, self.game.event_date)
        wrong_snapshot = SimpleNamespace(title="Dallas Cowboys at New York Giants")

        with patch(
            "nfl_schedule_collector.VividNFLBrowser",
            return_value=browser,
        ) as browser_class, patch(
            "nfl_schedule_collector.NFLSnapshotParser.parse",
            return_value=wrong_snapshot,
        ):
            with self.assertRaisesRegex(RuntimeError, "away/home order"):
                _capture_resolution(
                    self.resolution,
                    headless=True,
                    timeout=45,
                )

        self.assertEqual(browser_class.call_count, 1)
        browser.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
