"""Offline shell orchestration tests; fake clients never contact a database."""
from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "tools/export_mysql_staging_source.sh"
CLIENT = r'''#!/usr/bin/env -S python3 -S
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
name = Path(sys.argv[0]).name
with open(os.environ['FAKE_CALLS'], 'a') as stream:
    stream.write(json.dumps({'name': name, 'args': args}) + '\n')
if name == 'mysql':
    query = next((s.split('=', 1)[1] for s in args if s.startswith('--execute=')), '')
    if 'SELECT VERSION()' in query:
        print('8.0.46')
    elif 'UNION ALL' in query:
        if os.environ.get('FAKE_OBSTACLE'):
            print('Non-InnoDB table or view: needs_review')
    elif 'COUNT(*)' in query:
        print('0' if os.environ.get('FAKE_EMPTY') else ('7' if '_mlb' in query else '6'))
    else:
        print('metadata-only')
else:
    if os.environ.get('FAKE_FAIL_DUMP') and '--no-data' not in args:
        print('-- partial dump output before failure')
        print('simulated client error', file=sys.stderr)
        sys.exit(2)
    print('CREATE TABLE `event` (`id` int, `name` varchar(20)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3 COLLATE=utf8mb3_general_ci;')
    if '--no-data' not in args:
        print("INSERT INTO `event` VALUES (1,'Montréal utf8mb3');")
'''


@unittest.skipUnless(all(shutil.which(c) for c in ('bash', 'gzip', 'tar', 'sha256sum', 'mktemp')), 'requires POSIX shell tools')
class SourceExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        (self.home / '.my.cnf').write_text('[client]\npassword=synthetic-test-only\n')
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name in ('mysql', 'mysqldump'):
            target = self.bin / name
            target.write_text(CLIENT)
            target.chmod(0o700)
        self.calls = self.root / 'calls.jsonl'
        self.env = dict(os.environ, HOME=str(self.home), PATH=f'{self.bin}:{os.environ["PATH"]}', FAKE_CALLS=str(self.calls))

    def run_script(self, **changes):
        return subprocess.run(['bash', str(SCRIPT)], env=dict(self.env, **changes), text=True, capture_output=True, timeout=15)

    def entries(self):
        return [json.loads(s) for s in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_success_generates_valid_compressed_copies_and_schema_only_review(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('EXPORT COMPLETE', result.stdout)
        out, = self.home.glob('ticketsignal-export.*')
        for sport in ('mlb', 'nfl', 'nhl'):
            text = gzip.decompress((out / f'{sport}.sql.gz').read_bytes()).decode()
            self.assertIn('Montréal utf8mb3', text)
            self.assertIn('utf8mb3_general_ci', text)
            self.assertNotIn('INSERT INTO', (out / f'{sport}.schema.sql').read_text())
        with tarfile.open(out / 'schema-review.tar.gz') as archive:
            names = archive.getnames()
            self.assertNotIn('.my.cnf', names)
            self.assertFalse(any(n.endswith('.sql.gz') for n in names))
            self.assertEqual(len([n for n in names if n.endswith('.schema.sql')]), 3)
        self.assertEqual(len((out / 'export-times.tsv').read_text().splitlines()), 7)
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)
        self.assertEqual((out / 'mlb.sql.gz').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('synthetic-test-only', result.stdout + result.stderr)
        check = subprocess.run(['sha256sum', '--check', 'SHA256SUMS.txt'], cwd=out, capture_output=True)
        self.assertEqual(check.returncode, 0)

    def test_dump_targets_are_fixed_and_source_locking_is_disabled(self):
        self.assertEqual(self.run_script().returncode, 0)
        dumps = [x['args'] for x in self.entries() if x['name'] == 'mysqldump']
        self.assertEqual(len(dumps), 6)
        for args in dumps:
            self.assertTrue(args[0].startswith('--defaults-file='))
            self.assertIn('--host=bunnyjeff.mysql.pythonanywhere-services.com', args)
            self.assertIn(args[-1], {f'bunnyjeff$ticketsignal_{s}' for s in ('mlb', 'nfl', 'nhl')})
            for option in ('--single-transaction', '--quick', '--no-tablespaces', '--set-gtid-purged=OFF', '--skip-lock-tables', '--skip-add-drop-table', '--skip-add-locks'):
                self.assertIn(option, args)
            for option in ('--all-databases', '--databases', '--lock-all-tables', '--source-data', '--delete-source-logs'):
                self.assertNotIn(option, args)
        queries = [next(a for a in x['args'] if a.startswith('--execute=')) for x in self.entries() if x['name'] == 'mysql']
        self.assertTrue(all(q.split('=', 1)[1].lstrip().startswith('SELECT') for q in queries))

    def test_missing_credentials_stop_without_calls_or_output_files(self):
        (self.home / '.my.cnf').unlink()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('saved MySQL credentials', result.stderr)
        self.assertEqual(self.entries(), [])
        self.assertEqual(list(self.home.glob('ticketsignal-export.*')), [])

    def test_preflight_obstacle_prevents_all_dumps(self):
        result = self.run_script(FAKE_OBSTACLE='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('needs migration review', result.stderr)
        self.assertFalse(any(x['name'] == 'mysqldump' for x in self.entries()))

    def test_empty_source_prevents_all_dumps(self):
        result = self.run_script(FAKE_EMPTY='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('could not confirm nonempty source schema', result.stderr)
        self.assertFalse(any(x['name'] == 'mysqldump' for x in self.entries()))

    def test_dump_failure_is_not_hidden_by_successful_gzip(self):
        result = self.run_script(FAKE_FAIL_DUMP='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('EXPORT COMPLETE', result.stdout)
        self.assertIn('Export stopped', result.stderr)
        out, = self.home.glob('ticketsignal-export.*')
        self.assertTrue((out / 'mlb.sql.gz.partial').exists())
        self.assertFalse((out / 'mlb.sql.gz').exists())
        self.assertFalse((out / 'SHA256SUMS.txt').exists())

    def test_second_export_does_not_overwrite_first(self):
        self.assertEqual(self.run_script().returncode, 0)
        before, = self.home.glob('ticketsignal-export.*')
        old = (before / 'mlb.sql.gz').read_bytes()
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(len(list(self.home.glob('ticketsignal-export.*'))), 2)
        self.assertEqual((before / 'mlb.sql.gz').read_bytes(), old)


if __name__ == '__main__':
    unittest.main()
