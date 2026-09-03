from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest

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
from Flask_App.analytics_maintenance import backfill_sport
from Flask_App.materialized_analytics import read_summary_rows


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
                self.assertFalse(first.complete)

                second = backfill_sport("mlb", limit=5)
                self.assertEqual(second.processed, 1)
                self.assertEqual(second.remaining, 0)
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

    def test_invalid_sport_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "mlb, nfl, nhl"):
            backfill_sport("soccer")


if __name__ == "__main__":
    unittest.main()
