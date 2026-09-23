"""Bounded application-level preview checks, not a production deployment.

Only read-guarded staging engines are used. No Selenium, ingestion or refresh.
Diagnostics contain types/locations and counts, never SQL parameters or secrets.
"""
from __future__ import annotations
import json
import resource
import signal
import time
import traceback

from flask import template_rendered, got_request_exception
from sqlalchemy import event
from Flask_App.staging_site import create_app
from Flask_App import staging_site_config as cfg


def main():
    app = create_app()
    app.logger.disabled = True
    paths = {str(r) for r in app.url_map.iter_rules()}
    client = app.test_client()
    checks, errors = [], []
    sql_stats = {'completed': 0}

    def record_error(_sender, exception, **_kw):
        frames = traceback.extract_tb(exception.__traceback__)
        errors.append({'type': type(exception).__name__, 'locations': [
            {'function': f.name, 'line': f.lineno} for f in frames[-8:]]})
        print('REQUEST_EXCEPTION ' + json.dumps(errors[-1]), flush=True)

    got_request_exception.connect(record_error, app, weak=False)

    def deadline(_signum, _frame):
        raise TimeoutError('Preview request exceeded the 120-second test budget.')

    def after_sql(_a, _b, _c, _d, _e, _f):
        sql_stats['completed'] += 1
        if sql_stats['completed'] % 100 == 0:
            print('Completed SQL reads: ' + str(sql_stats['completed']), flush=True)

    def get(path, *, query=None, expected=200):
        started = time.monotonic()
        print('REQUEST ' + path, flush=True)
        contexts = []
        def capture(_sender, template, context, **_kw): contexts.append(context)
        old_handler = signal.signal(signal.SIGALRM, deadline)
        signal.alarm(120)
        try:
            with template_rendered.connected_to(capture, app):
                response = client.get(path, query_string=query)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
        check = {'path': path, 'status': response.status_code, 'seconds': round(time.monotonic()-started, 3)}
        checks.append(check); print('CHECK ' + json.dumps(check), flush=True)
        if response.status_code != expected:
            raise RuntimeError('Unexpected response on ' + path + ': ' + str(response.status_code))
        if response.headers.get('X-TicketSignal-Environment') != 'staging-readonly':
            raise RuntimeError('Missing staging label.')
        if response.mimetype == 'text/html' and response.status_code == 200 and b'ticketsignal-staging-banner' not in response.data:
            raise RuntimeError('Missing visible snapshot banner.')
        return response, contexts

    try:
        get('/healthz'); get('/readyz')
        for sport in cfg.SCHEMAS:
            event.listen(cfg.engine_for(sport), 'after_cursor_execute', after_sql)
        response, contexts = get('/')
        home = contexts[-1]
        if not home.get('event_count') or not home.get('games_dict'):
            raise RuntimeError('MLB home has no imported games.')
        get('/baseball')
        for candidates in (('/nfl', '/football'), ('/nhl', '/hockey')):
            available = [p for p in candidates if p in paths]
            if not available: raise RuntimeError('Missing sport landing route.')
            get(available[0])
        venue = next(iter(home['games_dict']))
        response, _ = get('/api/baseball/options', query={'venue': venue})
        options = response.get_json()
        choices = [(game, sections[0]) for game, sections in options.get('sections_by_game', {}).items() if sections]
        if not choices: raise RuntimeError('No selectable MLB game sections.')
        game, section = choices[0]
        response, contexts = get('/graph', query={'event': venue, 'game': game, 'section': section, 'mode': 'single', 'display':'money'})
        graph = contexts[-1] if contexts else {}
        if not graph.get('chartX') or not graph.get('chartY'):
            raise RuntimeError('Selected MLB graph rendered without data.')
        for path in ('/api/collector/snapshot', '/api/nfl/snapshot', '/api/nhl/snapshot', '/api/analytics/backfill'):
            response = client.post(path, json={})
            if response.status_code != 409: raise RuntimeError('Preview did not block writes.')
        get('/concerts', expected=503)
        if cfg.BLOCKED_SQL:
            raise RuntimeError('Existing view attempted a blocked SQL operation: ' + ','.join(cfg.BLOCKED_SQL))
        print('STAGING_APP_REPORT ' + json.dumps({'passed':True,'checks':checks,'write_endpoints_blocked':4,
              'blocked_sql_attempts':0,'sql_reads':sql_stats['completed'],
              'max_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}), flush=True)
        print('PASS: Flask sports landing pages, MLB options and populated single-game graph; read-only TiDB engines. No production access or website deployment.', flush=True)
        return 0
    except Exception as error:
        print('STAGING_APP_REPORT ' + json.dumps({'passed':False,'checks':checks,'blocked_sql_verbs':cfg.BLOCKED_SQL,
              'request_errors':errors,'sql_reads':sql_stats['completed'],
              'error_type':type(error).__name__,'message':str(error) if type(error) is RuntimeError else 'Details omitted.'}), flush=True)
        return 1
    finally:
        got_request_exception.disconnect(record_error, app)
        cfg.clear_engines()


if __name__ == '__main__': raise SystemExit(main())
