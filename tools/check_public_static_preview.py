"""Check the owner's deployed STATIC URL, not Flask or a database.

No database credentials, writes, provider scraping, deployments, or schedules.
The target is fixed; HTTP redirects and arbitrary data paths are rejected.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone
from urllib.request import build_opener, HTTPRedirectHandler, Request
from urllib.parse import urlsplit, urlencode

BASE = 'https://ticketsignal-static-preview.onrender.com'
DATA = re.compile(r'data/(?:index|report|game|series)-[a-f0-9]{64}\.json\Z')
OUTPUT = Path('static-public-results')
REPORT = {'base_url': BASE, 'passed': False, 'mode': 'public-static-get-only',
          'started_utc': datetime.now(timezone.utc).isoformat(),
          'http_checks': [], 'browser_flows': [], 'errors': []}
BODY_LIMIT = 2 * 1024 * 1024


class CheckError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise CheckError(message)


def target(path):
    require(isinstance(path, str) and (path in ('', 'manifest.json', 'app.js', 'styles.css')
                                     or DATA.fullmatch(path)), 'Non-static path rejected.')
    return BASE + '/' + path


def validate_series(value):
    x, y = value.get('x'), value.get('y')
    require(isinstance(x, list) and isinstance(y, list) and len(x) == len(y) and len(x) > 0,
            'Chart dimensions invalid.')
    require(all(type(v) in (int, float) and math.isfinite(v) for v in x + y), 'Non-finite chart.')
    require(all(a >= b for a, b in zip(x, x[1:])), 'Chart chronology reversed.')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CheckError('Public preview redirected unexpectedly.')


class Reader:
    def __init__(self):
        self.opener = build_opener(NoRedirect())
        self.total = 0

    def read(self, path, *, json_data=True):
        require(len(REPORT['http_checks']) < 35, 'HTTP request budget exceeded.')
        start = time.monotonic()
        request = Request(target(path), headers={'User-Agent': 'TicketSignal-Static-Acceptance/1.0',
                                                'Accept-Encoding': 'identity'}, method='GET')
        with self.opener.open(request, timeout=25) as response:
            require(response.status == 200, 'Unexpected public response.')
            raw = response.read(BODY_LIMIT + 1)
            headers = response.headers
            require(len(raw) <= BODY_LIMIT, 'Public response exceeds test body limit.')
            self.total += len(raw)
            require(self.total <= 20 * 1024 * 1024, 'HTTP byte budget exceeded.')
        timing = round(time.monotonic() - start, 4)
        require('noindex' in headers.get('X-Robots-Tag', ''), 'Preview noindex header missing.')
        if DATA.fullmatch(path):
            expected = path.rsplit('-', 1)[1][:-5]
            require(hashlib.sha256(raw).hexdigest() == expected, 'Public data checksum mismatch.')
        record = {'path': path or '/', 'status': 200, 'seconds': timing, 'bytes': len(raw),
                  'cache_control': headers.get('Cache-Control'), 'content_type': headers.get('Content-Type'),
                  'checksum_verified': bool(DATA.fullmatch(path))}
        REPORT['http_checks'].append(record)
        print('HTTP_CHECK ' + json.dumps(record), flush=True)
        return json.loads(raw) if json_data else raw


def prepare_samples(reader):
    html = reader.read('', json_data=False)
    require(html == Path('static_preview/index.html').read_bytes(), 'Public HTML differs from reviewed checkout.')
    for path in ('app.js', 'styles.css'):
        require(reader.read(path, json_data=False) == Path('static_preview', path).read_bytes(),
                'Public static asset differs from reviewed checkout: ' + path)
    manifest = reader.read('manifest.json')
    require(manifest.get('version') == 1 and manifest.get('mode') == 'historical-snapshot-preview'
            and manifest.get('live_updates_enabled') is False, 'Unexpected snapshot manifest.')
    require({s['sport'] for s in manifest['sports']} == {'mlb', 'nfl', 'nhl'}, 'Sports missing.')
    REPORT['publication_generated_at'] = manifest['generated_at']
    REPORT['live_updates_enabled'] = manifest['live_updates_enabled']
    REPORT['source_freshness'] = {s['sport']: s['captured_through'] for s in manifest['sports']}
    samples = []
    for sport in manifest['sports']:
        index = reader.read(sport['file'])
        entry = next(r for r in index['reports'] if r['game_count'] > 0)
        report = reader.read(entry['file'])
        games = [g for g in report['games'] if g['section_count'] > 0]
        require(games, 'Selected report has no usable games.')
        game_entry = max(games, key=lambda g: g['capture_count'])
        game = reader.read(game_entry['file'])
        section = max(game['sections'], key=lambda s: s['points'])
        shard = reader.read(section['file'])
        series = shard['sections'][section['key']]
        validate_series(series)
        require(len(series['x']) == section['points'], 'Point count mismatch.')
        samples.append({'sport': sport['sport'], 'report': entry['id'], 'game': game_entry['id'],
                        'section': section, 'series': series, 'report_count': len(index['reports']),
                        'game_count': len(index['games'])})
    REPORT['directory_counts'] = [{k: s[k] for k in ('sport', 'report_count', 'game_count')} for s in samples]
    return samples


def check_browser(samples):
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import Select, WebDriverWait

    driver_path = shutil.which('chromedriver')
    require(driver_path, 'ChromeDriver unavailable.')
    opts = webdriver.ChromeOptions()
    for arg in ('--headless=new', '--no-sandbox', '--disable-dev-shm-usage',
                '--disable-background-networking', '--window-size=1280,1000'):
        opts.add_argument(arg)
    opts.set_capability('goog:loggingPrefs', {'browser': 'ALL', 'performance': 'ALL'})
    browser = webdriver.Chrome(service=Service(driver_path), options=opts)
    browser.set_page_load_timeout(35)
    browser.execute_cdp_cmd('Network.enable', {})
    browser.execute_cdp_cmd('Network.setCacheDisabled', {'cacheDisabled': True})
    browser.execute_cdp_cmd('Network.setBlockedURLs', {'urls': ['*pythonanywhere*', '*tidbcloud*', '*/api/*']})
    wait = WebDriverWait(browser, 25, poll_frequency=0.05)
    logs = []
    try:
        for sample in samples:
            sport = sample['sport']
            browser.get('about:blank')
            browser.set_window_size(1280, 1000)
            started = time.monotonic()
            browser.get(BASE + '/#sport=' + sport)
            wait.until(lambda d: d.execute_script(
                'return state.index !== null && state.sport === arguments[0] && document.querySelectorAll(".team-card").length > 0 && !document.getElementById("status").textContent', sport))
            directory_seconds = time.monotonic() - started
            require(len(browser.find_elements(By.CSS_SELECTOR, '.team-card')) == sample['report_count'], 'Directory count differs.')
            started = time.monotonic()
            browser.find_element(By.CSS_SELECTOR, '[data-report="' + sample['report'] + '"]').click()
            wait.until(lambda d: d.execute_script(
                'return state.report !== null && !document.getElementById("detail").hidden && !document.getElementById("status").textContent'))
            report_seconds = time.monotonic() - started
            started = time.monotonic()
            browser.find_element(By.CSS_SELECTOR, '[data-view="game"]').click()
            wait.until(lambda d: d.execute_script('return state.game !== null && !document.getElementById("status").textContent'))
            Select(browser.find_element(By.ID, 'game')).select_by_value(sample['game'])
            wait.until(lambda d: d.execute_script('return state.game !== null && state.game.id === arguments[0] && state.series !== null', sample['game']))
            Select(browser.find_element(By.ID, 'game-section')).select_by_value(sample['section']['key'])
            wait.until(lambda d: d.execute_script('return state.series !== null && document.getElementById("chart-title").textContent === arguments[0] && document.querySelectorAll("#chart .curve").length === 1', sample['section']['name']))
            game_seconds = time.monotonic() - started
            actual = browser.execute_script('return {x:state.series.x, y:state.series.y}')
            require(actual == {k: sample['series'][k] for k in ('x', 'y')}, 'Browser chart values differ from downloaded publication.')
            if sample['series']['y'][0] != 0:
                Select(browser.find_element(By.ID, 'display')).select_by_value('percent')
                require('100.0%' in browser.find_element(By.ID, 'chart-tooltip').text, 'Relative view did not start at 100%.')
            chart = browser.find_element(By.ID, 'chart')
            chart.send_keys(Keys.ARROW_RIGHT)
            if len(actual['x']) > 1:
                require('point 2 of ' in browser.find_element(By.ID, 'chart-tooltip').text, 'Keyboard chart inspection failed.')
            browser.save_screenshot(str(OUTPUT / (sport + '-desktop.png')))
            browser.set_window_size(390, 844)
            require(not browser.execute_script('return document.documentElement.scrollWidth > window.innerWidth + 1'), 'Mobile page overflow.')
            browser.save_screenshot(str(OUTPUT / (sport + '-mobile.png')))
            browser.set_window_size(1280, 1000)
            browser.find_element(By.ID, 'back').click()
            require(browser.find_element(By.ID, 'directory').is_displayed(), 'Back navigation failed.')
            search = browser.find_element(By.ID, 'team-search')
            search.send_keys('zzzzzznonexistent')
            require(len(browser.find_elements(By.CSS_SELECTOR, '.team-card')) == 0, 'Search filter failed.')
            search.clear()
            search.send_keys('a')
            browser.get(BASE + '/#' + urlencode({'sport': sport, 'report': sample['report']}))
            wait.until(lambda d: d.execute_script('return state.report !== null && !document.getElementById("detail").hidden'))
            record = {'sport': sport, 'passed': True, 'browser_cache_disabled': True,
                      'directory_usable_seconds': round(directory_seconds, 3),
                      'report_click_to_ready_seconds': round(report_seconds, 3),
                      'game_selection_to_chart_seconds': round(game_seconds, 3),
                      'chart_points': len(actual['x']), 'chart_values_match_public_json': True,
                      'relative_toggle': sample['series']['y'][0] != 0,
                      'keyboard_inspection': True, 'mobile_no_overflow': True, 'deep_link': True, 'search_and_back': True}
            REPORT['browser_flows'].append(record)
            print('BROWSER_CHECK ' + json.dumps(record), flush=True)
            logs.extend(browser.get_log('browser'))
        requests = []
        encodings = set()
        for record in browser.get_log('performance'):
            msg = json.loads(record['message'])['message']
            if msg.get('method') == 'Network.requestWillBeSent':
                request = msg['params']['request']
                url = request['url']
                if url.startswith('http'):
                    parsed = urlsplit(url)
                    require(parsed.scheme == 'https' and parsed.netloc == urlsplit(BASE).netloc, 'Browser contacted another origin.')
                    require(request['method'] == 'GET', 'Browser attempted non-GET request.')
                    path = parsed.path.lstrip('/')
                    require(path in ('', 'manifest.json', 'app.js', 'styles.css', 'favicon.ico') or DATA.fullmatch(path), 'Browser requested a non-static resource.')
                    requests.append(parsed.path)
            if msg.get('method') == 'Network.responseReceived':
                for key, value in msg['params']['response'].get('headers', {}).items():
                    if key.lower() == 'content-encoding': encodings.add(value)
        severe = [entry for entry in logs if entry['level'] == 'SEVERE']
        REPORT['browser_network'] = {'http_requests': len(requests), 'external_requests': 0,
                                     'api_requests': 0, 'write_requests': 0,
                                     'observed_compression': sorted(encodings), 'severe_console_errors': len(severe)}
        require(not severe, 'Browser logged a severe error: ' + (severe[0]['message'][:300] if severe else ''))
    except Exception:
        try:
            browser.save_screenshot(str(OUTPUT / 'failure.png'))
            REPORT['browser_diagnostic'] = {'status': browser.find_element(By.ID, 'status').text[:300]}
        except Exception:
            REPORT['browser_diagnostic'] = {'status': 'Diagnostic capture unavailable.'}
        raise
    finally:
        browser.quit()


def self_test():
    for bad in ('https://bunnyjeff.pythonanywhere.com', '//example.com', '../.env', 'data/../secret', 'api/backfill', 'manifest.json?x=1'):
        try: target(bad)
        except CheckError: pass
        else: raise AssertionError('Unsafe target accepted.')
    require(target('manifest.json') == BASE + '/manifest.json', 'Target mismatch.')
    validate_series({'x': [3, 3, 1], 'y': [0, 4, 5]})
    for bad in ({'x': [], 'y': []}, {'x': [1, 2], 'y': [1, 2]}, {'x': [1], 'y': [float('nan')]}):
        try: validate_series(bad)
        except CheckError: pass
        else: raise AssertionError('Invalid chart accepted.')
    print('PASS: offline URL boundary and chart validation checks.')


def main():
    if '--self-test' in sys.argv:
        self_test()
        return 0
    OUTPUT.mkdir(exist_ok=True)
    try:
        samples = prepare_samples(Reader())
        check_browser(samples)
        REPORT['passed'] = True
    except Exception as error:
        REPORT['errors'].append({'type': type(error).__name__, 'message': str(error)[:500],
                                 'locations': [{'function': f.name, 'line': f.lineno} for f in traceback.extract_tb(error.__traceback__)[-4:]]})
    REPORT['finished_utc'] = datetime.now(timezone.utc).isoformat()
    (OUTPUT / 'report.json').write_text(json.dumps(REPORT, indent=2) + '\n')
    print('PUBLIC_STATIC_REPORT ' + json.dumps(REPORT), flush=True)
    return 0 if REPORT['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
