"""Independent collectors: partial success is durable and NFL uses two browsers.

Only the free staging entry point installs these adapters. Production collectors
are unchanged. Browser workers never share a driver or write to the database;
the coordinator queues each valid payload and delivers it in its own transaction.
"""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
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
    slot = nfl.hourly_capture_slot(started)
    pending_dir = Path(pending_dir)
    receipts_path = pending_dir.parent / 'nfl-committed.json'
    receipts = read_json(receipts_path)
    if receipts.get('slot') != slot.isoformat():
        receipts = {'slot': slot.isoformat(), 'completed': {}}
    completed = receipts['completed']
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
    due = nfl.schedule_games_due(schedule, slot)
    def key(game):
        return str(game.schedule_id) + '|' + game.event_date.isoformat()
    already = [game for game in due if key(game) in completed]
    remaining = [game for game in due if key(game) not in completed]
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
        'already_committed': len(already), 'captured': 0, 'uploaded': 0,
        'duplicates': 0, 'committed': len(already), 'failed': 0, 'deferred': 0,
        'unresolved_count': len(unresolved), 'unresolved': unresolved,
        'replayed': replayed, 'errors': list(queue_errors) + list(feed_errors) + list(search_errors),
        'uploads': []}
    def progress():
        report['pending'] = len(list(pending_dir.glob('*.json')))
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
            if time.monotonic() - clock > 14 * 60:
                report['deferred'] += 1 + sum(1 for _ in iterator)
                return
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
                    payload = nfl.nfl_snapshot_to_payload(url, event_date, slot, snapshot,
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
                    completed[key(game)] = {'iteration_id': response['iteration_id'], 'url': url}
                    write_json(receipts_path, receipts)
                    pending.unlink(missing_ok=True)
                    report['committed'] += 1
                    report['uploaded' if response['status'] == 'stored' else 'duplicates'] += 1
                    item = {'schedule_id': game.schedule_id, 'result': response['status'],
                        'iteration_id': response['iteration_id'], 'sections': len(snapshot.sections),
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
    slot = now.replace(minute=(0 if now.minute < 30 else 30) if sport == 'mlb' else 0,
                       second=0, microsecond=0).isoformat()
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
    # NFL/NHL retain their hourly adaptive tiers, without a minute-based skip.
    with patch.object(nfl, 'run_schedule_collector', run_parallel_nfl):
        code = capture_cycle(sport, root, force=True)
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
