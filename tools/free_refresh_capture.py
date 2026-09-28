"""Independent capture delivery into existing TiDB STAGING raw tables only.

No production endpoint, schema creation, deletion, deployment, or billing change.
Existing collectors and their parsers remain unchanged. The optional network
smoke discovers one real MLB event and verifies its committed snapshot twice.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
from unittest.mock import patch

from sqlalchemy import event as sql_event, func, or_, select
from sqlalchemy.orm import Session

from Flask_App.tidb_staging import SCHEMAS, create_staging_engine

TABLES = {
    'mlb': ('event', 'iterations', 'tickets'),
    'nfl': ('nfl_event', 'nfl_iterations', 'nfl_tickets'),
    'nhl': ('nhl_event', 'nhl_iterations', 'nhl_tickets'),
}


def models_for(sport):
    if sport == 'mlb':
        from models import Event, Iteration, Ticket
        return Event, Iteration, Ticket
    if sport == 'nfl':
        from Flask_App.nfl_blueprint import NFLEvent, NFLIteration, NFLTicket
        return NFLEvent, NFLIteration, NFLTicket
    if sport == 'nhl':
        from Flask_App.nhl_blueprint import NHLEvent, NHLIteration, NHLTicket
        return NHLEvent, NHLIteration, NHLTicket
    raise ValueError('Unknown sport')


def require_write_statement(statement, sport):
    """Allow reads and narrowly scoped raw inserts/event updates; never DDL."""
    from Flask_App.staging_site_config import require_read_sql
    sql = statement.strip()
    if re.match(r'^(SELECT|SHOW|DESCRIBE)\b', sql, re.I):
        require_read_sql(sql)
        return
    if any(token in sql for token in (';', '/*', '--', '#')):
        raise RuntimeError('Unsafe write statement blocked')
    matched = re.match(r'^(INSERT\s+INTO|UPDATE)\s+`?([a-z_]+)`?\s', sql, re.I)
    if not matched or matched[2] not in TABLES[sport]:
        raise RuntimeError('Non-raw-table write blocked')
    if matched[1].upper() == 'UPDATE' and matched[2] != TABLES[sport][0]:
        raise RuntimeError('Stored capture history is append-only')


def open_writer(sport):
    from Flask_App.staging_site_config import validate_environment, check_connection
    validate_environment()
    if os.environ.get('TICKETSIGNAL_ENABLE_STAGING_WRITES') != '1':
        raise RuntimeError('Explicit staging-write opt-in is required')
    engine = create_staging_engine(sport)

    @sql_event.listens_for(engine, 'connect')
    def verify_target(connection, _record):
        check_connection(connection, SCHEMAS[sport])

    @sql_event.listens_for(engine, 'before_cursor_execute')
    def guard(_connection, _cursor, statement, _parameters, _context, _many):
        require_write_statement(statement, sport)

    return engine


def parse_payload(sport, payload, now=None):
    from collector import as_utc, snapshot_from_payload, CAPTURE_WINDOW_HOURS
    from models import captured_datetime_for_storage, event_datetime_for_storage
    now = now or datetime.now(timezone.utc)
    if len(json.dumps(payload)) > 4 * 1024 * 1024:
        raise ValueError('Snapshot is larger than the bounded input limit')
    if sport == 'mlb':
        if payload.get('event_type') not in (None, 'mlb', 'baseball'):
            raise ValueError('Wrong sport')
        url, at, captured, snapshot = snapshot_from_payload(payload)
        if '--sports-mlb-baseball/' not in url.lower():
            raise ValueError('MLB URL classification failed')
        metadata, geometry = {}, None
        window = CAPTURE_WINDOW_HOURS
    else:
        from Flask_App import nfl_blueprint as nfl, nhl_blueprint as nhl
        api = nfl if sport == 'nfl' else nhl
        url, at, captured, snapshot, metadata, geometry = getattr(api, sport+'_snapshot_from_payload')(payload)
        window = 30 * 24
    captured = as_utc(captured)
    age = now - captured
    if age < -timedelta(minutes=5) or age > timedelta(days=7):
        raise ValueError('Capture time is outside the allowed replay window')
    lead = (as_utc(at) - captured).total_seconds() / 3600
    if not 0 < lead <= window:
        raise ValueError('Event is outside the sport capture window')
    if not 1 <= len(snapshot.sections) <= 2000:
        raise ValueError('Invalid bounded section count')
    slot = captured.replace(minute=0 if captured.minute < 30 else 30, second=0, microsecond=0)
    return url, event_datetime_for_storage(at), captured_datetime_for_storage(slot), snapshot, metadata, geometry


def store_payload(engine, sport, payload, *, now=None):
    """Atomic raw snapshot; duplicate half-hour slots are preserved, not replaced.

    The workflow must serialize writers per sport. No summary rows are changed:
    the separate static builder derives reports from the raw history.
    """
    url, event_at, captured, snapshot, metadata, geometry = parse_payload(sport, payload, now)
    Event, Iteration, Ticket = models_for(sport)
    with Session(engine) as session, session.begin():
        condition = Event.URL == url if sport == 'mlb' else or_(
            Event.source_url == url, Event.source_id == snapshot.source_id)
        stored = session.scalars(select(Event).where(condition)).one_or_none()
        if stored is not None:
            existing = session.scalars(select(Iteration).where(
                Iteration.event_id == stored.id, Iteration.captured_at == captured)).one_or_none()
            if existing is not None:
                count = session.scalar(select(func.count()).select_from(Ticket).where(Ticket.iteration_id == existing.id))
                if not count:
                    raise RuntimeError('Existing iteration has no ticket rows; refusing silent acceptance')
                return {'status': 'duplicate', 'event_id': int(stored.id),
                        'iteration_id': int(existing.id), 'sections': int(count),
                        'captured_at': captured.isoformat(), 'sport': sport}
        names = [row.section for row in snapshot.sections]
        if stored is None:
            stored = Event(title=snapshot.title, event_date=event_at)
            if sport == 'mlb':
                stored.URL, stored.Place, stored.event_sections = url, snapshot.venue, names
            else:
                stored.source_url, stored.source_id = url, snapshot.source_id
                stored.venue, stored.sections = snapshot.venue, names
            session.add(stored)
            session.flush()
        latest = session.scalar(select(func.max(Iteration.captured_at)).where(Iteration.event_id == stored.id))
        if latest is None or captured >= latest:
            stored.title, stored.event_date = snapshot.title, event_at
            if sport == 'mlb':
                stored.URL, stored.Place = url, snapshot.venue
                stored.event_sections = list(dict.fromkeys([*(stored.event_sections or []), *names]))
            else:
                from Flask_App import nfl_blueprint as nfl, nhl_blueprint as nhl
                api = nfl if sport == 'nfl' else nhl
                stored.source_url, stored.source_id, stored.venue = url, snapshot.source_id, snapshot.venue
                stored.sections = list(dict.fromkeys([*(stored.sections or []), *names]))
                api._apply_event_metadata(stored, snapshot, metadata, geometry, captured)
        iteration = Iteration(event_id=stored.id, captured_at=captured)
        session.add(iteration)
        session.flush()
        # No ticket primary keys are needed here. Core executemany avoids one
        # network round trip per section to retrieve generated ORM identifiers.
        # The event, iteration and entire batch still commit atomically.
        rows = []
        for row in snapshot.sections:
            values = dict(iteration_id=iteration.id, section=row.section, price=row.price)
            values['ticketsPerSection' if sport == 'mlb' else 'listing_count'] = row.listing_count
            rows.append(values)
        session.execute(Ticket.__table__.insert(), rows)
        result = {'status': 'stored', 'event_id': int(stored.id), 'iteration_id': int(iteration.id),
                  'sections': len(names), 'captured_at': captured.isoformat(), 'sport': sport}
    # Independent post-commit read. A transaction failure cannot masquerade as an upload.
    with Session(engine) as check:
        count = check.scalar(select(func.count()).select_from(Ticket).where(Ticket.iteration_id == result['iteration_id']))
        if count != result['sections']:
            raise RuntimeError('Committed snapshot readback count mismatch')
    return result


def capture(sport, directory, *, smoke=False):
    import collector
    import nfl_collector
    import nfl_schedule_collector
    import nhl_schedule_collector
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    engine = open_writer(sport)
    try:
        if smoke:
            if sport != 'mlb':
                raise ValueError('The bounded single-game smoke is MLB only')
            path = directory/'captured.json'
            collector.run_auto_smoke_capture(True, 35, path)
            payload = json.loads(path.read_text())
            payload['schema_version'] = 1
            first = store_payload(engine, sport, payload)
            second = store_payload(engine, sport, payload)
            if first['iteration_id'] != second['iteration_id'] or second['status'] != 'duplicate':
                raise RuntimeError('Idempotency readback failed')
            report = {'passed': True, 'first': first, 'second': second,
                      'destination': SCHEMAS[sport], 'production_requests': 0,
                      'title': payload['title'], 'source_url': payload['source_url']}
            (directory/'smoke-report.json').write_text(json.dumps(report, indent=2))
            print('FREE_CAPTURE_SMOKE '+json.dumps(report), flush=True)
            return 0

        def deliver(_endpoint, _token, payload, **_kwargs):
            try:
                return store_payload(engine, sport, payload)
            except Exception as exc:
                # Provider exceptions can contain SQL parameters. Log type only.
                raise RuntimeError('Staging delivery failed: '+type(exc).__name__) from None

        def no_http_upload(*_args, **_kwargs):
            raise RuntimeError('HTTP snapshot uploads are disabled in the independent pipeline')

        module = {'mlb': collector, 'nfl': nfl_schedule_collector, 'nhl': nhl_schedule_collector}[sport]
        function = module.run_remote_collector if sport == 'mlb' else module.run_schedule_collector
        with ExitStack() as patches:
            patches.enter_context(patch.object(collector, 'post_snapshot', no_http_upload))
            for target in (collector, nfl_collector, nfl_schedule_collector, nhl_schedule_collector):
                patches.enter_context(patch.object(target, 'post_snapshot_with_retry', deliver))
            code = function('https://staging-write.invalid/no-http', 'not-an-http-token', True, 35,
                            directory/'health.json', directory/'pending')
        health = json.loads((directory/'health.json').read_text())
        if health.get('pending', 0):
            return 1
        return code
    finally:
        engine.dispose()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sport', choices=TABLES, required=True)
    p.add_argument('--directory', default='free-capture')
    p.add_argument('--smoke', action='store_true')
    a = p.parse_args()
    try:
        return capture(a.sport, a.directory, smoke=a.smoke)
    except Exception as exc:
        print('FREE_CAPTURE_FAILED '+type(exc).__name__, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
