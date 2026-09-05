from __future__ import annotations

import base64
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from Flask_App import database_config


class DatabaseConfigTests(unittest.TestCase):
    def tearDown(self):
        database_config.clear_mysql_engine_cache()

    def test_mysql_url_uses_separate_sport_database_and_decodes_password(self):
        env = {
            "MYSQL_HOST": "example.mysql.local",
            "MYSQL_USERNAME": "ticket-user",
            "MYSQL_PASSWORD_B64": base64.b64encode(b"p@ss:/ word").decode("ascii"),
            "MYSQL_MLB_DATABASE": "ticket$mlb",
            "MYSQL_NFL_DATABASE": "ticket$nfl",
            "MYSQL_NHL_DATABASE": "ticket$nhl",
        }
        with patch.dict(os.environ, env, clear=False):
            url = database_config.mysql_url("nfl")
        self.assertEqual(url.drivername, "mysql+pymysql")
        self.assertEqual(url.host, "example.mysql.local")
        self.assertEqual(url.username, "ticket-user")
        self.assertEqual(url.password, "p@ss:/ word")
        self.assertEqual(url.database, "ticket$nfl")
        self.assertEqual(url.query["charset"], "utf8mb4")

    def test_explicit_sqlite_path_overrides_mysql_backend(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test.db"
            with patch.dict(
                os.environ,
                {"TICKETSIGNAL_DATABASE_BACKEND": "mysql"},
                clear=False,
            ):
                engine = database_config.create_ticket_engine(
                    "mlb",
                    sqlite_path=path,
                    force_sqlite=True,
                )
            try:
                self.assertEqual(engine.dialect.name, "sqlite")
            finally:
                engine.dispose()

    def test_invalid_backend_is_rejected(self):
        with patch.dict(
            os.environ,
            {"TICKETSIGNAL_DATABASE_BACKEND": "mongo"},
            clear=False,
        ):
            with self.assertRaises(RuntimeError):
                database_config.configured_backend()

    def test_migration_pause_marker_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "pause"
            with patch.object(database_config, "MIGRATION_PAUSE_PATH", marker):
                self.assertFalse(database_config.migration_pause_active())
                database_config.begin_migration_pause()
                self.assertTrue(database_config.migration_pause_active())
                database_config.end_migration_pause()
                self.assertFalse(database_config.migration_pause_active())


if __name__ == "__main__":
    unittest.main()
