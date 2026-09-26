"""Validate native static output and original UI interactions; no live data writes."""
from __future__ import annotations
import argparse
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import shutil
import threading
import time
from urllib.parse import urlencode, urlsplit, unquote

CSP = "default-src 'self'; script-src 'self'; connect-src 'self'; style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; font-src https://fonts.gstatic.com; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"

def validate(root):
    from tools.check_static_preview import check_bundle
    root=Path(root).resolve();_,_,counts=check_bundle(root)
    assets=json.loads((root/'original-assets.json').read_bytes())
    for path,expected in assets.items():
        if hashlib.sha256((root/'static'/path).read_bytes()).hexdigest()!=expected:raise AssertionError('Original asset mismatch')
    for file in (root/'native').glob('data-*.json'):
        if hashlib.sha256(file.read_bytes()).hexdigest()!=file.stem[5:]:raise AssertionError('Native data hash mismatch')
    checked=set();missing=[]
    for file in root.rglob('*.html'):
        text=file.read_text()
        for key,value in re.findall(r'\b(href|src|data-static-json|data-original-script|data-static-boot)="([^"]+)"',text):
            import html
            parts=urlsplit(html.unescape(value))
            if parts.netloc or parts.scheme:continue
            path=parts.path
            if not path.startswith('/') or path.startswith('/api/') or 'TSVALUE_' in path:continue
            path=unquote(path)
            if path in checked:continue
            checked.add(path);p=root/path.lstrip('/')
            if not p.is_file() and not (p/'index.html').is_file():missing.append(path)
        if re.search(r'<script(?![^>]*(?:src=|type="text/plain"|type="application/json"))[^>]*>\s*\S',text):raise AssertionError('Inline executable script')
    if missing:raise AssertionError('Missing native targets: '+str(missing[:12]))
    counts.update(original_pages=len(list(root.rglob('*.html'))), original_assets=len(assets), checked_targets=len(checked))
    return counts

class Handler(SimpleHTTPRequestHandler):
    def log_message(self,*args):pass
    def end_headers(self):
        self.send_header('Content-Security-Policy',CSP);super().end_headers()


def browser_check(root,output):
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait, Select
    root=Path(root).resolve();out=Path(output);out.mkdir(parents=True,exist_ok=True)
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(root)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base=f'http://127.0.0.1:{server.server_port}'
    opts=webdriver.ChromeOptions()
    for arg in ['--headless=new','--no-sandbox','--disable-dev-shm-usage','--window-size=1440,1100']:opts.add_argument(arg)
    opts.set_capability('goog:loggingPrefs',{'browser':'ALL','performance':'ALL'})
    driver=webdriver.Chrome(service=Service(shutil.which('chromedriver')),options=opts)
    driver.set_page_load_timeout(35);driver.execute_cdp_cmd('Network.enable',{})
    driver.execute_cdp_cmd('Network.setBlockedURLs',{'urls':['*pythonanywhere*','*tidbcloud*','*/api/*']})
    wait=WebDriverWait(driver,25,poll_frequency=.1)
    results=[];errors=[]
    manifest=json.loads((root/'original-manifest.json').read_bytes())
    def data(path):return json.loads((root/path.lstrip('/')).read_bytes())
    def ready():
        # Navigation can replace <body> between WebDriver calls. Read readiness
        # atomically in page JavaScript so the test does not hold stale elements.
        state=wait.until(lambda d:d.execute_script(
            "const b=document.body;if(!b)return '';"
            "if(b.dataset.staticError==='true')return 'error';"
            "if(b.dataset.staticReady==='true')return 'ready';"
            "return '';"
        ))
        if state=='error':
            text=driver.execute_script("return document.body ? document.body.innerText : ''")
            raise AssertionError(text[-1500:])
    def click(element):
        # The original CSS scrolls smoothly. Ensure visibility, then make a real
        # WebDriver click rather than bypassing hit testing with a script click.
        driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})",element)
        wait.until(lambda d:d.execute_script("const r=arguments[0].getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight+1",element))
        element.click()
    try:
        for sport in ['mlb','nfl','nhl']:
            cat=data(manifest['sports'][sport]);entry=next(r for r in cat['reports'] if cat['sections'].get(r['id']))
            driver.get(base+('/' if sport=='mlb' else '/'+sport+'/'));ready()
            wait.until(lambda d:d.execute_script('return document.fonts.status')=='loaded')
            assert 'Start with a team.' in driver.find_element(By.TAG_NAME,'h1').text
            driver.save_screenshot(str(out/(sport+'-home-desktop.png')))
            driver.set_window_size(390,844);driver.save_screenshot(str(out/(sport+'-home-mobile.png')))
            assert not driver.execute_script('return document.documentElement.scrollWidth>innerWidth+1')
            driver.set_window_size(1440,1100)
            link=driver.find_element(By.CSS_SELECTOR,'.nfl-stadium-card[href="/reports/'+entry['id']+'.html"]')
            click(link);ready()
            assert driver.find_elements(By.ID,'section-jump')
            driver.save_screenshot(str(out/(sport+'-report.png')))
            Select(driver.find_element(By.ID,'section-jump')).select_by_index(1)
            click(driver.find_element(By.CSS_SELECTOR,'[data-section-jump-button]'));ready()
            assert driver.find_elements(By.ID,'venue-section-timeline-data')
            assert driver.find_elements(By.CSS_SELECTOR,'#section-games summary')
            driver.save_screenshot(str(out/(sport+'-section.png')))
            usable=[]
            for group,path in cat['options'].items():
                options=data(path)
                for row in options['games']:
                    g=cat['games'].get(row['value'])
                    if g and g['section_count'] and g['capture_count']>1:
                        record=data(g['file']);choices=[s for s in record['sections'] if s['name'] in options['sections_by_game'].get(g['id'],[]) and s['points']>1]
                        if choices:usable.append((g['capture_count'],group,g,choices[0]));break
                if usable:break
            assert usable,'No browser sample';_,group,game,section=max(usable,key=lambda x:x[0])
            driver.get(base+('/' if sport=='mlb' else '/'+sport+'/'));ready()
            if sport=='mlb':click(driver.find_element(By.CSS_SELECTOR,'[data-target="game-panel"]'))
            form=driver.find_element(By.CSS_SELECTOR,'#game-panel .selection-form' if sport=='mlb' else '.'+sport+'-selection-form')
            Select(form.find_element(By.CSS_SELECTOR,'.place-select')).select_by_value(group)
            wait.until(lambda d:len(form.find_elements(By.CSS_SELECTOR,'.game-select option'))>1)
            Select(form.find_element(By.CSS_SELECTOR,'.game-select')).select_by_value(game['id'])
            Select(form.find_element(By.CSS_SELECTOR,'.section-select')).select_by_value(section['name'])
            start=time.monotonic();click(form.find_element(By.CSS_SELECTOR,'.submit-analysis'));ready()
            assert driver.find_elements(By.CSS_SELECTOR,'.interactive-chart__line')
            actual=driver.execute_script('return window.__staticChart')
            expected=data(section['file'])['sections'][section['key']]
            assert actual['x']==expected['x'] and actual['y']==expected['y'],'Native chart changed published observations'
            driver.save_screenshot(str(out/(sport+'-graph.png')))
            click(driver.find_element(By.CSS_SELECTOR,'.view-toggle' if sport=='mlb' else '.nfl-chart-actions a:last-child'));ready()
            relative=driver.execute_script('return window.__staticChart');assert relative['display']=='percentage'
            expected_y=expected['y'] if expected['y'][0]==0 else [100]+[round((v/expected['y'][0])*100) for v in expected['y'][1:]]
            assert relative['y']==expected_y,'Relative normalization differs'
            if sport!='mlb':
                click(driver.find_element(By.CSS_SELECTOR,'.nfl-chart-actions a:first-child'));ready()
                assert driver.find_elements(By.CSS_SELECTOR,'[data-stadium-map] path')
                search=driver.find_element(By.CSS_SELECTOR,'[data-map-search]');search.send_keys(section['name'])
                click(driver.find_element(By.CSS_SELECTOR,'[data-map-search-button]'))
                wait.until(lambda d:d.find_element(By.CSS_SELECTOR,'[data-section-name]').text==section['name'])
                assert driver.find_element(By.CSS_SELECTOR,'[data-section-history]').get_attribute('href')
                driver.save_screenshot(str(out/(sport+'-map.png')))
            results.append({'sport':sport,'native_home_report_section_graph':True,'maps':sport!='mlb',
                            'points':len(actual['x']),'original_chart_values_match':True,'flow_seconds':round(time.monotonic()-start,2)})
            errors += [e for e in driver.get_log('browser') if e['level']=='SEVERE']
        mlb=data(manifest['sports']['mlb']);venue=next(v for v,p in mlb['market'].items() if any(data(f)['percentage']['y'] for f in data(p).values()))
        market=data(mlb['market'][venue]);section=next(s for s,f in market.items() if data(f)['percentage']['y']);expected=data(market[section])
        driver.get(base+'/graph/?'+urlencode({'event':venue,'section':section}));ready()
        actual=driver.execute_script('return window.__staticChart');assert actual['x']==expected['money']['x'] and actual['y']==expected['money']['y']
        driver.get(base+'/predict/?'+urlencode({'event':venue,'section':section}));ready()
        assert driver.find_element(By.CSS_SELECTOR,'.time-value strong').text==f"{expected['time']:.1f}"
        driver.save_screenshot(str(out/'mlb-buying-window.png'))
        errors += [e for e in driver.get_log('browser') if e['level']=='SEVERE']
        requests=[]
        for item in driver.get_log('performance'):
            msg=json.loads(item['message'])['message']
            if msg.get('method')=='Network.requestWillBeSent':requests.append(msg['params']['request'])
        disallowed=[r for r in requests if r['method'] not in ('GET','HEAD') or ('/api/' in r['url']) or ('pythonanywhere' in r['url']) or ('tidbcloud' in r['url'])]
        if errors:raise AssertionError('Browser errors: '+str(errors[:4]))
        if disallowed:raise AssertionError('Unexpected API/production/write requests')
        result={'passed':True,'flows':results,'legacy_market_and_buying_window':True,'api_requests':0,'write_requests':0,'console_errors':0,
                'note':'Local CI static server; not public Render timings. Original external Google Fonts allowed.'}
        (out/'browser-report.json').write_text(json.dumps(result,indent=2));return result
    except Exception:
        driver.save_screenshot(str(out/'failure.png'))
        (out/'failure.html').write_text(driver.page_source)
        (out/'console.json').write_text(json.dumps(driver.get_log('browser'),indent=2))
        raise
    finally:driver.quit();server.shutdown();server.server_close();thread.join(timeout=5)


def main():
    p=argparse.ArgumentParser();p.add_argument('directory');p.add_argument('--browser',action='store_true');p.add_argument('--output',default='original-browser-results');args=p.parse_args()
    print('ORIGINAL_FILES_VERIFIED '+json.dumps(validate(args.directory)),flush=True)
    if args.browser:print('ORIGINAL_BROWSER_REPORT '+json.dumps(browser_check(args.directory,args.output)),flush=True)
if __name__=='__main__':main()
