"""Validate published files and exercise them in Chrome on a local static server.

The test browser has no database credentials and uses no Flask server. No
public preview, provider website, collector, or production URL is contacted.
"""
from __future__ import annotations
import argparse
import functools
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import shutil
import threading
import time


def check_bundle(root):
    root=Path(root).resolve()
    manifest=json.loads((root/'manifest.json').read_bytes())
    checksums=json.loads((root/'checksums.json').read_bytes())
    if manifest['mode']!='historical-snapshot-preview' or manifest['live_updates_enabled'] is not False:
        raise RuntimeError('Unexpected snapshot mode.')
    if {s['sport'] for s in manifest['sports']} != {'mlb','nfl','nhl'}:
        raise RuntimeError('One of the requested sports is missing.')
    forbidden=('.env','.sql','.sqlite','.db','.gz','.py','.zip')
    for file in root.rglob('*'):
        if file.is_symlink() or file.is_file() and file.suffix in forbidden:
            raise RuntimeError('An unpublished source file entered the public bundle.')
    def read(path):
        if not re.fullmatch(r'data/(?:game|report|series|index)-[a-f0-9]{64}\.json',path):
            raise RuntimeError('Unexpected file reference.')
        if path not in checksums:raise RuntimeError('Missing checksum reference.')
        return json.loads((root/path).read_bytes())
    for path,info in checksums.items():
        if not re.fullmatch(r'data/(?:game|report|series|index)-[a-f0-9]{64}\.json',path):
            raise RuntimeError('Unexpected checksum path.')
        raw=(root/path).read_bytes()
        if len(raw)!=info['bytes'] or hashlib.sha256(raw).hexdigest()!=info['sha256']:
            raise RuntimeError('Published data integrity mismatch.')
    samples=[];games=sections=points=reports=0
    for item in manifest['sports']:
        index=read(item['file'])
        lookup={g['id']:g for g in index['games']}
        for game in index['games']:
            record=read(game['file']);games+=1
            loaded={}
            for section in record['sections']:
                blob=loaded.setdefault(section['file'],None)
                if blob is None:loaded[section['file']]=blob=read(section['file'])
                chart=blob['sections'][section['key']]
                x,y=chart['x'],chart['y']
                if len(x)!=section['points'] or len(x)!=len(y) or not x:
                    raise RuntimeError('Invalid published chart dimensions.')
                if any(a<b for a,b in zip(x,x[1:])):raise RuntimeError('Reversed chart chronology.')
                sections+=1;points+=len(x)
        chosen=None
        for report in index['reports']:
            payload=read(report['file']);reports+=1
            if any(g['id'] not in lookup for g in payload['games']):raise RuntimeError('Unknown report game.')
            eligible=[g for g in payload['games'] if g['section_count']]
            if chosen is None and eligible:
                chosen={'sport':item['sport'],'report':report['id'],'game':eligible[0]['id']}
        if chosen:samples.append(chosen)
    return manifest,samples,{'games':games,'reports':reports,'sections':sections,'chart_points':points,
                              'json_files':len(checksums),'samples':len(samples)}


class Handler(SimpleHTTPRequestHandler):
    def log_message(self,*args):pass
    def end_headers(self):
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; connect-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'")
        super().end_headers()


def check_browser(root,samples,screenshots):
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.support.ui import WebDriverWait, Select
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    driver_path=shutil.which('chromedriver')
    if not driver_path:raise RuntimeError('ChromeDriver is required for the browser gate.')
    server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Handler,directory=str(root)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    origin=f'http://127.0.0.1:{server.server_port}'
    opts=webdriver.ChromeOptions()
    for arg in ('--headless=new','--no-sandbox','--disable-dev-shm-usage','--disable-background-networking','--window-size=1280,1000'):
        opts.add_argument(arg)
    opts.set_capability('goog:loggingPrefs',{'browser':'ALL','performance':'ALL'})
    browser=webdriver.Chrome(service=Service(driver_path),options=opts)
    measurements=[];errors=[]
    try:
        for sample in samples:
            start=time.monotonic()
            browser.get(origin+'/#sport='+sample['sport']+'&report='+sample['report'])
            wait=WebDriverWait(browser,30)
            wait.until(lambda d:d.find_element(By.ID,'detail').is_displayed())
            browser.find_element(By.CSS_SELECTOR,'[data-view=game]').click()
            wait.until(lambda d:len(d.find_elements(By.CSS_SELECTOR,'#game-section option'))>0)
            Select(browser.find_element(By.ID,'game')).select_by_value(sample['game'])
            wait.until(lambda d:len(d.find_elements(By.CSS_SELECTOR,'#chart .curve'))==1 and not d.find_element(By.ID,'status').text)
            title=browser.find_element(By.ID,'chart-title').text
            if not title:raise RuntimeError('Game chart missing title.')
            choices=browser.find_elements(By.CSS_SELECTOR,'#game-section option')
            if len(choices)>1:
                Select(browser.find_element(By.ID,'game-section')).select_by_index(1)
                wait.until(lambda d:d.find_element(By.ID,'chart-title').text!=title and len(d.find_elements(By.CSS_SELECTOR,'#chart .curve'))==1)
            percent=browser.find_element(By.CSS_SELECTOR,'#display option[value=percent]')
            if percent.is_enabled():
                Select(browser.find_element(By.ID,'display')).select_by_value('percent')
                wait.until(lambda d:'%' in d.find_element(By.ID,'chart-tooltip').text)
            chart=browser.find_element(By.ID,'chart');chart.send_keys(Keys.ARROW_RIGHT)
            if 'point ' not in browser.find_element(By.ID,'chart-tooltip').text:
                raise RuntimeError('Keyboard chart inspection failed.')
            measurements.append({'sport':sample['sport'],'local_browser_flow_seconds':round(time.monotonic()-start,3)})
            if screenshots:
                Path(screenshots).mkdir(parents=True,exist_ok=True)
                browser.save_screenshot(str(Path(screenshots)/(sample['sport']+'-desktop.png')))
            browser.set_window_size(390,844)
            if browser.execute_script('return document.documentElement.scrollWidth > window.innerWidth + 1'):
                raise RuntimeError('Mobile layout overflows the viewport.')
            if screenshots:browser.save_screenshot(str(Path(screenshots)/(sample['sport']+'-mobile.png')))
            browser.set_window_size(1280,1000)
            browser.find_element(By.ID,'back').click()
            search=browser.find_element(By.ID,'team-search');search.send_keys('zzzzzznonexistent')
            if browser.find_elements(By.CSS_SELECTOR,'.team-card'):raise RuntimeError('Team search failed.')
            search.clear();search.send_keys('a');
            for entry in browser.get_log('browser'):
                if entry['level']=='SEVERE':errors.append(entry['message'])
        external=[]
        for record in browser.get_log('performance'):
            msg=json.loads(record['message'])['message']
            if msg.get('method')=='Network.requestWillBeSent':
                url=msg['params']['request']['url']
                if url.startswith('http') and not url.startswith(origin+'/'):external.append(url)
        if errors:raise RuntimeError('Browser console errors: '+str(len(errors)))
        if external:raise RuntimeError('The static UI attempted an external HTTP request.')
        return {'passed':True,'flows':measurements,'external_http_requests':0,'flask_server':False}
    finally:
        browser.quit();server.shutdown();server.server_close();thread.join(timeout=5)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('directory');parser.add_argument('--browser',action='store_true');parser.add_argument('--screenshots')
    args=parser.parse_args();root=Path(args.directory).resolve()
    _,samples,stats=check_bundle(root)
    print('STATIC_FILES_VERIFIED '+json.dumps(stats),flush=True)
    if args.browser:print('STATIC_BROWSER_REPORT '+json.dumps(check_browser(root,samples,args.screenshots)),flush=True)


if __name__=='__main__':main()
