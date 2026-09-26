"""Storage safety checks for the independent free publisher only.

Never changes billing, repository settings, production caches, or databases.
Only this publisher's namespaced completed-run artifacts/caches may be removed.
Account-wide zero-dollar spending caps remain the owner's final billing guard.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request
import zlib

REPO='DevingGrosko/TicketPricePredictor-Public'
API='https://api.github.com/repos/'+REPO
ARTIFACT='ticketsignal-free-pages'
CACHE=re.compile(r'^ticketsignal-free-v1-(raw|pending-mlb|pending-nfl|pending-nhl)-(\d+)-(\d+)$')
MB=1024**2


def stale_caches(rows,keep=2):
    if keep<1:raise ValueError('Keep at least one cache')
    groups={}
    for row in rows:
        match=CACHE.fullmatch(row.get('key',''))
        if not match or row.get('ref')!='refs/heads/main':continue
        groups.setdefault(match[1],[]).append(row)
    return [row for group in groups.values() for row in sorted(group,key=lambda r:r.get('created_at',''),reverse=True)[keep:]]


def check_artifact_budget(existing_bytes,new_bytes):
    if min(existing_bytes,new_bytes)<0 or new_bytes>128*MB or existing_bytes+new_bytes>450*MB:
        raise RuntimeError('Artifact safety budget exceeded; refusing upload or paid storage expansion')


def api(path,method='GET'):
    if not path.startswith('/') or path.startswith('//') or '..' in path:
        raise ValueError('Invalid API path')
    token=os.environ.get('GH_TOKEN','')
    if not token:raise RuntimeError('Missing scoped workflow token')
    request=urllib.request.Request(API+path,method=method,headers={
        'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json',
        'X-GitHub-Api-Version':'2022-11-28'})
    with urllib.request.urlopen(request,timeout=30) as response:
        raw=response.read(5*MB)
        return json.loads(raw) if raw else None


def list_rows(path,key):
    values=[]
    for page in range(1,101):
        value=api(path+('? ' if False else '?')+'per_page=100&page='+str(page))
        rows=value[key];values.extend(rows)
        if len(rows)<100:return values
    raise RuntimeError('Storage inventory exceeded bounded pagination')


def prune():
    run_id=int(os.environ['GITHUB_RUN_ID'])
    # A unique artifact name prevents touching existing collector/test artifacts.
    for item in list_rows('/actions/artifacts','artifacts'):
        if item.get('name')!=ARTIFACT or item.get('expired'):continue
        old=int(item.get('workflow_run',{}).get('id',0))
        if not old or old==run_id:continue
        run=api('/actions/runs/'+str(old))
        if run.get('path')!='.github/workflows/free-ticket-site.yml' or run.get('status')!='completed':continue
        api('/actions/artifacts/'+str(int(item['id'])),'DELETE')
    for item in stale_caches(list_rows('/actions/caches','actions_caches')):
        match=CACHE.fullmatch(item['key']);old=int(match[2])
        if old==run_id:continue
        run=api('/actions/runs/'+str(old))
        if run.get('path')!='.github/workflows/free-ticket-site.yml' or run.get('status')!='completed':continue
        api('/actions/caches/'+str(int(item['id'])),'DELETE')


def preflight():
    # This call deliberately does not auto-create a Pages site or change its source.
    pages=api('/pages')
    if pages.get('build_type')!='workflow':
        raise RuntimeError('Set the repository Pages source to GitHub Actions first')
    prune()
    usage=api('/actions/cache/usage')
    if int(usage.get('active_caches_size_in_bytes',0))>8*1024**3:
        raise RuntimeError('Repository cache is near its free allowance; no expansion allowed')
    print('FREE_STORAGE_PREFLIGHT passed',flush=True)


def before_upload(tar_path):
    # Same deflate level as upload-artifact; add a margin for archive metadata.
    compressor=zlib.compressobj(6);compressed=0
    with Path(tar_path).open('rb') as source:
        while chunk:=source.read(MB):compressed+=len(compressor.compress(chunk))
    compressed+=len(compressor.flush())+MB
    existing=sum(int(row['size_in_bytes']) for row in list_rows('/actions/artifacts','artifacts') if not row.get('expired'))
    check_artifact_budget(existing,compressed)
    print('FREE_ARTIFACT_BUDGET '+json.dumps({'existing_repository_bytes':existing,'estimated_new_bytes':compressed}),flush=True)


def remove_current(artifact_id):
    item=api('/actions/artifacts/'+str(int(artifact_id)))
    if item['name']!=ARTIFACT or int(item['workflow_run']['id'])!=int(os.environ['GITHUB_RUN_ID']):
        raise RuntimeError('Refusing to delete another workflow artifact')
    api('/actions/artifacts/'+str(int(artifact_id)),'DELETE')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('operation',choices=('preflight','before-upload','remove-current'));p.add_argument('--file');p.add_argument('--artifact-id');a=p.parse_args()
    try:
        if a.operation=='preflight':preflight()
        elif a.operation=='before-upload':before_upload(a.file)
        else:remove_current(a.artifact_id)
    except Exception as exc:
        print('FREE_STORAGE_STOP '+type(exc).__name__,flush=True)
        raise SystemExit(1)
