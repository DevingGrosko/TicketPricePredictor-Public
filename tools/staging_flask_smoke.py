"""Bounded application-level preview checks, not a production deployment.

The only database engines are the read-guarded staging engines. No exports are
loaded, no Selenium runs, and no ingestion or summary refresh is authorized.
"""
from __future__ import annotations
import json
import resource
import time

from flask import template_rendered
from Flask_App.staging_site import create_app
from Flask_App import staging_site_config as cfg


def main():
    app = create_app()
    app.logger.disabled = True  # Never publish raw exceptions/row values in CI.
    paths = {str(r) for r in app.url_map.iter_rules()}
    print('Registered GET paths: ' + json.dumps(sorted(str(r) for r in app.url_map.iter_rules() if 'GET' in r.methods)), flush=True)
    client = app.test_client()
    checks = []

    def get(path, *, query=None, expected=200):
        started = time.monotonic()
        contexts = []
        def capture(_sender, template, context, **_kw): contexts.append(context)
        with template_rendered.connected_to(capture, app):
            response = client.get(path, query_string=query)
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
              'blocked_sql_attempts':0,'max_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}), flush=True)
        print('PASS: Flask sports landing pages, MLB options and populated single-game graph; read-only TiDB engines. No production access or website deployment.', flush=True)
        return 0
    except Exception as error:
        print('STAGING_APP_REPORT ' + json.dumps({'passed':False,'checks':checks,'blocked_sql_verbs':cfg.BLOCKED_SQL,
              'error_type':type(error).__name__,'message':str(error) if type(error) is RuntimeError else 'Details omitted.'}), flush=True)
        return 1
    finally:
        cfg.clear_engines()


if __name__ == '__main__': raise SystemExit(main())
