import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tools.observe_vivid_inventory import (
    Observation, initialize_homepage, sanitized_build_markers, sanitized_query_metadata,
)
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
        self.assertEqual(event["query_keys"], ["productionId", "quantity", "token"])
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


class SanitizerTests(unittest.TestCase):
    def test_price_group_identity_is_numeric_and_unknown_values_are_omitted(self):
        result = sanitized_query_metadata(
            "productionId=7302493&priceGroupId=21&includeIpAddress=true&currency=USD"
            "&localizeCurrency=true&token=private-value&_rsc=private-flight-value&empty=")
        self.assertEqual(result["query"], {"productionId": "7302493", "priceGroupId": "21",
            "includeIpAddress": "true", "currency": "USD", "localizeCurrency": "true"})
        self.assertIn("token", result["query_keys"])
        self.assertIn("_rsc", result["query_keys"])
        self.assertIn("empty", result["query_keys"])
        self.assertNotIn("private", json.dumps(result))

    def test_malformed_duplicate_and_non_numeric_price_groups_never_export_values(self):
        for value in ["true", "USD", "private", "-21", "21.5", "1" * 13, "21&priceGroupId=22"]:
            with self.subTest(value=value):
                result = sanitized_query_metadata("priceGroupId=" + value)
                self.assertNotIn("priceGroupId", result["query"])
                self.assertIn("priceGroupId", result["query_keys"])

    def test_query_key_names_are_bounded_and_non_identifiers_omitted(self):
        query = "&".join(f"key{i}=private" for i in range(80)) + "&%3Cprivate%3E=private"
        result = sanitized_query_metadata(query)
        self.assertEqual(len(result["query_keys"]), 64)
        self.assertEqual(result["query_key_count"], 81)
        self.assertNotIn("private", json.dumps(result))

    def test_public_build_fields_and_script_paths_exclude_private_state_and_query(self):
        path = "/athena-assets/d6790c45/prod/next-assets/_next/static/chunks/turbopack-public.js"
        raw = {"next_data_present": True, "next_data_valid": True, "next_f_present": False,
            "asset_prefix": "/athena-assets/d6790c45/prod/next-assets", "build_id": "public-build",
            "page": "/[slug]/production/[id]", "props": {"nonce": "private-state"},
            "script_sources": ["https://www.vividseats.com" + path + "?token=private-query",
                path, "https://other.example" + path, "/private-script.js?token=private-query"]}
        result = sanitized_build_markers(raw)
        self.assertEqual(result["athena_builds"], ["d6790c45"])
        self.assertEqual(result["script_paths"], [path])
        self.assertEqual(result["static_script_count"], 1)
        self.assertEqual(result["page"], "/[slug]/production/[id]")
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("props", result)

    def test_private_or_malformed_build_field_values_are_rejected(self):
        result = sanitized_build_markers({"asset_prefix": "https://other.example/private",
            "build_id": "private?token=secret", "page": "/private?token=secret",
            "next_data_present": "secret", "script_sources": []})
        self.assertNotIn("asset_prefix", result)
        self.assertNotIn("build_id", result)
        self.assertNotIn("page", result)
        self.assertNotIn("next_data_present", result)

    def test_observation_records_build_change_without_exporting_raw_state(self):
        driver = ObservationTests().driver()
        state = dict(driver.execute_script.return_value)
        driver.execute_script.side_effect = [
            {**state, "build": {"next_data_present": True, "build_id": identity,
                "script_sources": [], "props": {"token": "private"}}}
            for identity in ["public-build", "public-build", "next-build"]]
        observed = Observation(driver)
        observed.active = True
        for _ in range(3):
            observed.next_probe = 0
            observed.get_log("performance")
        self.assertEqual([row["build_id"] for row in observed.page_builds], ["public-build", "next-build"])
        self.assertNotIn("build", observed.page_states[0])
        self.assertNotIn("private", json.dumps(observed.page_builds))


if __name__ == "__main__":
    unittest.main()
