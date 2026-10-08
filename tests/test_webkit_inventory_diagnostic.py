import asyncio
import ast
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tools.webkit_inventory_diagnostic import Responses, TARGETS, event_url_matches, run_targets, validated_public_capture


def payload():
    return {"global": [{"productionId": "7302493", "productionName": "Utah Mammoth at Boston Bruins", "mapTitle": "TD Garden", "venueId": "573", "listingCount": "2"}],
            "tickets": [{"l": "Section101", "p": "35", "q": "2"}, {"l": "Section102", "p": "40", "q": "1"}]}


def metadata():
    return {"id": "7302493", "page_id": "7302493", "query_id": "7302493", "utc_date": "2026-10-08T23:00:00Z",
            "title": "Utah Mammoth at Boston Bruins", "venue": "TD Garden", "venue_id": "573"}


class CaptureTests(unittest.TestCase):
    def test_validation_requires_complete_matching_public_identity_date_and_quantities(self):
        validated_public_capture(payload(), metadata(), TARGETS[0])
        for field, value in (("id", "999"), ("page_id", "999"), ("utc_date", "2026-10-08T23:00:00"),
                             ("utc_date", "2026-10-09T23:00:00Z"), ("venue_id", "999")):
            data = metadata(); data[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validated_public_capture(payload(), data, TARGETS[0])
        for mutate in (lambda p: p["tickets"].pop(), lambda p: p["tickets"][0].update(q="0"),
                       lambda p: p["tickets"][0].update(p="NaN")):
            data = payload(); mutate(data)
            with self.assertRaises(ValueError):
                validated_public_capture(data, metadata(), TARGETS[0])

    def test_public_allowlist_redacts_unknown_fields(self):
        data = payload()
        data.update(token="private")
        data["global"][0].update(account="private")
        data["tickets"][0].update(email="private", nested={"secret": "private"})
        clean = validated_public_capture(data, metadata(), TARGETS[0])
        self.assertNotIn("private", json.dumps(clean))

    def test_observer_reads_only_matching_unfiltered_native_body_candidates_after_click(self):
        report = {"responses": []}; observer = Responses(TARGETS[0], report); observer.clicked_ms = 1000
        def response(suffix="", status=200, when=1001, pid="7302493", method="GET"):
            return SimpleNamespace(url="https://www.vividseats.com/hermes/api/v1/listings?productionId=" + pid + suffix, status=status,
                request=SimpleNamespace(method=method, resource_type="xhr", timing={"startTime": when}))
        for row in (response("&quantity=2"), response(status=404), response(when=999), response(pid="999"), response(method="POST"), response("&token=private")):
            observer.observe(row)
        self.assertTrue(observer.candidates.empty())
        observer.observe(response("&priceGroupId=21"))
        self.assertEqual(observer.candidates.qsize(), 1)
        self.assertNotIn("private", json.dumps(report))
        observer.observe(response(status=403))
        self.assertTrue(observer.denied)

    def test_only_public_exact_production_popup_url_is_allowed(self):
        self.assertTrue(event_url_matches(TARGETS[0]["event_url"], "7302493"))
        for url in ("https://other.example/production/7302493", "https://www.vividseats.com/production/73024930",
                    "https://user:private@www.vividseats.com/production/7302493", TARGETS[0]["event_url"] + "?quantity=2"):
            self.assertFalse(event_url_matches(url, "7302493"))

    def test_expected404_completes_honest_report_without_tooling_failure_or_extra_captures(self):
        async def failure(_playwright, _target, report, _path):
            report.update(phase="event", browser_closed=True)
            report["responses"] = [{"kind": "inventory", "phase": "event", "status": 404}]
            raise asyncio.TimeoutError()
        with tempfile.TemporaryDirectory() as directory, patch("tools.webkit_inventory_diagnostic.version", return_value="1.63.0"), \
                patch("tools.webkit_inventory_diagnostic.observe_target", side_effect=failure) as capture:
            result = asyncio.run(run_targets(None, directory))
        self.assertEqual(capture.call_count, 2)
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["all_captured"])
        self.assertEqual(result["tooling_errors"], 0)
        self.assertEqual([r["category"] for r in result["observations"]], ["provider-inventory-not-found"] * 2)

    def test_validation_crash_fails_tooling_and_stops_second_navigation_without_raw_error(self):
        with tempfile.TemporaryDirectory() as directory, patch("tools.webkit_inventory_diagnostic.version", return_value="1.63.0"), \
                patch("tools.webkit_inventory_diagnostic.observe_target", new=AsyncMock(side_effect=ValueError("private raw error"))) as capture:
            result = asyncio.run(run_targets(None, directory))
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(result["tooling_errors"], 1)
        self.assertEqual(result["observations"][1]["status"], "skipped")
        self.assertNotIn("private", json.dumps(result))

    def test_denial_stops_remaining_target_and_timeout_never_keeps_a_tentative_success(self):
        from tools.webkit_inventory_diagnostic import DiagnosticOutcome
        async def denied(_playwright, _target, report, _path):
            report['browser_closed'] = True
            raise DiagnosticOutcome('access-denial-or-challenge')
        with tempfile.TemporaryDirectory() as directory, patch('tools.webkit_inventory_diagnostic.version', return_value='1.63.0'), \
                patch('tools.webkit_inventory_diagnostic.observe_target', side_effect=denied) as capture:
            result = asyncio.run(run_targets(None, directory))
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(result['observations'][1]['category'], 'stopped-after-access-denial')
        self.assertFalse(result['all_captured'])
        async def interrupted(_playwright, _target, report, _path):
            report.update(status='captured', phase='event')
            raise asyncio.TimeoutError()
        with tempfile.TemporaryDirectory() as directory, patch('tools.webkit_inventory_diagnostic.version', return_value='1.63.0'), \
                patch('tools.webkit_inventory_diagnostic.observe_target', side_effect=interrupted):
            result = asyncio.run(run_targets(None, directory))
        self.assertFalse(result['all_captured'])
        self.assertTrue(all(row['status'] == 'failed' for row in result['observations']))

    def test_api_configuration_has_no_fingerprint_profile_header_request_or_state_overrides(self):
        source = Path(__file__).resolve().parents[1] / "tools/webkit_inventory_diagnostic.py"
        tree = ast.parse(source.read_text())
        forbidden = {"headers", "all_headers", "cookies", "storage_state", "route", "fetch", "set_extra_http_headers", "add_init_script", "launch_persistent_context"}
        self.assertFalse(any(isinstance(node, ast.Attribute) and node.attr in forbidden for node in ast.walk(tree)))
        launches = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "launch"]
        self.assertEqual(len(launches), 1)
        self.assertEqual([(kw.arg, ast.literal_eval(kw.value)) for kw in launches[0].keywords], [("headless", False)])
        contexts = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "new_context"]
        self.assertTrue(all(not node.args and not node.keywords for node in contexts))


if __name__ == "__main__":
    unittest.main()
