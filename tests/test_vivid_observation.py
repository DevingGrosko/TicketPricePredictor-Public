import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tools.observe_vivid_inventory import Observation
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


if __name__ == "__main__":
    unittest.main()
