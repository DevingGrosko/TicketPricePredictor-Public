from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest

from flask import Flask
from jinja2 import Environment
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import (
    Base,
    Event,
    Iteration,
    Ticket,
    captured_datetime_for_storage,
    event_datetime_for_storage,
)
from Flask_App.nfl_stadium_blueprint import (
    build_mlb_stadium_context,
    build_nhl_arena_context,
    mlb_event_home_team,
    mlb_team_for_venue,
    nfl_stadium_blueprint,
)
from Flask_App.nhl_blueprint import (
    CreateNHLModel,
    NHLEvent,
    NHLIteration,
    NHLTicket,
    nhl_blueprint,
)


class MLBVenueInsightTests(unittest.TestCase):
    def _app(self) -> Flask:
        app = Flask(__name__)
        app.add_url_rule("/", endpoint="home", view_func=lambda: "home")
        app.add_url_rule("/graph", endpoint="graph", view_func=lambda: "graph")
        app.add_url_rule("/predict", endpoint="predict", view_func=lambda: "predict")
        app.register_blueprint(nfl_stadium_blueprint)
        return app

    def _store_game(self, session, source_id, event_date, first, final):
        event = Event(
            title="New York Mets at Washington Nationals",
            event_date=event_datetime_for_storage(event_date),
            event_sections=list(first),
            URL=(
                "https://www.vividseats.com/washington-nationals-tickets-"
                f"--sports-mlb-baseball/production/{source_id}"
            ),
            Place="Nationals Park",
        )
        first_iteration = Iteration(
            event=event,
            captured_at=captured_datetime_for_storage(
                event_date - timedelta(hours=48)
            ),
        )
        final_iteration = Iteration(
            event=event,
            captured_at=captured_datetime_for_storage(
                event_date - timedelta(hours=4)
            ),
        )
        first_iteration.tickets = [
            Ticket(section=section, price=price, ticketsPerSection=3)
            for section, price in first.items()
        ]
        final_iteration.tickets = [
            Ticket(section=section, price=price, ticketsPerSection=3)
            for section, price in final.items()
        ]
        session.add(event)

    def test_mlb_context_is_team_first_and_ranks_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "mlb.db"
            engine = create_engine(f"sqlite:///{db_path}")
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine, expire_on_commit=False)
            previous = os.environ.get("DATABASE_PATH")
            os.environ["DATABASE_PATH"] = str(db_path)
            try:
                now = datetime.now(timezone.utc)
                games = [
                    (
                        "9100001",
                        now - timedelta(days=12),
                        {"Section 100": 100, "Section 200": 80, "Section 500": 50},
                        {"Section 100": 70, "Section 200": 96, "Section 500": 48},
                    ),
                    (
                        "9100002",
                        now - timedelta(days=9),
                        {"Section 100": 110, "Section 200": 70, "Section 500": 60},
                        {"Section 100": 77, "Section 200": 84, "Section 500": 58},
                    ),
                    (
                        "9100003",
                        now - timedelta(days=6),
                        {"Section 100": 90, "Section 200": 90, "Section 500": 55},
                        {"Section 100": 63, "Section 200": 108, "Section 500": 52},
                    ),
                ]
                with Session() as session:
                    for source_id, event_date, first, final in games:
                        self._store_game(
                            session, source_id, event_date, first, final
                        )
                    session.commit()

                app = self._app()
                with app.test_request_context(
                    "/baseball/stadium?venue=Nationals%20Park"
                ):
                    context = build_mlb_stadium_context("Nationals Park")

                self.assertEqual(context["selected_team_label"], "Washington Nationals")
                self.assertEqual(context["game_count"], 3)
                self.assertEqual(context["completed_game_count"], 3)
                self.assertEqual(context["cheapest_sections"][0]["name"], "Section 500")
                self.assertEqual(context["biggest_drops"][0]["name"], "Section 100")
                self.assertEqual(
                    context["all_sections"][0]["average_price_label"], "$53.83"
                )

                by_name = {row["name"]: row for row in context["all_sections"]}
                self.assertAlmostEqual(
                    by_name["Section 100"]["average_percent_drop"], 30.0
                )
                self.assertIn("/graph?", by_name["Section 100"]["detail_url"])
                self.assertIn("mode=multi", by_name["Section 100"]["detail_url"])
                self.assertIn("/predict?", by_name["Section 100"]["secondary_url"])
                self.assertEqual(context["stadiums"][0]["team_label"], "Washington Nationals")
                self.assertEqual(context["games"][0]["sections"][0], "Section 100")
            finally:
                engine.dispose()
                if previous is None:
                    os.environ.pop("DATABASE_PATH", None)
                else:
                    os.environ["DATABASE_PATH"] = previous

    def test_mlb_team_display_prefers_venue_over_title_order(self):
        event = Event(
            title="Washington Nationals vs. New York Mets",
            event_date=datetime(2026, 9, 1),
            event_sections=[],
            URL="https://example.com/--sports-mlb-baseball/production/1",
            Place="Nationals Park",
        )
        self.assertEqual(mlb_team_for_venue("Nationals Park"), "Washington Nationals")
        self.assertEqual(mlb_event_home_team(event), "Washington Nationals")


class NHLVenueInsightTests(unittest.TestCase):
    def _store_game(self, session, source_id, event_date, first, final):
        event = NHLEvent(
            source_id=source_id,
            title="New York Rangers at Pittsburgh Penguins",
            event_date=event_datetime_for_storage(event_date),
            sections=list(first),
            source_url=f"https://www.vividseats.com/game/production/{source_id}",
            venue="PPG Paints Arena",
            away_team="New York Rangers",
            home_team="Pittsburgh Penguins",
            canonical_venue="PPG Paints Arena",
            venue_timezone="America/New_York",
            country="US",
            neutral_site=False,
            game_type=2,
            season=20262027,
            currency="USD",
            provider_venue="PPG Paints Arena",
        )
        first_iteration = NHLIteration(
            event=event,
            captured_at=captured_datetime_for_storage(
                event_date - timedelta(hours=48)
            ),
        )
        final_iteration = NHLIteration(
            event=event,
            captured_at=captured_datetime_for_storage(
                event_date - timedelta(hours=4)
            ),
        )
        first_iteration.tickets = [
            NHLTicket(section=section, price=price, listing_count=3)
            for section, price in first.items()
        ]
        final_iteration.tickets = [
            NHLTicket(section=section, price=price, listing_count=3)
            for section, price in final.items()
        ]
        session.add(event)

    def test_nhl_context_is_team_first_and_currency_aware(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "nhl.db"
            previous = os.environ.get("NHL_DATABASE_PATH")
            os.environ["NHL_DATABASE_PATH"] = str(db_path)
            model = CreateNHLModel(db_path)
            try:
                now = datetime.now(timezone.utc)
                games = [
                    (
                        "9200001",
                        now - timedelta(days=12),
                        {"Section 100": 100, "Section 200": 80, "Section 500": 50},
                        {"Section 100": 70, "Section 200": 96, "Section 500": 48},
                    ),
                    (
                        "9200002",
                        now - timedelta(days=9),
                        {"Section 100": 110, "Section 200": 70, "Section 500": 60},
                        {"Section 100": 77, "Section 200": 84, "Section 500": 58},
                    ),
                    (
                        "9200003",
                        now - timedelta(days=6),
                        {"Section 100": 90, "Section 200": 90, "Section 500": 55},
                        {"Section 100": 63, "Section 200": 108, "Section 500": 52},
                    ),
                ]
                with model.getSession()() as session:
                    for source_id, event_date, first, final in games:
                        self._store_game(
                            session, source_id, event_date, first, final
                        )
                    session.commit()

                app = Flask(__name__)
                app.register_blueprint(nhl_blueprint)
                app.register_blueprint(nfl_stadium_blueprint)
                with app.test_request_context(
                    "/nhl/arena?venue=PPG%20Paints%20Arena"
                ):
                    context = build_nhl_arena_context("PPG Paints Arena")

                self.assertEqual(context["selected_team_label"], "Pittsburgh Penguins")
                self.assertEqual(context["currency_label"], "USD")
                self.assertEqual(context["game_count"], 3)
                self.assertEqual(context["cheapest_sections"][0]["name"], "Section 500")
                self.assertEqual(context["biggest_drops"][0]["name"], "Section 100")
                self.assertEqual(context["stadiums"][0]["team_label"], "Pittsburgh Penguins")
                self.assertIn("/nhl/map?", context["games"][0]["direct_url"])
            finally:
                model.engine.dispose()
                if previous is None:
                    os.environ.pop("NHL_DATABASE_PATH", None)
                else:
                    os.environ["NHL_DATABASE_PATH"] = previous


class VenueTemplateTests(unittest.TestCase):
    def test_all_team_first_templates_parse(self):
        root = Path(__file__).resolve().parents[1]
        environment = Environment()
        for relative in (
            "Flask_App/templates/base.html",
            "Flask_App/templates/HomeScreen.html",
            "Flask_App/templates/NFLHomeScreen.html",
            "Flask_App/templates/NHLHomeScreen.html",
            "Flask_App/templates/nfl_stadium.html",
        ):
            environment.parse((root / relative).read_text())

    def test_directory_cards_put_team_name_before_venue_details(self):
        root = Path(__file__).resolve().parents[1]
        dashboard = (root / "Flask_App/templates/nfl_stadium.html").read_text()
        self.assertIn('<h2>{{ stadium.team_label', dashboard)
        self.assertIn('nfl-stadium-card__eyebrow">{{ stadium.venue }}', dashboard)

        mlb_home = (root / "Flask_App/templates/HomeScreen.html").read_text()
        self.assertIn("{% set team = mlb_team_for_venue(place) %}", mlb_home)
        self.assertIn("<h3>{{ team }}</h3>", mlb_home)


if __name__ == "__main__":
    unittest.main()
