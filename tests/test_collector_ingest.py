import os
import copy
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from collector import EventSnapshot, SectionSnapshot, snapshot_to_payload

graph_builder_stub = types.ModuleType("graph_builder")
graph_builder_stub.GraphBuilder = object
graph_builder_stub.ConcertGraphBuilder = object
sys.modules.setdefault("graph_builder", graph_builder_stub)

from Flask_App.flask_app import app
from models import Base, Iteration, Ticket


class CollectorIngestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "test.db"
        self.engine = create_engine(f"sqlite:///{self.database}")
        Base.metadata.create_all(self.engine)
        os.environ["DATABASE_PATH"] = str(self.database)
        os.environ["COLLECTOR_INGEST_TOKEN"] = "test-ingest-token"
        app.config.update(TESTING=True)
        self.client = app.test_client()

        captured_at = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        event_date = captured_at + timedelta(hours=24)
        snapshot = EventSnapshot(
            source_id="5967811",
            title="Test Team at Washington Nationals",
            venue="Nationals Park",
            sections=tuple(
                SectionSnapshot(
                    section=f"Section {index}",
                    price=20 + index,
                    listing_count=2,
                    row="A",
                    quantity="2",
                    displayed_price=str(20 + index),
                    alternate_price=str(30 + index),
                )
                for index in range(101, 111)
            ),
        )
        self.payload = snapshot_to_payload(
            "https://www.vividseats.com/test-tickets-7-25-2026--sports-mlb-baseball/production/5967811",
            event_date,
            captured_at,
            snapshot,
        )

    def tearDown(self):
        os.environ.pop("DATABASE_PATH", None)
        os.environ.pop("COLLECTOR_INGEST_TOKEN", None)
        self.engine.dispose()
        self.temporary.cleanup()

    def test_requires_collector_bearer_token(self):
        response = self.client.post("/api/collector/snapshot", json=self.payload)
        self.assertEqual(response.status_code, 401)

    @patch("Flask_App.flask_app.write_capture_audit")
    @patch("Flask_App.flask_app.create_daily_backup")
    def test_stores_one_iteration_and_deduplicates_its_capture_slot(self, backup, audit):
        headers = {"Authorization": "Bearer test-ingest-token"}
        first = self.client.post(
            "/api/collector/snapshot",
            json=self.payload,
            headers=headers,
        )
        second = self.client.post(
            "/api/collector/snapshot",
            json=self.payload,
            headers=headers,
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.get_json()["status"], "stored")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()["status"], "duplicate")
        Session = sessionmaker(bind=self.engine)
        with Session() as session:
            self.assertEqual(session.query(Iteration).count(), 1)
            self.assertEqual(session.query(Ticket).count(), 10)
        backup.assert_called_once()
        audit.assert_called_once()

    @patch("Flask_App.flask_app.write_capture_audit")
    @patch("Flask_App.flask_app.create_daily_backup")
    def test_accepts_authenticated_snapshot_replayed_after_temporary_outage(
        self,
        backup,
        audit,
    ):
        payload = copy.deepcopy(self.payload)
        captured_at = datetime.now(timezone.utc) - timedelta(hours=12)
        event_date = captured_at + timedelta(hours=24)
        payload["captured_at"] = captured_at.isoformat()
        payload["event_date"] = event_date.isoformat()

        response = self.client.post(
            "/api/collector/snapshot",
            json=payload,
            headers={"Authorization": "Bearer test-ingest-token"},
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["status"], "stored")
        backup.assert_called_once()
        audit.assert_called_once()

    @patch("Flask_App.flask_app.write_capture_audit")
    @patch("Flask_App.flask_app.create_daily_backup")
    def test_rejects_snapshot_outside_the_seventy_two_hour_window(
        self,
        backup,
        audit,
    ):
        payload = copy.deepcopy(self.payload)
        captured_at = datetime.now(timezone.utc)
        event_date = captured_at + timedelta(hours=73)
        payload["captured_at"] = captured_at.isoformat()
        payload["event_date"] = event_date.isoformat()

        response = self.client.post(
            "/api/collector/snapshot",
            json=payload,
            headers={"Authorization": "Bearer test-ingest-token"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("72-hour", response.get_json()["error"])
        backup.assert_not_called()
        audit.assert_not_called()

    @patch("Flask_App.flask_app.write_capture_audit")
    @patch("Flask_App.flask_app.create_daily_backup")
    def test_rejects_snapshot_with_source_id_mismatch(self, backup, audit):
        payload = copy.deepcopy(self.payload)
        payload["source_id"] = "9999999"

        response = self.client.post(
            "/api/collector/snapshot",
            json=payload,
            headers={"Authorization": "Bearer test-ingest-token"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("source ID", response.get_json()["error"])
        backup.assert_not_called()
        audit.assert_not_called()

    @patch("Flask_App.flask_app.write_capture_audit")
    @patch("Flask_App.flask_app.create_daily_backup")
    def test_rejects_snapshot_that_is_too_old_to_replay(self, backup, audit):
        payload = copy.deepcopy(self.payload)
        captured_at = datetime.now(timezone.utc) - timedelta(days=8)
        event_date = datetime.now(timezone.utc) + timedelta(hours=24)
        payload["captured_at"] = captured_at.isoformat()
        payload["event_date"] = event_date.isoformat()

        response = self.client.post(
            "/api/collector/snapshot",
            json=payload,
            headers={"Authorization": "Bearer test-ingest-token"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("seven-day replay window", response.get_json()["error"])
        backup.assert_not_called()
        audit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
