"""Bot-dispatched owners explicitly publish after capture and mirror completion."""
from io import StringIO
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from tools.single_capture_owner import OWNER, REPO, publication_handoff_needed, run


def capture(sport, conclusion='success'):
    return dict(name='collect-'+sport, status='completed', conclusion=conclusion, steps=[
        dict(name=f'Attempt shared {sport.upper()} capture for the half-hour', status='completed', conclusion=conclusion)])


def mirror(sport, conclusion='success'):
    return dict(name=f'mirror-{sport}-staging / mirror', status='completed', conclusion=conclusion, steps=[
        dict(name='Deliver original observations without contacting Vivid', status='completed', conclusion=conclusion)])


def owner():
    return dict(id=10, path=OWNER, head_branch='main', status='in_progress',
        actor={'login':'github-actions[bot]'}, triggering_actor={'login':'github-actions[bot]'},
        repository={'full_name':REPO, 'private':False}, head_repository={'full_name':REPO})


ENV = dict(GITHUB_REPOSITORY=REPO, GITHUB_REF='refs/heads/main', GITHUB_RUN_ID='10',
           GITHUB_EVENT_NAME='workflow_dispatch', DISPATCH_SOURCE='github_free_backup',
           GITHUB_ACTOR='github-actions[bot]', GITHUB_TRIGGERING_ACTOR='github-actions[bot]')


class PublicationHandoffTests(unittest.TestCase):
    def test_failed_and_partial_attempts_publish_while_parent_end_job_is_active(self):
        for conclusions in (('success','success'), ('failure','success'), ('failure','failure')):
            with self.subTest(conclusions=conclusions):
                calls=[]
                jobs=[capture('nfl', conclusions[0]), capture('nhl', conclusions[1]), mirror('nfl'), mirror('nhl','failure')]
                def api(path, **kwargs):
                    calls.append((path,kwargs))
                    if path == '/actions/runs/10': return owner()
                    if '/jobs?' in path: return {'total_count':len(jobs), 'jobs':jobs}
                with patch.dict(os.environ,ENV,clear=True), patch('tools.single_capture_owner.api',api), patch('sys.stdout',StringIO()):
                    report=run('publication-handoff')
                self.assertTrue(report['dispatched']);self.assertEqual(report['owner_run_id'],10)
                self.assertEqual(calls[-1],('/actions/workflows/free-ticket-site.yml/dispatches',
                    {'payload':{'ref':'main','inputs':{'owner_run_id':'10'}}}))
                self.assertEqual(len(calls),3)

    def test_proven_duplicate_without_delivery_never_dispatches(self):
        jobs=[capture('nfl','skipped'),capture('nhl','skipped'),mirror('nfl','skipped'),mirror('nhl','skipped')]
        with patch.dict(os.environ,ENV,clear=True), \
             patch('tools.single_capture_owner.api',side_effect=[owner(), {'total_count':len(jobs),'jobs':jobs}]) as api, \
             patch('sys.stdout',StringIO()):
            report=run('publication-handoff')
        self.assertFalse(report['dispatched']);self.assertEqual(api.call_count,2)
        self.assertEqual(report['reason'],'both-captures-skipped-and-no-delivery-attempt')

    def test_actual_saved_delivery_can_publish_without_a_new_capture(self):
        jobs=[capture('nfl','skipped'),capture('nhl','skipped'),mirror('nfl'),mirror('nhl','skipped')]
        self.assertTrue(publication_handoff_needed(jobs)[0])

    def test_running_missing_or_unknown_relevant_job_cannot_publish_prematurely(self):
        good=[capture('nfl'),capture('nhl'),mirror('nfl'),mirror('nhl')]
        for bad in ([good[0],good[2],good[3]], good[:3],
                    [{**good[0],'status':'in_progress'},*good[1:]],
                    [{**good[0],'steps':[]},*good[1:]],
                    [{**good[0],'steps':[{**good[0]['steps'][0],'conclusion':None}]},*good[1:]],
                    [*good[:3],{**good[3],'status':'in_progress'}]):
            with self.subTest(jobs=bad), self.assertRaises(RuntimeError):
                publication_handoff_needed(bad)
        fake=[*good[:3],{**good[3],'name':'mirror-nhl-staging-spoof / mirror'}]
        with self.assertRaises(RuntimeError): publication_handoff_needed(fake)

    def test_wrong_event_source_branch_actor_or_repository_stops_before_api(self):
        for change in ({'GITHUB_REF':'refs/heads/feature'}, {'GITHUB_REPOSITORY':'other/repo'},
                       {'GITHUB_EVENT_NAME':'push'}, {'DISPATCH_SOURCE':'pythonanywhere_scheduler'},
                       {'GITHUB_ACTOR':'human'}, {'GITHUB_TRIGGERING_ACTOR':'human'}):
            with self.subTest(change=change),patch.dict(os.environ,{**ENV,**change},clear=True), \
                 patch('tools.single_capture_owner.api') as api,self.assertRaises(RuntimeError):
                run('publication-handoff')
            api.assert_not_called()

    def test_spoofed_owner_metadata_or_other_owner_id_cannot_dispatch(self):
        changes=({'id':11},{'head_branch':'feature'},{'path':'.github/workflows/other.yml'},
                 {'repository':{'full_name':REPO,'private':True}},
                 {'repository':{'full_name':'other/repo','private':False}},
                 {'head_repository':{'full_name':'other/repo'}},{'actor':{'login':'human'}},
                 {'triggering_actor':{'login':'human'}})
        for change in changes:
            with self.subTest(change=change),patch.dict(os.environ,ENV,clear=True), \
                 patch('tools.single_capture_owner.api',return_value={**owner(),**change}) as api, \
                 self.assertRaises(RuntimeError):run('publication-handoff')
            self.assertEqual(api.call_count,1)
        with patch.dict(os.environ,ENV,clear=True),patch('tools.single_capture_owner.api') as api, \
             self.assertRaises(RuntimeError): run('publication-handoff',owner_run_id=11)
        api.assert_not_called()

    def test_dispatch_failure_is_visible_and_has_no_fallback_dispatch_loop(self):
        jobs=[capture('nfl'),capture('nhl'),mirror('nfl'),mirror('nhl')]
        with patch.dict(os.environ,ENV,clear=True),patch('tools.single_capture_owner.api',
                side_effect=[owner(),{'total_count':4,'jobs':jobs},TimeoutError('dispatch failed')]) as api, \
             self.assertRaises(TimeoutError): run('publication-handoff')
        self.assertEqual(api.call_count,3)

    def test_workflow_handoff_is_narrow_and_publisher_does_not_restart_recovery(self):
        import yaml
        project=Path(__file__).resolve().parents[1]
        caller=yaml.load((project/'.github/workflows/collect-ticket-prices.yml').read_text(),Loader=yaml.BaseLoader)
        handoff=caller['jobs']['publish-backup-capture']
        for guard in ("always()", "github.ref == 'refs/heads/main'", "github.event.repository.private == false",
                      "github.event_name == 'workflow_dispatch'", "inputs.dispatch_source == 'github_free_backup'",
                      "github.actor == 'github-actions[bot]'", "github.triggering_actor == 'github-actions[bot]'"):
            self.assertIn(guard,handoff['if'])
        self.assertIn("needs.collect-nfl.outputs.mirror == 'true' || needs.collect-nhl.outputs.mirror == 'true'",handoff['if'])
        self.assertEqual(handoff['needs'],['collect-nfl','collect-nhl','mirror-nfl','mirror-nhl'])
        self.assertEqual(handoff['permissions'],{'contents':'read','actions':'write'})
        command=handoff['steps'][-1]
        self.assertEqual(command['run'],'python -m tools.single_capture_owner publication-handoff')
        self.assertEqual(command['env']['GH_TOKEN'],'${{ github.token }}')
        self.assertNotIn('secrets',str(handoff))
        publisher=yaml.load((project/'.github/workflows/free-ticket-site.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(publisher['on']['workflow_dispatch']['inputs']['owner_run_id']['default'],'')
        steps=publisher['jobs']['ready']['steps']
        decision=next(step for step in steps if step.get('id')=='decision')
        self.assertEqual(decision['env']['OWNER_RUN_ID'],'${{ github.event.workflow_run.id || inputs.owner_run_id }}')
        self.assertIn('|| -n "$OWNER_RUN_ID"',decision['run'])
        watchdog=next(step for step in steps if step.get('name','').startswith('Recover a missed'))
        self.assertIn('!inputs.owner_run_id',watchdog['if'])


if __name__=='__main__':unittest.main()
