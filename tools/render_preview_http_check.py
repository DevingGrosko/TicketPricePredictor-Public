"""One bounded public-HTTPS smoke check of the owner's exact Render preview.

Only GET requests, no credentials, no production host, no database connection,
no write endpoints and no deployment. Does not run browser JavaScript.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import math
import signal
import ssl
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler

BASE = 'https://ticketsignal-staging-preview.onrender.com'
ALLOWED = frozenset(('/', '/healthz', '/readyz', '/baseball', '/nfl', '/nhl',
    '/nfl/archive', '/api/baseball/options', '/graph', '/baseball/stadium',
    '/nfl/stadium', '/nhl/arena', '/concerts'))
MAX_BODY = 4 * 1024 * 1024


class CheckError(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise CheckError(message)


def target_url(path):
    parts = urlsplit(path)
    require(not parts.scheme and not parts.netloc and not parts.fragment,
            'Only a relative preview URL is allowed.')
    require('\\' not in path and not any(ord(c) < 32 for c in path), 'Invalid URL.')
    require(parts.path in ALLOWED or (
        parts.path.startswith('/static/') and '%' not in parts.path and '..' not in parts.path),
        'URL path is outside the reviewed read-only scope.')
    require(len(path) <= 4096, 'URL length exceeded.')
    return BASE + path


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Page(HTMLParser):
    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.select = None
        self.options = {}
        self.links = []
        self.assets = []
        self.charts = []
        self.chart = None
        self.banner = False
        self.feed(body)
        self.close()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.banner |= a.get('id') == 'ticketsignal-staging-banner'
        if tag == 'select':
            self.select = a.get('id', '')
            self.options.setdefault(self.select, [])
        elif tag == 'option' and self.select is not None and a.get('value'):
            self.options[self.select].append(a['value'])
        if tag == 'a' and a.get('href'):
            self.links.append(a['href'])
        if tag == 'script' and a.get('src'):
            self.assets.append(a['src'])
        if tag == 'link' and a.get('rel') == 'stylesheet' and a.get('href'):
            self.assets.append(a['href'])
        if tag == 'script' and 'interactive-chart__data' in a.get('class', '').split():
            self.chart = []

    def handle_data(self, data):
        if self.chart is not None:
            self.chart.append(data)

    def handle_endtag(self, tag):
        if tag == 'select':
            self.select = None
        if tag == 'script' and self.chart is not None:
            self.charts.append(json.loads(''.join(self.chart)))
            self.chart = None


def validate_chart(page):
    require(bool(page.charts), 'No populated chart data found.')
    chart = page.charts[0]
    x, y = chart.get('x'), chart.get('y')
    require(isinstance(x, list) and isinstance(y, list) and len(x) == len(y) and len(x) > 0,
            'Chart arrays are empty or incompatible.')
    require(all(type(v) in (int, float) and math.isfinite(v) for v in x + y),
            'Chart contains nonnumeric or nonfinite data.')
    return len(x)


def main():
    started = datetime.now(timezone.utc).isoformat()
    checks, failures = [], []
    opener = build_opener(HTTPSHandler(context=ssl.create_default_context()), NoRedirects())

    def deadline(_sig, _frame):
        raise TimeoutError('Request deadline exceeded.')

    signal.signal(signal.SIGALRM, deadline)

    def fetch(path, *, expected=200, kind='html', label=None):
        url = target_url(path)
        label = label or urlsplit(path).path
        print('REQUEST ' + label, flush=True)
        t = time.monotonic()
        check = {'check': label, 'path': urlsplit(path).path, 'method': 'GET'}
        try:
            signal.alarm(130)
            request = Request(url, headers={
                'User-Agent': 'TicketSignal-Staging-Smoke/1.0', 'Accept-Encoding': 'identity'
            }, method='GET')
            try:
                response = opener.open(request, timeout=120)
            except HTTPError as error:
                response = error
            with response:
                status = response.code
                content_type = response.headers.get('Content-Type', '')
                staging = response.headers.get('X-TicketSignal-Environment')
                robots = response.headers.get('X-Robots-Tag', '')
                body = response.read(MAX_BODY + 1)
            check.update(status=status, bytes=len(body), staging=staging,
                         content_type=content_type, noindex='noindex' in robots.lower())
            require(len(body) <= MAX_BODY, 'Response exceeded size budget.')
            require(status == expected, 'Unexpected HTTP status: ' + str(status))
            if kind != 'asset':
                require(staging == 'staging-readonly', 'Response is not the labeled staging application.')
                require(check['noindex'], 'Missing preview noindex header.')
            if kind == 'html':
                require('text/html' in content_type, 'Expected HTML.')
                result = Page(body.decode('utf-8'))
                require(result.banner, 'Missing visible saved-snapshot banner.')
            elif kind == 'json':
                require('application/json' in content_type, 'Expected JSON.')
                result = json.loads(body)
            else:
                require(bool(body) and 'text/html' not in content_type, 'Static asset is missing or an error page.')
                result = None
            check['passed'] = True
            return result
        except Exception as error:
            check.update(passed=False, error_type=type(error).__name__,
                         reason=str(error) if isinstance(error, CheckError) else 'Transport, parsing, or deadline error.')
            raise
        finally:
            signal.alarm(0)
            check['seconds'] = round(time.monotonic() - t, 3)
            checks.append(check)
            print('HTTP_CHECK ' + json.dumps(check, sort_keys=True), flush=True)

    pages = {}
    try:
        # A single retry allows for a just-created or sleeping free service.
        for attempt in range(2):
            try:
                health = fetch('/healthz', kind='json', label='health-attempt-' + str(attempt + 1))
                require(health == {'status': 'ok', 'environment': 'staging-readonly'}, 'Health payload mismatch.')
                break
            except Exception:
                if attempt == 1:
                    raise
                print('One health retry after 20 seconds; no ongoing keepalive.', flush=True)
                time.sleep(20)
        ready = fetch('/readyz', kind='json')
        require(ready.get('status') == 'ok' and ready.get('databases') == 3, 'Database readiness mismatch.')
        for path in ('/', '/nfl', '/nhl'):
            pages[path] = fetch(path)
        # One warmed request only; not a load test or a keepalive job.
        fetch('/', label='home-warm-repeat')
        home = pages['/']
        venues = home.options.get('game-place', [])
        require(bool(venues), 'MLB landing page has no selectable venues.')
        options = fetch('/api/baseball/options?' + urlencode({'venue': venues[0]}), kind='json')
        choices = [(game, sections[0]) for game, sections in options.get('sections_by_game', {}).items()
                   if isinstance(sections, list) and sections]
        require(bool(choices), 'MLB options have no populated game/section.')
        game, section = choices[0]
        graph = fetch('/graph?' + urlencode({'event': venues[0], 'game': game,
                      'section': section, 'mode': 'single', 'display': 'money'}))
        point_count = validate_chart(graph)
        print('CHART_CHECK ' + json.dumps({'sport': 'mlb', 'mode': 'single', 'points': point_count,
                                          'populated_numeric_arrays': True, 'browser_rendering_tested': False}), flush=True)
        asset_paths = list(dict.fromkeys(p for page in (*pages.values(), graph) for p in page.assets
                                        if p.startswith('/static/')))
        require(bool(asset_paths), 'No local CSS/JavaScript assets linked.')
        # Bound requests but include the actual chart JS as well as home assets.
        picked = asset_paths[:5]
        picked += [p for p in graph.assets if '/static/js/graph.js' in p and p not in picked]
        for path in picked:
            fetch(path, kind='asset')
        # User-facing team reports are a separate gate from a working landing page.
        for home_path, report_path in (('/', '/baseball/stadium'), ('/nfl', '/nfl/stadium'), ('/nhl', '/nhl/arena')):
            link = next((p for p in pages[home_path].links
                         if p.startswith('/') and urlsplit(p).path == report_path), None)
            if link is None:
                failures.append({'check': report_path, 'reason': 'No report link found on tested landing page.'})
                continue
            try:
                fetch(link)
            except Exception:
                failures.append({'check': report_path, 'reason': 'Report request failed; see HTTP_CHECK.'})
        concert = fetch('/concerts', expected=503, kind='json')
        require(concert.get('status') == 'not_migrated', 'Expected explicit concert not-migrated status.')
    except Exception as error:
        failures.append({'check': 'core-preview', 'error_type': type(error).__name__,
                         'reason': str(error) if isinstance(error, CheckError) else 'See HTTP_CHECK.'})
    report = {'base_url': BASE, 'started_utc': started, 'finished_utc': datetime.now(timezone.utc).isoformat(),
              'mode': 'public-https-get-only', 'passed': not failures, 'checks': checks, 'failures': failures,
              'database_connections_opened': 0, 'write_requests': 0, 'production_requests': 0,
              'browser_javascript_tested': False, 'ingestion_or_schedule_tested': False}
    print('RENDER_PREVIEW_REPORT ' + json.dumps(report, sort_keys=True), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
