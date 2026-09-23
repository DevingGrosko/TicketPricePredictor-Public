"""Build a sports snapshot site. TiDB is read ONLY during the build.

Run from a clean checkout with TICKETSIGNAL_STAGING_SITE=1 and TIDB_STAGING_*
secrets. Output contains whitelisted public chart/report fields, never SQL,
connection settings, raw backups, or the temporary SQLite working database.
Nothing is deployed, scheduled, or written to TiDB by this module.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace

SPORTS = {
    'mlb': ('event', 'iterations', 'tickets'),
    'nfl': ('nfl_event', 'nfl_iterations', 'nfl_tickets'),
    'nhl': ('nhl_event', 'nhl_iterations', 'nhl_tickets'),
}
COMMON = ['id', 'title', 'event_date', 'sections', 'venue', 'source_url',
          'source_id', 'schedule_id', 'home_team', 'away_team',
          'canonical_venue', 'country', 'neutral_site', 'provider_venue']
COLUMNS = {
    'mlb': ['id', 'title', 'event_date', 'event_sections', 'URL', 'Place'],
    'nfl': COMMON + ['city'],
    'nhl': COMMON + ['game_type', 'season', 'currency', 'venue_timezone'],
}
SHARD_LIMIT = 384 * 1024
FILE_LIMIT = 2 * 1024 * 1024
BUNDLE_LIMIT = 400 * 1024 * 1024


class BuildError(RuntimeError):
    """A bounded, non-secret build failure suitable for the public CI log."""


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':'), sort_keys=True).encode('utf-8')


def utc_iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


@lru_cache(maxsize=100000)
def parsed_capture(value):
    return datetime.fromisoformat(value)


def valid_chart(chart):
    x, y = chart['x'], chart['y']
    if not x or len(x) != len(y):
        raise BuildError('Empty or mismatched chart arrays.')
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in x + y):
        raise BuildError('Invalid chart value.')
    if any(a < b for a, b in zip(x, x[1:])):
        raise BuildError('Chart timestamps are not in capture order.')
    return len(x)


class Bundle:
    """Only content-addressed JSON and explicitly copied UI assets are public."""
    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=False)
        (self.root / 'data').mkdir()
        self.files = {}
        self.bytes = 0
        self.max_file = 0

    def blob(self, kind, value):
        if kind not in {'series', 'game', 'report', 'index'}:
            raise BuildError('Invalid output category.')
        raw = encoded(value)
        if len(raw) > FILE_LIMIT:
            raise BuildError('A public JSON file exceeded its size budget.')
        digest = hashlib.sha256(raw).hexdigest()
        relative = f'data/{kind}-{digest}.json'
        if relative not in self.files:
            self.bytes += len(raw)
            if self.bytes > BUNDLE_LIMIT:
                raise BuildError('The public bundle exceeded its size budget.')
            (self.root / relative).write_bytes(raw)
            self.files[relative] = {'sha256': digest, 'bytes': len(raw)}
            self.max_file = max(self.max_file, len(raw))
        return relative

    def series(self, series):
        references, pending, names = [], {}, []
        size = 32

        def flush():
            nonlocal pending, names, size
            if pending:
                file = self.blob('series', {'sections': pending})
                references.extend({**row, 'file': file} for row in names)
                pending, names, size = {}, [], 32

        for label, chart in sorted(series.items(), key=lambda item: item[0].casefold()):
            count = valid_chart(chart)
            key = hashlib.sha256(label.encode()).hexdigest()
            needed = len(encoded(chart)) + len(key) + 8
            if needed > SHARD_LIMIT:
                raise BuildError('One section exceeds the chart-shard budget.')
            if size + needed > SHARD_LIMIT:
                flush()
            pending[key] = chart
            names.append({'name': label, 'key': key, 'points': count})
            size += needed
        flush()
        return references

    def finish(self, manifest, assets):
        for name in ('index.html', 'app.js', 'styles.css'):
            source = Path(assets) / name
            if not source.is_file() or source.is_symlink():
                raise BuildError('Missing static UI asset.')
            shutil.copyfile(source, self.root / name)
        (self.root / 'robots.txt').write_text('User-agent: *\nDisallow: /\n')
        (self.root / 'checksums.json').write_bytes(encoded(self.files))
        (self.root / 'manifest.json').write_bytes(encoded(manifest))
        # Validate every generated file before a hosting build can publish it.
        for relative, info in self.files.items():
            raw = (self.root / relative).read_bytes()
            if hashlib.sha256(raw).hexdigest() != info['sha256']:
                raise BuildError('Generated file checksum mismatch.')
        return {'json_files': len(self.files), 'json_bytes': self.bytes,
                'largest_json_bytes': self.max_file,
                'manifest_bytes': (self.root / 'manifest.json').stat().st_size}


def read_sport(sport, spool, settings, event_utc):
    """Copy required columns in one source transaction; stream ticket rows."""
    tables = SPORTS[sport]
    spool.execute('CREATE TABLE raw (id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, '
                  'section TEXT NOT NULL, price INTEGER NOT NULL, hours REAL NOT NULL, captured TEXT NOT NULL)')
    engine = settings.engine_for(sport)
    with engine.connect() as source:
        # All statements after the driver's transaction start are SELECTs.
        isolation = str(source.exec_driver_sql('SELECT @@transaction_isolation').scalar_one())
        if isolation.replace('-', ' ').upper() != 'REPEATABLE READ':
            raise BuildError('Snapshot build requires repeatable-read isolation.')
        raw_events = source.exec_driver_sql('SELECT ' + ','.join('`' + k + '`' for k in COLUMNS[sport])
                                           + ' FROM `' + tables[0] + '`').mappings().all()
        events = {}
        for row in raw_events:
            values = {k: None for k in set(COMMON + COLUMNS['nhl'] + COLUMNS['mlb'])}
            values.update(row)
            for key in ('sections', 'event_sections'):
                if isinstance(values[key], str):
                    values[key] = json.loads(values[key])
                values[key] = values[key] or []
            values['currency'] = values.get('currency') or 'USD'
            events[int(row['id'])] = SimpleNamespace(**values)
        event_times = {key: event_utc(e.event_date).timestamp() for key, e in events.items()}
        iterations = {}
        latest = defaultdict(lambda: None)
        captures = defaultdict(int)
        for iid, eid, captured in source.exec_driver_sql(
                'SELECT id,event_id,captured_at FROM `' + tables[1] + '`'):
            if int(eid) not in events:
                raise BuildError('Iteration references an unknown event.')
            timestamp = captured.replace(tzinfo=timezone.utc) if captured.tzinfo is None else captured
            lead = round((event_times[int(eid)] - timestamp.timestamp()) / 3600, 3)
            iterations[int(iid)] = (int(eid), lead, captured.isoformat())
            latest[int(eid)] = max(latest[int(eid)] or captured, captured)
            captures[int(eid)] += 1
        expected = int(source.exec_driver_sql('SELECT COUNT(*) FROM `' + tables[2] + '`').scalar_one())
        if expected > 25000000:
            raise BuildError('Snapshot exceeds the initial build row budget.')
        total, batch = 0, []
        result = source.execution_options(stream_results=True).exec_driver_sql(
            'SELECT id,iteration_id,section,price FROM `' + tables[2] + '`')
        for rid, iid, section, price in result:
            if int(iid) not in iterations:
                raise BuildError('Ticket references an unknown iteration.')
            eid, lead, captured = iterations[int(iid)]
            if not isinstance(section, str) or type(price) is not int:
                raise BuildError('Unexpected ticket column types.')
            batch.append((int(rid), eid, section, price, lead, captured))
            if len(batch) >= 10000:
                spool.executemany('INSERT INTO raw VALUES (?,?,?,?,?,?)', batch)
                total += len(batch)
                batch.clear()
                if total % 500000 == 0:
                    print(f'READ {sport}: {total:,}/{expected:,} ticket rows', flush=True)
        result.close()
        if batch:
            spool.executemany('INSERT INTO raw VALUES (?,?,?,?,?,?)', batch)
            total += len(batch)
        if total != expected:
            raise BuildError('Source row count does not match the streamed snapshot.')
        # Close the source BEFORE sorting or computing public output.
    spool.commit()
    spool.execute('CREATE INDEX raw_event ON raw(event_id)')
    print(f'SOURCE {sport}: {len(events)} games, {len(iterations)} captures, {total:,} tickets', flush=True)
    return events, latest, captures, {'games': len(events), 'captures': len(iterations), 'tickets': total}


def build_sport(sport, spool, events, latest, captures, bundle, now, api):
    """Use the existing pure report functions; never run a web request to build."""
    public, prepared, menu = {}, {}, {}
    canonical = api.section_identity
    for eid, event in sorted(events.items()):
        if api.is_preseason(sport, event):
            continue
        if sport == 'mlb' and (not api.event_has_complete_public_data(event)
                              or api.MLB_URL_MARKER not in str(event.URL or '').lower()):
            continue
        venue = api._event_venue_for_sport(event, sport)
        if not venue:
            continue
        team = api._home_team_for_report(event, sport) or venue
        public[eid] = event
        raw_series, per_capture = {}, {}
        for label, price, hours, capture in spool.execute(
                'SELECT section,price,hours,captured FROM raw WHERE event_id=? ORDER BY captured,id', (eid,)):
            identity = canonical(sport, venue, label)
            if identity is None:
                continue
            if 0 < hours <= (96 if sport == 'mlb' else 720):
                chart = raw_series.setdefault(label, {'x': [], 'y': []})
                chart['x'].append(hours)
                chart['y'].append(price)
            if price > 0:
                key = (identity.key, capture)
                old = per_capture.get(key)
                if old is None or price < old[0]:
                    per_capture[key] = (price, identity.raw_label)
        histories = defaultdict(list)
        for (key, capture), (price, label) in per_capture.items():
            histories[key].append((parsed_capture(capture), price, label))
        for key, history in histories.items():
            history.sort(key=lambda item: item[0])
            points = api._bucketed_game_prices(event, ((t, p) for t, p, _ in history), sport)
            if points:
                prepared[(key, eid)] = [{**point, 'section_name': history[-1][2]} for point in points]
        record = {'id': str(eid), 'sport': sport, 'title': event.title, 'venue': venue, 'team': team,
                  'currency': event.currency, 'event_at': api.event_datetime_utc(event.event_date).isoformat(),
                  'captured_through': utc_iso(latest[eid]), 'capture_count': captures[eid]}
        file = bundle.blob('game', {**record, 'sections': bundle.series(raw_series)})
        menu[eid] = {**record, 'file': file, 'section_count': len(raw_series)}
    reports = []
    # Preserve latest-season, home-team and SAME-building boundaries. Never
    # combine tenants of a shared stadium, alternate venues, or currencies.
    for team_events in api._report_groups(list(events.values()), sport).values():
        eligible, year = api.latest_season_events(team_events, sport)
        grouped = defaultdict(list)
        for event in eligible:
            if int(event.id) in public:
                grouped[(api._event_venue_for_sport(event, sport), event.currency)].append(event)
        for (venue, currency), cohort in sorted(grouped.items()):
            ids = {int(e.id) for e in cohort}
            subset = {key: val for key, val in prepared.items() if key[1] in ids}
            sections, _ = api._finalize_section_insights(
                cohort, subset, now, currency=currency, sport_key=sport,
                detail_url_builder=lambda e, s: None, secondary_url_builder=None,
                event_label_builder=lambda e: str(e.title))
            cheapest, drops = api._rank_sections(sections)
            # The report's exploratory timeline is precomputed using the SAME
            # per-game bucket function; missing windows are never filled in.
            grouped_points = defaultdict(lambda: defaultdict(list))
            for (key, _), points in subset.items():
                for point in points:
                    grouped_points[key][point['slot']].append(point['price'])
            for section in sections:
                timeline, _, _, _ = api._aggregate_bucket_points(
                    grouped_points[section['section_key']], sport_key=sport,
                    currency=currency, total_game_count=section['game_count'])
                section['timeline'] = timeline
            team = api._home_team_for_report(cohort[0], sport) or venue
            rid = hashlib.sha256(encoded([sport, team, venue, currency, year])).hexdigest()
            meta = {'id': rid, 'sport': sport, 'team': team, 'venue': venue, 'currency': currency,
                    'season': api.season_label(sport, year), 'game_count': len(cohort),
                    'captured_through': utc_iso(max((latest[i] for i in ids if latest[i]), default=None))}
            report = {**meta, 'analysis_at': now.isoformat(), 'sections': sections,
                      'cheapest': [s['section_key'] for s in cheapest],
                      'drops': [s['section_key'] for s in drops],
                      'games': [menu[e.id] for e in sorted(cohort, key=lambda e: e.event_date, reverse=True)]}
            reports.append({**meta, 'file': bundle.blob('report', report)})
    index = {'sport': sport, 'reports': sorted(reports, key=lambda r: (r['team'], r['venue'])),
             'games': sorted(menu.values(), key=lambda g: g['event_at'], reverse=True)}
    path = bundle.blob('index', index)
    print(f'BUILT {sport}: {len(menu)} eligible game histories, {len(reports)} reports', flush=True)
    return {'sport': sport, 'file': path, 'eligible_games': len(menu), 'reports': len(reports),
            'captured_through': utc_iso(max((v for v in latest.values() if v), default=None))}


def build(output):
    from Flask_App import staging_site_config as settings
    settings.validate_environment()
    # Import definitions and pure calculations, NOT Flask_App.flask_app or its
    # request handlers. No ORM models or maintenance functions are instantiated.
    from Flask_App import nfl_stadium_blueprint as api
    from models import event_datetime_utc
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    destination = Path(output).resolve()
    if destination.exists():
        raise BuildError('Output already exists; use a new build directory.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ticketsignal-static-', dir=destination.parent) as workspace:
        bundle = Bundle(Path(workspace) / 'public')
        manifest = {'version': 1, 'mode': 'historical-snapshot-preview', 'generated_at': now.isoformat(),
                    'live_updates_enabled': False, 'sports': [], 'source_counts': {}}
        try:
            for sport in SPORTS:
                spool = sqlite3.connect(Path(workspace) / (sport + '.sqlite'))
                try:
                    spool.execute('PRAGMA journal_mode=OFF')
                    spool.execute('PRAGMA synchronous=OFF')
                    spool.execute('PRAGMA cache_size=-65536')
                    events, latest, captures, counts = read_sport(sport, spool, settings, event_datetime_utc)
                    manifest['source_counts'][sport] = counts
                    manifest['sports'].append(build_sport(sport, spool, events, latest, captures, bundle, now, api))
                finally:
                    spool.close()
                    parsed_capture.cache_clear()
                (Path(workspace) / (sport + '.sqlite')).unlink()
            if settings.BLOCKED_SQL:
                raise BuildError('Unexpected SQL was blocked; refusing publication.')
            stats = bundle.finish(manifest, Path(__file__).resolve().parents[1] / 'static_preview')
            # Same-filesystem rename. A failed build never exposes partial output.
            bundle.root.rename(destination)
        finally:
            settings.clear_engines()
    report = {'passed': True, 'source_counts': manifest['source_counts'],
              'sports': manifest['sports'], **stats, 'seconds': round(time.monotonic() - started, 2),
              'database_writes': 0, 'deployed': False, 'live_updates_enabled': False}
    print('STATIC_BUILD_REPORT ' + json.dumps(report, sort_keys=True), flush=True)
    print('PASS: static files validated. No production access, database writes, schedule or deployment.', flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='static-preview-dist')
    args = parser.parse_args()
    try:
        build(args.output)
    except Exception as error:
        # Avoid printing provider exception messages or SQL parameter values.
        import traceback
        frames = traceback.extract_tb(error.__traceback__)
        print('STATIC_BUILD_FAILED ' + json.dumps({'type': type(error).__name__,
              'message': str(error) if isinstance(error, BuildError) else 'Provider details withheld.',
              'locations': [{'function': f.name, 'line': f.lineno} for f in frames[-5:]]}), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
