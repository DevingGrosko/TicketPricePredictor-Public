"""Read-only Firefox canary through the actual free capture wrappers.

This enters the same HTTP-diagnostic/recovery contexts as the free CLI, then
uses the shared browser canary with the production current-inventory recovery
guard and explicit schedule times. It never enters hardened collection, opens a
writer, loads delivery credentials or changes a scheduler.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


def event_dates_from_json(text, events):
    """Require explicit UTC schedule times for exactly the requested productions."""
    values = json.loads(text)
    identities = {event['production_id'] for event in events}
    if not isinstance(values, dict) or set(values) != identities:
        raise ValueError('Event dates must map every canary production ID exactly once')
    dates = {}
    for pid, value in values.items():
        if not isinstance(value, str):
            raise ValueError('Each event date must be an explicit UTC datetime')
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError('Each event date must include an explicit UTC offset')
        dates[pid] = stamp.astimezone(timezone.utc)
    return dates


class RecoveryCanaryBrowser:
    """Keep the concrete, patched browser inside the production recovery guard."""
    def __init__(self, inner, events, event_dates, evidence):
        self.inner, self.events = inner, events
        self.event_dates, self.evidence = event_dates, evidence

    @property
    def driver(self):
        return self.inner.driver

    @property
    def capture_diagnostics(self):
        return self.inner.capture_diagnostics

    def close(self):
        return self.inner.close()

    def capture(self, url):
        from tools.browser_capture_canary import safe_diagnostics
        from vivid_inventory import CurrentInventoryRecovery, CURRENT_INVENTORY_COOLDOWN_SECONDS
        pid = urlsplit(url).path.rstrip('/').split('/')[-1]
        event = self.events[pid]
        guard = CurrentInventoryRecovery(self.event_dates[pid], 7 * 24 if event['sport'] == 'nfl' else 72)
        row = {'production_id': pid, 'event_date': self.event_dates[pid].isoformat(),
               'eligible_at_start': guard._eligible(), 'status': 'failed'}
        try:
            result = guard.capture(self.inner, url)
            row['status'] = 'captured'
            return result
        finally:
            row.update(reload_used=guard.used, recovered=guard.recovered,
                       cooldown_seconds=CURRENT_INVENTORY_COOLDOWN_SECONDS if guard.used else 0)
            row['attempts'] = []
            for attempt in guard.attempts:
                clean = {'attempt': attempt['attempt'], 'status': attempt['status'],
                         'diagnostics': safe_diagnostics(attempt.get('diagnostics', {}))}
                category = attempt.get('category')
                if isinstance(category, str):
                    if re.fullmatch(r'[a-z0-9-]{1,80}', category):
                        clean['category'] = category
                row['attempts'].append(clean)
            self.evidence.append(row)


def run_free_canary(events, directory, *, event_dates, timeout=45, factory=None, runner=None):
    from tools.free_live_http_diagnostics import http_diagnostics
    from tools.free_live_provider_recovery import provider_recovery
    from tools.free_live_collect import write_json
    if runner is None:
        from tools.browser_capture_canary import run_canary
        runner = run_canary
    if set(event_dates) != {event['production_id'] for event in events}:
        raise ValueError('Every canary production needs an explicit event date')
    for stamp in event_dates.values():
        if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError('Canary event dates must be explicit UTC datetimes')
    if factory is None:
        from nfl_collector import VividNFLBrowser
        factory = VividNFLBrowser
    evidence = []
    by_pid = {event['production_id']: event for event in events}
    def recovery_factory(**kwargs):
        return RecoveryCanaryBrowser(factory(**kwargs), by_pid, event_dates, evidence)
    # Order matters: provider_recovery must install the already wrapped capture
    # method, exactly as tools.free_live_http_diagnostics' production entry does.
    with http_diagnostics(), provider_recovery():
        report = runner(events, directory, timeout=timeout, factory=recovery_factory)
    report['free_integration'] = dict(http_diagnostics=True, provider_recovery=True,
        capture_method_wrappers=True, current_inventory_recovery=True,
        delivery_enabled=False, scheduler_changed=False)
    report['recovery_observations'] = evidence
    write_json(Path(directory) / 'report.json', report)
    return report


def main():
    from tools.browser_capture_canary import events_from_json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', required=True)
    parser.add_argument('--event-dates', required=True,
                        help='JSON mapping of production IDs to explicit UTC schedule datetimes')
    parser.add_argument('--directory', type=Path, default=Path('free-firefox-canary'))
    parser.add_argument('--timeout', type=int, default=45)
    args = parser.parse_args()
    if os.environ.get('TICKETSIGNAL_BROWSER_ENGINE') != 'firefox':
        parser.error('The read-only free canary requires explicit Firefox opt-in')
    if not 20 <= args.timeout <= 60:
        parser.error('timeout must be between 20 and 60 seconds')
    events = events_from_json(args.events)
    event_dates = event_dates_from_json(args.event_dates, events)
    report = run_free_canary(events, args.directory, event_dates=event_dates, timeout=args.timeout)
    print('FREE_BROWSER_CAPTURE_CANARY ' + json.dumps(report, sort_keys=True))
    return int(report['status'] != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
