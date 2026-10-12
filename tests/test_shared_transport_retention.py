"""Transport deletion requires durable verified copies; saturation never purges evidence."""
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.test_shared_capture import acknowledgment, payload
from tools.shared_capture import MirrorQueue, export_observations, verify_delivery_checkpoint
from tools.shared_capture_storage import (
    ARCHIVE_MARGIN, PEER_UPLOAD_RESERVE, PROVENANCE_RUN_LIMIT, PUBLISHER_RESERVE,
    QUEUE_LIMIT, TOTAL_ARTIFACT_LIMIT, TRANSPORT_LIMIT, artifact_upload_budget,
    prune_shared_caches, removable_transport_artifacts, retained_artifact_cache_keys,
)
from tools.single_capture_owner import OWNER, REPO

AT = datetime(2026, 10, 8, 5, tzinfo=timezone.utc)


def artifact(run, sport='nfl', *, identity=None):
    return dict(id=identity or run, name=f'shared-observations-{sport}-{run}', expired=False,
        created_at=(AT+timedelta(minutes=run, seconds=20)).isoformat(), size_in_bytes=100,
        workflow_run=dict(id=run, head_branch='main', head_sha='commit'+str(run)))


def provenance(run, *, status='completed', attempt=1):
    return dict(id=run, path=OWNER, status=status, conclusion='failure', run_attempt=attempt,
        run_started_at=(AT+timedelta(minutes=run)).isoformat(), head_branch='main',
        head_sha='commit'+str(run), repository={'full_name': REPO}, head_repository={'full_name': REPO})


def checkpoint_caches(run, sport='nfl', attempt=1):
    return [dict(id=1000+run*2+i, key=f'shared-capture-v1-{sport}-{role}-{run}-{attempt}',
        ref='refs/heads/main', version='path-v1', created_at=(AT+timedelta(minutes=run, seconds=10)).isoformat())
        for i, role in enumerate(('capture', 'tidb'))]


def jobs(run, attempt, sport='nfl'):
    def step(name): return dict(name=name, status='completed', conclusion='success')
    return [dict(name='collect-'+sport, status='completed', conclusion='failure', steps=[
        step(f'Checkpoint the {sport.upper()} shared queue even after a failed capture'),
        step(f'Prepare a public {sport.upper()} export including an explicit empty checkpoint')]),
        dict(name=f'mirror-{sport}-staging / mirror', status='completed', conclusion='failure', steps=[
        step('Verify current export in the delivery checkpoint'),
        step('Preserve independent delivery acknowledgments and failures')])]


class TransportRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_empty_checkpoint_has_a_nonrecord_file_so_cache_save_is_not_empty(self):
        incoming = self.root/'incoming'; export_observations('nfl', self.root/'source', incoming)
        target = self.root/'target'
        self.assertEqual(verify_delivery_checkpoint('nfl', target, incoming)['records'], 0)
        self.assertEqual({p.name for p in target.iterdir()}, {'delivery.checkpoint'})
        self.assertEqual(MirrorQueue(target, 'nfl').records(), [])

    def test_all_original_records_must_be_present_matching_and_restorable(self):
        first, second = payload(), payload(pid='6491666')
        source = MirrorQueue(self.root/'source', 'nfl'); source.enqueue(first); source.enqueue(second)
        incoming = self.root/'incoming'; export_observations('nfl', source.root, incoming)
        target = MirrorQueue(self.root/'target', 'nfl'); target.enqueue(first)
        with self.assertRaisesRegex(ValueError, 'missing'):
            verify_delivery_checkpoint('nfl', target.root, incoming)
        self.assertFalse((target.root/'delivery.checkpoint').exists())
        target.enqueue(second)
        target.acknowledge(first, 'pythonanywhere', acknowledgment(first))
        target.acknowledge(first, 'tidb', acknowledgment(first, 'tidb'))
        self.assertEqual(verify_delivery_checkpoint('nfl', target.root, incoming)['records'], 2)
        self.assertNotIn('payload', target.records()[0][1] if target.records()[0][1]['source_id']==first['source_id'] else target.records()[1][1])
        path = target.root/source.records()[0][0].name
        record = json.loads(path.read_bytes()); record['payload_sha256'] = '0'*64
        path.write_text(json.dumps(record))
        with self.assertRaises(ValueError): verify_delivery_checkpoint('nfl', target.root, incoming)

    def test_root_checkpoint_marker_counts_toward_admission_budget(self):
        value = payload(); control = MirrorQueue(self.root/'control', 'nfl')
        path = control.enqueue(value)
        queue = MirrorQueue(self.root/'queue', 'nfl', byte_limit=path.stat().st_size+2048)
        (queue.root/'delivery.checkpoint').write_bytes(b'bounded-marker')
        with self.assertRaisesRegex(ValueError, 'budget exhausted'):
            queue.enqueue(value)
        self.assertEqual(queue.records(), [])

    def test_checkpoint_marker_cannot_push_restored_state_beyond_actual_byte_cap(self):
        incoming = self.root/'incoming'; export_observations('nfl', self.root/'source', incoming)
        target = MirrorQueue(self.root/'target', 'nfl')
        padding = target.root/'recovery/padding'; padding.parent.mkdir()
        with padding.open('wb') as stream: stream.truncate(QUEUE_LIMIT)
        with self.assertRaisesRegex(ValueError, 'byte budget'):
            verify_delivery_checkpoint('nfl', target.root, incoming)
        self.assertFalse((target.root/'delivery.checkpoint').exists()); self.assertTrue(padding.exists())

    def test_latest_two_per_sport_and_red_partial_runs_have_safe_checkpoint_proof(self):
        rows = [artifact(i) for i in range(1, 5)]+[artifact(i, 'nhl', identity=100+i) for i in range(1, 5)]
        caches = [row for sport in ('nfl', 'nhl') for i in range(1, 5) for row in checkpoint_caches(i, sport)]
        def all_jobs(run, attempt): return jobs(run, attempt)+jobs(run, attempt, 'nhl')
        remove = removable_transport_artifacts(rows, caches, provenance, all_jobs, current_run=99)
        self.assertEqual({row['id'] for row in remove}, {1, 2, 101, 102})
        # All mocked producer and mirror conclusions are failure; verified copies,
        # rather than green provider/database status, justify duplicate removal.

    def test_current_active_wrong_scope_namespace_rerun_and_unconfirmed_are_preserved(self):
        rows = [artifact(i) for i in range(1, 10)]
        rows += [{**artifact(20), 'name': 'nfl-collector-report-20'},
                 {**artifact(21), 'workflow_run': {'id': 21, 'head_branch': 'other', 'head_sha': 'commit21'}}]
        caches = [row for i in range(1, 10) for row in checkpoint_caches(i)]
        def run(identity):
            result = provenance(identity)
            if identity == 2: result['status'] = 'in_progress'
            if identity == 3: result['repository']['full_name'] = 'other/repo'
            if identity == 4: result['path'] = '.github/workflows/other.yml'
            if identity == 5: result['run_attempt'] = 2
            return result
        def unsafe_jobs(identity, attempt):
            value = jobs(identity, attempt)
            if identity == 6: value[1]['steps'][0]['conclusion'] = 'failure'
            return value
        remove = removable_transport_artifacts(rows, caches, run, unsafe_jobs, current_run=7)
        self.assertEqual([row['id'] for row in remove], [1])

    def test_cache_ref_version_attempt_and_export_proof_are_mandatory(self):
        rows = [artifact(i) for i in range(1, 4)]
        for change in ('missing', 'other-ref', 'missing-version', 'prior-attempt', 'skipped-checkpoint', 'failed-export'):
            with self.subTest(change=change):
                caches = checkpoint_caches(1)
                if change == 'missing': caches.pop()
                elif change == 'other-ref': caches[0]['ref'] = 'refs/heads/other'
                elif change == 'missing-version': caches[0]['version'] = None
                elif change == 'prior-attempt': caches[0]['key'] = caches[0]['key'][:-1]+'2'
                value = jobs(1, 1)
                if change == 'skipped-checkpoint': value[1]['steps'][0]['conclusion'] = 'skipped'
                if change == 'failed-export': value[0]['steps'][1]['conclusion'] = 'failure'
                self.assertEqual(removable_transport_artifacts(rows, caches, provenance,
                    lambda *_: value, current_run=99), [])

    def test_provenance_reads_are_bounded_and_unexamined_artifacts_remain(self):
        rows = [artifact(i) for i in range(1, PROVENANCE_RUN_LIMIT+10)]
        read = Mock(side_effect=provenance)
        remove = removable_transport_artifacts(rows, [], read, Mock(), current_run=99)
        self.assertEqual(read.call_count, PROVENANCE_RUN_LIMIT); self.assertEqual(remove, [])

    def test_duplicate_pagination_rows_cannot_displace_latest_two_or_repeat_deletion(self):
        rows = [artifact(i) for i in range(1, 4)]
        rows.extend([artifact(2), artifact(3)])
        remove = removable_transport_artifacts(rows, checkpoint_caches(1), provenance, jobs, current_run=99)
        self.assertEqual([row['id'] for row in remove], [1])

    def test_prior_attempt_artifact_is_not_confirmed_by_current_attempt_checkpoints(self):
        rows = [artifact(i) for i in range(1, 4)]
        def run(identity):
            value = provenance(identity, attempt=2)
            value['run_started_at'] = (AT+timedelta(minutes=identity, seconds=30)).isoformat()
            return value
        self.assertEqual(removable_transport_artifacts(rows, checkpoint_caches(1, attempt=2),
            run, jobs, current_run=99), [])

    def test_peer_and_publisher_reserves_and_unconfirmed_saturation_refuse_new_upload(self):
        new = QUEUE_LIMIT + ARCHIVE_MARGIN
        safe = TRANSPORT_LIMIT-new-PEER_UPLOAD_RESERVE
        rows = [{**artifact(1), 'size_in_bytes': safe}]
        evidence = artifact_upload_budget(rows, new)
        self.assertEqual(evidence['peer_upload_reserve_bytes'], PEER_UPLOAD_RESERVE)
        self.assertEqual(evidence['publisher_reserve_bytes'], PUBLISHER_RESERVE)
        rows[0]['size_in_bytes'] += 1
        with self.assertRaisesRegex(RuntimeError, 'preserve unconfirmed'):
            artifact_upload_budget(rows, new)
        # No cleanup or deletion is involved in preflight even if all data is unconfirmed.
        self.assertEqual(rows[0]['size_in_bytes'], safe+1)
        total = TOTAL_ARTIFACT_LIMIT-new-PEER_UPLOAD_RESERVE-PUBLISHER_RESERVE
        unrelated = [dict(name='other-untouched-artifact', expired=False, size_in_bytes=total)]
        artifact_upload_budget(unrelated, new)
        unrelated[0]['size_in_bytes'] += 1
        with self.assertRaises(RuntimeError): artifact_upload_budget(unrelated, new)

    def test_preserved_historical_exports_allow_three_worst_case_serialized_cycles(self):
        mib = 1024**2
        # Round the measured 103.875MiB of retained originals up conservatively;
        # six maximum-size uploads represent NFL+NHL for three half-hour cycles.
        original = {**artifact(1), 'size_in_bytes':104*mib}
        rows = [original, dict(name='other-preserved-artifacts', expired=False, size_in_bytes=9*mib)]
        new = 21*mib
        for run in range(2, 8):
            evidence = artifact_upload_budget(rows, new)
            self.assertEqual(evidence['peer_upload_reserve_bytes'], 21*mib)
            self.assertEqual(evidence['publisher_reserve_bytes'], 128*mib)
            self.assertEqual(evidence['maximum_repository_bytes'], 450*mib)
            rows.append({**artifact(run), 'size_in_bytes':new})
        self.assertIs(rows[0], original)
        self.assertEqual(original['size_in_bytes'], 104*mib)
        # Space remains bounded: a seventh maximum export must wait for verified
        # retention cleanup; admission never deletes the historical originals.
        with self.assertRaisesRegex(RuntimeError, 'preserve unconfirmed'):
            artifact_upload_budget(rows, new)
        self.assertEqual(len(rows), 8)

    def test_256mib_transport_partition_and_450mib_repository_limit_are_independent(self):
        mib = 1024**2
        new = 21*mib
        rows = [{**artifact(1), 'size_in_bytes':214*mib}]
        artifact_upload_budget(rows, new)  # 214 + new21 + peer21 = 256MiB.
        rows[0]['size_in_bytes'] += 1
        with self.assertRaises(RuntimeError): artifact_upload_budget(rows, new)
        rows = [{**artifact(1), 'size_in_bytes':104*mib},
                dict(name='other-preserved-artifacts', expired=False, size_in_bytes=176*mib)]
        artifact_upload_budget(rows, new)  # 104 + 176 + 21 + 21 + 128 = 450MiB.
        rows[1]['size_in_bytes'] += 1
        with self.assertRaises(RuntimeError): artifact_upload_budget(rows, new)

    def test_artifact_deletion_failure_aborts_cache_pruning_before_any_cache_delete(self):
        env = dict(GITHUB_REPOSITORY=REPO, GITHUB_REF='refs/heads/main', GITHUB_RUN_ID='99', GH_TOKEN='test')
        def inventory(path, key): return checkpoint_caches(1)+checkpoint_caches(2)+checkpoint_caches(3)
        with patch.dict(os.environ, env, clear=True), patch('tools.shared_capture_storage._inventory', inventory), \
             patch('tools.shared_capture_storage.prune_transport_artifacts', side_effect=ConnectionError('Delete unacknowledged')), \
             patch('tools.shared_capture_storage._delete') as delete:
            with self.assertRaises(ConnectionError): prune_shared_caches()
        delete.assert_not_called()

    def test_retained_artifacts_protect_all_attempts_versions_and_both_checkpoint_roles(self):
        caches = checkpoint_caches(1) + checkpoint_caches(1, attempt=2) + checkpoint_caches(2)
        caches += [{**checkpoint_caches(1)[0], 'version':'other-path'},
                   {**checkpoint_caches(1)[0], 'key':'ticketsignal-free-v1-state-nfl-shared-1-1'},
                   {**checkpoint_caches(1)[0], 'key':'ticketsignal-free-v1-state-nfl-1-1'}]
        rows = [artifact(1), {**artifact(2), 'expired':True}]
        protected = retained_artifact_cache_keys(rows, caches)
        self.assertEqual(protected, {row['key'] for row in caches if '-1-' in row['key']
                                     and row['key'] != 'ticketsignal-free-v1-state-nfl-1-1'})

    def test_failed_mirror_retains_artifact_and_exact_caches_for_a_future_recovery(self):
        env = dict(GITHUB_REPOSITORY=REPO, GITHUB_REF='refs/heads/main', GITHUB_RUN_ID='99', GH_TOKEN='test')
        rows = [artifact(i) for i in range(1, 5)]
        caches = [row for i in range(1, 5) for row in checkpoint_caches(i)]
        def inventory(path, key): return caches if key == 'actions_caches' else rows
        def api(path):
            if '/attempts/' in path:
                run = int(path.split('/')[3]); values = jobs(run, 1)
                if run == 1: values[1]['steps'][0]['conclusion'] = 'failure'
                return {'total_count':len(values), 'jobs':values}
            return provenance(int(path.rsplit('/',1)[1]))
        with patch.dict(os.environ, env, clear=True), patch('tools.shared_capture_storage._inventory', inventory), \
             patch('tools.shared_capture_storage.api', side_effect=api), \
             patch('tools.shared_capture_storage._delete') as delete, patch('sys.stdout', new_callable=StringIO):
            report = prune_shared_caches()
        self.assertEqual(delete.call_args_list[0].args, ('/actions/artifacts/2',))
        self.assertEqual(delete.call_count, 1)
        self.assertEqual(report['retained_artifact_checkpoint_keys_preserved'], 6)
        # Failed artifact1 and its exact checkpoints remain; artifacts3/4 are the
        # latest two, and the two spare cache generations remain conservative.

    def test_deleting_verified_artifacts_releases_only_their_cache_protection(self):
        env = dict(GITHUB_REPOSITORY=REPO, GITHUB_REF='refs/heads/main', GITHUB_RUN_ID='99', GH_TOKEN='test')
        rows = [artifact(i) for i in range(1, 7)]
        caches = [row for i in range(1, 7) for row in checkpoint_caches(i)]
        def inventory(path, key): return caches if key == 'actions_caches' else rows
        def api(path):
            if '/attempts/' in path:
                run = int(path.split('/')[3]); values = jobs(run, 1)
                return {'total_count':len(values), 'jobs':values}
            return provenance(int(path.rsplit('/',1)[1]))
        with patch.dict(os.environ, env, clear=True), patch('tools.shared_capture_storage._inventory', inventory), \
             patch('tools.shared_capture_storage.api', side_effect=api), \
             patch('tools.shared_capture_storage._delete') as delete, patch('sys.stdout', new_callable=StringIO):
            report = prune_shared_caches()
        paths = [call.args[0] for call in delete.call_args_list]
        self.assertEqual(set(paths[:4]), {'/actions/artifacts/'+str(i) for i in range(1,5)})
        self.assertEqual(set(paths[4:]), {'/actions/caches/'+str(row['id']) for i in (1,2) for row in checkpoint_caches(i)})
        self.assertEqual(report['retained_artifact_checkpoint_keys_preserved'], 4)
        self.assertEqual(report['provenance_runs_read'], 6)

    def test_workflow_preflight_checkpoint_and_cleanup_order_are_explicit(self):
        import yaml
        project = Path(__file__).resolve().parents[1]
        owner = yaml.load((project/'.github/workflows/collect-ticket-prices.yml').read_text(), Loader=yaml.BaseLoader)
        for sport in ('nfl', 'nhl'):
            steps = owner['jobs']['collect-'+sport]['steps']
            before = next(step for step in steps if step.get('id') == 'shared-upload-budget')
            self.assertIn('before-artifact-upload --sport '+sport, before['run'])
            self.assertEqual(before['if'], "always() && steps.shared-export.outcome == 'success'")
            upload = next(step for step in steps if step.get('name', '').startswith('Export only public'))
            self.assertEqual(upload['if'], "always() && steps.shared-upload-budget.outcome == 'success'")
            self.assertEqual(upload['with']['retention-days'], '90')
        storage = owner['jobs']['preserve-shared-cache-budget']
        self.assertEqual(storage['concurrency'], {'group':'shared-capture-storage-maintenance', 'cancel-in-progress':'false'})
        consumer = yaml.load((project/'.github/workflows/shared-snapshot-mirror.yml').read_text(), Loader=yaml.BaseLoader)
        steps = consumer['jobs']['mirror']['steps']
        verification = next(i for i, step in enumerate(steps) if step.get('id') == 'checkpoint')
        budget = next(i for i, step in enumerate(steps) if step.get('id') == 'budget')
        save = next(i for i, step in enumerate(steps) if step.get('name') == 'Preserve independent delivery acknowledgments and failures')
        self.assertLess(verification, budget); self.assertLess(budget, save)
        self.assertIn('verify-checkpoint', steps[verification]['run'])
        delivery = next(step for step in steps if step.get('name') == 'Deliver original observations without contacting Vivid')
        self.assertIn('cp bridge/tools/shared_capture_storage.py source/tools/shared_capture_storage.py', delivery['run'])


if __name__ == '__main__': unittest.main()
