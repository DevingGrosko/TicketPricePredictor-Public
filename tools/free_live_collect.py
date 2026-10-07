"""Independent collectors: partial success is durable and NFL uses two browsers.

Only the free staging entry point installs these adapters. Production collectors
are unchanged. Browser workers never share a driver or write to the database;
the coordinator queues each valid payload and delivers it in its own transaction.
"""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from dataclasses import asdict
import json
from pathlib import Path
import time
from unittest.mock import patch

SPORTS = ('mlb', 'nfl', 'nhl')


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, allow_nan=False, indent=2) + '\n')
    temporary.replace(path)


def read_json(path):
    path = Path(path)
    if not path.exists():
        return {}
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ValueError('Invalid collector state file')
    result = json.loads(path.read_text())
    if not isinstance(result, dict):
        raise ValueError('Invalid collector state')
    return result


def capture_one(resolution, headless, timeout):
    import nfl_schedule_collector as nfl
    started = time.monotonic()
    url, event_date, snapshot = nfl._capture_resolution(
        resolution, headless=headless, timeout=timeout)
    if not nfl.nfl_is_within_capture_window(event_date, datetime.now(timezone.utc)):
        raise ValueError('Captured event is outside its permitted window')
    return url, event_date, snapshot, round(time.monotonic() - started, 3)


def run_parallel_nfl(endpoint, token, headless, timeout, health_output, pending_dir,
                     *, workers=2):
    import nfl_schedule_collector as nfl
    if workers not in (1, 2):
        raise ValueError('Only one or two independent browser workers are allowed')
    started = datetime.now(timezone.utc)
    clock = time.monotonic()
    slot = nfl.half_hour_capture_slot(started)
    pending_dir = Path(pending_dir)
    receipts_path = pending_dir.parent / 'nfl-committed.json'
    receipts = read_json(receipts_path)
    previous_slot = receipts.get('slot')
    completed = receipts.setdefault('completed', {})
    # Upgrade the old hourly receipts without forgetting what was saved.
    for entry in completed.values():
        entry.setdefault('captured_at', previous_slot)
    backlog_path = pending_dir.parent / 'nfl-backlog.json'
    backlog = read_json(backlog_path)
    pending_games = backlog.setdefault('pending', {})
    backlog['version'] = 1

    replayed, _available, queue_errors = nfl.replay_pending_snapshots(endpoint, token, pending_dir)
    # Receipt cache is only an optimization. TiDB's duplicate-slot check remains
    # authoritative when the cache is missing or a commit preceded a crash.
    try:
        schedule, schedule_source = nfl.fetch_schedule_games(started)
    except Exception as exc:
        write_json(health_output, {'status': 'degraded', 'event_type': 'nfl',
            'started_at': started.isoformat(), 'capture_slot': slot.isoformat(),
            'errors': ['Schedule unavailable: ' + type(exc).__name__],
            'replayed': replayed, 'pending': len(list(pending_dir.glob('*.json')))})
        return 1
    def key(game):
        return str(game.schedule_id) + '|' + game.event_date.isoformat()

    def done_this_slot(game):
        entry = completed.get(key(game), {})
        stamp = entry.get('captured_at')
        return bool(stamp and nfl.half_hour_capture_slot(datetime.fromisoformat(stamp)) == slot)

    def remember(game, first_due):
        identity = str(game.schedule_id)
        row = asdict(game)
        row['event_date'] = game.event_date.isoformat()
        previous = pending_games.get(identity, {})
        pending_games[identity] = {
            'first_due': previous.get('first_due', first_due), 'game': row,
        }

    # Recover the previous version's deferred games even when activation falls
    # in a new slot. Those old prices cannot be recreated: retry captures NOW.
    if not backlog.get('migrated_receipts') and previous_slot:
        for game in nfl.schedule_games_due(schedule, datetime.fromisoformat(previous_slot)):
            if key(game) not in completed:
                remember(game, previous_slot)
    backlog['migrated_receipts'] = True
    current_due = nfl.schedule_games_due(schedule, slot)
    for game in current_due:
        if not done_this_slot(game):
            remember(game, slot.isoformat())

    # Keep work independent of the cadence slot. Refresh rescheduled games from
    # the current schedule, but do not silently delete unresolved future games.
    scheduled = {str(game.schedule_id): game for game in schedule}
    carried, expired = [], []
    for identity, item in list(pending_games.items()):
        game = scheduled.get(identity)
        if game is None:
            row = dict(item['game'])
            row['event_date'] = datetime.fromisoformat(row['event_date'])
            game = nfl.ScheduledNFLGame(**row)
        if not nfl.nfl_is_within_capture_window(game.event_date, started):
            expired.append(identity)
            del pending_games[identity]
            continue
        remember(game, item['first_due'])
        if done_this_slot(game):
            del pending_games[identity]
        else:
            carried.append(game)
    # Newly due observations precede older slow-tier retries. No historical prices are recreated.
    carried.sort(key=lambda game: (
        not nfl.nfl_capture_is_due(game.event_date, slot, game.schedule_id),
        pending_games[str(game.schedule_id)]['first_due'], game.event_date, str(game.schedule_id)))
    already = [game for game in current_due if done_this_slot(game)]
    remaining = carried
    due = already + remaining
    write_json(backlog_path, backlog)  # Checkpoint before any provider requests.
    receipts['slot'] = slot.isoformat()
    write_json(receipts_path, receipts)
    if remaining:
        feed, feed_errors = nfl.discover_nfl_games(headless, timeout)
        resolutions, search_errors = nfl.resolve_schedule_games(
            remaining, feed, headless=headless, timeout=timeout)
    else:
        resolutions, feed_errors, search_errors = [], [], []
    # Fail explicitly rather than silently skipping schedule identities omitted
    # by a resolver; a failed game must not stop the other resolutions.
    by_key = {}
    for resolution in resolutions:
        identity = key(resolution.game)
        if identity in by_key:
            raise ValueError('Duplicate resolved game identity')
        by_key[identity] = resolution
    unresolved = [key(game) for game in remaining
                  if key(game) not in by_key or not by_key[key(game)].candidates]
    work = [by_key[key(game)] for game in remaining if key(game) not in unresolved]
    report = {'status': 'running', 'event_type': 'nfl', 'started_at': started.isoformat(),
        'capture_slot': slot.isoformat(), 'workers': workers, 'schedule_source': schedule_source,
        'scheduled_in_window': len(schedule), 'scheduled_due': len(due),
        'current_cadence_due': len(current_due), 'carried_forward': len([g for g in remaining
            if pending_games[str(g.schedule_id)]['first_due'] < slot.isoformat()]),
        'no_longer_collectible': expired,
        'already_committed': len(already), 'captured': 0, 'uploaded': 0,
        'duplicates': 0, 'committed': len(already), 'failed': 0, 'deferred': 0,
        'unresolved_count': len(unresolved), 'unresolved': unresolved,
        'replayed': replayed, 'errors': list(queue_errors) + list(feed_errors) + list(search_errors),
        'uploads': []}
    def progress():
        report['pending'] = len(list(pending_dir.glob('*.json')))
        report['unfinished_games'] = sorted(pending_games)
        write_json(backlog_path, backlog)
        report['coverage_percent'] = round(100 * report['committed'] / len(due), 2) if due else 100.0
        report['seconds'] = round(time.monotonic() - clock, 3)
        write_json(health_output, report)
    progress()
    iterator = iter(work)
    # At most two snapshots in flight. A slow game cannot hold the other
    # worker's successful results behind an ordered executor.map iterator.
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='free-nfl') as pool:
        futures = {}
        def submit_next():
            try:
                resolution = next(iterator)
            except StopIteration:
                return
            # Drain every game. A wall-clock budget must not discard work.
            futures[pool.submit(capture_one, resolution, headless, timeout)] = resolution
        for _ in range(workers):
            submit_next()
        while futures:
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                resolution = futures.pop(future)
                game = resolution.game
                try:
                    url, event_date, snapshot, capture_seconds = future.result()
                    # A carried-over game is a new observation, never a backdated price.
                    observed_at = datetime.now(timezone.utc)
                    payload = nfl.nfl_snapshot_to_payload(url, event_date, observed_at, snapshot,
                        schedule=game.snapshot_metadata(snapshot.venue))
                    pending = nfl.queue_snapshot(payload, pending_dir)
                    report['captured'] += 1
                    commit_start = time.monotonic()
                    # Try EACH payload even after another game's failed write.
                    # Delivery errors retain the queue entry; a later game may
                    # succeed after a transient connection or validation error.
                    response = nfl.post_snapshot_with_retry(endpoint, token, payload)
                    if response.get('status') not in ('stored', 'duplicate'):
                        raise RuntimeError('Unacknowledged staging write')
                    completed[key(game)] = {'iteration_id': response['iteration_id'], 'url': url,
                        'captured_at': observed_at.isoformat()}
                    pending_games.pop(str(game.schedule_id), None)
                    write_json(receipts_path, receipts)
                    pending.unlink(missing_ok=True)
                    report['committed'] += 1
                    report['uploaded' if response['status'] == 'stored' else 'duplicates'] += 1
                    item = {'schedule_id': game.schedule_id, 'result': response['status'],
                        'iteration_id': response['iteration_id'], 'sections': len(snapshot.sections),
                        'captured_at': observed_at.isoformat(),
                        'capture_seconds': capture_seconds,
                        'commit_seconds': round(time.monotonic() - commit_start, 3)}
                    report['uploads'].append(item)
                    print('FREE_NFL_GAME ' + json.dumps(item), flush=True)
                except Exception as exc:
                    report['failed'] += 1
                    report['errors'].append(str(game.schedule_id) + ': ' + type(exc).__name__)
                    print('FREE_NFL_GAME_FAILED ' + str(game.schedule_id) + ' ' + type(exc).__name__, flush=True)
                progress()
                submit_next()
    report['status'] = 'healthy' if (
        report['committed'] == len(due) and not report['errors'] and not report['pending']
    ) else 'degraded'
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    progress()
    print('FREE_NFL_RESULT ' + json.dumps(report), flush=True)
    return 0 if report['status'] == 'healthy' else 1


def run(sport, directory):
    from tools.free_refresh_cycle import run as capture_cycle
    import nfl_schedule_collector as nfl
    if sport not in SPORTS:
        raise ValueError('Unknown sport')
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    slot = nfl.half_hour_capture_slot(now).isoformat()
    state = read_json(root / 'completed-slot.json')
    if (state.get('sport') == sport and state.get('slot') == slot
            and not list((root / 'pending').glob('*.json'))):
        write_json(root / 'health.json', {'status': 'not-due', 'event_type': sport,
            'evaluated_at': now.isoformat(), 'capture_slot': slot,
            'reason': 'This slot was already completed; no pending uploads.'})
        return 0
    write_json(root / 'health.json', {'status': 'running', 'event_type': sport,
        'started_at': now.isoformat(), 'capture_slot': slot})
    # Evaluate the actual current slot even if a cron was delayed past :30.
    # NFL/NHL evaluate both half-hour slots and retain the slower adaptive tiers.
    from contextlib import ExitStack
    import collector
    from tools.free_live_mlb import run_remote_mlb
    from tools.free_live_map_cache import cached_map_matching
    with ExitStack() as patches:
        map_cache = patches.enter_context(cached_map_matching())
        patches.enter_context(patch.object(nfl, 'run_schedule_collector', run_parallel_nfl))
        if sport == 'mlb':
            patches.enter_context(patch.object(collector, 'run_remote_collector', run_remote_mlb))
        code = capture_cycle(sport, root, force=True)
        print('FREE_MAP_CACHE ' + json.dumps(map_cache.cache_info()._asdict()), flush=True)
    health = read_json(root / 'health.json')
    if code == 0 and health.get('status') == 'healthy':
        write_json(root / 'completed-slot.json', {'sport': sport, 'slot': slot})
    return code


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sport', choices=SPORTS, required=True)
    parser.add_argument('--directory', required=True)
    args = parser.parse_args()
    try:
        result = run(args.sport, args.directory)
    except Exception as exc:
        print('FREE_LIVE_COLLECT_FAILED ' + type(exc).__name__, flush=True)
        result = 1
    raise SystemExit(result)
