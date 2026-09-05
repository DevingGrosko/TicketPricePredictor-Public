"""Database backend selection and migration coordination for TicketSignal.

SQLite remains the default and rollback backend. Production can be switched to
three independent MySQL databases by setting ``TICKETSIGNAL_DATABASE_BACKEND``
to ``mysql`` and providing the server-owned MySQL settings in ``.env``.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
import re
from threading import RLock
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, URL


PROJECT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_DIR / ".env"
MIGRATION_PAUSE_PATH = PROJECT_DIR / ".database-migration-paused"
MIGRATION_MANIFEST_PATH = PROJECT_DIR / "mysql_migration_manifest.json"

_BACKEND_ENV = "TICKETSIGNAL_DATABASE_BACKEND"
_DATABASE_ENV = {
    "mlb": "MYSQL_MLB_DATABASE",
    "nfl": "MYSQL_NFL_DATABASE",
    "nhl": "MYSQL_NHL_DATABASE",
}
_ENGINE_LOCK = RLock()
_MYSQL_ENGINES: dict[str, Engine] = {}


def _load_environment() -> None:
    load_dotenv(ENV_PATH, override=False)


def configured_backend() -> str:
    """Return the selected production backend (``sqlite`` or ``mysql``)."""

    _load_environment()
    backend = os.getenv(_BACKEND_ENV, "sqlite").strip().casefold() or "sqlite"
    if backend not in {"sqlite", "mysql"}:
        raise RuntimeError(
            f"{_BACKEND_ENV} must be either 'sqlite' or 'mysql', not {backend!r}."
        )
    return backend


def _mysql_password() -> str:
    encoded = os.getenv("MYSQL_PASSWORD_B64", "").strip()
    if encoded:
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8")
        except Exception as exc:
            raise RuntimeError("MYSQL_PASSWORD_B64 is not valid base64 UTF-8.") from exc
    password = os.getenv("MYSQL_PASSWORD", "")
    if password:
        return password
    raise RuntimeError("MYSQL_PASSWORD_B64 is not configured in the server .env file.")


def mysql_url(sport_key: str) -> URL:
    """Build a SQLAlchemy URL without interpolating or logging the password."""

    _load_environment()
    sport = sport_key.strip().casefold()
    database_env = _DATABASE_ENV.get(sport)
    if database_env is None:
        raise ValueError(f"Unsupported sport database: {sport_key!r}")

    host = os.getenv("MYSQL_HOST", "").strip()
    username = os.getenv("MYSQL_USERNAME", "").strip()
    database = os.getenv(database_env, "").strip()
    missing = [
        name
        for name, value in (
            ("MYSQL_HOST", host),
            ("MYSQL_USERNAME", username),
            (database_env, database),
        )
        if not value
    ]
    if missing:
        raise RuntimeError("Missing MySQL settings: " + ", ".join(missing))

    return URL.create(
        "mysql+pymysql",
        username=username,
        password=_mysql_password(),
        host=host,
        database=database,
        query={"charset": "utf8mb4"},
    )


def create_mysql_engine(sport_key: str) -> Engine:
    """Return one pooled MySQL engine per sport in the current process."""

    sport = sport_key.strip().casefold()
    with _ENGINE_LOCK:
        existing = _MYSQL_ENGINES.get(sport)
        if existing is not None:
            return existing
        engine = create_engine(
            mysql_url(sport),
            pool_pre_ping=True,
            pool_recycle=240,
            pool_size=1,
            max_overflow=0,
            pool_timeout=20,
            connect_args={
                "connect_timeout": 10,
                "read_timeout": 90,
                "write_timeout": 90,
            },
        )
        _MYSQL_ENGINES[sport] = engine
        return engine


def create_ticket_engine(
    sport_key: str,
    *,
    sqlite_path: str | Path,
    force_sqlite: bool = False,
) -> Engine:
    """Create the selected engine while preserving explicit SQLite test paths."""

    path = Path(sqlite_path).expanduser().resolve()
    if force_sqlite or configured_backend() == "sqlite":
        path.parent.mkdir(parents=True, exist_ok=True)
        return create_engine(
            f"sqlite:///{path}",
            echo=False,
            connect_args={"timeout": 30},
        )
    return create_mysql_engine(sport_key)


def is_sqlite_engine(engine: Any) -> bool:
    return getattr(getattr(engine, "dialect", None), "name", "") == "sqlite"


def is_mysql_engine(engine: Any) -> bool:
    return getattr(getattr(engine, "dialect", None), "name", "") == "mysql"


def dispose_ticket_engine(engine: Engine) -> None:
    """Dispose per-use SQLite engines while retaining shared MySQL pools."""

    if is_sqlite_engine(engine):
        engine.dispose()


def migration_pause_active() -> bool:
    return MIGRATION_PAUSE_PATH.exists()


def begin_migration_pause() -> None:
    MIGRATION_PAUSE_PATH.write_text(
        "Ticket collection is paused during the SQLite-to-MySQL cutover.\n",
        encoding="utf-8",
    )
    MIGRATION_PAUSE_PATH.chmod(0o600)


def end_migration_pause() -> None:
    try:
        MIGRATION_PAUSE_PATH.unlink()
    except FileNotFoundError:
        pass


def update_backend_setting(backend: str) -> None:
    """Safely replace only the backend selector in the server-owned .env."""

    normalized = backend.strip().casefold()
    if normalized not in {"sqlite", "mysql"}:
        raise ValueError("backend must be sqlite or mysql")

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    pattern = re.compile(
        rf"^\s*(?:export\s+)?{re.escape(_BACKEND_ENV)}\s*=",
        flags=re.IGNORECASE,
    )
    kept = [line for line in lines if not pattern.match(line)]
    if kept and kept[-1]:
        kept.append("")
    kept.append(f"{_BACKEND_ENV}={normalized}")

    temporary = ENV_PATH.with_suffix(ENV_PATH.suffix + ".tmp")
    temporary.write_text("\n".join(kept) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(ENV_PATH)
    os.environ[_BACKEND_ENV] = normalized


def clear_mysql_engine_cache() -> None:
    """Dispose pooled engines; primarily useful in tests and maintenance tools."""

    with _ENGINE_LOCK:
        engines = list(_MYSQL_ENGINES.values())
        _MYSQL_ENGINES.clear()
    for engine in engines:
        engine.dispose()
