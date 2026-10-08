"""Read-only multi-event Firefox capture canary; no database or upload calls."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit


def events_from_json(text):
    from collector import validated_vivid_url
    events = json.loads(text)
    if not isinstance(events, list) or not 2 <= len(events) <= 4:
        raise ValueError('Canary requires two to four explicit NFL/NHL events')
    result, identities = [], set()
    for event in events:
        if not isinstance(event, dict) or set(event) != {'sport', 'url'} or event['sport'] not in ('nfl', 'nhl'):
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
        result.append(dict(sport=event['sport'], url=url, production_id=pid))
    if {event['sport'] for event in result} != {'nfl', 'nhl'}:
        raise ValueError('Canary must include both NFL and NHL')
    return result


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
    if value.get('engine') in ('firefox', 'chrome'):
        result['engine'] = value['engine']
    if type(value.get('document_status')) is int:
        result['document_status'] = value['document_status']
    if value.get('acquisition_method') == 'original-response-bidi':
        result['acquisition_method'] = value['acquisition_method']
    runtime = value.get('runtime')
    if isinstance(runtime, dict):
        result['runtime'] = {key: runtime[key] for key in ('engine', 'selenium', 'browser_version', 'driver_version')
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


def run_canary(events, directory, *, timeout=45, pace_seconds=0, factory=None):
    if type(pace_seconds) is not int or not 0 <= pace_seconds <= 90:
        raise ValueError('pace_seconds must be an integer between 0 and 90')
    import selenium
    from nfl_collector import VividNFLBrowser, NFLSnapshotParser, nfl_snapshot_to_payload
    from nhl_collector import NHLSnapshotParser, nhl_snapshot_to_payload
    from vivid_inventory import validate_inventory
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    report = dict(status='running', database_calls=0, upload_calls=0, pace_seconds=pace_seconds,
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
    phases = [('shared-session', events + [events[0]]), ('restarted-session', [events[0]])]
    for phase, cohort in phases:
        browser = None
        session = dict(phase=phase, status='starting')
        report['browser_sessions'].append(session)
        try:
            browser = factory(headless=False, timeout=timeout)
            capabilities = browser.driver.capabilities
            if str(capabilities.get('browserName', '')).casefold() != 'firefox':
                raise ValueError('The Firefox canary did not start Firefox')
            session.update(status='started', browser_version=capabilities.get('browserVersion'),
                           driver_version=capabilities.get('moz:geckodriverVersion'))
            for event in cohort:
                observe(browser, event, phase)
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
    expected = len(events) + 2
    success = (len(report['observations']) == expected
        and all(row['status'] == 'captured' for row in report['observations'])
        and all(row.get('closed') for row in report['browser_sessions']))
    report.update(status='passed' if success else 'failed',
                  finished_at=datetime.now(timezone.utc).isoformat())
    _write(report_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', required=True, help='JSON list with sport and public event URL')
    parser.add_argument('--directory', type=Path, default=Path('firefox-canary'))
    parser.add_argument('--timeout', type=int, default=45)
    parser.add_argument('--pace-seconds', type=int, default=0,
                        help='Idle seconds between observations, including browser restarts (0..90; default 0).')
    args = parser.parse_args()
    if not 20 <= args.timeout <= 60:
        parser.error('timeout must be between20 and60 seconds')
    if not 0 <= args.pace_seconds <= 90:
        parser.error('pace-seconds must be between 0 and 90')
    result = run_canary(events_from_json(args.events), args.directory, timeout=args.timeout, pace_seconds=args.pace_seconds)
    print('BROWSER_CAPTURE_CANARY ' + json.dumps(result, sort_keys=True))
    return int(result['status'] != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
