"""Offline interruption, independent delivery and real legacy-hook checks."""
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import collector
import nfl_schedule_collector as nfl
import nhl_schedule_collector as nhl
from collector import EventSnapshot, SectionSnapshot, SnapshotUploadError
from nfl_collector import nfl_snapshot_to_payload
from nhl_collector import nhl_snapshot_to_payload
from tools.shared_capture import MirrorQueue, deliver_tidb, run_legacy, saved_observations


def payload(sport='nfl', pid='6493143', captured=None):
    captured = captured or datetime.now(timezone.utc)
    title = 'Minnesota Vikings at New Orleans Saints' if sport == 'nfl' else 'Utah Mammoth at Boston Bruins'
    snapshot = EventSnapshot(pid, title, 'Arena', tuple(SectionSnapshot('Section '+str(i), 70+i, 2, 'A', '2', str(70+i), '')
                                                     for i in range(10)))
    if sport == 'nhl':
        from nhl_collector import NHLEventSnapshot
        snapshot = NHLEventSnapshot(pid, title, 'Arena', snapshot.sections, currency='USD')
    build = nfl_snapshot_to_payload if sport == 'nfl' else nhl_snapshot_to_payload
    return build('https://www.vividseats.com/game/production/'+pid,
                 captured + timedelta(hours=12), captured, snapshot, schedule={'schedule_id': 'game-'+pid})


def acknowledgment(value, destination='pythonanywhere', status='stored'):
    from Flask_App.collection_cadence import half_hour_capture_slot
    response = dict(event_type=value['event_type'], status=status, event_id=1, iteration_id=2,
                    sections=value['section_count'], captured_at=half_hour_capture_slot(
                        datetime.fromisoformat(value['captured_at'])).isoformat())
    if destination == 'tidb':
        response.update(source_id=value['source_id'], observed_at=value['captured_at'],
                        price_readback_verified=True, identity_readback_verified=True)
    return response


class SharedCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.mirror, self.pending, self.health = self.root/'mirror', self.root/'pending', self.root/'health.json'

    def test_queue_aliases_are_durable_before_real_pa_call_and_restored_for_both_sports(self):
        for sport, module in (('nfl', nfl), ('nhl', nhl)):
            with self.subTest(sport=sport):
                value = payload(sport, '6493143' if sport == 'nfl' else '7302493')
                directory = self.root/sport
                original = (collector.queue_snapshot, module.queue_snapshot, collector.post_snapshot_with_retry)
                def post(endpoint, token, observation, **kwargs):
                    self.assertEqual(endpoint, f'https://bunnyjeff.pythonanywhere.com/api/{sport}/snapshot')
                    self.assertEqual(token, 'test-token')
                    state = MirrorQueue(directory, sport).records()[0][1]
                    self.assertEqual(state['payload'], observation)
                    self.assertIsNone(state['acknowledged']['pythonanywhere'])
                    return acknowledgment(observation)
                def runner(endpoint, token, headless, timeout, health, pending):
                    self.assertFalse(headless)
                    path = module.queue_snapshot(value, pending)
                    self.assertTrue(path.exists())
                    module.post_snapshot_with_retry(endpoint, token, value)
                    path.unlink()
                    health.write_text(json.dumps({'status':'healthy'}))
                    return 0
                with patch.dict(os.environ, {'COLLECTOR_INGEST_TOKEN':'test-token'}, clear=True), \
                     patch.object(collector, 'post_snapshot_with_retry', post), patch('sys.stdout', StringIO()):
                    self.assertEqual(run_legacy(sport, directory, self.pending, self.health, runner=runner), 0)
                self.assertEqual((collector.queue_snapshot, module.queue_snapshot, collector.post_snapshot_with_retry), original)
                record = MirrorQueue(directory, sport).records()[0][1]
                self.assertIsNotNone(record['acknowledged']['pythonanywhere'])
                self.assertIsNone(record['acknowledged']['tidb'])
                self.assertNotIn('test-token', json.dumps(record))

    def test_real_legacy_schedule_loops_capture_once_and_queue_before_upload_for_both_sports(self):
        for sport,module in (('nfl',nfl),('nhl',nhl)):
            with self.subTest(sport=sport):
                value=payload(sport,'6493143' if sport=='nfl' else '7302493')
                url,event,_captured,snapshot=collector.snapshot_from_payload(value)
                title=value['title'];away,home=title.split(' at ')
                game_class=module.ScheduledNFLGame if sport=='nfl' else module.ScheduledNHLGame
                game=game_class('game-'+value['source_id'],event,away,home,'Arena',title)
                candidate=SimpleNamespace(url=url)
                resolution=module.ScheduleResolution(game,(candidate,),'vivid-'+sport+'-feed')
                directory=self.root/sport
                if sport=='nhl':
                    from nhl_collector import NHLEventSnapshot
                    snapshot=NHLEventSnapshot(snapshot.source_id,snapshot.title,snapshot.venue,snapshot.sections)
                def post(endpoint,token,observation,**kwargs):
                    self.assertEqual(MirrorQueue(directory,sport).records()[0][1]['payload'],observation)
                    return acknowledgment(observation)
                with patch.dict(os.environ,{'COLLECTOR_INGEST_TOKEN':'test'},clear=True), \
                     patch.object(collector,'post_snapshot_with_retry',post), \
                     patch.object(module,'fetch_schedule_games',return_value=([game],'official' if sport=='nfl' else ['official'])), \
                     patch.object(module,'discover_'+sport+'_games',return_value=([],[])), \
                     patch.object(module,'resolve_schedule_games',return_value=([resolution],[])), \
                     patch.object(module,'_capture_resolution',return_value=(url,event,snapshot)) as capture, \
                     patch('sys.stdout',StringIO()):
                    self.assertEqual(run_legacy(sport,directory,self.root/(sport+'-pending'),
                                               self.root/(sport+'-health.json')),0)
                self.assertEqual(capture.call_count,1)
                health=json.loads((self.root/(sport+'-health.json')).read_text())
                self.assertEqual((health['captured'],health['uploaded'],health['failed']),(1,1,0))

    def test_pa_failure_and_post_latch_still_mirror_every_capture_then_deliver_independently(self):
        values = [payload(pid='6493143'), payload(pid='6491666')]
        def failed_post(*args, **kwargs):
            raise SnapshotUploadError('PA unavailable', retryable=True)
        def runner(endpoint, token, headless, timeout, health, pending):
            nfl.queue_snapshot(values[0], pending)
            with self.assertRaises(SnapshotUploadError):
                nfl.post_snapshot_with_retry(endpoint, token, values[0])
            # Match the real legacy latch: later snapshots are queued with no POST.
            nfl.queue_snapshot(values[1], pending)
            health.write_text(json.dumps({'status':'queued'}))
            return 0
        with patch.dict(os.environ, {'COLLECTOR_INGEST_TOKEN':'test-token'}, clear=True), \
             patch.object(collector, 'post_snapshot_with_retry', failed_post), patch('sys.stdout', StringIO()):
            self.assertEqual(run_legacy('nfl', self.mirror, self.pending, self.health, runner=runner), 1)
            calls = []
            def sender(observation):
                calls.append(observation); return acknowledgment(observation, 'tidb')
            self.assertEqual(deliver_tidb('nfl', self.root/'tidb', self.mirror, sender=sender), 0)
        records = MirrorQueue(self.root/'tidb', 'nfl').records()
        self.assertEqual(len(records), 2)
        self.assertTrue(all('payload' in record and record['acknowledged']['tidb']
                            and record['acknowledged']['pythonanywhere'] is None for _,record in records))
        self.assertEqual({row['captured_at'] for row in calls}, {row['captured_at'] for row in values})

    def test_existing_pending_is_mirrored_before_replay_unlinks_it(self):
        value = payload()
        collector.queue_snapshot(value, self.pending)
        def post(endpoint, token, observation, **kwargs):
            self.assertEqual(MirrorQueue(self.mirror,'nfl').records()[0][1]['payload'], value)
            return acknowledgment(observation)
        def runner(endpoint, token, headless, timeout, health, pending):
            count, available, errors = collector.replay_pending_snapshots(endpoint, token, pending)
            self.assertEqual((count, available, errors), (1, True, []))
            health.write_text(json.dumps({'status':'healthy'})); return 0
        with patch.dict(os.environ, {'COLLECTOR_INGEST_TOKEN':'test-token'}, clear=True), \
             patch.object(collector,'post_snapshot_with_retry',post), patch('sys.stdout',StringIO()):
            self.assertEqual(run_legacy('nfl',self.mirror,self.pending,self.health,runner=runner),0)
        self.assertEqual(list(self.pending.glob('*.json')), [])

    def test_pa_duplicate_remains_unverified_pending_and_red_without_readback(self):
        value=payload();queue=MirrorQueue(self.mirror,'nfl');queue.enqueue(value)
        def post(*args,**kwargs):return acknowledgment(value,status='duplicate')
        with patch.dict(os.environ,{'COLLECTOR_INGEST_TOKEN':'test'},clear=True), \
             patch.object(collector,'post_snapshot_with_retry',post),patch('sys.stdout',StringIO()):
            self.assertEqual(run_legacy('nfl',self.mirror,self.pending,self.health,saved=[value]),1)
        record=MirrorQueue(self.mirror,'nfl').records()[0][1]
        self.assertIsNone(record['acknowledged']['pythonanywhere'])
        self.assertEqual(record['payload'],value)
        self.assertEqual(len(list(self.pending.glob('*.rejected'))),1)
        self.assertEqual(json.loads(self.health.read_text())['status'],'queued')

    def test_lost_pa_queue_is_recreated_from_mirror_and_same_slot_skips_provider_work(self):
        value = payload(); queue = MirrorQueue(self.mirror,'nfl'); queue.enqueue(value)
        game = SimpleNamespace(schedule_id='game-6493143', event_date=datetime.fromisoformat(value['event_date']))
        def post(*args, **kwargs): return acknowledgment(value)
        def runner(endpoint, token, headless, timeout, health, pending):
            collector.replay_pending_snapshots(endpoint,token,pending)
            from Flask_App.collection_cadence import half_hour_capture_slot
            self.assertEqual(nfl.schedule_games_due([game],half_hour_capture_slot(datetime.fromisoformat(value['captured_at']))),[])
            health.write_text(json.dumps({'status':'healthy'}));return 0
        with patch.dict(os.environ,{'COLLECTOR_INGEST_TOKEN':'test-token'},clear=True), \
             patch.object(collector,'post_snapshot_with_retry',post), \
             patch.object(nfl,'schedule_games_due',return_value=[game]), patch('sys.stdout',StringIO()):
            self.assertEqual(run_legacy('nfl',self.mirror,self.pending,self.health,runner=runner),0)

    def test_temporary_tidb_failure_and_lost_ack_replay_exact_payload_without_recapture(self):
        value=payload(); captured=MirrorQueue(self.mirror,'nfl')
        captured.acknowledge(value,'pythonanywhere',acknowledgment(value))
        delivered=self.root/'delivered'; attempts=[]
        def sender(observation):
            attempts.append(observation.copy())
            return acknowledgment(observation,'tidb','stored' if len(attempts)==1 else 'duplicate')
        save=MirrorQueue._save
        def interrupted(queue,path,record):
            if record['acknowledged']['tidb']:
                raise OSError('Runner interrupted after real commit')
            return save(queue,path,record)
        with patch.object(MirrorQueue,'_save',interrupted),patch('sys.stdout',StringIO()):
            self.assertEqual(deliver_tidb('nfl',delivered,self.mirror,sender=sender),1)
        with patch('sys.stdout',StringIO()):
            self.assertEqual(deliver_tidb('nfl',delivered,self.mirror,sender=sender),0)
            self.assertEqual(deliver_tidb('nfl',delivered,self.mirror,sender=Mock(side_effect=AssertionError('No delivery after ACK'))),0)
        self.assertEqual(attempts,[value,value])
        self.assertNotIn('payload',MirrorQueue(delivered,'nfl').records()[0][1])

    def test_tidb_failure_retains_pending_payload_and_other_game_still_commits(self):
        values=[payload(),payload(pid='6491666')];queue=MirrorQueue(self.mirror,'nfl')
        for value in values: queue.enqueue(value)
        def sender(value):
            if value['source_id']=='6493143': raise ConnectionError('private connection details')
            return acknowledgment(value,'tidb')
        output=StringIO()
        with patch('sys.stdout',output):
            self.assertEqual(deliver_tidb('nfl',self.root/'tidb',self.mirror,sender=sender),1)
        self.assertNotIn('private connection details',output.getvalue())
        self.assertEqual(len(MirrorQueue(self.root/'tidb','nfl').pending('tidb')),1)

    def test_collision_private_fields_and_hash_corruption_fail_closed(self):
        value=payload(); queue=MirrorQueue(self.mirror,'nfl');path=queue.enqueue(value)
        changed=json.loads(json.dumps(value));changed['sections'][0]['price']+=1
        with self.assertRaisesRegex(ValueError,'Different observation'): queue.enqueue(changed)
        for change in ({'Authorization':'private'}, {'schedule':{'token':'private'}}):
            with self.subTest(change=change),self.assertRaises(ValueError): queue.enqueue({**value,**change})
        record=json.loads(path.read_text());record['payload']['sections'][0]['price']+=1;path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError,'Immutable'):queue.read(path)

    def test_transport_urls_naive_original_times_and_wrong_receipt_sport_are_rejected(self):
        value=payload();queue=MirrorQueue(self.mirror,'nfl')
        changes=({'source_url':value['source_url']+'?token=private'},
                 {'source_url':value['source_url'].replace('www.vividseats.com','private@www.vividseats.com')},
                 {'captured_at':datetime.now().isoformat()})
        for change in changes:
            with self.subTest(change=change),self.assertRaises(ValueError):queue.enqueue({**value,**change})
        with self.assertRaises(ValueError):
            queue.acknowledge(value,'pythonanywhere',{**acknowledgment(value),'event_type':'nhl'})

    def test_stale_artifact_cannot_erase_acks_and_independent_states_merge(self):
        value=payload();source=MirrorQueue(self.mirror,'nfl');source.enqueue(value)
        target=MirrorQueue(self.root/'delivered','nfl');target.merge(self.mirror)
        target.acknowledge(value,'tidb',acknowledgment(value,'tidb'))
        source.acknowledge(value,'pythonanywhere',acknowledgment(value))
        target.merge(self.mirror)
        record=target.records()[0][1]
        self.assertTrue(all(record['acknowledged'].values()))
        self.assertNotIn('payload',record)
        stale=MirrorQueue(self.root/'stale','nfl');stale.enqueue(value)
        target.merge(stale.root)
        self.assertNotIn('payload',target.records()[0][1])

    def test_late_consumer_checkpoint_cannot_erase_newer_producer_observation(self):
        first,second=payload(),payload(pid='6491666')
        producer=MirrorQueue(self.mirror,'nfl')
        producer.acknowledge(first,'pythonanywhere',acknowledgment(first))
        consumer=MirrorQueue(self.root/'consumer-a','nfl');consumer.merge(self.mirror)
        producer.acknowledge(second,'pythonanywhere',acknowledgment(second))
        consumer.acknowledge(first,'tidb',acknowledgment(first,'tidb'))
        def runner(endpoint,token,headless,timeout,health,pending):
            self.assertEqual(len(MirrorQueue(self.mirror,'nfl').records()),2)
            health.write_text(json.dumps({'status':'healthy'}));return 0
        with patch.dict(os.environ,{'COLLECTOR_INGEST_TOKEN':'test'},clear=True),patch('sys.stdout',StringIO()):
            self.assertEqual(run_legacy('nfl',self.mirror,self.pending,self.health,runner=runner,
                                       acknowledgments=consumer.root),0)
        pending=MirrorQueue(self.mirror,'nfl').pending('tidb')
        self.assertEqual([row['source_id'] for _,row in pending],['6491666'])

    def test_fixed_real_saved_pilot_replays_both_sports_with_zero_browser_or_schedule_calls(self):
        manifest='docs/shared-observations/manifest-0400.json'
        digest='6b894990f80fb18f766427767944d0096f74e44bd0683e3630eda9e76999e287'
        for sport,module,sections in (('nfl',nfl,[211,200]),('nhl',nhl,[60,77])):
            with self.subTest(sport=sport):
                saved=saved_observations(manifest,digest,sport)
                self.assertEqual(len(saved),2)
                self.assertEqual([row['section_count'] for row in saved],sections)
                posted=[]
                def post(endpoint,token,value,**kwargs):
                    posted.append(value);return acknowledgment(value)
                directory=self.root/sport;health=self.root/(sport+'-health.json')
                with patch.dict(os.environ,{'COLLECTOR_INGEST_TOKEN':'test'},clear=True), \
                     patch.object(collector,'post_snapshot_with_retry',post), \
                     patch.object(module,'run_schedule_collector',side_effect=AssertionError('No scheduler in replay')), \
                     patch('nfl_collector.VividNFLBrowser.__init__',side_effect=AssertionError('No browser in replay')), \
                     patch('sys.stdout',StringIO()):
                    self.assertEqual(run_legacy(sport,directory,self.root/(sport+'-pending'),health,saved=saved),0)
                    self.assertEqual(deliver_tidb(sport,self.root/(sport+'-tidb'),directory,
                                                 sender=lambda value:acknowledgment(value,'tidb')),0)
                report=json.loads(health.read_text())
                self.assertEqual((report['mode'],report['captured'],report['replayed']),('delivery-only',0,2))
                self.assertIsNone(report['coverage_percent']);self.assertIsNone(report['scheduled_due'])
                self.assertEqual(posted,saved)

    def test_saved_manifest_hash_and_path_escape_are_rejected_before_delivery(self):
        manifest='docs/shared-observations/manifest-0400.json'
        with self.assertRaisesRegex(ValueError,'integrity mismatch'):
            saved_observations(manifest,'0'*64,'nfl')
        with self.assertRaises(ValueError):
            saved_observations('../../.env','0'*64,'nfl')

    def test_budget_reserves_acks_and_never_deletes_existing_pending_to_make_room(self):
        value=payload();queue=MirrorQueue(self.mirror,'nfl');path=queue.enqueue(value)
        before=path.read_bytes();queue.byte_limit=path.stat().st_size+2048
        with self.assertRaisesRegex(ValueError,'budget exhausted'):queue.enqueue(payload(pid='6491666'))
        self.assertEqual(path.read_bytes(),before)
        queue.acknowledge(value,'pythonanywhere',acknowledgment(value))

    def test_credential_mix_and_mlb_are_rejected_before_capture(self):
        runner=Mock()
        for env in ({}, {'COLLECTOR_INGEST_TOKEN':'private','TIDB_STAGING_PASSWORD':'private'}):
            with patch.dict(os.environ,env,clear=True),self.assertRaises(RuntimeError):
                run_legacy('nfl',self.mirror,self.pending,self.health,runner=runner)
        runner.assert_not_called()
        with self.assertRaises(ValueError):MirrorQueue(self.root/'mlb','mlb')

    def test_legacy_failures_propagate_and_aliases_restore_on_interruption(self):
        originals=(collector.queue_snapshot,nfl.queue_snapshot,nhl.queue_snapshot)
        def interrupted(*args):raise RuntimeError('Capture interrupted')
        with patch.dict(os.environ,{'COLLECTOR_INGEST_TOKEN':'private'},clear=True),self.assertRaises(RuntimeError):
            run_legacy('nfl',self.mirror,self.pending,self.health,runner=interrupted)
        self.assertEqual((collector.queue_snapshot,nfl.queue_snapshot,nhl.queue_snapshot),originals)

    def test_workflow_pilot_is_opt_in_and_credentials_are_separate_per_sport_job(self):
        import yaml
        root=Path(__file__).resolve().parents[1]
        legacy=yaml.load((root/'.github/workflows/collect-ticket-prices.yml').read_text(),Loader=yaml.BaseLoader)
        mirror=yaml.load((root/'.github/workflows/shared-snapshot-mirror.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(legacy['on']['workflow_dispatch']['inputs']['shared_capture']['default'],'false')
        self.assertEqual(legacy['on']['workflow_dispatch']['inputs']['delivery_only']['default'],'false')
        self.assertEqual(legacy['on']['workflow_dispatch']['inputs']['browser_navigation']['default'],'direct')
        self.assertEqual(legacy['jobs']['collect-baseball']['if'],'${{ false }}')
        self.assertEqual(set(mirror['on']),{'workflow_call'})
        self.assertEqual(set(mirror['on']['workflow_call']['secrets']),
                         {'TIDB_STAGING_HOST','TIDB_STAGING_USERNAME','TIDB_STAGING_PASSWORD'})
        for sport in ('nfl','nhl'):
            job=legacy['jobs']['mirror-'+sport]
            self.assertEqual(job['needs'],'collect-'+sport)
            self.assertIn('workflow_dispatch',job['if']);self.assertIn('inputs.shared_capture',job['if'])
            self.assertNotIn('COLLECTOR_INGEST_TOKEN',job['secrets'])
            steps=legacy['jobs']['collect-'+sport]['steps']
            caches=[step for step in steps if step.get('uses','').startswith('actions/cache/restore')]
            prefixes={step['with']['restore-keys'] for step in caches}
            self.assertIn('shared-capture-v1-'+sport+'-capture-',prefixes)
            self.assertIn('shared-capture-v1-'+sport+'-tidb-',prefixes)
            consumer_restore=next(step for step in caches if '-tidb-' in step['with']['restore-keys'])
            self.assertEqual(consumer_restore['with']['path'],'shared-acknowledgments/'+sport)
            own_save=next(step for step in steps if step.get('name','').startswith('Checkpoint the optional'))
            self.assertIn('github.run_attempt',own_save['with']['key'])
        job=mirror['jobs']['mirror']
        self.assertEqual(job['environment'],'tidb-staging')
        self.assertEqual(job['concurrency']['group'],'free-refresh-staging-${{ inputs.sport }}-writer')
        env=next(step['env'] for step in job['steps'] if step.get('name','').startswith('Deliver original'))
        self.assertNotIn('COLLECTOR_INGEST_TOKEN',env)
        self.assertEqual(env['TICKETSIGNAL_ENABLE_STAGING_WRITES'],'1')
        consumer_cache=next(step for step in job['steps'] if step.get('uses','').startswith('actions/cache/restore'))
        self.assertEqual(consumer_cache['with']['restore-keys'],'shared-capture-v1-${{ inputs.sport }}-tidb-')
        self.assertEqual(consumer_cache['with']['path'],'shared-acknowledgments/${{ inputs.sport }}')
        consumer_save=next(step for step in job['steps'] if step.get('uses','').startswith('actions/cache/save'))
        self.assertEqual(consumer_save['with']['path'],consumer_cache['with']['path'])


if __name__=='__main__':unittest.main()
