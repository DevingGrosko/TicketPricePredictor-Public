"""Check the fixed public restored preview. No secrets, SQL, deploys or writes."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

BASE = 'https://ticketsignal-static-preview.onrender.com'
ROOT = Path(__file__).resolve().parents[1]
OUT = Path('public-original-results')
MAX_BODY = 4 * 1024 * 1024
ALLOWED_PATH = re.compile(r'^/(?:|original-manifest\.json|original-assets\.json|manifest\.json|native/(?:bridge\.js|preview\.css|data-[a-f0-9]{64}\.json)|data/(?:game|series|report|index)-[a-f0-9]{64}\.json|static/[A-Za-z0-9_./-]+)$')


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def fixed_url(path):
    require(isinstance(path, str), 'Non-string publication path')
    require(not path.startswith('//'), 'Protocol-relative path rejected')
    path = '/' + path.lstrip('/')
    require(bool(ALLOWED_PATH.fullmatch(path)) and '..' not in path.split('/'), 'Unexpected publication path')
    return BASE + path


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AssertionError('Unexpected redirect from fixed static publication')


class Reader:
    def __init__(self):
        self.opener = build_opener(NoRedirect())
        self.cache, self.checks, self.total_bytes = {}, [], 0

    def raw(self, path):
        url = fixed_url(path)
        if url in self.cache:
            return self.cache[url]
        require(len(self.checks) < 80, 'Bounded HTTP request budget exceeded')
        start = time.monotonic()
        request = Request(url, headers={'Accept-Encoding': 'identity', 'User-Agent': 'TicketSignal-preview-acceptance/1.0'})
        with self.opener.open(request, timeout=25) as response:
            require(response.status == 200 and response.geturl() == url, 'Unexpected HTTP response')
            raw = response.read(MAX_BODY + 1)
            headers = dict(response.headers.items())
        require(len(raw) <= MAX_BODY, 'Oversized response')
        self.total_bytes += len(raw)
        require(self.total_bytes <= 30 * 1024 * 1024, 'Bounded download budget exceeded')
        match = re.search(r'-([a-f0-9]{64})\.json$', url)
        if match:
            require(hashlib.sha256(raw).hexdigest() == match[1], 'Published data checksum mismatch')
        check = {'path': urlsplit(url).path, 'seconds': round(time.monotonic()-start, 3), 'bytes': len(raw), 'hashed_data_verified': bool(match)}
        self.checks.append(check)
        self.cache[url] = (raw, headers)
        return raw, headers

    def data(self, path):
        return json.loads(self.raw(path)[0])


def main():
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait, Select

    OUT.mkdir(exist_ok=True)
    reader = Reader()
    result = {'passed': False, 'base_url': BASE, 'started_utc': datetime.now(timezone.utc).isoformat(), 'mode': 'public-static-get-only', 'flows': [], 'errors': []}
    driver = None
    try:
        home, headers = reader.raw('/')
        require(b'/native/bridge.js' in home, 'Public homepage is still the old simplified preview')
        csp = next((v for k,v in headers.items() if k.lower() == 'content-security-policy'), '')
        require("script-src 'self'" in csp and 'https://fonts.googleapis.com' in csp and 'https://fonts.gstatic.com' in csp, 'Restored UI security headers not applied')
        require("script-src 'self' 'unsafe-inline'" not in csp, 'Unexpected inline executable script allowance')
        manifest = reader.data('/original-manifest.json')
        require(manifest.get('presentation') == 'original-templates' and manifest.get('live_updates_enabled') is False, 'Unexpected publication mode')
        result['publication_generated_at'] = manifest['generated_at']
        result['live_updates_enabled'] = manifest['live_updates_enabled']
        assets = reader.data('/original-assets.json')
        for path, digest in assets.items():
            expected = ROOT/'Flask_App'/'static'/path
            require(expected.is_file(), 'Unexpected original asset')
            raw, _ = reader.raw('/static/'+path)
            require(hashlib.sha256(raw).hexdigest() == digest and raw == expected.read_bytes(), 'Original asset differs: '+path)
        for public, local in [('/native/bridge.js', 'static_original/bridge.js'), ('/native/preview.css', 'static_original/preview.css')]:
            require(reader.raw(public)[0] == (ROOT/local).read_bytes(), 'Restored adapter differs from reviewed source')
        result['original_assets_verified'] = len(assets)
        cats = {sport: reader.data(manifest['sports'][sport]) for sport in ('mlb','nfl','nhl')}
        result['source_freshness'] = {sport: c['captured_through'] for sport,c in cats.items()}
        result['directory_counts'] = {sport: {'reports': len(c['reports']), 'games': len(c['games'])} for sport,c in cats.items()}

        opts = webdriver.ChromeOptions()
        for arg in ('--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--window-size=1440,1100'):
            opts.add_argument(arg)
        opts.set_capability('goog:loggingPrefs', {'browser':'ALL', 'performance':'ALL'})
        driver = webdriver.Chrome(service=Service(shutil.which('chromedriver')), options=opts)
        driver.set_page_load_timeout(35)
        driver.execute_cdp_cmd('Network.enable', {})
        driver.execute_cdp_cmd('Network.setCacheDisabled', {'cacheDisabled':True})
        driver.execute_cdp_cmd('Network.setBlockedURLs', {'urls':['*pythonanywhere*','*tidbcloud*','*/api/*']})
        wait = WebDriverWait(driver, 25, poll_frequency=.1)
        console, network = [], []

        def ready():
            state = wait.until(lambda d: d.execute_script("const b=document.body;return b?.dataset.staticError==='true'?'error':b?.dataset.staticReady==='true'?'ready':''"))
            require(driver.current_url.startswith(BASE+'/'), 'Browser left the static origin')
            require(state == 'ready', 'Page reported an unavailable selection: '+driver.current_url)

        def click(element):
            driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", element)
            element.click()

        def capture_logs():
            console.extend(e for e in driver.get_log('browser') if e['level'] == 'SEVERE')
            for entry in driver.get_log('performance'):
                msg = json.loads(entry['message'])['message']
                if msg.get('method') == 'Network.requestWillBeSent':
                    r = msg['params']['request']
                    network.append({'url':r['url'], 'method':r['method']})

        for sport, cat in cats.items():
            home_path = '/' if sport == 'mlb' else '/'+sport+'/'
            started = time.monotonic()
            driver.get(BASE+home_path); ready()
            home_seconds = time.monotonic()-started
            wait.until(lambda d: d.execute_script('return document.fonts.status') == 'loaded')
            require('Start with a team.' in driver.find_element(By.TAG_NAME,'h1').text, 'Original heading missing')
            driver.save_screenshot(str(OUT/(sport+'-home-desktop.png')))
            driver.set_window_size(390,844)
            require(not driver.execute_script('return document.documentElement.scrollWidth>innerWidth+1'), 'Mobile home has horizontal overflow')
            driver.save_screenshot(str(OUT/(sport+'-home-mobile.png')))
            driver.set_window_size(1440,1100)
            entry = next(r for r in cat['reports'] if cat['sections'].get(r['id']))
            require(bool(re.fullmatch('[a-f0-9]{64}',entry['id'])), 'Invalid report id')
            link = driver.find_element(By.CSS_SELECTOR,'.nfl-stadium-card[href="/reports/'+entry['id']+'.html"]')
            started = time.monotonic(); click(link); ready(); report_seconds = time.monotonic()-started
            require(driver.find_elements(By.ID,'section-jump'), 'Original section navigation missing')
            driver.save_screenshot(str(OUT/(sport+'-report.png')))
            Select(driver.find_element(By.ID,'section-jump')).select_by_index(1)
            started = time.monotonic(); click(driver.find_element(By.CSS_SELECTOR,'[data-section-jump-button]')); ready(); section_seconds = time.monotonic()-started
            require(driver.find_elements(By.ID,'venue-section-timeline-data') and driver.find_elements(By.CSS_SELECTOR,'#section-games summary'), 'Section timeline/evidence missing')
            driver.save_screenshot(str(OUT/(sport+'-section.png')))
            chosen = None
            for group,path in list(cat['options'].items())[:6]:
                options = reader.data(path)
                for row in options['games']:
                    game = cat['games'].get(row['value'])
                    if not game or not game['section_count'] or game['capture_count'] <= 1:
                        continue
                    record = reader.data(game['file'])
                    choices = [s for s in record['sections'] if s['name'] in options['sections_by_game'].get(game['id'],[]) and s['points']>1]
                    if choices:
                        chosen = group, game, choices[0]
                        break
                if chosen:
                    break
            require(chosen is not None, 'No representative multi-point chart within bounded sample')
            group, game, section = chosen
            expected = reader.data(section['file'])['sections'][section['key']]
            driver.get(BASE+home_path); ready()
            if sport == 'mlb':
                click(driver.find_element(By.CSS_SELECTOR,'[data-target="game-panel"]'))
            form = driver.find_element(By.CSS_SELECTOR,'#game-panel .selection-form' if sport=='mlb' else '.'+sport+'-selection-form')
            Select(form.find_element(By.CSS_SELECTOR,'.place-select')).select_by_value(group)
            wait.until(lambda d:len(form.find_elements(By.CSS_SELECTOR,'.game-select option'))>1)
            Select(form.find_element(By.CSS_SELECTOR,'.game-select')).select_by_value(game['id'])
            Select(form.find_element(By.CSS_SELECTOR,'.section-select')).select_by_value(section['name'])
            started = time.monotonic(); click(form.find_element(By.CSS_SELECTOR,'.submit-analysis')); ready(); chart_seconds = time.monotonic()-started
            require(driver.find_elements(By.CSS_SELECTOR,'.interactive-chart__line'), 'Chart did not render')
            actual = driver.execute_script('return window.__staticChart')
            require(actual['x'] == expected['x'] and actual['y'] == expected['y'], 'Chart differs from published observations')
            driver.save_screenshot(str(OUT/(sport+'-graph.png')))
            click(driver.find_element(By.CSS_SELECTOR,'.view-toggle' if sport=='mlb' else '.nfl-chart-actions a:last-child')); ready()
            relative = driver.execute_script('return window.__staticChart')
            expected_y = expected['y'] if expected['y'][0] == 0 else [100]+[round(v/expected['y'][0]*100) for v in expected['y'][1:]]
            require(relative['display']=='percentage' and relative['y']==expected_y, 'Percentage toggle differs')
            map_seconds = None
            if sport != 'mlb':
                started = time.monotonic(); click(driver.find_element(By.CSS_SELECTOR,'.nfl-chart-actions a:first-child')); ready(); map_seconds = time.monotonic()-started
                require(driver.find_elements(By.CSS_SELECTOR,'[data-stadium-map] path'), 'Map did not render')
                search = driver.find_element(By.CSS_SELECTOR,'[data-map-search]'); search.clear(); search.send_keys(section['name'])
                click(driver.find_element(By.CSS_SELECTOR,'[data-map-search-button]'))
                wait.until(lambda d:d.find_element(By.CSS_SELECTOR,'[data-section-name]').text==section['name'])
                require(driver.find_element(By.CSS_SELECTOR,'[data-section-history]').get_attribute('href').startswith(BASE+'/'), 'Missing map history link')
                driver.save_screenshot(str(OUT/(sport+'-map.png')))
            result['flows'].append({'sport':sport, 'passed':True, 'home_seconds':round(home_seconds,3), 'team_report_seconds':round(report_seconds,3), 'section_detail_seconds':round(section_seconds,3), 'game_submit_to_chart_seconds':round(chart_seconds,3), 'map_seconds':None if map_seconds is None else round(map_seconds,3), 'chart_points':len(actual['x']), 'chart_values_match':True, 'percentage_toggle':True, 'mobile_home_no_overflow':True})
            print('PUBLIC_ORIGINAL_FLOW '+json.dumps(result['flows'][-1]), flush=True)
            capture_logs()

        selected = None
        for venue,path in list(cats['mlb']['market'].items())[:3]:
            market = reader.data(path)
            for section,file in list(market.items())[:5]:
                payload = reader.data(file)
                if payload['percentage']['y'] and payload['time'] is not None:
                    selected = venue, section, payload
                    break
            if selected:
                break
        require(selected is not None, 'No historical buying-window sample within budget')
        venue, section, payload = selected
        query = urlencode({'event':venue,'section':section})
        driver.get(BASE+'/graph/?'+query); ready()
        actual = driver.execute_script('return window.__staticChart')
        require(actual['x']==payload['money']['x'] and actual['y']==payload['money']['y'], 'Multi-game chart differs')
        driver.get(BASE+'/predict/?'+query); ready()
        require(driver.find_element(By.CSS_SELECTOR,'.time-value strong').text == f"{payload['time']:.1f}", 'Buying-window label differs')
        driver.save_screenshot(str(OUT/'mlb-buying-window.png'))
        driver.get(BASE+'/concerts/'); ready()
        require('not been migrated' in driver.find_element(By.CSS_SELECTOR,'.static-snapshot-note').text, 'Concert limitation is not explicit')
        capture_logs()
        allowed_hosts = {urlsplit(BASE).netloc, 'fonts.googleapis.com', 'fonts.gstatic.com'}
        unexpected = [r for r in network if r['method'] not in ('GET','HEAD') or (urlsplit(r['url']).scheme in ('https','http') and urlsplit(r['url']).netloc not in allowed_hosts) or '/api/' in r['url']]
        require(not unexpected, 'Unexpected API, external, or write request')
        require(not console, 'Severe browser error: '+str(console[:2]))
        result.update(passed=True, legacy_market_and_buying_window=True, concerts_unavailable_notice=True, api_requests=0, write_requests=0, severe_console_errors=0, browser_requests=len(network), browser_cache_disabled=True, note='Single GitHub runner; CDN caches may be warm. Selenium action timings, not Web Vitals or load testing. Original Google Fonts allowed.')
    except Exception as exc:
        result['errors'].append(type(exc).__name__+': '+str(exc)[:1800])
        if driver:
            driver.save_screenshot(str(OUT/'failure.png'))
        raise
    finally:
        if driver:
            driver.quit()
        result['http_checks'] = reader.checks
        result['finished_utc'] = datetime.now(timezone.utc).isoformat()
        (OUT/'report.json').write_text(json.dumps(result,indent=2))
        print('PUBLIC_ORIGINAL_REPORT '+json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
