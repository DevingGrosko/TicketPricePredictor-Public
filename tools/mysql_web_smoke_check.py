#!/usr/bin/env python3
"""Start and exercise TicketSignal with all active sport storage on MySQL."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url


def main() -> int:
    admin_url = os.environ.get(
        "MYSQL_TEST_ADMIN_URL",
        "mysql+pymysql://root:root@127.0.0.1:3306/mysql?charset=utf8mb4",
    )
    parsed = make_url(admin_url)
    names = {
        "mlb": "ticketsignal_web_mlb",
        "nfl": "ticketsignal_web_nfl",
        "nhl": "ticketsignal_web_nhl",
    }

    admin = create_engine(admin_url, pool_pre_ping=True)
    with admin.begin() as connection:
        for database in names.values():
            connection.exec_driver_sql(f"DROP DATABASE IF EXISTS `{database}`")
            connection.exec_driver_sql(
                f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 "
                "COLLATE utf8mb4_unicode_ci"
            )

    os.environ.update(
        {
            "TICKETSIGNAL_DATABASE_BACKEND": "mysql",
            "MYSQL_HOST": parsed.host or "127.0.0.1",
            "MYSQL_USERNAME": parsed.username or "root",
            "MYSQL_PASSWORD": parsed.password or "",
            "MYSQL_MLB_DATABASE": names["mlb"],
            "MYSQL_NFL_DATABASE": names["nfl"],
            "MYSQL_NHL_DATABASE": names["nhl"],
            "FLASK_SECRET_KEY": "mysql-web-smoke",
            "COLLECTOR_INGEST_TOKEN": "mysql-web-smoke-token",
        }
    )
    os.environ.pop("MYSQL_PASSWORD_B64", None)

    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.environ["DATABASE_PATH"] = str(root / "legacy-baseball.db")
            os.environ["NFL_DATABASE_PATH"] = str(root / "legacy-nfl.db")
            os.environ["NHL_DATABASE_PATH"] = str(root / "legacy-nhl.db")
            os.environ["CONCERT_DATABASE_PATH"] = str(root / "concerts.db")

            from Flask_App.flask_app import app

            app.config.update(TESTING=True)
            client = app.test_client()
            for path in (
                "/",
                "/baseball",
                "/baseball/stadium",
                "/nfl",
                "/nfl/stadium",
                "/nhl",
                "/nhl/stadium",
                "/concerts",
            ):
                response = client.get(path)
                if response.status_code != 200:
                    raise RuntimeError(
                        f"MySQL-backed route {path} returned {response.status_code}: "
                        f"{response.get_data(as_text=True)[:500]}"
                    )

            status = client.get(
                "/api/database/status",
                headers={"Authorization": "Bearer mysql-web-smoke-token"},
            )
            if status.status_code != 200:
                raise RuntimeError(
                    "Database status route returned "
                    f"{status.status_code}: {status.get_data(as_text=True)[:500]}"
                )
            payload = status.get_json() or {}
            if payload.get("backend") != "mysql":
                raise RuntimeError(f"Web app did not report MySQL: {payload}")
            databases = payload.get("databases") or {}
            for sport in ("mlb", "nfl", "nhl"):
                details = databases.get(sport) or {}
                if not details.get("connected") or details.get("dialect") != "mysql":
                    raise RuntimeError(
                        f"Web app did not verify {sport.upper()} MySQL storage: {payload}"
                    )

            for path in (
                "/api/collector/snapshot",
                "/api/nfl/snapshot",
                "/api/nhl/snapshot",
                "/api/analytics/backfill",
            ):
                response = client.post(path, json={})
                if response.status_code != 401:
                    raise RuntimeError(
                        f"Protected route {path} returned {response.status_code}, expected 401"
                    )
    finally:
        try:
            from Flask_App.database_config import clear_mysql_engine_cache

            clear_mysql_engine_cache()
        except Exception:
            pass
        with admin.begin() as connection:
            for database in names.values():
                connection.exec_driver_sql(f"DROP DATABASE IF EXISTS `{database}`")
        admin.dispose()

    print("MySQL-backed Flask route smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
