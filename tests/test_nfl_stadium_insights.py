from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask
from jinja2 import Environment

from collector import EventSnapshot, SectionSnapshot
from Flask_App.nfl_blueprint import nfl_blueprint, store_nfl_snapshot
from Flask_App.nfl_stadium_blueprint import (
    build_nfl_section_context,
    build_nfl_stadium_context,
    nfl_stadium,
    nfl_stadium_blueprint,
)


class NFLStadiumInsightTests(unittest.TestCase):
    def _snapshot(self, source_id, prices):
        return EventSnapshot(
            source_id=source_id,
            title="Dallas Cowboys at New York Giants",
            venue="MetLife Stadium",
            sections=tuple(
                SectionSnapshot(
                    section=section,
                    price=price,
                    listing_count=3,
                    row="A",
                    quantity="2",
                    displayed_price=str(price),
                    alternate_price="",
                )
                for section, price in prices.items()
            ),
        )

    def _store_game(self, db_path, source_id, event_date, first, final):
        url = (
            "https://www.vividseats.com/game/production/"
            f"{source_id}"
        )
        first_capture = event_date - timedelta(hours=48)
        final_capture = event_date - timedelta(hours=4)
        store_nfl_snapshot(
            url,
            event_date,
            self._snapshot(source_id, first),
            first_capture,
            db_path=db_path,
        )
        store_nfl_snapshot(
            url,
            event_date,
            self._snapshot(source_id, final),
            final_capture,
            db_path=db_path,
        )

    def test_stadium_context_ranks_price_and_decline_across_games(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "nfl.db"
            previous = os.environ.get("NFL_DATABASE_PATH")
            os.environ["NFL_DATABASE_PATH"] = str(db_path)
            try:
                now = datetime.now(timezone.utc)
                games = [
                    (
                        "8000001",
                        now - timedelta(days=12),
                        {
                            "Section 100": 100,
                            "Section 200": 80,
                            "Section 500": 50,
                        },
                        {
                            "Section 100": 70,
                            "Section 200": 96,
                            "Section 500": 48,
                        },
                    ),
                    (
                        "8000002",
                        now - timedelta(days=9),
                        {
                            "Section 100": 110,
                            "Section 200": 70,
                            "Section 500": 60,
                        },
                        {
                            "Section 100": 77,
                            "Section 200": 84,
                            "Section 500": 58,
                        },
                    ),
                    (
                        "8000003",
                        now - timedelta(days=6),
                        {
                            "Section 100": 90,
                            "Section 200": 90,
                            "Section 500": 55,
                        },
                        {
                            "Section 100": 63,
                            "Section 200": 108,
                            "Section 500": 52,
                        },
                    ),
                ]
                for source_id, event_date, first, final in games:
                    self._store_game(
                        db_path,
                        source_id,
                        event_date,
                        first,
                        final,
                    )

                app = Flask(__name__)
                app.register_blueprint(nfl_blueprint)
                app.register_blueprint(nfl_stadium_blueprint)
                with app.test_request_context(
                    "/nfl/stadium?venue=MetLife%20Stadium"
                ):
                    context = build_nfl_stadium_context(
                        "MetLife Stadium"
                    )

                self.assertEqual(context["game_count"], 3)
                self.assertEqual(context["completed_game_count"], 3)
                self.assertEqual(context["section_count"], 3)
                self.assertEqual(
                    context["cheapest_sections"][0]["name"],
                    "Section 500",
                )
                self.assertEqual(
                    context["biggest_drops"][0]["name"],
                    "Section 100",
                )

                by_name = {
                    section["name"]: section
                    for section in context["all_sections"]
                }
                self.assertAlmostEqual(
                    by_name["Section 100"]["average_price"],
                    85.0,
                )
                self.assertAlmostEqual(
                    by_name["Section 100"][
                        "average_percent_drop"
                    ],
                    30.0,
                )
                self.assertEqual(
                    by_name["Section 100"][
                        "average_dollar_drop_label"
                    ],
                    "−$30",
                )
                self.assertEqual(
                    by_name["Section 100"]["drop_frequency"],
                    100,
                )
                self.assertFalse(
                    by_name["Section 100"]["is_low_price_sample"]
                )
                self.assertEqual(
                    by_name["Section 200"]["direction"],
                    "up",
                )
                self.assertIn(
                    "/nfl/stadium/section?",
                    by_name["Section 100"]["detail_url"],
                )

                with app.test_request_context(
                    "/nfl/stadium/section?venue=MetLife%20Stadium&section=Section%20100"
                ):
                    section_context = build_nfl_section_context(
                        "MetLife Stadium",
                        "Section 100",
                    )

                self.assertIsNone(section_context["error"])
                self.assertEqual(
                    section_context["selected_section"],
                    "Section 100",
                )
                self.assertAlmostEqual(
                    section_context["section_summary"]["average_price"],
                    85.0,
                )
                self.assertEqual(len(section_context["timeline"]["points"]), 2)
                self.assertAlmostEqual(
                    section_context["timeline"]["points"][0]["average_price"],
                    100.0,
                )
                self.assertAlmostEqual(
                    section_context["timeline"]["points"][-1]["average_price"],
                    70.0,
                )
                self.assertEqual(
                    section_context["timeline"]["movement_label"],
                    "−$30",
                )
                self.assertEqual(
                    section_context["timeline"]["movement_percent_label"],
                    "−30.0%",
                )
                self.assertEqual(len(section_context["section_games"]), 3)
                self.assertEqual(
                    section_context["map_data"]["selected_section"],
                    "Section 100",
                )
                self.assertEqual(
                    section_context["map_data"]["geometry_mode"],
                    "schematic",
                )
            finally:
                if previous is None:
                    os.environ.pop("NFL_DATABASE_PATH", None)
                else:
                    os.environ["NFL_DATABASE_PATH"] = previous

    @patch("Flask_App.nfl_stadium_blueprint.render_template")
    @patch(
        "Flask_App.nfl_stadium_blueprint.build_nfl_stadium_context"
    )
    def test_stadium_route_uses_selected_venue(
        self,
        build_context,
        render,
    ):
        build_context.return_value = {
            "stadiums": [],
            "stadium_count": 0,
            "selected_venue": "MetLife Stadium",
            "error": None,
        }
        render.return_value = "dashboard"

        app = Flask(__name__)
        app.register_blueprint(nfl_stadium_blueprint)
        with app.test_request_context(
            "/nfl/stadium?venue=MetLife%20Stadium"
        ):
            response = nfl_stadium()

        self.assertEqual(response, "dashboard")
        build_context.assert_called_once_with("MetLife Stadium")
        render.assert_called_once_with(
            "nfl_stadium.html",
            stadiums=[],
            stadium_count=0,
            selected_venue="MetLife Stadium",
            error=None,
        )

    def test_stadium_templates_parse(self):
        root = Path(__file__).resolve().parents[1]
        environment = Environment()
        for relative in (
            "Flask_App/templates/base.html",
            "Flask_App/templates/NFLHomeScreen.html",
            "Flask_App/templates/nfl_stadium.html",
            "Flask_App/templates/venue_section.html",
        ):
            environment.parse((root / relative).read_text())


if __name__ == "__main__":
    unittest.main()
