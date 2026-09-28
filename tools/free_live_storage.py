"""Bounded cache cleanup for independent collectors and the Pages publisher.

Only this pipeline's namespaced, completed-run caches are removable. Existing
production collectors, account billing and database rows are never modified.
"""
from __future__ import annotations
import argparse
import os
import re
from urllib.error import HTTPError
from unittest.mock import patch

from tools import free_refresh_storage as legacy

CACHE = re.compile(r'^ticketsignal-free-v1-(raw|pending-mlb|pending-nfl|pending-nhl|state-mlb|state-nfl|state-nhl)-(\d+)-(\d+)$')
WORKFLOWS = {'.github/workflows/free-ticket-site.yml', '.github/workflows/free-ticket-collect.yml'}


def removable_cache_rows(rows, runs, current_run):
    with patch.object(legacy, 'CACHE', CACHE):
        stale = legacy.stale_caches(rows, keep=2)
    answer = []
    for item in stale:
        run_id = int(CACHE.fullmatch(item['key'])[2])
        run = runs(run_id)
        if run_id != current_run and run.get('status') == 'completed' and run.get('path') in WORKFLOWS:
            answer.append(item)
    return answer


def preflight(collector=False):
    # Publisher alone performs cleanup, avoiding concurrent collector pruners.
    # Capture-only runs require no Pages permissions and cannot delete artifacts.
    if not collector:
        legacy.preflight()
        current = int(os.environ['GITHUB_RUN_ID'])
        runs = {}
        def get_run(run_id):
            if run_id not in runs:
                runs[run_id] = legacy.api('/actions/runs/' + str(run_id))
            return runs[run_id]
        rows = legacy.list_rows('/actions/caches', 'actions_caches')
        for item in removable_cache_rows(rows, get_run, current):
            try:
                legacy.api('/actions/caches/' + str(int(item['id'])), 'DELETE')
            except HTTPError as exc:
                if exc.code != 404:
                    raise
    usage = legacy.api('/actions/cache/usage')
    if int(usage.get('active_caches_size_in_bytes', 0)) > 8 * 1024**3:
        raise RuntimeError('Cache safety allowance reached; no paid expansion')
    print('FREE_LIVE_STORAGE_PREFLIGHT passed', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['preflight'])
    parser.add_argument('--collector', action='store_true')
    args = parser.parse_args()
    try:
        preflight(args.collector)
    except Exception as exc:
        print('FREE_LIVE_STORAGE_STOP ' + type(exc).__name__, flush=True)
        raise SystemExit(1)
