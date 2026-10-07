"""Bounded GET-only verification of the exact freshly deployed public version."""
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.error import URLError
from urllib.request import Request, build_opener
from tools.check_public_pages import fixed_url, Redirects


def capture_freshness(catalogs, now=None):
    """Check every published active game, not just the newest sport timestamp."""
    from nfl_collector import nfl_capture_interval_hours
    from nhl_collector import nhl_capture_interval_hours
    now = now or datetime.now(timezone.utc)
    result = {}
    for sport, interval_for in [('nfl', nfl_capture_interval_hours), ('nhl', nhl_capture_interval_hours)]:
        active, stale = 0, []
        for identity, game in catalogs[sport].get('games', {}).items():
            event_at = datetime.fromisoformat(game['event_at'])
            interval = interval_for(event_at, now)
            if interval is None:
                continue
            active += 1
            stamp = game.get('captured_through')
            captured_at = datetime.fromisoformat(stamp) if stamp else None
            # Allow one missed tick plus build/trigger delay; never describe a
            # regenerated five-day-old catalog as a fresh collection.
            allowance = timedelta(hours=interval * 2, minutes=15)
            if captured_at is None or now - captured_at > allowance or captured_at > now + timedelta(minutes=5):
                stale.append({'game': identity, 'captured_through': stamp,
                              'expected_interval_minutes': int(interval * 60)})
        result[sport] = {'active_published_games': active, 'fresh_games': active - len(stale),
                         'stale_games': stale}
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
    report = {'generated_at': value['generated_at'], 'sports': freshness,
              'live_updates_enabled': value['live_updates_enabled'],
              'capture_freshness': capture_freshness(catalogs),
              'note': 'Per-sport maxima are not a claim of gap-free history.'}
    print('FREE_LIVE_PUBLICATION ' + json.dumps(report), flush=True)
    if any(sport['stale_games'] for sport in report['capture_freshness'].values()):
        raise RuntimeError('Published active games have overdue price captures; see per-game freshness report')
    return report


if __name__ == '__main__':
    verify(os.environ['EXPECTED_GENERATED_AT'])
