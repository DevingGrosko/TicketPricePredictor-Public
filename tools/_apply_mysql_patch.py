from __future__ import annotations

import ast
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content.rstrip() + "\n", encoding="utf-8")


def top_level_node(text: str, name: str, kind: type[ast.AST] | None = None) -> ast.AST:
    tree = ast.parse(text)
    matches = [
        node
        for node in tree.body
        if getattr(node, "name", None) == name and (kind is None or isinstance(node, kind))
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one top-level {name!r}, found {len(matches)}")
    return matches[0]


def replace_top_level(path: str, name: str, replacement: str, kind: type[ast.AST] | None = None) -> None:
    text = read(path)
    node = top_level_node(text, name, kind)
    lines = text.splitlines()
    lines[node.lineno - 1 : node.end_lineno] = replacement.strip("\n").splitlines()
    write(path, "\n".join(lines))


def class_method_node(text: str, class_name: str, method_name: str) -> ast.AST:
    class_node = top_level_node(text, class_name, ast.ClassDef)
    matches = [
        node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method_name
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one {class_name}.{method_name}, found {len(matches)}"
        )
    return matches[0]


def replace_class_method(path: str, class_name: str, method_name: str, replacement: str) -> None:
    text = read(path)
    node = class_method_node(text, class_name, method_name)
    lines = text.splitlines()
    replacement_lines = ["    " + line if line else "" for line in replacement.strip("\n").splitlines()]
    lines[node.lineno - 1 : node.end_lineno] = replacement_lines
    write(path, "\n".join(lines))


def insert_before_marker(path: str, marker: str, block: str) -> None:
    text = read(path)
    if block.strip() in text:
        return
    if marker not in text:
        raise RuntimeError(f"Marker {marker!r} missing from {path}")
    text = text.replace(marker, block.rstrip() + "\n\n" + marker, 1)
    write(path, text)


def rename_top_level_function(path: str, old: str, new: str) -> None:
    text = read(path)
    node = top_level_node(text, old, ast.FunctionDef)
    lines = text.splitlines()
    line = lines[node.lineno - 1]
    expected = f"def {old}("
    if expected not in line:
        raise RuntimeError(f"Could not rename {old} in {path}: {line}")
    lines[node.lineno - 1] = line.replace(expected, f"def {new}(", 1)
    write(path, "\n".join(lines))


DATABASE_RUNTIME = r'''"""Portable SQLAlchemy engine configuration for SQLite and MySQL.

Production can use independent MySQL databases for MLB, NFL, and NHL. Local
and test environments continue to fall back to the existing SQLite files.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from sqlalchemy import String, create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.compiler import compiles


DEFAULT_MYSQL_POOL_RECYCLE_SECONDS = 280
DEFAULT_MYSQL_POOL_SIZE = 2
DEFAULT_MYSQL_MAX_OVERFLOW = 2
DEFAULT_UNBOUNDED_STRING_LENGTH = 512


@compiles(String, "mysql")
def _compile_mysql_string(type_: String, compiler: Any, **kwargs: Any) -> str:
    """Give inferred/unbounded SQLAlchemy strings a safe MySQL length.

    SQLite accepts ``VARCHAR`` without a length, while MySQL does not. Existing
    models contain a number of inferred string columns, so the MySQL compiler
    supplies a conservative default without changing their SQLite schema.
    Explicit lengths remain unchanged.
    """

    if type_.length is None:
        type_ = String(DEFAULT_UNBOUNDED_STRING_LENGTH)
    return compiler.visit_VARCHAR(type_, **kwargs)


def normalize_database_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Database URL is empty.")
    if raw.startswith("mysql://"):
        raw = "mysql+pymysql://" + raw[len("mysql://") :]
    url = make_url(raw)
    if url.get_backend_name() == "mysql" and "charset" not in url.query:
        url = url.update_query_dict({"charset": "utf8mb4"})
    return url.render_as_string(hide_password=False)


def sqlite_url_from_path(path: str | Path) -> str:
    resolved = Path(path).expanduser().resolve()
    return f"sqlite:///{resolved}"


def database_url_from_env(
    url_environment_variable: str,
    path_environment_variable: str,
    default_path: str | Path,
) -> str:
    configured_url = os.environ.get(url_environment_variable, "").strip()
    if configured_url:
        return normalize_database_url(configured_url)
    configured_path = os.environ.get(
        path_environment_variable,
        str(default_path),
    )
    return sqlite_url_from_path(configured_path)


def sqlite_path_from_url(value: str) -> Path | None:
    url = make_url(normalize_database_url(value))
    if url.get_backend_name() != "sqlite" or not url.database:
        return None
    if url.database == ":memory:":
        return None
    return Path(url.database).expanduser().resolve()


def is_mysql_url(value: str) -> bool:
    return make_url(normalize_database_url(value)).get_backend_name() == "mysql"


def create_runtime_engine(
    database_url: str,
    *,
    echo: bool = False,
    sqlite_must_exist: bool = False,
) -> Engine:
    normalized = normalize_database_url(database_url)
    url = make_url(normalized)
    backend = url.get_backend_name()

    if backend == "sqlite":
        path = sqlite_path_from_url(normalized)
        if path is not None:
            if sqlite_must_exist and not path.exists():
                raise FileNotFoundError(f"Database file missing: {path}")
            path.parent.mkdir(parents=True, exist_ok=True)
        return create_engine(
            normalized,
            echo=echo,
            connect_args={"timeout": 30},
        )

    if backend == "mysql":
        return create_engine(
            normalized,
            echo=echo,
            pool_pre_ping=True,
            pool_recycle=int(
                os.environ.get(
                    "MYSQL_POOL_RECYCLE_SECONDS",
                    DEFAULT_MYSQL_POOL_RECYCLE_SECONDS,
                )
            ),
            pool_size=int(
                os.environ.get("MYSQL_POOL_SIZE", DEFAULT_MYSQL_POOL_SIZE)
            ),
            max_overflow=int(
                os.environ.get("MYSQL_MAX_OVERFLOW", DEFAULT_MYSQL_MAX_OVERFLOW)
            ),
        )

    raise ValueError(
        f"Unsupported database backend {backend!r}; use SQLite or MySQL."
    )


def redacted_database_url(value: str) -> str:
    return make_url(normalize_database_url(value)).render_as_string(
        hide_password=True
    )
'''


MIGRATION_TOOL = r'''#!/usr/bin/env python3
"""Copy TicketSignal's active SQLite databases into independent MySQL databases.

The script preserves primary keys, JSON values, timestamps, foreign-key
relationships, and materialized analytics rows. It never deletes the source
SQLite files. Run it while collection is paused, verify the result, then set
BASEBALL_DATABASE_URL, NFL_DATABASE_URL, and NHL_DATABASE_URL for cutover.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
from typing import Iterable

from sqlalchemy import MetaData, create_engine, delete, func, inspect, select
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Flask_App.database_runtime import (
    create_runtime_engine,
    normalize_database_url,
    redacted_database_url,
    sqlite_url_from_path,
)
from Flask_App.materialized_analytics import ensure_summary_schema
from models import Base, CreateModel
from Flask_App.nfl_blueprint import NFLBase, CreateNFLModel
from Flask_App.nhl_blueprint import NHLBase, CreateNHLModel


SUMMARY_TABLES = (
    "section_bucket_summary",
    "section_summary_state",
    "analytics_dirty_venue",
)


@dataclass(frozen=True)
class SportMigration:
    key: str
    path_environment_variable: str
    default_filename: str
    destination_environment_variable: str
    destination_alias: str
    base: object
    model_factory: object
    raw_tables: tuple[str, ...]

    @property
    def table_order(self) -> tuple[str, ...]:
        return self.raw_tables + SUMMARY_TABLES


SPORTS = {
    "mlb": SportMigration(
        key="mlb",
        path_environment_variable="DATABASE_PATH",
        default_filename="Event-collection.db",
        destination_environment_variable="BASEBALL_MYSQL_URL",
        destination_alias="BASEBALL_DATABASE_URL",
        base=Base,
        model_factory=CreateModel,
        raw_tables=("event", "iterations", "tickets"),
    ),
    "nfl": SportMigration(
        key="nfl",
        path_environment_variable="NFL_DATABASE_PATH",
        default_filename="NFL-collection.db",
        destination_environment_variable="NFL_MYSQL_URL",
        destination_alias="NFL_DATABASE_URL",
        base=NFLBase,
        model_factory=CreateNFLModel,
        raw_tables=("nfl_event", "nfl_iteration", "nfl_ticket"),
    ),
    "nhl": SportMigration(
        key="nhl",
        path_environment_variable="NHL_DATABASE_PATH",
        default_filename="NHL-collection.db",
        destination_environment_variable="NHL_MYSQL_URL",
        destination_alias="NHL_DATABASE_URL",
        base=NHLBase,
        model_factory=CreateNHLModel,
        raw_tables=("nhl_event", "nhl_iteration", "nhl_ticket"),
    ),
}


def source_path(config: SportMigration, override: str | None = None) -> Path:
    value = override or os.environ.get(
        config.path_environment_variable,
        str(ROOT / config.default_filename),
    )
    path = Path(value).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"{config.key}: source database missing: {path}")
    return path


def destination_url(
    config: SportMigration,
    override: str | None = None,
) -> str:
    value = (
        override
        or os.environ.get(config.destination_environment_variable, "").strip()
        or os.environ.get(config.destination_alias, "").strip()
    )
    if not value:
        raise ValueError(
            f"{config.key}: set {config.destination_environment_variable} "
            f"to the destination MySQL URL."
        )
    normalized = normalize_database_url(value)
    if not normalized.startswith(("mysql+pymysql://", "mysql+mysqldb://")):
        raise ValueError(f"{config.key}: destination must be a MySQL URL.")
    return normalized


def table_counts(engine: Engine, names: Iterable[str]) -> dict[str, int]:
    metadata = MetaData()
    metadata.reflect(engine, only=list(names))
    result: dict[str, int] = {}
    with engine.connect() as connection:
        for name in names:
            table = metadata.tables[name]
            result[name] = int(
                connection.execute(select(func.count()).select_from(table)).scalar_one()
            )
    return result


def prepare_destination(config: SportMigration, url: str) -> Engine:
    model = config.model_factory(database_url=url)
    engine = model.engine
    config.base.metadata.create_all(engine)
    ensure_summary_schema(engine)
    return engine


def clear_destination(engine: Engine, table_order: tuple[str, ...]) -> None:
    metadata = MetaData()
    metadata.reflect(engine, only=list(table_order))
    with engine.begin() as connection:
        if engine.dialect.name == "mysql":
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=0")
        try:
            for name in reversed(table_order):
                connection.execute(delete(metadata.tables[name]))
        finally:
            if engine.dialect.name == "mysql":
                connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=1")


def copy_table(
    source_engine: Engine,
    destination_engine: Engine,
    name: str,
    *,
    batch_size: int,
) -> int:
    source_metadata = MetaData()
    destination_metadata = MetaData()
    source_metadata.reflect(source_engine, only=[name])
    destination_metadata.reflect(destination_engine, only=[name])
    source_table = source_metadata.tables[name]
    destination_table = destination_metadata.tables[name]

    primary_keys = list(source_table.primary_key.columns)
    statement = select(source_table)
    if primary_keys:
        statement = statement.order_by(*primary_keys)

    copied = 0
    batch: list[dict[str, object]] = []
    with source_engine.connect() as source_connection:
        result = source_connection.execution_options(stream_results=True).execute(
            statement
        )
        with destination_engine.begin() as destination_connection:
            for row in result.mappings():
                batch.append(dict(row))
                if len(batch) >= batch_size:
                    destination_connection.execute(destination_table.insert(), batch)
                    copied += len(batch)
                    batch.clear()
            if batch:
                destination_connection.execute(destination_table.insert(), batch)
                copied += len(batch)
    return copied


def verify_counts(
    config: SportMigration,
    source_engine: Engine,
    destination_engine: Engine,
) -> dict[str, dict[str, int]]:
    source = table_counts(source_engine, config.table_order)
    destination = table_counts(destination_engine, config.table_order)
    mismatches = {
        name: {"source": source[name], "destination": destination[name]}
        for name in config.table_order
        if source[name] != destination[name]
    }
    if mismatches:
        raise RuntimeError(
            f"{config.key}: row-count verification failed: "
            + json.dumps(mismatches, sort_keys=True)
        )
    return {"source": source, "destination": destination}


def migrate_sport(
    config: SportMigration,
    *,
    source_override: str | None = None,
    destination_override: str | None = None,
    replace: bool = False,
    verify_only: bool = False,
    batch_size: int = 1000,
) -> dict[str, object]:
    source = source_path(config, source_override)
    destination = destination_url(config, destination_override)
    source_engine = create_engine(
        sqlite_url_from_path(source),
        connect_args={"timeout": 30},
    )
    destination_engine = prepare_destination(config, destination)

    try:
        source_inspector = inspect(source_engine)
        missing_source = [
            name for name in config.table_order if not source_inspector.has_table(name)
        ]
        if missing_source:
            raise RuntimeError(
                f"{config.key}: source is missing tables: {missing_source}"
            )

        if not verify_only:
            current = table_counts(destination_engine, config.table_order)
            occupied = {name: count for name, count in current.items() if count}
            if occupied and not replace:
                raise RuntimeError(
                    f"{config.key}: destination is not empty: {occupied}. "
                    "Use --replace only after confirming the destination is disposable."
                )
            if occupied:
                clear_destination(destination_engine, config.table_order)

            copied = {}
            for table_name in config.table_order:
                copied[table_name] = copy_table(
                    source_engine,
                    destination_engine,
                    table_name,
                    batch_size=batch_size,
                )
        else:
            copied = {}

        verification = verify_counts(config, source_engine, destination_engine)
        return {
            "sport": config.key,
            "source": str(source),
            "destination": redacted_database_url(destination),
            "copied": copied,
            "verified_counts": verification["destination"],
        }
    finally:
        source_engine.dispose()
        destination_engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sport",
        choices=("mlb", "nfl", "nhl", "all"),
        default="all",
    )
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--baseball-source")
    parser.add_argument("--nfl-source")
    parser.add_argument("--nhl-source")
    parser.add_argument("--baseball-url")
    parser.add_argument("--nfl-url")
    parser.add_argument("--nhl-url")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    selected = list(SPORTS) if args.sport == "all" else [args.sport]
    source_overrides = {
        "mlb": args.baseball_source,
        "nfl": args.nfl_source,
        "nhl": args.nhl_source,
    }
    destination_overrides = {
        "mlb": args.baseball_url,
        "nfl": args.nfl_url,
        "nhl": args.nhl_url,
    }

    reports = []
    for key in selected:
        report = migrate_sport(
            SPORTS[key],
            source_override=source_overrides[key],
            destination_override=destination_overrides[key],
            replace=args.replace,
            verify_only=args.verify_only,
            batch_size=max(1, args.batch_size),
        )
        reports.append(report)
        print(json.dumps(report, indent=2, sort_keys=True))

    print(json.dumps({"status": "ok", "sports": selected}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


MYSQL_INTEGRATION_CHECK = r'''#!/usr/bin/env python3
"""Create active TicketSignal schemas on a disposable MySQL/MariaDB server."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Flask_App.database_runtime import normalize_database_url
from models import CreateModel
from Flask_App.nfl_blueprint import CreateNFLModel
from Flask_App.nhl_blueprint import CreateNHLModel
from tools.migrate_sqlite_to_mysql import SPORTS, migrate_sport


def database_url(admin_url: str, database: str) -> str:
    return make_url(admin_url).set(database=database).render_as_string(
        hide_password=False
    )


def main() -> int:
    admin_url = normalize_database_url(
        os.environ.get(
            "MYSQL_TEST_ADMIN_URL",
            "mysql+pymysql://root:root@127.0.0.1:3306/mysql?charset=utf8mb4",
        )
    )
    databases = {
        "mlb": "ticketsignal_mlb_test",
        "nfl": "ticketsignal_nfl_test",
        "nhl": "ticketsignal_nhl_test",
    }
    admin = create_engine(admin_url, pool_pre_ping=True)
    with admin.begin() as connection:
        for name in databases.values():
            connection.exec_driver_sql(f"DROP DATABASE IF EXISTS `{name}`")
            connection.exec_driver_sql(
                f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )

    urls = {key: database_url(admin_url, name) for key, name in databases.items()}
    models = {
        "mlb": CreateModel(database_url=urls["mlb"]),
        "nfl": CreateNFLModel(database_url=urls["nfl"]),
        "nhl": CreateNHLModel(database_url=urls["nhl"]),
    }
    expected = {
        "mlb": {"event", "iterations", "tickets", "section_bucket_summary"},
        "nfl": {"nfl_event", "nfl_iteration", "nfl_ticket", "section_bucket_summary"},
        "nhl": {"nhl_event", "nhl_iteration", "nhl_ticket", "section_bucket_summary"},
    }
    for key, model in models.items():
        tables = set(inspect(model.engine).get_table_names())
        missing = expected[key] - tables
        if missing:
            raise RuntimeError(f"{key} MySQL schema missing tables: {sorted(missing)}")
        model.engine.dispose()

    # Exercise the copy/verification path against an empty but valid MLB source.
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "empty-mlb.db"
        source_model = CreateModel(db_path=source, create_if_missing=True)
        source_model.engine.dispose()
        migrate_sport(
            SPORTS["mlb"],
            source_override=str(source),
            destination_override=urls["mlb"],
            replace=True,
        )

    admin.dispose()
    print("MySQL schema and migration compatibility check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


DATABASE_RUNTIME_TEST = r'''from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from Flask_App.database_runtime import (
    database_url_from_env,
    normalize_database_url,
    redacted_database_url,
    sqlite_path_from_url,
    sqlite_url_from_path,
)


class DatabaseRuntimeTests(unittest.TestCase):
    def test_sqlite_fallback_uses_existing_path_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.db"
            with patch.dict(os.environ, {"TEST_PATH": str(path)}, clear=False):
                url = database_url_from_env("TEST_URL", "TEST_PATH", "ignored.db")
            self.assertEqual(sqlite_path_from_url(url), path.resolve())

    def test_mysql_scheme_is_normalized_to_pymysql_and_utf8mb4(self):
        normalized = normalize_database_url(
            "mysql://person:secret@example.mysql.pythonanywhere-services.com/db"
        )
        self.assertTrue(normalized.startswith("mysql+pymysql://"))
        self.assertIn("charset=utf8mb4", normalized)

    def test_redaction_hides_password(self):
        redacted = redacted_database_url(
            "mysql+pymysql://person:secret@localhost/db"
        )
        self.assertNotIn("secret", redacted)
        self.assertIn("***", redacted)


if __name__ == "__main__":
    unittest.main()
'''


MYSQL_DOC = r'''# Production MySQL migration

TicketSignal supports independent MySQL databases for the active MLB, NFL, and
NHL datasets. SQLite remains the default for local development and a rollback
source. The archived concert database remains SQLite.

## Why three databases

The sports intentionally use independent event tables and each currently owns
materialized analytics tables with the same names. Three logical MySQL
databases preserve that isolation and avoid a risky table-prefix rewrite.

## Required PythonAnywhere values

Create three MySQL databases in the PythonAnywhere **Databases** page and note
the hostname, username, database names, and password. Do not commit credentials.

Before migration, place destination URLs in the server-owned `.env` file:

```dotenv
BASEBALL_MYSQL_URL=mysql+pymysql://USER:PASSWORD@HOST/USER$ticket_mlb?charset=utf8mb4
NFL_MYSQL_URL=mysql+pymysql://USER:PASSWORD@HOST/USER$ticket_nfl?charset=utf8mb4
NHL_MYSQL_URL=mysql+pymysql://USER:PASSWORD@HOST/USER$ticket_nhl?charset=utf8mb4
```

Percent-encode special characters in the username or password.

## Migration and verification

Pause the PythonAnywhere dispatcher so no collection write occurs during the
copy. Keep the Flask app on SQLite while running:

```bash
cd ~/TicketPricePredictor-Public
source ~/.virtualenvs/YOUR_VENV/bin/activate
python tools/migrate_sqlite_to_mysql.py --sport all
```

The command refuses to overwrite a non-empty destination and verifies every
source/destination table count. It never changes or deletes the SQLite files.

After a successful copy, enable MySQL by adding:

```dotenv
BASEBALL_DATABASE_URL=${BASEBALL_MYSQL_URL}
NFL_DATABASE_URL=${NFL_MYSQL_URL}
NHL_DATABASE_URL=${NHL_MYSQL_URL}
```

Use the literal URLs in `.env`; Python dotenv files do not reliably expand the
shell-style references above. Reload the web app, verify all sports, and resume
the dispatcher.

## Rollback

Remove or comment out the three `*_DATABASE_URL` lines and reload the web app.
The application immediately returns to the original SQLite files. Keep the
SQLite files untouched until MySQL has run successfully for at least several
days.
'''


def patch_models() -> None:
    path = "models.py"
    insert_before_marker(
        path,
        "PROJECT_DIR = Path(__file__).resolve().parent",
        "from Flask_App.database_runtime import (\n"
        "    create_runtime_engine,\n"
        "    database_url_from_env,\n"
        "    normalize_database_url,\n"
        "    sqlite_path_from_url,\n"
        "    sqlite_url_from_path,\n"
        ")",
    )
    insert_before_marker(
        path,
        "def database_path() -> Path:",
        "def baseball_database_url() -> str:\n"
        "    return database_url_from_env(\n"
        "        \"BASEBALL_DATABASE_URL\",\n"
        "        \"DATABASE_PATH\",\n"
        "        PROJECT_DIR / \"Event-collection.db\",\n"
        "    )",
    )
    replace_top_level(
        path,
        "_ensure_baseball_performance_indexes",
        r'''
def _ensure_baseball_performance_indexes(engine, db_path: Path | None = None) -> None:
    """Create venue/history indexes on either SQLite or MySQL."""

    from sqlalchemy import inspect

    key = str(engine.url)
    with _BASEBALL_INDEX_LOCK:
        if key in _BASEBALL_INDEX_READY:
            return

        Base.metadata.create_all(engine)
        inspector = inspect(engine)
        required_tables = {"event", "iterations", "tickets"}
        if not required_tables.issubset(inspector.get_table_names()):
            return

        index_specs = {
            "event": (
                ("ix_event_place", ("Place",)),
                ("ix_event_url", ("URL",)),
            ),
            "iterations": (
                ("ix_iterations_event_captured", ("event_id", "captured_at")),
            ),
            "tickets": (
                ("ix_tickets_iteration_id", ("iteration_id",)),
                ("ix_tickets_section_iteration", ("section", "iteration_id")),
            ),
        }
        with engine.begin() as connection:
            preparer = engine.dialect.identifier_preparer
            for table_name, specifications in index_specs.items():
                existing = {
                    row["name"] for row in inspector.get_indexes(table_name)
                }
                for index_name, columns in specifications:
                    if index_name in existing:
                        continue
                    quoted_columns = ", ".join(
                        preparer.quote(column) for column in columns
                    )
                    connection.exec_driver_sql(
                        f"CREATE INDEX {preparer.quote(index_name)} "
                        f"ON {preparer.quote(table_name)} ({quoted_columns})"
                    )
            if engine.dialect.name == "sqlite":
                connection.exec_driver_sql("PRAGMA optimize")
        _BASEBALL_INDEX_READY.add(key)
''',
        ast.FunctionDef,
    )
    replace_class_method(
        path,
        "CreateModel",
        "__init__",
        r'''
def __init__(
    self,
    db_path: str | Path | None = None,
    *,
    database_url: str | None = None,
    create_if_missing: bool = False,
):
    if db_path is not None and database_url is not None:
        raise ValueError("Provide either db_path or database_url, not both.")
    if database_url is not None:
        self.database_url = normalize_database_url(database_url)
    elif db_path is not None:
        self.database_url = sqlite_url_from_path(db_path)
    else:
        self.database_url = baseball_database_url()

    active_sqlite_path = sqlite_path_from_url(self.database_url)
    self.sqlite_db_path = active_sqlite_path
    self.db_path = active_sqlite_path or database_path()
    self.engine = create_runtime_engine(
        self.database_url,
        echo=False,
        sqlite_must_exist=bool(active_sqlite_path and not create_if_missing),
    )
    Base.metadata.create_all(self.engine)
    _ensure_baseball_performance_indexes(self.engine, active_sqlite_path)

    # Imported lazily to avoid a module cycle: the analytics helper uses
    # the shared timezone conversion functions defined above.
    from Flask_App.materialized_analytics import ensure_summary_schema

    ensure_summary_schema(self.engine)
    self.SessionLocal = sessionmaker(
        bind=self.engine,
        autoflush=False,
        expire_on_commit=False,
    )
''',
    )


def patch_sport_blueprint(
    path: str,
    *,
    prefix: str,
    base_class: str,
    create_class: str,
    path_function: str,
    schema_function: str,
    default_constant: str,
    url_environment: str,
    path_environment: str,
) -> None:
    insert_before_marker(
        path,
        "# Inlined for the restricted PythonAnywhere deployment.",
        "from Flask_App.database_runtime import (\n"
        "    create_runtime_engine,\n"
        "    database_url_from_env,\n"
        "    normalize_database_url,\n"
        "    sqlite_path_from_url,\n"
        "    sqlite_url_from_path,\n"
        ")",
    )
    insert_before_marker(
        path,
        f"def {path_function}() -> Path:",
        f"def {prefix.lower()}_database_url() -> str:\n"
        f"    return database_url_from_env(\n"
        f"        \"{url_environment}\",\n"
        f"        \"{path_environment}\",\n"
        f"        {default_constant},\n"
        f"    )",
    )
    sqlite_schema_function = f"{schema_function}_sqlite"
    rename_top_level_function(path, schema_function, sqlite_schema_function)
    insert_before_marker(
        path,
        f"def {sqlite_schema_function}(",
        f'''def {schema_function}(engine: Any, db_path: Path | None) -> None:
    if engine.dialect.name == "sqlite":
        if db_path is None:
            raise RuntimeError("SQLite engine is missing its database path.")
        {sqlite_schema_function}(engine, db_path)
        return

    key = str(engine.url)
    with _SCHEMA_LOCK:
        if key in _SCHEMA_READY:
            return
        {base_class}.metadata.create_all(engine)
        ensure_summary_schema(engine)
        _SCHEMA_READY.add(key)''',
    )
    replace_class_method(
        path,
        create_class,
        "__init__",
        f'''
def __init__(
    self,
    db_path: str | Path | None = None,
    *,
    database_url: str | None = None,
):
    if db_path is not None and database_url is not None:
        raise ValueError("Provide either db_path or database_url, not both.")
    if database_url is not None:
        self.database_url = normalize_database_url(database_url)
    elif db_path is not None:
        self.database_url = sqlite_url_from_path(db_path)
    else:
        self.database_url = {prefix.lower()}_database_url()

    active_sqlite_path = sqlite_path_from_url(self.database_url)
    self.sqlite_db_path = active_sqlite_path
    self.db_path = active_sqlite_path or {path_function}()
    self.engine = create_runtime_engine(self.database_url, echo=False)
    {schema_function}(self.engine, active_sqlite_path)
    self.SessionLocal = sessionmaker(
        bind=self.engine,
        autoflush=False,
        expire_on_commit=False,
    )
''',
    )


def patch_environment_and_requirements() -> None:
    requirements = read("requirements.txt")
    if "PyMySQL" not in requirements:
        requirements += "\nPyMySQL>=1.1\n"
    write("requirements.txt", requirements)

    env = read(".env.example")
    additions = r'''
# Optional production MySQL URLs. Omit them to keep using the SQLite paths above.
BASEBALL_DATABASE_URL=mysql+pymysql://user:password@host/user$ticket_mlb?charset=utf8mb4
NFL_DATABASE_URL=mysql+pymysql://user:password@host/user$ticket_nfl?charset=utf8mb4
NHL_DATABASE_URL=mysql+pymysql://user:password@host/user$ticket_nhl?charset=utf8mb4

# Destination-only URLs used while copying from SQLite before cutover.
BASEBALL_MYSQL_URL=mysql+pymysql://user:password@host/user$ticket_mlb?charset=utf8mb4
NFL_MYSQL_URL=mysql+pymysql://user:password@host/user$ticket_nfl?charset=utf8mb4
NHL_MYSQL_URL=mysql+pymysql://user:password@host/user$ticket_nhl?charset=utf8mb4
'''
    if "BASEBALL_DATABASE_URL=" not in env:
        env += "\n" + additions
    write(".env.example", env)


def remove_temporary_files() -> None:
    for relative in (
        ".github/workflows/export-mysql-migration-source.yml",
        ".github/workflows/publish-mysql-migration.yml",
        "tools/_apply_mysql_patch.py",
    ):
        path = ROOT / relative
        if path.exists():
            path.unlink()


def main() -> None:
    write("Flask_App/database_runtime.py", DATABASE_RUNTIME)
    write("tools/migrate_sqlite_to_mysql.py", MIGRATION_TOOL)
    write("tools/mysql_integration_check.py", MYSQL_INTEGRATION_CHECK)
    write("tests/test_database_runtime.py", DATABASE_RUNTIME_TEST)
    write("docs/mysql-migration.md", MYSQL_DOC)
    patch_models()
    patch_sport_blueprint(
        "Flask_App/nfl_blueprint.py",
        prefix="NFL",
        base_class="NFLBase",
        create_class="CreateNFLModel",
        path_function="nfl_database_path",
        schema_function="_ensure_nfl_schema",
        default_constant="DEFAULT_NFL_DATABASE",
        url_environment="NFL_DATABASE_URL",
        path_environment="NFL_DATABASE_PATH",
    )
    patch_sport_blueprint(
        "Flask_App/nhl_blueprint.py",
        prefix="NHL",
        base_class="NHLBase",
        create_class="CreateNHLModel",
        path_function="nhl_database_path",
        schema_function="_ensure_nhl_schema",
        default_constant="DEFAULT_NHL_DATABASE",
        url_environment="NHL_DATABASE_URL",
        path_environment="NHL_DATABASE_PATH",
    )
    patch_environment_and_requirements()
    remove_temporary_files()


if __name__ == "__main__":
    main()
