from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import (
    Base,
    CreateModel,
    Event,
    Iteration,
    Ticket,
    captured_datetime_for_storage,
    event_datetime_for_storage,
)
from Flask_App.materialized_analytics import (
    ensure_summary_schema,
    refresh_event_summary,
)
from Flask_App.nfl_stadium_blueprint import (
    _aggregated_section_insights_for,
    _bucket_summary_rows_for,
    _section_insights_for,
    _snapshot_rows_for,
    format_mlb_title,
)


class DatabaseAggregationTests(unittest.TestCase):
    @staticmethod
    def _add_game(session, source_id: int, event_date: datetime, early, late):
        event = Event(
            title="New York Mets at Washington Nationals",
            event_date=event_datetime_for_storage(event_date),
            event_sections=["Section 100"],
            URL=(
                "https://www.vividseats.com/washington-nationals-tickets-"
                f"--sports-mlb-baseball/production/{source_id}"
            ),
            Place="Nationals Park",
        )
        for hours_before, prices in ((48, early), (4, late)):
            for offset, price in enumerate(prices):
                iteration = Iteration(
                    event=event,
                    captured_at=captured_datetime_for_storage(
                        event_date
                        - timedelta(hours=hours_before)
                        + timedelta(minutes=offset)
                    ),
                )
                iteration.tickets = [
                    Ticket(
                        section="Section 100",
                        price=price,
                        ticketsPerSection=2,
                    )
                ]
        session.add(event)

    def test_database_bucket_medians_match_raw_calculation(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "baseball.db"
            engine = create_engine(f"sqlite:///{db_path}")
            Base.metadata.create_all(engine)
            ensure_summary_schema(engine)
            Session = sessionmaker(bind=engine, expire_on_commit=False)
            now = datetime.now(timezone.utc).replace(microsecond=0)
            with Session() as session:
                for game_index, shift in enumerate((0, 10, -10), start=1):
                    self._add_game(
                        session,
                        9300000 + game_index,
                        now - timedelta(days=game_index + 2),
                        [90 + shift, 100 + shift, 100 + shift, 110 + shift],
                        [60 + shift, 70 + shift, 70 + shift, 80 + shift],
                    )
                session.commit()

                events = session.query(Event).order_by(Event.id).all()
                for event in events:
                    refresh_event_summary(
                        session,
                        sport_key="mlb",
                        event_id=event.id,
                        event_date=event.event_date,
                        venue=event.Place,
                        iteration_model=Iteration,
                        ticket_model=Ticket,
                        mark_complete=True,
                    )
                session.commit()

                raw_rows = _snapshot_rows_for(
                    session,
                    [event.id for event in events],
                    Iteration,
                    Ticket,
                )
                raw_insights, _captures = _section_insights_for(
                    events,
                    raw_rows,
                    now,
                    currency="USD",
                    sport_key="mlb",
                    detail_url_builder=lambda _event, section: section,
                    secondary_url_builder=None,
                    event_label_builder=format_mlb_title,
                )
                aggregate_insights, capture_counts, analyzed_count = (
                    _aggregated_section_insights_for(
                        session,
                        events,
                        now,
                        iteration_model=Iteration,
                        ticket_model=Ticket,
                        currency="USD",
                        sport_key="mlb",
                        detail_url_builder=lambda _event, section: section,
                        secondary_url_builder=None,
                        event_label_builder=format_mlb_title,
                    )
                )

                self.assertEqual(len(raw_insights), 1)
                self.assertEqual(len(aggregate_insights), 1)
                raw = raw_insights[0]
                aggregate = aggregate_insights[0]
                for field in (
                    "average_price",
                    "average_percent_drop",
                    "average_dollar_drop",
                    "average_first_to_last_percent",
                    "average_first_to_last_dollar",
                    "game_count",
                    "drop_game_count",
                    "observation_count",
                ):
                    with self.subTest(field=field):
                        self.assertEqual(aggregate[field], raw[field])
                self.assertEqual(analyzed_count, 1)
                self.assertEqual(set(capture_counts.values()), {8})

                buckets = _bucket_summary_rows_for(
                    session,
                    events[:1],
                    Iteration,
                    Ticket,
                    "mlb",
                )
                prices_by_slot = {int(row.slot): float(row.price) for row in buckets}
                self.assertEqual(prices_by_slot[2], 100.0)
                self.assertEqual(prices_by_slot[6], 70.0)
            engine.dispose()

    def test_create_model_adds_baseball_report_indexes(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "baseball.db"
            engine = create_engine(f"sqlite:///{db_path}")
            Base.metadata.create_all(engine)
            engine.dispose()

            previous = os.environ.get("DATABASE_PATH")
            os.environ["DATABASE_PATH"] = str(db_path)
            try:
                model = CreateModel()
                with model.engine.connect() as connection:
                    iteration_indexes = {
                        row[1]
                        for row in connection.exec_driver_sql(
                            "PRAGMA index_list('iterations')"
                        )
                    }
                    ticket_indexes = {
                        row[1]
                        for row in connection.exec_driver_sql(
                            "PRAGMA index_list('tickets')"
                        )
                    }
                model.engine.dispose()
            finally:
                if previous is None:
                    os.environ.pop("DATABASE_PATH", None)
                else:
                    os.environ["DATABASE_PATH"] = previous

            self.assertIn("ix_iterations_event_captured", iteration_indexes)
            self.assertIn("ix_tickets_iteration_id", ticket_indexes)
            self.assertIn("ix_tickets_section_iteration", ticket_indexes)


class LazyHomePageTests(unittest.TestCase):
    def test_landing_pages_fetch_sections_only_after_selection(self):
        root = Path(__file__).resolve().parents[1]
        expectations = (
            (
                "Flask_App/templates/HomeScreen.html",
                "baseballOptionsUrl",
                ("placesData", "gamesData", "gameSectionsData"),
            ),
            (
                "Flask_App/templates/NFLHomeScreen.html",
                "nflOptionsUrl",
                ("nflGamesData", "nflGameSectionsData"),
            ),
            (
                "Flask_App/templates/NHLHomeScreen.html",
                "nhlOptionsUrl",
                ("nhlGamesData", "nhlGameSectionsData"),
            ),
        )
        for relative, expected, removed in expectations:
            text = (root / relative).read_text()
            with self.subTest(relative=relative):
                self.assertIn(expected, text)
                for name in removed:
                    self.assertNotIn(name, text)


if __name__ == "__main__":
    unittest.main()
