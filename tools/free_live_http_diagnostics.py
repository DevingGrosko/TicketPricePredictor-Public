"""Observe NFL/NHL HTTP failures without changing capture or retry policy.

Only allowlisted metadata and recognized error phrases leave the browser. Never
persist raw bodies, complete URLs, query strings, request headers, or cookies.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import re
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

MAX_ERROR_BYTES = 64 * 1024
MAX_REQUESTS = 12
PHRASES = (
    'not found', 'no tickets', 'no listings', 'no inventory', 'sold out',
    'invalid production', 'invalid request', 'missing parameter', 'bad request',
    'access denied', 'request blocked', 'forbidden', 'unauthorized',
    'rate limit', 'too many requests', 'temporarily unavailable',
    'captcha', 'bot detected', 'enable javascript', 'enable cookies',
    'cloudflare', 'akamai', 'incapsula', 'imperva', 'perimeterx', 'datadome',
)
PARAMETERS = {'productionId', 'productionid', 'production_id', 'eventId', 'page', 'pageSize', 'limit', 'offset', 'quantity'}
MARKER_HEADERS = {'cf-ray', 'x-amz-cf-id', 'x-iinfo', 'x-datadome', 'x-px-block', 'x-sucuri-block'}


def body_summary(body: str, encoded: bool = False) -> dict:
    if not isinstance(body, str) or len(body) > MAX_ERROR_BYTES * 2:
        return {'body': 'omitted-size-limit'}
    try:
        raw = base64.b64decode(body, validate=True) if encoded else body.encode('utf-8')
    except (ValueError, UnicodeError):
        return {'body': 'omitted-invalid-encoding'}
    if len(raw) > MAX_ERROR_BYTES:
        return {'body': 'omitted-size-limit'}
    text = raw.decode('utf-8', errors='replace')
    lower = text.casefold()
    # Reconstruct from a fixed public vocabulary, never return arbitrary text.
    result = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
              'recognized_phrases': [p for p in PHRASES if p in lower]}
    try:
        value = json.loads(text)
    except ValueError:
        result['format'] = 'html' if re.search(r'<(?:!doctype|html|head|body)\b', lower) else 'text'
        return result
    result['format'] = 'json'
    if isinstance(value, dict):
        result['known_fields'] = sorted(set(value) & {'error', 'message', 'code', 'status', 'errorCode', 'errors', 'detail', 'title'})
        for key in ('code', 'status', 'errorCode'):
            item = value.get(key)
            if type(item) is int and 100 <= item <= 599:
                result[key] = item
        result['recognized_error_fields'] = {
            k: [p for p in PHRASES if p in str(value[k]).casefold()]
            for k in ('message', 'error', 'detail', 'title') if k in value
        }
    return result


def safe_headers(headers: dict) -> dict:
    result = {}
    for name, value in (headers or {}).items():
        key = str(name).casefold()
        text = str(value).strip()
        if key in MARKER_HEADERS:
            result[key + '-present'] = True
        elif key in ('age', 'retry-after') and re.fullmatch(r'\d{1,7}', text):
            result[key] = text
        elif key == 'content-type':
            mime = text.split(';', 1)[0].casefold()
            if mime in {'application/json', 'text/html', 'text/plain', 'application/problem+json'}:
                result[key] = mime
        elif key == 'server':
            labels = [v for v in ('nginx', 'cloudflare', 'envoy', 'amazons3', 'apache', 'akamaighost', 'varnish') if v in text.casefold()]
            result[key] = labels or ['other']
        elif key == 'cache-control':
            result[key] = re.findall(r'\b(?:no-store|no-cache|private|public|must-revalidate|(?:s-maxage|max-age)=\d+)\b', text.casefold())[:8]
        elif key == 'x-cache':
            result[key] = [v for v in ('hit', 'miss', 'error', 'cloudfront') if v in text.casefold()]
        elif key == 'x-amzn-errortype':
            result[key] = text if text in {'NotFoundException', 'MissingAuthenticationTokenException', 'ForbiddenException', 'TooManyRequestsException', 'InternalServerErrorException'} else 'other'
    return result


class TracedDriver:
    """Proxy Chrome log reads; HTTP requests themselves are untouched."""
    def __init__(self, driver):
        self.driver = driver
        self.active = False
        self.requests = {}
        self.notes = []

    def __getattr__(self, name):
        return getattr(self.driver, name)

    def get(self, url):
        self.active = True
        return self.driver.get(url)

    def refresh(self):
        self.active = True
        return self.driver.refresh()

    def get_log(self, kind):
        entries = self.driver.get_log(kind)
        if self.active and kind == 'performance':
            try:
                self.observe(entries)
                self.read_errors()
            except Exception as exc:
                self.notes.append(type(exc).__name__)
        return entries

    def observe(self, entries):
        for entry in entries:
            try:
                message = json.loads(entry['message'])['message']
                method, p = message['method'], message['params']
                rid = str(p.get('requestId') or '')
                if method in ('Network.requestWillBeSent', 'Network.responseReceived'):
                    source = p.get('request') if method == 'Network.requestWillBeSent' else p.get('response')
                    source = source or {}
                    url = urlsplit(source.get('url', ''))
                    if url.hostname not in {'www.vividseats.com', 'vividseats.com'} or url.path not in {'/hermes/api/v1/listings', '/hermes/api/v2/listings'}:
                        continue
                    if rid not in self.requests:
                        if len(self.requests) >= MAX_REQUESTS:
                            continue
                        self.requests[rid] = {'host': url.hostname, 'path': url.path, 'error_reads': 0}
                    item = self.requests[rid]
                    if method == 'Network.requestWillBeSent':
                        item['method'] = source.get('method') if source.get('method') in ('GET', 'POST', 'OPTIONS') else 'other'
                        item['query'] = {k: v[0] for k, v in parse_qs(url.query).items()
                                         if k in PARAMETERS and len(v) == 1 and re.fullmatch(r'\d{1,12}', v[0])}
                        item['has_post_data'] = bool(source.get('hasPostData') or source.get('postData'))
                    else:
                        item.update(status=source.get('status'), headers=safe_headers(source.get('headers')),
                                    disk_cache=bool(source.get('fromDiskCache')), service_worker=bool(source.get('fromServiceWorker')))
                elif rid in self.requests and method == 'Network.loadingFinished':
                    self.requests[rid]['finished'] = True
                elif rid in self.requests and method == 'Network.loadingFailed':
                    self.requests[rid]['network_failed'] = True
            except (ValueError, TypeError, KeyError, AttributeError):
                continue

    def read_errors(self):
        for rid, item in self.requests.items():
            if not (400 <= item.get('status', 0) < 600 and item.get('finished')):
                continue
            if 'error_body' in item or item['error_reads'] >= 3:
                continue
            item['error_reads'] += 1
            try:
                response = self.driver.execute_cdp_cmd('Network.getResponseBody', {'requestId': rid})
                item['error_body'] = body_summary(response.get('body', ''), response.get('base64Encoded', False))
            except Exception as exc:
                item['body_read_error'] = type(exc).__name__

    def execute_cdp_cmd(self, command, params):
        result = self.driver.execute_cdp_cmd(command, params)
        rid = str(params.get('requestId') or '')
        item = self.requests.get(rid)
        if command == 'Network.getResponseBody' and item and 200 <= item.get('status', 0) < 300:
            try:
                body = result.get('body', '')
                if result.get('base64Encoded'):
                    body = base64.b64decode(body).decode('utf-8')
                value = json.loads(body)
                item['success_body'] = {'json_object': isinstance(value, dict)}
                if isinstance(value, dict):
                    tickets = value.get('tickets')
                    item['success_body']['ticket_count'] = len(tickets) if isinstance(tickets, list) else None
                    metadata = value.get('global') or []
                    pid = str(metadata[0].get('productionId', '')) if metadata and isinstance(metadata[0], dict) else ''
                    if re.fullmatch(r'\d{1,12}', pid):
                        item['success_body']['production_id'] = pid
            except Exception as exc:
                self.notes.append(type(exc).__name__)
        return result

    def report(self):
        return {'requests': list(self.requests.values()), 'diagnostic_errors': self.notes[:5]}


@contextmanager
def http_diagnostics():
    from tools import free_live_provider_recovery as recovery
    original = recovery.capture

    def capture(browser, url, *, reload_page=False):
        driver = browser.driver
        traced = TracedDriver(driver)
        browser.driver = traced
        try:
            return original(browser, url, **({'reload_page': True} if reload_page else {}))
        finally:
            try:
                traced.get_log('performance')
                diagnostics = getattr(browser, 'capture_diagnostics', None)
                report = traced.report()
                pid = urlsplit(url).path.rstrip('/').split('/')[-1]
                if re.fullmatch(r'\d{1,12}', pid):
                    report['production_id'] = pid
                if isinstance(diagnostics, dict):
                    diagnostics['http_evidence'] = report
                print('FREE_HTTP_EVIDENCE ' + json.dumps(report), flush=True)
            except Exception as exc:
                print('FREE_HTTP_DIAGNOSTIC_ERROR ' + type(exc).__name__, flush=True)
            finally:
                browser.driver = driver

    with patch.object(recovery, 'capture', capture):
        yield


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--sport', choices=('nfl', 'nhl'), required=True)
    cli.add_argument('--directory', required=True)
    args = cli.parse_args()
    from tools.free_live_provider_recovery import run
    with http_diagnostics():
        raise SystemExit(run(args.sport, args.directory))
