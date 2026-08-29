from datetime import datetime, timezone
import unittest

from concert_collector import (
    CONCERT_VENUE_FEEDS,
    ConcertSnapshotParser,
    concert_cycle_exit_code,
    event_date_from_concert_url,
    extract_concert_event_urls,
    upcoming_concerts,
)


class ConcertDiscoveryTests(unittest.TestCase):
    def test_extracts_concert_links_and_ignores_sports_and_theater(self):
        page = '''
        <a href="/artist-tickets-new-york-madison-square-garden-9-1-2026--concerts-pop/production/123?qty=2">Concert</a>
        <a href="/team-tickets-new-york-9-1-2026--sports-nba-basketball/production/456">Game</a>
        <a href="/comic-tickets-new-york-9-1-2026--theater-comedy/production/789">Comedy</a>
        '''

        self.assertEqual(
            extract_concert_event_urls(page),
            {
                "https://www.vividseats.com/artist-tickets-new-york-"
                "madison-square-garden-9-1-2026--concerts-pop/production/123"
            },
        )

    def test_reads_date_from_concert_url(self):
        url = (
            "https://www.vividseats.com/artist-tickets-new-york-"
            "9-14-2026--concerts-rock/production/1234567"
        )

        self.assertEqual(
            event_date_from_concert_url(url).date().isoformat(), "2026-09-14"
        )
        self.assertIsNone(
            event_date_from_concert_url(
                "https://www.vividseats.com/team-9-14-2026--sports-nfl/production/9"
            )
        )

    def test_rolling_window_includes_early_september_progressively(self):
        now = datetime(2026, 8, 30, 4, tzinfo=timezone.utc)
        urls = {
            "https://www.vividseats.com/a-8-31-2026--concerts-pop/production/1",
            "https://www.vividseats.com/a-9-1-2026--concerts-pop/production/2",
            "https://www.vividseats.com/a-9-2-2026--concerts-pop/production/3",
            "https://www.vividseats.com/a-9-5-2026--concerts-pop/production/4",
        }

        self.assertEqual(
            upcoming_concerts(urls, now, horizon_days=3),
            [
                "https://www.vividseats.com/a-8-31-2026--concerts-pop/production/1",
                "https://www.vividseats.com/a-9-1-2026--concerts-pop/production/2",
                "https://www.vividseats.com/a-9-2-2026--concerts-pop/production/3",
            ],
        )

    def test_verified_arena_feeds_are_configured(self):
        self.assertEqual(
            CONCERT_VENUE_FEEDS["Madison Square Garden"],
            "https://www.vividseats.com/madison-square-garden-tickets/venue/973",
        )
        self.assertIn("Barclays Center", CONCERT_VENUE_FEEDS)
        self.assertIn("Capital One Arena", CONCERT_VENUE_FEEDS)
        self.assertIn("TD Garden", CONCERT_VENUE_FEEDS)


class ConcertSnapshotParserTests(unittest.TestCase):
    def test_accepts_named_sections_and_uses_lowest_displayed_price(self):
        section_names = [
            "GA",
            "Pit",
            "Floor A",
            "Floor B",
            "Lower Bowl",
            "Club Level",
            "Upper Bowl",
            "Lawn",
            "Standing Room",
            "Suite Level",
        ]
        tickets = [
            {
                "l": section,
                "p": "90.00",
                "tags": ["STANDING_ROOM_ONLY"]
                if section == "Standing Room"
                else [],
            }
            for section in section_names
        ]
        tickets.extend(
            [
                {"l": "GA", "r": "2", "q": "4", "p": "71.49", "aip": "95.00"},
                {"l": "Obstructed", "p": "10.00", "tags": ["OBSTRUCTED_VIEW"]},
            ]
        )
        payload = {
            "global": [
                {
                    "productionName": "Test Artist: World Tour",
                    "mapTitle": "Test Arena",
                    "productionId": "1234567",
                }
            ],
            "tickets": tickets,
        }

        snapshot = ConcertSnapshotParser.parse(payload)
        by_section = {row.section: row for row in snapshot.sections}

        self.assertEqual(snapshot.title, "Test Artist: World Tour")
        self.assertEqual(snapshot.venue, "Test Arena")
        self.assertIn("Standing Room", by_section)
        self.assertNotIn("Obstructed", by_section)
        self.assertEqual(by_section["GA"].price, 71)
        self.assertEqual(by_section["GA"].listing_count, 2)
        self.assertEqual(by_section["GA"].row, "2")
        self.assertEqual(by_section["GA"].quantity, "4")
        self.assertEqual(by_section["GA"].price_source, "p")

    def test_rejects_too_few_usable_sections(self):
        payload = {
            "global": [
                {
                    "productionName": "Small show",
                    "mapTitle": "Small Room",
                    "productionId": "123",
                }
            ],
            "tickets": [
                {"l": f"Section {index}", "p": "50"} for index in range(9)
            ],
        }

        with self.assertRaisesRegex(ValueError, "only 9 usable sections"):
            ConcertSnapshotParser.parse(payload)


class ConcertCycleTests(unittest.TestCase):
    def test_partial_capture_success_keeps_cycle_green(self):
        self.assertEqual(
            concert_cycle_exit_code(
                due=4, captured=3, failures=1, discovery_failures=0
            ),
            0,
        )

    def test_every_due_capture_failing_marks_cycle_failed(self):
        self.assertEqual(
            concert_cycle_exit_code(
                due=3, captured=0, failures=3, discovery_failures=0
            ),
            1,
        )

    def test_all_venue_discovery_failing_marks_cycle_failed(self):
        self.assertEqual(
            concert_cycle_exit_code(
                due=0,
                captured=0,
                failures=0,
                discovery_failures=len(CONCERT_VENUE_FEEDS),
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
