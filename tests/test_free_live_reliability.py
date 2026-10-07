"""Offline failure injection: successful snapshots survive unrelated failures."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import event
from test_free_refresh_capture import NOW, StorageTests, payload
from tools.free_refresh_capture import store_payload
from tools.free_live_collect import run_parallel_nfl, run, read_json
from tools.free_live_storage import removable_cache_rows


class BulkTests(StorageTests):
    def test_one_batch_not_one_insert_per_section(self):
        for sport in ('mlb', 'nfl', 'nhl'):
            engine, _, _, Ticket = self.setup_db(sport)
            inserts = []
            def record(_conn, _cursor, statement, _parameters, _context, many):
                if statement.startswith('INSERT INTO ' + Ticket.__tablename__ + ' '):
                    inserts.append(many)
            event.listen(engine, 'before_cursor_execute', record)
            store_payload(engine, sport, payload(sport), now=NOW)
            self.assertEqual(inserts, [True])


class ParallelTests(unittest.TestCase):
    def fixture(self, directory, count=3):
        import nfl_schedule_collector as nfl
        stack = ExitStack()
        self.addCleanup(stack.close)
        games = [nfl.ScheduledNFLGame(str(i), datetime.now(timezone.utc) + timedelta(hours=8),
                    'Dallas Cowboys', 'New York Giants', 'MetLife Stadium', 'Game') for i in range(count)]
        resolutions = [SimpleNamespace(game=g, candidates=['valid'], source='test') for g in games]
        stack.enter_context(patch.object(nfl, 'replay_pending_snapshots', return_value=(0, True, [])))
        stack.enter_context(patch.object(nfl, 'fetch_schedule_games', return_value=(games, 'test')))
        stack.enter_context(patch.object(nfl, 'schedule_games_due', side_effect=lambda items, slot: items))
        stack.enter_context(patch.object(nfl, 'discover_nfl_games', return_value=([], [])))
        stack.enter_context(patch.object(nfl, 'resolve_schedule_games', side_effect=lambda items, *a, **k:
            ([r for r in resolutions if r.game in items], [])))
        stack.enter_context(patch.object(nfl, 'nfl_snapshot_to_payload', side_effect=lambda url, at, slot, snapshot, **k:
            {'source_url': url, 'id': snapshot.source_id}))
        def queue(value, pending):
            pending.mkdir(parents=True, exist_ok=True)
            path = pending/(value['id'] + '.json')
            path.write_text(json.dumps(value))
            return path
        stack.enter_context(patch.object(nfl, 'queue_snapshot', side_effect=queue))
        def captured(resolution, *args):
            return ('https://example.invalid/' + resolution.game.schedule_id,
                resolution.game.event_date, SimpleNamespace(venue='MetLife Stadium',
                    source_id=resolution.game.schedule_id, sections=[1, 2]), 0.01)
        return nfl, stack, games, captured

    def test_fast_success_commits_before_slow_failure_and_next_game_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            nfl, stack, games, captured = self.fixture(directory)
            delivered = []
            first_started = threading.Event()
            fast_committed = threading.Event()
            main_thread = threading.get_ident()
            def capture(resolution, *args):
                sid = resolution.game.schedule_id
                if sid == '0':
                    first_started.set()
                    if not fast_committed.wait(3):
                        raise AssertionError('Successful game blocked behind slow game')
                    raise TimeoutError('injected provider timeout')
                if not first_started.wait(3):
                    raise AssertionError('Workers did not run in parallel')
                return captured(resolution)
            def store(_endpoint, _token, value):
                self.assertEqual(threading.get_ident(), main_thread)
                delivered.append(value['id'])
                fast_committed.set()
                return {'status': 'stored', 'iteration_id': 10 + int(value['id'])}
            stack.enter_context(patch('tools.free_live_collect.capture_one', side_effect=capture))
            stack.enter_context(patch.object(nfl, 'post_snapshot_with_retry', side_effect=store))
            root = Path(directory)
            code = run_parallel_nfl('unused', 'unused', True, 1, root/'health.json', root/'pending')
            report = read_json(root/'health.json')
            self.assertEqual(code, 1)
            self.assertEqual(set(delivered), {'1', '2'})
            self.assertTrue(fast_committed.is_set())
            self.assertEqual((report['committed'], report['failed'], report['pending']), (2, 1, 0))
            self.assertEqual(report['status'], 'degraded')
            self.assertEqual(report['workers'], 2)

    def test_one_failed_write_is_queued_but_does_not_disable_other_deliveries(self):
        with tempfile.TemporaryDirectory() as directory:
            nfl, stack, games, captured = self.fixture(directory)
            stack.enter_context(patch('tools.free_live_collect.capture_one', side_effect=captured))
            def store(_endpoint, _token, value):
                if value['id'] == '1':
                    raise RuntimeError('temporary database error')
                return {'status': 'stored', 'iteration_id': 10 + int(value['id'])}
            stack.enter_context(patch.object(nfl, 'post_snapshot_with_retry', side_effect=store))
            root = Path(directory)
            self.assertEqual(run_parallel_nfl('', '', True, 1, root/'health.json', root/'pending'), 1)
            report = read_json(root/'health.json')
            self.assertEqual((report['captured'], report['committed'], report['pending']), (3, 2, 1))
            self.assertTrue((root/'pending/1.json').exists())

    def test_receipts_avoid_recapture_in_same_half_hour_but_allow_next_half_hour(self):
        with tempfile.TemporaryDirectory() as directory:
            nfl, stack, games, captured = self.fixture(directory, 1)
            initial = datetime.now(timezone.utc).replace(minute=5, second=0, microsecond=0)
            class Clock(datetime):
                current = initial
                @classmethod
                def now(cls, tz=None):
                    return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)
            stack.enter_context(patch('tools.free_live_collect.datetime', Clock))
            capture = stack.enter_context(patch('tools.free_live_collect.capture_one', side_effect=captured))
            stack.enter_context(patch.object(nfl, 'post_snapshot_with_retry', return_value={'status': 'duplicate', 'iteration_id': 11}))
            root = Path(directory)
            args = ('', '', True, 1, root/'health.json', root/'pending')
            self.assertEqual(run_parallel_nfl(*args), 0)
            self.assertEqual(run_parallel_nfl(*args), 0)
            self.assertEqual(capture.call_count, 1)
            self.assertEqual(read_json(root/'health.json')['already_committed'], 1)
            Clock.current += timedelta(minutes=30)
            self.assertEqual(run_parallel_nfl(*args), 0)
            self.assertEqual(capture.call_count, 2)
            self.assertEqual(read_json(root/'health.json')['already_committed'], 0)
            self.assertEqual(run_parallel_nfl(*args), 0)
            self.assertEqual(capture.call_count, 2)

    def test_delayed_cron_is_not_skipped_by_minute(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('tools.free_refresh_cycle.run', return_value=1) as capture:
                self.assertEqual(run('nfl', directory), 1)
                self.assertEqual(capture.call_args.kwargs, {'force': True})

    def test_worker_count_is_bounded(self):
        for workers in (0, 3, 100):
            with self.assertRaises(ValueError):
                run_parallel_nfl('', '', True, 1, Path('unused'), Path('unused'), workers=workers)


class WorkflowTests(unittest.TestCase):
    def test_publisher_does_not_depend_on_capture_success(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        publish = yaml.load((root/'docs/free-live-publish.yml').read_text(), Loader=yaml.BaseLoader)
        collect = yaml.load((root/'docs/free-live-collect.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertNotIn('capture', publish['jobs'])
        self.assertEqual(publish['jobs']['build']['needs'], 'ready')
        self.assertEqual(publish['on']['workflow_run']['types'], ['completed'])
        self.assertEqual(publish['on']['schedule'][0]['cron'], '17,47 * * * *')
        self.assertNotIn('conclusion', publish['jobs']['ready']['if'])
        self.assertEqual(collect['jobs']['capture']['strategy']['fail-fast'], 'false')
        for flow in (publish, collect):
            for job in flow['jobs'].values():
                self.assertEqual(job['runs-on'], 'ubuntu-latest')
        self.assertNotIn('continue-on-error', str(collect))

    def test_cleanup_only_own_completed_workflows_and_keeps_recovery_copies(self):
        rows = [{'id': i, 'key': f'ticketsignal-free-v1-state-nfl-{i}-1',
                 'ref': 'refs/heads/main', 'created_at': str(i)} for i in range(1, 5)]
        rows += [{'id': 91, 'key': 'collector-pending-1', 'ref': 'refs/heads/main'}]
        def runs(i):
            return {'path': '.github/workflows/free-ticket-collect.yml', 'status': 'completed' if i != 2 else 'in_progress'}
        self.assertEqual([r['id'] for r in removable_cache_rows(rows, runs, 4)], [1])
        self.assertEqual(removable_cache_rows(rows, lambda i: {'path': 'production.yml', 'status':'completed'}, 4), [])
