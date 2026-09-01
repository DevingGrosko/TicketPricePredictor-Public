from datetime import datetime, timedelta, timezone
import unittest

from nhl_collector import (
    NHLSnapshotParser,
    extract_nhl_game_rows,
    is_nhl_game_title,
    nhl_capture_interval_hours,
    nhl_capture_is_due,
    nhl_capture_tier,
    nhl_snapshot_to_payload,
    ordered_matchup_from_title,
)


class NHLCollectorTests(unittest.TestCase):
    def _payload(self):
        return {
            "global": [
                {
                    "productionName": "Boston Bruins at Toronto Maple Leafs",
                    "mapTitle": "Scotiabank Arena",
                    "productionId": "7302542",
                    "currencyCode": "USD",
                }
            ],
            "tickets": [
                {
                    "l": f"Section {100 + index}",
                    "p": str(80 + index),
                    "aip": str(105 + index),
                    "r": "A",
                    "q": "2",
                    "tags": [],
                }
                for index in range(8)
            ]
            + [
                {
                    "l": "Section 100",
                    "p": "75",
                    "aip": "99",
                    "r": "B",
                    "q": "2",
                    "tags": [],
                }
            ],
        }

    def test_title_validation_requires_exactly_two_teams_and_a_matchup(self):
        title = "Boston Bruins at Toronto Maple Leafs"
        self.assertTrue(is_nhl_game_title(title))
        self.assertEqual(
            ordered_matchup_from_title(title),
            ("Boston Bruins", "Toronto Maple Leafs"),
        )
        self.assertFalse(
            is_nhl_game_title("Toronto Maple Leafs Parking")
        )
        self.assertFalse(is_nhl_game_title("Toronto Maple Leafs Tickets"))

    def test_parser_keeps_lowest_price_per_section_and_currency(self):
        snapshot = NHLSnapshotParser.parse(self._payload())
        self.assertEqual(snapshot.source_id, "7302542")
        self.assertEqual(snapshot.venue, "Scotiabank Arena")
        self.assertEqual(snapshot.currency, "USD")
        by_section = {row.section: row for row in snapshot.sections}
        self.assertEqual(by_section["Section 100"].price, 75)
        self.assertEqual(by_section["Section 100"].listing_count, 2)
        self.assertEqual(len(snapshot.sections), 8)

    def test_feed_parser_rejects_parking_and_accepts_game_links(self):
        page = """
        <a href="/nhl-hockey/toronto-maple-leafs-tickets/production/7302542">
          Boston Bruins at Toronto Maple Leafs Sep 29, 2026
        </a>
        <a href="/parking/production/7302543">
          Boston Bruins at Toronto Maple Leafs Parking
        </a>
        """
        rows = extract_nhl_game_rows(
            page,
            "https://www.vividseats.com/nhl-hockey/",
            datetime(2026, 8, 31, tzinfo=timezone.utc),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].date_hint.isoformat(), "2026-09-29")

    def test_cadence_tiers_cover_the_full_thirty_day_window(self):
        slot = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        expected = {
            720: (24, "days_15_to_30_daily"),
            336: (12, "days_8_to_14_every_12_hours"),
            168: (6, "days_4_to_7_every_6_hours"),
            72: (1, "final_72_hours_hourly"),
        }
        for lead_hours, (interval, tier) in expected.items():
            event_date = slot + timedelta(hours=lead_hours)
            with self.subTest(lead_hours=lead_hours):
                self.assertEqual(
                    nhl_capture_interval_hours(event_date, slot),
                    interval,
                )
                self.assertEqual(nhl_capture_tier(event_date, slot), tier)

        self.assertTrue(
            nhl_capture_is_due(
                slot + timedelta(hours=48),
                slot,
                "hourly-game",
            )
        )
        self.assertIsNone(
            nhl_capture_interval_hours(slot + timedelta(hours=721), slot)
        )

    def test_payload_marks_nhl_currency_and_schedule(self):
        snapshot = NHLSnapshotParser.parse(self._payload())
        payload = nhl_snapshot_to_payload(
            "https://www.vividseats.com/game/production/7302542",
            datetime(2026, 9, 29, 23, tzinfo=timezone.utc),
            datetime(2026, 9, 28, 23, tzinfo=timezone.utc),
            snapshot,
            schedule={
                "schedule_id": "2026020001",
                "away_team": "Boston Bruins",
                "home_team": "Toronto Maple Leafs",
            },
        )
        self.assertEqual(payload["event_type"], "nhl")
        self.assertEqual(payload["currency"], "USD")
        self.assertEqual(payload["schedule"]["schedule_id"], "2026020001")


if __name__ == "__main__":
    unittest.main()
