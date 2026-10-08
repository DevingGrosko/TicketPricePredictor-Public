"""Read-only, fixed NHL identity canary through the production capture path."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

TARGETS = (
    ('2026020264', '7299775', 'https://www.vividseats.com/calgary-flames-tickets-scotiabank-saddledome-11-6-2026/production/7299775'),
    ('2026020259', '7302223', 'https://www.vividseats.com/winnipeg-jets-tickets-canada-life-centre-11-5-2026/production/7302223'),
    ('2026020260', '7299771', 'https://www.vividseats.com/edmonton-oilers-tickets-rogers-place-11-5-2026/production/7299771'),
)
DALLAS_TARGETS = (
    ('2026020209', '7300563', 'https://www.vividseats.com/dallas-stars-tickets-american-airlines-center---tx-10-29-2026/production/7300563'),
    ('2026020147', '7300529', 'https://www.vividseats.com/dallas-stars-tickets-american-airlines-center---tx-10-20-2026/production/7300529'),
)
DENIALS = {'provider-access-denied', 'provider-rate-limited', 'provider-authentication-required'}


def write_public(path, value):
    encoded = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(encoded)
    temporary.replace(path)
    return hashlib.sha256(encoded).hexdigest()


def run(directory, *, fetcher=None, factory=None, now=None, timeout=45, cohort='canada'):
    if cohort not in ('canada', 'dallas'):
        raise ValueError('Only the two fixed reviewed NHL cohorts are available')
    targets = TARGETS if cohort == 'canada' else DALLAS_TARGETS
    import nhl_schedule_collector as production
    from nhl_collector import DiscoveredNHLGame, nhl_snapshot_to_payload
    from vivid_inventory import VividCaptureError
    from vivid_webkit import public_inventory
    directory = Path(directory)
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        raise ValueError('The diagnostic requires a fresh empty output directory')
    directory.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('The official schedule retrieval time must be aware')
    report = dict(status='running', cohort=cohort, database_calls=0, upload_calls=0,
        started_at=now.astimezone(timezone.utc).isoformat(), observations=[], browser_sessions=[])
    report_path = directory / 'report.json'
    write_public(report_path, report)
    try:
        # One invocation of the unchanged official fetch/parser. No feed/search
        # discovery or caller-supplied identity can become the trusted anchor.
        games, sources = (fetcher or production.fetch_schedule_games)(now)
        selected = {str(game.schedule_id): game for game in games if str(game.schedule_id) in {t[0] for t in targets}}
        if set(selected) != {t[0] for t in targets}:
            raise ValueError('Official source is missing a fixed target identity')
        report['official_schedule_sources'] = sources
        report['official_retrieved_at'] = now.astimezone(timezone.utc).isoformat()
    except Exception as exc:
        report.update(status='failed', error_type=type(exc).__name__)
        write_public(report_path, report)
        return report

    native_factory = factory or production.VividNFLBrowser
    for schedule_id, pid, url in targets:
        game = selected[schedule_id]
        entry = dict(schedule_id=schedule_id, source_id=pid, url=url,
            official_event_date=game.event_date.isoformat(), away_team=game.away_team,
            home_team=game.home_team, venue=game.venue, venue_timezone=game.venue_timezone)
        created, captures, categories = [], [], []

        def recording_factory(**options):
            browser = native_factory(**options)
            created.append(browser)
            session = dict(source_id=pid, closed=False)
            report['browser_sessions'].append(session)
            close = browser.close

            def recording_close():
                try:
                    close()
                    session['closed'] = True
                except Exception as exc:
                    session['close_error_type'] = type(exc).__name__
                    raise
            browser.close = recording_close
            if getattr(browser, '_webkit_session', None) is None:
                browser.close()
                raise ValueError('The NHL identity canary requires the stock WebKit engine')
            capture = browser.capture

            def recording_capture(*args, **kwargs):
                try:
                    raw, stamp = capture(*args, **kwargs)
                    captures.append((public_inventory(raw, pid), stamp))
                    return raw, stamp
                except VividCaptureError as exc:
                    categories.append(exc.category)
                    raise
            browser.capture = recording_capture
            return browser

        candidate = DiscoveredNHLGame(url, game.name, game.local_date)
        resolution = production.ScheduleResolution(game, (candidate,), 'fixed-observed-public-event')
        try:
            # Only diagnostic pass-through/closure recording is wrapped. The
            # production resolver, schedule configuration, recovery and parser
            # execute unchanged; no endpoint, token, or store call is present.
            with patch.object(production, 'VividNFLBrowser', recording_factory):
                captured_url, stamp, snapshot = production._capture_resolution(resolution, headless=False, timeout=timeout)
            if stamp != game.event_date or snapshot.source_id != pid or not captures:
                raise ValueError('Capture did not preserve the exact official identity')
            captured_at = datetime.now(timezone.utc)
            payload = nhl_snapshot_to_payload(captured_url, stamp, captured_at, snapshot,
                schedule=game.snapshot_metadata(snapshot.venue))
            filename = 'observation-nhl-' + pid + '.json'
            raw_filename = 'inventory-nhl-' + pid + '.json'
            payload_hash = write_public(directory / filename, payload)
            raw_hash = write_public(directory / raw_filename, captures[-1][0])
            entry.update(status='captured', captured_at=captured_at.isoformat(), event_date=stamp.isoformat(),
                section_count=len(snapshot.sections), inventory_listing_count=snapshot.inventory_listing_count,
                payload_file=filename, payload_sha256=payload_hash,
                inventory_file=raw_filename, inventory_sha256=raw_hash,
                diagnostics=snapshot.capture_diagnostics or {})
        except Exception as exc:
            entry.update(status='failed', error_type=type(exc).__name__)
            if categories:
                entry['category'] = categories[-1]
            if created:
                entry['diagnostics'] = dict(getattr(created[-1], 'capture_diagnostics', {}) or {})
        entry['finished_at'] = datetime.now(timezone.utc).isoformat()
        report['observations'].append(entry)
        write_public(report_path, report)
        if any(category in DENIALS for category in categories):
            report['stopped_after_access_denial'] = True
            break
    passed = (len(report['observations']) == len(targets)
        and all(row['status'] == 'captured' for row in report['observations'])
        and all(row['closed'] for row in report['browser_sessions']))
    report.update(status='passed' if passed else 'failed', finished_at=datetime.now(timezone.utc).isoformat())
    write_public(report_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--cohort', choices=('canada', 'dallas'), default='canada')
    args = parser.parse_args()
    report = run(args.directory, cohort=args.cohort)
    print('NHL_TIME_IDENTITY_CANARY ' + json.dumps(report, sort_keys=True), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
