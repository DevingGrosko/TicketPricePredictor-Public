import base64
import json
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from tools.free_live_http_diagnostics import body_summary, safe_headers, TracedDriver, http_diagnostics


def event(method, **params):
    return {'message': json.dumps({'message': {'method': method, 'params': params}})}


class Driver:
    def __init__(self):
        self.entries = []
        self.body = {'body': '{"message":"Not Found"}'}
        self.reads = 0
        self.fail_read = False

    def get(self, url):
        self.url = url

    def get_log(self, kind):
        entries, self.entries = self.entries, []
        return entries

    def execute_cdp_cmd(self, cmd, params):
        self.reads += 1
        if self.fail_read:
            raise RuntimeError('sensitive body-read exception')
        return self.body


class HttpDiagnosticsTests(unittest.TestCase):
    def test_body_is_allowlisted_not_arbitrary_excerpt(self):
        raw = json.dumps({'message': 'Not Found token=superSecretCredential', 'email': 'private@example.org', 'status': 404})
        value = body_summary(raw)
        self.assertEqual(value['status'], 404)
        self.assertEqual(value['recognized_phrases'], ['not found'])
        self.assertNotIn('superSecret', json.dumps(value))
        self.assertNotIn('private@example', json.dumps(value))

    def test_base64_body(self):
        value = body_summary(base64.b64encode(b'{"error":"Access Denied"}').decode(), True)
        self.assertIn('access denied', value['recognized_phrases'])

    def test_html_classification_without_body(self):
        value = body_summary('<html>Request blocked by Imperva. secret=do-not-print</html>')
        self.assertEqual(value['format'], 'html')
        self.assertIn('imperva', value['recognized_phrases'])
        self.assertNotIn('do-not-print', json.dumps(value))

    def test_oversize(self):
        self.assertEqual(body_summary('x'*140000), {'body': 'omitted-size-limit'})

    def test_headers_never_return_credentials_or_request_ids(self):
        value = safe_headers({'Set-Cookie': 'secret', 'Authorization': 'secret', 'cf-ray': 'secret', 'Server': 'nginx/secret', 'Content-Type': 'application/json; secret', 'Age': '5', 'Cache-Control': 'private, max-age=10'})
        self.assertNotIn('secret', json.dumps(value))
        self.assertEqual(value['server'], ['nginx'])
        self.assertTrue(value['cf-ray-present'])

    def test_error_body_read_only_after_completion(self):
        driver = Driver(); proxy = TracedDriver(driver); proxy.get('https://www.vividseats.com/')
        driver.entries = [event('Network.requestWillBeSent', requestId='r', request={'url':'https://www.vividseats.com/hermes/api/v1/listings?productionId=123&token=secret', 'method':'GET'}), event('Network.responseReceived', requestId='r', response={'url':'https://www.vividseats.com/hermes/api/v1/listings', 'status':404, 'headers':{}})]
        original = list(driver.entries)
        self.assertEqual(proxy.get_log('performance'), original)
        self.assertEqual(driver.reads, 0)
        driver.entries = [event('Network.loadingFinished', requestId='r')]
        proxy.get_log('performance')
        self.assertEqual(driver.reads, 1)
        item = proxy.report()['requests'][0]
        self.assertEqual(item['query'], {'productionId':'123'})
        self.assertEqual(item['error_body']['recognized_phrases'], ['not found'])
        self.assertNotIn('secret', json.dumps(proxy.report()))

    def test_body_read_failure_retried_locally_and_bounded(self):
        driver = Driver(); proxy = TracedDriver(driver); proxy.active = True
        proxy.requests['r'] = {'status':404,'finished':True,'error_reads':0}
        driver.fail_read = True
        for _ in range(5): proxy.read_errors()
        self.assertEqual(driver.reads, 3)
        self.assertNotIn('sensitive', json.dumps(proxy.report()))

    def test_success_records_shape_not_prices_or_raw_body(self):
        driver = Driver(); proxy = TracedDriver(driver)
        proxy.requests['r'] = {'status':200,'error_reads':0}
        driver.body = {'body': json.dumps({'global':[{'productionId':123}], 'tickets':[{'secret': 'not-logged'}]})}
        self.assertIs(proxy.execute_cdp_cmd('Network.getResponseBody', {'requestId':'r'}), driver.body)
        self.assertEqual(proxy.requests['r']['success_body']['ticket_count'],1)
        self.assertNotIn('not-logged',json.dumps(proxy.report()))

    def test_ignores_unrelated_origins_and_paths(self):
        driver = Driver(); proxy = TracedDriver(driver); proxy.active = True
        driver.entries = [event('Network.responseReceived', requestId='r', response={'url':'https://evil.test/hermes/api/v1/listings','status':404}), event('Network.responseReceived', requestId='s', response={'url':'https://www.vividseats.com/account','status':404})]
        proxy.get_log('performance')
        self.assertEqual(proxy.requests,{})

    def test_driver_and_original_error_restored_even_if_final_logs_fail(self):
        recovery = ModuleType('tools.free_live_provider_recovery')
        driver = Driver(); browser = SimpleNamespace(driver=driver)
        original_error = ValueError('original capture failure')
        def capture(browser, url):
            self.assertIsInstance(browser.driver,TracedDriver)
            driver.get_log = lambda kind: (_ for _ in ()).throw(RuntimeError('diagnostic failure'))
            raise original_error
        recovery.capture = capture
        with patch.dict(sys.modules, {'tools.free_live_provider_recovery': recovery}):
            with http_diagnostics():
                with self.assertRaises(ValueError) as raised:
                    recovery.capture(browser,'https://www.vividseats.com/production/123')
            self.assertIs(raised.exception,original_error)
            self.assertIs(recovery.capture,capture)
            self.assertIs(browser.driver,driver)


if __name__ == '__main__':
    unittest.main()
