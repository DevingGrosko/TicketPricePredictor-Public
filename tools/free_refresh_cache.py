"""Recoverable, build-local raw-history cache for the free static publisher.

TiDB remains the durable store. A missing cache triggers a bounded full seed;
subsequent builds read metadata and only unseen capture rows. Source reads use
one repeatable-read transaction. Capture IDs, not a maximum-ID watermark,
handle late commits. Source history is assumed append-only; changed/deleted
capture metadata fails closed. No cache or credentials enter the public site.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace

from tools import build_static_preview as source

VERSION = 'free-refresh-raw-v1'


def encode(value):
    return json.dumps(value, default=lambda v: v.isoformat() if isinstance(v, datetime) else _unsupported(v),
                      sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _unsupported(value):
    raise TypeError('Unsupported cache value: '+type(value).__name__)


def text_time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise source.BuildError('Invalid timestamp in source metadata')
    return value.isoformat()


def capture_diff(known, current):
    """Do not skip an ID that committed after a numerically larger ID."""
    if set(known)-set(current):
        raise source.BuildError('Capture history was removed; explicit cache reconciliation is required')
    for key, value in known.items():
        if tuple(current[key]) != tuple(value):
            raise source.BuildError('Capture metadata changed; explicit cache reconciliation is required')
    return set(current)-set(known)


def initialize(spool, sport):
    spool.execute('CREATE TABLE raw(id INTEGER PRIMARY KEY,event_id INTEGER NOT NULL,section TEXT NOT NULL,price INTEGER NOT NULL,hours REAL NOT NULL,captured TEXT NOT NULL,listing_count INTEGER)')
    spool.execute('CREATE INDEX raw_event ON raw(event_id)')
    spool.execute('CREATE TABLE captures(id INTEGER PRIMARY KEY,event_id INTEGER NOT NULL,captured TEXT NOT NULL)')
    spool.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,body TEXT NOT NULL)')
    spool.execute('CREATE TABLE cache_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    spool.executemany('INSERT INTO cache_meta VALUES (?,?)', [('version',VERSION),('sport',sport)])
    spool.commit()


def save_cache(spool, target):
    """Only replace the last known cache after a successful committed read."""
    target = Path(target); target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise source.BuildError('Cache symlink rejected')
    fd, name = tempfile.mkstemp(prefix=target.name+'.', suffix='.tmp', dir=target.parent)
    os.close(fd)
    try:
        with sqlite3.connect(name) as out:
            spool.backup(out)
        if Path(name).stat().st_size > 3 * 1024**3:
            raise source.BuildError('Cache exceeds the bounded local storage budget')
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)


class SnapshotCache:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.metrics = {}

    def read_sport(self, sport, spool, settings, event_utc, *, include_maps=False):
        if not include_maps or sport not in source.SPORTS:
            raise source.BuildError('Free refresh cache requires the original sports builder')
        target = self.directory/(sport+'.sqlite')
        seeded = target.is_file()
        if seeded:
            if target.is_symlink():
                raise source.BuildError('Cache symlink rejected')
            with sqlite3.connect(target.as_uri()+'?mode=ro', uri=True) as cached:
                if dict(cached.execute('SELECT key,value FROM cache_meta')) != {'version':VERSION,'sport':sport}:
                    raise source.BuildError('Unexpected cache identity')
                cached.backup(spool)
        else:
            initialize(spool, sport)
        previous_events = {eid: json.loads(body) for eid, body in spool.execute('SELECT id,body FROM events')}
        known = {iid: (eid, captured) for iid, eid, captured in spool.execute('SELECT id,event_id,captured FROM captures')}
        tables = source.SPORTS[sport]
        engine = settings.engine_for(sport)
        inserted = 0
        with engine.connect() as remote, spool:
            isolation = str(remote.exec_driver_sql('SELECT @@transaction_isolation').scalar_one())
            if isolation.replace('-', ' ').upper() != 'REPEATABLE READ':
                raise source.BuildError('Repeatable-read isolation is required')
            columns = source.COLUMNS[sport] + (['geometry_updated_at'] if sport != 'mlb' else [])
            sql = 'SELECT '+','.join('`'+c+'`' for c in columns)+' FROM `'+tables[0]+'`'
            rows = remote.exec_driver_sql(sql).mappings().all()
            events = {}
            persisted_events = []
            for row in rows:
                values = {key: None for key in set(source.COMMON+source.COLUMNS['nhl']+source.COLUMNS['mlb'])}
                values.update(dict(row))
                for key in ('sections','event_sections'):
                    if isinstance(values[key],str):
                        values[key] = json.loads(values[key])
                    values[key] = values[key] or []
                values['currency'] = values.get('currency') or 'USD'
                eid = int(values['id'])
                if sport != 'mlb':
                    stamp = text_time(values['geometry_updated_at']) if values.get('geometry_updated_at') else None
                    previous = previous_events.get(eid)
                    if previous is not None and previous.get('geometry_updated_at') == stamp:
                        values['map_geometry'] = previous.get('map_geometry')
                    else:
                        geometry = remote.exec_driver_sql('SELECT map_geometry FROM `'+tables[0]+'` WHERE id=%s', (eid,)).scalar_one()
                        values['map_geometry'] = json.loads(geometry) if isinstance(geometry,str) else geometry
                events[eid] = SimpleNamespace(**values)
                persisted_events.append((eid, encode(values)))
            if set(previous_events)-set(events):
                raise source.BuildError('Events were removed; explicit cache reconciliation is required')
            current = {int(iid):(int(eid),text_time(captured)) for iid,eid,captured in remote.exec_driver_sql(
                'SELECT id,event_id,captured_at FROM `'+tables[1]+'`')}
            unseen = capture_diff(known, current)
            if any(eid not in events for eid,_ in current.values()):
                raise source.BuildError('Capture references a missing event')
            event_seconds = {eid:event_utc(e.event_date).timestamp() for eid,e in events.items()}
            select_rows = 'SELECT id,iteration_id,section,price'+(',listing_count' if sport != 'mlb' else '')+' FROM `'+tables[2]+'`'
            groups = [None] if not seeded else [sorted(unseen)[i:i+300] for i in range(0,len(unseen),300)]
            batch = []
            for ids in groups:
                query = select_rows if ids is None else select_rows+' WHERE iteration_id IN ('+','.join(['%s']*len(ids))+')'
                result = remote.execution_options(stream_results=True).exec_driver_sql(query, () if ids is None else tuple(ids))
                try:
                    for row in result:
                        rid,iid,label,price = row[:4]
                        if int(iid) not in current or (seeded and int(iid) not in unseen):
                            raise source.BuildError('Unexpected ticket/capture identity')
                        eid,captured = current[int(iid)]
                        moment = datetime.fromisoformat(captured)
                        if moment.tzinfo is None:
                            moment = moment.replace(tzinfo=timezone.utc)
                        hours = round((event_seconds[eid]-moment.timestamp())/3600,3)
                        if not isinstance(label,str) or type(price) is not int:
                            raise source.BuildError('Unexpected raw ticket types')
                        batch.append((int(rid),eid,label,price,hours,captured,row[4] if sport!='mlb' else None))
                        inserted += 1
                        if inserted > (25000000 if not seeded else 1000000):
                            raise source.BuildError('Raw-read budget exceeded')
                        if len(batch) == 10000:
                            spool.executemany('INSERT INTO raw VALUES(?,?,?,?,?,?,?)',batch); batch.clear()
                            if inserted % 500000 == 0:
                                print('FREE_CACHE_READ '+sport+' '+str(inserted),flush=True)
                finally:
                    result.close()
            if batch:
                spool.executemany('INSERT INTO raw VALUES(?,?,?,?,?,?,?)',batch)
            # Recompute lead times locally when a known game is rescheduled.
            for eid,e in events.items():
                previous = previous_events.get(eid)
                if previous and previous['event_date'] != text_time(e.event_date):
                    updates = []
                    for rid,captured in spool.execute('SELECT id,captured FROM raw WHERE event_id=?',(eid,)):
                        at = datetime.fromisoformat(captured)
                        if at.tzinfo is None: at = at.replace(tzinfo=timezone.utc)
                        updates.append((round((event_seconds[eid]-at.timestamp())/3600,3),rid))
                    spool.executemany('UPDATE raw SET hours=? WHERE id=?',updates)
            spool.executemany('INSERT OR REPLACE INTO events VALUES (?,?)',persisted_events)
            spool.executemany('INSERT INTO captures VALUES (?,?,?)',[(iid,*current[iid]) for iid in sorted(unseen)])
        latest = defaultdict(lambda:None); captures = defaultdict(int)
        for eid,captured in current.values():
            at = datetime.fromisoformat(captured)
            latest[eid] = max(latest[eid] or at, at)
            captures[eid] += 1
        total = spool.execute('SELECT COUNT(*) FROM raw').fetchone()[0]
        self.metrics[sport] = {'seeded_from_cache':seeded, 'new_captures':len(unseen),
                               'ticket_rows_read_from_tidb':inserted, 'cached_ticket_rows':total}
        save_cache(spool,target)
        print('FREE_CACHE_RESULT '+json.dumps({'sport':sport,**self.metrics[sport]}),flush=True)
        return events,latest,captures,{'games':len(events),'captures':len(current),'tickets':total}
