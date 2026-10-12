"""Real child-process regressions for the capture/checkpoint shutdown boundary."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tools.run_bounded_capture import verify_stopped


PROJECT = Path(__file__).resolve().parents[1]
WRITER = '''import os, signal, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text(str(os.getpid()))
while True:
    with Path(sys.argv[2]).open('a') as output: output.write('x')
    time.sleep(0.01)
'''


@unittest.skipUnless(os.name == 'posix', 'The GitHub capture runner is POSIX')
class BoundedCaptureTests(unittest.TestCase):
    def command(self, root, code, *, seconds=0.5):
        return [sys.executable, '-m', 'tools.run_bounded_capture', 'run',
                '--seconds', str(seconds), '--grace-seconds', '0.1', '--settle-seconds', '2',
                '--marker', str(root/'stopped.json'), '--', sys.executable, '-c', code,
                str(root/'writer.pid'), str(root/'writes')]

    def assert_stopped(self, root):
        marker=verify_stopped(root/'stopped.json')
        self.assertTrue(marker['processes_stopped'])
        writes=root/'writes'
        before=writes.read_bytes() if writes.exists() else b''
        time.sleep(0.15)
        self.assertEqual(writes.read_bytes() if writes.exists() else b'',before)
        if sys.platform == 'linux' and (root/'writer.pid').exists():
            with self.assertRaises(ProcessLookupError):
                os.kill(int((root/'writer.pid').read_text()),0)
        return marker

    def test_normal_success_and_failure_keep_their_exit_status_after_shutdown(self):
        for code in (0,17):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                result=subprocess.run(self.command(root,f'import sys; sys.exit({code})'),
                                      cwd=PROJECT,capture_output=True,text=True,timeout=5)
                self.assertEqual(result.returncode,code,result.stderr)
                marker=self.assert_stopped(root)
                self.assertEqual(marker['exit_code'],code)
                self.assertFalse(marker['timed_out'])

    def test_deadline_kills_a_term_ignoring_writer_before_checkpoint_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            result=subprocess.run(self.command(root,WRITER),cwd=PROJECT,
                                  capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,124,result.stderr)
            marker=self.assert_stopped(root)
            self.assertTrue(marker['timed_out'])
            self.assertTrue(marker['killed'])

    def test_external_sigterm_and_sigint_also_stop_the_writer(self):
        for signum in (signal.SIGTERM,signal.SIGINT):
            with self.subTest(signal=signum), tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                process=subprocess.Popen(self.command(root,WRITER,seconds=10),cwd=PROJECT,
                                         stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                deadline=time.monotonic()+3
                while not (root/'writer.pid').exists() and time.monotonic()<deadline:
                    time.sleep(0.01)
                self.assertTrue((root/'writer.pid').exists())
                process.send_signal(signum)
                _stdout,stderr=process.communicate(timeout=5)
                self.assertEqual(process.returncode,128+signum,stderr)
                marker=self.assert_stopped(root)
                self.assertEqual(marker['received_signal'],signum)

    @unittest.skipUnless(sys.platform == 'linux', 'Orphan adoption uses the Ubuntu subreaper')
    def test_stubborn_grandchild_cannot_survive_a_leader_that_exits_on_term(self):
        self.grandchild(early=False,detached=False)

    @unittest.skipUnless(sys.platform == 'linux', 'Orphan adoption uses the Ubuntu subreaper')
    def test_successful_leader_cannot_leave_a_late_writer(self):
        self.grandchild(early=True,detached=False)

    @unittest.skipUnless(sys.platform == 'linux', 'Orphan adoption uses the Ubuntu subreaper')
    def test_detached_grandchild_is_adopted_killed_and_reaped(self):
        self.grandchild(early=True,detached=True)

    def grandchild(self, *, early, detached):
        leader=f'''import signal, subprocess, sys, time
from pathlib import Path
subprocess.Popen([sys.executable,'-c',{WRITER!r},*sys.argv[1:]],start_new_session={detached!r})
while not Path(sys.argv[1]).exists(): time.sleep(0.01)
signal.signal(signal.SIGTERM,lambda *_: sys.exit(0))
if {early!r}: sys.exit(0)
time.sleep(30)
'''
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            result=subprocess.run(self.command(root,leader),cwd=PROJECT,
                                  capture_output=True,text=True,timeout=6)
            self.assertEqual(result.returncode,0 if early else 124,result.stderr)
            marker=self.assert_stopped(root)
            self.assertTrue(marker['killed'])

    def test_missing_stale_and_live_group_markers_cannot_allow_checkpointing(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with self.assertRaises(RuntimeError):verify_stopped(root/'stopped.json')
            result=subprocess.run(self.command(root,'pass'),cwd=PROJECT,
                                  capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            with patch.dict(os.environ,{'GITHUB_RUN_ID':'another-run'}), self.assertRaises(RuntimeError):
                verify_stopped(root/'stopped.json')
            marker=json.loads((root/'stopped.json').read_text())
            marker['process_group']=os.getpgrp()
            (root/'stopped.json').write_text(json.dumps(marker))
            with self.assertRaises(RuntimeError):verify_stopped(root/'stopped.json')

    def test_launch_failure_removes_any_previous_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            marker=root/'stopped.json';marker.write_text('{"processes_stopped":true}')
            command=self.command(root,'pass')
            command[command.index('--')+1]='/nonexistent/capture-command'
            result=subprocess.run(command,cwd=PROJECT,capture_output=True,text=True,timeout=5)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse(marker.exists())


class BoundedCaptureWorkflowTests(unittest.TestCase):
    def test_both_sports_finish_shutdown_before_shared_and_raw_queue_checkpoints(self):
        import yaml
        workflow=yaml.load((PROJECT/'.github/workflows/collect-ticket-prices.yml').read_text(),Loader=yaml.BaseLoader)
        for sport in ('nfl','nhl'):
            with self.subTest(sport=sport):
                steps=workflow['jobs']['collect-'+sport]['steps']
                capture=next(step for step in steps if step.get('id')=='shared-capture')
                stopped=next(step for step in steps if step.get('id')=='shared-stopped')
                self.assertEqual(capture['timeout-minutes'],'26')
                self.assertIn('tools.run_bounded_capture run --seconds 1500',capture['run'])
                self.assertIn(f'"$RUNNER_TEMP/{sport}-capture-stopped.json"',capture['run'])
                self.assertIn('tools.run_bounded_capture verify',stopped['run'])
                self.assertIn('always()',stopped['if'])
                for key in ('shared-budget','pending-budget'):
                    budget=next(step for step in steps if step.get('id')==key)
                    self.assertLess(steps.index(stopped),steps.index(budget))
                    self.assertIn("steps.shared-stopped.outcome == 'success'",budget['if'])
                    self.assertIn('inputs.delivery_only',budget['if'])


if __name__=='__main__':unittest.main()
