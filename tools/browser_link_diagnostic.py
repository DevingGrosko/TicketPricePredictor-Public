"""Temporary read-only diagnostic; never merged into production."""
from datetime import datetime
from types import SimpleNamespace
import json, re, traceback
from nfl_collector import VividNFLBrowser
from vivid_performer_routes import configure_schedule_navigation

url = 'https://www.vividseats.com/baltimore-ravens-tickets-baltimore-mt-bank-stadium-3-1-2026/production/6487365'
game = SimpleNamespace(event_date=datetime.fromisoformat('2026-10-25T17:00:00+00:00'), home_team='Baltimore Ravens')
browser = VividNFLBrowser(headless=False, timeout=45)
try:
    configure_schedule_navigation(browser, 'nfl', game, url)
    payload, event_date = browser.capture(url)
    print('DIAGNOSTIC_SUCCESS', json.dumps({'tickets':len(payload.get('tickets', [])), 'event_date':event_date.isoformat()}))
except Exception as error:
    first_line = str(error).splitlines()[0][:400]
    first_line = re.sub(r'https?://[^\s]+', '[public URL omitted]', first_line)
    print('DIAGNOSTIC_ERROR', json.dumps({'type':type(error).__name__, 'message':first_line, 'frames':[{'function':f.name, 'line':f.lineno, 'file':f.filename.rsplit('/',1)[-1]} for f in traceback.extract_tb(error.__traceback__)[-8:]], 'diagnostics':getattr(browser,'capture_diagnostics',{})}))
finally:
    browser.close()
