#!/usr/bin/env python3
"""Exercise migration and populated reports using disposable CI databases only."""
from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, select, func
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    raw_url = os.environ.get('MYSQL_TEST_ADMIN_URL', '')
    if os.environ.get('GITHUB_ACTIONS') != 'true' or not raw_url:
        raise RuntimeError('This check requires an explicit GitHub Actions test database')
    parsed = make_url(raw_url)
    if parsed.host not in ('127.0.0.1', 'localhost') or (ROOT / '.env').exists():
        raise RuntimeError('Refusing nonlocal database or server-owned .env')

    from Flask_App import mysql_cutover as migration
    from Flask_App.mysql_cutover_collation_safe import install
    from Flask_App.materialized_analytics import (
        DIRTY_VENUE, TIMELINE_BUCKETS, ensure_summary_schema, refresh_event_summary,
    )
    from models import Base, Event, Iteration, Ticket, captured_datetime_for_storage, event_datetime_utc
    from Flask_App.nfl_blueprint import NFLBase, NFLEvent, NFLIteration, NFLTicket
    from Flask_App.nhl_blueprint import NHLBase, NHLEvent, NHLIteration, NHLTicket

    install()
    admin = create_engine(parsed, pool_pre_ping=True)
    names = {sport: 'ticketsignal_ci_' + uuid4().hex[:12] + '_' + sport for sport in migration.SPORTS}
    engines = {}
    created = []
    try:
        with admin.begin() as connection:
            for sport, name in names.items():
                connection.exec_driver_sql(f'CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
                created.append(name)
                engines[sport] = create_engine(parsed.set(database=name), pool_pre_ping=True)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = {sport: root / (sport + '.db') for sport in migration.SPORTS}
            env = {
                'TICKETSIGNAL_DATABASE_BACKEND': 'sqlite',
                'DATABASE_PATH': str(sources['mlb']),
                'NFL_DATABASE_PATH': str(sources['nfl']),
                'NHL_DATABASE_PATH': str(sources['nhl']),
                'CONCERT_DATABASE_PATH': str(root / 'concerts.db'),
                'COLLECTOR_INGEST_TOKEN': 'integration-test-only',
                'FLASK_SECRET_KEY': 'integration-test-only',
            }
            with patch.dict(os.environ, env), patch('Flask_App.database_config.create_mysql_engine', side_effect=lambda sport: engines[sport]), patch.object(migration, 'create_mysql_engine', side_effect=lambda sport: engines[sport]):
                specs = {
                    'mlb': (Base, Event, Iteration, Ticket, 'Nationals Park', 'Washington Nationals', 'New York Mets'),
                    'nfl': (NFLBase, NFLEvent, NFLIteration, NFLTicket, 'MetLife Stadium', 'New York Giants', 'Dallas Cowboys'),
                    'nhl': (NHLBase, NHLEvent, NHLIteration, NHLTicket, 'Capital One Arena', 'Washington Capitals', 'New York Rangers'),
                }
                fingerprints = {}
                for sport, (base, E, I, T, venue, home, away) in specs.items():
                    source = create_engine(f'sqlite:///{sources[sport]}')
                    base.metadata.create_all(source)
                    ensure_summary_schema(source)
                    Session = sessionmaker(bind=source)
                    with Session() as session:
                        for n in range(4):
                            date = datetime(2025, 11, 10 + n, 18)
                            labels = ['Section 100'] + (['Cheap 200'] if n == 0 else [])
                            args = dict(title=away + ' at ' + home, event_date=date)
                            url = f'https://www.vividseats.com/--sports-mlb-baseball/production/{n+1}'
                            if sport == 'mlb':
                                args.update(Place=venue, URL=url, event_sections=labels)
                            else:
                                args.update(home_team=home, away_team=away, venue=venue, canonical_venue=venue, source_url=url, source_id=str(n+1), sections=labels, country='US')
                            if sport == 'nhl':
                                args.update(game_type=2, currency='USD')
                            event = E(**args)
                            session.add(event)
                            session.flush()
                            for index, (lower, upper, *_) in enumerate(TIMELINE_BUCKETS[sport][-5:]):
                                for delta in (-.5, 0, .5):
                                    capture = captured_datetime_for_storage(event_datetime_utc(date) - timedelta(hours=(lower+upper)/2 + delta))
                                    iteration = I(event_id=event.id, captured_at=capture)
                                    session.add(iteration)
                                    session.flush()
                                    for label in labels:
                                        ticket = dict(section=label, price=1 if label == 'Cheap 200' else 100-10*index, iteration_id=iteration.id)
                                        ticket['ticketsPerSection' if sport == 'mlb' else 'listing_count'] = 3
                                        session.add(T(**ticket))
                            session.flush()
                            refresh_event_summary(session, sport_key=sport, event_id=event.id, event_date=date, venue=venue, iteration_model=I, ticket_model=T, mark_complete=True)
                        # These keys sort differently under binary and case-insensitive collations.
                        for label in ('Z arena', 'a arena'):
                            session.execute(DIRTY_VENUE.insert().values(venue=label, revision=1, dirty=True, updated_at=datetime(2025, 11, 20)))
                        session.commit()
                    source.dispose()
                    fingerprints[sport] = migration._source_fingerprint(sources[sport])
                    report = migration.migrate_sport(sport, replace=False, batch_size=17)
                    assert len(report['tables']) == 6, report
                    assert not any(report['foreign_keys'].values()), report
                    print(sport, 'SQLite-to-MySQL verification passed', flush=True)

                os.environ['TICKETSIGNAL_DATABASE_BACKEND'] = 'mysql'
                from Flask_App.flask_app import app
                from Flask_App.nfl_stadium_blueprint import (
                    build_mlb_stadium_context, build_nfl_stadium_context, build_nhl_arena_context,
                    build_mlb_section_context, build_nfl_section_context, build_nhl_section_context,
                )
                builders = {'mlb': (build_mlb_stadium_context, build_mlb_section_context), 'nfl': (build_nfl_stadium_context, build_nfl_section_context), 'nhl': (build_nhl_arena_context, build_nhl_section_context)}
                for sport, (_, E, I, T, venue, home, away) in specs.items():
                    with app.test_request_context():
                        report = builders[sport][0](venue, home)
                        detail = builders[sport][1](venue, 'Section 100', home)
                    assert [r['name'] for r in report['cheapest_sections']] == ['Section 100'], report
                    assert [r['name'] for r in report['biggest_drops']] == ['Section 100'], report
                    row = report['cheapest_sections'][0]
                    assert row['ranking_price'] == 72.5 and row['ranking_drop_percent'] == 40.0, row
                    assert detail['section_summary']['ranking_price'] == 72.5, detail
                    with engines[sport].connect() as connection:
                        assert connection.execute(select(func.count()).select_from(T.__table__)).scalar_one() == 75
                    assert migration._source_fingerprint(sources[sport]) == fingerprints[sport]
                    print(sport, 'MySQL-backed populated reports and raw preservation passed', flush=True)
    finally:
        for engine in engines.values():
            engine.dispose()
        with admin.begin() as connection:
            for name in created:
                connection.exec_driver_sql(f'DROP DATABASE `{name}`')
        admin.dispose()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
