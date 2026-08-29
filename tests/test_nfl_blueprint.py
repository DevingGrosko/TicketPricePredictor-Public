from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from flask import Flask

from collector import EventSnapshot, SectionSnapshot, snapshot_to_payload
from Flask_App.nfl_blueprint import (
    NFLBase,
    NFLEvent,
    NFLIteration,
    NFLTicket,
    CreateNFLModel,
    nfl_blueprint,
    store_nfl_snapshot,
)


class NFLDatabaseIsolationTests(unittest.TestCase):
    def _snapshot(self):
        return EventSnapshot(
            source_id="1234567",
            title="Dallas Cowboys at New York Giants",
            venue="MetLife Stadium",
            sections=tuple(
                SectionSnapshot(
                    section=f"Section {index}",
                    price=75 + index,
                    listing_count=2,
                    row="A",
                    quantity="2",
                    displayed_price=str(75 + index),
                    alternate_price="",
                )
                for index in range(10)
            ),
        )

    def test_hourly_storage_is_separate_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "nfl.db"
            snapshot = self._snapshot()
            event_date = datetime(
                2026, 9, 13, 13, tzinfo=ZoneInfo("America/New_York")
            )
            first = datetime(2026, 9, 6, 17, 5, tzinfo=timezone.utc)
            second = datetime(2026, 9, 6, 17, 55, tzinfo=timezone.utc)
            url = "https://www.vividseats.com/game/production/1234567"

            event_id, iteration_id, stored = store_nfl_snapshot(
                url, event_date, snapshot, first, db_path=db_path
            )
            duplicate_event, duplicate_iteration, duplicate = store_nfl_snapshot(
                url, event_date, snapshot, second, db_path=db_path
            )
            self.assertTrue(stored)
            self.assertFalse(duplicate)
            self.assertEqual(event_id, duplicate_event)
            self.assertEqual(iteration_id, duplicate_iteration)

            model = CreateNFLModel(db_path)
            try:
                with model.getSession()() as session:
                    self.assertEqual(session.query(NFLEvent).count(), 1)
                    self.assertEqual(session.query(NFLIteration).count(), 1)
                    self.assertEqual(session.query(NFLTicket).count(), 10)
            finally:
                model.engine.dispose()

    @patch("Flask_App.nfl_blueprint.write_nfl_audit")
    @patch("Flask_App.nfl_blueprint.create_nfl_daily_backup")
    def test_api_stores_nfl_and_rejects_wrong_event_type(self, backup, audit):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "nfl.db"
            old_db = os.environ.get("NFL_DATABASE_PATH")
            old_token = os.environ.get("COLLECTOR_INGEST_TOKEN")
            os.environ["NFL_DATABASE_PATH"] = str(db_path)
            os.environ["COLLECTOR_INGEST_TOKEN"] = "test-token"
            try:
                app = Flask(__name__, template_folder="../Flask_App/templates")
                app.register_blueprint(nfl_blueprint)
                app.config.update(TESTING=True)
                client = app.test_client()

                captured_at = datetime.now(timezone.utc).replace(
                    minute=0, second=0, microsecond=0
                )
                event_date = captured_at + timedelta(days=2)
                snapshot = self._snapshot()
                payload = snapshot_to_payload(
                    "https://www.vividseats.com/game/production/1234567",
                    event_date,
                    captured_at,
                    snapshot,
                )
                payload["event_type"] = "nfl"
                headers = {"Authorization": "Bearer test-token"}

                first = client.post("/api/nfl/snapshot", json=payload, headers=headers)
                second = client.post("/api/nfl/snapshot", json=payload, headers=headers)
                self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
                self.assertEqual(second.status_code, 200, second.get_data(as_text=True))
                self.assertEqual(first.get_json()["status"], "stored")
                self.assertEqual(second.get_json()["status"], "duplicate")

                wrong = dict(payload)
                wrong["event_type"] = "concert"
                rejected = client.post("/api/nfl/snapshot", json=wrong, headers=headers)
                self.assertEqual(rejected.status_code, 400)
                backup.assert_called()
                audit.assert_called_once()
            finally:
                if old_db is None:
                    os.environ.pop("NFL_DATABASE_PATH", None)
                else:
                    os.environ["NFL_DATABASE_PATH"] = old_db
                if old_token is None:
                    os.environ.pop("COLLECTOR_INGEST_TOKEN", None)
                else:
                    os.environ["COLLECTOR_INGEST_TOKEN"] = old_token

    @patch("Flask_App.nfl_blueprint.write_nfl_audit")
    @patch("Flask_App.nfl_blueprint.create_nfl_daily_backup")
    def test_api_accepts_thirty_days_and_rejects_beyond_window(self, backup, audit):
        with tempfile.TemporaryDirectory() as directory:
            old_db = os.environ.get("NFL_DATABASE_PATH")
            old_token = os.environ.get("COLLECTOR_INGEST_TOKEN")
            os.environ["NFL_DATABASE_PATH"] = str(Path(directory) / "nfl.db")
            os.environ["COLLECTOR_INGEST_TOKEN"] = "test-token"
            try:
                app = Flask(__name__, template_folder="../Flask_App/templates")
                app.register_blueprint(nfl_blueprint)
                app.config.update(TESTING=True)
                client = app.test_client()
                captured_at = datetime.now(timezone.utc).replace(
                    minute=0, second=0, microsecond=0
                )
                headers = {"Authorization": "Bearer test-token"}
                snapshot = self._snapshot()

                boundary_payload = snapshot_to_payload(
                    "https://www.vividseats.com/game/production/1234567",
                    captured_at + timedelta(hours=720),
                    captured_at,
                    snapshot,
                )
                boundary_payload["event_type"] = "nfl"
                accepted = client.post(
                    "/api/nfl/snapshot", json=boundary_payload, headers=headers
                )
                self.assertEqual(accepted.status_code, 201, accepted.get_data(as_text=True))

                outside_payload = snapshot_to_payload(
                    "https://www.vividseats.com/game/production/7654321",
                    captured_at + timedelta(hours=721),
                    captured_at,
                    EventSnapshot(
                        source_id="7654321",
                        title=snapshot.title,
                        venue=snapshot.venue,
                        sections=snapshot.sections,
                    ),
                )
                outside_payload["event_type"] = "nfl"
                rejected = client.post(
                    "/api/nfl/snapshot", json=outside_payload, headers=headers
                )
                self.assertEqual(rejected.status_code, 400)
                self.assertIn("30-day", rejected.get_json()["error"])
                backup.assert_called_once()
                audit.assert_called_once()
            finally:
                if old_db is None:
                    os.environ.pop("NFL_DATABASE_PATH", None)
                else:
                    os.environ["NFL_DATABASE_PATH"] = old_db
                if old_token is None:
                    os.environ.pop("COLLECTOR_INGEST_TOKEN", None)
                else:
                    os.environ["COLLECTOR_INGEST_TOKEN"] = old_token


if __name__ == "__main__":
    unittest.main()
