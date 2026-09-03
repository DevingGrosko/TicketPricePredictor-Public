from pathlib import Path
import unittest

from Flask_App.nfl_stadium_blueprint import (
    _currency_money,
    _currency_price_change,
    _public_sections,
    is_parking_section,
)


class ParkingSectionFilterTests(unittest.TestCase):
    def test_parking_inventory_is_detected(self):
        for value in (
            "Parking",
            "Parking Pass",
            "VIP Parking - Lot A",
            "Lot B",
            "Garage 3",
            "Park and Ride",
        ):
            with self.subTest(value=value):
                self.assertTrue(is_parking_section(value))

    def test_real_seating_sections_are_kept(self):
        for value in (
            "Section 101",
            "Club Level",
            "Upper 512",
            "Park Level 200",
            "Field Box 14",
        ):
            with self.subTest(value=value):
                self.assertFalse(is_parking_section(value))

    def test_public_sections_remove_parking(self):
        self.assertEqual(
            _public_sections(
                [
                    "Section 101",
                    "Parking Pass",
                    "Lot C",
                    "Club 2",
                ]
            ),
            ["Club 2", "Section 101"],
        )


class CurrencyFormattingTests(unittest.TestCase):
    def test_zero_dollar_values_render_without_crashing(self):
        self.assertEqual(_currency_money(0, "USD"), "$0")
        self.assertEqual(_currency_money(0, "CAD"), "CA$0")
        self.assertEqual(_currency_price_change(0, "USD"), "$0")
        self.assertEqual(_currency_price_change(0.0, "CAD"), "CA$0")


class SimplifiedNavigationTests(unittest.TestCase):
    def test_landing_pages_skip_preview_and_three_step_strip(self):
        root = Path(__file__).resolve().parents[1]
        for relative in (
            "Flask_App/templates/HomeScreen.html",
            "Flask_App/templates/NFLHomeScreen.html",
            "Flask_App/templates/NHLHomeScreen.html",
        ):
            text = (root / relative).read_text()
            with self.subTest(relative=relative):
                self.assertNotIn('class="nfl-data-bar"', text)
                self.assertNotIn('class="nfl-stadium-preview', text)
                self.assertIn("nfl-hero--compact", text)

    def test_dashboard_keeps_rankings_and_compact_lookup(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "Flask_App/templates/nfl_stadium.html").read_text()
        self.assertIn('id="overview"', text)
        self.assertIn("data-section-jump", text)
        self.assertIn("Single-game tools", text)
        self.assertNotIn('id="all-sections"', text)
        self.assertNotIn('id="single-games"', text)
        self.assertNotIn('class="nfl-dashboard-nav"', text)
        self.assertNotIn("nfl-section-table", text)
        self.assertNotIn("nfl-game-history-list", text)

        section = (root / "Flask_App/templates/venue_section.html").read_text()
        self.assertIn('<details class="venue-section-games', section)
        self.assertIn("Time-balanced average", section)
        self.assertIn("Typical maximum decline", section)
        self.assertIn("Average first-to-last", section)
        self.assertIn("Dropped at least", section)

        dashboard = (root / "Flask_App/templates/nfl_stadium.html").read_text()
        self.assertIn("Largest typical drops", dashboard)
        self.assertIn("fell {{ section.material_drop_threshold|int }}%+", dashboard)
        self.assertIn("Peak {{ section.drop_peak_label", dashboard)
        self.assertIn("→ low {{ section.drop_low_label", dashboard)
        self.assertIn("ranking-readability.css", dashboard)
        self.assertNotIn("Median across", dashboard)
        self.assertNotIn("nfl-ranking-card__badge", dashboard)
        self.assertNotIn("nfl-report-method-note", dashboard)

    def test_primary_nav_is_shorter(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "Flask_App/templates/base.html").read_text()
        self.assertIn(">MLB</a>", text)
        self.assertNotIn(">How it works</a>", text)


if __name__ == "__main__":
    unittest.main()
