"""Free-pipeline policy adapters. Production entry points are never modified."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from Flask_App.collection_cadence import half_hour_capture_slot
from decimal import Decimal
import json
from pathlib import Path
from unittest.mock import patch

UTC = timezone.utc


def due_since(schedule, slot, previous, select_due):
    """Recover missed cadence evaluations, taking NEW prices rather than backdating.

    One full daily cadence covers every existing NHL tier. Older missing prices
    cannot be reconstructed by a live scrape and remain historical gaps.
    """
    slot = half_hour_capture_slot(slot)
    start = slot
    if previous:
        previous = datetime.fromisoformat(previous).astimezone(UTC)
        start = min(slot, max(slot - timedelta(hours=23, minutes=30),
                             half_hour_capture_slot(previous) + timedelta(minutes=30)))
    chosen = {}
    while start <= slot:
        for game in select_due(schedule, start):
            chosen[str(game.schedule_id)] = game
        start += timedelta(minutes=30)
    return sorted(chosen.values(), key=lambda g: (g.event_date, str(g.schedule_id)))


def preserved_parser(original):
    """Validate trusted saved payloads without a seven-day expiration policy.

    The original sport/event/section checks still run. Only the comparison to
    wall-clock age changes; event lead time is evaluated at observation time.
    """
    def parse(sport, payload, now=None):
        from collector import parse_iso_datetime, as_utc
        now = now or datetime.now(UTC)
        stamp = parse_iso_datetime(str(payload.get('captured_at') or ''))
        if stamp is None:
            raise ValueError('Missing original capture timestamp')
        stamp = as_utc(stamp)
        if stamp > now + timedelta(minutes=5):
            raise ValueError('Capture timestamp is in the future')
        for row in payload.get('sections') or []:
            price = Decimal(str(row['price']))
            count = Decimal(str(row['listing_count']))
            if not price.is_finite() or price <= 0 or not count.is_finite() or count < 1 or count != int(count):
                raise ValueError('Invalid price or inventory count')
        return original(sport, payload, now=stamp)
    return parse


def replay_each(sport, endpoint, token, pending_dir):
    """Attempt every saved payload. Failed files stay intact; no global latch."""
    import collector
    from tools.free_refresh_capture import parse_payload
    pending_dir = Path(pending_dir)
    pending_dir.mkdir(parents=True, exist_ok=True)
    count, errors = 0, []
    files = sorted({*pending_dir.glob('*.json'), *pending_dir.glob('*.rejected')})
    for path in files:
        try:
            if path.is_symlink() or path.stat().st_size > 4 * 1024**2:
                raise ValueError('Invalid pending file')
            payload = json.loads(path.read_text())
            parse_payload(sport, payload)
            result = collector.post_snapshot_with_retry(endpoint, token, payload)
            if result.get('status') not in ('stored', 'duplicate'):
                raise ValueError('Unacknowledged saved snapshot')
            path.unlink()
            count += 1
        except Exception as exc:
            errors.append(path.name + ': ' + type(exc).__name__)
    return count, not errors, errors


def run_nhl(endpoint, token, headless, timeout, health_output, pending_dir):
    """Keep NHL matching/cadence; save successes independently of any failure."""
    import nhl_schedule_collector as nhl
    from tools.free_live_collect import read_json, write_json
    from tools.free_live_nhl_exclusions import apply_exclusions
    pending_dir = Path(pending_dir)
    root = pending_dir.parent
    state_path = root / 'nhl-progress.json'
    state = read_json(state_path)
    backlog, done = state.setdefault('pending', {}), state.setdefault('completed', {})
    now = datetime.now(UTC)
    slot = nhl.half_hour_capture_slot(now)
    previous = state.get('last_slot') or read_json(root / 'completed-slot.json').get('slot')
    replayed, _, errors = nhl.replay_pending_snapshots(endpoint, token, pending_dir)
    report = dict(status='running', event_type='nhl', started_at=now.isoformat(),
                  capture_slot=slot.isoformat(),
                  captured=0, uploaded=0, duplicates=0, committed=0, failed=0,
                  replayed=replayed, errors=list(errors), uploads=[], unresolved=[],
                  no_longer_collectible=[], excluded_games=[])
    def checkpoint():
        report['pending'] = len(list(pending_dir.glob('*.json'))) + len(list(pending_dir.glob('*.rejected')))
        report['unfinished_games'] = sorted(backlog)
        write_json(state_path, state)
        write_json(health_output, report)
    checkpoint()
    try:
        schedule, sources = nhl.fetch_schedule_games(now)
    except Exception as exc:
        report['status'] = 'degraded'
        report['errors'].append('Schedule: ' + type(exc).__name__)
        checkpoint()
        return 1
    raw_schedule_count = len(schedule)
    schedule, report['excluded_games'] = apply_exclusions(schedule, backlog)
    scheduled = {str(g.schedule_id): g for g in schedule}
    for game in due_since(schedule, slot, previous, nhl.schedule_games_due):
        identity = str(game.schedule_id)
        if done.get(identity, {}).get('slot') == slot.isoformat():
            continue
        row = asdict(game)
        row['event_date'] = game.event_date.isoformat()
        first = backlog.get(identity, {}).get('first_due', slot.isoformat())
        backlog[identity] = {'game': row, 'first_due': first}
    work = []
    for identity, row in list(backlog.items()):
        game = scheduled.get(identity)
        if game is None:
            values = dict(row['game'])
            values['event_date'] = datetime.fromisoformat(values['event_date'])
            game = nhl.ScheduledNHLGame(**values)
        if not nhl.nhl_is_within_capture_window(game.event_date, now):
            report['no_longer_collectible'].append(identity)
            del backlog[identity]
        else:
            work.append(game)
    # Current due work comes first; recovery takes new observations, never backdated prices.
    work.sort(key=lambda g: (not nhl.nhl_capture_is_due(g.event_date, slot, g.schedule_id),
                             backlog[str(g.schedule_id)]['first_due'], g.event_date))
    report.update(scheduled_in_window=raw_schedule_count, scheduled_in_scope=len(schedule),
                  scheduled_due=len(work), schedule_sources=sources)
    state['last_slot'] = slot.isoformat()
    checkpoint()
    if work:
        try:
            feed, warnings = nhl.discover_nhl_games(headless, timeout)
        except Exception as exc:
            feed, warnings = [], ['Feed: ' + type(exc).__name__]
        resolutions, search_errors = nhl.resolve_schedule_games(work, feed, headless=headless, timeout=timeout)
        report['errors'].extend(warnings + search_errors)
    else:
        resolutions = []
    resolved = {str(r.game.schedule_id): r for r in resolutions}
    for game in work:
        identity = str(game.schedule_id)
        resolution = resolved.get(identity)
        if resolution is None or not resolution.candidates:
            report['unresolved'].append(identity)
            checkpoint()
            continue
        try:
            url, event_at, snapshot = nhl._capture_resolution(resolution, headless=headless, timeout=timeout)
            observed = datetime.now(UTC)
            if not nhl.nhl_is_within_capture_window(event_at, observed):
                raise ValueError('Event is no longer collectible')
            payload = nhl.nhl_snapshot_to_payload(url, event_at, observed, snapshot,
                         schedule=game.snapshot_metadata(snapshot.venue))
            pending = nhl.queue_snapshot(payload, pending_dir)
            report['captured'] += 1
            # No endpoint_available latch. EACH saved game gets a delivery attempt.
            response = nhl.post_snapshot_with_retry(endpoint, token, payload)
            if response.get('status') not in ('stored', 'duplicate'):
                raise ValueError('Unacknowledged game')
            done[identity] = {'slot': nhl.half_hour_capture_slot(observed).isoformat(),
                              'event_date': event_at.isoformat()}
            backlog.pop(identity, None)
            report['committed'] += 1
            report['uploaded' if response['status'] == 'stored' else 'duplicates'] += 1
            item = dict(schedule_id=identity, result=response['status'], sections=len(snapshot.sections),
                        captured_at=observed.isoformat())
            report['uploads'].append(item)
            checkpoint()
            pending.unlink(missing_ok=True)
            print('FREE_NHL_GAME ' + json.dumps(item), flush=True)
        except Exception as exc:
            report['failed'] += 1
            report['errors'].append(identity + ': ' + type(exc).__name__)
        checkpoint()
    state['completed'] = {key: row for key, row in done.items()
                          if datetime.fromisoformat(row['event_date']) > now - timedelta(days=1)}
    report['status'] = 'degraded' if backlog or report['pending'] or report['errors'] else 'healthy'
    report['finished_at'] = datetime.now(UTC).isoformat()
    checkpoint()
    print('FREE_NHL_RESULT ' + json.dumps(report), flush=True)
    return int(report['status'] != 'healthy')


def run(sport, directory):
    import collector
    import nfl_collector
    import nfl_schedule_collector as nfl
    import nhl_schedule_collector as nhl
    from tools import free_refresh_capture as storage
    from tools.free_refresh_cycle import run as cycle
    from tools.free_live_collect import run_parallel_nfl, read_json, write_json
    from tools.free_live_map_cache import cached_map_matching
    from tools.free_live_mlb_schedule import run_mlb
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    previous = read_json(root / 'nfl-committed.json').get('slot') if sport == 'nfl' else None
    select_due = nfl.schedule_games_due
    parser = preserved_parser(storage.parse_payload)
    with ExitStack() as patches:
        patches.enter_context(cached_map_matching())
        patches.enter_context(patch.object(collector, 'MIN_USABLE_SECTIONS', 1))
        patches.enter_context(patch.object(storage, 'parse_payload', parser))
        patches.enter_context(patch.object(collector, 'run_remote_collector', run_mlb))
        patches.enter_context(patch.object(nfl, 'run_schedule_collector', run_parallel_nfl))
        patches.enter_context(patch.object(nhl, 'run_schedule_collector', run_nhl))
        patches.enter_context(patch.object(nfl, 'schedule_games_due',
            lambda games, slot: due_since(games, slot, previous, select_due)))
        for module in (collector, nfl_collector, nfl, nhl):
            patches.enter_context(patch.object(module, 'replay_pending_snapshots',
                lambda endpoint, token, pending: replay_each(sport, endpoint, token, pending)))
        result = cycle(sport, root, force=True)
    health = read_json(root / 'health.json')
    if result == 0 and health.get('status') == 'healthy':
        write_json(root / 'completed-slot.json', {'sport': sport,
                   'slot': health.get('capture_slot', half_hour_capture_slot(datetime.now(UTC)).isoformat())})
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sport', choices=('mlb', 'nfl', 'nhl'), required=True)
    parser.add_argument('--directory', required=True)
    args = parser.parse_args()
    try:
        code = run(args.sport, args.directory)
    except Exception as exc:
        print('FREE_HARDENING_FAILED ' + type(exc).__name__, flush=True)
        code = 1
    raise SystemExit(code)
