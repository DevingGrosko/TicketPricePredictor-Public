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
    find_nfl_game,
    nfl_blueprint,
    nfl_home,
    nfl_home_team,
    nfl_map,
    nfl_matchup_teams,
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


class NFLTeamGroupingTests(unittest.TestCase):
    def test_provider_titles_resolve_to_distinct_home_teams(self):
        self.assertEqual(
            nfl_matchup_teams("Dallas Cowboys at New York Giants"),
            ("Dallas Cowboys", "New York Giants"),
        )
        self.assertEqual(
            nfl_home_team("Dallas Cowboys at New York Giants"),
            "New York Giants",
        )
        self.assertEqual(
            nfl_home_team("Buffalo Bills at New York Jets"),
            "New York Jets",
        )

    @patch("Flask_App.nfl_blueprint.render_template")
    def test_shared_stadium_games_are_split_into_home_team_groups(self, render):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "nfl.db"
            old_db = os.environ.get("NFL_DATABASE_PATH")
            os.environ["NFL_DATABASE_PATH"] = str(db_path)
            try:
                sections = tuple(
                    SectionSnapshot(
                        section=f"Section {index}",
                        price=100 + index,
                        listing_count=2,
                        row="A",
                        quantity="2",
                        displayed_price=str(100 + index),
                        alternate_price="",
                    )
                    for index in range(10)
                )
                captured_at = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
                giants_id, _, _ = store_nfl_snapshot(
                    "https://www.vividseats.com/game/production/1111111",
                    captured_at + timedelta(days=10),
                    EventSnapshot(
                        source_id="1111111",
                        title="Dallas Cowboys at New York Giants",
                        venue="MetLife Stadium",
                        sections=sections,
                    ),
                    captured_at,
                    db_path=db_path,
                )
                store_nfl_snapshot(
                    "https://www.vividseats.com/game/production/2222222",
                    captured_at + timedelta(days=11),
                    EventSnapshot(
                        source_id="2222222",
                        title="Buffalo Bills at New York Jets",
                        venue="MetLife Stadium",
                        sections=sections,
                    ),
                    captured_at,
                    db_path=db_path,
                )

                app = Flask(__name__)
                app.register_blueprint(nfl_blueprint)
                render.return_value = "ok"
                with app.test_request_context("/nfl"):
                    response = nfl_home()

                self.assertEqual(response, "ok")
                context = render.call_args.kwargs
                self.assertEqual(
                    list(context["games_dict"]),
                    ["New York Giants", "New York Jets"],
                )
                self.assertEqual(context["team_count"], 2)
                self.assertEqual(context["game_count"], 2)
                self.assertIsNotNone(find_nfl_game("New York Giants", str(giants_id)))
                self.assertIsNone(find_nfl_game("New York Jets", str(giants_id)))
                # Existing venue-based bookmarks remain valid.
                self.assertIsNotNone(find_nfl_game("MetLife Stadium", str(giants_id)))
            finally:
                if old_db is None:
                    os.environ.pop("NFL_DATABASE_PATH", None)
                else:
                    os.environ["NFL_DATABASE_PATH"] = old_db


class NFLStadiumMapTests(unittest.TestCase):
    @patch("Flask_App.nfl_blueprint.render_template")
    def test_map_route_uses_latest_section_snapshot(self, render):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "nfl.db"
            old_db = os.environ.get("NFL_DATABASE_PATH")
            os.environ["NFL_DATABASE_PATH"] = str(db_path)
            try:
                def snapshot(source_id, base_price):
                    return EventSnapshot(
                        source_id=source_id,
                        title="Dallas Cowboys at New York Giants",
                        venue="MetLife Stadium",
                        sections=tuple(
                            SectionSnapshot(
                                section=f"Section {index}",
                                price=base_price + index,
                                listing_count=index + 1,
                                row="A",
                                quantity="2",
                                displayed_price=str(base_price + index),
                                alternate_price="",
                            )
                            for index in range(10)
                        ),
                    )

                first_capture = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
                game_id, _, _ = store_nfl_snapshot(
                    "https://www.vividseats.com/game/production/3333333",
                    first_capture + timedelta(days=10),
                    snapshot("3333333", 100),
                    first_capture,
                    db_path=db_path,
                )
                store_nfl_snapshot(
                    "https://www.vividseats.com/game/production/3333333",
                    first_capture + timedelta(days=10),
                    snapshot("3333333", 200),
                    first_capture + timedelta(hours=1),
                    db_path=db_path,
                )

                app = Flask(__name__)
                app.register_blueprint(nfl_blueprint)
                render.return_value = "map"
                path = (
                    f"/nfl/map?team=New%20York%20Giants&game={game_id}"
                    "&section=Section%201"
                )
                with app.test_request_context(path):
                    response = nfl_map()

                self.assertEqual(response, "map")
                self.assertEqual(render.call_args.args[0], "nfl_map.html")
                context = render.call_args.kwargs
                self.assertEqual(context["team"], "New York Giants")
                self.assertEqual(context["venue"], "MetLife Stadium")
                self.assertEqual(context["section_count"], 10)
                self.assertEqual(context["priced_section_count"], 10)
                by_name = {
                    item["name"]: item for item in context["map_data"]["sections"]
                }
                self.assertEqual(by_name["Section 1"]["price"], 201)
                self.assertEqual(by_name["Section 1"]["listing_count"], 2)
                self.assertEqual(
                    context["map_data"]["selected_section"], "Section 1"
                )
            finally:
                if old_db is None:
                    os.environ.pop("NFL_DATABASE_PATH", None)
                else:
                    os.environ["NFL_DATABASE_PATH"] = old_db


if __name__ == "__main__":
    unittest.main()
