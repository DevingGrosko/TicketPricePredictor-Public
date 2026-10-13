"""Offline shared-cache ownership and actual restored-byte budget checks."""
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from tools.shared_capture_storage import PROVENANCE_RUN_LIMIT, QUEUE_LIMIT, _delete, _storage_request, api, queue_budget, removable_generations
from tools.single_capture_owner import API, OWNER, REPO

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


class StorageDeleteRetryTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.timeouts = []
        self.requests = []
        self.output = StringIO()
        for mocked in (
                patch.dict(os.environ, GITHUB_REPOSITORY=REPO, GITHUB_REF='refs/heads/main', GH_TOKEN='test-secret'),
                patch('tools.shared_capture_storage.time.monotonic', side_effect=lambda: self.now),
                patch('tools.shared_capture_storage.time.sleep', side_effect=self.advance),
                patch('sys.stdout', self.output)):
            mocked.start()
            self.addCleanup(mocked.stop)

    def advance(self, seconds):
        self.now += seconds

    def failure(self, code):
        body = BytesIO(b'private-response-secret')
        error = HTTPError(API + '/actions/caches/123', code, 'failure', {}, body)
        self.addCleanup(body.close)
        return error

    def opening(self, outcomes, elapsed=0):
        responses = iter(outcomes)
        def send(request, *, timeout):
            self.requests.append(request)
            self.timeouts.append(timeout)
            self.advance(elapsed)
            outcome = next(responses)
            if isinstance(outcome, Exception):
                raise outcome
            response = Mock(status=200 if isinstance(outcome, dict) else outcome, fp=None, length=None)
            response.read1 = Mock(side_effect=[json.dumps(outcome).encode(), b''])
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            return response
        return patch('urllib.request.urlopen', side_effect=send)

    def test_502_then_204_retries_same_verified_id_with_remaining_budget(self):
        error = self.failure(502)
        with self.opening([error, 204], elapsed=3):
            _delete('/actions/caches/123')
        self.assertEqual(self.timeouts, [20, 16.5])
        self.assertEqual([(r.method, r.full_url) for r in self.requests],
                         [('DELETE', API + '/actions/caches/123')] * 2)
        self.assertTrue(error.fp.closed)
        self.assertEqual(self.output.getvalue(), '')

    def test_uncertain_delete_then_404_is_success_for_same_scoped_id(self):
        with self.opening([self.failure(502), self.failure(404)]):
            _delete('/actions/artifacts/123')
        self.assertEqual([r.full_url for r in self.requests], [API + '/actions/artifacts/123'] * 2)
        self.assertEqual(len(self.requests), 2)

    def test_all_allowed_transient_http_errors_are_bounded_to_three_attempts(self):
        for code in (408, 500, 502, 503, 504):
            with self.subTest(code=code):
                self.requests.clear(); self.timeouts.clear(); self.now = 0
                with self.opening([self.failure(code) for _ in range(3)]):
                    with self.assertRaises(HTTPError) as raised:
                        _delete('/actions/caches/123')
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(self.timeouts, [20, 19.5, 18.5])
                self.assertEqual(len(self.requests), 3)

    def test_exhausted_deadline_does_not_sleep_or_repeat_delete(self):
        with self.opening([self.failure(502)], elapsed=19.75):
            with self.assertRaises(HTTPError):
                _delete('/actions/caches/123')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.now, 19.75)

    def test_auth_permanent_and_non_http_errors_remain_strict(self):
        errors = [self.failure(code) for code in (400, 401, 403, 422, 429)]
        errors += [URLError('unclassified network failure'), TimeoutError('transport timeout')]
        for error in errors:
            with self.subTest(error=type(error).__name__, status=getattr(error, 'code', None)):
                self.requests.clear()
                with self.opening([error]):
                    with self.assertRaises(type(error)):
                        _delete('/actions/caches/123')
                self.assertEqual(len(self.requests), 1)

    def test_unacknowledged_success_and_unscoped_paths_never_count_as_deleted(self):
        with self.opening([200]):
            with self.assertRaises(RuntimeError):
                _delete('/actions/caches/123')
        for path in ('/actions/caches', '/actions/caches/0', '/actions/caches/123?key=other',
                     '/actions/caches/123/../456', '/actions/runs/123', '//elsewhere/123'):
            with self.subTest(path=path), patch('urllib.request.urlopen') as send:
                with self.assertRaises(ValueError):
                    _delete(path)
                send.assert_not_called()
        with patch.dict(os.environ, GITHUB_REF='refs/heads/unrelated'), patch('urllib.request.urlopen') as send:
            with self.assertRaises(RuntimeError):
                _delete('/actions/caches/123')
            send.assert_not_called()

    def test_502_on_storage_inventory_provenance_or_jobs_retries_only_get(self):
        paths = ['/actions/artifacts?per_page=100&page=1', '/actions/caches?per_page=100&page=2',
                 '/actions/runs/123', '/actions/runs/123/attempts/1/jobs?per_page=100']
        for path in paths:
            with self.subTest(path=path):
                self.requests.clear(); self.timeouts.clear(); self.now = 0
                with self.opening([self.failure(502), {'id':123}], elapsed=3):
                    self.assertEqual(api(path), {'id':123})
                self.assertEqual(self.timeouts, [20, 16.5])
                self.assertEqual([(r.method, r.full_url) for r in self.requests], [('GET', API + path)] * 2)

    def test_get_404_and_dispatch_post_are_never_retried_or_accepted(self):
        with self.opening([self.failure(404)]):
            with self.assertRaises(HTTPError):
                api('/actions/runs/123')
        self.assertEqual(len(self.requests), 1)
        with patch('urllib.request.urlopen') as send:
            for path, method in [('/actions/runs/123', 'POST'),
                                 ('/actions/workflows/collect-ticket-prices.yml/dispatches', 'GET')]:
                with self.assertRaises(ValueError):
                    _storage_request(path, method)
            send.assert_not_called()

    def test_retry_after_on_transient_response_is_left_strict_without_early_retry(self):
        for value in ('120', 'Tue, 13 Oct 2026 17:00:00 GMT', 'malformed', '0'):
            with self.subTest(value=value):
                self.requests.clear(); error = self.failure(503); error.headers['Retry-After'] = value
                with self.opening([error]):
                    with self.assertRaises(HTTPError):
                        api('/actions/runs/123')
                self.assertEqual(len(self.requests), 1)

    def test_get_body_shares_deadline_and_refreshes_socket_timeout(self):
        socket = Mock()
        response = Mock(status=200, length=None)
        response.fp.raw._sock = socket
        def read_body(_size):
            self.advance(19)
            return b'{"id":123}'
        response.read1.side_effect = read_body
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        def opening(_request, *, timeout):
            self.advance(2)
            return response
        with patch('urllib.request.urlopen', side_effect=opening) as send:
            with self.assertRaises(TimeoutError):
                api('/actions/runs/123')
        socket.settimeout.assert_called_once_with(18)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(response.read1.call_count, 1)

    def test_invalid_or_oversized_json_is_permanent_and_cannot_drive_deletion(self):
        for body in (b'not-json', b'x' * 33):
            with self.subTest(body_length=len(body)):
                response = Mock(status=200, fp=None, length=None)
                response.read1.side_effect = [body, b'']
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                with patch('tools.shared_capture_storage.STORAGE_RESPONSE_LIMIT', 32), \
                     patch('urllib.request.urlopen', return_value=response) as send:
                    with self.assertRaises(ValueError):
                        api('/actions/runs/123')
                self.assertEqual(send.call_count, 1)


if __name__=='__main__':unittest.main()
