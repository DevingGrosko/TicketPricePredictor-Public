"""Bounded GET-only acceptance of the actual TicketSignal GitHub Pages site."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

BASE = 'https://devinggrosko.github.io/TicketPricePredictor-Public'
PREFIX = '/TicketPricePredictor-Public'
HOST = 'devinggrosko.github.io'


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def fixed_url(path):
    require(isinstance(path, str) and path.startswith('/') and not path.startswith('//'), 'Invalid relative path')
    require('..' not in path and '%' not in path and '\\' not in path and '?' not in path and '#' not in path, 'Noncanonical data path')
    require(path in ('/original-manifest.json', '/original-assets.json', '/manifest.json') or
            re.fullmatch(r'/(?:native|data)/[a-z-]+[0-9a-f]{64}\.json', path) or
            re.fullmatch(r'/static/(?:css|js)/[a-zA-Z0-9_.-]+\.(?:css|js)', path), 'Unapproved data path')
    return BASE + path


class Redirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        require(parsed.scheme == 'https' and parsed.netloc == HOST and parsed.path.startswith(PREFIX+'/'), 'Redirect outside project')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def main():
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select, WebDriverWait
    opener = build_opener(Redirects())
    cache = {}; total = 0
    def read_bytes(path):
        nonlocal total
        if path not in cache:
            require(len(cache) < 150, 'HTTP sample limit reached')
            with opener.open(Request(fixed_url(path), headers={'Cache-Control':'no-cache'}), timeout=25) as response:
                raw = response.read(3*1024**2+1)
            total += len(raw)
            require(len(raw) <= 3*1024**2 and total <= 25*1024**2, 'HTTP byte budget exceeded')
            digest = re.search(r'([0-9a-f]{64})\.json$', path)
            if digest:
                require(hashlib.sha256(raw).hexdigest() == digest[1], 'Published data hash mismatch')
            cache[path] = raw
        return cache[path]
    def read(path):
        return json.loads(read_bytes(path))
    manifest = read('/original-manifest.json')
    require(set(manifest['sports']) == {'mlb','nfl','nhl'}, 'Missing sports')
    require(manifest.get('publication_host') == 'github-pages' and manifest.get('base_path') == PREFIX, 'Wrong publication')
    assets = read('/original-assets.json')
    css_count = 0
    for path, digest in assets.items():
        content = read_bytes('/static/'+path)
        require(hashlib.sha256(content).hexdigest() == digest, 'Asset hash mismatch')
        if path.startswith('css/'):
            require(content == (Path('Flask_App/static')/path).read_bytes(), 'Original CSS changed')
            css_count += 1
    options = webdriver.ChromeOptions()
    for flag in ('--headless=new','--no-sandbox','--disable-dev-shm-usage','--window-size=1440,1100'):
        options.add_argument(flag)
    options.set_capability('goog:loggingPrefs', {'browser':'ALL','performance':'ALL'})
    driver = webdriver.Chrome(service=Service(shutil.which('chromedriver')), options=options)
    driver.set_page_load_timeout(35)
    driver.execute_cdp_cmd('Network.enable', {})
    driver.execute_cdp_cmd('Network.setCacheDisabled', {'cacheDisabled':True})
    driver.execute_cdp_cmd('Network.setBlockedURLs', {'urls':['*pythonanywhere*','*tidbcloud*','*/api/*']})
    wait = WebDriverWait(driver,25,poll_frequency=.1)
    def ready():
        state = wait.until(lambda d:d.execute_script("return document.body?.dataset.staticError==='true'?'error':document.body?.dataset.staticReady==='true'?'ready':''"))
        require(state == 'ready', 'Static adapter failed')
        require(driver.current_url.startswith(BASE+'/'), 'Navigation left project')
    def click(item):
        driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", item)
        item.click(); ready()
    result = {'passed':False,'base_url':BASE+'/','tested_at':datetime.now(timezone.utc).isoformat(),
              'live_updates_enabled':manifest['live_updates_enabled'],'flows':[], 'original_css_verified':css_count,
              'source_freshness':{}, 'browser_cache_disabled':True}
    try:
        for sport in ('mlb','nfl','nhl'):
            cat = read(manifest['sports'][sport]); result['source_freshness'][sport] = cat['captured_through']
            home = BASE+('/' if sport=='mlb' else '/'+sport+'/')
            start = time.monotonic(); driver.get(home); ready(); home_seconds = time.monotonic()-start
            require('Start with a team.' in driver.find_element(By.TAG_NAME,'h1').text, 'Original homepage missing')
            require(not any(n.is_displayed() for n in driver.find_elements(By.CSS_SELECTOR,'.static-snapshot-note')), 'Banner is visible')
            driver.set_window_size(390,844)
            require(not driver.execute_script('return document.documentElement.scrollWidth>innerWidth+1'), 'Mobile overflow')
            driver.set_window_size(1440,1100)
            entry = next(r for r in cat['reports'] if cat['sections'].get(r['id']))
            click(driver.find_element(By.CSS_SELECTOR,'.nfl-stadium-card[href="'+PREFIX+'/reports/'+entry['id']+'.html"]'))
            Select(driver.find_element(By.ID,'section-jump')).select_by_index(1)
            click(driver.find_element(By.CSS_SELECTOR,'[data-section-jump-button]'))
            require(bool(driver.find_elements(By.ID,'venue-section-timeline-data')), 'Section evidence missing')
            chosen = None
            for group, filename in cat['options'].items():
                opts = read(filename)
                for item in opts['games']:
                    game = cat['games'].get(item['value'])
                    if not game or not game['section_count'] or game['capture_count'] < 2: continue
                    record = read(game['file'])
                    sections = [s for s in record['sections'] if s['points']>1 and s['name'] in opts['sections_by_game'].get(game['id'],[])]
                    if sections: chosen = group,game,sections[0]; break
                if chosen: break
            require(chosen is not None, 'No chart sample')
            group,game,section = chosen
            driver.get(home); ready()
            if sport == 'mlb':
                item = driver.find_element(By.CSS_SELECTOR,'[data-target="game-panel"]')
                driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})",item); item.click()
            form = driver.find_element(By.CSS_SELECTOR,'#game-panel .selection-form' if sport=='mlb' else '.'+sport+'-selection-form')
            Select(form.find_element(By.CSS_SELECTOR,'.place-select')).select_by_value(group)
            wait.until(lambda d:len(form.find_elements(By.CSS_SELECTOR,'.game-select option'))>1)
            Select(form.find_element(By.CSS_SELECTOR,'.game-select')).select_by_value(game['id'])
            Select(form.find_element(By.CSS_SELECTOR,'.section-select')).select_by_value(section['name'])
            start = time.monotonic(); click(form.find_element(By.CSS_SELECTOR,'.submit-analysis')); chart_seconds = time.monotonic()-start
            actual = driver.execute_script('return window.__staticChart'); expected = read(section['file'])['sections'][section['key']]
            require(actual['x']==expected['x'] and actual['y']==expected['y'], 'Chart data mismatch')
            click(driver.find_element(By.CSS_SELECTOR,'.view-toggle' if sport=='mlb' else '.nfl-chart-actions a:last-child'))
            relative = driver.execute_script('return window.__staticChart')
            require(relative['display']=='percentage', 'Percentage toggle failed')
            expected_y = expected['y'] if expected['y'][0]==0 else [round(v/expected['y'][0]*100) for v in expected['y']]
            require(relative['y']==expected_y, 'Percentage values differ')
            if sport != 'mlb':
                click(driver.find_element(By.CSS_SELECTOR,'.nfl-chart-actions a:first-child'))
                require(bool(driver.find_elements(By.CSS_SELECTOR,'[data-stadium-map] path')), 'Map missing')
                driver.find_element(By.CSS_SELECTOR,'[data-map-search]').send_keys(section['name'])
                driver.find_element(By.CSS_SELECTOR,'[data-map-search-button]').click()
                wait.until(lambda d:d.find_element(By.CSS_SELECTOR,'[data-section-name]').text==section['name'])
                require(driver.find_element(By.CSS_SELECTOR,'[data-section-history]').get_attribute('href').startswith(BASE+'/'), 'Map link escaped project')
            result['flows'].append({'sport':sport,'passed':True,'home_seconds':round(home_seconds,3),'chart_seconds':round(chart_seconds,3),'sample_points':len(actual['x'])})
        cat = read(manifest['sports']['mlb'])
        venue = next(v for v,p in cat['market'].items() if any(read(f)['percentage']['y'] for f in read(p).values()))
        market = read(cat['market'][venue]); section = next(s for s,p in market.items() if read(p)['percentage']['y']); expected = read(market[section])
        query = urlencode({'event':venue,'section':section})
        driver.get(BASE+'/graph/?'+query); ready(); actual = driver.execute_script('return window.__staticChart')
        require(actual['x']==expected['money']['x'] and actual['y']==expected['money']['y'], 'Market values differ')
        driver.get(BASE+'/predict/?'+query); ready()
        require(driver.find_element(By.CSS_SELECTOR,'.time-value strong').text==f"{expected['time']:.1f}", 'Buying window differs')
        sample = cat['games']['30313']; record = read(sample['file'])
        require(sample['captured_through'] >= '2026-09-26T23:30:00+00:00' and len(record['sections'])==109, 'Accepted post-export capture missing')
        result['post_export_capture'] = {'event_id':'30313','captured_through':sample['captured_through'],'sections':len(record['sections'])}
        errors = [x for x in driver.get_log('browser') if x['level']=='SEVERE']
        require(not errors, 'Severe browser errors: '+str(errors[:3]))
        count = 0
        for item in driver.get_log('performance'):
            msg = json.loads(item['message'])['message']
            if msg.get('method') != 'Network.requestWillBeSent': continue
            request = msg['params']['request']; parsed = urlsplit(request['url']); count += 1
            require(request['method'] in ('GET','HEAD'), 'Unexpected browser write')
            require(parsed.hostname in (HOST,'fonts.googleapis.com','fonts.gstatic.com') or parsed.scheme=='data', 'Unexpected browser origin')
            if parsed.hostname==HOST: require(parsed.path.startswith(PREFIX+'/') and '/api/' not in parsed.path, 'Project or API boundary violation')
        result.update(passed=True,market_and_buying_window=True,severe_console_errors=0,api_requests=0,write_requests=0,browser_requests=count,
                      http_files=len(cache),http_bytes=total,note='Actual public GitHub Pages URL; one runner, CDN may be warm. Historical gap remains.')
    finally:
        driver.quit()
        Path('public-pages-result.json').write_text(json.dumps(result,indent=2))
        print('PUBLIC_PAGES_RESULT '+json.dumps(result),flush=True)
    return result


if __name__ == '__main__': main()
