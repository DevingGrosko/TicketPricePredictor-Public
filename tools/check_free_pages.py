"""Browser acceptance for the mounted Pages site; no production/API requests."""
from __future__ import annotations
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import threading
from urllib.parse import urlencode, urlsplit

from tools.free_refresh_publish import PREFIX, validate_mounted


class ProjectHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args): pass
    def do_GET(self):
        if not urlsplit(self.path).path.startswith(PREFIX+'/'):
            self.send_error(404, 'Outside project mount'); return
        # Keep the incoming URL intact so the standard directory-slash redirect
        # includes the mount prefix, as a real Pages project does.
        super().do_GET()
    def translate_path(self, path):
        return super().translate_path(path[len(PREFIX):] if path.startswith(PREFIX+'/') else path)


def check(root, output):
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select, WebDriverWait
    root=Path(root).resolve(); out=Path(output);out.mkdir(parents=True,exist_ok=True)
    stats=validate_mounted(root)
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(ProjectHandler,directory=str(root)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    origin=f'http://127.0.0.1:{server.server_port}'; base=origin+PREFIX
    options=webdriver.ChromeOptions()
    for flag in ('--headless=new','--no-sandbox','--disable-dev-shm-usage','--window-size=1440,1100'):
        options.add_argument(flag)
    options.set_capability('goog:loggingPrefs',{'browser':'ALL','performance':'ALL'})
    driver=webdriver.Chrome(service=Service(shutil.which('chromedriver')),options=options)
    driver.set_page_load_timeout(35);driver.execute_cdp_cmd('Network.enable',{})
    driver.execute_cdp_cmd('Network.setBlockedURLs',{'urls':['*pythonanywhere*','*tidbcloud*','*/api/*']})
    wait=WebDriverWait(driver,25,poll_frequency=.1)
    def read(path):return json.loads((root/path.lstrip('/')).read_bytes())
    def ready():
        state=wait.until(lambda d:d.execute_script("return document.body?.dataset.staticError==='true'?'error':document.body?.dataset.staticReady==='true'?'ready':''"))
        if state=='error':raise AssertionError(driver.find_element(By.TAG_NAME,'body').text[-1000:])
        if not driver.current_url.startswith(base+'/'):raise AssertionError('Navigation left project path')
    def click(item):
        driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})",item)
        item.click();ready()
    flows=[]
    try:
        manifest=read('original-manifest.json')
        for sport in ('mlb','nfl','nhl'):
            cat=read(manifest['sports'][sport]);home=base+('/' if sport=='mlb' else '/'+sport+'/')
            driver.get(home);ready()
            assert 'Start with a team.' in driver.find_element(By.TAG_NAME,'h1').text
            assert not any(n.is_displayed() for n in driver.find_elements(By.CSS_SELECTOR,'.static-snapshot-note'))
            driver.set_window_size(390,844)
            assert not driver.execute_script('return document.documentElement.scrollWidth>innerWidth+1')
            driver.set_window_size(1440,1100)
            entry=next(r for r in cat['reports'] if cat['sections'].get(r['id']))
            click(driver.find_element(By.CSS_SELECTOR,'.nfl-stadium-card[href="'+PREFIX+'/reports/'+entry['id']+'.html"]'))
            Select(driver.find_element(By.ID,'section-jump')).select_by_index(1)
            click(driver.find_element(By.CSS_SELECTOR,'[data-section-jump-button]'))
            assert driver.find_elements(By.ID,'venue-section-timeline-data')
            assert driver.find_elements(By.CSS_SELECTOR,'#section-games summary')
            chosen=None
            for group,filename in cat['options'].items():
                opts=read(filename)
                for item in opts['games']:
                    game=cat['games'].get(item['value'])
                    if not game or not game['section_count'] or game['capture_count']<2:continue
                    record=read(game['file'])
                    sections=[s for s in record['sections'] if s['points']>1 and s['name'] in opts['sections_by_game'].get(game['id'],[])]
                    if sections:chosen=(group,game,sections[0]);break
                if chosen:break
            assert chosen,'No chart sample';group,game,section=chosen
            driver.get(home);ready()
            if sport=='mlb':
                item=driver.find_element(By.CSS_SELECTOR,'[data-target="game-panel"]')
                driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})",item);item.click()
            form=driver.find_element(By.CSS_SELECTOR,'#game-panel .selection-form' if sport=='mlb' else '.'+sport+'-selection-form')
            Select(form.find_element(By.CSS_SELECTOR,'.place-select')).select_by_value(group)
            wait.until(lambda d:len(form.find_elements(By.CSS_SELECTOR,'.game-select option'))>1)
            Select(form.find_element(By.CSS_SELECTOR,'.game-select')).select_by_value(game['id'])
            Select(form.find_element(By.CSS_SELECTOR,'.section-select')).select_by_value(section['name'])
            click(form.find_element(By.CSS_SELECTOR,'.submit-analysis'))
            actual=driver.execute_script('return window.__staticChart');expected=read(section['file'])['sections'][section['key']]
            assert actual['x']==expected['x'] and actual['y']==expected['y']
            click(driver.find_element(By.CSS_SELECTOR,'.view-toggle' if sport=='mlb' else '.nfl-chart-actions a:last-child'))
            relative=driver.execute_script('return window.__staticChart')
            assert relative['display']=='percentage'
            assert relative['y']==(expected['y'] if expected['y'][0]==0 else [round(v/expected['y'][0]*100) for v in expected['y']])
            if sport!='mlb':
                click(driver.find_element(By.CSS_SELECTOR,'.nfl-chart-actions a:first-child'))
                assert driver.find_elements(By.CSS_SELECTOR,'[data-stadium-map] path')
                driver.find_element(By.CSS_SELECTOR,'[data-map-search]').send_keys(section['name'])
                item=driver.find_element(By.CSS_SELECTOR,'[data-map-search-button]');item.click()
                wait.until(lambda d:d.find_element(By.CSS_SELECTOR,'[data-section-name]').text==section['name'])
                link=driver.find_element(By.CSS_SELECTOR,'[data-section-history]')
                assert link.get_attribute('href').startswith(base+'/')
            driver.save_screenshot(str(out/(sport+'-flow.png')))
            flows.append({'sport':sport,'passed':True,'chart_points':len(actual['x']),'project_navigation':True})
        cat=read(manifest['sports']['mlb'])
        venue=next(v for v,p in cat['market'].items() if any(read(f)['percentage']['y'] for f in read(p).values()))
        market=read(cat['market'][venue]);section=next(s for s,p in market.items() if read(p)['percentage']['y']);expected=read(market[section])
        query=urlencode({'event':venue,'section':section})
        driver.get(base+'/graph/?'+query);ready();actual=driver.execute_script('return window.__staticChart')
        assert actual['x']==expected['money']['x'] and actual['y']==expected['money']['y']
        driver.get(base+'/predict/?'+query);ready()
        assert driver.find_element(By.CSS_SELECTOR,'.time-value strong').text==f"{expected['time']:.1f}"
        errors=[x for x in driver.get_log('browser') if x['level']=='SEVERE']
        if errors:raise AssertionError(str(errors[:4]))
        for item in driver.get_log('performance'):
            msg=json.loads(item['message'])['message']
            if msg.get('method')!='Network.requestWillBeSent':continue
            request=msg['params']['request'];url=urlsplit(request['url'])
            assert request['method'] in ('GET','HEAD')
            assert url.hostname in ('127.0.0.1','fonts.googleapis.com','fonts.gstatic.com') or url.scheme=='data'
            if url.hostname=='127.0.0.1':assert url.path.startswith(PREFIX+'/') and '/api/' not in url.path
        result={'passed':True,'flows':flows,'market_and_buying_window':True,'severe_console_errors':0,
                'public_api_requests':0,'files':stats,'note':'Local mounted static server, not public Pages deployment.'}
        (out/'report.json').write_text(json.dumps(result,indent=2));print('FREE_PAGES_BROWSER '+json.dumps(result),flush=True)
        return result
    except Exception:
        driver.save_screenshot(str(out/'failure.png'))
        (out/'failure.html').write_text(driver.page_source)
        (out/'console.json').write_text(json.dumps(driver.get_log('browser')))
        raise
    finally:
        driver.quit();server.shutdown();server.server_close();thread.join(timeout=5)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('directory');p.add_argument('--output',default='free-browser-results');a=p.parse_args()
    check(a.directory,a.output)
