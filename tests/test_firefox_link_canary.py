from pathlib import Path
import json
import signal
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools import firefox_inventory_diagnostic as diagnostic
from tools.firefox_link_canary import PLAN, capture_process, run_link_canary


class LinkCanaryTests(unittest.TestCase):
    def fake_capture(self, command, *, broken_pid=None, incomplete=False):
        def argument(name): return command[command.index(name) + 1]
        self.commands.append(command)
        pid = argument("--production-id")
        self.assertEqual(argument("--navigation"), "performer")
        self.assertEqual(argument("--timeout"), "75")
        self.assertNotIn("--headless", command)
        result = {"status": "captured", "production_id": pid, "navigation_mode": "performer",
                  "acquisition_method": "original-response-bidi", "browser_closed": True,
                  "visible_event_link_clicked": True, "event_page_reached": True, "quantity_actions": [],
                  "metadata_identity_match": True, "metadata_time_match": True,
                  "event_date": diagnostic.utc_stamp(argument("--expected-event-utc")).isoformat(),
                  "original_inventory_statuses": [200], "captured_listing_count": 1}
        payload = {"global": [{"productionId": broken_pid or pid, "listingCount": "2" if incomplete else "1"}],
                   "tickets": [{"l": "Section101", "p": "50.25", "q": "2"}]}
        Path(argument("--output")).write_text(json.dumps(result))
        Path(argument("--inventory-output")).write_text(json.dumps(payload))
        return 0

    def test_four_explicit_targets_repeat_first_and_preserve_separate_outputs(self):
        self.commands = []
        with tempfile.TemporaryDirectory() as tmp, patch("builtins.print"):
            report = run_link_canary(tmp, capture=self.fake_capture)
            saved = json.loads((Path(tmp) / "report.json").read_text())
            self.assertEqual(report["status"], "passed")
            self.assertEqual(len(list(Path(tmp).glob("*/inventory.json"))), 4)
            self.assertEqual(len(list(Path(tmp).glob("*/result.json"))), 4)
        self.assertEqual(saved["database_calls"], 0)
        self.assertTrue(saved["fresh_owned_process_per_capture"])
        self.assertEqual([row["production_id"] for row in saved["observations"]], ["7302493", "6493143", "7301789", "7302493"])
        self.assertEqual(PLAN[0], PLAN[3])
        self.assertEqual(len({command[-1] for command in self.commands}), 4)

    def test_failure_keeps_all_captures_and_cannot_hide_wrong_identity_or_partial_body(self):
        for defect in ("exit", "identity", "count"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as tmp, patch("builtins.print"):
                self.commands = []
                def capture(command):
                    index = len(self.commands)
                    result = self.fake_capture(command, broken_pid="999" if index == 1 and defect == "identity" else None,
                                               incomplete=index == 1 and defect == "count")
                    return 1 if index == 1 and defect == "exit" else result
                report = run_link_canary(tmp, capture=capture)
                self.assertEqual(report["status"], "failed")
                self.assertEqual(len(report["observations"]), 4)
                self.assertEqual([row["status"] for row in report["observations"]], ["captured", "failed", "captured", "captured"])
                self.assertEqual(len(list(Path(tmp).glob("*/inventory.json"))), 4)

    def test_existing_evidence_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "report.json"; p.write_text("preserve")
            with self.assertRaises(ValueError): run_link_canary(tmp, capture=Mock())
            self.assertEqual(p.read_text(), "preserve")

    def test_expired_child_cleans_only_its_new_process_group(self):
        process = Mock(pid=12345)
        process.wait.side_effect = [subprocess.TimeoutExpired("owned", 120), subprocess.TimeoutExpired("owned", 10), -9]
        with patch("tools.firefox_link_canary.subprocess.Popen", return_value=process) as launch, \
             patch("tools.firefox_link_canary.os.killpg") as kill:
            self.assertEqual(capture_process(["python", "diagnostic.py"]), 124)
        self.assertTrue(launch.call_args.kwargs["start_new_session"])
        self.assertEqual([call.args for call in kill.call_args_list], [(12345, signal.SIGTERM), (12345, signal.SIGKILL)])

    def test_nfl_metadata_uses_explicit_utc_and_keeps_slug_independent(self):
        target = PLAN[1]
        diagnostic.validate_target(target["event_url"], target["performer_url"], target["production_id"])
        self.assertIn("3-7-2027", target["event_url"])
        self.assertEqual(target["expected_event_utc"], "2026-10-11T17:00:00Z")
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(production_id=target["production_id"], expected_event_utc=target["expected_event_utc"],
                                   inventory_output=Path(tmp) / "inventory.json")
            driver = SimpleNamespace(execute_script=lambda _script: {"id": "6493143", "page_id": 6493143,
                                      "utc_date": target["expected_event_utc"], "title": "Saints", "venue": "Superdome"})
            raw = {"global": [{"productionId": "6493143", "listingCount": "1"}], "tickets": [{"l": "101", "p": "100.10", "q": "2"}]}
            report = {}
            self.assertTrue(diagnostic.save_capture(args, report, raw, "original-response-bidi", driver))
            self.assertEqual(report["event_date"], "2026-10-11T17:00:00+00:00")
            with self.assertRaises(ValueError): diagnostic.sanitize_inventory(raw, "7302493")


if __name__ == "__main__":
    unittest.main()
