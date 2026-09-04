#!/usr/bin/env python3
"""Start the Flask application against disposable MySQL databases."""

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
    admin = create_engine(admin_url, pool_pre_ping=True)
    database_names = {
        "BASEBALL_DATABASE_URL": "ticketsignal_web_mlb",
        "NFL_DATABASE_URL": "ticketsignal_web_nfl",
        "NHL_DATABASE_URL": "ticketsignal_web_nhl",
    }
    with admin.begin() as connection:
        for name in database_names.values():
            connection.exec_driver_sql(f"DROP DATABASE IF EXISTS `{name}`")
            connection.exec_driver_sql(
                f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )

    parsed = make_url(admin_url)
    for variable, name in database_names.items():
        os.environ[variable] = parsed.set(database=name).render_as_string(
            hide_password=False
        )

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        os.environ["DATABASE_PATH"] = str(root / "legacy-baseball.db")
        os.environ["NFL_DATABASE_PATH"] = str(root / "legacy-nfl.db")
        os.environ["NHL_DATABASE_PATH"] = str(root / "legacy-nhl.db")
        os.environ["CONCERT_DATABASE_PATH"] = str(root / "concerts.db")
        os.environ["FLASK_SECRET_KEY"] = "mysql-web-smoke"
        os.environ["COLLECTOR_INGEST_TOKEN"] = "mysql-web-smoke-token"

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

    admin.dispose()
    print("MySQL-backed Flask route smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
