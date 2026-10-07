from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import Mock

from vivid_inventory import CurrentInventoryRecovery, VividCaptureError

NOW = datetime(2026, 10, 7, 2, tzinfo=timezone.utc)
URL = "https://www.vividseats.com/example/production/123"


def evidence(status=404, document=200):
    return {"production_id": "123", "document_status": document,
            "responses": [{"path": "/hermes/api/v1/listings", "status": status}],
            "headers": {"Authorization": "private-token"}, "body": "private-body"}


class Browser:
    def __init__(self, *statuses, document=200):
        self.statuses = iter(statuses)
        self.document = document
        self.calls = []
        self.capture_diagnostics = {}

    def capture(self, url, *, reload_page=False):
        self.calls.append((url, reload_page))
        status = next(self.statuses)
        self.capture_diagnostics = evidence(status, self.document)
        if status != 200:
            category = "provider-inventory-not-found" if status == 404 else "provider-access-denied"
            raise VividCaptureError(category, self.capture_diagnostics)
        return {"tickets": [1]}, NOW + timedelta(hours=24)


class CurrentRecoveryTests(unittest.TestCase):
    def recovery(self, hours=24, tier=168, now=None):
        self.sleep = Mock()
        return CurrentInventoryRecovery(NOW + timedelta(hours=hours), tier,
                                        now=now or (lambda: NOW), sleep=self.sleep)

    def test_404_recovers_with_one_same_browser_reload_and_safe_evidence(self):
        browser = Browser(404, 200)
        result = self.recovery().capture(browser, URL)
        self.assertTrue(result[0]["tickets"])
        self.assertEqual(browser.calls, [(URL, False), (URL, True)])
        self.sleep.assert_called_once_with(15)
        report = browser.capture_diagnostics["inventory_recovery"]
        self.assertTrue(report["recovered"])
        self.assertEqual([row["diagnostics"]["responses"][0]["status"] for row in report["attempts"]], [404, 200])
        self.assertNotIn("private", str(browser.capture_diagnostics))

    def test_second_404_fails_without_global_retry_or_a_third_attempt(self):
        browser = Browser(404, 404, 200)
        with self.assertRaises(VividCaptureError) as caught:
            self.recovery().capture(browser, URL)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(browser.calls), 2)
        self.assertFalse(caught.exception.diagnostics["inventory_recovery"]["recovered"])
        self.assertEqual(len(caught.exception.diagnostics["inventory_recovery"]["attempts"]), 2)
        self.assertNotIn("private", str(caught.exception))

    def test_denial_on_reload_stops_without_another_attempt(self):
        browser = Browser(404, 403, 200)
        with self.assertRaises(VividCaptureError) as caught:
            self.recovery().capture(browser, URL)
        self.assertEqual(caught.exception.category, "provider-access-denied")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(browser.calls), 2)
        self.sleep.assert_called_once_with(15)

    def test_tier_boundaries_and_future_requirement(self):
        for hours, tier, count in [(168, 168, 2), (72, 72, 2), (169, 168, 1),
                                   (73, 72, 1), (0, 168, 1), (-1, 72, 1)]:
            with self.subTest(hours=hours, tier=tier):
                browser = Browser(404, 200)
                recovery = self.recovery(hours, tier)
                if count == 2:
                    recovery.capture(browser, URL)
                else:
                    with self.assertRaises(VividCaptureError):
                        recovery.capture(browser, URL)
                    self.sleep.assert_not_called()
                self.assertEqual(len(browser.calls), count)

    def test_access_denials_and_non_200_documents_never_reload(self):
        for status, document in [(401, 200), (403, 200), (429, 200), (404, 403), (404, 404)]:
            with self.subTest(status=status, document=document):
                browser = Browser(status, 200, document=document)
                with self.assertRaises(VividCaptureError):
                    self.recovery().capture(browser, URL)
                self.assertEqual(len(browser.calls), 1)
                self.sleep.assert_not_called()

    def test_one_reload_cap_is_shared_across_candidate_urls(self):
        recovery = self.recovery()
        first, second = Browser(404, 404), Browser(404, 200)
        with self.assertRaises(VividCaptureError):
            recovery.capture(first, URL)
        with self.assertRaises(VividCaptureError):
            recovery.capture(second, URL)
        self.assertEqual(len(first.calls), 2)
        self.assertEqual(len(second.calls), 1)
        self.sleep.assert_called_once_with(15)

    def test_game_starting_during_cooldown_is_not_reloaded(self):
        clock = iter([NOW, NOW + timedelta(seconds=15)])
        browser = Browser(404, 200)
        with self.assertRaises(VividCaptureError):
            self.recovery(hours=10 / 3600, now=lambda: next(clock)).capture(browser, URL)
        self.assertEqual(len(browser.calls), 1)
        self.sleep.assert_called_once_with(15)

    def test_successful_first_attempt_is_not_reloaded(self):
        browser = Browser(200)
        self.recovery().capture(browser, URL)
        self.assertEqual(len(browser.calls), 1)
        self.sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
