from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from concert_models import (
    ConcertEvent,
    ConcertIteration,
    ConcertTicket,
    CreateConcertModel,
)
from legacy_concert_migration import migrate_legacy_concert_rows
from models import Base, Event, Iteration, Ticket


class LegacyConcertMigrationTests(unittest.TestCase):
    def test_moves_concert_history_and_leaves_baseball_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseball_db = root / "baseball.db"
            concert_db = root / "concerts.db"
            engine = create_engine(f"sqlite:///{baseball_db}")
            Base.metadata.create_all(engine)
            SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

            concert_url = (
                "https://www.vividseats.com/artist-tickets-new-york-"
                "8-31-2026--concerts-pop/production/6965624"
            )
            baseball_url = (
                "https://www.vividseats.com/new-york-yankees-tickets-"
                "8-31-2026--sports-mlb-baseball/production/5965676"
            )
            with SessionLocal() as session:
                concert = Event(
                    title="Test Artist",
                    event_date=datetime(2026, 8, 31, 20),
                    event_sections=["GA", "Floor A"],
                    URL=concert_url,
                    Place="Test Arena",
                )
                first = Iteration(
                    event=concert,
                    captured_at=datetime(2026, 8, 29, 3, 30),
                )
                second = Iteration(
                    event=concert,
                    captured_at=datetime(2026, 8, 29, 4, 0),
                )
                first.tickets.extend(
                    [
                        Ticket(section="GA", price=100, ticketsPerSection=4),
                        Ticket(section="Floor A", price=120, ticketsPerSection=2),
                    ]
                )
                second.tickets.extend(
                    [
                        Ticket(section="GA", price=95, ticketsPerSection=5),
                        Ticket(section="Floor A", price=115, ticketsPerSection=3),
                    ]
                )
                baseball = Event(
                    title="Red Sox at Yankees",
                    event_date=datetime(2026, 8, 31, 19),
                    event_sections=["Section 110"],
                    URL=baseball_url,
                    Place="Yankee Stadium",
                )
                baseball_iteration = Iteration(
                    event=baseball,
                    captured_at=datetime(2026, 8, 29, 4, 0),
                )
                baseball_iteration.tickets.append(
                    Ticket(
                        section="Section 110",
                        price=80,
                        ticketsPerSection=6,
                    )
                )
                session.add_all([concert, baseball])
                session.commit()

            report = migrate_legacy_concert_rows(
                baseball_path=baseball_db,
                concert_path=concert_db,
                lock_path=root / "migration.lock",
                make_backups=False,
            )
            self.assertEqual(report["legacy_events_found"], 1)
            self.assertEqual(report["events_migrated"], 1)
            self.assertEqual(report["iterations_migrated"], 2)
            self.assertEqual(report["tickets_migrated"], 4)

            with SessionLocal() as session:
                remaining = session.query(Event).all()
                self.assertEqual(len(remaining), 1)
                self.assertEqual(remaining[0].URL, baseball_url)
                self.assertEqual(session.query(Iteration).count(), 1)
                self.assertEqual(session.query(Ticket).count(), 1)

            concert_model = CreateConcertModel(concert_db)
            with concert_model.getSession()() as session:
                migrated = session.query(ConcertEvent).one()
                self.assertEqual(migrated.source_id, "6965624")
                self.assertEqual(migrated.title, "Test Artist")
                self.assertEqual(migrated.venue, "Test Arena")
                self.assertEqual(session.query(ConcertIteration).count(), 2)
                self.assertEqual(session.query(ConcertTicket).count(), 4)
                prices = [
                    row.price
                    for row in session.query(ConcertTicket)
                    .filter(ConcertTicket.section == "GA")
                    .order_by(ConcertTicket.price)
                    .all()
                ]
                self.assertEqual(prices, [95, 100])

            rerun = migrate_legacy_concert_rows(
                baseball_path=baseball_db,
                concert_path=concert_db,
                lock_path=root / "migration.lock",
                make_backups=False,
            )
            self.assertEqual(rerun["legacy_events_found"], 0)
            self.assertEqual(rerun["events_migrated"], 0)

            engine.dispose()


if __name__ == "__main__":
    unittest.main()
