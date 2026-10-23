"""Scheduled-only browser capture and durable delivery of collected observations."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import re
import time
from unittest.mock import patch

from Flask_App.collection_cadence import half_hour_capture_slot
from tools.historical_capture_conflicts import ArchivedCaptureConflict, preserve_pending

STATE_LIMIT = 1024**2
CAPTURE_ATTEMPT_LIMIT = 2
DELIVERY_RECOVERY_LIMIT = 20
DEADLINE_SECONDS = 24 * 60
PHASE_RESERVE_SECONDS = 180


def capture_progress(sport, schedule_id, status, report, elapsed, *, category=None, diagnostics=None, source_id=None):
    """Emit a bounded operational record; never serialize page or exception text."""
    phases = {'performer-navigation', 'performer-link-scan', 'event-link-ready', 'performer-ready-wait',
              'performer-overlay-controls', 'event-link-click', 'event-popup', 'event-domcontentloaded', 'event-reload',
              'inventory-wait', 'inventory-body', 'event-identity', 'complete'}
    reasons = {'pointer-intercepted', 'element-not-stable', 'outside-viewport', 'element-not-visible',
               'element-not-enabled', 'element-detached', 'navigation-wait', 'unclassified'}
    categories = {'capture-deferred-deadline', 'provider-access-denied', 'provider-rate-limited',
        'provider-authentication-required', 'provider-inventory-timeout', 'provider-inventory-not-found',
        'provider-server-error', 'provider-http-error', 'provider-recovery-failed', 'provider-event-unresolved', 'provider-gap',
        'unexpected-inventory-payload', 'inventory-identity-mismatch', 'empty-inventory', 'incomplete-inventory',
        'invalid-inventory-ticket', 'filtered-inventory-only', 'event-metadata-identity-mismatch',
        'event-metadata-time-mismatch', 'event-metadata-timeout', 'event-route-not-configured',
        'reload-event-identity-mismatch', 'event-navigation-identity-mismatch', 'event-link-not-found',
        'delivery-failed', 'archived-observation-conflict', 'capture-attempt-limit'}
    row = {'sport': sport if sport in ('nfl', 'nhl') else 'unknown',
           'status': status if status in ('started', 'stored', 'duplicate', 'failed', 'unresolved',
               'provider-gap', 'deferred', 'delivery-failed', 'archived-conflict', 'interrupted') else 'failed'}
    for key in ('captured', 'captured_current', 'uploaded', 'duplicates', 'failed', 'current_due', 'scheduled_selected'):
        if type(report.get(key)) is int and 0 <= report[key] <= 1000000:
            row[key] = report[key]
    if type(elapsed) in (int, float) and math.isfinite(elapsed) and 0 <= elapsed <= 86400:
        row['elapsed_seconds'] = round(elapsed, 3)
    if category is not None:
        row['category'] = category if isinstance(category, str) and category in categories else 'other-failure'
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    for key, value in (('schedule_id', schedule_id), ('source_id', source_id or diagnostics.get('production_id'))):
        if type(value) in (str, int) and re.fullmatch(r'[0-9]{1,12}', str(value)):
            row[key] = str(value)
    clean = {}
    for key in ('phase', 'timeout_phase'):
        if isinstance(diagnostics.get(key), str) and diagnostics[key] in phases:
            clean[key] = diagnostics[key]
    for key in ('event_link_trial_timeout_reason', 'event_link_click_timeout_reason'):
        if isinstance(diagnostics.get(key), str) and diagnostics[key] in reasons:
            clean[key] = diagnostics[key]
    trials = diagnostics.get('event_link_trial_attempts')
    if type(trials) is int and 1 <= trials <= 2:
        clean['event_link_trial_attempts'] = trials
    for key in ('performer_document_status', 'document_status'):
        value = diagnostics.get(key)
        if type(value) is int and 100 <= value <= 599:
            clean[key] = value
    state = diagnostics.get('event_link_actionability')
    if isinstance(state, dict):
        clean['event_link_actionability'] = {key: state[key] for key in
            ('attached', 'has_box', 'in_viewport', 'receives_pointer', 'obstructed_by_dialog')
            if type(state.get(key)) is bool}
    responses = diagnostics.get('responses')
    if isinstance(responses, list):
        clean['inventory_http_statuses'] = [value['status'] for value in responses[:10]
            if isinstance(value, dict) and type(value.get('status')) is int and 100 <= value['status'] <= 599]
    if isinstance(diagnostics.get('blocker_element'), dict):
        from vivid_webkit import safe_blocker_element
        clean['blocker_element'] = safe_blocker_element(diagnostics['blocker_element'])
    if 'performer_overlay_actions' in diagnostics:
        from vivid_webkit import safe_overlay_actions
        clean['performer_overlay_actions'] = safe_overlay_actions(diagnostics['performer_overlay_actions'])
    if diagnostics.get('blocker_diagnostic_status') in ('captured', 'unavailable', 'timeout', 'budget-exhausted', 'metadata-only'):
        clean['blocker_diagnostic_status'] = diagnostics['blocker_diagnostic_status']
    if clean:
        row['diagnostics'] = clean
    print('SHARED_CAPTURE_PROGRESS ' + json.dumps(row, sort_keys=True, allow_nan=False), flush=True)


class CaptureDeadlineReached(RuntimeError):
    category = 'capture-deferred-deadline'
    retryable = False


class CaptureAttemptLimitReached(RuntimeError):
    category = 'capture-attempt-limit'
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


def excluded_nhl(game):
    return (str(game.schedule_id) == '2026020182' and game.away_team == 'Montreal Canadiens'
            and game.home_team == 'Winnipeg Jets' and game.venue == 'Princess Auto Stadium')


def replay_pa(module, endpoint, token, pending, *, clock=time.monotonic, budget=120, mirror=None):
    """Bound old delivery work; every unattempted or rejected payload stays durable."""
    import collector
    started = clock(); count = 0; errors = []
    files = sorted({*pending.glob('*.json'), *pending.glob('*.rejected')})
    attempted = 0
    for path in files:
        if attempted >= DELIVERY_RECOVERY_LIMIT or clock() - started >= budget:
            break
        attempted += 1
        try:
            value = json.loads(path.read_bytes()); collector.snapshot_from_payload(value)
            module.post_snapshot_with_retry(endpoint, token, value, timeout=20, retry_delays=(2, 5))
            path.unlink(); count += 1
        except ArchivedCaptureConflict:
            if mirror is None:
                raise ValueError('Archived conflict requires its verified durable mirror')
            preserve_pending(path, mirror.read(mirror.enqueue(value)))
        except collector.SnapshotUploadError as exc:
            message = path.name + ': ' + type(exc).__name__
            if getattr(exc, 'receipt_validation_failed', False) is True:
                message += ': observation conflict or unverified receipt'
            errors.append(message)
            if not exc.retryable and exc.status_code in (400, 409, 422):
                path.replace(path.with_suffix('.rejected'))
        except Exception as exc:
            errors.append(path.name + ': ' + type(exc).__name__)
    return count, errors, len(files) - attempted


def run_owner(sport, module, mirror, endpoint, token, timeout, health_output, pending,
              *, legacy=None, now=None, clock=time.monotonic, deadline_seconds=DEADLINE_SECONDS,
              blocker_diagnostics=False, stop_after_first_failure=False,
              selected_slot=None, repair_schedule_ids=()):
    """Capture only games due now; a failed scrape never becomes later catch-up work."""
    if type(blocker_diagnostics) is not bool or type(stop_after_first_failure) is not bool:
        raise ValueError('Diagnostic options must be boolean')
    if stop_after_first_failure and not blocker_diagnostics:
        raise ValueError('Stopping after a failure requires explicit blocker diagnostics')
    now = now or (lambda: datetime.now(timezone.utc))
    started = now(); slot = half_hour_capture_slot(started); began = clock()
    if selected_slot is not None and selected_slot != slot.isoformat():
        raise ValueError('Capture slot changed after the final owner gate; do not scrape a different slot')
    if (not isinstance(repair_schedule_ids, (tuple, list)) or len(repair_schedule_ids) > 3
            or len(set(repair_schedule_ids)) != len(repair_schedule_ids)
            or any(not isinstance(v, str) or not v.isascii() or not v.isdigit() or len(v) != 10
                   for v in repair_schedule_ids) or repair_schedule_ids and sport != 'nhl'):
        raise ValueError('Manual NHL refresh requires up to three distinct official schedule IDs')
    # Finish browser work within the slot admitted by the final gate. The usual
    # phase reserve also leaves time for a capture already in progress to finish.
    deadline_seconds = min(deadline_seconds, max(0, (slot + timedelta(minutes=30) - started).total_seconds()))
    deadline = began + deadline_seconds
    path = mirror.root / 'recovery' / 'state.json'
    state = {'version': 1, 'sport': sport, 'pending': {}}
    state_writable = False
    report = dict(status='running', event_type=sport, mode='capture', captured=0, uploaded=0,
        duplicates=0, failed=0, replayed=0, pending=0, uploads=[], errors=[], captured_current=0,
        capture_slot=slot.isoformat(), started_at=started.isoformat(), deferred_games=[],
        deadline_seconds=deadline_seconds, deadline_reached=False, unresolved=[], provider_gaps=[], excluded_games=[],
        capture_failures=[], blocker_diagnostics=blocker_diagnostics, diagnostic_stopped=False,
        capture_policy='scheduled-only', capture_attempt_limit=CAPTURE_ATTEMPT_LIMIT)
    report['manual_refresh_schedule_ids'] = list(repair_schedule_ids)
    def save():
        report['pending'] = len(list(pending.glob('*.json'))) + len(list(pending.glob('*.rejected')))
        report['archived_pythonanywhere_conflicts'] = len(list(pending.glob('*.archived-conflict')))
        report['unfinished_games'] = sorted(state['pending'])
        report['seconds'] = round(clock() - began, 3)
        health_output.parent.mkdir(parents=True, exist_ok=True)
        health_output.write_text(json.dumps(report, indent=2) + '\n')
        if state_writable:
            # Keep unfinished games in this run's health report, never in a retry
            # backlog. Original observations and delivery receipts live separately.
            checkpoint(path, {**state, 'pending': {}}, mirror)
    def room():
        return deadline - clock() > PHASE_RESERVE_SECONDS
    try:
        if blocker_diagnostics:
            from vivid_webkit import BlockerScreenshotBudget
            diagnostic_directory = health_output.parent / (sport + '-blocker-diagnostics')
            if diagnostic_directory.is_symlink():
                raise ValueError('Blocker diagnostics require a job-local directory')
            diagnostic_directory.mkdir(parents=True, exist_ok=True)
            screenshot_budget = BlockerScreenshotBudget()
        restored = read_state(path)
        if restored and (restored.get('version') != 1 or restored.get('sport') != sport
                         or not isinstance(restored.get('pending'), dict)):
            raise ValueError('Recovered progress identifies a different sport')
        report['discarded_capture_backlog'] = len(restored.get('pending', {}))
        # Ignore old browser backlog/last-slot catch-up, including legacy imports.
        # Delivery replay below still preserves and sends every collected payload.
        state = {'version': 1, 'sport': sport, 'pending': {}, 'capture_policy': 'scheduled-only'}
        state_writable = True
        save()
        report['replayed'], replay_errors, report['deferred_uploads'] = replay_pa(
            module, endpoint, token, pending, clock=clock, mirror=mirror)
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
        is_due = getattr(module, sport+'_capture_is_due')
        in_window = getattr(module, sport+'_is_within_capture_window')
        covered = {(row['schedule_id'], row['capture_slot'], datetime.fromisoformat(row['event_date']))
                   for _, row in mirror.records()}
        def observed(game):
            return (str(game.schedule_id), slot.isoformat(), game.event_date) in covered
        if set(repair_schedule_ids) - {str(game.schedule_id) for game in schedule}:
            raise ValueError('Manual refresh game is not in the current official schedule window')
        raw_due = [game for game in schedule if is_due(game.event_date, slot, game.schedule_id)
                   or str(game.schedule_id) in repair_schedule_ids]
        report['no_longer_collectible'] = []
        current = []
        for game in raw_due:
            if not observed(game):
                if not in_window(game.event_date, started):
                    report['no_longer_collectible'].append(str(game.schedule_id))
                    continue
                current.append(game)
                state['pending'][str(game.schedule_id)] = {'first_due': slot.isoformat(), 'game': game_value(game)}
        current.sort(key=lambda game: (game.event_date, str(game.schedule_id)))
        work = current
        report.update(scheduled_in_window=raw_schedule_count, scheduled_in_scope=len(schedule), scheduled_due=len(current),
            scheduled_selected=len(work),
            current_due=len(raw_due), already_observed_current=sum(observed(game) for game in raw_due),
            current_due_selected=len(current), recovery_selected=0, schedule_source=source)
        state['last_slot'] = slot.isoformat(); save()
        feed = []
        if work and room():
            feed, warnings = getattr(module, 'discover_'+sport+'_games')(False, min(timeout, 20))
            report['errors'].extend(warnings)
        for index, game in enumerate(work):
            if report['diagnostic_stopped']:
                break
            identity = str(game.schedule_id)
            attempts = []
            capture_calls = 0
            if not room():
                report['deadline_reached'] = True
                report['deferred_games'].extend(str(row.schedule_id) for row in work[index:]); break
            state['pending'][identity]['last_attempt'] = now().isoformat(); save()
            outcome, outcome_category, outcome_diagnostics, source_id = 'interrupted', None, {}, None
            capture_progress(sport, identity, 'started', report, clock() - began)
            try:
                # Incremental resolution prevents older searches from delaying current captures.
                resolutions, errors = module.resolve_schedule_games([game], feed, headless=False, timeout=min(timeout, 20))
                report['errors'].extend(errors)
                resolution = next((row for row in resolutions if str(row.game.schedule_id) == identity), None)
                if resolution is None or not resolution.candidates:
                    outcome, outcome_category = 'unresolved', 'provider-event-unresolved'
                    report['unresolved'].append(identity)
                    if sport == 'nhl':
                        report['provider_gaps'].append(identity + ': no exact-date Vivid event was available')
                    else:
                        report['errors'].append(identity + ': unresolved official event')
                    save(); continue
                if not room():
                    outcome, outcome_category = 'deferred', CaptureDeadlineReached.category
                    report['deadline_reached'] = True
                    report['deferred_games'].extend(str(row.schedule_id) for row in work[index:]); break
                native_capture = module.VividNFLBrowser.capture
                native_init = module.VividNFLBrowser.__init__
                def bounded_init(browser, *args, **kwargs):
                    if not room():
                        attempts.append(dict(category=CaptureDeadlineReached.category,
                            error_type='CaptureDeadlineReached', capture_diagnostics={}))
                        raise CaptureDeadlineReached('No further browser starts inside this capture budget')
                    if capture_calls >= CAPTURE_ATTEMPT_LIMIT:
                        raise CaptureAttemptLimitReached('No further browser starts for this scheduled game')
                    result = native_init(browser, *args, **kwargs)
                    if blocker_diagnostics and getattr(browser, '_webkit_session', None) is not None:
                        browser._webkit_session.configure_blocker_diagnostics(
                            diagnostic_directory, screenshot_budget=screenshot_budget)
                    return result
                def traced_capture(browser, *args, **kwargs):
                    nonlocal capture_calls
                    try:
                        if not room():
                            raise CaptureDeadlineReached('No further provider requests inside this capture budget')
                        if capture_calls >= CAPTURE_ATTEMPT_LIMIT:
                            raise CaptureAttemptLimitReached('No further capture requests for this scheduled game')
                        capture_calls += 1
                        return native_capture(browser, *args, **kwargs)
                    except Exception as exc:
                        attempts.append(dict(category=getattr(exc, 'category', None), error_type=type(exc).__name__,
                            capture_diagnostics=dict(getattr(browser, 'capture_diagnostics', {}) or {})))
                        raise
                with patch.object(module.VividNFLBrowser, '__init__', bounded_init), \
                     patch.object(module.VividNFLBrowser, 'capture', traced_capture):
                    url, event_at, snapshot = module._capture_resolution(resolution, headless=False, timeout=timeout)
                source_id = getattr(snapshot, 'source_id', None)
                outcome_diagnostics = getattr(snapshot, 'capture_diagnostics', None) or {}
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
                    outcome = 'stored' if response['status'] == 'stored' else 'duplicate'
                    report['uploads'].append(dict(schedule_id=identity, source_id=value['source_id'],
                        captured_at=observed_at.isoformat(), sections=value['section_count'], result=response['status'],
                        title=snapshot.title, resolution_source=resolution.source,
                        inventory_listing_count=getattr(snapshot, 'inventory_listing_count', None),
                        capture_diagnostics=getattr(snapshot, 'capture_diagnostics', None)))
                except ArchivedCaptureConflict:
                    outcome, outcome_category = 'archived-conflict', 'archived-observation-conflict'
                    preserve_pending(queued, mirror.read(mirror.enqueue(value)))
                except Exception as exc:
                    outcome, outcome_category = 'delivery-failed', 'delivery-failed'
                    report['errors'].append(identity + ': delivery ' + type(exc).__name__)
            except Exception as exc:
                category = getattr(exc, 'category', None) or next((row['category'] for row in reversed(attempts)
                    if row['category']), None)
                outcome, outcome_category = 'failed', category or 'other-failure'
                if category == CaptureDeadlineReached.category:
                    outcome = 'deferred'
                    report['deadline_reached'] = True
                    report['deferred_games'].extend(str(row.schedule_id) for row in work[index:]); break
                elif sport == 'nhl' and isinstance(exc, module.NHLProviderGapError):
                    outcome, outcome_category = 'provider-gap', 'provider-gap'
                    report['provider_gaps'].append(identity + ': ' + type(exc).__name__)
                else:
                    report['failed'] += 1
                    report['errors'].append(identity + ': ' + type(exc).__name__ + (': '+category if category else ''))
                    report['capture_failures'].append(dict(schedule_id=identity, error_type=type(exc).__name__,
                        category=category, attempts=attempts[-16:]))
            finally:
                if not outcome_diagnostics and attempts:
                    outcome_diagnostics = next((row['capture_diagnostics'] for row in reversed(attempts)
                                                if row['capture_diagnostics']), {})
                capture_progress(sport, identity, outcome, report, clock() - began,
                    category=outcome_category, diagnostics=outcome_diagnostics, source_id=source_id)
                if stop_after_first_failure and outcome in ('failed', 'unresolved', 'provider-gap'):
                    report['diagnostic_stopped'] = True
                    report['deferred_games'].extend(str(row.schedule_id) for row in work[index+1:])
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
        report['pending'] = len(list(pending.glob('*.json'))) + len(list(pending.glob('*.rejected')))
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
            if attempted >= DELIVERY_RECOVERY_LIMIT or clock() - started >= budget:
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
