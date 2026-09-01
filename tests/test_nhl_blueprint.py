from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from flask import Flask

from collector import EventSnapshot, SectionSnapshot
from models import captured_datetime_for_storage, event_datetime_for_storage
from Flask_App.nhl_blueprint import (
    CreateNHLModel,
    NHLEvent,
    NHLIteration,
    NHLTicket,
    _SCHEMA_READY,
    compact_completed_nhl_games,
    nhl_blueprint,
    nhl_home,
    normalize_nhl_schedule_metadata,
    select_nhl_compaction_iteration_ids,
    store_nhl_snapshot,
)


EASTERN = ZoneInfo("America/New_York")


class NHLBlueprintTests(unittest.TestCase):
    def _snapshot(self, source_id: str, title: str = "Boston Bruins at Toronto Maple Leafs"):
        return EventSnapshot(
            source_id=source_id,
            title=title,
            venue="Scotiabank Arena",
            sections=tuple(
                SectionSnapshot(
                    section=f"Section {100 + index}",
                    price=90 + index,
                    listing_count=2,
                    row="A",
                    quantity="2",
                    displayed_price=str(90 + index),
                    alternate_price=str(110 + index),
                )
                for index in range(10)
            ),
        )

    def test_metadata_accepts_canada_and_rejects_overseas_venues(self):
        metadata = normalize_nhl_schedule_metadata(
            {
                "schedule_id": "2026020001",
                "away_team": "Boston Bruins",
                "home_team": "Toronto Maple Leafs",
                "canonical_venue": "Scotiabank Arena",
                "venue_timezone": "America/Toronto",
                "country": "Canada",
                "game_type": 2,
                "season": 20262027,
            },
            title="Boston Bruins at Toronto Maple Leafs",
            provider_venue="Scotiabank Arena",
            currency="USD",
        )
        self.assertEqual(metadata["country"], "Canada")
        self.assertEqual(metadata["currency"], "USD")

        with self.assertRaisesRegex(ValueError, "U.S. and Canadian"):
            normalize_nhl_schedule_metadata(
                {
                    "away_team": "Boston Bruins",
                    "home_team": "Toronto Maple Leafs",
                    "country": "Finland",
                },
                title="Boston Bruins at Toronto Maple Leafs",
                provider_venue="Helsinki Ice Hall",
                currency="USD",
            )

    @patch("Flask_App.nhl_blueprint.render_template")
    def test_store_and_home_context_keep_upcoming_and_completed_games_together(self, render):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "NHL-collection.db"
            old_path = os.environ.get("NHL_DATABASE_PATH")
            os.environ["NHL_DATABASE_PATH"] = str(db_path)
            _SCHEMA_READY.discard(str(db_path.resolve()))
            try:
                now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
                store_nhl_snapshot(
                    "https://www.vividseats.com/game/production/9300001",
                    now + timedelta(days=2),
                    self._snapshot("9300001"),
                    now,
                    db_path=db_path,
                    schedule_metadata={
                        "schedule_id": "2026020001",
                        "away_team": "Boston Bruins",
                        "home_team": "Toronto Maple Leafs",
                        "canonical_venue": "Scotiabank Arena",
                        "venue_timezone": "America/Toronto",
                        "country": "Canada",
                        "game_type": 2,
                        "season": 20262027,
                    },
                    currency="USD",
                )
                store_nhl_snapshot(
                    "https://www.vividseats.com/game/production/9300002",
                    now - timedelta(days=1),
                    self._snapshot(
                        "9300002",
                        "New York Rangers at Boston Bruins",
                    ),
                    now - timedelta(days=2),
                    db_path=db_path,
                    schedule_metadata={
                        "schedule_id": "2026020002",
                        "away_team": "New York Rangers",
                        "home_team": "Boston Bruins",
                        "canonical_venue": "TD Garden",
                        "venue_timezone": "America/New_York",
                        "country": "USA",
                        "game_type": 1,
                        "season": 20262027,
                    },
                    currency="USD",
                )

                render.return_value = "ok"
                app = Flask(__name__)
                app.register_blueprint(nhl_blueprint)
                with app.test_request_context("/nhl"):
                    self.assertEqual(nhl_home(), "ok")
                context = render.call_args.kwargs
                self.assertEqual(context["game_count"], 2)
                self.assertEqual(context["upcoming_count"], 1)
                self.assertEqual(context["completed_count"], 1)
                self.assertIn("Toronto Maple Leafs", context["games_dict"])
                self.assertIn("Boston Bruins", context["games_dict"])
                self.assertTrue(
                    context["games_dict"]["Toronto Maple Leafs"][0]["label"]
                    .startswith("Upcoming · Regular season")
                )
                self.assertTrue(
                    context["games_dict"]["Boston Bruins"][0]["label"]
                    .startswith("Completed · Preseason")
                )
            finally:
                if old_path is None:
                    os.environ.pop("NHL_DATABASE_PATH", None)
                else:
                    os.environ["NHL_DATABASE_PATH"] = old_path
                _SCHEMA_READY.discard(str(db_path.resolve()))

    def test_compaction_keeps_final_day_hourly_and_representative_earlier_points(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "NHL-collection.db"
            _SCHEMA_READY.discard(str(db_path.resolve()))
            model = CreateNHLModel(db_path)
            event_time = datetime(2026, 10, 20, 19, tzinfo=EASTERN)
            try:
                with model.getSession()() as session:
                    event = NHLEvent(
                        source_id="compaction-game",
                        title="Boston Bruins at Toronto Maple Leafs",
                        event_date=event_datetime_for_storage(event_time),
                        sections=["Section 100"],
                        source_url="https://www.vividseats.com/game/production/9300003",
                        venue="Scotiabank Arena",
                        away_team="Boston Bruins",
                        home_team="Toronto Maple Leafs",
                        canonical_venue="Scotiabank Arena",
                        venue_timezone="America/Toronto",
                        country="Canada",
                        game_type=2,
                        season=20262027,
                        currency="USD",
                    )
                    session.add(event)
                    session.flush()
                    lead_hours = list(range(1, 73)) + list(range(78, 169, 6))
                    for lead in lead_hours:
                        captured = event_time.astimezone(timezone.utc) - timedelta(hours=lead)
                        iteration = NHLIteration(
                            event=event,
                            captured_at=captured_datetime_for_storage(captured),
                        )
                        iteration.tickets.append(
                            NHLTicket(
                                section="Section 100",
                                price=100 + lead,
                                listing_count=1,
                            )
                        )
                        session.add(iteration)
                    session.commit()
                    iterations = sorted(event.iterations, key=lambda item: item.captured_at)
                    self.assertEqual(len(iterations), 88)
                    self.assertEqual(
                        len(select_nhl_compaction_iteration_ids(event.event_date, iterations)),
                        56,
                    )
            finally:
                model.engine.dispose()

            report = compact_completed_nhl_games(
                now=event_time.astimezone(timezone.utc) + timedelta(days=15),
                db_path=db_path,
            )
            self.assertEqual(report["games_compacted"], 1)
            self.assertEqual(report["iterations_before"], 88)
            self.assertEqual(report["iterations_retained"], 56)
            self.assertEqual(report["iterations_deleted"], 32)

            _SCHEMA_READY.discard(str(db_path.resolve()))
            model = CreateNHLModel(db_path)
            try:
                with model.getSession()() as session:
                    event = session.query(NHLEvent).one()
                    self.assertIsNotNone(event.compacted_at)
                    self.assertEqual(event.original_iteration_count, 88)
                    self.assertEqual(event.retained_iteration_count, 56)
                    self.assertEqual(session.query(NHLIteration).count(), 56)
                    self.assertEqual(session.query(NHLTicket).count(), 56)
            finally:
                model.engine.dispose()
                _SCHEMA_READY.discard(str(db_path.resolve()))


if __name__ == "__main__":
    unittest.main()
