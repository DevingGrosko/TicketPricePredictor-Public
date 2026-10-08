"""Read-only stock-browser capture canary; no database or upload calls."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

NORMAL_ROUTES = {
    '7302493': ('https://www.vividseats.com/boston-bruins-tickets--sports-nhl-hockey/performer/104', '2026-10-08T23:00:00+00:00'),
    '7301789': ('https://www.vividseats.com/boston-bruins-tickets--sports-nhl-hockey/performer/104', '2026-10-10T17:00:00+00:00'),
    '6493143': ('https://www.vividseats.com/en/new-orleans-saints-tickets--sports-nfl-football/performer/597', '2026-10-11T17:00:00+00:00'),
}

def events_from_json(text):
    from collector import validated_vivid_url
    events = json.loads(text)
    if not isinstance(events, list) or not 2 <= len(events) <= 4:
        raise ValueError('Canary requires two to four explicit NFL/NHL events')
    result, identities = [], set()
    for event in events:
        required, optional = {'sport', 'url'}, {'home_team', 'event_date'}
        if (not isinstance(event, dict) or not required <= set(event) or set(event) - required - optional
                or bool(optional & set(event)) != (optional <= set(event)) or event['sport'] not in ('nfl', 'nhl')):
            raise ValueError('Each canary event needs a supported sport and public event URL')
        parsed = urlsplit(event['url'])
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError('Canary URLs cannot contain queries, fragments or credentials')
        url = validated_vivid_url(event['url'])
        parsed = urlsplit(url)
        pid = parsed.path.rstrip('/').split('/')[-1]
        if not re.fullmatch(r'\d{1,12}', pid) or pid in identities:
            raise ValueError('Canary event production IDs must be distinct')
        identities.add(pid)
        clean = dict(sport=event['sport'], url=url, production_id=pid)
        if optional <= set(event):
            from vivid_performer_routes import performer_url
            if not isinstance(event['home_team'], str) or not isinstance(event['event_date'], str):
                raise ValueError('Canary schedule identity needs a team name and aware event date')
            team = event['home_team'].strip()
            performer_url(event['sport'], team)  # Validate against the observed public directory.
            stamp = datetime.fromisoformat(event['event_date'])
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError('Canary event date requires an explicit UTC offset')
            clean.update(home_team=team, event_date=stamp.astimezone(timezone.utc).isoformat())
        result.append(clean)
    if {event['sport'] for event in result} != {'nfl', 'nhl'}:
        raise ValueError('Canary must include both NFL and NHL')
    return result


def normal_route(event):
    if 'home_team' in event and 'event_date' in event:
        from vivid_performer_routes import performer_url
        return performer_url(event['sport'], event['home_team']), datetime.fromisoformat(event['event_date'])
    try:
        url, stamp = NORMAL_ROUTES[event['production_id']]
    except KeyError:
        raise ValueError('Normal navigation needs an explicit known route or paired schedule identity') from None
    return url, datetime.fromisoformat(stamp)


def _write(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n')
    temporary.replace(path)


def safe_diagnostics(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    pid = str(value.get('production_id', ''))
    if re.fullmatch(r'\d{1,12}', pid):
        result['production_id'] = pid
    if value.get('engine') in ('firefox', 'chrome', 'webkit'):
        result['engine'] = value['engine']
    if type(value.get('document_status')) is int:
        result['document_status'] = value['document_status']
    phases = {'performer-navigation', 'performer-link-scan', 'event-link-click',
              'event-popup', 'event-domcontentloaded', 'event-reload', 'inventory-wait',
              'inventory-body', 'event-identity', 'complete'}
    for key in ('phase', 'timeout_phase'):
        if isinstance(value.get(key), str) and value[key] in phases:
            result[key] = value[key]
    def safe_elapsed(number):
        return type(number) in (int, float) and math.isfinite(number) and 0 <= number <= 600000
    if safe_elapsed(value.get('capture_elapsed_ms')):
        result['capture_elapsed_ms'] = value['capture_elapsed_ms']
    if isinstance(value.get('phase_times_ms'), dict):
        result['phase_times_ms'] = {key: number for key, number in value['phase_times_ms'].items()
                                   if key in phases and safe_elapsed(number)}
    if type(value.get('performer_document_status')) is int:
        result['performer_document_status'] = value['performer_document_status']
    if type(value.get('performer_domcontentloaded')) is bool:
        result['performer_domcontentloaded'] = value['performer_domcontentloaded']
    for key in ('performer_ready_state', 'performer_ready_state_at_click', 'event_ready_state'):
        if value.get(key) in ('loading', 'interactive', 'complete'):
            result[key] = value[key]
    if value.get('acquisition_method') in ('original-response-bidi', 'original-response-playwright'):
        result['acquisition_method'] = value['acquisition_method']
    if value.get('navigation_mode') in ('performer', 'direct'):
        result['navigation_mode'] = value['navigation_mode']
    for key in ('visible_event_link_clicked', 'event_opened_new_window', 'event_opened_native_popup'):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    for key in ('event_link_click_attempts', 'preclick_responses_ignored'):
        if type(value.get(key)) is int:
            result[key] = value[key]
    if value.get('last_link_error_type') in ('ElementNotInteractableException', 'StaleElementReferenceException'):
        result['last_link_error_type'] = value['last_link_error_type']
    runtime = value.get('runtime')
    if isinstance(runtime, dict):
        result['runtime'] = {key: runtime[key] for key in ('engine', 'selenium', 'playwright', 'browser_version', 'driver_version')
            if isinstance(runtime.get(key), str) and re.fullmatch(r'[A-Za-z0-9 .()_-]{1,100}', runtime[key])}
        if type(runtime.get('headed')) is bool:
            result['runtime']['headed'] = runtime['headed']
    result['responses'] = []
    for row in value.get('responses') or []:
        if (not isinstance(row, dict) or row.get('path') not in ('/hermes/api/v1/listings', '/hermes/api/v2/listings')
                or type(row.get('status')) is not int):
            continue
        clean = dict(path=row['path'], status=row['status'])
        if row.get('protocol') in ('h3', 'h2', 'http/1.1', 'http/2', 'http/3'):
            clean['protocol'] = row['protocol']
        if type(row.get('from_cache')) is bool:
            clean['from_cache'] = row['from_cache']
        query = row.get('query')
        if isinstance(query, dict):
            clean['query'] = {key: val for key, val in query.items()
                if key in ('productionId', 'currency', 'priceGroupId', 'quantity', 'recommended', 'sf',
                           'localizeCurrency', 'includeIpAddress')
                and isinstance(val, str) and re.fullmatch(r'[A-Za-z0-9.-]{1,24}', val)}
        for key in ('request_header_names', 'response_header_names'):
            if isinstance(row.get(key), list):
                clean[key] = [name for name in row[key] if name in ('accept', 'brand-name', 'content-type',
                    'cache-control', 'if-none-match', 'if-modified-since')]
        result['responses'].append(clean)
        if len(result['responses']) == 10:
            break
    return result


def run_canary(events, directory, *, timeout=45, pace_seconds=0, factory=None, normal_navigation=False, isolated_events=False, engine='firefox'):
    if engine not in ('firefox', 'webkit'):
        raise ValueError('Canary engine must be Firefox or WebKit')
    if engine == 'webkit':
        normal_navigation = True
    if type(pace_seconds) is not int or not 0 <= pace_seconds <= 90:
        raise ValueError('pace_seconds must be an integer between 0 and 90')
    if type(normal_navigation) is not bool or type(isolated_events) is not bool:
        raise ValueError('Canary navigation/session options must be boolean')
    routes = {event['production_id']: normal_route(event) for event in events} if normal_navigation else {}
    import selenium
    from nfl_collector import VividNFLBrowser, NFLSnapshotParser, nfl_snapshot_to_payload
    from nhl_collector import NHLSnapshotParser, nhl_snapshot_to_payload
    from vivid_inventory import validate_inventory
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    report = dict(status='running', database_calls=0, upload_calls=0, pace_seconds=pace_seconds,
                  normal_navigation=normal_navigation, engine=engine,
                  isolated_events=isolated_events,
                  observations=[], browser_sessions=[], selenium_version=selenium.__version__,
                  started_at=datetime.now(timezone.utc).isoformat())
    report_path = directory / 'report.json'
    _write(report_path, report)
    factory = factory or VividNFLBrowser

    def observe(browser, event, phase):
        # Optional lower request cadence is a diagnostic control, not a retry.
        # The shared report keeps this delay across browser-session boundaries.
        if report['observations'] and pace_seconds:
            time.sleep(pace_seconds)
        entry = dict(sport=event['sport'], source_id=event['production_id'], phase=phase,
                     started_at=datetime.now(timezone.utc).isoformat())
        try:
            raw, event_at = browser.capture(event['url'])
            validate_inventory(raw, event['production_id'])
            advertised = raw['global'][0].get('listingCount')
            if isinstance(advertised, bool) or int(advertised) != len(raw['tickets']):
                raise ValueError('Returned inventory is incomplete')
            observed = datetime.now(timezone.utc)
            if event_at.tzinfo is None or not 0 < (event_at - observed).total_seconds() <= 30 * 24 * 3600:
                raise ValueError('Event time is outside the current capture window')
            if event.get('event_date') and event_at.astimezone(timezone.utc) != datetime.fromisoformat(event['event_date']):
                raise ValueError('Event time does not match explicit schedule identity')
            parser, build = (NFLSnapshotParser, nfl_snapshot_to_payload) if event['sport'] == 'nfl' else (
                NHLSnapshotParser, nhl_snapshot_to_payload)
            snapshot = parser.parse(raw)
            if snapshot.source_id != event['production_id']:
                raise ValueError('Parser returned a different production')
            payload = build(event['url'], event_at, observed, snapshot)
            index = len(report['observations'])
            filename = f"observation-{index:02d}-{event['sport']}-{event['production_id']}.json"
            _write(directory / filename, payload)
            entry.update(status='captured', inventory_listing_count=len(raw['tickets']),
                advertised_listing_count=int(advertised), section_count=len(snapshot.sections),
                title=snapshot.title, event_date=event_at.isoformat(), captured_at=observed.isoformat(),
                payload_file=filename)
        except Exception as exc:
            entry.update(status='failed', error_type=type(exc).__name__)
            category = getattr(exc, 'category', '')
            if isinstance(category, str) and re.fullmatch(r'[a-z0-9-]{1,80}', category):
                entry['category'] = category
            if type(getattr(exc, 'retryable', None)) is bool:
                entry['retryable'] = exc.retryable
        entry['diagnostics'] = safe_diagnostics(getattr(browser, 'capture_diagnostics', {}))
        entry['finished_at'] = datetime.now(timezone.utc).isoformat()
        report['observations'].append(entry)
        _write(report_path, report)

    # Distinct events in one profile, then a repeat after navigation, then a
    # fresh browser. This catches stale-body reuse and startup-only successes.
    phases = ([(f'isolated-event-{index}', [event]) for index, event in enumerate(events)] if isolated_events else
              [('shared-session', events + [events[0]]), ('restarted-session', [events[0]])])
    for phase, cohort in phases:
        browser = None
        session = dict(phase=phase, status='starting')
        report['browser_sessions'].append(session)
        try:
            browser = factory(headless=False, timeout=timeout)
            webkit = getattr(browser, '_webkit_session', None)
            if engine == 'webkit':
                if webkit is None:
                    raise ValueError('The WebKit canary did not start WebKit')
                capabilities = {'browserName': 'webkit'}
            else:
                capabilities = browser.driver.capabilities
                if str(capabilities.get('browserName', '')).casefold() != 'firefox':
                    raise ValueError('The Firefox canary did not start Firefox')
            if normal_navigation:
                performer_urls = {event['production_id']: routes[event['production_id']][0] for event in cohort}
                expected_dates = {event['production_id']: routes[event['production_id']][1] for event in cohort}
                if engine == 'webkit':
                    webkit.configure_normal_navigation(performer_urls, expected_dates)
                else:
                    from vivid_firefox import configure_normal_navigation
                    configure_normal_navigation(browser, performer_urls, expected_dates)
            session.update(status='started', browser_version=capabilities.get('browserVersion'),
                           driver_version=capabilities.get('moz:geckodriverVersion'))
            for event in cohort:
                observe(browser, event, phase)
                if report['observations'][-1].get('category') in ('provider-access-denied', 'provider-rate-limited', 'provider-authentication-required'):
                    report['stopped_after_access_denial'] = True
                    break
        except Exception as exc:
            session.update(status='failed', error_type=type(exc).__name__)
        finally:
            if browser is not None:
                try:
                    browser.close()
                    session['closed'] = True
                except Exception as exc:
                    session['closed'] = False
                    session['close_error_type'] = type(exc).__name__
            _write(report_path, report)
        if report.get('stopped_after_access_denial'):
            break
    expected = len(events) if isolated_events else len(events) + 2
    success = (len(report['observations']) == expected
        and all(row['status'] == 'captured' for row in report['observations'])
        and all(row.get('closed') for row in report['browser_sessions']))
    report.update(status='passed' if success else 'failed',
                  finished_at=datetime.now(timezone.utc).isoformat())
    _write(report_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', required=True, help='JSON events with sport/url and optional paired home_team/event_date')
    parser.add_argument('--directory', type=Path, default=Path('firefox-canary'))
    parser.add_argument('--timeout', type=int, default=45)
    parser.add_argument('--engine', choices=('firefox', 'webkit'), default='firefox')
    parser.add_argument('--pace-seconds', type=int, default=0,
                        help='Idle seconds between observations, including browser restarts (0..90; default 0).')
    parser.add_argument('--normal-navigation', action='store_true',
                        help='Use observed performer links from paired schedule fields or the fixed known routes; default direct.')
    parser.add_argument('--isolated-events', action='store_true',
                        help='Start and close one fresh browser per distinct event, without repeats.')
    args = parser.parse_args()
    if not 20 <= args.timeout <= 60:
        parser.error('timeout must be between20 and60 seconds')
    if not 0 <= args.pace_seconds <= 90:
        parser.error('pace-seconds must be between 0 and 90')
    result = run_canary(events_from_json(args.events), args.directory, timeout=args.timeout,
                        pace_seconds=args.pace_seconds, normal_navigation=args.normal_navigation, isolated_events=args.isolated_events, engine=args.engine)
    print('BROWSER_CAPTURE_CANARY ' + json.dumps(result, sort_keys=True))
    return int(result['status'] != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
