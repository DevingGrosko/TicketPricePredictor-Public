from datetime import datetime, timedelta, timezone
import unittest
from zoneinfo import ZoneInfo

from nfl_collector import (
    NFLSnapshotParser,
    date_hint_from_text,
    date_hint_from_url,
    extract_nfl_game_rows,
    hourly_capture_slot,
    is_nfl_game_title,
    nfl_is_within_capture_window,
    nfl_snapshot_to_payload,
    upcoming_nfl_games,
)


class NFLDiscoveryTests(unittest.TestCase):
    def test_extracts_games_and_excludes_non_game_products(self):
        now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        page = """
        <a href="/dallas-cowboys-new-york-giants-9-13-2026/production/1234567">
          Dallas Cowboys at New York Giants — Sun Sep 13, 2026
        </a>
        <a href="/parking-cowboys-giants/production/2234567">
          Parking: Dallas Cowboys at New York Giants — Sep 13, 2026
        </a>
        <a href="/new-york-giants-season-tickets/production/3234567">
          New York Giants Season Tickets
        </a>
        <a href="/ravens-steelers-9-14-2026/production/4234567"
           aria-label="Baltimore Ravens at Pittsburgh Steelers Sep 14, 2026">
          Tickets
        </a>
        <a href="/concert/production/5234567">Taylor Swift Sep 14, 2026</a>
        """
        rows = extract_nfl_game_rows(page, now=now)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].date_hint.isoformat(), "2026-09-13")
        self.assertEqual(rows[1].date_hint.isoformat(), "2026-09-14")
        self.assertTrue(all("production/" in row.url for row in rows))

    def test_url_date_wins_over_stale_or_misleading_anchor_text(self):
        now = datetime(2026, 8, 29, 12, tzinfo=timezone.utc)
        page = """
        <a href="/dallas-cowboys-new-york-giants-9-13-2026--sports-nfl-football/production/1234567">
          Dallas Cowboys at New York Giants — Aug 18, 2026
        </a>
        """
        rows = extract_nfl_game_rows(page, now=now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].date_hint.isoformat(), "2026-09-13")

    def test_infers_year_across_new_year(self):
        now = datetime(2026, 12, 20, 12, tzinfo=timezone.utc)
        self.assertEqual(
            date_hint_from_text("Buffalo Bills at New York Jets Jan 3", now).isoformat(),
            "2027-01-03",
        )

    def test_reads_date_from_vivid_nfl_url(self):
        url = (
            "https://www.vividseats.com/dallas-cowboys-tickets-new-york-"
            "9-13-2026--sports-nfl-football/production/1234567"
        )
        self.assertEqual(date_hint_from_url(url).isoformat(), "2026-09-13")

    def test_filters_to_rolling_calendar_window(self):
        now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        page = """
        <a href="/a/production/1000001">Dallas Cowboys at New York Giants Sep 6, 2026</a>
        <a href="/b/production/1000002">Baltimore Ravens at Pittsburgh Steelers Sep 9, 2026</a>
        """
        rows = extract_nfl_game_rows(page, now=now)
        eligible = upcoming_nfl_games(rows, now, horizon_days=7)
        self.assertEqual([row.url.rsplit("/", 1)[-1] for row in eligible], ["1000001"])

    def test_title_validation_requires_real_matchup(self):
        self.assertTrue(is_nfl_game_title("Dallas Cowboys at New York Giants"))
        self.assertTrue(is_nfl_game_title("Baltimore Ravens vs Pittsburgh Steelers"))
        self.assertFalse(is_nfl_game_title("New York Giants Season Tickets"))
        self.assertFalse(is_nfl_game_title("Parking: Cowboys at Giants"))
        self.assertFalse(
            is_nfl_game_title("Parking: Dallas Cowboys at New York Giants")
        )


class NFLCadenceTests(unittest.TestCase):
    def test_window_is_exactly_seven_days(self):
        now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        self.assertTrue(nfl_is_within_capture_window(now + timedelta(hours=168), now))
        self.assertFalse(
            nfl_is_within_capture_window(
                now + timedelta(hours=168, seconds=1),
                now,
            )
        )
        self.assertFalse(nfl_is_within_capture_window(now, now))

    def test_every_run_in_an_hour_maps_to_one_capture_slot(self):
        first = datetime(2026, 9, 1, 12, 1, 4, tzinfo=timezone.utc)
        second = datetime(2026, 9, 1, 12, 58, 59, tzinfo=timezone.utc)
        expected = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        self.assertEqual(hourly_capture_slot(first), expected)
        self.assertEqual(hourly_capture_slot(second), expected)


class NFLSnapshotParserTests(unittest.TestCase):
    def test_uses_lowest_displayed_price_per_section(self):
        tickets = []
        for index in range(10):
            tickets.extend(
                [
                    {"l": f"Section {100 + index}", "p": 150 + index, "q": "2"},
                    {"l": f"Section {100 + index}", "p": 100 + index, "q": "4"},
                ]
            )
        payload = {
            "global": [
                {
                    "productionName": "Dallas Cowboys at New York Giants",
                    "mapTitle": "MetLife Stadium",
                    "productionId": "1234567",
                }
            ],
            "tickets": tickets,
        }
        snapshot = NFLSnapshotParser.parse(payload)
        self.assertEqual(len(snapshot.sections), 10)
        self.assertEqual(snapshot.sections[0].price, 100)
        self.assertEqual(snapshot.sections[0].listing_count, 2)

    def test_payload_is_explicitly_typed(self):
        payload = {
            "global": [
                {
                    "productionName": "Dallas Cowboys at New York Giants",
                    "mapTitle": "MetLife Stadium",
                    "productionId": "1234567",
                }
            ],
            "tickets": [
                {"l": f"Section {index}", "p": 50 + index}
                for index in range(10)
            ],
        }
        snapshot = NFLSnapshotParser.parse(payload)
        result = nfl_snapshot_to_payload(
            "https://www.vividseats.com/game/production/1234567",
            datetime(2026, 9, 13, 13, tzinfo=ZoneInfo("America/New_York")),
            datetime(2026, 9, 6, 17, tzinfo=timezone.utc),
            snapshot,
        )
        self.assertEqual(result["event_type"], "nfl")


if __name__ == "__main__":
    unittest.main()
