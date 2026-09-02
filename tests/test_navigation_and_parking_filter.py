from pathlib import Path
import unittest

from Flask_App.nfl_stadium_blueprint import (
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

    def test_dashboard_keeps_only_core_navigation(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "Flask_App/templates/nfl_stadium.html").read_text()
        self.assertIn('href="#overview"', text)
        self.assertIn('href="#all-sections"', text)
        self.assertIn('href="#single-games"', text)
        self.assertNotIn('href="#methodology"', text)
        self.assertNotIn("nfl-methodology", text)

    def test_primary_nav_is_shorter(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "Flask_App/templates/base.html").read_text()
        self.assertIn(">MLB</a>", text)
        self.assertNotIn(">How it works</a>", text)


if __name__ == "__main__":
    unittest.main()
