from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest

from flask import Flask
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
from Flask_App.materialized_analytics import refresh_event_summary
from Flask_App.nfl_stadium_blueprint import nfl_stadium_blueprint
from Flask_App.team_report_materialized import (
    read_mlb_team_report,
    refresh_mlb_team_report,
    stale_mlb_team_report_venues,
)


class MaterializedMLBTeamReportTests(unittest.TestCase):
    @staticmethod
    def _add_game(session, source_id: int, event_date: datetime, shift: int = 0):
        event = Event(
            title="New York Mets at Washington Nationals",
            event_date=event_datetime_for_storage(event_date),
            event_sections=["Section 100", "Section 200"],
            URL=(
                "https://www.vividseats.com/washington-nationals-tickets-"
                f"--sports-mlb-baseball/production/{source_id}"
            ),
            Place="Nationals Park",
        )
        # Supply every MLB report bucket. The final five buckets have three
        # observations each so both price and drop ranking evidence is valid.
        bucket_hours = (66, 54, 42, 30, 18, 9, 3)
        for slot, hours_before in enumerate(bucket_hours):
            repeats = 3 if slot >= 2 else 1
            for repeat in range(repeats):
                iteration = Iteration(
                    event=event,
                    captured_at=captured_datetime_for_storage(
                        event_date
                        - timedelta(hours=hours_before)
                        + timedelta(minutes=repeat)
                    ),
                )
                iteration.tickets = [
                    Ticket(
                        section="Section 100",
                        price=100 + shift - slot * 5,
                        ticketsPerSection=2,
                    ),
                    Ticket(
                        section="Section 200",
                        price=70 + shift + slot * 2,
                        ticketsPerSection=2,
                    ),
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
        return event

    def test_team_payload_persists_and_detects_stale_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "baseball.db"
            engine = create_engine(f"sqlite:///{db_path}")
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine, expire_on_commit=False)
            now = datetime.now(timezone.utc).replace(microsecond=0)
            with Session() as session:
                for index, shift in enumerate((0, 5, -5), start=1):
                    self._add_game(
                        session,
                        9700000 + index,
                        now - timedelta(days=10 - index),
                        shift,
                    )
                session.commit()
            engine.dispose()

            previous = os.environ.get("DATABASE_PATH")
            os.environ["DATABASE_PATH"] = str(db_path)
            try:
                self.assertTrue(refresh_mlb_team_report("Nationals Park"))

                model = CreateModel()
                with model.getSession()() as session:
                    payload = read_mlb_team_report(session, "Nationals Park", now.year)
                    self.assertIsNotNone(payload)
                    self.assertEqual(payload["game_count"], 3)
                    self.assertEqual(len(payload["all_sections"]), 2)
                    self.assertEqual(
                        payload["cheapest_section_keys"][0],
                        next(
                            row["section_key"]
                            for row in payload["all_sections"]
                            if row["name"] == "Section 100"
                        ),
                    )
                    self.assertEqual(
                        payload["biggest_drop_keys"][0],
                        next(
                            row["section_key"]
                            for row in payload["all_sections"]
                            if row["name"] == "Section 100"
                        ),
                    )
                    events = session.query(Event).order_by(Event.id).all()
                    self.assertEqual(stale_mlb_team_report_venues(session, events), [])

                    # A new event-level summary revision makes the persisted
                    # report stale until the team refresh succeeds.
                    event = events[-1]
                    iteration = Iteration(
                        event=event,
                        captured_at=captured_datetime_for_storage(
                            event.event_date - timedelta(hours=2, minutes=10)
                        ),
                    )
                    iteration.tickets = [
                        Ticket(section="Section 100", price=51, ticketsPerSection=2),
                        Ticket(section="Section 200", price=90, ticketsPerSection=2),
                    ]
                    session.add(iteration)
                    session.flush()
                    refresh_event_summary(
                        session,
                        sport_key="mlb",
                        event_id=event.id,
                        event_date=event.event_date,
                        venue=event.Place,
                        iteration_model=Iteration,
                        ticket_model=Ticket,
                        bucket_slots=(6,),
                    )
                    session.commit()
                    self.assertIsNone(
                        read_mlb_team_report(session, "Nationals Park", now.year)
                    )
                    self.assertEqual(
                        stale_mlb_team_report_venues(session, events),
                        ["Nationals Park"],
                    )
                model.engine.dispose()
            finally:
                if previous is None:
                    os.environ.pop("DATABASE_PATH", None)
                else:
                    os.environ["DATABASE_PATH"] = previous

    def test_flask_app_replaces_only_mlb_team_report_view(self):
        # Importing the production app should leave the existing endpoint name
        # and URL intact while swapping its view function.
        from Flask_App.flask_app import app
        from Flask_App.team_report_materialized import render_materialized_mlb_team_report

        self.assertIs(
            app.view_functions["nfl_stadium.mlb_stadium"],
            render_materialized_mlb_team_report,
        )
        self.assertIn("/baseball/stadium", {rule.rule for rule in app.url_map.iter_rules()})
        self.assertIsNot(
            app.view_functions["nfl_stadium.nfl_stadium"],
            render_materialized_mlb_team_report,
        )


if __name__ == "__main__":
    unittest.main()
