"""Bounded GET-only verification of the exact freshly deployed public version."""
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.error import URLError
from urllib.request import Request, build_opener
from tools.check_public_pages import fixed_url, Redirects


def capture_deadline(sport, event_at, captured_at, now, interval):
    """Give a faster tier time to start without forgiving old stale captures."""
    allowance = timedelta(hours=interval * 2, minutes=15)
    deadline = captured_at + allowance
    transitions = {'nfl': ((336, 6), (168, 3)),
                   'nhl': ((336, 24), (168, 12), (72, 6))}
    for lead_hours, previous_interval in transitions[sport]:
        boundary = event_at - timedelta(hours=lead_hours)
        previous_allowance = timedelta(hours=previous_interval * 2, minutes=15)
        if captured_at < boundary <= now and captured_at >= boundary - previous_allowance:
            deadline = max(deadline, boundary + allowance)
    return deadline


def first_capture_deadline(sport, scheduled):
    """A new scheduled game gets its first due phase plus one missed-tick grace."""
    from Flask_App.collection_cadence import half_hour_capture_slot
    from nfl_collector import nfl_capture_interval_hours, nfl_capture_is_due
    from nhl_collector import nhl_capture_interval_hours, nhl_capture_is_due
    interval_for, is_due = ((nfl_capture_interval_hours, nfl_capture_is_due) if sport == 'nfl'
                           else (nhl_capture_interval_hours, nhl_capture_is_due))
    entered = scheduled.event_date - timedelta(hours=720)
    slot = half_hour_capture_slot(entered)
    if slot < entered:
        slot += timedelta(minutes=30)
    # One daily phase covers the longest retained cadence. Use the same schedule
    # ID as collection; no synthetic backfilled observation is implied.
    for _ in range(49):
        if is_due(scheduled.event_date, slot, str(scheduled.schedule_id)):
            return slot + timedelta(hours=interval_for(scheduled.event_date, slot), minutes=15)
        slot += timedelta(minutes=30)
    raise ValueError('No first capture phase in a complete daily cadence')


def official_schedules(now):
    """Read public official schedules independently of successful price captures."""
    import nfl_schedule_collector as nfl
    import nhl_schedule_collector as nhl
    from tools.free_live_nhl_exclusions import apply_exclusions
    schedules, errors, excluded = {}, {}, {'nfl': set(), 'nhl': set()}
    for sport, module in [('nfl', nfl), ('nhl', nhl)]:
        try:
            def validated_fetch(url, timeout):
                payload = module.fetch_json(url, timeout)
                field = 'events' if sport == 'nfl' else 'gameWeek'
                if not isinstance(payload, dict) or not isinstance(payload.get(field), list):
                    raise ValueError('Official schedule response lacks the expected game collection')
                return payload
            games, _source = module.fetch_schedule_games(now, fetcher=validated_fetch)
            if sport == 'nhl':
                games, exclusions = apply_exclusions(games, {})
                excluded[sport] = {row['schedule_id'] for row in exclusions}
            schedules[sport] = games
        except Exception as exc:
            errors[sport] = 'Official ' + sport.upper() + ' schedule unavailable: ' + type(exc).__name__
    return schedules, errors, excluded


def capture_freshness(catalogs, now=None, schedules=None, schedule_errors=None, excluded=None):
    """Check published game freshness and independent scheduled-game coverage."""
    from nfl_collector import nfl_capture_interval_hours
    from nhl_collector import nhl_capture_interval_hours
    now = now or datetime.now(timezone.utc)
    schedule_errors, excluded = schedule_errors or {}, excluded or {}
    result = {}
    for sport, interval_for in [('nfl', nfl_capture_interval_hours), ('nhl', nhl_capture_interval_hours)]:
        active, stale = 0, []
        published_by_schedule = {}
        expected_ids = ({str(game.schedule_id) for game in schedules[sport]}
                        if schedules is not None and sport in schedules else None)
        for identity, game in catalogs[sport].get('games', {}).items():
            schedule_id = str(game.get('schedule_id') or '')
            if schedule_id:
                published_by_schedule.setdefault(schedule_id, []).append(game)
            if (schedule_id in excluded.get(sport, set())
                    or expected_ids is not None and schedule_id and schedule_id not in expected_ids):
                continue
            event_at = datetime.fromisoformat(game['event_at'])
            interval = interval_for(event_at, now)
            if interval is None:
                continue
            active += 1
            stamp = game.get('captured_through')
            captured_at = datetime.fromisoformat(stamp) if stamp else None
            if (captured_at is None or captured_at > now + timedelta(minutes=5)
                    or now > capture_deadline(sport, event_at, captured_at, now, interval)):
                stale.append({'game': identity, 'captured_through': stamp,
                              'expected_interval_minutes': int(interval * 60)})
        coverage = {'status': 'not-checked', 'expected_scheduled_games': None,
                    'missing_due_games': [], 'awaiting_first_capture': []}
        if sport in schedule_errors:
            coverage.update(status='unavailable', warning=schedule_errors[sport])
        elif schedules is not None and sport in schedules:
            coverage.update(status='verified', expected_scheduled_games=len(schedules[sport]))
            for scheduled in schedules[sport]:
                published = published_by_schedule.get(str(scheduled.schedule_id), [])
                if any(datetime.fromisoformat(game['event_at']) == scheduled.event_date
                       for game in published):
                    continue
                deadline = first_capture_deadline(sport, scheduled)
                entry = {'schedule_id': str(scheduled.schedule_id),
                         'event_at': scheduled.event_date.isoformat(),
                         'first_capture_deadline': deadline.isoformat()}
                coverage['missing_due_games' if now > deadline else 'awaiting_first_capture'].append(entry)
            if coverage['missing_due_games']:
                coverage['status'] = 'incomplete'
        result[sport] = {'active_published_games': active, 'fresh_games': active - len(stale),
                         'stale_games': stale, 'schedule_coverage': coverage}
    return result


def verify(expected):
    opener = build_opener(Redirects())
    def read(path):
        with opener.open(Request(fixed_url(path), headers={'Cache-Control': 'no-cache'}), timeout=20) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('Oversized public metadata')
        if path.lstrip('/').startswith(('native/', 'data/')) and hashlib.sha256(raw).hexdigest() not in path:
            raise ValueError('Public data hash mismatch')
        return json.loads(raw)
    for attempt in range(8):
        try:
            value = read('/original-manifest.json')
            if value.get('generated_at') == expected:
                break
        except URLError:
            pass
        if attempt == 7:
            raise RuntimeError('Expected publication not available at public URL')
        time.sleep(15)
    catalogs = {sport: read(path) for sport, path in value['sports'].items()}
    freshness = {sport: catalog['captured_through'] for sport, catalog in catalogs.items()}
    if set(freshness) != {'mlb', 'nfl', 'nhl'}:
        raise ValueError('Missing sport catalog')
    now = datetime.now(timezone.utc)
    schedules, schedule_errors, excluded = official_schedules(now)
    report = {'generated_at': value['generated_at'], 'sports': freshness,
              'live_updates_enabled': value['live_updates_enabled'],
              'capture_freshness': capture_freshness(catalogs, now, schedules, schedule_errors, excluded),
              'note': 'Per-sport maxima are not a claim of gap-free history.'}
    print('FREE_LIVE_PUBLICATION ' + json.dumps(report), flush=True)
    if schedule_errors:
        print('FREE_LIVE_COVERAGE_WARNING ' + json.dumps(schedule_errors), flush=True)
        raise RuntimeError('Official schedule coverage unavailable; price coverage could not be verified')
    if any(sport['stale_games'] or sport['schedule_coverage']['missing_due_games']
           for sport in report['capture_freshness'].values()):
        raise RuntimeError('Eligible games have overdue or missing price captures; see per-game freshness and coverage report')
    return report


if __name__ == '__main__':
    verify(os.environ['EXPECTED_GENERATED_AT'])
