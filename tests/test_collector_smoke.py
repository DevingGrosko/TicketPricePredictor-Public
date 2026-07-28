import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from collector import EventSnapshot, SectionSnapshot, run_smoke_capture, smoke_candidates


class FakeBrowser:
    closed = False

    def __init__(self, headless: bool, timeout: int):
        self.headless = headless
        self.timeout = timeout

    def capture(self, url: str):
        return {"tickets": [], "global": []}, datetime(2026, 7, 25, tzinfo=timezone.utc)

    def close(self):
        type(self).closed = True


class SmokeCaptureTests(unittest.TestCase):
    @patch("collector.SnapshotParser.parse")
    @patch("collector.VividBrowser", FakeBrowser)
    def test_smoke_capture_writes_result_without_database(self, parse):
        parse.return_value = EventSnapshot(
            source_id="5967811",
            title="Test Team at Washington Nationals",
            venue="Nationals Park",
            sections=(
                SectionSnapshot("Section 101", 42, 3, "A", "2", "42", "55"),
                SectionSnapshot("Section 102", 67, 2, "B", "2", "67", "80"),
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result.json"
            result = run_smoke_capture(
                "https://example.com/event", headless=True, timeout=45, output=output
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["section_count"], 2)
        self.assertEqual(payload["lowest_section_price"], 42)
        self.assertEqual(payload["highest_section_price"], 67)
        self.assertTrue(FakeBrowser.closed)

    def test_smoke_candidates_choose_nearest_upcoming_games(self):
        urls = {
            "https://www.vividseats.com/team-tickets-venue-7-27-2026--sports-mlb-baseball/production/3",
            "https://www.vividseats.com/team-tickets-venue-7-29-2026--sports-mlb-baseball/production/2",
            "https://www.vividseats.com/team-tickets-venue-7-28-2026--sports-mlb-baseball/production/1",
            "https://www.vividseats.com/team-tickets-venue-6-20-2026--sports-mlb-baseball/production/4",
        }

        candidates = smoke_candidates(
            urls, datetime(2026, 7, 27, 12, tzinfo=timezone.utc), limit=2
        )

        self.assertEqual(
            candidates,
            [
                "https://www.vividseats.com/team-tickets-venue-7-27-2026--sports-mlb-baseball/production/3",
                "https://www.vividseats.com/team-tickets-venue-7-28-2026--sports-mlb-baseball/production/1",
            ],
        )


if __name__ == "__main__":
    unittest.main()
