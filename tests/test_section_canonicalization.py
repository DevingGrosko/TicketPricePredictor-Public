from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest

from Flask_App.section_canonicalization import (
    canonical_section_key,
    canonical_section_label,
    canonicalize_section_labels,
    is_excluded_ticket_area,
)
from Flask_App.nfl_stadium_blueprint import _canonicalize_snapshot_rows


class SectionCanonicalizationTests(unittest.TestCase):
    def test_low_risk_formatting_aliases_share_one_identity(self):
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

    def test_fenway_aura_rebrand_is_venue_scoped(self):
        self.assertEqual(
            canonical_section_key("mlb", "Fenway Park", "Pavilion Box 11"),
            canonical_section_key("mlb", "Fenway Park", "Aura Pavilion Box 11"),
        )
        self.assertNotEqual(
            canonical_section_key("mlb", "Another Park", "Pavilion Box 11"),
            canonical_section_key("mlb", "Another Park", "Aura Pavilion Box 11"),
        )
        self.assertEqual(
            canonical_section_label("mlb", "Fenway Park", "Pavilion Box 11"),
            "Aura Pavilion Box 11",
        )

    def test_same_number_different_products_remain_separate(self):
        labels = (
            "Section 101",
            "Club 101",
            "Suite 101",
            "Upper 101",
            "Field Box 35",
            "Field Box Club 35",
            "Xfinity Club 226",
            "Xfinity Club Tables 226",
        )
        keys = [canonical_section_key("mlb", "Example Park", label) for label in labels]
        self.assertEqual(len(keys), len(set(keys)))

    def test_access_and_no_admission_products_are_excluded(self):
        for label in (
            "GP Atrium (No Admission)",
            "Optum Field Lounge (No Admission)",
            "NORTHWEST SIDELINE PASS",
            "401 W Washington St - Government Center",
            "Lexus Club Pass (No Admission)",
            "Parking Pass",
        ):
            with self.subTest(label=label):
                self.assertTrue(is_excluded_ticket_area(label))
                self.assertIsNone(canonical_section_label("nfl", "Venue", label))

        self.assertFalse(is_excluded_ticket_area("Standing Room Only"))
        self.assertIsNotNone(
            canonical_section_label("nfl", "Venue", "Standing Room Only")
        )

    def test_public_list_deduplicates_aliases_without_merging_products(self):
        labels = canonicalize_section_labels(
            "mlb",
            "Fenway Park",
            [
                "Pavilion Box 11",
                "Aura Pavilion Box 11",
                "GRANSTAND OUTFIELD 3",
                "Grandstand Outfield 3",
                "Club 101",
                "Section 101",
                "GP Atrium (No Admission)",
            ],
        )
        self.assertEqual(labels.count("Aura Pavilion Box 11"), 1)
        self.assertEqual(labels.count("Grandstand Outfield 3"), 1)
        self.assertIn("Club 101", labels)
        self.assertIn("Section 101", labels)
        self.assertEqual(len(labels), 4)


class CanonicalSnapshotRowTests(unittest.TestCase):
    def test_aliases_in_same_capture_use_cheapest_price(self):
        captured = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        event = SimpleNamespace(id=1, Place="Yankee Stadium")
        rows = [
            SimpleNamespace(
                event_id=1,
                captured_at=captured,
                section="011",
                price=120,
            ),
            SimpleNamespace(
                event_id=1,
                captured_at=captured,
                section="11",
                price=95,
            ),
        ]

        canonical = _canonicalize_snapshot_rows([event], rows, "mlb")
        self.assertEqual(len(canonical), 1)
        self.assertEqual(canonical[0].section, "11")
        self.assertEqual(canonical[0].price, 95)

    def test_aliases_across_games_recover_one_combined_history(self):
        capture_one = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
        capture_two = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
        events = [
            SimpleNamespace(id=1, Place="Fenway Park"),
            SimpleNamespace(id=2, Place="Fenway Park"),
        ]
        rows = [
            SimpleNamespace(
                event_id=1,
                captured_at=capture_one,
                section="Pavilion Box 11",
                price=90,
            ),
            SimpleNamespace(
                event_id=2,
                captured_at=capture_two,
                section="Aura Pavilion Box 11",
                price=80,
            ),
        ]

        canonical = _canonicalize_snapshot_rows(events, rows, "mlb")
        self.assertEqual(len(canonical), 2)
        self.assertEqual(
            {row.section for row in canonical},
            {"Aura Pavilion Box 11"},
        )
        self.assertEqual({row.event_id for row in canonical}, {1, 2})

    def test_raw_rows_are_not_mutated(self):
        captured = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        event = SimpleNamespace(id=1, Place="Fenway Park")
        row = SimpleNamespace(
            event_id=1,
            captured_at=captured,
            section="Pavilion Box 11",
            price=90,
        )
        _canonicalize_snapshot_rows([event], [row], "mlb")
        self.assertEqual(row.section, "Pavilion Box 11")
        self.assertEqual(row.price, 90)


class CanonicalizationPresentationTests(unittest.TestCase):
    def test_team_cards_describe_marketplace_areas_not_physical_sections(self):
        root = Path(__file__).resolve().parents[1]
        for relative in (
            "Flask_App/templates/HomeScreen.html",
            "Flask_App/templates/NFLHomeScreen.html",
            "Flask_App/templates/NHLHomeScreen.html",
            "Flask_App/templates/nfl_stadium.html",
        ):
            text = (root / relative).read_text()
            with self.subTest(relative=relative):
                self.assertIn("tracked area", text.casefold())


if __name__ == "__main__":
    unittest.main()
