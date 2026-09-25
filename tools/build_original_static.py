"""Build the original TicketSignal templates over isolated staging snapshots.

Only build-time SELECTs are permitted. Nothing is deployed or scheduled here.
The original production templates/CSS/JavaScript are source inputs, never edited.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time
from urllib.parse import urlencode
from unittest.mock import patch

from flask import Flask, render_template
from tools import build_static_preview as source

ROOT = Path(__file__).resolve().parents[1]
PATHS = {
    'home': '/', 'graph': '/graph/', 'predict': '/predict/',
    'baseball_options': '/api/baseball/options', 'concerts_home': '/concerts/',
    'concerts_graph': '/concerts/graph/',
    'nfl.nfl_home': '/nfl/', 'nhl.nhl_home': '/nhl/',
    'nfl.nfl_graph': '/nfl/graph/', 'nhl.nhl_graph': '/nhl/graph/',
    'nfl.nfl_map': '/nfl/map/', 'nhl.nhl_map': '/nhl/map/',
    'nfl.nfl_archive': '/nfl/archive/', 'nhl.nhl_archive': '/nhl/archive/',
    'nfl.nfl_options': '/api/nfl/options', 'nhl.nhl_options': '/api/nhl/options',
    'nfl_stadium.mlb_stadium': '/baseball/stadium/',
    'nfl_stadium.mlb_section': '/baseball/stadium/section/',
    'nfl_stadium.nfl_stadium': '/nfl/stadium/',
    'nfl_stadium.nfl_section': '/nfl/stadium/section/',
    'nfl_stadium.nhl_arena': '/nhl/arena/',
    'nfl_stadium.nhl_section': '/nhl/arena/section/',
}
HOME = {'mlb': 'home', 'nfl': 'nfl.nfl_home', 'nhl': 'nhl.nhl_home'}
REPORT = {'mlb': 'nfl_stadium.mlb_stadium', 'nfl': 'nfl_stadium.nfl_stadium', 'nhl': 'nfl_stadium.nhl_arena'}
SECTION = {'mlb': 'nfl_stadium.mlb_section', 'nfl': 'nfl_stadium.nfl_section', 'nhl': 'nfl_stadium.nhl_section'}
MAX_NATIVE_BYTES = 600 * 1024 * 1024
TOKEN = 'TSVALUE_'


def digest(value):
    return hashlib.sha256(source.encoded(value)).hexdigest()


def market_history(histories, display='money', hours=48):
    """Original quarter-hour alignment, weighting and sample threshold.

    Unlike report buckets, this is the legacy market tool's exact algorithm.
    Each history is (hours-before-event, prices), already in capture order.
    """
    def standardize(values):
        if not values or values[0] == 0: return values
        return [100] + [round((v / values[0]) * 100) for v in values[1:]]
    bins = {i + offset: [] for i in reversed(range(hours)) for offset in (.75, .5, .25, 0)}
    keys = list(bins)
    total = 0
    for x, y in histories:
        pairs = [(a, b) for a, b in zip(x, y) if a <= hours]
        if not pairs:
            continue
        total += 1
        if display != 'money':
            px, py = zip(*pairs)
            pairs = list(zip(px, standardize(list(py))))
        i = j = 0
        while j < len(keys) and i < len(pairs):
            if keys[j] - .125 <= pairs[i][0] < keys[j] + .125:
                bins[keys[j]].append(pairs[i][1]); i += 1; j += 1
            elif pairs[i][0] > keys[j] + .125:
                i += 1
            else:
                j += 1
    minimum = 2 if total >= 2 else 1
    bins = {k: v for k, v in bins.items() if len(v) >= minimum}
    return {'x': list(bins), 'y': [sum(v) / len(v) for v in bins.values()], 'total': total}


class OriginalPages:
    def __init__(self, bundle, now):
        self.bundle, self.root, self.now = bundle, bundle.root, now
        self.assets, self.pages, self.native_bytes = {}, 0, 0
        self.landing_html = None
        self.indexes, self.routes = {}, {}
        self.catalog = {'version': 1, 'presentation': 'original-templates', 'live_updates_enabled': False,
                        'generated_at': now.isoformat(), 'sports': {}, 'routes': {}}
        self.app = Flask('static-original-build', template_folder=str(ROOT/'Flask_App/templates'))
        self.app.jinja_env.globals['url_for'] = self.url
        self.app.jinja_env.globals['mlb_team_for_venue'] = lambda venue: venue
        for endpoint, path in PATHS.items():
            self.app.add_url_rule(path, endpoint, lambda: '')
        self.context = self.app.test_request_context('/')
        self.context.push()
        self.api = None

    def close(self):
        self.context.pop()

    def write(self, relative, content):
        target = self.root / relative.lstrip('/')
        raw = content.encode() if isinstance(content, str) else content
        if not target.exists():
            self.native_bytes += len(raw)
        if self.native_bytes > MAX_NATIVE_BYTES:
            raise source.BuildError('Original-interface files exceeded the publication budget.')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        return '/' + relative.lstrip('/')

    def blob(self, value):
        raw = source.encoded(value)
        if len(raw) > 3 * 1024 * 1024:
            raise source.BuildError('One native data payload exceeded its size budget.')
        return self.write('native/data-'+hashlib.sha256(raw).hexdigest()+'.json', raw)

    def read(self, file):
        return json.loads((self.root/file.lstrip('/')).read_bytes())

    def find_report(self, sport, team='', venue=''):
        rows = self.indexes.get(sport, {}).get('reports', [])
        if team:
            rows = [r for r in rows if r['team'].casefold() == team.casefold()]
        if venue:
            rows = [r for r in rows if self.api.report_venue(r['venue']).casefold() == self.api.report_venue(venue).casefold()]
        return rows[0] if rows else None

    def section_path(self, sport, report, section):
        identity = self.api.section_identity(sport, report['venue'], section)
        key = identity.key if identity else section
        return '/sections/'+digest([sport, report['id'], key])+'.html'

    def url(self, endpoint, **values):
        values = {k: v for k, v in values.items() if v is not None and not k.startswith('_')}
        if endpoint == 'static':
            return '/static/'+values['filename']
        for sport in HOME:
            if endpoint in (REPORT[sport], SECTION[sport]) and (values.get('team') or values.get('venue')):
                report = self.find_report(sport, str(values.get('team', '')), str(values.get('venue', '')))
                if report:
                    if endpoint == REPORT[sport]:
                        return '/reports/'+report['id']+'.html'
                    if values.get('section'):
                        return self.section_path(sport, report, values['section'])
            if endpoint == sport+'.'+sport+'_map' and str(values.get('game', '')).isdigit():
                game = str(values.pop('game'))
                return '/maps/'+sport+'-'+game+'.html'+('?' + urlencode(values) if values else '')
        path = PATHS.get(endpoint)
        if path is None:
            raise source.BuildError('Unmapped original endpoint: '+endpoint)
        return path + ('?'+urlencode(values) if values else '')

    def page(self, relative, template, endpoint, context, boot=None):
        with self.app.test_request_context(PATHS.get(endpoint, '/')):
            text = render_template(template, **context)
        # Keep all visible production markup and styling. Only script delivery
        # and the clearly identified snapshot notice are adapted for static use.
        def script(match):
            attrs, body = match.group(1), match.group(2)
            if 'application/json' in attrs:
                value = json.loads(body)
                selected = None
                if 'map-data' in attrs and isinstance(value, dict):
                    value = dict(value)
                    selected = value.pop('selected_section', None)
                path = self.blob(value)
                selection = '' if selected is None else ' data-selected-section="'+html.escape(str(selected), quote=True)+'"'
                return '<script'+attrs+' data-static-json="'+path+'"'+selection+'>{}</script>'
            src = re.search(r'\bsrc="([^"]+)"', attrs)
            if src:
                path = html.unescape(src.group(1))
            else:
                if not body.strip():
                    return ''
                path = self.write('native/script-'+hashlib.sha256(body.encode()).hexdigest()+'.js', body)
            return '<script type="text/plain" data-original-script="'+html.escape(path, quote=True)+'"></script>'
        text = re.sub(r'<script\b([^>]*)>(.*?)</script>', script, text, flags=re.S)
        # The PNG fallback would require rendering >70k redundant server plots.
        text = re.sub(r'<noscript>.*?</noscript>', '<noscript><p class="form-note">JavaScript is required for the published interactive chart.</p></noscript>', text, flags=re.S)
        text = text.replace('Live + historical', 'Saved snapshot')
        if boot and boot.get('kind') == 'home':
            alias = 'explore' if boot['sport'] == 'mlb' else 'explore-' + boot['sport']
            text = text.replace('<section class="explore-section mlb-detailed-tools"', '<span id="' + alias + '"></span><section class="explore-section mlb-detailed-tools"')
            text = text.replace('<section class="nfl-explore nfl-single-game-explorer"', '<span id="' + alias + '"></span><section class="nfl-explore nfl-single-game-explorer"')
        if boot and boot.get('kind') in ('graph', 'predict'):
            text = text.replace('<body ', '<body data-static-pending="true" ', 1)
        notice = ('<aside class="static-snapshot-note" role="note">Staging preview · saved September 21 export · '
                  'automatic updates are not enabled. <span data-source-freshness></span></aside>')
        text = text.replace('<main>', notice+'\n<main>', 1)
        cfg = self.blob(boot or {})
        text = text.replace('</head>', '<link rel="icon" href="data:,"><meta name="robots" content="noindex,nofollow"><link rel="stylesheet" href="/native/preview.css">\n</head>')
        text = text.replace('</body>', '<script src="/native/bridge.js" data-static-boot="'+cfg+'"></script>\n</body>')
        self.pages += 1
        if relative == 'index.html': self.landing_html = text
        return self.write(relative, text)

    def config(self, sport, currency='USD'):
        if sport == 'mlb': return self.api._mlb_page_config()
        if sport == 'nfl': return self.api._nfl_page_config()
        return self.api._nhl_page_config(currency)

    def render_sport(self, sport, spool, events, latest, captures, bundle, now, api, public, prepared, menu, index):
        self.api = api
        self.indexes[sport] = index
        self.app.jinja_env.globals['mlb_team_for_venue'] = api.mlb_team_for_venue
        self.app.jinja_env.globals['is_parking_section'] = api.is_parking_section
        clean_geometry = api.sanitize_map_geometry
        geometry_cache = {}
        def cached_geometry(geometry, names):
            key = (id(geometry), tuple(names))
            if key not in geometry_cache: geometry_cache[key] = clean_geometry(geometry, names)
            return geometry_cache[key]
        with patch.object(api, 'url_for', self.url), patch.object(api, 'sanitize_map_geometry', cached_geometry):
            self._sport(sport, spool, events, latest, captures, public, prepared, menu, index)

    def _sport(self, sport, spool, events, latest, captures, public, prepared, menu, index):
        api = self.api
        labeler = {'mlb': api.format_mlb_title, 'nfl': api.format_nfl_title, 'nhl': api.format_nhl_title}[sport]
        for e in events.values():
            value = getattr(e, 'map_geometry', None)
            if isinstance(value, str): e.map_geometry = json.loads(value)
        directory = api._generic_venue_index(list(events.values()), self.now,
                        venue_getter=lambda e: api._event_venue_for_sport(e, sport),
                        team_getter=lambda e: api._home_team_for_report(e, sport), endpoint=REPORT[sport])
        groups = defaultdict(list)
        for e in sorted(public.values(), key=lambda e: e.event_date):
            groups[e.Place if sport == 'mlb' else api._home_team_for_report(e, sport)].append(e)
        option_paths = {}
        for key, rows in sorted(groups.items()):
            opts = {'games': [], 'sections_by_game': {}, 'multi_sections': []}
            occurrences = defaultdict(set)
            for e in rows:
                sections = api._public_sections(e.event_sections if sport == 'mlb' else e.sections)
                opts['games'].append({'value': str(e.id), 'label': labeler(e)})
                opts['sections_by_game'][str(e.id)] = sections
                for sec in sections: occurrences[sec].add(e.id)
            opts['multi_sections'] = sorted([sec for sec, ids in occurrences.items() if len(ids) > 1], key=str.casefold)
            option_paths[key] = self.blob(opts)
        games = {str(eid): {**meta, 'label': labeler(events[eid]), 'place': events[eid].Place if sport == 'mlb' else meta['venue']} for eid, meta in menu.items()}
        catalog = {'options': option_paths, 'games': games, 'reports': index['reports'],
                   'captured_through': source.utc_iso(max((v for v in latest.values() if v), default=None)),
                   'market': {}, 'sections': {}}
        self.catalog['sports'][sport] = catalog
        home_context = {'team_reports': directory, 'games_dict': dict(sorted(groups.items())),
                        'event_count': len(public), 'game_count': len(public), 'team_count': len(groups),
                        'completed_count': sum(api._event_completed(e, self.now) for e in public.values()),
                        'arena_count': len({api._event_venue_for_sport(e, sport) for e in public.values()}),
                        'currency_label': 'USD / CAD' if sport == 'nhl' else 'USD', 'compacted_count': 0}
        self.page(('index.html' if sport == 'mlb' else sport+'/index.html'),
                  {'mlb': 'HomeScreen.html', 'nfl': 'NFLHomeScreen.html', 'nhl': 'NHLHomeScreen.html'}[sport],
                  HOME[sport], home_context, {'kind': 'home', 'sport': sport})
        self.page(PATHS[REPORT[sport]].strip('/')+'/index.html', 'nfl_stadium.html', REPORT[sport],
                  {**self.config(sport), 'stadiums': directory, 'stadium_count': len(directory), 'selected_venue': '', 'error': None},
                  {'kind': 'report-router', 'sport': sport})
        by_report_sections = {}
        for entry in index['reports']:
            report = self.read(entry['file'])
            cohort = [events[int(g['id'])] for g in report['games']]
            all_sections = [dict(s) for s in report['sections']]
            for section in all_sections:
                section['detail_url'] = self.section_path(sport, entry, section['name'])
            lookup = {s['section_key']: s for s in all_sections}
            base = {**self.config(sport, report['currency']), 'error': None,
                    'selected_venue': report['venue'], 'selected_team': report['team'], 'selected_team_label': report['team'],
                    'report_season': report['season'], 'stadiums': directory, 'stadium_count': len(directory),
                    'venue_options': sorted({r['venue'] for r in index['reports'] if r['team'] == report['team']}),
                    'all_sections': all_sections, 'cheapest_sections': [lookup[k] for k in report['cheapest']],
                    'biggest_drops': [lookup[k] for k in report['drops']], 'game_count': len(cohort)}
            self.page('reports/'+entry['id']+'.html', 'nfl_stadium.html', REPORT[sport], base,
                      {'kind': 'report', 'sport': sport, 'report': entry['id']})
            rows_by_section = defaultdict(list)
            ids = {e.id for e in cohort}
            for (key, eid), points in prepared.items():
                if eid not in ids: continue
                rows_by_section[key].extend({'event_id': eid, 'section_key': key, 'slot': p['slot'],
                        'price': p['price'], 'section': p['section_name'], 'observation_count': p['observation_count'],
                        'first_captured_at': p.get('first_captured_at'), 'last_captured_at': p.get('last_captured_at')} for p in points)
            def game_url(e, section):
                return self.url('graph' if sport == 'mlb' else sport+'.'+sport+'_graph',
                            **({'event': e.Place, 'mode': 'single'} if sport == 'mlb' else {'team': api._home_team_for_report(e, sport)}),
                            game=str(e.id), section=section)
            def map_url(e, section):
                return self.url(sport+'.'+sport+'_map', team=api._home_team_for_report(e, sport), game=str(e.id), section=section)
            for section in all_sections:
                context = api._build_section_detail_context(base, cohort, rows_by_section[section['section_key']], section['name'], self.now,
                    sport_key=sport, currency=report['currency'], report_endpoint=REPORT[sport],
                    section_getter=lambda e: e.event_sections if sport == 'mlb' else e.sections,
                    geometry_getter=(None if sport == 'mlb' else lambda e: getattr(e, 'map_geometry', None)),
                    event_label_builder=labeler, game_url_builder=game_url,
                    map_url_builder=None if sport == 'mlb' else map_url,
                    source_url_getter=lambda e: e.URL if sport == 'mlb' else e.source_url,
                    buying_window_url_builder=(lambda s: self.url('predict', event=report['venue'], section=s)) if sport == 'mlb' else None)
                path = self.section_path(sport, entry, section['name'])
                self.page(path, 'venue_section.html', SECTION[sport], context,
                          {'kind': 'section', 'sport': sport, 'report': entry['id']})
                by_report_sections.setdefault(entry['id'], []).append({'name': section['name'], 'key': section['section_key'], 'url': path})
        catalog['sections'] = {rid: self.blob(rows) for rid, rows in by_report_sections.items()}
        if sport == 'mlb':
            self._market(spool, public, groups, option_paths, catalog)
        else:
            self._maps(sport, spool, public, latest, labeler)
        self._graph_shell(sport)
        print(f'ORIGINAL_UI {sport}: original templates rendered; {self.pages:,} pages so far', flush=True)

    def _market(self, spool, public, groups, option_paths, catalog):
        for venue, events in groups.items():
            opts = self.read(option_paths[venue])
            if not opts['multi_sections']: continue
            labels = set(opts['multi_sections'])
            histories = defaultdict(dict)
            for e in events:
                # Preserve raw quarter-hour observations (including near-zero
                # boundary values); do not substitute wider report buckets.
                for sec, price, lead in spool.execute('SELECT section,price,hours FROM raw WHERE event_id=? ORDER BY captured,id', (e.id,)):
                    if sec in labels:
                        pair = histories[sec].setdefault(e.id, ([], []))
                        pair[0].append(lead); pair[1].append(price)
            files = {}
            for label in opts['multi_sections']:
                rows = list(histories[label].values())
                absolute, relative = market_history(rows, 'money'), market_history(rows, 'percentage')
                files[label] = self.blob({'money': absolute, 'percentage': relative,
                    'time': relative['x'][relative['y'].index(min(relative['y']))] if relative['y'] else None})
            catalog['market'][venue] = self.blob(files)
        self.page('predict/index.html', 'lowestPrice.html', 'predict',
                  {'place': TOKEN+'place', 'section': TOKEN+'section', 'time': 0, 'totalGames': 0}, {'kind': 'predict', 'sport': 'mlb'})

    def _maps(self, sport, spool, public, latest, labeler):
        from Flask_App import nfl_blueprint as nfl
        from Flask_App import nhl_blueprint as nhl
        api = self.api
        for eid, e in public.items():
            # Values come ONLY from the latest stored capture, not a per-section
            # mix of different times. Listing counts are retained, not inferred.
            at = latest[eid]
            tickets = {name: (price, count) for name, price, count in spool.execute(
                      'SELECT section,price,listing_count FROM raw WHERE event_id=? AND captured=? ORDER BY id',
                      (eid, at.isoformat() if at else ''))}
            names = sorted(set(e.sections) | set(tickets), key=str.casefold)
            sections = [{'name': n, 'price': tickets.get(n, (None,None))[0], 'listing_count': tickets.get(n, (None,None))[1]} for n in names]
            geometry = nfl.sanitize_map_geometry(getattr(e, 'map_geometry', None), names)
            usable = nfl.geometry_is_usable(geometry, names)
            venue = api._event_venue_for_sport(e, sport)
            data = {'team': api._home_team_for_report(e, sport), 'game': str(eid), 'venue': venue,
                    'sections': sections, 'geometry': geometry, 'geometry_mode': 'provider' if usable else 'schematic',
                    'selected_section': '', 'graph_url': self.url(sport+'.'+sport+'_graph'), 'currency': e.currency}
            context = {'error': None, 'team': data['team'], 'venue': venue, 'city': getattr(e, 'city', '') or '', 'country': e.country or '',
                       'neutral_site': bool(e.neutral_site), 'game': str(eid), 'gameLabel': labeler(e),
                       'section_count': len(sections), 'priced_section_count': sum(s['price'] is not None for s in sections),
                       'latest_capture_label': (nfl if sport == 'nfl' else nhl).__dict__['format_'+sport+'_capture_label'](at),
                       'source_url': e.source_url, 'has_provider_geometry': usable, 'map_geometry_source': (geometry or {}).get('source',''),
                       'map_geometry_sections': nfl.geometry_section_count(geometry), 'map_data': data, 'currency': e.currency}
            if sport == 'nhl':
                context['visible_map_sections'] = {'total': len(sections), 'priced': context['priced_section_count']}
            self.page('maps/'+sport+'-'+str(eid)+'.html', sport+'_map.html', sport+'.'+sport+'_map', context,
                      {'kind': 'map', 'sport': sport, 'game': str(eid)})
        self._router(PATHS[sport+'.'+sport+'_map'], sport, 'map-router')

    def _router(self, path, sport, kind):
        # Original empty-state layout supplies a usable failure state as well.
        self.page(path.strip('/')+'/index.html', 'graph.html', HOME[sport],
                  {'error': 'Loading the published selection…', 'mode': 'single', 'place': '', 'section': '',
                   'totalGames': 0, 'game': '', 'displayType': 'percentage', 'displayLabel': '%'}, {'kind': kind, 'sport': sport})

    def _graph_shell(self, sport):
        self.page(('graph' if sport == 'mlb' else sport+'/graph')+'/index.html',
                  'graph.html' if sport == 'mlb' else sport+'_graph.html',
                  'graph' if sport == 'mlb' else sport+'.'+sport+'_graph',
                  {'place': TOKEN+'place', 'team': TOKEN+'team', 'venue': TOKEN+'venue', 'section': TOKEN+'section',
                   'gameLabel': TOKEN+'gameLabel', 'game': TOKEN+'game', 'currency': TOKEN+'currency',
                   'mode': 'single', 'displayType': 'percentage', 'displayLabel': '%', 'displayMode': 'money',
                   'chartX': [], 'chartY': [], 'img': '', 'totalGames': 0}, {'kind': 'graph', 'sport': sport})

    def finish(self):
        if self.landing_html is None: raise source.BuildError('Missing original MLB homepage.')
        self.write('index.html', self.landing_html)
        for path in (ROOT/'Flask_App/static').rglob('*'):
            if path.is_file() and path.suffix in {'.css', '.js', '.png', '.svg', '.jpg', '.jpeg', '.webp', '.ico'}:
                relative = path.relative_to(ROOT/'Flask_App/static')
                self.write('static/'+str(relative), path.read_bytes())
                self.assets[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.write('native/bridge.js', (ROOT/'static_original/bridge.js').read_bytes())
        self.write('native/preview.css', (ROOT/'static_original/preview.css').read_bytes())
        # Index data are split by sport; browsers do not fetch all sport catalogs.
        manifest = {k: v for k, v in self.catalog.items() if k != 'sports'}
        manifest['sports'] = {sport: self.blob(value) for sport, value in self.catalog['sports'].items()}
        self.write('original-manifest.json', source.encoded(manifest))
        self.write('original-assets.json', source.encoded(self.assets))
        self.write('robots.txt', 'User-agent: *\nDisallow: /\n')
        for sport in ('mlb','nfl','nhl'):
            self._router(PATHS[SECTION[sport]], sport, 'section-router')
        self.page('concerts/index.html', 'ConcertHomeScreen.html', 'concerts_home',
                  {'games_dict': {}, 'game_sections_dict': {}, 'concert_count': 0, 'venue_count': 0,
                   'section_count': 0, 'concerts_dict': {}, 'concert_sections_dict': {}, 'sections_dict': {}}, {'kind': 'concerts'})
        for legacy in ('app.js', 'styles.css'):
            (self.root/legacy).unlink(missing_ok=True)
        return {'original_pages': self.pages, 'original_asset_files': len(self.assets), 'native_bytes': self.native_bytes}


def build(output):
    from Flask_App import staging_site_config as settings
    from Flask_App import nfl_stadium_blueprint as api
    from models import event_datetime_utc
    settings.validate_environment()
    started = time.monotonic(); now = datetime.now(timezone.utc)
    destination = Path(output).resolve()
    if destination.exists(): raise source.BuildError('Output already exists; refusing replacement.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='original-static-', dir=destination.parent) as work:
        bundle = source.Bundle(Path(work)/'site')
        renderer = OriginalPages(bundle, now)
        manifest = {'version':1, 'mode':'historical-snapshot-preview', 'presentation':'original-templates',
                    'generated_at':now.isoformat(), 'live_updates_enabled':False, 'sports':[], 'source_counts':{}}
        try:
            for sport in source.SPORTS:
                spool = sqlite3.connect(Path(work)/(sport+'.sqlite'))
                try:
                    spool.execute('PRAGMA journal_mode=OFF'); spool.execute('PRAGMA synchronous=OFF')
                    events, latest, captures, counts = source.read_sport(sport, spool, settings, event_datetime_utc, include_maps=True)
                    manifest['source_counts'][sport] = counts
                    manifest['sports'].append(source.build_sport(sport, spool, events, latest, captures, bundle, now, api,
                                                                 page_builder=renderer.render_sport))
                finally:
                    spool.close(); source.parsed_capture.cache_clear()
                (Path(work)/(sport+'.sqlite')).unlink()
            if settings.BLOCKED_SQL: raise source.BuildError('Unexpected non-read SQL was blocked.')
            # Verify JSON first, then replace ONLY the independent static UI.
            stats = bundle.finish(manifest, ROOT/'static_preview')
            stats.update(renderer.finish())
            bundle.root.rename(destination)
        finally:
            renderer.close(); settings.clear_engines()
    report = {**stats, 'source_counts':manifest['source_counts'], 'seconds':round(time.monotonic()-started,2),
              'database_writes':0, 'deployed':False, 'live_updates_enabled':False}
    print('ORIGINAL_STATIC_BUILD '+json.dumps(report),flush=True)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--output',default='static-preview-dist')
    args=parser.parse_args()
    try: build(args.output)
    except Exception as exc:
        import traceback
        frames=traceback.extract_tb(exc.__traceback__)
        print('ORIGINAL_STATIC_FAILED '+json.dumps({'type':type(exc).__name__, 'message':str(exc) if isinstance(exc,source.BuildError) else 'Build details withheld.',
              'locations':[{'function':f.name,'line':f.lineno} for f in frames[-5:]]}),flush=True)
        return 1
    return 0

if __name__=='__main__': raise SystemExit(main())
