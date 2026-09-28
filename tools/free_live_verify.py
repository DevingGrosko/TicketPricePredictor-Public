"""Bounded GET-only verification of the exact freshly deployed public version."""
import hashlib
import json
import os
import time
from urllib.error import URLError
from urllib.request import Request, build_opener
from tools.check_public_pages import fixed_url, Redirects


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
    freshness = {sport: read(path)['captured_through'] for sport, path in value['sports'].items()}
    if set(freshness) != {'mlb', 'nfl', 'nhl'}:
        raise ValueError('Missing sport catalog')
    report = {'generated_at': value['generated_at'], 'sports': freshness,
              'live_updates_enabled': value['live_updates_enabled'],
              'note': 'Per-sport maxima are not a claim of gap-free history.'}
    print('FREE_LIVE_PUBLICATION ' + json.dumps(report), flush=True)
    return report


if __name__ == '__main__':
    verify(os.environ['EXPECTED_GENERATED_AT'])
