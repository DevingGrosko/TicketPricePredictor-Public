import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tools.observe_vivid_inventory import Observation, initialize_homepage
from vivid_inventory import VividCaptureError


class ObservationTests(unittest.TestCase):
    def driver(self, status=404):
        event = {"message": json.dumps({"message": {"method": "Network.responseReceived",
            "params": {"timestamp": 42.0, "type": "Fetch", "response": {"status": status,
            "url": "https://www.vividseats.com/hermes/api/v1/listings?productionId=7302493&quantity=0&token=private"}}}})}
        return SimpleNamespace(get_log=Mock(return_value=[event]),
            execute_script=Mock(return_value={"ready_state": "complete", "inventory_error": True,
                "quantity_modal": False, "listings": None, "challenge": False}))

    def test_metadata_records_identity_timing_and_no_private_query_values(self):
        observation = Observation(self.driver())
        observation.active = True
        observation.get_log("performance")
        event = observation.events[0]
        self.assertEqual(event["query"], {"productionId": "7302493", "quantity": "0"})
        self.assertEqual(event["status"], 404)
        self.assertEqual(event["resource_type"], "Fetch")
        self.assertIn("network_seconds", event)
        self.assertNotIn("private", json.dumps(observation.events))
        self.assertTrue(observation.page_states[0]["inventory_error"])

    def test_denial_stops_observation_immediately(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                driver = self.driver(status)
                observation = Observation(driver)
                observation.active = True
                with self.assertRaises(VividCaptureError):
                    observation.get_log("performance")
                driver.execute_script.assert_not_called()

    def homepage_driver(self, status=200, challenge=False):
        driver = self.driver()
        driver.get = Mock()
        driver.get_log.return_value = [{"message": json.dumps({"message": {
            "method": "Network.responseReceived", "params": {"timestamp": 42,
            "type": "Document", "response": {"status": status,
            "url": "https://www.vividseats.com/"}}}})}]
        driver.execute_script.return_value["challenge"] = challenge
        return driver

    def test_homepage_initialization_requires_complete_successful_document(self):
        driver = self.homepage_driver()
        observed = Observation(driver)
        initialize_homepage(observed)
        driver.get.assert_called_once_with("https://www.vividseats.com/")
        self.assertEqual(observed.events[0]["phase"], "homepage")
        self.assertEqual(observed.homepage_document_status, 200)
        self.assertEqual(observed.reloads, 0)

    def test_homepage_denial_or_challenge_stops_before_event_navigation(self):
        for status, challenge in [(401, False), (403, False), (429, False), (200, True)]:
            with self.subTest(status=status, challenge=challenge):
                driver = self.homepage_driver(status, challenge)
                with self.assertRaises(VividCaptureError):
                    initialize_homepage(Observation(driver))
                driver.get.assert_called_once_with("https://www.vividseats.com/")

    def test_homepage_error_or_incomplete_document_is_rejected(self):
        for status, ready in [(404, "complete"), (200, "interactive")]:
            with self.subTest(status=status, ready=ready):
                driver = self.homepage_driver(status)
                driver.execute_script.return_value["ready_state"] = ready
                with self.assertRaises(VividCaptureError):
                    initialize_homepage(Observation(driver))


if __name__ == "__main__":
    unittest.main()
