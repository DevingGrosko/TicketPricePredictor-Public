"""No external requests or account mutations in these tests."""
from datetime import datetime, timezone
from functools import partial
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from tools.free_refresh_cycle import due, run
from tools.free_refresh_storage import MB, check_artifact_budget, stale_caches, remove_current
from tools.check_free_pages import PREFIX, ProjectHandler


class OrchestrationTests(unittest.TestCase):
    def test_preserves_adaptive_league_evaluation(self):
        first=datetime(2026,9,26,12,17,tzinfo=timezone.utc)
        second=first.replace(minute=47)
        for sport in ('mlb','nfl','nhl'):
            self.assertTrue(due(sport,first))
            self.assertTrue(due(sport,second))
            self.assertTrue(due(sport,second,True))

    def test_independent_nhl_schedule_does_not_take_production_recovery_skip(self):
        import nhl_schedule_collector as nhl
        original=nhl.nhl_should_skip_for_trigger
        def capture(sport,directory):
            self.assertEqual(sport,'nhl')
            self.assertFalse(nhl.nhl_should_skip_for_trigger('schedule'))
            return 0
        with tempfile.TemporaryDirectory() as directory, patch('tools.free_refresh_capture.capture',side_effect=capture):
            self.assertEqual(run('nhl',directory,force=True),0)
        self.assertIs(nhl.nhl_should_skip_for_trigger,original)
        self.assertTrue(nhl.nhl_should_skip_for_trigger('schedule'))

    def test_storage_stops_before_oversized_upload(self):
        check_artifact_budget(300*MB,100*MB)
        for existing,new in ((0,129*MB),(400*MB,100*MB),(-1,1)):
            with self.assertRaises(RuntimeError):check_artifact_budget(existing,new)

    def test_cache_cleanup_never_selects_production_or_other_branch(self):
        rows=[{'id':i,'key':f'ticketsignal-free-v1-raw-{i}-1','ref':'refs/heads/main','created_at':f'2026-09-26T00:0{i}:00Z'} for i in range(1,5)]
        rows += [{'id':90,'key':'collector-pending-90','ref':'refs/heads/main'},
                 {'id':91,'key':'ticketsignal-free-v1-raw-91-1','ref':'refs/heads/another-branch'},
                 {'id':92,'key':'ticketsignal-free-v1-raw-unexpected','ref':'refs/heads/main'}]
        self.assertEqual({r['id'] for r in stale_caches(rows)},{1,2})

    def test_artifact_cleanup_requires_exact_current_run_and_name(self):
        for artifact in ({'name':'collector-report','workflow_run':{'id':1}},
                         {'name':'ticketsignal-free-pages','workflow_run':{'id':2}}):
            with patch.dict('os.environ',{'GITHUB_RUN_ID':'1'}),patch('tools.free_refresh_storage.api',return_value=artifact) as api:
                with self.assertRaises(RuntimeError):remove_current(5)
                self.assertEqual(api.call_count,1)

    def test_directory_redirect_retains_project_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'graph').mkdir();(root/'graph/index.html').write_text('graph')
            server=ThreadingHTTPServer(('127.0.0.1',0),partial(ProjectHandler,directory=directory))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                client=HTTPConnection('127.0.0.1',server.server_port)
                client.request('GET',PREFIX+'/graph?event=1');response=client.getresponse()
                self.assertEqual(response.status,301)
                self.assertEqual(response.getheader('Location'),PREFIX+'/graph/?event=1')
                response.read();client.close()
                client=HTTPConnection('127.0.0.1',server.server_port)
                client.request('GET','/graph/');response=client.getresponse()
                self.assertEqual(response.status,404);response.read();client.close()
            finally:
                server.shutdown();server.server_close();thread.join(timeout=5)


if __name__=='__main__':unittest.main()
