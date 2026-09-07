from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import sessionmaker

from models import (
    Base,
    Event,
    Iteration,
    Ticket,
    captured_datetime_for_storage,
    event_datetime_for_storage,
    event_has_complete_public_data,
)
from Flask_App.analytics_maintenance import (
    _remove_retired_mlb_history,
    backfill_sport,
)
from Flask_App.materialized_analytics import (
    SECTION_BUCKET_SUMMARY,
    SECTION_SUMMARY_STATE,
    ensure_summary_schema,
    read_summary_rows,
)


class AnalyticsMaintenanceTests(unittest.TestCase):
    def test_mlb_backfill_is_batched_and_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseball.db"
            engine = create_engine(f"sqlite:///{path}")
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine, expire_on_commit=False)
            event_date = datetime(2026, 9, 10, 23, tzinfo=timezone.utc)
            with Session() as session:
                for index in range(2):
                    event = Event(
                        title="New York Mets at Boston Red Sox",
                        event_date=event_datetime_for_storage(
                            event_date + timedelta(days=index)
                        ),
                        event_sections=["Section 10"],
                        URL=(
                            "https://www.vividseats.com/red-sox-tickets-"
                            f"--sports-mlb-baseball/production/{991100 + index}"
                        ),
                        Place="Fenway Park",
                    )
                    iteration = Iteration(
                        event=event,
                        captured_at=captured_datetime_for_storage(
                            event_date + timedelta(days=index, hours=-48)
                        ),
                    )
                    iteration.tickets = [
                        Ticket(
                            section="Section 10",
                            price=100 + index,
                            ticketsPerSection=2,
                        )
                    ]
                    session.add(event)
                session.commit()
            engine.dispose()

            previous = os.environ.get("DATABASE_PATH")
            os.environ["DATABASE_PATH"] = str(path)
            try:
                first = backfill_sport("mlb", limit=1)
                self.assertEqual(first.processed, 1)
                self.assertEqual(first.remaining, 1)
                self.assertEqual(first.retired_events_removed, 0)
                self.assertFalse(first.complete)

                second = backfill_sport("mlb", limit=5)
                self.assertEqual(second.processed, 1)
                self.assertEqual(second.remaining, 0)
                self.assertEqual(second.retired_events_removed, 0)
                self.assertTrue(second.complete)

                from models import CreateModel

                model = CreateModel()
                with model.getSession()() as session:
                    rows = read_summary_rows(session, [1, 2])
                model.engine.dispose()
                self.assertEqual(len(rows), 2)
            finally:
                if previous is None:
                    os.environ.pop("DATABASE_PATH", None)
                else:
                    os.environ["DATABASE_PATH"] = previous

    def test_retired_rays_home_history_is_deleted_without_touching_rays_away_game(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseball.db"
            engine = create_engine(f"sqlite:///{path}")
            Base.metadata.create_all(engine)
            ensure_summary_schema(engine)
            Session = sessionmaker(bind=engine, expire_on_commit=False)
            event_date = datetime(2026, 9, 10, 23, tzinfo=timezone.utc)

            with Session() as session:
                rays_home = Event(
                    title="New York Yankees at Tampa Bay Rays",
                    event_date=event_datetime_for_storage(event_date),
                    event_sections=["Section 101"],
                    URL=(
                        "https://www.vividseats.com/rays-tickets-"
                        "--sports-mlb-baseball/production/880001"
                    ),
                    Place="George M. Steinbrenner Field",
                )
                rays_home_iteration = Iteration(
                    event=rays_home,
                    captured_at=captured_datetime_for_storage(
                        event_date - timedelta(hours=12)
                    ),
                )
                rays_home_iteration.tickets = [
                    Ticket(section="Section 101", price=50, ticketsPerSection=2)
                ]

                rays_away = Event(
                    title="Tampa Bay Rays at Washington Nationals",
                    event_date=event_datetime_for_storage(event_date + timedelta(days=1)),
                    event_sections=["Section 201"],
                    URL=(
                        "https://www.vividseats.com/nationals-tickets-"
                        "--sports-mlb-baseball/production/880002"
                    ),
                    Place="Nationals Park",
                )
                rays_away_iteration = Iteration(
                    event=rays_away,
                    captured_at=captured_datetime_for_storage(
                        event_date + timedelta(days=1, hours=-12)
                    ),
                )
                rays_away_iteration.tickets = [
                    Ticket(section="Section 201", price=75, ticketsPerSection=3)
                ]

                session.add_all([rays_home, rays_away])
                session.commit()
                rays_home_id = int(rays_home.id)
                rays_away_id = int(rays_away.id)

            refreshed_at = datetime(2026, 9, 11, tzinfo=timezone.utc).replace(tzinfo=None)
            with engine.begin() as connection:
                connection.execute(
                    insert(SECTION_BUCKET_SUMMARY),
                    [
                        {
                            "event_id": rays_home_id,
                            "section_key": "mlb|george m steinbrenner field|section 101",
                            "bucket_slot": 6,
                            "section_name": "Section 101",
                            "median_price": 50.0,
                            "observation_count": 1,
                            "first_captured_at": refreshed_at,
                            "last_captured_at": refreshed_at,
                            "refreshed_at": refreshed_at,
                        },
                        {
                            "event_id": rays_away_id,
                            "section_key": "mlb|nationals park|section 201",
                            "bucket_slot": 6,
                            "section_name": "Section 201",
                            "median_price": 75.0,
                            "observation_count": 1,
                            "first_captured_at": refreshed_at,
                            "last_captured_at": refreshed_at,
                            "refreshed_at": refreshed_at,
                        },
                    ],
                )
                connection.execute(
                    insert(SECTION_SUMMARY_STATE),
                    [
                        {
                            "event_id": rays_home_id,
                            "summary_version": 2,
                            "event_signature": "a" * 64,
                            "source_iteration_id": 1,
                            "source_iteration_count": 1,
                            "complete": True,
                            "refreshed_at": refreshed_at,
                        },
                        {
                            "event_id": rays_away_id,
                            "summary_version": 2,
                            "event_signature": "b" * 64,
                            "source_iteration_id": 2,
                            "source_iteration_count": 1,
                            "complete": True,
                            "refreshed_at": refreshed_at,
                        },
                    ],
                )
            engine.dispose()

            previous = os.environ.get("DATABASE_PATH")
            os.environ["DATABASE_PATH"] = str(path)
            try:
                self.assertFalse(
                    event_has_complete_public_data(
                        SimpleNamespace(
                            title="New York Yankees at Tampa Bay Rays",
                            Place="George M. Steinbrenner Field",
                            event_date=event_datetime_for_storage(event_date),
                        )
                    )
                )
                self.assertTrue(
                    event_has_complete_public_data(
                        SimpleNamespace(
                            title="Tampa Bay Rays at Washington Nationals",
                            Place="Nationals Park",
                            event_date=event_datetime_for_storage(event_date + timedelta(days=1)),
                        )
                    )
                )

                self.assertEqual(_remove_retired_mlb_history(), 1)

                from models import CreateModel

                model = CreateModel()
                with model.getSession()() as session:
                    remaining_events = {
                        int(row.id): row.title
                        for row in session.query(Event).order_by(Event.id).all()
                    }
                    remaining_iterations = set(
                        session.execute(select(Iteration.__table__.c.event_id)).scalars()
                    )
                    remaining_summary_events = set(
                        session.execute(
                            select(SECTION_BUCKET_SUMMARY.c.event_id)
                        ).scalars()
                    )
                    remaining_state_events = set(
                        session.execute(
                            select(SECTION_SUMMARY_STATE.c.event_id)
                        ).scalars()
                    )
                    remaining_ticket_prices = list(
                        session.execute(select(Ticket.__table__.c.price)).scalars()
                    )
                model.engine.dispose()

                self.assertNotIn(rays_home_id, remaining_events)
                self.assertIn(rays_away_id, remaining_events)
                self.assertEqual(remaining_iterations, {rays_away_id})
                self.assertEqual(remaining_summary_events, {rays_away_id})
                self.assertEqual(remaining_state_events, {rays_away_id})
                self.assertEqual(remaining_ticket_prices, [75])
            finally:
                if previous is None:
                    os.environ.pop("DATABASE_PATH", None)
                else:
                    os.environ["DATABASE_PATH"] = previous

    def test_invalid_sport_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "mlb, nfl, nhl"):
            backfill_sport("soccer")


if __name__ == "__main__":
    unittest.main()
