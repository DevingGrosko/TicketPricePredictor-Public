import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from collector import (
    SnapshotUploadError,
    post_snapshot_with_retry,
    queue_snapshot,
    replay_pending_snapshots,
)


def valid_payload() -> dict:
    sections = [
        {
            "section": f"Section {index}",
            "price": 40 + index,
            "listing_count": 1,
            "row": "A",
            "quantity": "2",
            "displayed_price": str(40 + index),
            "alternate_price": str(50 + index),
            "price_source": "p",
        }
        for index in range(10)
    ]
    return {
        "schema_version": 1,
        "captured_at": "2026-07-28T14:30:00+00:00",
        "event_date": "2026-07-29T19:05:00-04:00",
        "source_url": (
            "https://www.vividseats.com/team-tickets-venue-"
            "7-29-2026--sports-mlb-baseball/production/5967878"
        ),
        "source_id": "5967878",
        "title": "Toronto Blue Jays at Washington Nationals",
        "venue": "Nationals Park",
        "section_count": len(sections),
        "sections": sections,
    }


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b'{"status":"stored"}'


class DeliveryQueueTests(unittest.TestCase):
    def test_retry_recovers_from_temporary_transport_failure(self):
        attempts = []
        delays = []

        def opener(_request, timeout):
            attempts.append(timeout)
            if len(attempts) < 3:
                raise urllib.error.URLError("planned maintenance")
            return FakeResponse()

        result = post_snapshot_with_retry(
            "https://example.com/ingest",
            "token",
            valid_payload(),
            retry_delays=(2, 5),
            sleep=delays.append,
            opener=opener,
        )

        self.assertEqual(result["status"], "stored")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(delays, [2, 5])

    def test_successful_replay_removes_pending_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            pending = Path(temporary)
            path = queue_snapshot(valid_payload(), pending)

            with patch(
                "collector.post_snapshot_with_retry",
                return_value={"status": "stored"},
            ):
                replayed, available, errors = replay_pending_snapshots(
                    "https://example.com/ingest",
                    "token",
                    pending,
                )

        self.assertEqual(replayed, 1)
        self.assertTrue(available)
        self.assertEqual(errors, [])
        self.assertFalse(path.exists())

    def test_failed_replay_keeps_pending_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            pending = Path(temporary)
            path = queue_snapshot(valid_payload(), pending)
            original = path.read_text(encoding="utf-8")

            def unavailable(*_args, **_kwargs):
                raise SnapshotUploadError("planned maintenance", retryable=True)

            with patch("collector.post_snapshot_with_retry", side_effect=unavailable):
                replayed, available, errors = replay_pending_snapshots(
                    "https://example.com/ingest",
                    "token",
                    pending,
                )

            retained = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(replayed, 0)
        self.assertFalse(available)
        self.assertEqual(len(errors), 1)
        self.assertEqual(json.loads(original), retained)


if __name__ == "__main__":
    unittest.main()
