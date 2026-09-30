"""Offline history-merge tests. No network, credentials, or production writes."""
import json
import sqlite3
import unittest
from unittest.mock import patch
from tools import free_live_history_backfill as b

class Cursor:
    def __init__(self,c): self.owner=c; self.q=c.db.cursor()
    def __enter__(self): return self
    def __exit__(self,*args): self.q.close()
    def execute(self,sql,params=()):
        self.owner.statements.append(sql)
        self.q.execute(sql.replace('%s','?').replace(' FOR UPDATE',''),params)
        return self
    def executemany(self,sql,rows):
        self.owner.statements.append(sql)
        if self.owner.fail and sql.startswith('INSERT INTO `tickets`'):
            raise RuntimeError('injected write failure')
        self.q.executemany(sql.replace('%s','?'),rows)
        return self
    def fetchall(self): return [dict(r) for r in self.q.fetchall()]
    def fetchone(self):
        row=self.q.fetchone()
        return None if row is None else dict(row)
    @property
    def lastrowid(self): return self.q.lastrowid

class Connection:
    def __init__(self,sport='mlb'):
        self.db=sqlite3.connect(':memory:')
        self.db.row_factory=sqlite3.Row
        self.fail=False; self.statements=[]
        et,it,tt=b.tables(sport)
        fields=['`'+name+'` '+('INTEGER PRIMARY KEY AUTOINCREMENT' if name=='id' else 'TEXT')
                for name in b.E_COLUMNS[sport]]
        self.db.execute('CREATE TABLE '+et+' ('+','.join(fields)+')')
        self.db.execute('CREATE TABLE '+it+' (id INTEGER PRIMARY KEY AUTOINCREMENT,event_id INTEGER,captured_at TEXT)')
        self.db.execute('CREATE TABLE '+tt+' (id INTEGER PRIMARY KEY AUTOINCREMENT,iteration_id INTEGER,section TEXT,price INTEGER,`'+b.T_COLUMNS(sport)[3]+'` INTEGER)')
    def begin(self): self.db.execute('BEGIN')
    def cursor(self): return Cursor(self)
    def commit(self): self.db.commit()
    def rollback(self): self.db.rollback()

class MergeTests(unittest.TestCase):
    def source(self,sport='mlb',captures=2):
        s=sqlite3.connect(':memory:');self.addCleanup(s.close)
        s.executescript('CREATE TABLE events(id INTEGER PRIMARY KEY,body TEXT);CREATE TABLE captures(id INTEGER PRIMARY KEY,event_id INTEGER,captured_at TEXT);CREATE TABLE tickets(id INTEGER PRIMARY KEY,section TEXT,price INTEGER,listing_count INTEGER,iteration_id INTEGER);')
        e={name:None for name in b.E_COLUMNS[sport]}
        e.update(id=17,title='Visitor at Home',event_date='2026-09-26 19:00:00.000000')
        e.update({'URL':'https://www.vividseats.com/a/production/123','Place':'Nationals Park','event_sections':'["101"]'} if sport=='mlb' else
                 {'source_url':'https://www.vividseats.com/a/production/123','source_id':'123','venue':'Arena','sections':'["101"]'})
        s.execute('INSERT INTO events VALUES (?,?)',(17,json.dumps(e)))
        for i in range(captures):
            s.execute('INSERT INTO captures VALUES (?,?,?)',(50+i,17,'2026-09-21 '+str(10+i)+':00:00.000000'))
            s.execute('INSERT INTO tickets VALUES (?,?,?,?,?)',(i+1,'101',80+i,2,50+i))
        s.commit();return s,e
    def target(self,sport='mlb'):
        c=Connection(sport);self.addCleanup(c.db.close);return c
    def test_append_uses_new_ids_and_preserves_timestamps_and_prices(self):
        s,e=self.source();c=self.target();out=b.merge_event(c,'mlb',s,e,None)
        self.assertEqual((out['inserted_captures'],out['inserted_rows']),(2,2))
        self.assertNotEqual(out['event_id'],17)
        self.assertEqual([r[0] for r in c.db.execute('SELECT price FROM tickets ORDER BY id')],[80,81])
        self.assertEqual(c.db.execute('SELECT captured_at FROM iterations ORDER BY id').fetchone()[0],'2026-09-21 10:00:00.000000')
    def test_repeat_is_idempotent(self):
        s,e=self.source();c=self.target();first=b.merge_event(c,'mlb',s,e,None)
        second=b.merge_event(c,'mlb',s,e,first['event_id'])
        self.assertEqual(second['inserted_captures'],0)
        self.assertEqual(c.db.execute('SELECT count(*) FROM tickets').fetchone()[0],2)
    def test_overlapping_prices_are_never_overwritten(self):
        s,e=self.source();c=self.target();first=b.merge_event(c,'mlb',s,e,None)
        c.db.execute('UPDATE tickets SET price=999 WHERE id=1');c.commit()
        b.merge_event(c,'mlb',s,e,first['event_id'])
        self.assertEqual(c.db.execute('SELECT price FROM tickets WHERE id=1').fetchone()[0],999)
    def test_mid_transaction_failure_rolls_back_every_new_row(self):
        s,e=self.source();c=self.target();c.fail=True
        with self.assertRaises(RuntimeError): b.merge_event(c,'mlb',s,e,None)
        for table in ('event','iterations','tickets'):
            self.assertEqual(c.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
    def test_existing_history_survives_failed_new_batch(self):
        s,e=self.source(captures=1);c=self.target();first=b.merge_event(c,'mlb',s,e,None)
        s.execute('INSERT INTO captures VALUES (51,17,?)',('2026-09-21 11:00:00.000000',))
        s.execute('INSERT INTO tickets VALUES (2,?,100,3,51)',('102',));s.commit();c.fail=True
        with self.assertRaises(RuntimeError): b.merge_event(c,'mlb',s,e,first['event_id'])
        self.assertEqual(c.db.execute('SELECT COUNT(*) FROM iterations').fetchone()[0],1)
        self.assertEqual(c.db.execute('SELECT price FROM tickets').fetchone()[0],80)
    def test_empty_existing_overlap_is_not_accepted(self):
        s,e=self.source();c=self.target();first=b.merge_event(c,'mlb',s,e,None)
        c.db.execute('DELETE FROM tickets WHERE iteration_id=1');c.commit()
        with self.assertRaises(b.Stop): b.merge_event(c,'mlb',s,e,first['event_id'])
    def test_source_numerical_id_does_not_match_unrelated_target(self):
        s,e=self.source();c=self.target();first=b.merge_event(c,'mlb',s,e,None)
        c.db.execute("UPDATE event SET URL='https://www.vividseats.com/wrong/production/999'");c.commit()
        with self.assertRaises(b.Stop): b.merge_event(c,'mlb',s,e,first['event_id'])
    def test_alternate_url_slug_matches_same_production(self):
        s,e=self.source();c=self.target();b.merge_event(c,'mlb',s,e,None)
        c.db.execute("UPDATE event SET URL='https://www.vividseats.com/changed/production/123'");c.commit()
        again=b.merge_event(c,'mlb',s,e,None)
        self.assertEqual(again['inserted_captures'],0)
        self.assertEqual(c.db.execute('SELECT COUNT(*) FROM event').fetchone()[0],1)
    def test_all_sports_preserve_quantities(self):
        for sport in ('mlb','nfl','nhl'):
            with self.subTest(sport=sport):
                s,e=self.source(sport);c=self.target(sport);out=b.merge_event(c,sport,s,e,None)
                self.assertEqual(out['inserted_rows'],2)
                self.assertEqual(c.db.execute('SELECT '+b.T_COLUMNS(sport)[3]+' FROM '+b.tables(sport)[2]).fetchone()[0],2)
    def test_older_source_cannot_replace_newer_event_time(self):
        s,e=self.source(captures=1);c=self.target();first=b.merge_event(c,'mlb',s,e,None)
        c.db.execute('UPDATE event SET event_date=?',('2026-09-27 20:00:00',))
        c.db.execute('INSERT INTO iterations(event_id,captured_at) VALUES (?,?)',(first['event_id'],'2026-09-29 00:00:00'));c.commit()
        s.execute('INSERT INTO captures VALUES (51,17,?)',('2026-09-22 11:00:00.000000',))
        s.execute('INSERT INTO tickets VALUES (2,?,90,3,51)',('102',));s.commit()
        out=b.merge_event(c,'mlb',s,e,first['event_id'])
        self.assertFalse(out['date_updated'])
        self.assertEqual(c.db.execute('SELECT event_date FROM event').fetchone()[0],'2026-09-27 20:00:00')
        self.assertEqual(json.loads(c.db.execute('SELECT event_sections FROM event').fetchone()[0]),['101','102'])
    def test_newer_source_can_restore_reviewed_reschedule(self):
        s,e=self.source(captures=1);c=self.target();first=b.merge_event(c,'mlb',s,e,None)
        c.db.execute('UPDATE event SET event_date=?',('2026-09-25 20:00:00',));c.commit()
        s.execute('INSERT INTO captures VALUES (51,17,?)',('2026-09-22 11:00:00.000000',))
        s.execute('INSERT INTO tickets VALUES (2,?,90,3,51)',('101',));s.commit()
        self.assertTrue(b.merge_event(c,'mlb',s,e,first['event_id'])['date_updated'])
    def test_no_capture_or_ticket_update_delete_or_schema_changes(self):
        s,e=self.source();c=self.target();b.merge_event(c,'mlb',s,e,None)
        for sql in c.statements:
            self.assertFalse(sql.upper().startswith(('DELETE','ALTER','CREATE','DROP','TRUNCATE')))
            if sql.startswith('UPDATE'): self.assertTrue(sql.startswith('UPDATE `event`'))
    def test_literal_parser_does_not_execute_sql(self):
        self.assertEqual(list(b.rows("(7,'a\\'b',NULL,12)")),[(7,"a'b",None,12)])
        with self.assertRaises(b.Stop): list(b.rows('(1,SLEEP(5))'))
    def test_timezone_and_fraction_preserved(self):
        self.assertEqual(b.stamp('2026-09-21 10:00:00.123456'),'2026-09-21 10:00:00.123456')
        with self.assertRaises(b.Stop): b.stamp('2026-09-21T10:00:00+00:00')
    def test_provider_identity_mismatch_and_untrusted_host_rejected(self):
        with self.assertRaises(b.Stop): b.event_key('mlb',{'URL':'https://attacker.example/production/123'})
        with self.assertRaises(b.Stop): b.event_key('nfl',{'source_url':'https://www.vividseats.com/a/production/123','source_id':'999'})

class FakeAPI:
    def __init__(self,state='active',busy=False):
        self.state=state;self.busy=busy;self.calls=[];self.fail_disable=False;self.fail_enable=False
    def call(self,method,workflow,suffix='',payload=None):
        self.calls.append((method,workflow,suffix,payload))
        if method=='GET' and suffix=='':
            return {'path':'.github/workflows/'+b.COLLECT_WORKFLOW,'state':self.state}
        if method=='GET': return {'total_count':int(self.busy)}
        if suffix=='/disable':
            self.state='disabled_manually'
            if self.fail_disable: raise b.Stop('simulated lost acknowledgment')
        if suffix=='/enable':
            if self.fail_enable: raise b.Stop('simulated outage')
            self.state='active'
        return {}

class MaintenanceTests(unittest.TestCase):
    def test_normal_pause_drain_resume_is_limited_to_free_collector(self):
        api=FakeAPI();report={}
        with patch.object(b.time,'sleep'):
            with b.CollectorPause(api,report,lambda:None) as guard:
                guard.check();self.assertEqual(api.state,'disabled_manually')
            self.assertEqual(api.state,'active')
            self.assertEqual(report['collector_maintenance']['status'],'resumed')
        self.assertTrue(all(row[1]==b.COLLECT_WORKFLOW for row in api.calls))
        self.assertFalse(any('cancel' in row[2] for row in api.calls))
    def test_import_error_still_resumes_collector(self):
        api=FakeAPI();report={}
        with patch.object(b.time,'sleep'),self.assertRaises(RuntimeError):
            with b.CollectorPause(api,report,lambda:None): raise RuntimeError('simulated database failure')
        self.assertEqual(api.state,'active')
    def test_lost_disable_acknowledgment_restores_original_state(self):
        api=FakeAPI();api.fail_disable=True
        with self.assertRaises(b.Stop):
            with b.CollectorPause(api,{},lambda:None): self.fail('Must not reach import')
        self.assertEqual(api.state,'active')
    def test_already_disabled_collector_is_not_silently_enabled(self):
        api=FakeAPI(state='disabled_manually')
        with self.assertRaises(b.Stop):
            with b.CollectorPause(api,{},lambda:None): self.fail('Must not reach import')
        self.assertTrue(all(x[0]=='GET' for x in api.calls))
    def test_existing_run_prevents_import_and_is_not_cancelled(self):
        api=FakeAPI(busy=True)
        with patch.object(b.time,'monotonic',side_effect=[0,1801]),self.assertRaises(b.Stop):
            with b.CollectorPause(api,{},lambda:None): self.fail('Must not import while busy')
        self.assertEqual(api.state,'active')
        self.assertFalse(any('cancel' in x[2] for x in api.calls))
    def test_unexpected_external_enable_blocks_more_writes(self):
        api=FakeAPI()
        with patch.object(b.time,'sleep'),self.assertRaises(b.Stop):
            with b.CollectorPause(api,{},lambda:None) as guard:
                api.state='active';guard.check()
        self.assertEqual(api.state,'active')
    def test_restore_failure_is_visible_not_success(self):
        api=FakeAPI();report={};api.fail_enable=True
        with patch.object(b.time,'sleep'),self.assertRaises(b.Stop):
            with b.CollectorPause(api,report,lambda:None): pass
        self.assertEqual(report['collector_maintenance']['status'],'resume-not-verified')
    def test_api_whitelist_refuses_legacy_workflow_and_other_branch(self):
        api=b.GitHubAPI('not-a-real-test-token')
        with self.assertRaises(b.Stop): api.call('PUT','collect-ticket-prices.yml','/disable')
        with self.assertRaises(b.Stop): api.call('POST',b.COLLECT_WORKFLOW,'/dispatches',{'ref':'other'})
        with self.assertRaises(b.Stop): b.GitHubAPI('Bearer token')

if __name__=='__main__': unittest.main(verbosity=2)
