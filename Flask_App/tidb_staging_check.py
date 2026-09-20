"""Read-only connectivity check for the three EMPTY TiDB staging schemas.

Run from the repository root with python -m Flask_App.tidb_staging_check.
Only the isolated connection helper is imported, never the Flask application.
No tables are created, no history is imported, and no production endpoint is used.
"""
from __future__ import annotations

import os
from pathlib import Path
import ssl

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from Flask_App.tidb_staging import SCHEMAS, StagingTiDBConfig, create_staging_engine


class CheckFailed(RuntimeError):
    """A fixed diagnostic code, rather than a raw database error message."""


def check_schema(sport: str, config: StagingTiDBConfig) -> None:
    """Check one staging schema using only SELECTs; always close the engine."""
    if sport not in SCHEMAS:
        raise CheckFailed("invalid_target")
    engine = create_staging_engine(sport, config=config)
    try:
        with engine.connect() as connection:
            value, selected, version = connection.execute(
                text("SELECT 1, DATABASE(), VERSION()")
            ).one()
            if value != 1 or selected != SCHEMAS[sport]:
                raise CheckFailed("wrong_database")
            if "tidb" not in str(version).lower():
                raise CheckFailed("wrong_server")
            count = connection.execute(
                text("SELECT COUNT(*) FROM information_schema.tables "
                     "WHERE table_schema = :schema"),
                {"schema": SCHEMAS[sport]},
            ).scalar_one()
            if count != 0:
                raise CheckFailed("not_empty")
    finally:
        engine.dispose()


def safe_diagnostic(error: Exception) -> str:
    """Never print credentials, a raw server message, or connection strings."""
    fixed = {
        "invalid_target": "Rejected a non-staging target.",
        "wrong_database": "The selected database did not match the expected staging schema.",
        "wrong_server": "The server did not identify itself as TiDB.",
        "not_empty": "The staging schema already contains tables or views; nothing was changed.",
    }
    if isinstance(error, CheckFailed):
        return fixed.get(str(error), "Staging validation failed.")
    if isinstance(error, ssl.SSLError):
        return "TLS verification failed; do not disable certificate verification."
    if isinstance(error, ValueError):
        return "Missing or invalid TIDB_STAGING_* settings; check the three environment secrets."
    if isinstance(error, DBAPIError):
        args = getattr(error.orig, "args", ())
        code = args[0] if args and isinstance(args[0], int) else None
        messages = {
            1044: "Access to a staging schema was denied; check database grants.",
            1045: "Login was denied; check staging credentials and TiDB connection access rules.",
            1049: "An expected staging database was not found on this instance.",
            2003: "Connection failed; check hostname, DNS, port 4000 and network access rules.",
            2013: "The connection was interrupted or timed out.",
            2026: "TLS negotiation failed; do not disable certificate verification.",
        }
        if code in messages:
            return messages[code]
    return "Connection check failed; raw error details were withheld to protect credentials."


def main() -> int:
    lines = ["TiDB staging connection check (read-only)"]
    success = True
    try:
        config = StagingTiDBConfig.from_environment()
    except Exception as error:
        lines.append("FAIL: " + safe_diagnostic(error))
        success = False
    else:
        for sport in SCHEMAS:
            try:
                check_schema(sport, config)
            except Exception as error:
                lines.append(f"FAIL {sport}: " + safe_diagnostic(error))
                success = False
            else:
                lines.append(f"PASS {sport}: {SCHEMAS[sport]} is reachable and empty.")
    lines.append("No application data, schemas, production settings or collector endpoints were changed.")
    if success:
        lines.append("PASS: all three staging databases passed; no import or deployment was performed.")
    print("\n".join(lines), flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with Path(summary).open("a", encoding="utf-8") as stream:
                stream.write("\n".join(lines) + "\n")
        except OSError:
            print("Could not write the optional job summary.")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
