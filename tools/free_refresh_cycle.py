"""Run an independent capture cycle, without modifying production collectors.

MLB is evaluated each half-hour. NFL and NHL preserve their existing adaptive
per-game schedules and are evaluated once an hour. Publishing is separate.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import patch


def due(sport, moment, force=False):
    if sport not in ('mlb','nfl','nhl'):raise ValueError('Unsupported sport')
    return force or sport=='mlb' or moment.minute<30


def run(sport, directory, *, force=False):
    from tools.free_refresh_capture import capture
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    now=datetime.now(timezone.utc)
    if not due(sport,now,force):
        report={'status':'not-due','sport':sport,'evaluated_at':now.isoformat(),
                'reason':'Preserving hourly evaluation of adaptive per-game capture tiers.'}
        (directory/'health.json').write_text(json.dumps(report,indent=2))
        print('FREE_CAPTURE_CADENCE '+json.dumps(report),flush=True)
        return 0
    # The production NHL helper treats every GitHub cron as its baseball-only
    # recovery job. This independent scheduler is a different caller. Scope the
    # trigger override to this process; the adaptive schedule itself is intact.
    with patch('nhl_schedule_collector.nhl_should_skip_for_trigger',return_value=False):
        return capture(sport,directory)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sport',choices=('mlb','nfl','nhl'),required=True)
    parser.add_argument('--directory',required=True)
    parser.add_argument('--force',action='store_true')
    args=parser.parse_args()
    try:code=run(args.sport,args.directory,force=args.force)
    except Exception as exc:
        print('FREE_CAPTURE_CYCLE_FAILED '+type(exc).__name__,flush=True);code=1
    raise SystemExit(code)
