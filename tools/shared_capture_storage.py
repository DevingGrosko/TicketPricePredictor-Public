"""Bounded shared-queue retention; original free state/history stays untouched."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError

from tools.single_capture_owner import OWNER, api, check_scope

KEY = re.compile(r'^shared-capture-v1-(nfl|nhl)-(capture|tidb)-(\d+)-(\d+)$')
MIGRATION_KEY = re.compile(r'^ticketsignal-free-v1-state-(nfl|nhl)-shared-(\d+)-(\d+)$')
MIRROR = '.github/workflows/shared-snapshot-mirror.yml'
QUEUE_LIMIT = 20 * 1024**2


def queue_budget(directory):
    """Measure actual restored files, never the compressed Actions cache size."""
    root = Path(directory)
    total = files = 0
    if root.is_symlink() or root.exists() and not root.is_dir():
        raise RuntimeError('Shared queue must be an owned directory')
    for path in root.rglob('*') if root.exists() else []:
        if path.is_symlink():
            raise RuntimeError('Shared queue symlinks are forbidden')
        if path.is_file():
            total += path.stat().st_size
            files += 1
            if total > QUEUE_LIMIT:
                raise RuntimeError('Actual shared queue exceeds20MiB; preserve evidence and refuse cache save')
    return {'queue_bytes':total, 'queue_files':files, 'maximum_bytes':QUEUE_LIMIT}


def removable_generations(rows, runs_for, *, current_run, current_ref='refs/heads/main', keep=2):
    """Keep two completed generations per sport/role/ref/path-version plus live owners."""
    if keep != 2:
        raise ValueError('Shared queues retain exactly two completed generations')
    groups, runs = {}, {}
    for row in rows:
        match = KEY.fullmatch(row.get('key', ''))
        migration = MIGRATION_KEY.fullmatch(row.get('key', ''))
        version = row.get('version')
        if not (match or migration) or row.get('ref') != current_ref or not isinstance(version, str) or not version:
            continue
        if migration:
            sport, run_text, attempt = migration.groups()
            role = 'legacy-delivery'
        else:
            sport, role, run_text, attempt = match.groups()
        run_id = int(run_text)
        if run_id == current_run:
            continue
        if run_id not in runs:
            runs[run_id] = runs_for(run_id)
        run = runs[run_id]
        paths = {OWNER, MIRROR} if role == 'tidb' else {OWNER}
        if run.get('path') not in paths or run.get('status') != 'completed':
            continue
        try:
            created = datetime.fromisoformat(row['created_at'].replace('Z','+00:00'))
            if created.tzinfo is None:
                continue
        except (KeyError, AttributeError, TypeError, ValueError):
            continue
        groups.setdefault((sport,role,current_ref,version), []).append((created,run_id,int(attempt),row))
    return [item[3] for group in groups.values()
            for item in sorted(group, key=lambda item:item[:3], reverse=True)[keep:]]


def prune_shared_caches():
    check_scope()
    rows = []
    for page in range(1, 21):
        value = api('/actions/caches?per_page=100&page=' + str(page))
        rows.extend(value['actions_caches'])
        if len(value['actions_caches']) < 100:
            break
    else:
        raise RuntimeError('Cache inventory exceeds bounded pagination; no pruning')
    remove = removable_generations(rows, lambda run:api('/actions/runs/' + str(run)),
                                   current_run=int(os.environ['GITHUB_RUN_ID']), current_ref=os.environ['GITHUB_REF'])
    for row in remove:
        # API helper uses GET/POST only; DELETE remains a fixed scoped operation.
        from urllib.request import Request, urlopen
        from tools.single_capture_owner import API
        headers = {'Authorization':'Bearer ' + os.environ['GH_TOKEN'], 'Accept':'application/vnd.github+json',
                   'X-GitHub-Api-Version':'2022-11-28'}
        try:
            with urlopen(Request(API + '/actions/caches/' + str(int(row['id'])), method='DELETE', headers=headers), timeout=20):
                pass
        except HTTPError as exc:
            if exc.code != 404:
                raise
    report = {'removed_shared_generations':len(remove), 'completed_generations_retained_per_group':2}
    print('SHARED_CAPTURE_STORAGE ' + json.dumps(report), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('queue-budget','prune-caches'))
    parser.add_argument('--directory')
    args = parser.parse_args()
    if args.operation == 'queue-budget':
        if not args.directory:
            parser.error('queue-budget requires an owned queue directory')
        print('SHARED_CAPTURE_QUEUE ' + json.dumps(queue_budget(args.directory)), flush=True)
    else:
        prune_shared_caches()
