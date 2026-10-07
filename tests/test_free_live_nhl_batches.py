"""NHL recovery cannot hold current observations behind an exhaustive backlog."""
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from tools import free_live_hardening as live
from tools.free_live_collect import read_json, write_json


class NHLBatchTests(unittest.TestCase):
    @contextmanager
    def fixture(self, current_count=1, recovery_count=41):
        import nhl_schedule_collector as nhl
        initial = datetime(2026, 10, 7, 2, 35, tzinfo=timezone.utc)
        class Clock(datetime):
            current = initial
            @classmethod
            def now(cls, tz=None):
                return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

        def game(identity, lead):
            return nhl.ScheduledNHLGame(identity, initial + lead, 'Toronto Maple Leafs',
                'Montreal Canadiens', 'Bell Centre', identity)
        current = [game(f'current-{i:03}', timedelta(hours=8)) for i in range(current_count)]
        recovery = []
        # Slow games are outside their real phases in both tested slots.
        next_slot = nhl.half_hour_capture_slot(initial + timedelta(minutes=30))
        for i in range(1000):
            candidate = game(f'recovery-{i:03}', timedelta(days=20))
            if not nhl.nhl_capture_is_due(candidate.event_date, next_slot, candidate.schedule_id):
                recovery.append(candidate)
            if len(recovery) == recovery_count:
                break
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            pending = root / 'pending'
            pending.mkdir()
            saved = pending / 'saved-observation.json'
            saved.write_text('{"original_capture":"retained"}\n')
            backlog = {}
            for row in recovery:
                value = asdict(row)
                value['event_date'] = row.event_date.isoformat()
                backlog[row.schedule_id] = {'game': value,
                    'first_due': (initial - timedelta(hours=5)).isoformat()}
            write_json(root / 'nhl-progress.json', {'pending': backlog, 'completed': {},
                'last_slot': nhl.half_hour_capture_slot(initial).isoformat()})
            stack.enter_context(patch.object(live, 'datetime', Clock))
            stack.enter_context(patch.object(nhl, 'replay_pending_snapshots',
                return_value=(0, False, ['Saved observation is still awaiting delivery'])))
            stack.enter_context(patch.object(nhl, 'fetch_schedule_games',
                return_value=(current + recovery, ['fixture'])))
            stack.enter_context(patch.object(nhl, 'discover_nhl_games', return_value=([], [])))
            events, batches, captures, deliveries = [], [], [], []
            def resolve(rows, *args, **kwargs):
                identities = [row.schedule_id for row in rows]
                batches.append(identities)
                events.append(('resolve', identities))
                return [SimpleNamespace(game=row, candidates=[1]) for row in rows], []
            stack.enter_context(patch.object(nhl, 'resolve_schedule_games', side_effect=resolve))
            def capture(resolution, **kwargs):
                row = resolution.game
                captures.append(row.schedule_id)
                events.append(('capture', row.schedule_id))
                if row.schedule_id.startswith('recovery-'):
                    raise TimeoutError('Provider failure remains visible')
                return row.schedule_id, row.event_date, SimpleNamespace(venue='Bell Centre', sections=[1])
            stack.enter_context(patch.object(nhl, '_capture_resolution', side_effect=capture))
            stack.enter_context(patch.object(nhl, 'nhl_snapshot_to_payload',
                side_effect=lambda url, event_at, observed, snapshot, **kwargs:
                    {'id': url, 'captured_at': observed.isoformat()}))
            def queue(payload, directory):
                path = directory / (payload['id'] + '.json')
                path.write_text(json.dumps(payload))
                return path
            stack.enter_context(patch.object(nhl, 'queue_snapshot', side_effect=queue))
            def deliver(endpoint, token, payload):
                deliveries.append(payload)
                events.append(('deliver', payload['id']))
                return {'status': 'stored'}
            stack.enter_context(patch.object(nhl, 'post_snapshot_with_retry', side_effect=deliver))
            yield SimpleNamespace(root=root, saved=saved, clock=Clock, current=current, recovery=recovery,
                events=events, batches=batches, captures=captures, deliveries=deliveries,
                run=lambda: live.run_nhl('', '', True, 1, root / 'health.json', pending))

    def test_due_delivery_precedes_recovery_and_failed_batches_rotate_without_losing_state(self):
        with self.fixture() as fixture:
            current = fixture.current[0].schedule_id
            older = [row.schedule_id for row in fixture.recovery]
            self.assertEqual(fixture.run(), 1)
            self.assertEqual(fixture.batches, [[current], older[:20]])
            recovery_started = fixture.events.index(('resolve', older[:20]))
            self.assertIn(('deliver', current), fixture.events[:recovery_started])
            report = read_json(fixture.root / 'health.json')
            self.assertEqual((report['current_due'], report['current_due_selected'],
                report['recovery_selected'], report['deferred']), (1, 1, 20, 21))
            self.assertEqual((report['committed'], report['failed'], report['pending']), (1, 20, 1))
            state = read_json(fixture.root / 'nhl-progress.json')
            self.assertEqual(set(state['pending']), set(older))
            self.assertTrue(all(state['pending'][identity].get('last_attempt') for identity in older[:20]))
            self.assertTrue(all('last_attempt' not in state['pending'][identity] for identity in older[20:]))

            # A restart within this same slot honors the saved current-game
            # receipt while the next untouched recovery cohort gets a turn.
            fixture.batches.clear()
            self.assertEqual(fixture.run(), 1)
            self.assertEqual(fixture.batches, [older[20:40]])
            self.assertEqual(fixture.captures.count(current), 1)
            report = read_json(fixture.root / 'health.json')
            self.assertEqual((report['current_due'], report['current_due_selected'],
                report['already_committed'], report['recovery_selected'], report['deferred']), (1, 0, 1, 20, 21))
            self.assertEqual(set(read_json(fixture.root / 'nhl-progress.json')['pending']), set(older))
            self.assertEqual(fixture.saved.read_text(), '{"original_capture":"retained"}\n')

            # The next half-hour is a distinct observation. Previously untouched
            # recovery games still go before any already-failed recovery game.
            fixture.clock.current += timedelta(minutes=30)
            fixture.batches.clear()
            self.assertEqual(fixture.run(), 1)
            self.assertEqual(fixture.batches[0], [current])
            self.assertEqual(fixture.batches[1][0], older[40])
            self.assertEqual(fixture.captures.count(current), 2)
            self.assertEqual([payload['captured_at'] for payload in fixture.deliveries],
                ['2026-10-07T02:35:00+00:00', '2026-10-07T03:05:00+00:00'])
            self.assertEqual(read_json(fixture.root / 'nhl-progress.json')['completed'][current]['slot'],
                '2026-10-07T03:00:00+00:00')
            self.assertEqual(fixture.saved.read_text(), '{"original_capture":"retained"}\n')

    def test_all_current_due_games_are_selected_even_when_larger_than_recovery_limit(self):
        with self.fixture(current_count=22, recovery_count=21) as fixture:
            current = [row.schedule_id for row in fixture.current]
            older = [row.schedule_id for row in fixture.recovery]
            self.assertEqual(fixture.run(), 1)
            self.assertEqual(fixture.batches, [current, older[:20]])
            self.assertEqual([payload['id'] for payload in fixture.deliveries], current)
            report = read_json(fixture.root / 'health.json')
            self.assertEqual((report['current_due_selected'], report['recovery_selected'],
                report['scheduled_selected'], report['deferred']), (22, 20, 42, 1))
            self.assertEqual(report['deferred_games'], older[20:])
            self.assertEqual(report['unfinished_games'], sorted(older))
