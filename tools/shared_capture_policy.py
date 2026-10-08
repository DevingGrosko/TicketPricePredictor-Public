"""Bounded single-owner recovery and delivery-only migration of public free state."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import time
from unittest.mock import patch

from Flask_App.collection_cadence import half_hour_capture_slot

STATE_LIMIT = 1024**2
RECOVERY_LIMIT = 20
DEADLINE_SECONDS = 24 * 60
PHASE_RESERVE_SECONDS = 180


class CaptureDeadlineReached(RuntimeError):
    category = 'capture-deferred-deadline'
    retryable = False


def read_state(path):
    path = Path(path)
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file() or path.stat().st_size > STATE_LIMIT:
        raise ValueError('Invalid bounded public collector progress')
    value = json.loads(path.read_bytes())
    from tools.shared_capture import public_only
    public_only(value)
    if not isinstance(value, dict):
        raise ValueError('Collector progress must be an object')
    return value


def checkpoint(path, value, mirror):
    from tools.shared_capture import encoded
    data = encoded(value)
    if len(data) > STATE_LIMIT:
        raise ValueError('Collector progress exceeds its bound')
    # Include the nested progress file and reserved future receipt bytes.
    reserved = sum(1024 * sum(ack is None for ack in row['acknowledged'].values())
                   for _, row in mirror.records())
    used = sum(p.stat().st_size for p in mirror.root.rglob('*') if p.is_file() and p != path)
    if used + len(data) + reserved > mirror.byte_limit:
        raise ValueError('Shared progress and pending observations exceed their byte budget')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(data); temporary.replace(path)


def game_value(game):
    result = asdict(game); result['event_date'] = game.event_date.isoformat()
    return result


def game_from(module, sport, row):
    value = dict(row); value['event_date'] = datetime.fromisoformat(value['event_date'])
    if value['event_date'].tzinfo is None:
        raise ValueError('Recovered official game requires an aware event date')
    return getattr(module, 'Scheduled'+sport.upper()+'Game')(**value)


def migrate_progress(state, legacy, sport):
    """Import identities once; files and original captured payloads stay untouched."""
    if state.get('legacy_migrated') or legacy is None:
        return
    root = Path(legacy)
    old = read_state(root / ('nfl-backlog.json' if sport == 'nfl' else 'nhl-progress.json'))
    for identity, entry in old.get('pending', {}).items():
        state['pending'].setdefault(str(identity), entry)
    prior = read_state(root / ('nfl-committed.json' if sport == 'nfl' else 'completed-slot.json'))
    state.setdefault('last_slot', old.get('last_slot') or prior.get('slot'))
    if old or prior:
        state['legacy_migrated'] = True


def due_since(schedule, slot, previous, select_due):
    start = slot
    if previous:
        previous = half_hour_capture_slot(datetime.fromisoformat(previous))
        start = min(slot, max(slot - timedelta(hours=23, minutes=30), previous + timedelta(minutes=30)))
    selected = {}
    while start <= slot:
        for game in select_due(schedule, start):
            selected[str(game.schedule_id)] = game
        start += timedelta(minutes=30)
    return list(selected.values())


def excluded_nhl(game):
    return (str(game.schedule_id) == '2026020182' and game.away_team == 'Montreal Canadiens'
            and game.home_team == 'Winnipeg Jets' and game.venue == 'Princess Auto Stadium')


def replay_pa(module, endpoint, token, pending, *, clock=time.monotonic, budget=120):
    """Bound old delivery work; every unattempted or rejected payload stays durable."""
    import collector
    started = clock(); count = 0; errors = []
    files = sorted({*pending.glob('*.json'), *pending.glob('*.rejected')})
    attempted = 0
    for path in files:
        if attempted >= RECOVERY_LIMIT or clock() - started >= budget:
            break
        attempted += 1
        try:
            value = json.loads(path.read_bytes()); collector.snapshot_from_payload(value)
            module.post_snapshot_with_retry(endpoint, token, value, timeout=20, retry_delays=(2, 5))
            path.unlink(); count += 1
        except collector.SnapshotUploadError as exc:
            errors.append(path.name + ': ' + type(exc).__name__)
            if not exc.retryable and exc.status_code in (400, 409, 422):
                path.replace(path.with_suffix('.rejected'))
        except Exception as exc:
            errors.append(path.name + ': ' + type(exc).__name__)
    return count, errors, len(files) - attempted


def run_owner(sport, module, mirror, endpoint, token, timeout, health_output, pending,
              *, legacy=None, now=None, clock=time.monotonic, deadline_seconds=DEADLINE_SECONDS):
    """Reuse trusted collection functions; resolve/capture current games before older work."""
    now = now or (lambda: datetime.now(timezone.utc))
    started = now(); slot = half_hour_capture_slot(started); began = clock()
    deadline = began + deadline_seconds
    path = mirror.root / 'recovery' / 'state.json'
    state = {'version': 1, 'sport': sport, 'pending': {}}
    state_writable = False
    report = dict(status='running', event_type=sport, mode='capture', captured=0, uploaded=0,
        duplicates=0, failed=0, replayed=0, pending=0, uploads=[], errors=[], captured_current=0,
        capture_slot=slot.isoformat(), started_at=started.isoformat(), deferred_games=[],
        deadline_seconds=deadline_seconds, deadline_reached=False, unresolved=[], provider_gaps=[], excluded_games=[],
        capture_failures=[])
    def save():
        report['pending'] = len(list(pending.glob('*.json'))) + len(list(pending.glob('*.rejected')))
        report['unfinished_games'] = sorted(state['pending'])
        report['seconds'] = round(clock() - began, 3)
        health_output.parent.mkdir(parents=True, exist_ok=True)
        health_output.write_text(json.dumps(report, indent=2) + '\n')
        if state_writable:
            checkpoint(path, state, mirror)
    def room():
        return deadline - clock() > PHASE_RESERVE_SECONDS
    try:
        restored = read_state(path)
        if restored and (restored.get('version') != 1 or restored.get('sport') != sport
                         or not isinstance(restored.get('pending'), dict)):
            raise ValueError('Recovered progress identifies a different sport')
        state = restored or state
        migrate_progress(state, legacy, sport)
        state_writable = True
        save()
        report['replayed'], replay_errors, report['deferred_uploads'] = replay_pa(
            module, endpoint, token, pending, clock=clock)
        report['errors'].extend(replay_errors)
        schedule, source = module.fetch_schedule_games(started)
        raw_schedule_count = len(schedule)
        # Preserve the already approved free-scope exemption for this exact event.
        if sport == 'nhl':
            excluded = [game for game in schedule if excluded_nhl(game)]
            report['excluded_games'] = [game_value(game) for game in excluded]
            for game in excluded:
                state['pending'].pop(str(game.schedule_id), None)
            schedule = [game for game in schedule if game not in excluded]
        official = {str(game.schedule_id): game for game in schedule}
        is_due = getattr(module, sport+'_capture_is_due')
        in_window = getattr(module, sport+'_is_within_capture_window')
        covered = {(row['schedule_id'], row['capture_slot'], datetime.fromisoformat(row['event_date']))
                   for _, row in mirror.records()}
        def observed(game):
            return (str(game.schedule_id), slot.isoformat(), game.event_date) in covered
        raw_due = [game for game in schedule if is_due(game.event_date, slot, game.schedule_id)]
        for game in due_since(schedule, slot, state.get('last_slot'), module.schedule_games_due):
            if not observed(game):
                identity = str(game.schedule_id); old = state['pending'].get(identity, {})
                state['pending'][identity] = {**old, 'first_due': old.get('first_due', slot.isoformat()),
                                            'game': game_value(game)}
        active = []
        report['no_longer_collectible'] = []
        for identity, entry in list(state['pending'].items()):
            game = official.get(identity) or game_from(module, sport, entry['game'])
            if sport == 'nhl' and excluded_nhl(game):
                report['excluded_games'].append(game_value(game)); del state['pending'][identity]
            elif not in_window(game.event_date, started):
                report['no_longer_collectible'].append(identity); del state['pending'][identity]
            elif observed(game):
                del state['pending'][identity]
            else:
                entry['game'] = game_value(game); active.append(game)
        current = [game for game in active if is_due(game.event_date, slot, game.schedule_id)]
        older = [game for game in active if game not in current]
        current.sort(key=lambda game: (game.event_date, str(game.schedule_id)))
        older.sort(key=lambda game: (state['pending'][str(game.schedule_id)].get('last_attempt') or '',
            state['pending'][str(game.schedule_id)]['first_due'], game.event_date, str(game.schedule_id)))
        recovery, unselected = older[:RECOVERY_LIMIT], older[RECOVERY_LIMIT:]
        work = current + recovery
        report.update(scheduled_in_window=raw_schedule_count, scheduled_in_scope=len(schedule), scheduled_due=len(active),
            scheduled_selected=len(work),
            current_due=len(raw_due), already_observed_current=sum(observed(game) for game in raw_due),
            current_due_selected=len(current), recovery_selected=len(recovery),
            deferred_games=[str(game.schedule_id) for game in unselected], schedule_source=source)
        state['last_slot'] = slot.isoformat(); save()
        feed = []
        if work and room():
            feed, warnings = getattr(module, 'discover_'+sport+'_games')(False, min(timeout, 20))
            report['errors'].extend(warnings)
        for index, game in enumerate(work):
            identity = str(game.schedule_id)
            attempts = []
            if not room():
                report['deadline_reached'] = True
                report['deferred_games'].extend(str(row.schedule_id) for row in work[index:]); break
            state['pending'][identity]['last_attempt'] = now().isoformat(); save()
            try:
                # Incremental resolution prevents older searches from delaying current captures.
                resolutions, errors = module.resolve_schedule_games([game], feed, headless=False, timeout=min(timeout, 20))
                report['errors'].extend(errors)
                resolution = next((row for row in resolutions if str(row.game.schedule_id) == identity), None)
                if resolution is None or not resolution.candidates:
                    report['unresolved'].append(identity)
                    if sport == 'nhl':
                        report['provider_gaps'].append(identity + ': no exact-date Vivid event was available')
                    else:
                        report['errors'].append(identity + ': unresolved official event')
                    save(); continue
                if not room():
                    report['deadline_reached'] = True
                    report['deferred_games'].extend(str(row.schedule_id) for row in work[index:]); break
                native_capture = module.VividNFLBrowser.capture
                native_init = module.VividNFLBrowser.__init__
                def bounded_init(browser, *args, **kwargs):
                    if not room():
                        attempts.append(dict(category=CaptureDeadlineReached.category,
                            error_type='CaptureDeadlineReached', capture_diagnostics={}))
                        raise CaptureDeadlineReached('No further browser starts inside this capture budget')
                    return native_init(browser, *args, **kwargs)
                def traced_capture(browser, *args, **kwargs):
                    try:
                        if not room():
                            raise CaptureDeadlineReached('No further provider requests inside this capture budget')
                        return native_capture(browser, *args, **kwargs)
                    except Exception as exc:
                        attempts.append(dict(category=getattr(exc, 'category', None), error_type=type(exc).__name__,
                            capture_diagnostics=dict(getattr(browser, 'capture_diagnostics', {}) or {})))
                        raise
                with patch.object(module.VividNFLBrowser, '__init__', bounded_init), \
                     patch.object(module.VividNFLBrowser, 'capture', traced_capture):
                    url, event_at, snapshot = module._capture_resolution(resolution, headless=False, timeout=timeout)
                observed_at = now()
                if not in_window(event_at, observed_at):
                    raise ValueError('Captured event is no longer within its collection window')
                value = getattr(module, sport+'_snapshot_to_payload')(url, event_at, observed_at, snapshot,
                    schedule=game.snapshot_metadata(snapshot.venue))
                queued = module.queue_snapshot(value, pending)
                report['captured'] += 1
                report['captured_current'] += game in current
                del state['pending'][identity]  # A durable original payload replaces browser retry work.
                save()
                try:
                    response = module.post_snapshot_with_retry(endpoint, token, value, timeout=20, retry_delays=(2, 5))
                    queued.unlink(missing_ok=True)
                    report['uploaded' if response['status'] == 'stored' else 'duplicates'] += 1
                    report['uploads'].append(dict(schedule_id=identity, source_id=value['source_id'],
                        captured_at=observed_at.isoformat(), sections=value['section_count'], result=response['status'],
                        title=snapshot.title, resolution_source=resolution.source,
                        inventory_listing_count=getattr(snapshot, 'inventory_listing_count', None),
                        capture_diagnostics=getattr(snapshot, 'capture_diagnostics', None)))
                except Exception as exc:
                    report['errors'].append(identity + ': delivery ' + type(exc).__name__)
            except Exception as exc:
                category = getattr(exc, 'category', None) or next((row['category'] for row in reversed(attempts)
                    if row['category']), None)
                if category == CaptureDeadlineReached.category:
                    report['deadline_reached'] = True
                    report['deferred_games'].extend(str(row.schedule_id) for row in work[index:]); break
                elif sport == 'nhl' and isinstance(exc, module.NHLProviderGapError):
                    report['provider_gaps'].append(identity + ': ' + type(exc).__name__)
                else:
                    report['failed'] += 1
                    report['errors'].append(identity + ': ' + type(exc).__name__ + (': '+category if category else ''))
                    report['capture_failures'].append(dict(schedule_id=identity, error_type=type(exc).__name__,
                        category=category, attempts=attempts[-16:]))
            save()
    except BaseException as exc:
        report['errors'].append('Owner: ' + type(exc).__name__)
        if not isinstance(exc, Exception):
            raise
    finally:
        report['finished_at'] = now().isoformat()
        report['provider_gap_count'] = len(report['provider_gaps'])
        report['unresolved_count'] = len(report['unresolved'])
        report['provider_supported_expected'] = max(0, report.get('scheduled_due', 0) - report['provider_gap_count'])
        report['provider_supported_coverage_percent'] = (round(100 * report['captured'] /
            report['provider_supported_expected'], 2) if report['provider_supported_expected'] else None)
        report['status'] = 'degraded' if state['pending'] or report['errors'] or report['pending'] else 'healthy'
        report['coverage_percent'] = (round(100 * (report['captured'] + report.get('already_observed_current', 0)) /
            (report.get('scheduled_due', 0) + report.get('already_observed_current', 0)), 2)
            if report.get('scheduled_due', 0) + report.get('already_observed_current', 0) else None)
        report['current_coverage_percent'] = (round(100 * (report['captured_current'] +
            report.get('already_observed_current', 0)) / report['current_due'], 2)
            if report.get('current_due') else None)
        save()
    return int(report['status'] != 'healthy')


def replay_free_pending(sport, directory, *, sender=None, now=None, clock=time.monotonic, budget=120):
    """TiDB-only replay of old public payloads; never calls a browser or PA endpoint."""
    from tools.import_shared_snapshot import deliver_tidb
    from tools import free_refresh_capture as storage
    from tools.shared_capture import public_only
    import collector
    if os.environ.get('COLLECTOR_INGEST_TOKEN'):
        raise RuntimeError('Legacy free replay requires a separate TiDB credential environment')
    now = now or (lambda: datetime.now(timezone.utc))
    root = Path(directory); pending = root / 'pending'
    errors = []; delivered = 0; changed = False; started = clock(); attempted = 0
    original = storage.parse_payload
    def preserved(kind, value, now=None):
        stamp = datetime.fromisoformat(str(value.get('captured_at', '')).replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp > (now or datetime.now(timezone.utc)) + timedelta(minutes=5):
            raise ValueError('Invalid original legacy observation timestamp')
        for row in value.get('sections') or []:
            price, count = Decimal(str(row['price'])), Decimal(str(row['listing_count']))
            if not price.is_finite() or price <= 0 or not count.is_finite() or count < 1 or count != int(count):
                raise ValueError('Invalid legacy price or listing count')
        return original(kind, value, now=stamp)
    with patch.object(storage, 'parse_payload', preserved), patch.object(collector, 'MIN_USABLE_SECTIONS', 1):
        for path in sorted({*pending.glob('*.json'), *pending.glob('*.rejected')}):
            if attempted >= RECOVERY_LIMIT or clock() - started >= budget:
                break
            attempted += 1
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024**2:
                    raise ValueError('Invalid bounded legacy pending payload')
                value = json.loads(path.read_bytes()); public_only(value)
                # Exact original fields are validated at observation time, not backdated to now.
                storage.parse_payload(sport, value, now=now())
                receipt = (sender or deliver_tidb)(value)
                if (receipt.get('event_type') != sport or receipt.get('source_id') != value['source_id']
                        or receipt.get('observed_at') != value['captured_at']
                        or receipt.get('price_readback_verified') is not True
                        or receipt.get('identity_readback_verified') is not True):
                    raise ValueError('Legacy pending destination readback did not match')
                path.unlink(); delivered += 1; changed = True
            except Exception as exc:
                errors.append(path.name + ': ' + type(exc).__name__)
    return dict(delivered=delivered, errors=errors, changed=changed,
                pending=len(list(pending.glob('*.json'))) + len(list(pending.glob('*.rejected'))))
