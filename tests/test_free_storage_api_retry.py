"""Bounded storage API retry tests; no network or account changes."""
from contextlib import redirect_stdout
from http.client import IncompleteRead, RemoteDisconnected
import io
import json
import ssl
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from tools.free_refresh_storage import api


class Response:
    def __init__(self, content=b'{"ok":true}', error=None):
        self.content, self.error = content, error

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        if self.error:
            raise self.error
        return self.content


class StorageRetryTests(unittest.TestCase):
    def call(self, outcomes, method='GET'):
        output = io.StringIO()
        with patch.dict('os.environ', {'GH_TOKEN': 'private-test-token'}), \
             patch('tools.free_refresh_storage.urllib.request.urlopen', side_effect=outcomes) as opener, \
             patch('tools.free_refresh_storage.time.sleep') as sleep, redirect_stdout(output):
            result = api('/actions/cache/usage?secret=never-log-query', method)
        return result, opener, sleep, output.getvalue()

    def test_transport_and_read_failures_recover_with_bounded_delays(self):
        for error in (RemoteDisconnected('secret exception detail'), ConnectionResetError(),
                      TimeoutError(), URLError(TimeoutError()), IncompleteRead(b'private', 20)):
            with self.subTest(type=type(error).__name__):
                result, opener, sleep, output = self.call([error, Response()])
                self.assertEqual(result, {'ok': True})
                self.assertEqual(opener.call_count, 2)
                sleep.assert_called_once_with(1)
                self.assertIn('FREE_STORAGE_API_RETRY', output)
                self.assertNotIn('private-test-token', output)
                self.assertNotIn('secret exception detail', output)
                self.assertNotIn('never-log-query', output)
                self.assertEqual(opener.call_args.kwargs['timeout'], 30)
        result, opener, _sleep, _output = self.call([Response(error=RemoteDisconnected()), Response()])
        self.assertEqual((result, opener.call_count), ({'ok': True}, 2))

    def test_three_attempt_limit_is_hard(self):
        output = io.StringIO()
        with patch.dict('os.environ', {'GH_TOKEN': 'private-test-token'}), \
             patch('tools.free_refresh_storage.urllib.request.urlopen', side_effect=RemoteDisconnected()) as opener, \
             patch('tools.free_refresh_storage.time.sleep') as sleep, redirect_stdout(output):
            with self.assertRaises(RemoteDisconnected):
                api('/actions/cache/usage')
        self.assertEqual(opener.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 3])
        self.assertIn('FREE_STORAGE_API_STOP', output.getvalue())

    def test_server_errors_recover_but_permissions_and_not_found_do_not_retry(self):
        for code in (500, 502, 503, 504):
            error = HTTPError('https://example.invalid', code, 'private', {}, None)
            result, opener, _sleep, _output = self.call([error, Response()])
            self.assertEqual((result, opener.call_count), ({'ok': True}, 2))
        for method in ('GET', 'DELETE'):
            for code in (400, 401, 403, 404, 429):
                error = HTTPError('https://example.invalid', code, 'private', {}, None)
                with patch.dict('os.environ', {'GH_TOKEN': 'test'}), \
                     patch('tools.free_refresh_storage.urllib.request.urlopen', side_effect=error) as opener, \
                     patch('tools.free_refresh_storage.time.sleep') as sleep, redirect_stdout(io.StringIO()):
                    with self.assertRaises(HTTPError):
                        api('/pages', method)
                self.assertEqual(opener.call_count, 1)
                sleep.assert_not_called()

    def test_delete_retries_same_resource_and_accepts_missing_after_lost_acknowledgment(self):
        missing = HTTPError('https://example.invalid', 404, 'missing', {}, None)
        result, opener, sleep, _output = self.call([RemoteDisconnected(), missing], 'DELETE')
        self.assertIsNone(result)
        self.assertEqual(opener.call_count, 2)
        self.assertIs(opener.call_args_list[0].args[0], opener.call_args_list[1].args[0])
        self.assertEqual(opener.call_args.args[0].method, 'DELETE')
        sleep.assert_called_once_with(1)

    def test_non_idempotent_methods_and_certificate_errors_do_not_retry(self):
        for method, error in [('POST', RemoteDisconnected()),
                              ('GET', URLError(ssl.SSLCertVerificationError('invalid certificate')))]:
            with patch.dict('os.environ', {'GH_TOKEN': 'test'}), \
                 patch('tools.free_refresh_storage.urllib.request.urlopen', side_effect=error) as opener, \
                 patch('tools.free_refresh_storage.time.sleep') as sleep, redirect_stdout(io.StringIO()):
                with self.assertRaises(type(error)):
                    api('/pages', method)
            self.assertEqual(opener.call_count, 1)
            sleep.assert_not_called()

    def test_invalid_json_and_path_remain_hard_failures(self):
        with patch.dict('os.environ', {'GH_TOKEN': 'test'}), \
             patch('tools.free_refresh_storage.urllib.request.urlopen', return_value=Response(b'broken')) as opener, \
             patch('tools.free_refresh_storage.time.sleep') as sleep:
            with self.assertRaises(json.JSONDecodeError):
                api('/pages')
            self.assertEqual(opener.call_count, 1)
            with self.assertRaises(ValueError):
                api('//other-host')
            sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
