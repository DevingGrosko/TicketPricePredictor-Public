from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME

from Flask_App import database_config
from Flask_App.mysql_cutover import _mysql_metadata


class MySQLCutoverSupportTests(unittest.TestCase):
    def tearDown(self):
        database_config.clear_mysql_engine_cache()

    def test_mysql_metadata_has_bounded_strings_and_microseconds(self):
        for sport in ("mlb", "nfl", "nhl"):
            metadata = _mysql_metadata(sport)
            for table in metadata.tables.values():
                for column in table.columns:
                    if isinstance(column.type, String):
                        self.assertIsNotNone(column.type.length)
                    if isinstance(column.type, DateTime):
                        self.assertIsInstance(column.type, MYSQL_DATETIME)
                        self.assertEqual(column.type.fsp, 6)


    def test_dispose_helper_keeps_mysql_pool_but_disposes_sqlite(self):
        class Dialect:
            def __init__(self, name):
                self.name = name

        class Engine:
            def __init__(self, name):
                self.dialect = Dialect(name)
                self.disposed = False

            def dispose(self):
                self.disposed = True

        sqlite = Engine("sqlite")
        mysql = Engine("mysql")
        database_config.dispose_ticket_engine(sqlite)
        database_config.dispose_ticket_engine(mysql)
        self.assertTrue(sqlite.disposed)
        self.assertFalse(mysql.disposed)

    def test_pause_marker_blocks_all_ticket_ingestion_routes(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "paused"
            marker.write_text("paused", encoding="utf-8")
            with patch.object(database_config, "MIGRATION_PAUSE_PATH", marker), patch.dict(
                os.environ,
                {
                    "TICKETSIGNAL_DATABASE_BACKEND": "sqlite",
                    "COLLECTOR_INGEST_TOKEN": "test-token",
                },
                clear=False,
            ):
                from Flask_App.flask_app import app

                client = app.test_client()
                headers = {"Authorization": "Bearer test-token"}
                for path in (
                    "/api/collector/snapshot",
                    "/api/nfl/snapshot",
                    "/api/nhl/snapshot",
                ):
                    response = client.post(path, json={}, headers=headers)
                    self.assertEqual(response.status_code, 503, path)
                    self.assertEqual(response.get_json()["status"], "maintenance")


if __name__ == "__main__":
    unittest.main()
