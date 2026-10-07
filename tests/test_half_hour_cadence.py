"""Slot regression tests: preserve history and slower tiers across :00/:30."""
from datetime import datetime, timedelta, timezone
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
import tempfile
import unittest

from Flask_App.collection_cadence import half_hour_capture_slot
from collector import EventSnapshot, SectionSnapshot
import nfl_collector as nfl
import nhl_collector as nhl
from Flask_App.nfl_blueprint import CreateNFLModel, NFLIteration, NFLTicket, store_nfl_snapshot
from Flask_App.nhl_blueprint import CreateNHLModel, NHLIteration, NHLTicket, store_nhl_snapshot
from models import captured_datetime_for_storage


class HalfHourCadenceTests(unittest.TestCase):
    def test_slot_boundary_and_timezone_normalization(self):
        first = datetime(2026, 10, 6, 12, 29, 59, tzinfo=timezone.utc)
        second = first + timedelta(seconds=1)
        self.assertEqual(half_hour_capture_slot(first), first.replace(minute=0, second=0))
        self.assertEqual(half_hour_capture_slot(second), second)
        self.assertEqual(half_hour_capture_slot(second.astimezone(timezone(timedelta(hours=-4)))), second)

    def test_closest_games_are_due_in_both_halves_of_every_hour(self):
        start = datetime(2026, 10, 6, tzinfo=timezone.utc)
        for module, lead in ((nfl, 100), (nhl, 48)):
            due = getattr(module, module.__name__[:3] + '_capture_is_due')
            event = start + timedelta(hours=lead)
            for tick in range(48):
                with self.subTest(sport=module.__name__, tick=tick):
                    self.assertTrue(due(event, start + timedelta(minutes=30*tick), 'game'))
            self.assertFalse(due(event, event, 'game'))
            self.assertFalse(due(event, event + timedelta(minutes=30), 'game'))

    def test_longer_tiers_keep_their_existing_phase_and_intervals(self):
        start = datetime(2026, 10, 6, tzinfo=timezone.utc)
        for module, lead, interval in ((nfl, 250, 3), (nfl, 600, 6),
                                       (nhl, 150, 6), (nhl, 300, 12), (nhl, 600, 24)):
            sport = module.__name__[:3]
            due = getattr(module, sport + '_capture_is_due')
            phase = getattr(module, sport + '_capture_phase')('game', interval)
            actual = [start + timedelta(minutes=30*tick) for tick in range(48)
                      if due(start+timedelta(hours=lead), start+timedelta(minutes=30*tick), 'game')]
            expected = [start + timedelta(hours=hour) for hour in range(24)
                        if int((start+timedelta(hours=hour)).timestamp()//3600) % interval == phase]
            with self.subTest(sport=sport, interval=interval):
                self.assertEqual(actual, expected)

    def test_existing_hourly_history_and_new_half_hour_history_remain_idempotent(self):
        start = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
        for sport, model_cls, iteration, ticket, store in (
            ('nfl', CreateNFLModel, NFLIteration, NFLTicket, store_nfl_snapshot),
            ('nhl', CreateNHLModel, NHLIteration, NHLTicket, store_nhl_snapshot)):
            title = 'Dallas Cowboys at New York Giants' if sport == 'nfl' else 'Boston Bruins at Toronto Maple Leafs'
            venue = 'MetLife Stadium' if sport == 'nfl' else 'Scotiabank Arena'
            snapshot = EventSnapshot(source_id='1234567', title=title, venue=venue,
                sections=tuple(SectionSnapshot(section=str(i), price=100+i, listing_count=2,
                    row='A', quantity='2', displayed_price=str(100+i), alternate_price='') for i in range(10)))
            with self.subTest(sport=sport), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/f'{sport}.db'
                url = 'https://www.vividseats.com/game/production/1234567'
                def save(captured):
                    return store(url, start+timedelta(days=2), snapshot, captured, db_path=path)
                first = save(start)  # Existing hourly records keep their exact :00 keys.
                duplicate_first = save(start+timedelta(minutes=29, seconds=59))
                second = save(start+timedelta(minutes=31))
                duplicate_second = save(start+timedelta(minutes=58))
                self.assertTrue(first[2]); self.assertTrue(second[2])
                self.assertFalse(duplicate_first[2]); self.assertFalse(duplicate_second[2])
                self.assertEqual(first[:2], duplicate_first[:2])
                self.assertEqual(second[:2], duplicate_second[:2])
                self.assertNotEqual(first[1], second[1])
                model = model_cls(path)
                try:
                    with model.getSession()() as session:
                        rows = session.query(iteration).order_by(iteration.captured_at).all()
                        self.assertEqual([row.captured_at for row in rows], [
                            captured_datetime_for_storage(start),
                            captured_datetime_for_storage(start+timedelta(minutes=30))])
                        self.assertEqual(session.query(ticket).count(), 20)
                finally:
                    model.engine.dispose()

    def test_capture_crossing_a_tick_records_the_observation_time(self):
        import nfl_schedule_collector as nfl_schedule
        import nhl_schedule_collector as nhl_schedule
        initial = datetime(2026, 10, 6, 12, 5, tzinfo=timezone.utc)
        for sport, module in (('nfl', nfl_schedule), ('nhl', nhl_schedule)):
            class Clock(datetime):
                current = initial
                @classmethod
                def now(cls, tz=None):
                    return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)
            with self.subTest(sport=sport), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                game_class = module.ScheduledNFLGame if sport == 'nfl' else module.ScheduledNHLGame
                game = game_class('game', initial+timedelta(hours=8),
                    'Dallas Cowboys' if sport == 'nfl' else 'Boston Bruins',
                    'New York Giants' if sport == 'nfl' else 'Toronto Maple Leafs',
                    'MetLife Stadium' if sport == 'nfl' else 'Scotiabank Arena', 'Game')
                snapshot = SimpleNamespace(venue=game.venue, sections=[1], source_id='1', title='Game', currency='USD')
                resolution = SimpleNamespace(game=game, candidates=[1], source='fixture')
                stack.enter_context(patch.object(module,'datetime',Clock))
                stack.enter_context(patch.object(module,'replay_pending_snapshots',return_value=(0,True,[])))
                stack.enter_context(patch.object(module,'fetch_schedule_games',return_value=([game],['fixture'])))
                stack.enter_context(patch.object(module,'discover_'+sport+'_games',return_value=([],[])))
                stack.enter_context(patch.object(module,'resolve_schedule_games',return_value=([resolution],[])))
                if sport == 'nhl':
                    stack.enter_context(patch.object(module,'nhl_should_skip_for_trigger',return_value=False))
                def capture(*args,**kwargs):
                    Clock.current = initial+timedelta(minutes=30)
                    return 'https://www.vividseats.com/game/production/1',game.event_date,snapshot
                stack.enter_context(patch.object(module,'_capture_resolution',side_effect=capture))
                stamps=[]
                def payload(url,event,captured,*args,**kwargs):
                    stamps.append(captured)
                    return {'captured_at':captured.isoformat()}
                stack.enter_context(patch.object(module,sport+'_snapshot_to_payload',side_effect=payload))
                def queue(value,pending):
                    pending.mkdir(parents=True,exist_ok=True);path=pending/'game.json'
                    path.write_text('{}');return path
                stack.enter_context(patch.object(module,'queue_snapshot',side_effect=queue))
                stack.enter_context(patch.object(module,'post_snapshot_with_retry',return_value=
                    {'status':'stored','event_id':1,'iteration_id':1}))
                root=Path(directory)
                self.assertEqual(module.run_schedule_collector('','',True,1,root/'health.json',root/'pending'),0)
                self.assertEqual(stamps,[initial+timedelta(minutes=30)])


if __name__ == '__main__':
    unittest.main()
