from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest

from Flask_App.section_canonicalization import (
    canonical_section_key,
    is_excluded_ticket_area,
)
from Flask_App.nfl_stadium_blueprint import LOW_SAMPLE_GAMES, _supported_area_count


class SectionIdentityTests(unittest.TestCase):
    def test_low_risk_aliases_share_one_identity(self):
        cases = (
            ("mlb", "Yankee Stadium", "011", "11"),
            ("mlb", "Nationals Park", "INFIELD BOX 119", "infield box 119"),
            ("mlb", "Fenway Park", "Granstand Outfield 3", "Grandstand Outfield 3"),
            ("nfl", "SoFi Stadium", "72CLUB", "72 CLUB"),
            ("nfl", "Example Stadium", "Sec. 101", "Section 0101"),
        )
        for sport, venue, left, right in cases:
            with self.subTest(venue=venue, left=left, right=right):
                self.assertEqual(
                    canonical_section_key(sport, venue, left),
                    canonical_section_key(sport, venue, right),
                )

    def test_fenway_rebrand_is_venue_scoped(self):
        self.assertEqual(
            canonical_section_key("mlb", "Fenway Park", "Pavilion Box 11"),
            canonical_section_key("mlb", "Fenway Park", "Aura Pavilion Box 11"),
        )
        self.assertNotEqual(
            canonical_section_key("mlb", "Another Park", "Pavilion Box 11"),
            canonical_section_key("mlb", "Another Park", "Aura Pavilion Box 11"),
        )

    def test_different_ticket_products_remain_separate(self):
        labels = ("Section 101", "Club 101", "Suite 101", "Upper 101")
        keys = [canonical_section_key("mlb", "Example Park", label) for label in labels]
        self.assertEqual(len(keys), len(set(keys)))

    def test_non_admission_products_are_excluded(self):
        for label in (
            "GP Atrium (No Admission)",
            "NORTHWEST SIDELINE PASS",
            "401 W Washington St - Government Center",
            "Parking Pass",
        ):
            with self.subTest(label=label):
                self.assertTrue(is_excluded_ticket_area(label))


class SupportedAreaCountTests(unittest.TestCase):
    @staticmethod
    def _row(event_id: int, section: str, price: float = 100.0):
        return SimpleNamespace(
            event_id=event_id,
            captured_at=datetime(2026, 9, event_id, 12, tzinfo=timezone.utc),
            section=section,
            price=price,
        )

    def test_count_uses_canonical_identity_and_three_game_support(self):
        events = [
            SimpleNamespace(id=1, Place="Fenway Park"),
            SimpleNamespace(id=2, Place="Fenway Park"),
            SimpleNamespace(id=3, Place="Fenway Park"),
            SimpleNamespace(id=4, Place="Fenway Park"),
        ]
        rows = [
            self._row(1, "Pavilion Box 11"),
            self._row(2, "Aura Pavilion Box 11"),
            self._row(3, "AURA PAVILION BOX 11"),
            self._row(4, "One Game Only"),
            self._row(1, "Parking Pass"),
        ]

        self.assertEqual(LOW_SAMPLE_GAMES, 3)
        self.assertEqual(_supported_area_count(events, rows, "mlb"), 1)

    def test_distinct_products_each_count_when_supported(self):
        events = [
            SimpleNamespace(id=1, Place="Example Park"),
            SimpleNamespace(id=2, Place="Example Park"),
            SimpleNamespace(id=3, Place="Example Park"),
        ]
        rows = [
            self._row(event_id, section)
            for event_id in (1, 2, 3)
            for section in ("Section 101", "Club 101")
        ]

        self.assertEqual(_supported_area_count(events, rows, "mlb"), 2)

    def test_invalid_prices_and_missing_capture_do_not_count(self):
        events = [
            SimpleNamespace(id=1, Place="Example Park"),
            SimpleNamespace(id=2, Place="Example Park"),
            SimpleNamespace(id=3, Place="Example Park"),
        ]
        rows = [
            self._row(1, "Section 101", price=100),
            self._row(2, "Section 101", price=0),
            SimpleNamespace(
                event_id=3,
                captured_at=None,
                section="Section 101",
                price=90,
            ),
        ]

        self.assertEqual(_supported_area_count(events, rows, "mlb"), 0)


class PresentationTests(unittest.TestCase):
    def test_team_cards_do_not_show_section_counts(self):
        root = Path(__file__).resolve().parents[1]
        templates = (
            "Flask_App/templates/HomeScreen.html",
            "Flask_App/templates/NFLHomeScreen.html",
            "Flask_App/templates/NHLHomeScreen.html",
            "Flask_App/templates/nfl_stadium.html",
        )
        for relative in templates:
            text = (root / relative).read_text()
            with self.subTest(relative=relative):
                self.assertNotIn("<dd>sections</dd>", text.casefold())

        self.assertNotIn(
            "stadium.section_count",
            (root / "Flask_App/templates/nfl_stadium.html").read_text(),
        )

    def test_report_labels_supported_count_as_areas_analyzed(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "Flask_App/templates/nfl_stadium.html").read_text()
        self.assertIn("{{ analyzed_area_count }}", text)
        self.assertIn("<dd>Areas analyzed</dd>", text)


if __name__ == "__main__":
    unittest.main()
