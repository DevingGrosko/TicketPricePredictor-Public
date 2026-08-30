from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from collector import EventSnapshot, SectionSnapshot
from Flask_App.nfl_blueprint import (
    CreateNFLModel,
    NFLEvent,
    format_nfl_capture_label,
    nfl_archive,
    nfl_blueprint,
    nfl_home,
    nfl_map,
    store_nfl_snapshot,
)
from nfl_metadata import (
    canonical_venue_name,
    eastern_iso,
    extract_map_geometry_from_svg,
    geometry_is_usable,
    sanitize_map_geometry,
)
from nfl_schedule_collector import parse_schedule_payload


class NFLMetadataEnhancementTests(unittest.TestCase):
    def _snapshot(self, source_id: str, title: str = "Dallas Cowboys at New York Giants"):
        return EventSnapshot(
            source_id=source_id,
            title=title,
            venue="US Bank Stadium",
            sections=tuple(
                SectionSnapshot(
                    section=f"Section {index}",
                    price=100 + index,
                    listing_count=2,
                    row="A",
                    quantity="2",
                    displayed_price=str(100 + index),
                    alternate_price="",
                )
                for index in range(10)
            ),
        )

    def _geometry(self):
        return {
            "source": "vivid-test-svg",
            "view_box": [0, 0, 100, 100],
            "sections": [
                {
                    "name": f"Section {index}",
                    "shapes": [
                        {
                            "path": (
                                f"M {index * 5} 0 L {index * 5 + 4} 0 "
                                f"L {index * 5 + 4} 4 L {index * 5} 4 Z"
                            ),
                            "transform": "",
                        }
                    ],
                }
                for index in range(10)
            ],
        }

    def test_canonical_venue_and_eastern_offsets(self):
        self.assertEqual(canonical_venue_name("US Bank Stadium"), "U.S. Bank Stadium")
        summer = datetime(2026, 7, 1, 17, tzinfo=timezone.utc)
        winter = datetime(2026, 1, 1, 17, tzinfo=timezone.utc)
        self.assertTrue(eastern_iso(summer).endswith("-04:00"))
        self.assertTrue(eastern_iso(winter).endswith("-05:00"))
        self.assertIn("EDT", format_nfl_capture_label(summer))
        self.assertIn("EST", format_nfl_capture_label(winter))

    def test_svg_geometry_is_matched_and_unsafe_markup_is_rejected(self):
        sections = [f"Section {index}" for index in range(10)]
        svg = "<svg viewBox='0 0 100 100'>" + "".join(
            (
                f"<g id='section-{index}'><path d='M {index * 5} 0 "
                f"L {index * 5 + 4} 0 L {index * 5 + 4} 4 "
                f"L {index * 5} 4 Z'/></g>"
            )
            for index in range(10)
        ) + "</svg>"
        geometry = extract_map_geometry_from_svg(svg, sections)
        self.assertIsNotNone(geometry)
        self.assertEqual(geometry["mapped_section_count"], 10)
        self.assertTrue(geometry_is_usable(geometry, sections))

        rejected = sanitize_map_geometry(
            {
                "view_box": [0, 0, 100, 100],
                "sections": [
                    {
                        "name": "Section 1",
                        "shapes": [
                            {"path": "M 0 0 L 1 1 Z' onload='alert(1)"}
                        ],
                    }
                ],
            },
            sections,
        )
        self.assertIsNone(rejected)

    def test_schedule_parser_carries_location_and_neutral_site(self):
        now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        event_date = now + timedelta(days=2)
        payload = {
            "events": [
                {
                    "id": "schedule-1",
                    "date": event_date.isoformat().replace("+00:00", "Z"),
                    "name": "Dallas Cowboys at Minnesota Vikings",
                    "competitions": [
                        {
                            "id": "schedule-1",
                            "neutralSite": True,
                            "venue": {
                                "fullName": "US Bank Stadium",
                                "address": {
                                    "city": "Minneapolis",
                                    "country": "USA",
                                },
                            },
                            "status": {"type": {"completed": False}},
                            "competitors": [
                                {
                                    "homeAway": "away",
                                    "team": {"displayName": "Dallas Cowboys"},
                                },
                                {
                                    "homeAway": "home",
                                    "team": {"displayName": "Minnesota Vikings"},
                                },
                            ],
                        }
                    ],
                }
            ]
        }
        game = parse_schedule_payload(payload, now)[0]
        self.assertEqual(game.venue, "U.S. Bank Stadium")
        self.assertEqual(game.city, "Minneapolis")
        self.assertEqual(game.country, "USA")
        self.assertTrue(game.neutral_site)

    def test_existing_database_is_migrated_and_backfilled(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "legacy-nfl.db"
            with sqlite3.connect(db_path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE nfl_event (
                        id INTEGER PRIMARY KEY,
                        source_id VARCHAR NOT NULL UNIQUE,
                        title VARCHAR NOT NULL,
                        event_date DATETIME NOT NULL,
                        sections JSON NOT NULL,
                        source_url VARCHAR NOT NULL UNIQUE,
                        venue VARCHAR NOT NULL
                    );
                    INSERT INTO nfl_event (
                        source_id, title, event_date, sections, source_url, venue
                    ) VALUES (
                        'legacy-1',
                        'Dallas Cowboys at New York Giants',
                        '2026-09-10 20:00:00',
                        '["Section 1"]',
                        'https://www.vividseats.com/game/production/9000001',
                        'US Bank Stadium'
                    );
                    """
                )

            model = CreateNFLModel(db_path)
            try:
                with model.getSession()() as session:
                    event = session.query(NFLEvent).one()
                    self.assertEqual(event.away_team, "Dallas Cowboys")
                    self.assertEqual(event.home_team, "New York Giants")
                    self.assertEqual(event.provider_venue, "US Bank Stadium")
                    self.assertEqual(event.canonical_venue, "U.S. Bank Stadium")
            finally:
                model.engine.dispose()

    @patch("Flask_App.nfl_blueprint.render_template")
    def test_metadata_geometry_and_archive_are_exposed(self, render):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "nfl.db"
            old_db = os.environ.get("NFL_DATABASE_PATH")
            os.environ["NFL_DATABASE_PATH"] = str(db_path)
            try:
                now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
                future_snapshot = self._snapshot("future-1")
                future_id, _, _ = store_nfl_snapshot(
                    "https://www.vividseats.com/game/production/9000002",
                    now + timedelta(days=2),
                    future_snapshot,
                    now,
                    db_path=db_path,
                    schedule_metadata={
                        "schedule_id": "espn-future-1",
                        "away_team": "Dallas Cowboys",
                        "home_team": "New York Giants",
                        "canonical_venue": "US Bank Stadium",
                        "city": "Minneapolis",
                        "country": "USA",
                        "neutral_site": False,
                    },
                    map_geometry=self._geometry(),
                )
                past_snapshot = self._snapshot(
                    "past-1", "Buffalo Bills at New York Jets"
                )
                store_nfl_snapshot(
                    "https://www.vividseats.com/game/production/9000003",
                    now - timedelta(days=1),
                    past_snapshot,
                    now - timedelta(days=3),
                    db_path=db_path,
                )

                app = Flask(__name__)
                app.register_blueprint(nfl_blueprint)
                render.return_value = "ok"

                with app.test_request_context("/nfl"):
                    self.assertEqual(nfl_home(), "ok")
                history_context = render.call_args.kwargs
                self.assertEqual(history_context["game_count"], 2)
                self.assertEqual(history_context["upcoming_count"], 1)
                self.assertEqual(history_context["completed_count"], 1)
                self.assertEqual(history_context["stadium_count"], 1)
                self.assertEqual(
                    history_context["stadium_game_counts"],
                    {"U.S. Bank Stadium": 2},
                )
                self.assertIn("New York Giants", history_context["games_dict"])
                self.assertIn("New York Jets", history_context["games_dict"])
                self.assertTrue(
                    history_context["games_dict"]["New York Giants"][0]["label"]
                    .startswith("Upcoming ·")
                )
                self.assertTrue(
                    history_context["games_dict"]["New York Jets"][0]["label"]
                    .startswith("Completed ·")
                )

                render.reset_mock(return_value=True)
                render.return_value = "history"
                with app.test_request_context("/nfl/archive"):
                    self.assertEqual(nfl_archive(), "history")
                legacy_context = render.call_args.kwargs
                self.assertEqual(legacy_context["game_count"], 2)
                self.assertIn("New York Giants", legacy_context["games_dict"])
                self.assertIn("New York Jets", legacy_context["games_dict"])


                render.reset_mock(return_value=True)
                render.return_value = "map"
                with app.test_request_context(
                    f"/nfl/map?team=New%20York%20Giants&game={future_id}"
                ):
                    self.assertEqual(nfl_map(), "map")
                map_context = render.call_args.kwargs
                self.assertEqual(map_context["venue"], "U.S. Bank Stadium")
                self.assertEqual(map_context["city"], "Minneapolis")
                self.assertTrue(map_context["has_provider_geometry"])
                self.assertEqual(map_context["map_geometry_sections"], 10)
                self.assertEqual(
                    map_context["map_data"]["geometry_mode"], "provider"
                )

                model = CreateNFLModel(db_path)
                try:
                    with model.getSession()() as session:
                        event = session.query(NFLEvent).filter_by(id=future_id).one()
                        self.assertEqual(event.schedule_id, "espn-future-1")
                        self.assertEqual(event.canonical_venue, "U.S. Bank Stadium")
                        self.assertEqual(event.map_source, "vivid-test-svg")
                finally:
                    model.engine.dispose()
            finally:
                if old_db is None:
                    os.environ.pop("NFL_DATABASE_PATH", None)
                else:
                    os.environ["NFL_DATABASE_PATH"] = old_db


if __name__ == "__main__":
    unittest.main()
