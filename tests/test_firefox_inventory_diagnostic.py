import base64
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "tools/firefox_inventory_diagnostic.py"
spec = importlib.util.spec_from_file_location("firefox_diagnostic", SOURCE)
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)
URL = "https://www.vividseats.com/hermes/api/v1/listings?productionId=7302493&quantity=0&priceGroupId=21"
PAYLOAD = {"global": [{"productionId": 7302493, "productionName": "Bruins", "listingCount": "1", "ipAddress": "PRIVATE-IP"}],
           "tickets": [{"l": "Loge1", "p": "99.5", "aip": "125.05", "q": 2, "r": "3", "sellerEmail": "PRIVATE-SELLER"}]}


class FakeNetwork:
    def __init__(self, driver, body_supported=True, status=200):
        self.driver, self.body_supported, self.status = driver, body_supported, status
        self.events, self.body_calls = [], []

    def add_event_handler(self, name, callback):
        self.events.append(name)
        self.callback = callback

    def add_data_collector(self, **kwargs):
        self.collector_options = kwargs
        if not self.body_supported:
            raise NotImplementedError("PRIVATE-ERROR")
        return {"collector": "local-test-collector"}

    def get_data(self, **kwargs):
        self.body_calls.append(kwargs)
        return {"bytes": {"type": "string", "value": json.dumps(PAYLOAD)}}


class FakeDriver:
    def __init__(self, body_supported=True, status=200, modal=False, filtered=False):
        self.command_executor = SimpleNamespace(_client_config=SimpleNamespace(timeout=None))
        self.capabilities = {"browserVersion": "1", "moz:geckodriverVersion": "2"}
        self.current_window_handle = "local-test-window"
        self.network = FakeNetwork(self, body_supported, status)
        self.fetches, self.navigations = 0, []
        self.modal = modal
        self.inventory_url = URL + "&quantity=2" if filtered else URL
        self.links = []
        self.current_url = "about:blank"
        self.window_handles = [self.current_window_handle]
        self.window_urls = {self.current_window_handle: self.current_url}
        self.switch_to = SimpleNamespace(window=self.switch_window)

    def switch_window(self, handle):
        assert handle in self.window_handles
        self.current_window_handle = handle
        self.current_url = self.window_urls[handle]

    def set_page_load_timeout(self, value): pass
    def set_script_timeout(self, value): pass
    def find_elements(self, *_): return self.links
    def quit(self): pass

    def get(self, url):
        self.current_url = url
        self.window_urls[self.current_window_handle] = url
        self.navigations.append(url)
        self.network.callback({"request": {"url": self.inventory_url, "request": "transient-request", "method": "GET",
                                          "headers": [{"name": "Cookie", "value": "PRIVATE-COOKIE"},
                                                      {"name": "if-none-match", "value": "PRIVATE-ETAG"}]},
                               "response": {"status": self.network.status, "protocol": "h3", "fromCache": False,
                                            "headers": [{"name": "cache-control", "value": "public,max-age=15"}]}})

    def execute_script(self, source):
        if source == diagnostic.DOM_SCRIPT:
            return {"ready_state": "complete", "listing_count": 1, "inventory_error": False,
                    "challenge_visible": False, "quantity_modal_visible": self.modal, "webdriver": True}
        if source == "return performance.timeOrigin;":
            return 123
        # Real Firefox can omit this request after its resource buffer fills.
        return [] if self.modal else [self.inventory_url]

    def execute_async_script(self, source, url):
        assert source == diagnostic.FETCH_SCRIPT and url == URL
        self.fetches += 1
        return {"status": 200, "payload": PAYLOAD}


class Link:
    def __init__(self, href, *, visible=True, enabled=True, target=None, driver=None, open_window=True, window_url=None):
        self.href, self.visible, self.enabled, self.target, self.driver = href, visible, enabled, target, driver
        self.clicks = 0
        self.open_window, self.window_url = open_window, window_url
    def get_attribute(self, name): return self.href if name == "href" else self.target
    def is_displayed(self): return self.visible
    def is_enabled(self): return self.enabled
    def click(self):
        self.clicks += 1
        if self.driver:
            self.driver.navigations.append("visible-event-link-click")
            if self.target == "_blank":
                if not self.open_window:
                    return
                self.driver.window_handles.append("new-event-window")
                self.driver.window_urls["new-event-window"] = self.window_url or diagnostic.EVENT
            else:
                self.driver.current_url = diagnostic.EVENT
            self.driver.network.callback({"request": {"url": self.driver.inventory_url, "request": "event-request", "method": "GET"},
                                          "response": {"status": self.driver.network.status}})


class FirefoxDiagnosticTests(unittest.TestCase):
    def test_accepts_only_exact_unfiltered_event_requests(self):
        self.assertTrue(diagnostic.unfiltered_url(URL))
        for suffix in ("&quantity=2", "&recommended=true", "&page=1", "&sf=1", "&pageSize=100", "&limit=500"):
            with self.subTest(suffix=suffix):
                self.assertFalse(diagnostic.unfiltered_url(URL + suffix))
        self.assertFalse(diagnostic.unfiltered_url(URL.replace("7302493", "123")))
        self.assertFalse(diagnostic.unfiltered_url(URL.replace("www.vividseats.com", "other.example")))

    def test_whitelist_preserves_all_listing_rows_without_private_fields(self):
        payload = json.loads(json.dumps(PAYLOAD))
        payload["tickets"] *= 20
        payload["global"][0]["listingCount"] = "20"
        clean = diagnostic.sanitize_inventory(payload)
        self.assertEqual(len(clean["tickets"]), 20)
        self.assertEqual(clean["tickets"][0]["p"], "99.5")
        self.assertEqual(clean["tickets"][0]["aip"], "125.05")
        self.assertNotIn("PRIVATE", json.dumps(clean))
        for bad in ({"global": [{"productionId": 99}], "tickets": payload["tickets"]},
                    {"global": payload["global"], "tickets": []},
                    {"global": payload["global"], "tickets": [{"l": "Loge1", "p": float("nan")}]},
                    {"global": payload["global"], "tickets": payload["tickets"] + [None]}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError): diagnostic.sanitize_inventory(bad)

    def test_bidi_event_redaction_and_denial(self):
        driver = FakeDriver()
        evidence = diagnostic.Evidence()
        driver.network.add_event_handler("response_completed", evidence.response)
        driver.get(diagnostic.EVENT)
        rows, raw, denial = evidence.snapshot()
        self.assertFalse(denial)
        self.assertNotIn("PRIVATE", json.dumps(rows))
        self.assertEqual(rows[0]["request_header_names"], ["if-none-match"])
        self.assertEqual(rows[0]["protocol"], "h3")
        evidence.response({"request": {"url": "https://www.vividseats.com/unrelated-auth-resource"}, "response": {"status": 403}})
        self.assertTrue(evidence.snapshot()[2])

    def test_original_body_then_single_observed_url_fallback(self):
        import selenium.webdriver
        import selenium.webdriver.firefox.service
        for supported, status, modal, filtered in ((True, 200, False, False), (False, 404, False, False),
                                                   (True, 403, False, False), (True, 200, True, False),
                                                   (True, 200, True, True)):
            with self.subTest(supported=supported, status=status, modal=modal, filtered=filtered), tempfile.TemporaryDirectory() as tmp:
                driver = FakeDriver(supported, status, modal, filtered)
                args = SimpleNamespace(output=Path(tmp) / "result.json", inventory_output=Path(tmp) / "inventory.json", timeout=75)
                report = {}
                clock = [0]
                def tick():
                    clock[0] += 1
                    return clock[0]
                with patch.object(selenium.webdriver, "Firefox", return_value=driver) as launch, \
                     patch.object(selenium.webdriver.firefox.service, "Service", return_value=object()), \
                     patch.object(diagnostic.shutil, "which", side_effect=lambda value: "/installed/" + value), \
                     patch.object(diagnostic, "binary_version", return_value="test1"), \
                     patch.object(diagnostic, "prepare_quantity", wraps=diagnostic.prepare_quantity) as controls, \
                     patch.object(diagnostic.time, "monotonic", side_effect=tick), \
                     patch.object(diagnostic.time, "sleep"):
                    success = diagnostic.run(args, report)
                self.assertEqual(driver.network.events, ["response_completed"])
                self.assertEqual(driver.navigations, [diagnostic.EVENT])
                self.assertTrue(launch.call_args.kwargs["options"].enable_bidi)
                self.assertEqual(launch.call_args.kwargs["options"].arguments, [])
                self.assertNotIn("PRIVATE", args.output.read_text())
                self.assertTrue(report["browser_closed"])
                if status == 403 or filtered:
                    self.assertFalse(success)
                    self.assertEqual(driver.fetches, 0)
                    self.assertFalse(args.inventory_output.exists())
                else:
                    self.assertTrue(success)
                    self.assertEqual(driver.fetches, 0 if supported else 1)
                    self.assertNotIn("PRIVATE", args.inventory_output.read_text())
                    self.assertEqual(report["original_inventory_statuses"], [status])
                    if supported:
                        controls.assert_not_called()
                        self.assertEqual(report["acquisition_method"], "original-response-bidi")
                        self.assertEqual(report["quantity_actions"], [])

    def test_bidi_string_and_base64_body(self):
        raw = json.dumps(PAYLOAD)
        for encoding, value in (("string", raw), ("base64", base64.b64encode(raw.encode()).decode())):
            self.assertEqual(diagnostic.decode_bidi_body({"bytes": {"type": encoding, "value": value}}), PAYLOAD)

    def test_visible_link_choice_is_exact_safe_and_bounded(self):
        driver = FakeDriver()
        wanted = Link(diagnostic.EVENT)
        driver.links = [Link(diagnostic.EVENT, visible=False), Link(diagnostic.EVENT, enabled=False),
                        Link(diagnostic.EVENT, target="_parent"), Link(diagnostic.EVENT + "?quantity=2"),
                        Link(diagnostic.EVENT.replace("www.vividseats.com", "other.example")),
                        Link(diagnostic.EVENT.replace("7302493", "123")), wanted]
        self.assertIs(diagnostic.visible_event_link(driver), wanted)
        self.assertEqual(wanted.clicks, 0)
        driver.links = [Link(diagnostic.EVENT, visible=False)] * 50 + [wanted]
        self.assertIsNone(diagnostic.visible_event_link(driver))
        blank = Link(diagnostic.EVENT, target="_blank")
        driver.links = [blank]
        self.assertIs(diagnostic.visible_event_link(driver), blank)

    def test_missing_performer_link_times_out_without_direct_navigation(self):
        driver = FakeDriver()
        evidence = diagnostic.Evidence()
        driver.network.callback = evidence.response
        report = {}
        with patch.object(diagnostic.time, "monotonic", side_effect=range(20)), patch.object(diagnostic.time, "sleep"):
            self.assertFalse(diagnostic.navigate_performer(driver, evidence, report, 4))
        self.assertEqual(driver.navigations, [diagnostic.PERFORMER])
        self.assertEqual(report["category"], "visible-event-link-not-found")

    def test_performer_click_uses_original_body_and_never_fetches_or_changes_quantity(self):
        import selenium.webdriver, selenium.webdriver.firefox.service
        for status, target in ((200, None), (404, None), (200, "_blank"), (404, "_blank")):
            with self.subTest(status=status, target=target), tempfile.TemporaryDirectory() as tmp:
                driver = FakeDriver(status=status, modal=True)
                link = Link(diagnostic.EVENT, driver=driver, target=target)
                driver.links = [link]
                args = SimpleNamespace(output=Path(tmp) / "result.json", inventory_output=Path(tmp) / "inventory.json", timeout=75, navigation="performer")
                report, clock = {}, [0]
                def tick(): clock[0] += 1; return clock[0]
                with patch.object(selenium.webdriver, "Firefox", return_value=driver), \
                     patch.object(selenium.webdriver.firefox.service, "Service", return_value=object()), \
                     patch.object(diagnostic.shutil, "which", side_effect=lambda value: "/installed/" + value), \
                     patch.object(diagnostic, "binary_version", return_value="test1"), \
                     patch.object(diagnostic, "prepare_quantity") as controls, \
                     patch.object(diagnostic.time, "monotonic", side_effect=tick), patch.object(diagnostic.time, "sleep"):
                    success = diagnostic.run(args, report)
                self.assertEqual(success, status == 200)
                self.assertEqual(driver.navigations, [diagnostic.PERFORMER, "visible-event-link-click"])
                self.assertEqual(link.clicks, 1)
                self.assertEqual(driver.fetches, 0)
                controls.assert_not_called()
                self.assertEqual(report["navigation_transition"], "new-window-document" if target else "client-side")
                self.assertEqual(report["event_document_response_count"], 0)
                self.assertNotIn("contexts", driver.network.collector_options)
                if target:
                    self.assertTrue(report["event_opened_new_window"])
                    self.assertEqual(driver.current_window_handle, "new-event-window")
                if status == 200:
                    self.assertEqual(len(driver.network.body_calls), 1)
                    self.assertEqual(driver.network.body_calls[0]["request"], "event-request")

    def test_blank_window_expiry_and_wrong_event_cannot_fallback_or_capture(self):
        for opened, url, category in ((False, None, "new-event-window-not-found"),
                                     (True, diagnostic.EVENT.replace("7302493", "123"), "new-window-event-identity-not-confirmed")):
            with self.subTest(opened=opened):
                driver, evidence, report = FakeDriver(), diagnostic.Evidence(), {}
                driver.network.callback = evidence.response
                link = Link(diagnostic.EVENT, driver=driver, target="_blank", open_window=opened, window_url=url)
                driver.links = [link]
                with patch.object(diagnostic.time, "monotonic", side_effect=range(30)), patch.object(diagnostic.time, "sleep"):
                    self.assertFalse(diagnostic.navigate_performer(driver, evidence, report, 20))
                self.assertEqual(link.clicks, 1)
                self.assertEqual(driver.navigations, [diagnostic.PERFORMER, "visible-event-link-click"])
                self.assertEqual(report["category"], category)
                self.assertEqual(driver.fetches, 0)


if __name__ == "__main__":
    unittest.main()
