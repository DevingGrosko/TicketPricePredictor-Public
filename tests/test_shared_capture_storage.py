"""Offline shared-cache ownership and actual restored-byte budget checks."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from tools.shared_capture_storage import PROVENANCE_RUN_LIMIT, QUEUE_LIMIT, queue_budget, removable_generations
from tools.single_capture_owner import OWNER, REPO

NOW=datetime(2026,10,8,5,tzinfo=timezone.utc)


def cache(identity,sport='nfl',role='capture',version='path-version1',ref='refs/heads/main'):
    return {'id':identity,'key':f'shared-capture-v1-{sport}-{role}-{identity}-1','version':version,
            'ref':ref,'created_at':(NOW+timedelta(seconds=identity)).isoformat()}


def provenance(identity, **changes):
    return dict(id=identity, status='completed', path=OWNER, head_branch='main',
                repository={'full_name':REPO}, head_repository={'full_name':REPO}, **changes)


class SharedStorageTests(unittest.TestCase):
    def test_pagination_duplicates_do_not_count_as_two_distinct_cache_generations(self):
        rows = [cache(2), cache(2), cache(1), {**cache(2), 'id': 20}]
        remove = removable_generations(rows, provenance, current_run=99)
        self.assertEqual(remove, [])

    def test_two_completed_generations_each_sport_role_branch_and_path_version(self):
        rows=[cache(1),cache(2),cache(3),cache(4,role='tidb'),cache(5,role='tidb'),cache(6,role='tidb'),
              cache(7,sport='nhl'),cache(8,sport='nhl'),cache(9,sport='nhl'),
              cache(10,version='different-path'),cache(11,version='different-path'),cache(12,version='different-path'),
              cache(13,ref='refs/heads/other')]
        remove=removable_generations(rows,provenance,current_run=99)
        self.assertEqual({row['id'] for row in remove},{1,4,7,10})

    def test_active_current_unrelated_namespace_workflow_and_unknown_version_are_untouched(self):
        rows=[cache(1),cache(2),cache(3),cache(4),cache(5),cache(6),cache(7)]
        rows += [{'id':8,'key':'ticketsignal-free-v1-state-nhl-8-1','ref':'refs/heads/main'},
                 {**cache(9),'version':None}]
        def run(identity):
            return {**provenance(identity), 'status':'in_progress' if identity==4 else 'completed',
                    'path':'.github/workflows/other.yml' if identity==5 else OWNER}
        remove=removable_generations(rows,run,current_run=6)
        self.assertEqual({row['id'] for row in remove},{1,2})
        self.assertTrue(all(row['id'] not in {4,5,6,8,9} for row in remove))

    def test_queue_limit_measures_actual_files_and_does_not_delete_failed_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);body=root/'pending.json';receipt=root/'ack.json'
            body.write_bytes(b'payload');receipt.write_bytes(b'ack')
            self.assertEqual(queue_budget(root)['queue_bytes'],10)
            with body.open('wb') as output:output.truncate(QUEUE_LIMIT)
            with self.assertRaises(RuntimeError):queue_budget(root)
            self.assertTrue(body.exists() and receipt.exists())
            receipt.unlink()
            self.assertEqual(queue_budget(root)['queue_bytes'],QUEUE_LIMIT)
            alias=root/'symlink';alias.symlink_to(body)
            with self.assertRaises(RuntimeError):queue_budget(root)

    def test_migrated_free_delivery_keeps_two_and_never_prunes_original_free_caches(self):
        rows = [{**cache(i), 'key': f'ticketsignal-free-v1-state-nfl-shared-{i}-1'}
                for i in range(1, 9)]
        rows.extend([{**cache(9), 'key': 'ticketsignal-free-v1-state-nfl-9-1'},
                     {**cache(10), 'key': 'ticketsignal-free-v1-state-mlb-shared-10-1'},
                     {**cache(11), 'key': 'ticketsignal-free-v1-state-nfl-shared-invalid-1'}])
        rows[6]['version'] = 'another-cache-path'
        rows[7]['ref'] = 'refs/heads/unrelated'
        def run(identity):
            return {**provenance(identity), 'status': 'in_progress' if identity == 4 else 'completed',
                    'path': '.github/workflows/free-ticket-collect.yml' if identity == 5 else OWNER}
        remove = removable_generations(rows, run, current_run=6)
        self.assertEqual([row['id'] for row in remove], [1])

    def test_unknown_provenance_and_artifact_proof_are_not_pruned(self):
        rows = [cache(i) for i in range(1, 8)]
        def run(identity):
            value = provenance(identity)
            if identity == 1: value['head_repository'] = {'full_name':'other/repo'}
            if identity == 2: value['id'] = 999
            if identity == 3: value['head_branch'] = 'other'
            return value
        remove = removable_generations(rows, run, current_run=99,
                                       protected_keys={cache(4)['key']})
        self.assertEqual([row['id'] for row in remove], [5])

    def test_generation_provenance_reads_are_bounded_newest_first(self):
        seen = []
        def run(identity):
            seen.append(identity)
            return provenance(identity)
        count = PROVENANCE_RUN_LIMIT + 10
        remove = removable_generations([cache(i) for i in range(1, count+1)], run, current_run=999)
        self.assertEqual(len(seen), PROVENANCE_RUN_LIMIT)
        self.assertEqual(seen[:2], [count, count-1])
        self.assertFalse(any(row['id'] <= 10 for row in remove))


if __name__=='__main__':unittest.main()
