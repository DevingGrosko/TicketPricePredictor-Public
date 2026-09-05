#!/usr/bin/env python3
"""Exercise TicketSignal's current schemas against a disposable MariaDB server."""

from __future__ import annotations

from datetime import datetime, timezone
import os

from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.engine import make_url


def main() -> int:
    admin_url = os.environ.get(
        "MYSQL_TEST_ADMIN_URL",
        "mysql+pymysql://root:root@127.0.0.1:3306/mysql?charset=utf8mb4",
    )
    parsed = make_url(admin_url)
    names = {
        "mlb": "ticketsignal_ci_mlb",
        "nfl": "ticketsignal_ci_nfl",
        "nhl": "ticketsignal_ci_nhl",
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
        }
    )
    os.environ.pop("MYSQL_PASSWORD_B64", None)

    from Flask_App.database_config import (
        clear_mysql_engine_cache,
        create_mysql_engine,
    )
    from Flask_App.mysql_cutover import RAW_TABLE_ORDER, _mysql_metadata

    clear_mysql_engine_cache()
    try:
        for sport in ("mlb", "nfl", "nhl"):
            engine = create_mysql_engine(sport)
            metadata = _mysql_metadata(sport)
            metadata.create_all(engine, checkfirst=True)

            present = set(inspect(engine).get_table_names())
            required = {
                *RAW_TABLE_ORDER[sport],
                "section_bucket_summary",
                "section_summary_state",
                "analytics_dirty_venue",
            }
            missing = required - present
            if missing:
                raise RuntimeError(
                    f"{sport}: MySQL schema is missing tables: {sorted(missing)}"
                )

            dirty = metadata.tables["analytics_dirty_venue"]
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            with engine.begin() as connection:
                connection.execute(
                    dirty.insert().values(
                        venue=f"CI {sport.upper()} venue",
                        revision=1,
                        dirty=True,
                        updated_at=now,
                    )
                )
                count = int(
                    connection.execute(
                        select(func.count()).select_from(dirty)
                    ).scalar_one()
                )
                if count != 1:
                    raise RuntimeError(
                        f"{sport}: expected one summary-state test row, got {count}"
                    )
                connection.execute(dirty.delete())
    finally:
        clear_mysql_engine_cache()
        with admin.begin() as connection:
            for database in names.values():
                connection.exec_driver_sql(f"DROP DATABASE IF EXISTS `{database}`")
        admin.dispose()

    print("MySQL schema and connection checks passed for MLB, NFL, and NHL.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
