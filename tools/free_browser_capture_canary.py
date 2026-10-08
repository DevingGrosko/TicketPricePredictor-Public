"""Read-only Firefox canary through the actual free capture wrappers.

This enters the same HTTP-diagnostic/recovery contexts as the free CLI, then
uses the shared browser canary. It never enters hardened collection, opens a
writer, loads delivery credentials or changes a scheduler.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def run_free_canary(events, directory, *, timeout=45, runner=None):
    from tools.free_live_http_diagnostics import http_diagnostics
    from tools.free_live_provider_recovery import provider_recovery
    from tools.free_live_collect import write_json
    if runner is None:
        from tools.browser_capture_canary import run_canary
        runner = run_canary
    # Order matters: provider_recovery must install the already wrapped capture
    # method, exactly as tools.free_live_http_diagnostics' production entry does.
    with http_diagnostics(), provider_recovery():
        report = runner(events, directory, timeout=timeout)
    report['free_integration'] = dict(http_diagnostics=True, provider_recovery=True,
        capture_method_wrappers=True, delivery_enabled=False, scheduler_changed=False)
    write_json(Path(directory) / 'report.json', report)
    return report


def main():
    from tools.browser_capture_canary import events_from_json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', required=True)
    parser.add_argument('--directory', type=Path, default=Path('free-firefox-canary'))
    parser.add_argument('--timeout', type=int, default=45)
    args = parser.parse_args()
    if os.environ.get('TICKETSIGNAL_BROWSER_ENGINE') != 'firefox':
        parser.error('The read-only free canary requires explicit Firefox opt-in')
    if not 20 <= args.timeout <= 60:
        parser.error('timeout must be between 20 and 60 seconds')
    report = run_free_canary(events_from_json(args.events), args.directory, timeout=args.timeout)
    print('FREE_BROWSER_CAPTURE_CANARY ' + json.dumps(report, sort_keys=True))
    return int(report['status'] != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
