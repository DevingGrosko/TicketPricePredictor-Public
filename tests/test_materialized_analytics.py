from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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
from Flask_App.materialized_analytics import (
    dirty_venue_count,
    dirty_venues,
    ensure_summary_schema,
    mark_venue_clean,
    read_summary_rows,
    refresh_event_summary,
    refresh_event_summary_safely,
    stale_event_ids,
    timeline_bucket_slot,
    venue_revision,
)


class MaterializedAnalyticsTests(unittest.TestCase):
    def _database(self):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "analytics.db"
        engine = create_engine(f"sqlite:///{path}")
        Base.metadata.create_all(engine)
        ensure_summary_schema(engine)
        return directory, engine, sessionmaker(bind=engine, expire_on_commit=False)

    @staticmethod
    def _event(event_date):
        return Event(
            title="New York Mets at Boston Red Sox",
            event_date=event_datetime_for_storage(event_date),
            event_sections=["INFIELD BOX 119", "infield box 119"],
            URL=(
                "https://www.vividseats.com/red-sox-tickets-"
                "--sports-mlb-baseball/production/991001"
            ),
            Place="Fenway Park",
        )

    def test_refresh_combines_aliases_and_uses_one_minimum_per_capture(self):
        directory, engine, Session = self._database()
        try:
            event_date = datetime(2026, 9, 10, 23, tzinfo=timezone.utc)
            with Session() as session:
                event = self._event(event_date)
                for hours, rows in (
                    (48, [("INFIELD BOX 119", 120), ("infield box 119", 110)]),
                    (47, [("INFIELD BOX 119", 130)]),
                    (4, [("infield box 119", 80)]),
                ):
                    iteration = Iteration(
                        event=event,
                        captured_at=captured_datetime_for_storage(
                            event_date - timedelta(hours=hours)
                        ),
                    )
                    iteration.tickets = [
                        Ticket(section=section, price=price, ticketsPerSection=1)
                        for section, price in rows
                    ]
                session.add(event)
                session.flush()

                result = refresh_event_summary(
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

                summaries = read_summary_rows(session, [event.id])
                self.assertEqual(result.row_count, 2)
                self.assertTrue(result.complete)
                self.assertEqual(len({row.section_key for row in summaries}), 1)
                by_slot = {int(row.slot): row for row in summaries}
                self.assertEqual(float(by_slot[2].price), 120.0)
                self.assertEqual(int(by_slot[2].observation_count), 2)
                self.assertEqual(float(by_slot[6].price), 80.0)
                self.assertEqual(dirty_venue_count(session), 1)
        finally:
            engine.dispose()
            directory.cleanup()

    def test_full_refresh_ignores_observations_outside_the_analysis_horizon(self):
        directory, engine, Session = self._database()
        try:
            event_date = datetime(2026, 9, 10, 23, tzinfo=timezone.utc)
            with Session() as session:
                event = self._event(event_date)
                outside = Iteration(
                    event=event,
                    captured_at=captured_datetime_for_storage(
                        event_date - timedelta(hours=240)
                    ),
                )
                outside.tickets = [
                    Ticket(section="INFIELD BOX 119", price=999, ticketsPerSection=1)
                ]
                inside = Iteration(
                    event=event,
                    captured_at=captured_datetime_for_storage(
                        event_date - timedelta(hours=48)
                    ),
                )
                inside.tickets = [
                    Ticket(section="INFIELD BOX 119", price=100, ticketsPerSection=1)
                ]
                session.add(event)
                session.flush()

                result = refresh_event_summary(
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

                summaries = read_summary_rows(session, [event.id])
                self.assertEqual(result.row_count, 1)
                self.assertEqual(len(summaries), 1)
                self.assertEqual(float(summaries[0].price), 100.0)
                self.assertEqual(int(summaries[0].observation_count), 1)
        finally:
            engine.dispose()
            directory.cleanup()

    def test_incremental_refresh_updates_only_the_affected_window(self):
        directory, engine, Session = self._database()
        try:
            event_date = datetime(2026, 9, 10, 23, tzinfo=timezone.utc)
            with Session() as session:
                event = self._event(event_date)
                early = Iteration(
                    event=event,
                    captured_at=captured_datetime_for_storage(
                        event_date - timedelta(hours=48)
                    ),
                )
                early.tickets = [
                    Ticket(section="INFIELD BOX 119", price=100, ticketsPerSection=1)
                ]
                session.add(event)
                session.flush()
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

                late = Iteration(
                    event=event,
                    captured_at=captured_datetime_for_storage(
                        event_date - timedelta(hours=4)
                    ),
                )
                late.tickets = [
                    Ticket(section="infield box 119", price=70, ticketsPerSection=1)
                ]
                session.add(late)
                session.flush()
                slot = timeline_bucket_slot("mlb", event.event_date, late.captured_at)
                self.assertEqual(slot, 6)
                refresh_event_summary(
                    session,
                    sport_key="mlb",
                    event_id=event.id,
                    event_date=event.event_date,
                    venue=event.Place,
                    iteration_model=Iteration,
                    ticket_model=Ticket,
                    bucket_slots=(slot,),
                )
                session.commit()

                summaries = read_summary_rows(session, [event.id])
                by_slot = {int(row.slot): float(row.price) for row in summaries}
                self.assertEqual(by_slot[2], 100.0)
                self.assertEqual(by_slot[6], 70.0)
                self.assertEqual(
                    stale_event_ids(
                        session,
                        [event],
                        sport_key="mlb",
                        venue_getter=lambda item: item.Place,
                        iteration_model=Iteration,
                    ),
                    [],
                )
        finally:
            engine.dispose()
            directory.cleanup()

    def test_incremental_refresh_falls_back_to_full_after_missed_iterations(self):
        directory, engine, Session = self._database()
        try:
            event_date = datetime(2026, 9, 10, 23, tzinfo=timezone.utc)
            with Session() as session:
                event = self._event(event_date)
                first = Iteration(
                    event=event,
                    captured_at=captured_datetime_for_storage(
                        event_date - timedelta(hours=48)
                    ),
                )
                first.tickets = [
                    Ticket(section="INFIELD BOX 119", price=100, ticketsPerSection=1)
                ]
                session.add(event)
                session.flush()
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

                middle = Iteration(
                    event=event,
                    captured_at=captured_datetime_for_storage(
                        event_date - timedelta(hours=20)
                    ),
                )
                middle.tickets = [
                    Ticket(section="INFIELD BOX 119", price=85, ticketsPerSection=1)
                ]
                final = Iteration(
                    event=event,
                    captured_at=captured_datetime_for_storage(
                        event_date - timedelta(hours=4)
                    ),
                )
                final.tickets = [
                    Ticket(section="INFIELD BOX 119", price=70, ticketsPerSection=1)
                ]
                session.add_all((middle, final))
                session.commit()

                slot = timeline_bucket_slot("mlb", event.event_date, final.captured_at)
                result = refresh_event_summary(
                    session,
                    sport_key="mlb",
                    event_id=event.id,
                    event_date=event.event_date,
                    venue=event.Place,
                    iteration_model=Iteration,
                    ticket_model=Ticket,
                    bucket_slots=(slot,),
                )
                session.commit()
                self.assertEqual(result.bucket_count, 7)
                summaries = read_summary_rows(session, [event.id])
                self.assertEqual({int(row.slot) for row in summaries}, {2, 4, 6})
        finally:
            engine.dispose()
            directory.cleanup()

    def test_safe_refresh_failure_preserves_existing_summary(self):
        directory, engine, Session = self._database()
        try:
            event_date = datetime(2026, 9, 10, 23, tzinfo=timezone.utc)
            with Session() as session:
                event = self._event(event_date)
                iteration = Iteration(
                    event=event,
                    captured_at=captured_datetime_for_storage(
                        event_date - timedelta(hours=4)
                    ),
                )
                iteration.tickets = [
                    Ticket(section="INFIELD BOX 119", price=80, ticketsPerSection=1)
                ]
                session.add(event)
                session.flush()
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
                event_id = event.id

            with patch(
                "Flask_App.materialized_analytics.refresh_event_summary",
                side_effect=RuntimeError("synthetic refresh failure"),
            ):
                result = refresh_event_summary_safely(
                    Session,
                    sport_key="mlb",
                    event_id=event_id,
                    event_date=event_datetime_for_storage(event_date),
                    venue="Fenway Park",
                    iteration_model=Iteration,
                    ticket_model=Ticket,
                    bucket_slots=(6,),
                )
            self.assertIsNone(result)
            with Session() as session:
                rows = read_summary_rows(session, [event_id])
                self.assertEqual(len(rows), 1)
                self.assertEqual(float(rows[0].price), 80.0)
        finally:
            engine.dispose()
            directory.cleanup()

    def test_dirty_revision_prevents_old_refresh_from_clearing_new_work(self):
        directory, engine, Session = self._database()
        try:
            event_date = datetime(2026, 9, 10, 23, tzinfo=timezone.utc)
            with Session() as session:
                event = self._event(event_date)
                iteration = Iteration(
                    event=event,
                    captured_at=captured_datetime_for_storage(
                        event_date - timedelta(hours=4)
                    ),
                )
                iteration.tickets = [
                    Ticket(section="INFIELD BOX 119", price=80, ticketsPerSection=1)
                ]
                session.add(event)
                session.flush()
                first = refresh_event_summary(
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
                self.assertEqual(
                    dirty_venues(session),
                    [("Fenway Park", first.venue_revision)],
                )

                second = refresh_event_summary(
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
                self.assertGreater(second.venue_revision, first.venue_revision)
                self.assertFalse(
                    mark_venue_clean(session, event.Place, first.venue_revision)
                )
                self.assertTrue(
                    mark_venue_clean(session, event.Place, second.venue_revision)
                )
                session.commit()
                self.assertEqual(dirty_venue_count(session), 0)
                self.assertEqual(
                    venue_revision(session, event.Place), second.venue_revision
                )
        finally:
            engine.dispose()
            directory.cleanup()


if __name__ == "__main__":
    unittest.main()
