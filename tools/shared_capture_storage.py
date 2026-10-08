"""Bounded shared-queue retention; original free state/history stays untouched."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError

from tools.single_capture_owner import OWNER, REPO, api, check_scope, utc_time

KEY = re.compile(r'^shared-capture-v1-(nfl|nhl)-(capture|tidb)-(\d+)-(\d+)$')
MIGRATION_KEY = re.compile(r'^ticketsignal-free-v1-state-(nfl|nhl)-shared-(\d+)-(\d+)$')
MIRROR = '.github/workflows/shared-snapshot-mirror.yml'
QUEUE_LIMIT = 20 * 1024**2
ARTIFACT = re.compile(r'^shared-observations-(nfl|nhl)-(\d+)$')
TRANSPORT_LIMIT = 128 * 1024**2
TOTAL_ARTIFACT_LIMIT = 450 * 1024**2
PUBLISHER_RESERVE = 128 * 1024**2
ARCHIVE_MARGIN = 1024**2
PEER_UPLOAD_RESERVE = QUEUE_LIMIT + ARCHIVE_MARGIN
PROVENANCE_RUN_LIMIT = 40


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


def _success(job, name):
    return any(step.get('name') == name and step.get('status') == 'completed'
               and step.get('conclusion') == 'success' for step in job.get('steps') or [])


def removable_transport_artifacts(rows, caches, runs_for, jobs_for, *, current_run):
    """Delete only checkpoint duplicates; unknown/active/sole artifacts are retained."""
    ordered, seen = [], set()
    for row in rows:
        match = ARTIFACT.fullmatch(row.get('name', ''))
        created = utc_time(row.get('created_at'))
        workflow = row.get('workflow_run') or {}
        if (not match or row.get('expired') or created is None
                or type(row.get('id')) is not int or row['id'] <= 0
                or type(row.get('size_in_bytes')) is not int or row['size_in_bytes'] < 0
                or int(match[2]) == current_run or workflow.get('id') != int(match[2])
                or workflow.get('head_branch') != 'main'):
            continue
        if row['id'] in seen:
            continue
        seen.add(row['id'])
        ordered.append((created, row['id'], match[1], int(match[2]), row))
    known, groups, job_lists = {}, {}, {}
    for created, _artifact_id, sport, run_id, row in sorted(ordered, key=lambda item:item[:2], reverse=True):
        if run_id not in known:
            if len(known) >= PROVENANCE_RUN_LIMIT:
                continue  # Bounded reads never justify deleting unexamined artifacts.
            known[run_id] = runs_for(run_id)
        run = known[run_id]
        started = utc_time(run.get('run_started_at'))
        attempt = run.get('run_attempt')
        if (run.get('path') != OWNER or run.get('status') != 'completed'
                or run.get('head_branch') != 'main'
                or (run.get('repository') or {}).get('full_name') != REPO
                or (run.get('head_repository') or {}).get('full_name') != REPO
                or type(attempt) is not int or attempt <= 0 or started is None or created < started
                or not run.get('head_sha') or row['workflow_run'].get('head_sha') != run['head_sha']):
            continue
        groups.setdefault(sport, []).append((row, run_id, attempt, started))
    remove = []
    for sport, group in groups.items():
        for row, run_id, attempt, started in group[2:]:
            expected_keys = {f'shared-capture-v1-{sport}-{role}-{run_id}-{attempt}' for role in ('capture', 'tidb')}
            durable_keys = {cache['key'] for cache in caches
                if cache.get('key') in expected_keys and cache.get('ref') == 'refs/heads/main'
                and isinstance(cache.get('version'), str) and cache['version']
                and (utc_time(cache.get('created_at')) or datetime.min.replace(tzinfo=timezone.utc)) >= started}
            if durable_keys != expected_keys:
                continue
            if (run_id, attempt) not in job_lists:
                job_lists[run_id, attempt] = jobs_for(run_id, attempt)
            jobs = job_lists[run_id, attempt]
            producers = [job for job in jobs if job.get('name') == 'collect-'+sport and job.get('status') == 'completed']
            consumers = [job for job in jobs if job.get('name') == f'mirror-{sport}-staging / mirror'
                         and job.get('status') == 'completed']
            if (any(_success(job, f'Checkpoint the {sport.upper()} shared queue even after a failed capture')
                    and _success(job, f'Prepare a public {sport.upper()} export including an explicit empty checkpoint') for job in producers)
                    and any(_success(job, 'Verify current export in the delivery checkpoint')
                            and _success(job, 'Preserve independent delivery acknowledgments and failures') for job in consumers)):
                remove.append(row)
    return remove


def _inventory(path, key):
    rows = []
    for page in range(1, 21):
        value = api(path + '?per_page=100&page=' + str(page))
        rows.extend(value[key])
        if len(value[key]) < 100:
            return rows
    raise RuntimeError('Storage inventory exceeds bounded pagination; no destructive cleanup')


def _delete(path):
    from urllib.request import Request, urlopen
    from tools.single_capture_owner import API
    headers = {'Authorization':'Bearer ' + os.environ['GH_TOKEN'], 'Accept':'application/vnd.github+json',
               'X-GitHub-Api-Version':'2022-11-28'}
    try:
        with urlopen(Request(API + path, method='DELETE', headers=headers), timeout=20):
            pass
    except HTTPError as exc:
        if exc.code != 404:
            raise


def prune_transport_artifacts(*, rows=None, caches=None):
    check_scope()
    rows = rows if rows is not None else _inventory('/actions/artifacts', 'artifacts')
    caches = caches if caches is not None else _inventory('/actions/caches', 'actions_caches')
    def jobs(run, attempt):
        value = api(f'/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100')
        if value.get('total_count', 0) > 100:
            raise RuntimeError('Owner checkpoint job inventory exceeds bound')
        return value['jobs']
    remove = removable_transport_artifacts(rows, caches, lambda run:api('/actions/runs/'+str(run)), jobs,
                                          current_run=int(os.environ['GITHUB_RUN_ID']))
    for row in remove:
        _delete('/actions/artifacts/'+str(row['id']))
    total = sum(bool(ARTIFACT.fullmatch(row.get('name', ''))) and not row.get('expired') for row in rows)
    report = {'removed_transport_artifacts': len(remove), 'retained_transport_artifacts': total-len(remove),
              'latest_completed_artifacts_retained_per_sport': 2, 'unconfirmed_artifacts_preserved': True}
    print('SHARED_TRANSPORT_STORAGE '+json.dumps(report), flush=True)
    return report


def artifact_upload_budget(rows, new_bytes):
    total = transport = 0
    if type(new_bytes) is not int or not 0 <= new_bytes <= PEER_UPLOAD_RESERVE:
        raise RuntimeError('Transport upload exceeds its bounded export allowance')
    for row in rows:
        if row.get('expired'):
            continue
        size = row.get('size_in_bytes')
        if type(size) is not int or size < 0:
            raise RuntimeError('Artifact usage is unknown; refuse new upload')
        total += size
        if ARTIFACT.fullmatch(row.get('name', '')):
            transport += size
    if (transport + new_bytes + PEER_UPLOAD_RESERVE > TRANSPORT_LIMIT
            or total + new_bytes + PEER_UPLOAD_RESERVE + PUBLISHER_RESERVE > TOTAL_ARTIFACT_LIMIT):
        raise RuntimeError('Transport artifact safety budget exhausted; preserve unconfirmed data and refuse upload')
    return dict(existing_transport_bytes=transport, existing_repository_bytes=total,
                estimated_new_bytes=new_bytes, peer_upload_reserve_bytes=PEER_UPLOAD_RESERVE,
                publisher_reserve_bytes=PUBLISHER_RESERVE, maximum_transport_bytes=TRANSPORT_LIMIT,
                maximum_repository_bytes=TOTAL_ARTIFACT_LIMIT)


def before_artifact_upload(sport, directory):
    check_scope()
    from tools.shared_capture import validate_export
    report = validate_export(sport, directory)
    evidence = artifact_upload_budget(_inventory('/actions/artifacts', 'artifacts'), report['bytes'] + ARCHIVE_MARGIN)
    print('SHARED_TRANSPORT_BUDGET '+json.dumps(evidence), flush=True)
    return evidence


def prune_shared_caches():
    check_scope()
    rows = _inventory('/actions/caches', 'actions_caches')
    # Artifact confirmation uses these still-present exact cache keys. A failed
    # artifact deletion aborts here before the evidence checkpoints are pruned.
    prune_transport_artifacts(caches=rows)
    remove = removable_generations(rows, lambda run:api('/actions/runs/' + str(run)),
                                   current_run=int(os.environ['GITHUB_RUN_ID']), current_ref=os.environ['GITHUB_REF'])
    for row in remove:
        _delete('/actions/caches/' + str(int(row['id'])))
    report = {'removed_shared_generations':len(remove), 'completed_generations_retained_per_group':2}
    print('SHARED_CAPTURE_STORAGE ' + json.dumps(report), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('queue-budget','prune-caches','before-artifact-upload'))
    parser.add_argument('--directory')
    parser.add_argument('--sport', choices=('nfl','nhl'))
    args = parser.parse_args()
    if args.operation == 'queue-budget':
        if not args.directory:
            parser.error('queue-budget requires an owned queue directory')
        print('SHARED_CAPTURE_QUEUE ' + json.dumps(queue_budget(args.directory)), flush=True)
    elif args.operation == 'before-artifact-upload':
        if not args.directory or not args.sport:
            parser.error('before-artifact-upload requires sport and export directory')
        before_artifact_upload(args.sport, args.directory)
    else:
        prune_shared_caches()
