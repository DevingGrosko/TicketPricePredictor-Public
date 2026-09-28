"""A publisher tick can recover a missed collector tick. No paid scheduler.

This is redundancy, not a guarantee against a GitHub-wide scheduling delay.
Only the fixed free collection workflow can be dispatched; production is read
neither for data nor for its credentials. Active/queued runs are never killed.
"""
from datetime import datetime, timezone
import json
import os
from urllib.request import Request, urlopen

REPO = 'DevingGrosko/TicketPricePredictor-Public'
TARGET = '.github/workflows/free-ticket-collect.yml'
API = 'https://api.github.com/repos/' + REPO


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
    run()
