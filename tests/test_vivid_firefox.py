from datetime import datetime, timezone
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from collector import VividBrowser
from nfl_collector import VividNFLBrowser
from vivid_firefox import EVENT_METADATA_SCRIPT, FirefoxInventorySession, event_datetime, native_unfiltered_request, validate_full_inventory
from vivid_inventory import VividCaptureError

URL = "https://www.vividseats.com/date-slug-is-not-metadata-3-5-2027/production/123"
AT = datetime(2026, 10, 11, 17, tzinfo=timezone.utc)


def payload(pid="123", price="25"):
    return {"global": [{"productionId": pid, "listingCount": "12"}],
            "tickets": [{"l": f"Section{i}", "p": price, "aip": "30.10", "r": "1", "q": "2"} for i in range(12)]}


class Network:
    def __init__(self, driver):
        self.driver = driver
        self.callbacks, self.removed_handlers, self.collectors, self.removed_collectors = [], [], [], []
        self.bodies, self.reads = {}, []
        self.once_unavailable = False

    def add_data_collector(self, **kwargs):
        self.collectors.append(kwargs)
        return {"collector": f"collector{len(self.collectors)}"}

    def add_event_handler(self, kind, callback):
        assert kind == "response_completed"
        self.callbacks.append(callback)
        self.current = callback
        return len(self.callbacks)

    def remove_event_handler(self, kind, callback_id):
        self.removed_handlers.append(callback_id)

    def remove_data_collector(self, collector):
        self.removed_collectors.append(collector)

    def get_data(self, **kwargs):
        self.reads.append(kwargs)
        if self.once_unavailable:
            self.once_unavailable = False
            raise RuntimeError("Transient local read")
        return {"bytes": {"type": "string", "value": json.dumps(self.bodies[kwargs["request"]])}}


class Driver:
    current_window_handle = "context1"
    capabilities = {"browserVersion": "156.0", "moz:geckodriverVersion": "0.37.1"}
    def __init__(self, status=200, query="", wrong_payload=False, no_events=False):
        self.command_executor = SimpleNamespace(_client_config=SimpleNamespace(timeout=0))
        self.service = Mock()
        self.network = Network(self)
        self.status, self.query, self.wrong_payload, self.no_events = status, query, wrong_payload, no_events
        self.navigations, self.quits, self.sequence = [], 0, 0
        self.metadata_wrong = False
        self.stale_events = False
        self.raise_quit = False
        self.unrelated_status = None

    def set_page_load_timeout(self, _value): pass
    def set_script_timeout(self, _value): pass

    def event(self, pid, request_id, *, status=None, when=1001, price="25"):
        url = "https://www.vividseats.com/hermes/api/v1/listings?productionId=" + pid + self.query
        self.network.bodies[request_id] = payload("999" if self.wrong_payload else pid, price)
        return {"request": {"url": url, "request": request_id, "method": "GET", "timings": {"requestTime": when},
                            "headers": [{"name": "Cookie", "value": "PRIVATE-COOKIE"}, {"name": "if-none-match", "value": "PRIVATE-ETAG"}]},
                "response": {"status": self.status if status is None else status, "mimeType": "application/json", "protocol": "h3",
                             "headers": [{"name": "content-type", "value": "application/json"}]}}

    def get(self, url):
        self.current_url = url
        self.navigations.append(("get", url))
        self.sequence += 1
        pid = url.rstrip("/").split("/")[-1]
        if self.no_events:
            return
        if self.stale_events:
            old = self.event(pid, "stale-old-time", when=999, price="1")
            self.network.current(old)
            if len(self.network.callbacks) > 1:
                self.network.callbacks[0](self.event(pid, "stale-old-generation", when=1001, price="2"))
        self.network.current({"navigation": "document-navigation", "request": {"url": url, "request": "document", "timings": {"requestTime": 1001}},
                              "response": {"status": 200}})
        if self.unrelated_status is not None:
            self.network.current(self.event("999", "unrelated-prefetch", status=self.unrelated_status))
        self.network.current(self.event(pid, f"inventory{self.sequence}", price=str(20 + self.sequence)))

    def refresh(self):
        previous = self.current_url
        self.get(previous)
        self.navigations[-1] = ("refresh", previous)

    def execute_script(self, script, *_args):
        assert script == EVENT_METADATA_SCRIPT
        pid = self.current_url.rstrip("/").split("/")[-1]
        return {"id": "999" if self.metadata_wrong else pid, "page_id": pid, "utc_date": AT.isoformat()}

    def find_elements(self, *_args):
        raise AssertionError("Capture must not operate quantity UI to accept a full native response")

    def quit(self):
        self.quits += 1
        if self.raise_quit:
            raise RuntimeError("quit failed")


class FirefoxTests(unittest.TestCase):
    def browser(self, driver):
        browser = VividNFLBrowser.__new__(VividNFLBrowser)
        browser.timeout = 1
        browser.driver = driver
        browser._looks_like_map_response = lambda _url, _mime: False
        browser._dom_map_geometry = lambda *_args: None
        browser._open_map_view = lambda: False
        session = FirefoxInventorySession.__new__(FirefoxInventorySession)
        import threading
        session.owner, session.timeout = browser, 1
        session._generation, session._lock = 0, threading.Lock()
        session.network, session.runtime = driver.network, {"engine": "firefox"}
        browser._firefox_session = session
        return browser

    def capture(self, browser, url=URL, **kwargs):
        clock = [0]
        def sleep(value): clock[0] += value
        with patch("vivid_firefox.time.time", return_value=1), \
             patch("vivid_firefox.time.monotonic", side_effect=lambda: clock[0]), \
             patch("vivid_firefox.time.sleep", side_effect=sleep), \
             patch("vivid_firefox.MAP_SETTLE_SECONDS", 0):
            return browser.capture(url, **kwargs)

    def test_full_original_response_ignores_quantity_overlay_and_bad_date_slug(self):
        driver = Driver()
        browser = self.browser(driver)
        raw, stamp = self.capture(browser)
        self.assertEqual(stamp, AT)
        self.assertEqual(len(raw["tickets"]), 12)
        self.assertEqual(raw["tickets"][0]["aip"], "30.10")
        self.assertEqual(raw["_map_geometry_diagnostics"]["status"], "unavailable")
        self.assertEqual(len(driver.navigations), 1)
        self.assertEqual(len(driver.network.reads), 1)
        self.assertNotIn("PRIVATE", json.dumps(browser.capture_diagnostics))
        self.assertEqual(browser.capture_diagnostics["responses"][0]["request_header_names"], ["if-none-match"])
        self.assertEqual(driver.network.removed_handlers, [1])
        self.assertEqual(driver.network.removed_collectors, ["collector1"])

    def test_different_games_and_same_game_repetition_reject_previous_epoch(self):
        driver = Driver()
        driver.stale_events = True
        browser = self.browser(driver)
        for index, pid in enumerate(("123", "456", "123"), 1):
            raw, _ = self.capture(browser, URL.rsplit("/", 1)[0] + "/" + pid)
            self.assertEqual(raw["global"][0]["productionId"], pid)
            self.assertEqual(raw["tickets"][0]["p"], str(20 + index))
        self.assertEqual(len(driver.network.removed_collectors), 3)
        self.assertEqual(len(driver.network.reads), 3)

    def test_filtered_200_wrong_identity_missing_metadata_and_timeout_fail_closed(self):
        cases = [(Driver(query="&quantity=2"), "filtered-inventory-only"),
                 (Driver(wrong_payload=True), "provider-inventory-timeout"),
                 (Driver(no_events=True), "provider-inventory-timeout")]
        wrong_metadata = Driver()
        wrong_metadata.metadata_wrong = True
        cases.append((wrong_metadata, "event-metadata-timeout"))
        for driver, category in cases:
            with self.subTest(category=category):
                browser = self.browser(driver)
                with self.assertRaises(VividCaptureError) as caught:
                    self.capture(browser)
                self.assertEqual(caught.exception.category, category)
                self.assertEqual(len(driver.network.removed_handlers), 1)
                self.assertEqual(len(driver.network.removed_collectors), 1)
                if driver.query:
                    self.assertEqual(driver.network.reads, [])

    def test_local_body_read_retries_and_normal_refresh_keep_same_driver(self):
        driver = Driver()
        driver.network.once_unavailable = True
        browser = self.browser(driver)
        self.capture(browser)
        self.assertEqual(len(driver.network.reads), 2)
        self.assertEqual(browser.capture_diagnostics["body_read_retries"], 1)
        self.capture(browser, reload_page=True)
        self.assertEqual(driver.navigations[-1][0], "refresh")

    def test_denials_and_404_preserve_failure_categories_and_cleanup(self):
        for status, category in ((401, "provider-access-denied"), (403, "provider-access-denied"),
                                 (429, "provider-rate-limited"), (404, "provider-inventory-not-found"), (503, "provider-server-error")):
            with self.subTest(status=status):
                browser = self.browser(Driver(status=status))
                with self.assertRaises(VividCaptureError) as caught:
                    self.capture(browser)
                self.assertEqual(caught.exception.category, category)
                self.assertEqual(caught.exception.retryable, status == 503)
                self.assertEqual(len(browser.driver.network.removed_collectors), 1)

    def test_other_production_prefetch_failures_cannot_fail_current_game(self):
        for status in (401, 403, 404, 429):
            with self.subTest(status=status):
                driver = Driver()
                driver.unrelated_status = status
                browser = self.browser(driver)
                raw, _ = self.capture(browser)
                self.assertEqual(raw["global"][0]["productionId"], "123")
                self.assertEqual(browser.capture_diagnostics["unrelated_inventory_responses_ignored"], 1)
                self.assertEqual(len(browser.capture_diagnostics["responses"]), 1)

    def test_full_count_and_identity_are_required(self):
        validate_full_inventory(payload(), "123")
        for count in (11, "13", True, "12.0", None):
            value = payload()
            value["global"][0]["listingCount"] = count
            with self.subTest(count=count), self.assertRaises(VividCaptureError):
                validate_full_inventory(value, "123")
        for suffix in ("&quantity=2", "&quantity=", "&recommended=true", "&page=1", "&limit=100",
                       "&section=101", "&quantity=0&quantity=0", "&priceGroupId=21&priceGroupId=21", "&scarcity=true"):
            self.assertFalse(native_unfiltered_request("https://www.vividseats.com/hermes/api/v1/listings?productionId=123" + suffix, "123"))
        self.assertTrue(native_unfiltered_request("https://www.vividseats.com/hermes/api/v2/listings?productionId=123&quantity=0&recommended=false&sf=false&currency=USD&priceGroupId=21&includeIpAddress=true&localizeCurrency=true", "123"))
        for field, bad in (("p", "not-a-price"), ("p", "NaN"), ("p", True), ("l", " "), ("q", "0"), ("q", "two")):
            value = payload()
            value["tickets"][0][field] = bad
            with self.subTest(field=field, bad=bad), self.assertRaises(VividCaptureError):
                validate_full_inventory(value, "123")

    def test_utc_metadata_requires_matching_page_identity_and_explicit_offset(self):
        for data in ({"id": "123", "page_id": "456", "utc_date": AT.isoformat()},
                     {"id": "123", "page_id": "123", "utc_date": "2026-10-11T17:00:00"},
                     {"id": "456", "page_id": "123", "utc_date": AT.isoformat()}):
            driver = SimpleNamespace(execute_script=lambda _script: data)
            self.assertIsNone(event_datetime(driver, "123"))

    def test_failed_driver_start_stops_partially_created_service(self):
        import selenium, selenium.webdriver, selenium.webdriver.firefox.service
        service = Mock()
        with patch.object(selenium, "__version__", "4.50.0"), \
             patch.object(selenium.webdriver, "Firefox", side_effect=RuntimeError("start failed")), \
             patch.object(selenium.webdriver.firefox.service, "Service", return_value=service), \
             patch("vivid_firefox.shutil.which", side_effect=lambda name: "/installed/" + name):
            with self.assertRaises(RuntimeError):
                FirefoxInventorySession(SimpleNamespace(), headless=False, timeout=1)
        service.stop.assert_called_once()

    def test_context_manager_stops_service_even_when_quit_fails(self):
        browser = self.browser(Driver())
        with browser as value:
            self.assertIs(value, browser)
        self.assertEqual(browser.driver.quits, 1)
        browser.driver.service.stop.assert_called_once()
        browser.driver.raise_quit = True
        with self.assertRaises(RuntimeError):
            browser.close()
        self.assertEqual(browser.driver.service.stop.call_count, 2)

    def test_opt_in_retains_class_method_wrappers_and_default_chrome(self):
        import selenium, selenium.webdriver, selenium.webdriver.firefox.service
        driver = Driver()
        with patch.dict(os.environ, {"TICKETSIGNAL_BROWSER_ENGINE": "firefox"}), \
             patch.object(selenium, "__version__", "4.50.0"), \
             patch.object(selenium.webdriver, "Firefox", return_value=driver) as launch, \
             patch.object(selenium.webdriver.firefox.service, "Service", return_value=Mock()), \
             patch("vivid_firefox.shutil.which", side_effect=lambda name: "/installed/" + name):
            browser = VividNFLBrowser(timeout=1)
        self.assertIsInstance(browser, VividNFLBrowser)
        self.assertEqual(launch.call_args.kwargs["options"].arguments, [])
        self.assertTrue(launch.call_args.kwargs["options"].enable_bidi)
        original = VividNFLBrowser.capture
        with patch.object(VividNFLBrowser, "capture", new=lambda owner, url: original(owner, url)):
            # Free recovery captures the original method before patching it.
            raw, _ = self.capture(browser)
        self.assertTrue(raw["tickets"])
        def chrome_init(owner, **_kwargs): owner.driver = Mock()
        with patch.dict(os.environ, {}, clear=True), patch.object(VividBrowser, "__init__", chrome_init), \
             patch("vivid_firefox.FirefoxInventorySession") as firefox:
            chrome = VividNFLBrowser()
        self.assertEqual(chrome.browser_engine, "chrome")
        firefox.assert_not_called()


if __name__ == "__main__":
    unittest.main()
