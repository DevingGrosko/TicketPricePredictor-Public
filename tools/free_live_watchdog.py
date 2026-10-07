"""A publisher tick can recover a missed collector tick. No paid scheduler.

This is redundancy, not a guarantee against a GitHub-wide scheduling delay.
Only the fixed free collection workflow can be dispatched; production is read
neither for data nor for its credentials. Active/queued runs are never killed.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from urllib.request import Request, urlopen

REPO = 'DevingGrosko/TicketPricePredictor-Public'
TARGET = '.github/workflows/free-ticket-collect.yml'
API = 'https://api.github.com/repos/' + REPO


def capture_jobs(jobs):
    """Recognize collection jobs; timer-only completions are not captures."""
    return [job for job in jobs
            if job.get('name') == 'capture' or job.get('name', '').startswith('capture (')]


def collection_timer_decision(rows, now, current_run, jobs_for):
    """The GitHub fallback fills a missed slot, without repeating its dispatcher."""
    from Flask_App.collection_cadence import half_hour_capture_slot
    slot = half_hour_capture_slot(now)
    own = [row for row in rows if row.get('path') == TARGET
           and row.get('head_branch') == 'main' and row.get('id') != current_run]
    if any(row.get('status') != 'completed' for row in own):
        return False, 'Another collection is active or queued; its recovery is preserved'
    for row in own:
        started = row.get('run_started_at') or row.get('created_at')
        if not started:
            continue
        started = datetime.fromisoformat(started.replace('Z', '+00:00'))
        if not slot <= started <= now:
            continue
        jobs = capture_jobs(jobs_for(row['id']))
        if any(job.get('status') == 'completed'
               and job.get('conclusion') in ('success', 'failure') for job in jobs):
            return False, 'This half-hour already attempted collection; failures remain reported'
    return True, 'No other collection attempted this half-hour'


def publication_needed(jobs):
    """Skip only the completion of a collector whose capture jobs were skipped."""
    captures = capture_jobs(jobs)
    timer_succeeded = any(job.get('name') == 'timer' and job.get('status') == 'completed'
                          and job.get('conclusion') == 'success' for job in jobs)
    if timer_succeeded and captures and all(job.get('status') == 'completed'
                                           and job.get('conclusion') == 'skipped' for job in captures):
        return False, 'Duplicate collection timer skipped capture; no new publication requested'
    return True, 'Collection attempted or its outcome is uncertain; publish partial captures'


def timer_gate(kind):
    """Emit workflow decisions without changing collectors, snapshots or alerts."""
    from tools.free_refresh_storage import api
    if os.environ.get('GITHUB_REPOSITORY') != REPO or os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise RuntimeError('Timer gates are restricted to the public main workflows')
    event = os.environ['GITHUB_EVENT_NAME']
    decision, reason = True, 'External, manual and push triggers retain their collection/publication'
    if kind == 'collection' and event == 'schedule':
        rows = api('/actions/workflows/free-ticket-collect.yml/runs?branch=main&per_page=10')['workflow_runs']
        decision, reason = collection_timer_decision(rows, datetime.now(timezone.utc),
            int(os.environ['GITHUB_RUN_ID']), lambda run_id: api('/actions/runs/' + str(run_id) + '/jobs?per_page=100')['jobs'])
    elif kind == 'publication' and event == 'workflow_run':
        run_id = int(os.environ['COLLECTION_RUN_ID'])
        decision, reason = publication_needed(api('/actions/runs/' + str(run_id) + '/jobs?per_page=100')['jobs'])
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('run=' + str(decision).lower() + '\n')
    print('FREE_TIMER_GATE ' + json.dumps({'kind': kind, 'run': decision, 'reason': reason}), flush=True)


def should_dispatch(rows, now):
    own = [r for r in rows if r.get('path') == TARGET and r.get('head_branch') == 'main']
    if any(r.get('status') != 'completed' for r in own):
        return False, 'Collection is active or queued'
    starts = [datetime.fromisoformat(r['created_at'].replace('Z', '+00:00')) for r in own]
    age = (now-max(starts)).total_seconds() if starts else None
    return age is None or age >= 30*60, 'No collector started in the last 30 minutes' if age is None or age >= 30*60 else 'Recent collection exists'


def run():
    if os.environ.get('GITHUB_REPOSITORY') != REPO or os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise RuntimeError('Watchdog is restricted to the public main workflow')
    token = os.environ['GH_TOKEN']
    headers = {'Authorization': 'Bearer '+token, 'Accept': 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28'}
    endpoint = '/actions/workflows/free-ticket-collect.yml'
    request = Request(API+endpoint+'/runs?branch=main&per_page=10', headers=headers)
    with urlopen(request, timeout=20) as response:
        value = json.load(response)
    decision, reason = should_dispatch(value['workflow_runs'], datetime.now(timezone.utc))
    if decision:
        request = Request(API+endpoint+'/dispatches', method='POST',
                          data=b'{"ref":"main"}', headers={**headers, 'Content-Type':'application/json'})
        with urlopen(request, timeout=20) as response:
            if response.status != 204:
                raise RuntimeError('Collector dispatch was not acknowledged')
    print('FREE_TIMER_WATCHDOG '+json.dumps({'dispatched':decision, 'reason':reason}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--collection-gate', action='store_true')
    group.add_argument('--publication-gate', action='store_true')
    args = parser.parse_args()
    if args.collection_gate or args.publication_gate:
        timer_gate('collection' if args.collection_gate else 'publication')
    else:
        run()
