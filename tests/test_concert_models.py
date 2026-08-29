from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from collector import EventSnapshot, SectionSnapshot
from concert_models import (
    ConcertEvent,
    ConcertIteration,
    ConcertTicket,
    CreateConcertModel,
    store_concert_snapshot,
)


class ConcertDatabaseIsolationTests(unittest.TestCase):
    def test_concerts_are_stored_in_their_own_database_and_hour_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "concerts.db"
            snapshot = EventSnapshot(
                source_id="1234567",
                title="Artist at Test Arena",
                venue="Test Arena",
                sections=tuple(
                    SectionSnapshot(
                        section=f"Section {index}",
                        price=50 + index,
                        listing_count=1,
                        row="",
                        quantity="2",
                        displayed_price=str(50 + index),
                        alternate_price="",
                    )
                    for index in range(10)
                ),
            )
            event_date = datetime(2026, 9, 5, 20, tzinfo=timezone.utc)
            captured_at = datetime(2026, 8, 30, 14, tzinfo=timezone.utc)
            url = (
                "https://www.vividseats.com/artist-9-5-2026--concerts-pop/"
                "production/1234567"
            )

            event_id, iteration_id, stored = store_concert_snapshot(
                url,
                event_date,
                snapshot,
                captured_at,
                db_path=database,
            )
            self.assertTrue(stored)
            duplicate_event, duplicate_iteration, stored_again = store_concert_snapshot(
                url,
                event_date,
                snapshot,
                captured_at,
                db_path=database,
            )
            self.assertFalse(stored_again)
            self.assertEqual(duplicate_event, event_id)
            self.assertEqual(duplicate_iteration, iteration_id)

            model = CreateConcertModel(database)
            with model.getSession()() as session:
                self.assertEqual(session.query(ConcertEvent).count(), 1)
                self.assertEqual(session.query(ConcertIteration).count(), 1)
                self.assertEqual(session.query(ConcertTicket).count(), 10)

            self.assertTrue(database.exists())
            self.assertFalse((Path(directory) / "Event-collection.db").exists())


if __name__ == "__main__":
    unittest.main()
