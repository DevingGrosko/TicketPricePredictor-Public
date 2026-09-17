from datetime import datetime, timezone
from pathlib import Path
import unittest

from nfl_schedule_collector import fetch_schedule_games


class NFLScheduleSourceResilienceTests(unittest.TestCase):
    def test_season_scoreboard_is_primary_source(self):
        calls = []

        def fetcher(url, timeout):
            calls.append((url, timeout))
            return {"events": []}

        games, source = fetch_schedule_games(
            datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
            fetcher=fetcher,
        )

        self.assertEqual(games, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(source, calls[0][0])
        self.assertIn("dates=2026", source)
        self.assertIn("limit=1000", source)
        self.assertNotIn("20260917-", source)

    def test_january_uses_previous_nfl_season_year(self):
        calls = []

        def fetcher(url, timeout):
            calls.append(url)
            return {"events": []}

        fetch_schedule_games(
            datetime(2027, 1, 10, 12, tzinfo=timezone.utc),
            fetcher=fetcher,
        )

        self.assertEqual(len(calls), 1)
        self.assertIn("dates=2026", calls[0])

    def test_range_query_remains_secondary_fallback(self):
        calls = []

        def fetcher(url, timeout):
            calls.append(url)
            if len(calls) == 1:
                raise RuntimeError("season endpoint unavailable")
            return {"events": []}

        games, source = fetch_schedule_games(
            datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
            fetcher=fetcher,
        )

        self.assertEqual(games, [])
        self.assertEqual(len(calls), 2)
        self.assertEqual(source, calls[1])
        self.assertIn("dates=20260917-20261018", source)


class TicketCollectionWorkflowCadenceTests(unittest.TestCase):
    def test_github_recovery_schedule_explicitly_skips_hourly_leagues(self):
        workflow = Path(".github/workflows/collect-ticket-prices.yml").read_text(
            encoding="utf-8"
        )

        self.assertEqual(workflow.count('if [[ "$EVENT_NAME" == "schedule" ]]'), 2)
        self.assertIn(
            "Skipping NFL on the best-effort GitHub baseball recovery schedule.",
            workflow,
        )
        self.assertIn(
            "Skipping NHL on the best-effort GitHub baseball recovery schedule.",
            workflow,
        )


if __name__ == "__main__":
    unittest.main()
