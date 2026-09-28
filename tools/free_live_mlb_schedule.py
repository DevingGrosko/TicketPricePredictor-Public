"""League-wide MLB discovery from official schedule; never trust URL dates.

Only exact, verified game identities are delivered. No source/production writes.
The existing 72-hour collection horizon and exclusion of preseason remain.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json
import re
import time
import unicodedata

UTC = timezone.utc


def normalized(value):
    value = unicodedata.normalize('NFKD', str(value)).encode('ascii', 'ignore').decode().casefold()
    value = re.sub(r'[^a-z0-9]+', ' ', value).strip()
    return value.replace('oakland athletics', 'athletics').replace('sacramento athletics', 'athletics')


def schedule_games(payload, now):
    if not isinstance(payload, dict) or not isinstance(payload.get('dates'), list):
        raise ValueError('Malformed MLB schedule')
    games = {}
    for day in payload['dates']:
        for game in day.get('games', []):
            if game.get('gameType') not in ('R', 'F', 'D', 'L', 'W'):
                continue
            if game.get('status', {}).get('abstractGameState') != 'Preview':
                continue
            at = datetime.fromisoformat(game['gameDate'].replace('Z', '+00:00'))
            if at.tzinfo is None:
                raise ValueError('Ambiguous official game time')
            if not timedelta(0) < at - now <= timedelta(hours=72):
                continue
            identity = str(game['gamePk'])
            away = game['teams']['away']['team']['name']
            home = game['teams']['home']['team']['name']
            if not identity.isdigit() or not away or not home:
                raise ValueError('Incomplete MLB identity')
            games[identity] = dict(schedule_id=identity, event_date=at.isoformat(),
                                   away_team=away, home_team=home,
                                   venue=game.get('venue', {}).get('name', ''))
    return sorted(games.values(), key=lambda g: (g['event_date'], g['schedule_id']))


def fetch_schedule(now):
    query = urlencode(dict(sportId=1, startDate=(now-timedelta(days=1)).date().isoformat(),
                           endDate=(now+timedelta(days=3)).date().isoformat(), hydrate='team,venue'))
    url = 'https://statsapi.mlb.com/api/v1/schedule?' + query
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={'User-Agent': 'TicketSignal/1.0'}), timeout=20) as response:
                raw = response.read(4*1024**2 + 1)
            if len(raw) > 4*1024**2:
                raise ValueError('Oversized MLB schedule')
            return schedule_games(json.loads(raw), now), url
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def validate_match(game, url, raw, provider_at):
    import collector as mlb
    snapshot = mlb.SnapshotParser.parse(raw)
    source = re.search(r'/production/(\d+)', url)
    if source is None or source[1] != snapshot.source_id:
        raise ValueError('Provider returned the wrong production')
    title = normalized(snapshot.title)
    away, home = normalized(game['away_team']), normalized(game['home_team'])
    a, h = title.find(away), title.find(home)
    if min(a, h) < 0 or a >= h:
        raise ValueError('Provider teams or home/away order do not match')
    official = datetime.fromisoformat(game['event_date'])
    if provider_at.tzinfo is None or abs((provider_at-official).total_seconds()) > 90*60:
        raise ValueError('Provider start does not match official schedule')
    return snapshot, official


def capture_game(game, headless, timeout, known_url=None):
    import collector as mlb
    class MetadataBrowser(mlb.VividBrowser):
        def _event_datetime(self, _url):
            # Retain structured page/DOM metadata, disable legacy URL fallback.
            return super()._event_datetime('')
        def capture(self, url):
            # With page_load_strategy=none, old event metadata can otherwise
            # survive briefly while a new candidate's listings arrive.
            self.driver.get('about:blank')
            return super().capture(url)
    browser = None
    candidates = [known_url] if known_url else []
    errors = []
    try:
        browser = MetadataBrowser(headless=headless, timeout=timeout)
        for phase in ('known', 'search'):
            if phase == 'search':
                at = datetime.fromisoformat(game['event_date'])
                query = f"{game['away_team']} at {game['home_team']} {at:%B %d}"
                search = 'https://www.vividseats.com/search?' + urlencode({'searchTerm': query})
                candidates = sorted(browser.discover_event_urls(search) - set(candidates))
            for url in candidates:
                try:
                    url = mlb.validated_vivid_url(url)
                    raw, provider_at = browser.capture(url)
                    snapshot, official = validate_match(game, url, raw, provider_at)
                    return url, official, snapshot
                except Exception as exc:
                    errors.append(type(exc).__name__)
    finally:
        if browser is not None:
            try: browser.close()
            except Exception: pass
    if errors and all(e in ('TimeoutError', 'TimeoutException') for e in errors):
        raise TimeoutError('Provider pages timed out')
    raise RuntimeError('No verified provider game: ' + ','.join(sorted(set(errors))))


def run_mlb(endpoint, token, headless, timeout, health_output, pending_dir):
    import collector as mlb
    from tools.free_live_collect import read_json, write_json
    root = Path(pending_dir).parent
    state_path = root/'mlb-schedule-progress.json'
    state = read_json(state_path)
    backlog, done = state.setdefault('pending', {}), state.setdefault('completed', {})
    now = datetime.now(UTC)
    slot = now.replace(minute=(now.minute//30)*30, second=0, microsecond=0).isoformat()
    replayed, _, errors = mlb.replay_pending_snapshots(endpoint, token, Path(pending_dir))
    report = dict(status='running', event_type='mlb', coverage_mode='official-MLB-schedule',
                  started_at=now.isoformat(), capture_slot=slot, captured=0, uploaded=0,
                  duplicates=0, committed=0, failed=0, replayed=replayed, errors=list(errors),
                  limited_inventory_captures=[], uploads=[], no_longer_collectible=[])
    def checkpoint():
        report['pending'] = len(list(Path(pending_dir).glob('*.json'))) + len(list(Path(pending_dir).glob('*.rejected')))
        report['unfinished_games'] = sorted(backlog)
        write_json(state_path, state)
        write_json(health_output, report)
    checkpoint()
    try:
        games, source = fetch_schedule(now)
    except Exception as exc:
        report['status'] = 'degraded'
        report['errors'].append('MLB schedule: ' + type(exc).__name__)
        checkpoint()
        return 1
    for game in games:
        identity = game['schedule_id']
        if done.get(identity, {}).get('slot') != slot:
            backlog[identity] = game
    for identity, game in list(backlog.items()):
        if datetime.fromisoformat(game['event_date']) <= now:
            report['no_longer_collectible'].append(identity)
            del backlog[identity]
    report.update(scheduled_in_window=len(games), scheduled_due=len(backlog), schedule_source=source,
                  already_committed=len(games)-sum(g['schedule_id'] in backlog for g in games))
    checkpoint()
    for game in sorted(list(backlog.values()), key=lambda g: g['event_date']):
        identity = game['schedule_id']
        for attempt in range(2):
            try:
                url, at, snapshot = capture_game(game, headless, timeout, done.get(identity, {}).get('url'))
                observed = datetime.now(UTC)
                if not timedelta(0) < at-observed <= timedelta(hours=72):
                    raise ValueError('Game is no longer inside collection window')
                payload = mlb.snapshot_to_payload(url, at, observed, snapshot)
                path = mlb.queue_snapshot(payload, Path(pending_dir))
                report['captured'] += 1
                result = mlb.post_snapshot_with_retry(endpoint, token, payload)
                if result.get('status') not in ('stored', 'duplicate'):
                    raise ValueError('Unacknowledged MLB game')
                done[identity] = dict(slot=observed.replace(minute=(observed.minute//30)*30,
                                        second=0, microsecond=0).isoformat(), url=url, event_date=at.isoformat())
                del backlog[identity]
                report['committed'] += 1
                report['uploaded' if result['status'] == 'stored' else 'duplicates'] += 1
                if len(snapshot.sections) < 10:
                    report['limited_inventory_captures'].append(identity)
                item = dict(schedule_id=identity, result=result['status'], sections=len(snapshot.sections),
                            captured_at=observed.isoformat(), url=url)
                report['uploads'].append(item)
                checkpoint()
                path.unlink(missing_ok=True)
                print('FREE_MLB_GAME ' + json.dumps(item), flush=True)
                break
            except Exception as exc:
                if attempt == 0 and (isinstance(exc, TimeoutError) or type(exc).__name__ == 'TimeoutException'):
                    continue
                report['failed'] += 1
                report['errors'].append(identity + ': ' + type(exc).__name__)
                break
        checkpoint()
    state['completed'] = {key: value for key, value in done.items()
                          if datetime.fromisoformat(value['event_date']) > now-timedelta(days=1)}
    report['status'] = 'degraded' if backlog or report['pending'] or report['errors'] else 'healthy'
    report['finished_at'] = datetime.now(UTC).isoformat()
    checkpoint()
    print('FREE_MLB_RESULT ' + json.dumps(report), flush=True)
    return int(report['status'] != 'healthy')
