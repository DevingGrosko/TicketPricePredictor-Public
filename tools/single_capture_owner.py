"""Draft single-owner half-hour gates and backup dispatch; no provider or store access."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO = 'DevingGrosko/TicketPricePredictor-Public'
OWNER = '.github/workflows/collect-ticket-prices.yml'
API = 'https://api.github.com/repos/' + REPO
ACTIVE = ('queued', 'in_progress', 'waiting', 'pending', 'requested')
CAPTURE_STEPS = {
    sport: {f'Collect due {sport.upper()} games across the adaptive 30-day window',
            f'Attempt shared {sport.upper()} capture for the half-hour'}
    for sport in ('nfl', 'nhl')
}
CAPTURE_STEPS['nhl'].add('Collect due NHL games across the adaptive seven-day window')
DELIVERY_STEPS = {'Deliver original observations without contacting Vivid'} | {
    name for sport in ('NFL', 'NHL') for name in
    (f'Deliver saved {sport} observations to both stores',
     f'Pilot one {sport} capture owner or replay the verified saved observations')}


def utc_time(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def capture_slot(now):
    if now.tzinfo is None:
        raise ValueError('Capture clocks require an aware timestamp')
    now = now.astimezone(timezone.utc)
    return now.replace(minute=30 if now.minute >= 30 else 0, second=0, microsecond=0)


def slot_decision(rows, jobs_for, now, *, current_run=None, sport=None, manual_repair=False):
    """A failed finished capture consumes its slot; delivery-only/skipped jobs do not."""
    if sport not in (None, 'nfl', 'nhl') or type(manual_repair) is not bool:
        raise ValueError('Invalid owner gate options')
    slot = capture_slot(now)
    evidence = {'slot':slot.isoformat(), 'attempts':[]}
    if manual_repair:
        return True, 'explicit-manual-repair', evidence
    own = [row for row in rows if row.get('path') == OWNER and row.get('head_branch') == 'main'
           and row.get('head_repository', {}).get('full_name', REPO) == REPO]
    current = next((row for row in own if row['id'] == current_run), None)
    current_created = utc_time(current.get('created_at')) if current else None
    job_cache = {}
    def jobs(row):
        if row['id'] not in job_cache:
            job_cache[row['id']] = jobs_for(row['id'])
        return job_cache[row['id']]
    for row in own:
        if row['id'] == current_run or row.get('status') == 'completed':
            continue
        created = utc_time(row.get('created_at'))
        # The current active owner must not yield to a newer queued follower.
        newer = (current_run is not None and current_created is not None and created is not None
                 and (created, row['id']) > (current_created, current_run))
        if not newer:
            relevant = [job for job in jobs(row) if job.get('name') == 'collect-' + sport] if sport else []
            if sport and relevant and all(job.get('status') == 'completed' for job in relevant):
                continue
            evidence['active_owner_run'] = row['id']
            return False, 'owner-active-or-queued', evidence
    sports = (sport,) if sport else ('nfl', 'nhl')
    for row in own:
        if row['id'] == current_run:
            continue
        finished = utc_time(row.get('updated_at'))
        if row.get('status') == 'completed' and finished is not None and finished < slot:
            continue
        for job in jobs(row):
            if job.get('name') not in {'collect-' + value for value in sports}:
                continue
            league = job['name'].removeprefix('collect-')
            for step in job.get('steps') or []:
                if (step.get('name') not in CAPTURE_STEPS[league] or step.get('status') != 'completed'
                        or step.get('conclusion') == 'skipped'):
                    continue
                started = utc_time(step.get('started_at'))
                if started is None:
                    if slot <= (utc_time(row.get('run_started_at')) or utc_time(row.get('created_at')) or slot) <= now:
                        return False, 'capture-attempt-time-unavailable', evidence
                    continue
                if slot <= started <= now:
                    evidence['attempts'].append({'run_id':row['id'], 'sport':league,
                        'started_at':started.isoformat(), 'conclusion':step.get('conclusion')})
    attempted_sports = {row['sport'] for row in evidence['attempts']}
    if set(sports) <= attempted_sports:
        return False, 'slot-already-attempted-including-failure', evidence
    return True, 'no-owner-attempt-in-this-slot', evidence


def api(path, *, payload=None):
    if not path.startswith('/') or path.startswith('//') or '..' in path:
        raise ValueError('Invalid owner API path')
    token = os.environ['GH_TOKEN']
    headers = {'Authorization':'Bearer ' + token, 'Accept':'application/vnd.github+json',
               'X-GitHub-Api-Version':'2022-11-28'}
    data = None if payload is None else json.dumps(payload).encode()
    if data is not None:
        headers['Content-Type'] = 'application/json'
    with urlopen(Request(API + path, data=data, headers=headers), timeout=20) as response:
        if data is not None:
            if response.status != 204:
                raise RuntimeError('Owner dispatch was not acknowledged')
            return None
        return json.load(response)


def owner_runs(read=None):
    read = read or api
    endpoint = '/actions/workflows/collect-ticket-prices.yml/runs?'
    recent = read(endpoint + urlencode({'branch':'main', 'per_page':100}))['workflow_runs']
    rows = {row['id']:row for row in recent}
    # Include old queued/active owners even when newer completed runs displaced them.
    for status in ACTIVE:
        value = read(endpoint + urlencode({'branch':'main', 'status':status, 'per_page':100}))
        if value.get('total_count', 0) > 100:
            raise RuntimeError('Active owner inventory exceeds bounded safety limit')
        rows.update({row['id']:row for row in value['workflow_runs']})
    return list(rows.values())


def publication_needed(owner, jobs):
    """Skip only a proven successful duplicate with no capture or delivery attempt."""
    if owner.get('status') != 'completed' or owner.get('conclusion') != 'success':
        return True, 'owner-failed-or-outcome-unknown'
    for sport in ('nfl', 'nhl'):
        captures = [job for job in jobs if job.get('name') == 'collect-' + sport]
        if not captures:
            return True, 'capture-jobs-unknown'
        for job in captures:
            if job.get('status') != 'completed':
                return True, 'capture-outcome-unknown'
            steps = [step for step in job.get('steps') or [] if step.get('name') in CAPTURE_STEPS[sport]]
            entirely_skipped = job.get('conclusion') == 'skipped' and not steps
            if not entirely_skipped and (not steps or any(step.get('status') != 'completed'
                                                         or step.get('conclusion') != 'skipped' for step in steps)):
                return True, 'capture-attempted-or-unknown'
    for job in jobs:
        steps = [step for step in job.get('steps') or [] if step.get('name') in DELIVERY_STEPS]
        if any(step.get('status') != 'completed' or step.get('conclusion') != 'skipped' for step in steps):
            return True, 'replay-or-mirror-attempted-or-unknown'
        mirror = any(job.get('name', '').startswith('mirror-' + sport + '-staging') for sport in ('nfl', 'nhl'))
        if mirror and (job.get('status') != 'completed' or job.get('conclusion') not in ('success', 'skipped')
                       or not steps and job.get('conclusion') != 'skipped'):
            return True, 'mirror-outcome-unknown'
    return False, 'both-captures-skipped-and-no-delivery-attempt'


def publication_handoff_needed(jobs):
    """An owner end-job can publish after completed attempts while the run is active."""
    attempted = []
    for sport in ('nfl', 'nhl'):
        captures = [job for job in jobs if job.get('name') == 'collect-'+sport]
        if not captures or any(job.get('status') != 'completed' for job in captures):
            raise RuntimeError('Publication handoff requires completed capture jobs')
        for job in captures:
            steps = [step for step in job.get('steps') or [] if step.get('name') in CAPTURE_STEPS[sport] | DELIVERY_STEPS]
            if not steps and job.get('conclusion') != 'skipped':
                raise RuntimeError('Publication handoff capture outcome is unavailable')
            for step in steps:
                if step.get('status') != 'completed':
                    raise RuntimeError('Publication handoff capture outcome is still active')
                if step.get('conclusion') not in ('success','failure','cancelled','timed_out','neutral','skipped'):
                    raise RuntimeError('Publication handoff capture conclusion is unavailable')
                if step.get('conclusion') != 'skipped':
                    attempted.append(sport)
    names = {f'mirror-{sport}-staging'+suffix for sport in ('nfl','nhl') for suffix in ('', ' / mirror')}
    mirrors = [job for job in jobs if job.get('name') in names]
    if any(job.get('status') != 'completed' for job in mirrors):
        raise RuntimeError('Publication handoff must wait for mirror jobs')
    for sport in set(attempted):
        if not any(job.get('name') in {f'mirror-{sport}-staging', f'mirror-{sport}-staging / mirror'} for job in mirrors):
            raise RuntimeError('Publication handoff mirror outcome is unavailable')
    delivered = any(step.get('name') in DELIVERY_STEPS and step.get('status') == 'completed'
                    and step.get('conclusion') != 'skipped'
                    for job in mirrors for step in job.get('steps') or [])
    if attempted or delivered:
        return True, 'completed-capture-or-delivery-attempt'
    return False, 'both-captures-skipped-and-no-delivery-attempt'


def check_scope():
    if os.environ.get('GITHUB_REPOSITORY') != REPO or os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise RuntimeError('Owner gates are restricted to the public main workflows')


def run(kind, *, sport=None, manual_repair=False, owner_run_id=None):
    check_scope()
    def jobs(run_id):
        value = api('/actions/runs/' + str(run_id) + '/jobs?per_page=100')
        if value.get('total_count', 0) > 100:
            raise RuntimeError('Owner job inventory exceeds bounded safety limit')
        return value['jobs']
    if kind == 'publication-handoff':
        current = int(os.environ['GITHUB_RUN_ID'])
        if (os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
                or os.environ.get('DISPATCH_SOURCE') != 'github_free_backup'
                or os.environ.get('GITHUB_ACTOR') != 'github-actions[bot]'
                or os.environ.get('GITHUB_TRIGGERING_ACTOR') != 'github-actions[bot]'
                or owner_run_id not in (None, current) or sport is not None or manual_repair):
            raise RuntimeError('Publication handoff is restricted to its own backup-dispatched owner')
        owner = api('/actions/runs/'+str(current))
        if (owner.get('id') != current or owner.get('path') != OWNER or owner.get('head_branch') != 'main'
                or (owner.get('head_repository') or {}).get('full_name') != REPO
                or (owner.get('repository') or {}).get('full_name') != REPO
                or (owner.get('repository') or {}).get('private') is not False
                or (owner.get('actor') or {}).get('login') != 'github-actions[bot]'
                or (owner.get('triggering_actor') or {}).get('login') != 'github-actions[bot]'
                or owner.get('status') not in ('in_progress', 'completed')):
            raise RuntimeError('Publication handoff must belong to the canonical public main owner')
        decision, reason = publication_handoff_needed(jobs(current))
        evidence = {'owner_run_id':current}
    elif kind == 'publication-gate':
        if type(owner_run_id) is not int or owner_run_id <= 0:
            raise ValueError('Publication gate requires an explicit owner run ID')
        evidence = {'owner_run_id':owner_run_id}
        try:
            owner = api('/actions/runs/' + str(owner_run_id))
            if (owner.get('path') != OWNER or owner.get('head_branch') != 'main'
                    or owner.get('head_repository', {}).get('full_name', REPO) != REPO
                    or owner.get('repository', {}).get('full_name', REPO) != REPO):
                raise RuntimeError('Publication completion must belong to the canonical main owner')
            decision, reason = publication_needed(owner, jobs(owner_run_id))
        except (HTTPError, URLError, TimeoutError, ConnectionError) as exc:
            if isinstance(exc, HTTPError) and not 500 <= exc.code <= 599:
                raise
            # The lookup only avoids duplicate builds. A temporary read failure
            # must not hide already committed valid observations from the site.
            decision, reason = True, 'publication-lookup-temporarily-unavailable'
            evidence['lookup_error_type'] = type(exc).__name__
            if isinstance(exc, HTTPError):
                evidence['lookup_status'] = exc.code
    else:
        rows = owner_runs()
        current = int(os.environ['GITHUB_RUN_ID']) if kind == 'slot-gate' else None
        if current is not None and not any(row['id'] == current for row in rows):
            rows.append(api('/actions/runs/' + str(current)))
        decision, reason, evidence = slot_decision(rows, jobs, datetime.now(timezone.utc),
                                                  current_run=current, sport=sport, manual_repair=manual_repair)
    report = dict(evidence, run=decision, reason=reason, kind=kind, dispatched=False)
    if kind == 'dispatch-backup' and decision:
        api('/actions/workflows/collect-ticket-prices.yml/dispatches',
            payload={'ref':'main', 'inputs':{'dispatch_source':'github_free_backup', 'shared_capture':True}})
        report['dispatched'] = True
    if kind == 'publication-handoff' and decision:
        api('/actions/workflows/free-ticket-site.yml/dispatches',
            payload={'ref':'main', 'inputs':{'owner_run_id':str(evidence['owner_run_id'])}})
        report['dispatched'] = True
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('run=' + str(decision).lower() + '\n')
    print('SINGLE_CAPTURE_OWNER ' + json.dumps(report), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('dispatch-backup','slot-gate','publication-gate','publication-handoff'))
    parser.add_argument('--sport', choices=('nfl','nhl'))
    parser.add_argument('--manual-repair', action='store_true')
    parser.add_argument('--owner-run-id', type=int)
    args = parser.parse_args()
    if args.operation == 'dispatch-backup' and (args.sport or args.manual_repair):
        parser.error('The automatic backup cannot override the owner gate')
    if args.operation == 'publication-gate' and (not args.owner_run_id or args.sport or args.manual_repair):
        parser.error('publication-gate requires only an explicit positive --owner-run-id')
    if args.owner_run_id is not None and args.operation != 'publication-gate':
        parser.error('--owner-run-id is only for publication-gate')
    run(args.operation, sport=args.sport, manual_repair=args.manual_repair, owner_run_id=args.owner_run_id)
