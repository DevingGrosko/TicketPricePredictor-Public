import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nhl_schedule_collector import run_schedule_collector


class NHLEmptyScheduleTests(unittest.TestCase):
    def test_empty_schedule_is_a_healthy_collection_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            health = root / "nhl-health.json"
            pending = root / "pending"

            with patch(
                "nhl_schedule_collector.replay_pending_snapshots",
                return_value=(0, True, []),
            ), patch(
                "nhl_schedule_collector.fetch_schedule_games",
                return_value=([], ["official-nhl-schedule"]),
            ):
                code = run_schedule_collector(
                    "https://example.test/api/nhl/snapshot",
                    "test-token",
                    True,
                    1,
                    health,
                    pending,
                )

            self.assertEqual(code, 0)
            report = json.loads(health.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "healthy")
            self.assertEqual(report["scheduled_in_window"], 0)
            self.assertEqual(report["scheduled_due"], 0)
            self.assertEqual(report["capture_window_hours"], 720)
            self.assertEqual(
                report["cadence_policy"],
                {
                    "days_15_to_30_hours": 24,
                    "days_8_to_14_hours": 12,
                    "days_4_to_7_hours": 6,
                    "final_72_hours": 1,
                    "staggering": "deterministic per NHL game ID",
                },
            )
            self.assertEqual(
                report["cadence_tiers"]["in_window"],
                {"1h": 0, "6h": 0, "12h": 0, "24h": 0},
            )


if __name__ == "__main__":
    unittest.main()
