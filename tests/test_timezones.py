import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from Flask_App.flask_app import format_event_title
from models import (
    captured_datetime_for_storage,
    event_datetime_for_storage,
    hours_before_event,
)


class TimezoneConventionTests(unittest.TestCase):
    def test_hours_before_event_handles_eastern_event_and_utc_capture(self):
        event_date = datetime(2026, 7, 28, 18, 45)
        captured_at = datetime(2026, 7, 28, 17, 38)

        self.assertAlmostEqual(
            hours_before_event(event_date, captured_at),
            5 + 7 / 60,
        )

    def test_storage_convention_is_explicit_for_sqlite(self):
        event_date = datetime(
            2026,
            7,
            28,
            18,
            45,
            tzinfo=ZoneInfo("America/New_York"),
        )
        captured_at = datetime(2026, 7, 28, 17, 38, tzinfo=timezone.utc)

        self.assertEqual(
            event_datetime_for_storage(event_date),
            datetime(2026, 7, 28, 18, 45),
        )
        self.assertEqual(
            captured_datetime_for_storage(captured_at),
            datetime(2026, 7, 28, 17, 38),
        )

    def test_event_label_converts_aware_utc_to_eastern(self):
        event = SimpleNamespace(
            title="Toronto Blue Jays at Washington Nationals",
            event_date=datetime(2026, 7, 28, 22, 45, tzinfo=timezone.utc),
        )

        self.assertEqual(
            format_event_title(event),
            "Toronto Blue Jays at Washington Nationals — Jul 28, 2026 · 6:45 PM",
        )


if __name__ == "__main__":
    unittest.main()
