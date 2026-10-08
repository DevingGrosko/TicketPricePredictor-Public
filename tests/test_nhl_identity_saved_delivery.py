"""Fixed-byte replay uses real queue/receipts and cannot recapture or change time."""
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

import collector
from tests.test_observation_receipt import proof
from tools import nhl_identity_saved_delivery as helper
from tools.shared_capture import MirrorQueue, deliver_tidb, export_observations


class SavedIdentityDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_exact_three_real_observations_preserve_original_times_and_counts(self):
        values=helper.load_fixed()
        self.assertEqual([value['source_id'] for value in values],['7299775','7302223','7299771'])
        self.assertEqual([value['section_count'] for value in values],[66,79,69])
        self.assertEqual([value['captured_at'] for value in values],[row[2] for row in helper.FILES])
        self.assertEqual([value['event_date'] for value in values],[row[3] for row in helper.FILES])

    def test_tampered_last_file_is_rejected_before_any_delivery(self):
        directory=self.root/'docs/shared-observations';directory.mkdir(parents=True)
        source=Path(helper.__file__).resolve().parents[1]/'docs/shared-observations'
        for pid,*_ in helper.FILES:
            name=f'snapshot-nhl-{pid}-identity-oct8.json';shutil.copy2(source/name,directory/name)
        with (directory/'snapshot-nhl-7299771-identity-oct8.json').open('ab') as out:out.write(b'\n')
        original=helper.load_fixed
        with patch.object(helper,'load_fixed',side_effect=lambda:original(self.root)), \
             patch.object(helper,'run_legacy') as delivery,self.assertRaisesRegex(ValueError,'SHA256'):
            helper.deliver(self.root/'mirror',self.root/'pending',self.root/'health.json')
        delivery.assert_not_called()

    def test_real_replay_mirrors_before_receipts_and_tidb_receives_exact_originals_without_browser(self):
        values=helper.load_fixed();posted=[];directory=self.root/'mirror'
        def post(endpoint,token,value,**kwargs):
            self.assertEqual(endpoint,'https://bunnyjeff.pythonanywhere.com/api/nhl/snapshot')
            self.assertEqual(token,'test')
            self.assertIn(value,[record['payload'] for _,record in MirrorQueue(directory,'nhl').records()])
            posted.append(value);return proof(value)
        with patch.dict(os.environ,{'COLLECTOR_INGEST_TOKEN':'test'},clear=True), \
             patch.object(collector,'post_snapshot_with_retry',post), \
             patch('nfl_collector.VividNFLBrowser.__init__',side_effect=AssertionError('No browser')), \
             patch('nhl_schedule_collector.fetch_schedule_games',side_effect=AssertionError('No schedule request')), \
             patch('sys.stdout',StringIO()):
            self.assertEqual(helper.deliver(directory,self.root/'pending',self.root/'health.json'),0)
            incoming=self.root/'incoming';export_observations('nhl',directory,incoming)
            received=[]
            def tidb(value):
                received.append(value)
                result=proof(value);result.update(source_id=value['source_id'],observed_at=value['captured_at'],
                    price_readback_verified=True,identity_readback_verified=True)
                return result
            self.assertEqual(deliver_tidb('nhl',self.root/'tidb',incoming,sender=tidb),0)
        self.assertEqual(posted,values);self.assertEqual({v['source_id'] for v in received},{v['source_id'] for v in values})
        self.assertTrue(all(v in values for v in received))
        report=json.loads((self.root/'health.json').read_text())
        self.assertEqual((report['captured'],report['replayed'],report['mode']),(0,3,'delivery-only'))
        self.assertIsNone(report['coverage_percent'])

    def test_manual_branch_workflow_has_no_provider_or_other_sport_path(self):
        import yaml
        project=Path(helper.__file__).resolve().parents[1]
        value=yaml.load((project/'.github/workflows/collect-ticket-prices.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(set(value['on']),{'workflow_dispatch'})
        self.assertEqual(set(value['jobs']),{'collect-baseball','collect-nfl','collect-nhl','mirror-nhl'})
        for sport in ('baseball','nfl'):self.assertEqual(value['jobs']['collect-'+sport]['if'],'${{ false }}')
        producer=value['jobs']['collect-nhl']
        self.assertIn("github.ref == 'refs/heads/codex/nhl-identity-delivery'",producer['if'])
        self.assertIn("github.event_name == 'workflow_dispatch'",producer['if'])
        self.assertIn("github.event.repository.private == false",producer['if'])
        self.assertEqual(producer['concurrency'],{'group':'nhl-ticket-price-collector','cancel-in-progress':'false'})
        scripts='\n'.join(step.get('run','') for step in producer['steps'])
        for forbidden in ('xvfb-run','playwright install','remote-run',' smoke ','dispatch-backup','prune-caches','publication-handoff'):
            self.assertNotIn(forbidden,scripts)
        delivery=next(step for step in producer['steps'] if step.get('id')=='delivery')
        self.assertEqual(set(delivery['env']),{'COLLECTOR_INGEST_TOKEN'})
        restore=next(step for step in producer['steps'] if step.get('uses','').startswith('actions/cache/restore'))
        self.assertEqual(restore['with']['restore-keys'],'shared-capture-v1-nhl-capture-${{ github.run_id }}-')
        mirror=value['jobs']['mirror-nhl']
        self.assertEqual(mirror['uses'],'./.github/workflows/shared-snapshot-mirror.yml')
        self.assertEqual(mirror['permissions'],{'contents':'read','actions':'read'})
        self.assertEqual(mirror['with'],{'sport':'nhl','source_ref':'c0f940ca1199a81306dfa87249912c6cca9e8991'})
        self.assertNotIn('COLLECTOR_INGEST_TOKEN',mirror['secrets'])


if __name__=='__main__':unittest.main()
