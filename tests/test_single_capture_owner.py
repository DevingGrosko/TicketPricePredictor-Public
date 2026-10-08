"""Offline clock/owner tests; no dispatches or provider requests."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from tools.single_capture_owner import OWNER, REPO, owner_runs, publication_needed, run, slot_decision

NOW = datetime(2026,10,8,4,47,tzinfo=timezone.utc)


def owner(identity, status='completed', created=NOW-timedelta(minutes=39), **kwargs):
    return dict(id=identity, path=OWNER, head_branch='main', status=status, created_at=created.isoformat(), **kwargs)


def job(sport, conclusion='failure', started=NOW-timedelta(minutes=9), label=None):
    return {'name':'collect-'+sport,'status':'completed','conclusion':conclusion,
            'steps':[{'name':label or f'Attempt shared {sport.upper()} capture for the half-hour',
                      'status':'completed','conclusion':conclusion,'started_at':started.isoformat()}]}


class SingleOwnerTests(unittest.TestCase):
    def decide(self, rows, jobs, **kwargs):
        return slot_decision(rows, lambda identity:jobs.get(identity,[]), NOW, **kwargs)

    def test_failed_finished_attempts_block_both_backup_clocks_in_same_half_hour(self):
        rows=[owner(1)]
        jobs={1:[job('nfl'),job('nhl','success')]}
        decision,reason,evidence=self.decide(rows,jobs)
        self.assertFalse(decision)
        self.assertEqual(reason,'slot-already-attempted-including-failure')
        self.assertEqual({item['conclusion'] for item in evidence['attempts']},{'failure','success'})
        self.assertEqual(evidence['slot'],'2026-10-08T04:30:00+00:00')
        later=NOW+timedelta(minutes=15)
        self.assertTrue(slot_decision(rows,lambda _:jobs[1],later)[0])

    def test_partial_sport_attempt_dispatches_missing_sport_without_repeating_failed_nfl(self):
        rows=[owner(1)];jobs={1:[job('nfl')]}
        self.assertTrue(self.decide(rows,jobs)[0])
        self.assertFalse(self.decide(rows,jobs,sport='nfl')[0])
        self.assertTrue(self.decide(rows,jobs,sport='nhl')[0])

    def test_capture_step_start_not_run_creation_defines_queued_slot(self):
        rows=[owner(1,created=NOW-timedelta(hours=2))]
        self.assertFalse(self.decide(rows,{1:[job('nfl')]},sport='nfl')[0])
        self.assertTrue(self.decide(rows,{1:[job('nfl',started=NOW-timedelta(minutes=25))]},sport='nfl')[0])

    def test_skipped_setup_failure_and_delivery_only_do_not_consume_capture_slot(self):
        for label,conclusion in [('Attempt shared NFL capture for the half-hour','skipped'),
                                  ('Pilot one NFL capture owner or replay the verified saved observations','success'),
                                  ('Deliver saved NFL observations to both stores','success')]:
            with self.subTest(label=label):
                self.assertTrue(self.decide([owner(1)],{1:[job('nfl',conclusion,label=label)]},sport='nfl')[0])
        self.assertTrue(self.decide([owner(1)],{1:[{'name':'collect-nfl','status':'completed','conclusion':'failure','steps':[]}]},sport='nfl')[0])
        for conclusion in ('cancelled','timed_out'):
            self.assertFalse(self.decide([owner(1)],{1:[job('nfl',conclusion)]},sport='nfl')[0])
        legacy = job('nhl',label='Collect due NHL games across the adaptive seven-day window')
        self.assertFalse(self.decide([owner(1)],{1:[legacy]},sport='nhl')[0])

    def test_publication_skips_only_successful_duplicate_without_delivery(self):
        completed = {**owner(1),'conclusion':'success'}
        skipped = [job('nfl','skipped'),job('nhl','skipped')]
        self.assertFalse(publication_needed(completed,skipped)[0])
        mirror = {'name':'mirror-nfl-staging / mirror','status':'completed','conclusion':'skipped','steps':[]}
        self.assertFalse(publication_needed(completed,skipped+[mirror])[0])
        mirror['conclusion']='success'
        mirror['steps']=[{'name':'Deliver original observations without contacting Vivid','status':'completed','conclusion':'success'}]
        self.assertTrue(publication_needed(completed,skipped+[mirror])[0])
        replay = {**job('nfl','success'),'steps':[{'name':'Deliver saved NFL observations to both stores','status':'completed','conclusion':'success'}]}
        self.assertTrue(publication_needed(completed,skipped+[replay])[0])

    def test_publication_preserves_capture_failure_and_unknown_or_failed_owner_mirror(self):
        completed = {**owner(1),'conclusion':'success'}
        skipped = [job('nfl','skipped'),job('nhl','skipped')]
        self.assertTrue(publication_needed(completed,[job('nfl'),skipped[1]])[0])
        for conclusion in ('failure','cancelled',None):
            self.assertTrue(publication_needed({**completed,'conclusion':conclusion},skipped)[0])
        self.assertTrue(publication_needed(completed,[])[0])
        mirror={'name':'mirror-nhl-staging / mirror','status':'completed','conclusion':'failure','steps':[]}
        self.assertTrue(publication_needed(completed,skipped+[mirror])[0])

    def test_transient_publication_owner_or_jobs_lookup_publishes_existing_data(self):
        errors = (HTTPError('https://api.github.com/',503,'temporary',None,None),
                  URLError(TimeoutError('private timeout details')),TimeoutError('private timeout details'),
                  ConnectionResetError('private transport details'))
        for error in errors:
            for stage in ('owner','jobs'):
                with self.subTest(error=type(error).__name__,stage=stage), tempfile.TemporaryDirectory() as directory:
                    output=Path(directory)/'output'
                    responses=[error] if stage=='owner' else [{**owner(1),'conclusion':'success'},error]
                    with patch.dict(os.environ,{'GITHUB_REPOSITORY':REPO,'GITHUB_REF':'refs/heads/main','GITHUB_OUTPUT':str(output)}), \
                         patch('tools.single_capture_owner.api',side_effect=responses):
                        report=run('publication-gate',owner_run_id=1)
                    self.assertTrue(report['run'])
                    self.assertEqual(report['reason'],'publication-lookup-temporarily-unavailable')
                    self.assertEqual(output.read_text(),'run=true\n')
                    self.assertNotIn('private',json.dumps(report))

    def test_publication_lookup_auth_not_found_wrong_scope_and_wrong_owner_remain_fail_closed(self):
        for code in (401,403,404,422):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                output=Path(directory)/'output'
                with patch.dict(os.environ,{'GITHUB_REPOSITORY':REPO,'GITHUB_REF':'refs/heads/main','GITHUB_OUTPUT':str(output)}), \
                     patch('tools.single_capture_owner.api',side_effect=HTTPError('https://api.github.com/',code,'denied',None,None)), \
                     self.assertRaises(HTTPError):
                    run('publication-gate',owner_run_id=1)
                self.assertFalse(output.exists())
        for change in ({'path':'.github/workflows/other.yml'},{'head_branch':'feature'},
                       {'head_repository':{'full_name':'another/repo'}},{'repository':{'full_name':'another/repo'}}):
            with self.subTest(change=change),patch.dict(os.environ,{'GITHUB_REPOSITORY':REPO,'GITHUB_REF':'refs/heads/main'}), \
                 patch('tools.single_capture_owner.api',return_value={**owner(1),**change}),self.assertRaises(RuntimeError):
                run('publication-gate',owner_run_id=1)
        for change in ({'GITHUB_REPOSITORY':'another/repo'},{'GITHUB_REF':'refs/heads/feature'}):
            with self.subTest(change=change),patch.dict(os.environ,{'GITHUB_REPOSITORY':REPO,'GITHUB_REF':'refs/heads/main',**change}), \
                 patch('tools.single_capture_owner.api') as read,self.assertRaises(RuntimeError):
                run('publication-gate',owner_run_id=1)
            read.assert_not_called()

    def test_active_or_queued_owner_preserved_and_newer_follower_does_not_skip_current_owner(self):
        for status in ('queued','in_progress','waiting','pending','requested'):
            with self.subTest(status=status):
                self.assertFalse(self.decide([owner(1,status)],{})[0])
        rows=[owner(1,'in_progress'),owner(2,'queued',created=NOW)]
        self.assertTrue(self.decide(rows,{},current_run=1,sport='nfl')[0])
        self.assertFalse(self.decide(rows,{},current_run=2,sport='nfl')[0])
        self.assertTrue(self.decide(rows,{},current_run=2,sport='nfl',manual_repair=True)[0])

    def test_finished_nfl_in_live_nhl_run_has_independent_slot_evidence(self):
        rows=[owner(1,'in_progress')]
        jobs={1:[job('nfl'),{'name':'collect-nhl','status':'in_progress'}]}
        self.assertFalse(self.decide(rows,jobs,sport='nfl')[0])
        self.assertFalse(self.decide(rows,jobs,sport='nhl')[0])
        jobs[1][0]=job('nfl',started=NOW-timedelta(minutes=25))
        self.assertTrue(self.decide(rows,jobs,sport='nfl')[0])

    def test_other_branches_workflows_and_forks_never_block(self):
        rows=[{**owner(1,'queued'),'head_branch':'feature'},
              {**owner(2,'queued'),'path':'.github/workflows/other.yml'},
              {**owner(3,'queued'),'head_repository':{'full_name':'some/fork'}}]
        self.assertTrue(self.decide(rows,{})[0])

    def test_active_inventory_includes_old_owner_and_excess_is_fail_closed(self):
        paths=[]
        def read(path):
            paths.append(path)
            return {'workflow_runs':[owner(1,'queued')] if 'status=queued' in path else [],'total_count':1}
        self.assertEqual([item['id'] for item in owner_runs(read)],[1])
        self.assertEqual(len(paths),6)
        with self.assertRaises(RuntimeError):
            owner_runs(lambda _: {'workflow_runs':[],'total_count':101})

    def test_backup_posts_only_to_fixed_owner_and_has_no_browser_store_calls(self):
        import tools.single_capture_owner as helper
        calls=[]
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return NOW
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ,{'GITHUB_REPOSITORY':REPO,'GITHUB_REF':'refs/heads/main','GITHUB_RUN_ID':'10','GITHUB_OUTPUT':str(Path(directory)/'out')}), \
             patch.object(helper,'datetime',Clock), patch.object(helper,'owner_runs',return_value=[]), \
             patch.object(helper,'api',side_effect=lambda path,**kwargs:calls.append((path,kwargs))):
            report=run('dispatch-backup')
        self.assertTrue(report['dispatched'])
        self.assertEqual(calls,[('/actions/workflows/collect-ticket-prices.yml/dispatches',
            {'payload':{'ref':'main','inputs':{'dispatch_source':'github_free_backup','shared_capture':True}}})])


if __name__=='__main__':unittest.main()
