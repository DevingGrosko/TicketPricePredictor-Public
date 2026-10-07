import base64
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from nfl_collector import VividNFLBrowser
from collector import VividBrowser
from nfl_schedule_collector import _retryable_capture_error
from vivid_inventory import InventoryView, VividCaptureError, inventory_request, read_inventory, unfiltered_request


URL = "https://www.vividseats.com/example/production/123"
API = "https://www.vividseats.com/hermes/api/v1/listings?productionId=123"
AT = datetime(2026, 10, 18, 17, tzinfo=timezone.utc)


def message(method, **params):
    return {"message": json.dumps({"message": {"method": method, "params": params}})}


def response(status=200, url=API, identity="inventory"):
    return message("Network.responseReceived", requestId=identity, type="XHR",
                   response={"url": url, "status": status, "mimeType": "application/json"})


def payload(production_id="123", tickets=None):
    return {"global": [{"productionId": production_id}],
            "tickets": [{"l": "Section 1", "p": 25}] if tickets is None else tickets}


class Driver:
    current_url = URL
    def __init__(self, batches, bodies):
        self.batches = iter([[], *batches])
        self.bodies = iter(bodies)
        self.reads = 0
        self.last = None

    def get_log(self, _kind):
        return next(self.batches, [])

    def get(self, _url):
        pass

    def find_elements(self, *_args):
        return []

    def execute_cdp_cmd(self, command, _params):
        if command == "Network.enable":
            return {}
        self.reads += 1
        item = next(self.bodies, self.last)
        self.last = item
        if isinstance(item, Exception):
            raise item
        return {"body": json.dumps(item)}


class CaptureTests(unittest.TestCase):
    def capture(self, batches, bodies):
        browser = VividNFLBrowser.__new__(VividNFLBrowser)
        browser.timeout = 35
        browser.driver = Driver(batches, bodies)
        browser._event_datetime = lambda _url: AT
        browser._looks_like_map_response = lambda *_args: False
        browser._dom_map_geometry = lambda *_args: None
        browser._open_map_view = lambda: False
        self.browser = browser
        self.clock = 0.0

        def sleep(seconds):
            self.clock += seconds

        with patch("nfl_collector.time.monotonic", side_effect=lambda: self.clock), \
             patch("nfl_collector.time.sleep", side_effect=sleep), \
             patch("nfl_collector.MAP_GEOMETRY_SETTLE_SECONDS", 0):
            return browser.capture(URL)

    def test_completed_body_is_retried_locally_without_another_request(self):
        result, stamp = self.capture([[response()], [message("Network.loadingFinished", requestId="inventory")]],
                                     [RuntimeError("Body not yet available"), payload()])
        self.assertEqual(stamp, AT)
        self.assertTrue(result["tickets"])
        self.assertEqual(self.browser.driver.reads, 2)
        self.assertEqual(self.browser.capture_diagnostics["body_read_retries"], 1)

    def test_http_404_is_explicit_nonretryable_and_bounded(self):
        with self.assertRaises(VividCaptureError) as caught:
            self.capture([[response(404)]], [])
        self.assertEqual(caught.exception.category, "provider-inventory-not-found")
        self.assertFalse(_retryable_capture_error(caught.exception))
        self.assertLess(self.clock, 6)
        self.assertEqual(self.browser.driver.reads, 0)

    def test_denial_and_rate_limit_stop_without_retry(self):
        for status in (401, 403, 429):
            with self.subTest(status=status), self.assertRaises(VividCaptureError) as caught:
                self.capture([[response(status)]], [])
            self.assertFalse(_retryable_capture_error(caught.exception))
            self.assertEqual(self.clock, 0)

    def test_provider_server_error_remains_retryable(self):
        with self.assertRaises(VividCaptureError) as caught:
            self.capture([[response(503)]], [])
        self.assertEqual(caught.exception.category, "provider-server-error")
        self.assertTrue(_retryable_capture_error(caught.exception))

    def test_wrong_event_response_cannot_be_stored(self):
        result, _ = self.capture([[response(identity="stale"), response(identity="current")]],
                                 [payload("999"), payload()])
        self.assertEqual(result["global"][0]["productionId"], "123")
        self.assertEqual(self.browser.capture_diagnostics["identity_responses_rejected"], 1)

    def test_quantity_subset_cannot_be_reported_as_whole_inventory(self):
        with self.assertRaises(VividCaptureError) as caught:
            self.capture([[response(url=API + "&quantity=2")]], [payload()])
        self.assertEqual(caught.exception.category, "filtered-inventory-only")
        self.assertEqual(self.browser.driver.reads, 0)

    def test_v2_unfiltered_response_and_missing_map_preserve_prices(self):
        result, _ = self.capture([[response(url=API.replace("/v1/", "/v2/") + "&recommended=false")]], [payload()])
        self.assertEqual(result["tickets"], payload()["tickets"])
        self.assertEqual(result["_map_geometry_diagnostics"]["status"], "unavailable")

    def test_badging_error_is_not_mistaken_for_inventory_failure(self):
        result, _ = self.capture([[response(403, "https://www.vividseats.com/hermes/api/v1/badging/productions/123/sold/listings"),
                                  response()]], [payload()])
        self.assertTrue(result["tickets"])
        self.assertEqual(len(self.browser.capture_diagnostics["responses"]), 1)

    def test_empty_inventory_is_not_a_success_or_timeout(self):
        with self.assertRaises(VividCaptureError) as caught:
            self.capture([[response()]], [payload(tickets=[])])
        self.assertEqual(caught.exception.category, "empty-inventory")
        self.assertFalse(caught.exception.retryable)


class RequestTests(unittest.TestCase):
    def test_only_provider_full_inventory_routes_are_recognized(self):
        self.assertTrue(inventory_request(API))
        self.assertFalse(inventory_request(API.replace("www.vividseats.com", "example.org")))
        self.assertFalse(inventory_request(API.split("?")[0] + "/top-deal"))

    def test_filtered_and_paginated_requests_are_rejected(self):
        for query in ("quantity=2", "recommended=true", "sf=true", "page=1", "limit=50", "pageSize=50"):
            self.assertFalse(unfiltered_request(API + "&" + query), query)
        self.assertTrue(unfiltered_request(API + "&quantity=0&recommended=false&currency=USD"))

    def test_base64_json_is_decoded(self):
        driver = SimpleNamespace(execute_cdp_cmd=lambda *_args: {
            "body": base64.b64encode(json.dumps(payload()).encode()).decode(), "base64Encoded": True})
        self.assertEqual(read_inventory(driver, "inventory"), payload())

    def test_driver_transport_timeout_is_set_after_client_configuration_exists(self):
        config = SimpleNamespace(timeout=120)
        driver = Mock(command_executor=SimpleNamespace(_client_config=config))
        def start_driver(**_kwargs):
            config.timeout = 120  # Chrome construction creates the configuration.
            return driver
        with patch("selenium.webdriver.Chrome", side_effect=start_driver):
            browser = VividBrowser(headless=True, timeout=7)
        self.assertIs(browser.driver, driver)
        self.assertEqual(config.timeout, 12)
        driver.set_page_load_timeout.assert_called_once_with(7)


class ViewTests(unittest.TestCase):
    def element(self, text, children=None):
        return SimpleNamespace(text=text, is_displayed=lambda: True, is_enabled=lambda: True,
                               click=lambda: self.clicks.append(text), find_elements=lambda *_args: children or [],
                               get_attribute=lambda _name: "")

    def test_quantity_modal_is_followed_by_all_quantity_filter(self):
        self.clicks = []
        two = self.element("2")
        dialog = self.element("How many tickets?", [two])
        view = InventoryView()
        driver = SimpleNamespace(current_url=URL, find_elements=lambda *_args: [dialog])
        view.prepare(driver)
        self.assertTrue(view.selected_quantity)
        clear = self.element("Clear")
        driver.find_elements = lambda _by, css: [clear] if "clear-filters-button" in css else []
        view.prepare(driver)
        self.assertEqual(self.clicks, ["2", "Clear"])
        self.assertFalse(view.selected_quantity)

    def test_existing_quantity_filter_is_cleared_without_opening_menu(self):
        self.clicks = []
        clear = self.element("Clear")
        driver = SimpleNamespace(current_url=URL + "?quantity=2",
                                 find_elements=lambda _by, css: [clear] if "clear-filters-button" in css else [])
        view = InventoryView()
        view.prepare(driver)
        self.assertEqual(self.clicks, ["Clear"])


if __name__ == "__main__":
    unittest.main()
