"""Offline shared-cache ownership and actual restored-byte budget checks."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from tools.shared_capture_storage import QUEUE_LIMIT, queue_budget, removable_generations
from tools.single_capture_owner import OWNER

NOW=datetime(2026,10,8,5,tzinfo=timezone.utc)


def cache(identity,sport='nfl',role='capture',version='path-version1',ref='refs/heads/main'):
    return {'id':identity,'key':f'shared-capture-v1-{sport}-{role}-{identity}-1','version':version,
            'ref':ref,'created_at':(NOW+timedelta(seconds=identity)).isoformat()}


class SharedStorageTests(unittest.TestCase):
    def test_two_completed_generations_each_sport_role_branch_and_path_version(self):
        rows=[cache(1),cache(2),cache(3),cache(4,role='tidb'),cache(5,role='tidb'),cache(6,role='tidb'),
              cache(7,sport='nhl'),cache(8,sport='nhl'),cache(9,sport='nhl'),
              cache(10,version='different-path'),cache(11,version='different-path'),cache(12,version='different-path'),
              cache(13,ref='refs/heads/other')]
        remove=removable_generations(rows,lambda _: {'status':'completed','path':OWNER},current_run=99)
        self.assertEqual({row['id'] for row in remove},{1,4,7,10})

    def test_active_current_unrelated_namespace_workflow_and_unknown_version_are_untouched(self):
        rows=[cache(1),cache(2),cache(3),cache(4),cache(5),cache(6),cache(7)]
        rows += [{'id':8,'key':'ticketsignal-free-v1-state-nhl-8-1','ref':'refs/heads/main'},
                 {**cache(9),'version':None}]
        def run(identity):
            return {'status':'in_progress' if identity==4 else 'completed',
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


if __name__=='__main__':unittest.main()
