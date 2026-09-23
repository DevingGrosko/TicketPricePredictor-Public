"""Opt-in, read-only website preview over the verified TiDB sports snapshots.

This module is not an ingestion backend. It never changes production settings,
creates database objects, or enables writes. The original import helpers remain
separate. A database SELECT-only user is also recommended for public previews.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
from threading import RLock

from sqlalchemy import create_engine, event
from Flask_App.tidb_staging import SCHEMAS, StagingTiDBConfig

FLAG = 'TICKETSIGNAL_STAGING_SITE'
_LOCK = RLock()
_ENGINES = {}
BLOCKED_SQL = []
_DANGEROUS = re.compile(
    r';|/\*|--|#|:=|\b(?:INTO|OUTFILE|DUMPFILE|FOR\s+UPDATE|FOR\s+SHARE|'
    r'LOCK\s+IN\s+SHARE|GET_LOCK|RELEASE_LOCK|SLEEP|BENCHMARK|LOAD_FILE|NEXTVAL)\b', re.I
)


class StagingReadOnlyError(RuntimeError):
    pass


def enabled() -> bool:
    value = os.environ.get(FLAG, '').strip()
    if value not in ('', '0', '1'):
        raise RuntimeError(FLAG + ' must be 0 or 1; refusing ambiguous routing.')
    return value == '1'


def validate_environment(*, website=False) -> StagingTiDBConfig:
    if not enabled():
        raise RuntimeError('The staging site requires ' + FLAG + '=1.')
    if os.environ.get('TICKETSIGNAL_DATABASE_BACKEND', 'mysql').strip().lower() != 'mysql':
        raise RuntimeError('Staging website requires the MySQL dialect; SQLite fallback is disabled.')
    forbidden = [k for k in os.environ if (
        k.startswith('MYSQL_') or k in (
            'COLLECTOR_INGEST_TOKEN', 'DATABASE_PATH', 'NFL_DATABASE_PATH',
            'NHL_DATABASE_PATH', 'CONCERT_DATABASE_PATH', 'PYTHONANYWHERE_SSH_KEY'
        )
    ) and os.environ[k]]
    if forbidden:
        raise RuntimeError('Remove production/local database settings from the isolated preview: ' + ', '.join(sorted(forbidden)))
    root = Path(__file__).resolve().parent
    if any((p / '.env').exists() for p in (root, *root.parents)):
        raise RuntimeError('Staging preview must run in a clean checkout without a discoverable .env.')
    if website and len(os.environ.get('FLASK_SECRET_KEY', '')) < 32:
        raise RuntimeError('Set a separate FLASK_SECRET_KEY with at least 32 characters.')
    return StagingTiDBConfig.from_environment()


def require_read_sql(statement: str) -> None:
    sql = statement.strip()
    if not re.match(r'\A(?:SELECT|SHOW|DESCRIBE)\s', sql, re.I) or _DANGEROUS.search(sql):
        verb = re.match(r'[A-Za-z]+', sql)
        if len(BLOCKED_SQL) < 100:
            BLOCKED_SQL.append(verb[0].upper() if verb else 'OTHER')
        raise StagingReadOnlyError('The staging preview permits database reads only.')


def check_connection(dbapi_connection, schema: str) -> None:
    with dbapi_connection.cursor() as cursor:
        cursor.execute('SELECT DATABASE(), VERSION(), @@foreign_key_checks')
        selected, version, fk = cursor.fetchone()
    if selected != schema or 'tidb' not in str(version).lower() or int(fk) != 1:
        raise RuntimeError('Staging connection target or constraint configuration is invalid.')


def engine_for(sport: str):
    config = validate_environment()
    if sport not in SCHEMAS:
        raise ValueError('Staging website supports the three verified sports only.')
    with _LOCK:
        if sport not in _ENGINES:
            # Existing GraphBuilder methods nest independent ORM sessions.
            # The single-connection import pool would block those requests.
            # Three connections and one web worker bound the preview resource use.
            engine = create_engine(
                config.url(sport), pool_pre_ping=True, pool_recycle=240,
                pool_size=3, max_overflow=0, pool_timeout=20,
                echo=False, hide_parameters=True,
                connect_args={'ssl': config.tls_context(), 'connect_timeout':10,
                              'read_timeout':90, 'write_timeout':90},
            )
            schema = SCHEMAS[sport]

            @event.listens_for(engine, 'connect')
            def verify_target(dbapi_connection, _record):
                check_connection(dbapi_connection, schema)

            @event.listens_for(engine, 'before_cursor_execute')
            def reads_only(_connection, _cursor, statement, _parameters, _context, _many):
                require_read_sql(statement)

            _ENGINES[sport] = engine
        return _ENGINES[sport]


def clear_engines() -> None:
    with _LOCK:
        engines = list(_ENGINES.values())
        _ENGINES.clear()
    for engine in engines:
        engine.dispose()
