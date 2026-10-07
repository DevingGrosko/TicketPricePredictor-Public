"""NFL/NHL recovery used ONLY by the independent free-pipeline entry point.

Keep legacy collectors, identity validation, inventory parsing, cadence, queues,
transactions, and publication unchanged. Never retry access denials or pretend
that an unsuccessful capture is an empty/successful observation.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
from functools import partial
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
from unittest.mock import patch

from nfl_collector import VividNFLBrowser
from vivid_inventory import CurrentInventoryRecovery, VividCaptureError

# Capture the shared implementation before provider_recovery replaces the class
# method. Resolving it through the class inside capture() would recurse.
_SHARED_CAPTURE = VividNFLBrowser.capture

RETRY_DELAYS = (2, 5)


class ProviderCaptureError(RuntimeError):
    def __init__(self, category, diagnostics, *, retryable=False):
        self.category = category
        self.diagnostics = diagnostics
        self.retryable = retryable
        super().__init__(category + ': ' + json.dumps(diagnostics, sort_keys=True))


def safe_message(exc):
    # Browser messages sometimes contain URLs. Never log query strings, cookies,
    # headers, or complete provider response bodies.
    text = re.sub(r'https?://[^\s\"\']+', lambda m: urlsplit(m[0]).path, str(exc))
    return ' '.join(text.split())[:1200]


def retryable(exc):
    if isinstance(exc, (ProviderCaptureError, VividCaptureError)):
        return exc.retryable
    return isinstance(exc, TimeoutError) or type(exc).__name__ == 'TimeoutException'


def capture(browser, url, *, reload_page=False):
    """Delegate inventory validation and map extraction to the shared browser."""
    try:
        return _SHARED_CAPTURE(browser, url, **({'reload_page': True} if reload_page else {}))
    except VividCaptureError as exc:
        # Preserve the shared failure policy, including nonretryable 404s and
        # filtered/paginated inventory. The outer diagnostic wrapper can still
        # enrich this same browser diagnostic object before it is logged.
        diagnostics = getattr(browser, 'capture_diagnostics', exc.diagnostics)
        raise ProviderCaptureError(exc.category, diagnostics, retryable=exc.retryable) from exc


def capture_resolution(sport, resolution, *, headless, timeout, events=None):
    """Retry transport failures only, with fresh browsers and short backoff."""
    import nfl_schedule_collector as nfl
    import nhl_schedule_collector as nhl
    from collector import validated_vivid_url
    module = nfl if sport == 'nfl' else nhl
    parser = module.NFLSnapshotParser if sport == 'nfl' else module.NHLSnapshotParser
    errors = []
    recovery = CurrentInventoryRecovery(resolution.game.event_date, 7 * 24 if sport == 'nfl' else 72)
    for candidate in resolution.candidates:
        for attempt in range(1, len(RETRY_DELAYS) + 2):
            browser = None
            try:
                url = validated_vivid_url(candidate.url)
                browser = module.VividNFLBrowser(headless=headless, timeout=timeout)
                raw, provider_at = recovery.capture(browser, url)
                snapshot = parser.parse(raw)
                module.validate_captured_match(resolution.game, provider_at, snapshot.title)
                item = {'sport': sport, 'schedule_id': str(resolution.game.schedule_id),
                        'attempt': attempt, 'status': 'captured',
                        'diagnostics': getattr(browser, 'capture_diagnostics', {})}
                if events is not None:
                    events.append(item)
                print('FREE_PROVIDER_CAPTURE ' + json.dumps(item), flush=True)
                return url, resolution.game.event_date, snapshot
            except Exception as exc:
                item = {'sport': sport, 'schedule_id': str(resolution.game.schedule_id),
                        'attempt': attempt, 'status': 'failed', 'type': type(exc).__name__,
                        'category': getattr(exc, 'category', 'validation-or-capture-error'),
                        'message': safe_message(exc),
                        'diagnostics': getattr(exc, 'diagnostics', {}),
                        'will_retry': attempt <= len(RETRY_DELAYS) and retryable(exc)}
                errors.append(item)
                if events is not None:
                    events.append(item)
                print('FREE_PROVIDER_CAPTURE ' + json.dumps(item), flush=True)
                if not item['will_retry']:
                    break
            finally:
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
            time.sleep(RETRY_DELAYS[attempt - 1])
    raise ProviderCaptureError('game-capture-failed', {'attempts': errors})


@contextmanager
def provider_recovery():
    import nfl_collector as browser_module
    import nfl_schedule_collector as nfl
    import nhl_schedule_collector as nhl
    events = []
    with ExitStack() as stack:
        stack.enter_context(patch.object(browser_module.VividNFLBrowser, 'capture', capture))
        for sport, module in [('nfl', nfl), ('nhl', nhl)]:
            stack.enter_context(patch.object(module, '_capture_resolution',
                                            partial(capture_resolution, sport, events=events)))
        yield events


def run(sport, directory):
    if sport not in ('nfl', 'nhl'):
        raise ValueError('Only active NFL/NHL collectors use provider recovery; MLB is paused')
    from tools.free_live_hardening import run as hardened_run
    from tools.free_live_collect import read_json, write_json
    with provider_recovery() as events:
        try:
            return hardened_run(sport, directory)
        finally:
            health_path = Path(directory) / 'health.json'
            health = read_json(health_path)
            health['provider_recovery'] = {
                'attempts': len(events),
                'recovered_after_retry': sum(e['status'] == 'captured' and (
                    e['attempt'] > 1 or e.get('diagnostics', {}).get('inventory_recovery', {}).get('recovered')
                ) for e in events),
                'failed_attempts': [e for e in events if e['status'] == 'failed'],
            }
            write_json(health_path, health)
            print('FREE_PROVIDER_RECOVERY_RESULT ' + json.dumps(health['provider_recovery']), flush=True)


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--sport', choices=('nfl', 'nhl'), required=True)
    cli.add_argument('--directory', required=True)
    args = cli.parse_args()
    raise SystemExit(run(args.sport, args.directory))
