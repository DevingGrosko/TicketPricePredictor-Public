from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import tempfile

from tools.schedule_capture_smoke import run


class ScheduleCaptureSmokeTests(unittest.TestCase):
    def test_one_official_game_uses_schedule_handler_and_records_recovery_failure(self):
        import nhl_schedule_collector as module
        game = SimpleNamespace(schedule_id="game", name="Utah Mammoth at Boston Bruins",
                               local_date=datetime.now(timezone.utc).date(),
                               event_date=datetime.now(timezone.utc) + timedelta(days=1))
        url = "https://www.vividseats.com/example/production/123"
        browser = Mock()
        evidence = {"production_id": "123", "document_status": 200,
                    "responses": [{"path": "/hermes/api/v1/listings", "status": 404}]}
        browser.capture_diagnostics = {**evidence, "headers": {"Cookie": "private-cookie"},
            "inventory_recovery": {"cooldown_seconds": 15, "recovered": False,
                "attempts": [{"attempt": 1, "diagnostics": evidence}, {"attempt": 2, "diagnostics": evidence}]}}

        def capture(resolution, **kwargs):
            self.assertEqual(len(resolution.candidates), 1)
            self.assertIs(resolution.game, game)
            self.assertFalse(kwargs["headless"])
            used = module.VividNFLBrowser(headless=False, timeout=45)
            used.close()
            raise RuntimeError("VividCaptureError: provider-inventory-not-found")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            with patch.object(module, "fetch_schedule_games", return_value=([game], ["official-source"])), \
                 patch.object(module, "VividNFLBrowser", return_value=browser) as factory, \
                 patch.object(module, "_capture_resolution", side_effect=capture) as handler:
                self.assertEqual(run("nhl", "game", url, 45, output), 1)
            payload = json.loads(output.read_text())
        factory.assert_called_once()
        handler.assert_called_once()
        self.assertEqual(payload["capture_browser_count"], 1)
        self.assertEqual(len(payload["capture_diagnostics"]["inventory_recovery"]["attempts"]), 2)
        self.assertEqual(payload["reported_exception_types"], ["VividCaptureError"])
        self.assertNotIn("private-cookie", json.dumps(payload))

    def test_unknown_official_game_fails_before_browser_creation(self):
        import nhl_schedule_collector as module
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            with patch.object(module, "fetch_schedule_games", return_value=([], ["official-source"])), \
                 patch.object(module, "VividNFLBrowser") as factory:
                self.assertEqual(run("nhl", "missing", "https://www.vividseats.com/example/production/123", 45, output), 1)
            self.assertEqual(json.loads(output.read_text())["phase"], "official-schedule")
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
