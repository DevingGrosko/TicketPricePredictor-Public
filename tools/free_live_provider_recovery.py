"""NFL/NHL recovery used ONLY by the independent free-pipeline entry point.

Keep legacy collectors, identity validation, inventory parsing, cadence, queues,
transactions, and publication unchanged. Never retry access denials or pretend
that an unsuccessful capture is an empty/successful observation.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager, ExitStack
from functools import partial
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
from unittest.mock import patch

RETRY_DELAYS = (2, 5)
MAX_BODY_BYTES = 16 * 1024 * 1024


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
    if isinstance(exc, ProviderCaptureError):
        return exc.retryable
    return isinstance(exc, TimeoutError) or type(exc).__name__ == 'TimeoutException'


def is_listings(url):
    path = urlsplit(url).path.casefold()
    return 'listings' in path and '/badging/' not in path


def response_payload(driver, request_id):
    result = driver.execute_cdp_cmd('Network.getResponseBody', {'requestId': request_id})
    body = result.get('body', '')
    if len(body) > MAX_BODY_BYTES * 2:
        raise ValueError('Oversized listings body')
    if result.get('base64Encoded'):
        body = base64.b64decode(body, validate=True).decode('utf-8')
    if len(body.encode('utf-8')) > MAX_BODY_BYTES:
        raise ValueError('Oversized listings body')
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError('Listings response is not a JSON object')
    return value


def capture(browser, url):
    """Read completed responses reliably; preserve the existing map extraction."""
    import nfl_collector as n
    from collector import event_metadata_is_still_rendering, validated_vivid_url
    url = validated_vivid_url(url)
    expected_id = urlsplit(url).path.rstrip('/').split('/')[-1]
    started = time.monotonic()
    driver = browser.driver
    diagnostics = {'production_id': expected_id, 'responses': [], 'body_read_retries': 0}
    browser.capture_diagnostics = diagnostics
    # Larger explicit payload buffers prevent large inventories from being
    # evicted while metadata/maps finish rendering. No cache/auth bypass.
    driver.execute_cdp_cmd('Network.enable', {
        'maxTotalBufferSize': 32 * 1024 * 1024,
        'maxResourceBufferSize': MAX_BODY_BYTES,
    })
    driver.get_log('performance')
    try:
        driver.get(url)
    except Exception as exc:
        if type(exc).__name__ != 'TimeoutException':
            raise
        # Keep the partially loaded page running: stopping it would abort the
        # listings request that this method still needs to observe.
        diagnostics['navigation_timeout'] = True
    deadline = time.monotonic() + browser.timeout
    requests = {}
    map_bodies = []
    captured_payload = None
    event_date = None
    listings_ready_at = None
    map_view_opened = False

    while time.monotonic() < deadline:
        for entry in driver.get_log('performance'):
            try:
                message = json.loads(entry['message'])['message']
                method, params = message['method'], message['params']
            except (KeyError, TypeError, ValueError):
                continue
            identity = str(params.get('requestId') or '')
            if method == 'Network.responseReceived':
                response = params.get('response') or {}
                response_url = str(response.get('url') or '')
                mime = str(response.get('mimeType') or '')
                listing = is_listings(response_url)
                if params.get('type') == 'Document':
                    diagnostics['document_status'] = response.get('status')
                if not listing and not browser._looks_like_map_response(response_url, mime):
                    continue
                item = {'url': response_url, 'mime': mime, 'listing': listing,
                        'status': response.get('status', 0), 'next_read': 0.0, 'attempts': 0}
                requests[identity] = item
                if listing:
                    diagnostics['responses'].append({'path': urlsplit(response_url).path,
                                                    'status': item['status']})
                    status = item['status']
                    if status in (401, 403, 429):
                        headers = response.get('headers') or {}
                        diagnostics['retry_after'] = next((str(v)[:80] for k, v in headers.items()
                                                          if k.casefold() == 'retry-after'), None)
                        raise ProviderCaptureError('rate-limited' if status == 429 else 'access-denied', diagnostics)
                    if status >= 500:
                        raise ProviderCaptureError('provider-server-error', diagnostics, retryable=True)
            elif method == 'Network.loadingFailed' and identity in requests:
                requests[identity]['network_error'] = str(params.get('errorText') or '')[:160]

        # A single getResponseBody failure used to discard the only completion
        # event. Retry LOCAL body reads until the same request's body is ready.
        # Also handle responseReceived/loadingFinished arriving across log polls.
        for identity, item in list(requests.items()):
            if time.monotonic() < item['next_read']:
                continue
            if not 200 <= item['status'] < 300:
                del requests[identity]
                continue
            item['attempts'] += 1
            item['next_read'] = time.monotonic() + min(2.0, 0.25 * item['attempts'])
            if not item['listing']:
                body = browser._response_text(identity)
                if body:
                    map_bodies.append((body, item['mime'], item['url']))
                    del requests[identity]
                continue
            try:
                payload = response_payload(driver, identity)
            except Exception as exc:
                diagnostics['body_read_retries'] += 1
                diagnostics['last_body_error'] = safe_message(exc)
                continue
            del requests[identity]
            metadata = payload.get('global') or []
            tickets = payload.get('tickets')
            if not metadata or not isinstance(metadata[0], dict) or not isinstance(tickets, list):
                diagnostics['unexpected_payload_keys'] = list(payload)[:12]
                continue
            if str(metadata[0].get('productionId') or '') != expected_id:
                diagnostics['rejected_production_id'] = str(metadata[0].get('productionId') or '')[:80]
                continue
            if not tickets:
                raise ProviderCaptureError('empty-inventory', diagnostics)
            captured_payload = payload
            listings_ready_at = listings_ready_at or time.monotonic()

        if event_date is None:
            try:
                event_date = browser._event_datetime(url)
            except Exception as exc:
                if not event_metadata_is_still_rendering(exc):
                    raise
                diagnostics['metadata_wait'] = type(exc).__name__

        if captured_payload is not None and event_date is not None:
            known_sections = sorted({
                ' '.join(str(ticket.get('l') or '').split())
                for ticket in captured_payload.get('tickets') or []
                if isinstance(ticket, dict) and str(ticket.get('l') or '').strip()
            }, key=str.casefold)
            candidates = [n.extract_map_geometry_from_json(captured_payload, known_sections,
                           source='vivid-listings-json', source_url=url)]
            for body, mime, response_url in map_bodies:
                candidates.append(browser._geometry_from_response(body, mime, response_url, known_sections))
            candidates.append(browser._dom_map_geometry(known_sections, url))
            geometry = n.choose_best_geometry(candidates, known_sections)
            usable = n.geometry_is_usable(geometry, known_sections)
            elapsed = time.monotonic() - listings_ready_at
            if usable or elapsed >= n.MAP_GEOMETRY_SETTLE_SECONDS:
                if geometry is not None:
                    captured_payload['_map_geometry'] = geometry
                captured_payload['_map_geometry_diagnostics'] = {
                    'status': 'captured' if usable else ('partial' if geometry is not None else 'unavailable'),
                    'source': geometry.get('source') if geometry else None,
                    'mapped_sections': n.geometry_section_count(geometry),
                    'coverage_ratio': geometry.get('coverage_ratio') if geometry else 0,
                    'network_map_responses': len(map_bodies), 'map_view_opened': map_view_opened,
                }
                diagnostics['seconds'] = round(time.monotonic() - started, 3)
                return captured_payload, event_date
            if not map_view_opened and elapsed >= 0.5:
                map_view_opened = browser._open_map_view()
        time.sleep(0.15)

    if captured_payload is not None and event_date is not None:
        return captured_payload, event_date
    diagnostics['seconds'] = round(time.monotonic() - started, 3)
    diagnostics['pending_requests'] = [
        {'path': urlsplit(item['url']).path, 'status': item['status'],
         'read_attempts': item['attempts'], 'network_error': item.get('network_error')}
        for item in requests.values() if item['listing']
    ]
    try:
        diagnostics['page_title'] = driver.title[:180]
    except Exception:
        pass
    if diagnostics.get('document_status') in (401, 403, 429):
        raise ProviderCaptureError('document-access-denied', diagnostics)
    category = 'metadata-timeout' if captured_payload is not None else 'listings-timeout'
    raise ProviderCaptureError(category, diagnostics, retryable=True)


def capture_resolution(sport, resolution, *, headless, timeout, events=None):
    """Retry transport failures only, with fresh browsers and short backoff."""
    import nfl_schedule_collector as nfl
    import nhl_schedule_collector as nhl
    from collector import validated_vivid_url
    module = nfl if sport == 'nfl' else nhl
    parser = module.NFLSnapshotParser if sport == 'nfl' else module.NHLSnapshotParser
    errors = []
    for candidate in resolution.candidates:
        for attempt in range(1, len(RETRY_DELAYS) + 2):
            browser = None
            try:
                url = validated_vivid_url(candidate.url)
                browser = module.VividNFLBrowser(headless=headless, timeout=timeout)
                raw, provider_at = browser.capture(url)
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
                'recovered_after_retry': sum(e['status'] == 'captured' and e['attempt'] > 1 for e in events),
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
