"""Offline regression checks for approved restrictions; no provider or DB calls."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import tempfile
import unittest

from tools.free_live_hardening import due_since, preserved_parser, replay_each, run_nhl
from tools.free_live_mlb_schedule import schedule_games, validate_match
from tools.free_live_watchdog import should_dispatch, TARGET
from test_free_refresh_capture import NOW, payload


class PolicyTests(unittest.TestCase):
    def test_old_capture_is_not_backdated_and_future_invalid_prices_still_fail(self):
        import collector
        from tools.free_refresh_capture import parse_payload
        parse = preserved_parser(parse_payload)
        old = payload()
        old['captured_at'] = (NOW-timedelta(days=40)).isoformat()
        old['event_date'] = (NOW-timedelta(days=39)).isoformat()
        old['sections'] = old['sections'][:1]; old['section_count'] = 1
        with patch.object(collector,'MIN_USABLE_SECTIONS',1):
            self.assertEqual(parse('mlb',old,NOW)[2].date(), (NOW-timedelta(days=40)).date())
            for changes in ({'captured_at':(NOW+timedelta(days=1)).isoformat()}, {'section_count':2}):
                with self.assertRaises(ValueError): parse('mlb',{**old,**changes},NOW)
            for bad_price in (0,-1,'NaN','Infinity'):
                bad=deepcopy(old);bad['sections'][0]['price']=bad_price
                with self.assertRaises(ValueError): parse('mlb',bad,NOW)
        self.assertEqual(collector.MIN_USABLE_SECTIONS,10)

    def test_pending_failure_does_not_block_next_valid_file(self):
        import collector
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root=Path(directory)
            for name in ('a','b'): (root/(name+'.json')).write_text(json.dumps({'id':name}))
            stack.enter_context(patch('tools.free_refresh_capture.parse_payload'))
            calls=[]
            def deliver(endpoint,token,data):
                calls.append(data['id'])
                if data['id']=='a': raise RuntimeError('injected')
                return {'status':'stored'}
            stack.enter_context(patch.object(collector,'post_snapshot_with_retry',side_effect=deliver))
            count,available,errors=replay_each('mlb','','',root)
            self.assertEqual(calls,['a','b']);self.assertEqual(count,1)
            self.assertFalse(available);self.assertTrue(errors)
            self.assertTrue((root/'a.json').exists());self.assertFalse((root/'b.json').exists())

    def test_missed_cadence_does_not_wait_for_next_daily_phase(self):
        game=SimpleNamespace(schedule_id='a',event_date=NOW+timedelta(days=20))
        choose=lambda games,slot: games if slot.hour==10 else []
        result=due_since([game],NOW,(NOW-timedelta(hours=4)).isoformat(),choose)
        self.assertEqual(result,[game])

    def test_watchdog_skips_active_and_recent_but_recovers_missed_tick(self):
        row=dict(path=TARGET,head_branch='main',created_at=(NOW-timedelta(hours=1)).isoformat(),status='completed')
        self.assertTrue(should_dispatch([row],NOW)[0])
        self.assertFalse(should_dispatch([{**row,'status':'in_progress'}],NOW)[0])
        self.assertFalse(should_dispatch([{**row,'created_at':NOW.isoformat()}],NOW)[0])

    def test_mlb_schedule_is_league_wide_not_venue_or_url_date_filtered(self):
        game=dict(gamePk=100,gameType='R',gameDate=(NOW+timedelta(hours=8)).isoformat(),
                  status={'abstractGameState':'Preview'},venue={'name':'Coors Field'},
                  teams={'away':{'team':{'name':'San Diego Padres'}},'home':{'team':{'name':'Colorado Rockies'}}})
        data={'dates':[{'games':[game,{**game,'gamePk':101,'gameType':'S'},
                                    {**game,'gamePk':102,'gameDate':(NOW-timedelta(hours=2)).isoformat()}]}]}
        actual=schedule_games(data,NOW)
        self.assertEqual(len(actual),1);self.assertEqual(actual[0]['venue'],'Coors Field')
        with self.assertRaises(ValueError): schedule_games({},NOW)

    def test_wrong_game_and_doubleheader_times_are_not_mixed(self):
        import collector
        game={'away_team':'San Diego Padres','home_team':'Colorado Rockies',
              'event_date':(NOW+timedelta(hours=8)).isoformat()}
        snapshot=SimpleNamespace(source_id='123',title='San Diego Padres at Colorado Rockies')
        url='https://www.vividseats.com/incorrect-01-01-2000--sports-mlb-baseball/production/123'
        with patch.object(collector.SnapshotParser,'parse',return_value=snapshot):
            self.assertEqual(validate_match(game,url,{},NOW+timedelta(hours=8))[0],snapshot)
            with self.assertRaises(ValueError): validate_match(game,url,{},NOW+timedelta(hours=13))
            with self.assertRaises(ValueError): validate_match(game,url+'4',{},NOW+timedelta(hours=8))


class NHLTests(unittest.TestCase):
    def test_each_game_delivery_attempted_after_first_database_failure(self):
        import nhl_schedule_collector as nhl
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root=Path(directory);now=datetime.now(timezone.utc)
            games=[nhl.ScheduledNHLGame(str(i),now+timedelta(hours=8),'Toronto Maple Leafs',
                'Montreal Canadiens','Bell Centre','Game') for i in range(3)]
            stack.enter_context(patch.object(nhl,'replay_pending_snapshots',return_value=(0,True,[])))
            stack.enter_context(patch.object(nhl,'fetch_schedule_games',return_value=(games,['fixture'])))
            stack.enter_context(patch.object(nhl,'schedule_games_due',side_effect=lambda g,s:g))
            stack.enter_context(patch.object(nhl,'discover_nhl_games',return_value=([],[])))
            stack.enter_context(patch.object(nhl,'resolve_schedule_games',return_value=(
                [SimpleNamespace(game=g,candidates=[1]) for g in games],[])))
            def capture(resolution,**kwargs):
                return resolution.game.schedule_id,resolution.game.event_date,SimpleNamespace(venue='Bell Centre',sections=[1])
            stack.enter_context(patch.object(nhl,'_capture_resolution',side_effect=capture))
            stack.enter_context(patch.object(nhl,'nhl_snapshot_to_payload',side_effect=lambda url,*a,**k:{'id':url}))
            def queue(data,pending):
                pending.mkdir(parents=True,exist_ok=True);path=pending/(data['id']+'.json')
                path.write_text(json.dumps(data));return path
            stack.enter_context(patch.object(nhl,'queue_snapshot',side_effect=queue))
            attempts=[]
            def deliver(endpoint,token,data):
                attempts.append(data['id'])
                if data['id']=='0':raise RuntimeError('injected')
                return {'status':'stored'}
            stack.enter_context(patch.object(nhl,'post_snapshot_with_retry',side_effect=deliver))
            self.assertEqual(run_nhl('','',True,1,root/'health.json',root/'pending'),1)
            self.assertEqual(attempts,['0','1','2'])
            report=json.loads((root/'health.json').read_text())
            self.assertEqual((report['captured'],report['uploaded'],report['pending']),(3,2,1))
            self.assertEqual(report['unfinished_games'],['0'])
