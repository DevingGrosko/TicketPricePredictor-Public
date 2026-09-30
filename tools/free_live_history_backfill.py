#!/usr/bin/env python3
"""Append missing TicketSignal history from the verified September 29 export.

Default is a read-only plan. --apply explicitly enables the TiDB-only merge.
No uploaded SQL is executed. No source database is accessed. No stored capture
or ticket row is updated/deleted. Existing event/slot observations take priority.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import getpass
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import shutil
import ssl
import sys
import tempfile
import time
from urllib.parse import urlsplit
from zipfile import ZipFile

HOST = 'gateway01.us-east-1.prod.aws.tidbcloud.com'
USER = '4YaxooV96wmjCbK.root'
START = '2026-09-20 00:00:00'
END = '2026-09-30 01:00:00'
HASHES = {
 'mlb.sql.gz':'9ec4dc8ac1f05a7d4909a37543012577e9bc754d68a43f270dd09b0c95e4caca',
 'nfl.sql.gz':'bab3ad5711c51ab585d841986a49906267670b85ea07815187e2d8a09e5e5a60',
 'nhl.sql.gz':'193aa901928f9b94087cfc0fb34a61916b33010f78eeec2ee7a72207c2c4bf5b',
 'schema-review.tar.gz':'0dfa8da8a649e4be419319c222e90b6cbb2bb10e3d4e3c02bba6e049c6edd7b0',
}
E_COLUMNS = {
 'mlb':'id title event_date event_sections URL Place'.split(),
 'nfl':'id source_id title event_date sections source_url venue schedule_id away_team home_team canonical_venue city country neutral_site provider_venue map_geometry map_source geometry_updated_at'.split(),
 'nhl':'id source_id title event_date sections source_url venue schedule_id away_team home_team canonical_venue venue_timezone country neutral_site game_type season currency provider_venue map_geometry map_source geometry_updated_at compacted_at original_iteration_count retained_iteration_count'.split(),
}
I_COLUMNS = ['id','event_id','captured_at']
T_COLUMNS = lambda sport: ['id','section','price','ticketsPerSection' if sport=='mlb' else 'listing_count','iteration_id']
VALUE = re.compile(r"'(?:[^'\\]|\\.|'')*'|NULL|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
INSERT = re.compile(r'INSERT INTO `([a-z_]+)` VALUES (.*);\Z', re.S)
ESC = {'0':'\0','b':'\b','n':'\n','r':'\r','t':'\t','Z':'\x1a',"'":"'",'"':'"','\\':'\\','%':'\\%','_':'\\_'}

class Stop(RuntimeError): pass

def require(ok, message):
    if not ok: raise Stop(message)

def stamp(value):
    d = value if isinstance(value,datetime) else datetime.fromisoformat(str(value))
    require(d.tzinfo is None, 'Stored timestamps must be timezone-naive; no conversion is allowed.')
    return d.isoformat(sep=' ',timespec='microseconds')

def dumps(value): return json.dumps(value,default=str,separators=(',',':'),ensure_ascii=True)

def rows(text):
    i = 0
    while i < len(text):
        require(text[i]=='(', 'Invalid literal row.')
        i += 1; row=[]
        while True:
            m=VALUE.match(text,i); require(m is not None,'Unsupported SQL value.')
            token=m[0];i=m.end()
            if token.startswith("'"):
                v=re.sub(r"\\(.)|''",lambda x: ESC.get(x[1],x[1]) if x[1] is not None else "'",token[1:-1],flags=re.S)
            elif token=='NULL': v=None
            else: v=Decimal(token) if any(c in token for c in '.eE') else int(token)
            row.append(v)
            require(i<len(text) and text[i] in ',)','Invalid value separator.')
            sep=text[i];i+=1
            if sep==')':break
        yield tuple(row)
        if i<len(text):require(text[i]==',','Invalid row separator.');i+=1

def tables(sport):
    prefix='' if sport=='mlb' else sport+'_'
    require(sport in E_COLUMNS,'Unknown sport.')
    return prefix+'event',prefix+'iterations',prefix+'tickets'

def event_key(sport,e):
    u=e.get('URL') if sport=='mlb' else e.get('source_url')
    p=urlsplit(str(u or ''))
    require(p.scheme=='https' and p.hostname in ('www.vividseats.com','vividseats.com'),'Unrecognized provider URL.')
    m=re.search(r'/production/(\d+)/?$',p.path)
    require(m is not None,'Missing provider production identity.')
    if sport!='mlb': require(str(e['source_id'])==m[1],'Source URL and provider ID disagree.')
    return m[1]

def validate_archive(path):
    with ZipFile(path) as z:
        require(len(z.infolist())==5 and set(z.namelist())==set(HASHES)|{'SHA256SUMS.txt'},'Unexpected bundle members.')
        for name,expected in HASHES.items():
            digest=hashlib.sha256()
            with z.open(name) as f:
                for b in iter(lambda:f.read(1024**2),b''):digest.update(b)
            require(digest.hexdigest()==expected,'Wrong or damaged export: '+name)
    print('VERIFIED: all four source checksums. No database connection yet.',flush=True)

def prepare(path, sport, work):
    """Create a private, disposable index of raw observations since September 20."""
    print(sport+': validating and indexing the source export...',flush=True)
    out=work/(sport+'.sqlite');require(not out.exists(),'Scratch index already exists.')
    db=sqlite3.connect(out)
    db.executescript('PRAGMA journal_mode=OFF;PRAGMA synchronous=OFF;CREATE TABLE events(id INTEGER PRIMARY KEY,body TEXT NOT NULL);CREATE TABLE captures(id INTEGER PRIMARY KEY,event_id INTEGER NOT NULL,captured_at TEXT NOT NULL);CREATE TABLE tickets(id INTEGER PRIMARY KEY,section TEXT NOT NULL,price INTEGER NOT NULL,listing_count INTEGER,iteration_id INTEGER NOT NULL);')
    et,it,tt=tables(sport);expected={et:E_COLUMNS[sport],it:I_COLUMNS,tt:T_COLUMNS(sport)}
    schema={};active=None;chosen=set();last='';scanned=0
    with ZipFile(path) as z, z.open(sport+'.sql.gz') as member, gzip.GzipFile(fileobj=member) as source:
        while True:
            raw=source.readline(4*1024**2+1)
            if not raw:break
            require(len(raw)<=4*1024**2,'Source statement exceeds reviewed limit.')
            line=raw.decode('utf-8');last=line
            if line.startswith('CREATE TABLE '):
                m=re.match(r'CREATE TABLE `([a-z_]+)` \(',line);require(m is not None,'Invalid table definition.')
                active=m[1];schema[active]=[];continue
            if active:
                m=re.match(r'  `([A-Za-z_]+)` ',line)
                if m:schema[active].append(m[1])
                if line.startswith(') ENGINE=InnoDB'):active=None
                continue
            text=line.rstrip('\n')
            if not text or text.startswith('--') or re.fullmatch(r'/\*!\d+ SET [^\r\n]* \*/;',text):continue
            m=INSERT.fullmatch(text);require(m is not None,'Unexpected non-data SQL.')
            name=m[1]
            if name not in expected:continue
            require(schema[name]==expected[name],'Source column order differs from reviewed export.')
            batch=[]
            for row in rows(m[2]):
                require(len(row)==len(expected[name]),'Unexpected row width.')
                if name==et:batch.append((row[0],dumps(dict(zip(expected[name],row)))))
                elif name==it:
                    at=stamp(row[2])
                    if stamp(START)<=at<=stamp(END):
                        chosen.add(row[0]);batch.append((row[0],row[1],at))
                else:
                    scanned+=1
                    if row[4] in chosen:
                        require(type(row[2]) is int and row[2]>0 and type(row[3]) is int and row[3]>0,'Invalid historical price/inventory.')
                        batch.append(row)
            if batch:
                table={et:'events',it:'captures',tt:'tickets'}[name]
                db.executemany('INSERT INTO '+table+' VALUES ('+','.join('?'*len(batch[0]))+')',batch)
            if name==tt and scanned//500000!=(scanned-len(batch))//500000:
                print(sport+': indexing archived price rows...',flush=True)
    require(active is None and last.startswith('-- Dump completed on '),'Incomplete source dump.')
    db.executescript('CREATE INDEX tickets_iteration ON tickets(iteration_id);CREATE INDEX captures_event_slot ON captures(event_id,captured_at);')
    require(not db.execute('SELECT 1 FROM captures GROUP BY event_id,captured_at HAVING count(*)>1 LIMIT 1').fetchone(),'Source contains duplicate capture slots requiring review.')
    require(not db.execute('SELECT 1 FROM captures c LEFT JOIN events e ON c.event_id=e.id WHERE e.id IS NULL LIMIT 1').fetchone(),'Source has orphaned captures.')
    require(not db.execute('SELECT 1 FROM captures c WHERE NOT EXISTS (SELECT 1 FROM tickets t WHERE t.iteration_id=c.id) LIMIT 1').fetchone(),'Source has an empty capture.')
    db.commit()
    print(sport+': source index ready: '+str(len(chosen))+' captures.',flush=True)
    return db

def source_events(source):
    return {i:json.loads(body) for i,body in source.execute('SELECT id,body FROM events WHERE id IN (SELECT event_id FROM captures)')}

def connect(sport,password):
    import pymysql
    c=pymysql.connect(host=HOST,port=4000,user=USER,password=password,database='ticketsignal_staging_'+sport,charset='utf8mb4',ssl=ssl.create_default_context(),connect_timeout=15,read_timeout=120,write_timeout=120,autocommit=False,cursorclass=pymysql.cursors.DictCursor)
    with c.cursor() as q:
        q.execute('SELECT DATABASE() AS db,VERSION() AS version,@@foreign_key_checks AS fk')
        row=q.fetchone()
        require(row['db']=='ticketsignal_staging_'+sport and 'tidb' in row['version'].lower() and int(row['fk'])==1,'Wrong destination or disabled constraints.')
        q.execute("SET SESSION tidb_txn_mode='pessimistic'")
    c.rollback();return c

def inventory(c,sport):
    et,it,_=tables(sport)
    with c.cursor() as q:
        q.execute('SELECT '+','.join('`'+x+'`' for x in E_COLUMNS[sport] if x!='map_geometry')+' FROM `'+et+'`');events=q.fetchall()
        q.execute('SELECT id,event_id,captured_at FROM `'+it+'`');captures=q.fetchall()
    c.rollback()
    bykey={}
    for e in events:
        key=event_key(sport,e)
        require(key not in bykey,'Ambiguous provider identity in destination: '+sport)
        bykey[key]=e
    slots=defaultdict(list)
    for row in captures:slots[(row['event_id'],stamp(row['captured_at']))].append(row['id'])
    return bykey,slots

def plan(c,sport,source):
    et,it,tt=tables(sport)
    with c.cursor() as q:
        q.execute('SELECT TABLE_NAME AS table_name,COLUMN_NAME AS column_name FROM information_schema.columns WHERE table_schema=%s ORDER BY TABLE_NAME,ORDINAL_POSITION',('ticketsignal_staging_'+sport,))
        columns=defaultdict(list)
        for row in q.fetchall():columns[row['table_name']].append(row['column_name'])
    c.rollback()
    for name,expected in [(et,E_COLUMNS[sport]),(it,I_COLUMNS),(tt,T_COLUMNS(sport))]:
        require(columns[name]==expected,'Destination schema differs: '+sport+'/'+name)
    target,slots=inventory(c,sport);events=source_events(source);missing=[];overlap=0;new=set()
    for iid,eid,at in source.execute('SELECT id,event_id,captured_at FROM captures'):
        e=events[eid];t=target.get(event_key(sport,e))
        if t is not None and (t['id'],at) in slots:overlap+=1
        else:
            n=source.execute('SELECT count(*) FROM tickets WHERE iteration_id=?',(iid,)).fetchone()[0]
            missing.append((iid,eid,at,n))
            if t is None:new.add(eid)
    return {'sport':sport,'missing_captures':len(missing),'missing_ticket_rows':sum(x[3] for x in missing),'new_events':len(new),'preserve_existing_slots':overlap,'missing_by_day':dict(sorted(Counter(x[2][:10] for x in missing).items()))}

def chunks(values,size=500):
    for start in range(0,len(values),size):yield values[start:start+size]

def section_rows(source, ids):
    result=[]
    for batch in chunks(ids):
        result.extend(source.execute('SELECT iteration_id,section,price,listing_count FROM tickets WHERE iteration_id IN ('+','.join('?'*len(batch))+')',batch))
    return result

def read_prices(q,sport,ids):
    _,_,tt=tables(sport);quantity=T_COLUMNS(sport)[3];result=[]
    for batch in chunks(ids):
        q.execute('SELECT iteration_id,section,price,`'+quantity+'` AS quantity FROM `'+tt+'` WHERE iteration_id IN ('+','.join(['%s']*len(batch))+')',tuple(batch))
        result.extend((r['iteration_id'],r['section'],r['price'],r['quantity']) for r in q.fetchall())
    return result

def merge_event(c,sport,source,e,known_id,apply=True):
    """One event transaction. A repeated run rechecks slots and adds no duplicates."""
    et,it,tt=tables(sport);quantity=T_COLUMNS(sport)[3];sections_column='event_sections' if sport=='mlb' else 'sections'
    captures=list(source.execute('SELECT id,captured_at FROM captures WHERE event_id=? ORDER BY captured_at',(e['id'],)))
    c.begin();added=[];expected=[];date_changed=False
    try:
        with c.cursor() as q:
            if known_id is not None:
                q.execute('SELECT * FROM `'+et+'` WHERE id=%s FOR UPDATE',(known_id,));stored=q.fetchone()
                require(stored is not None and event_key(sport,stored)==event_key(sport,e),'Destination event identity changed.')
                eid=stored['id']
            else:
                # Recheck by natural provider identity immediately before inserting.
                if sport=='mlb':
                    q.execute('SELECT * FROM event WHERE URL LIKE %s FOR UPDATE',('%/production/'+event_key(sport,e)+'%',))
                    matches=[r for r in q.fetchall() if event_key(sport,r)==event_key(sport,e)]
                else:
                    q.execute('SELECT * FROM `'+et+'` WHERE source_id=%s FOR UPDATE',(e['source_id'],));matches=q.fetchall()
                require(len(matches)<2,'Ambiguous destination event.')
                if matches:stored=matches[0];eid=stored['id']
                else:
                    require(apply,'Read-only path cannot create an event.')
                    cols=E_COLUMNS[sport][1:]
                    q.execute('INSERT INTO `'+et+'` ('+','.join('`'+x+'`' for x in cols)+') VALUES ('+','.join(['%s']*len(cols))+')',tuple(e[x] for x in cols))
                    eid=q.lastrowid;require(bool(eid),'No generated event ID.');stored={**e,'id':eid}
            q.execute('SELECT id,captured_at FROM `'+it+'` WHERE event_id=%s',(eid,));existing=q.fetchall()
            slots=defaultdict(list)
            for r in existing:slots[stamp(r['captured_at'])].append(r['id'])
            needed=[(i,at) for i,at in captures if at not in slots]
            overlap_ids=[rid for _,at in captures for rid in slots.get(at,[])]
            if overlap_ids:
                present=set()
                for batch in chunks(overlap_ids):
                    q.execute('SELECT DISTINCT iteration_id FROM `'+tt+'` WHERE iteration_id IN ('+','.join(['%s']*len(batch))+')',tuple(batch))
                    present.update(r['iteration_id'] for r in q.fetchall())
                require(present==set(overlap_ids),'An existing overlapping capture is empty; manual review required.')
            if not needed:c.rollback();return {'event_id':eid,'inserted_captures':0,'inserted_rows':0,'preserved':len(captures),'date_updated':False}
            require(apply,'Unexpected write in read-only mode.')
            for batch in chunks(needed):
                q.executemany('INSERT INTO `'+it+'` (event_id,captured_at) VALUES (%s,%s)',[(eid,at) for _,at in batch])
            q.execute('SELECT id,captured_at FROM `'+it+'` WHERE event_id=%s',(eid,));after=defaultdict(list)
            for r in q.fetchall():after[stamp(r['captured_at'])].append(r['id'])
            mapping={}
            for source_id,at in needed:
                require(len(after[at])==1,'Concurrent or duplicate capture needs review; transaction rolled back.')
                mapping[source_id]=after[at][0];added.append({'source_iteration':source_id,'iteration_id':after[at][0],'captured_at':at})
            original=section_rows(source,list(mapping))
            expected=[(mapping[i],section,price,qty) for i,section,price,qty in original]
            for batch in chunks(expected,1000):
                q.executemany('INSERT INTO `'+tt+'` (iteration_id,section,price,`'+quantity+'`) VALUES (%s,%s,%s,%s)',batch)
            names=json.loads(stored[sections_column]) if isinstance(stored[sections_column],str) else stored[sections_column]
            merged=list(dict.fromkeys([*(names or []),*(r[1] for r in expected)]))
            if merged!=names:q.execute('UPDATE `'+et+'` SET `'+sections_column+'`=%s WHERE id=%s',(dumps(merged),eid))
            latest=max((stamp(r['captured_at']) for r in existing),default='')
            if stamp(e['event_date'])!=stamp(stored['event_date']) and max(at for _,at in captures)>latest:
                q.execute('UPDATE `'+et+'` SET event_date=%s WHERE id=%s',(e['event_date'],eid));date_changed=True
            require(Counter(read_prices(q,sport,list(mapping.values())))==Counter(expected),'Pre-commit price readback mismatch.')
        c.commit()
    except BaseException:
        c.rollback();raise
    # Independent, committed readback; failures retain receipt data for safe resume.
    with c.cursor() as q:
        require(Counter(read_prices(q,sport,[x['iteration_id'] for x in added]))==Counter(expected),'Post-commit price readback mismatch; report before resuming.')
    c.rollback()
    return {'event_id':eid,'inserted_captures':len(added),'inserted_rows':len(expected),'preserved':len(captures)-len(added),'date_updated':date_changed,'added':added}

REPO = 'DevingGrosko/TicketPricePredictor-Public'
COLLECT_WORKFLOW = 'free-ticket-collect.yml'
PUBLISH_WORKFLOW = 'free-ticket-site.yml'
API_ROOT = 'https://api.github.com/repos/' + REPO + '/actions/workflows/'
OPEN_STATES = ('queued', 'in_progress', 'waiting', 'pending', 'requested')

class GitHubAPI:
    """Only this repository's collector maintenance and publisher dispatch."""
    def __init__(self, token):
        from urllib.request import build_opener, HTTPRedirectHandler
        require(token and token.isascii() and not any(c.isspace() for c in token),
                'Enter the scheduler token itself, without Bearer or whitespace.')
        self.token = token
        class NoRedirects(HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, message, headers, newurl):
                raise Stop('GitHub unexpectedly redirected the authenticated request.')
        self.opener = build_opener(NoRedirects())

    def call(self, method, workflow, suffix='', payload=None):
        from urllib.request import Request
        from urllib.error import HTTPError, URLError
        require(workflow in (COLLECT_WORKFLOW, PUBLISH_WORKFLOW), 'Unexpected workflow.')
        allowed = (method == 'GET' and workflow == COLLECT_WORKFLOW and
                   (suffix == '' or re.fullmatch(r'/runs\?status=(queued|in_progress|waiting|pending|requested)&per_page=1', suffix)))
        allowed = allowed or (method == 'PUT' and workflow == COLLECT_WORKFLOW and suffix in ('/disable', '/enable'))
        allowed = allowed or (method == 'POST' and suffix == '/dispatches' and payload == {'ref':'main'})
        require(allowed, 'Unexpected scheduler request.')
        request = Request(API_ROOT + workflow + suffix, method=method,
            data=None if payload is None else dumps(payload).encode(),
            headers={'Authorization':'Bearer ' + self.token,
                     'Accept':'application/vnd.github+json',
                     'Content-Type':'application/json',
                     'User-Agent':'TicketSignal-history-backfill',
                     'X-GitHub-Api-Version':'2026-03-10'})
        attempts = 1 if method == 'POST' else 4
        for attempt in range(attempts):
            try:
                with self.opener.open(request, timeout=30) as response:
                    raw = response.read(2 * 1024**2 + 1)
                require(len(raw) <= 2 * 1024**2, 'Oversized GitHub response.')
                return json.loads(raw) if raw else {}
            except HTTPError as exc:
                # Never print authentication headers or error response bodies.
                if exc.code not in (429, 500, 502, 503, 504) or attempt == attempts-1:
                    raise Stop('GitHub HTTP ' + str(exc.code) + ' during ' + method + ' ' + workflow + suffix) from None
            except (URLError, TimeoutError, OSError):
                if attempt == attempts-1:
                    raise Stop('GitHub request could not be confirmed: ' + method + ' ' + workflow + suffix) from None
            time.sleep(min(2**attempt, 8))

class CollectorPause:
    """Temporarily disable only the NEW writer and drain already accepted runs.

    This is necessary because the original MLB table has no unique capture-slot
    index. Its live writer does not participate in this importer's row locks.
    The legacy PythonAnywhere workflow and public site remain untouched.
    """
    def __init__(self, api, report, save, max_wait=1800):
        self.api, self.report, self.save = api, report, save
        self.max_wait = max_wait
        self.restore_required = False
        self.quiet = False

    def state(self):
        row = self.api.call('GET', COLLECT_WORKFLOW)
        require(row.get('path') == '.github/workflows/' + COLLECT_WORKFLOW,
                'GitHub returned a different collection workflow.')
        return row.get('state')

    def note(self, status):
        self.report['collector_maintenance'] = {'status':status, 'workflow':COLLECT_WORKFLOW,
            'updated_at':datetime.now(timezone.utc).isoformat(),
            'legacy_pythonanywhere_changed':False}
        self.save()

    def __enter__(self):
        require(self.state() == 'active', 'The free collector is not active; review its existing state before importing.')
        # Set intent before PUT because a timeout can follow a successful disable.
        self.restore_required = True
        self.note('pause-requested')
        try:
            self.api.call('PUT', COLLECT_WORKFLOW, '/disable')
            require(self.state() == 'disabled_manually', 'Free collector pause not confirmed; no import permitted.')
            self.note('paused-waiting-for-active-runs')
            print('Paused only Free TicketSignal collection. Waiting for existing runs to finish; neither website is stopped.', flush=True)
            end = time.monotonic() + self.max_wait
            quiet_checks = 0
            while quiet_checks < 2:
                require(self.state() == 'disabled_manually', 'Collector was re-enabled before import; stopping.')
                counts = {}
                for state in OPEN_STATES:
                    data = self.api.call('GET', COLLECT_WORKFLOW,
                                        '/runs?status=' + state + '&per_page=1')
                    count = data.get('total_count')
                    require(type(count) is int and count >= 0, 'Cannot establish whether collection is still running.')
                    counts[state] = count
                if any(counts.values()):
                    quiet_checks = 0
                    print('Waiting for pre-existing collection runs: ' + dumps(counts), flush=True)
                else:
                    quiet_checks += 1
                require(time.monotonic() < end, 'Existing collection did not finish within the maintenance wait; no rows imported.')
                if quiet_checks < 2: time.sleep(5 if not any(counts.values()) else 15)
            self.quiet = True
            self.note('paused-and-drained')
            return self
        except BaseException:
            self.restore()
            raise

    def check(self):
        require(self.quiet and self.state() == 'disabled_manually',
                'Free collector is no longer paused; refusing additional import writes.')

    def restore(self):
        if not self.restore_required: return
        self.quiet = False
        try:
            if self.state() != 'active':
                self.api.call('PUT', COLLECT_WORKFLOW, '/enable')
            require(self.state() == 'active', 'Collector restart not confirmed.')
            self.restore_required = False
            self.note('resumed')
            print('VERIFIED: Free TicketSignal collection re-enabled. The normal cron schedule can resume.', flush=True)
        except BaseException:
            self.note('resume-not-verified')
            print('ACTION REQUIRED: collector resume could not be verified. In GitHub Actions, open Free TicketSignal collection and Enable workflow. The report records this limitation.', flush=True)
            raise

    def __exit__(self, kind, value, traceback):
        self.restore()
        return False


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--apply',action='store_true',help='Pause only the new collector, merge, then restore it; requires the scheduler token.')
    args=parser.parse_args();os.umask(0o077)
    if args.source is None:
        options=list(Path.home().glob('ticketsignal-export.*/ticketsignal-history-backfill.zip'))
        valid=[]
        for p in options:
            try:validate_archive(p);valid.append(p)
            except (Stop,ValueError):pass
        require(len(valid)==1,'Specify --source with the exact September 29 bundle path.');args.source=valid[0]
    else:validate_archive(args.source)
    with (Path.home()/'.ticketsignal-history-backfill.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        work=Path(tempfile.mkdtemp(prefix='ticketsignal-history-merge.',dir=Path.home()))
        print('Working directory: '+str(work),flush=True)
        require(shutil.disk_usage(work).free>=140*1024**2,'At least 140 MiB of spare local disk is required for the checked source indexes.')
        sources={s:prepare(args.source,s,work) for s in E_COLUMNS}
        password=os.environ.get('TIDB_STAGING_PASSWORD') or getpass.getpass('TiDB staging password (NOT PythonAnywhere): ')
        connections={};report={'status':'preflight','source_hashes':HASHES,'work_dir':str(work),'plans':{},'results':{}}
        def save():
            p=work/'backfill-report.json';temp=p.with_suffix('.tmp');temp.write_text(json.dumps(report,indent=2,default=str));temp.replace(p)
        try:
            for s in E_COLUMNS:
                connections[s]=connect(s,password);report['plans'][s]=plan(connections[s],s,sources[s])
                print('PLAN '+dumps(report['plans'][s]),flush=True)
                require(report['plans'][s]['missing_ticket_rows']<900000,'Per-sport backfill exceeds the static incremental-build budget; review required.')
            save()
            if not args.apply:
                print('READ-ONLY PLAN COMPLETE. No rows inserted. Re-run with --apply to merge.',flush=True);return
            print('The merge must briefly pause only the NEW free collector to avoid racing its database writes.',flush=True)
            print('Use the GitHub scheduler token already entered in cron-job.org. It stays in memory and is not written to the report.',flush=True)
            token=getpass.getpass('GitHub scheduler token (Actions read/write; NOT GitHub password): ')
            api=GitHubAPI(token)
            with CollectorPause(api,report,save) as maintenance:
                report['status']='applying';save()
                for s in E_COLUMNS:
                    # Reconnect after waiting and between sports, avoiding idle sockets.
                    connections[s].close();connections[s]=connect(s,password)
                    c=connections[s];source=sources[s];target,_=inventory(c,s)
                    result={'inserted_captures':0,'inserted_rows':0,'preserved':0,'events':[]};report['results'][s]=result
                    for i,e in source_events(source).items():
                        maintenance.check()
                        old=target.get(event_key(s,e));known=None if old is None else old['id']
                        change=merge_event(c,s,source,e,known)
                        for k in ('inserted_captures','inserted_rows','preserved'):result[k]+=change[k]
                        result['events'].append({'source_event_id':i,**change});save()
                        if change['inserted_captures']:
                            print(s+': saved '+str(result['inserted_captures'])+' missing captures / '+str(result['inserted_rows'])+' price rows; exact readback passed.',flush=True)
                    final=plan(c,s,source);require(final['missing_captures']==0,'Some source capture slots remain absent.')
                    result['final_plan']=final;save();print('VERIFIED '+s+': all selected source slots represented; pre-existing captures preserved.',flush=True)
                report['data_verified']=True;save()
            report['status']='verified';save()
            # Dispatch once, but do not turn a successful merge into a data failure
            # merely because GitHub temporarily cannot accept the refresh request.
            try:
                api.call('POST', PUBLISH_WORKFLOW, '/dispatches', {'ref':'main'})
                report['publication_dispatch']='accepted'
            except Stop as exc:
                report['publication_dispatch']='not-confirmed'
                print('History is verified; immediate publication dispatch was not confirmed. The normal publication trigger can retry.',flush=True)
            save()
            print('BACKFILL VERIFIED. PythonAnywhere and raw existing TiDB captures were not modified.',flush=True)
            print('SEND THIS REPORT: '+str(work/'backfill-report.json'),flush=True)
            print('The next normal publication should include the appended history. Publication still needs independent verification.',flush=True)
        except BaseException as exc:
            report['status']='stopped';report['error_type']=type(exc).__name__
            if isinstance(exc,Stop):report['detail']=str(exc)
            save();print('STOP: '+(str(exc) if isinstance(exc,Stop) else type(exc).__name__),flush=True)
            print('Earlier committed event batches may remain. Nothing was deleted. Send '+str(work/'backfill-report.json'),flush=True)
            raise SystemExit(1) from None
        finally:
            for c in connections.values():
                try:c.close()
                except Exception:pass
            for source in sources.values():source.close()

if __name__=='__main__':
    import signal
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt('Interrupted; restoring collector when possible.')
    signal.signal(signal.SIGTERM, interrupted)
    try:main()
    except (Stop,BlockingIOError) as exc:raise SystemExit('STOP: '+str(exc)) from None
