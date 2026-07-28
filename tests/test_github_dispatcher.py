import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from github_dispatcher import (
    DISPATCH_URL,
    capture_slot,
    dispatch_slot,
    dispatch_workflow,
    next_slot,
)


class FakeResponse:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def getcode(self):
        return self.status


class DispatcherTests(unittest.TestCase):
    def test_slots_are_exactly_thirty_minutes_apart(self):
        before_half_hour = datetime(2026, 7, 28, 0, 37, tzinfo=timezone.utc)
        after_half_hour = datetime(2026, 7, 28, 0, 39, tzinfo=timezone.utc)

        self.assertEqual(
            capture_slot(before_half_hour),
            datetime(2026, 7, 28, 0, 8, tzinfo=timezone.utc),
        )
        self.assertEqual(
            capture_slot(after_half_hour),
            datetime(2026, 7, 28, 0, 38, tzinfo=timezone.utc),
        )
        self.assertEqual(
            next_slot(after_half_hour),
            datetime(2026, 7, 28, 1, 8, tzinfo=timezone.utc),
        )

    def test_dispatch_uses_scoped_bearer_token_and_main_branch(self):
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["method"] = request.method
            captured["authorization"] = request.get_header("Authorization")
            captured["payload"] = json.loads(request.data)
            captured["timeout"] = timeout
            return FakeResponse()

        dispatch_workflow("secret-token", opener=opener)

        self.assertEqual(captured["url"], DISPATCH_URL)
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["authorization"], "Bearer secret-token")
        self.assertEqual(captured["payload"]["ref"], "main")
        self.assertEqual(
            captured["payload"]["inputs"]["dispatch_source"],
            "pythonanywhere",
        )

    def test_restart_does_not_dispatch_the_same_slot_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            calls = []

            def opener(_request, timeout):
                self.assertEqual(timeout, 20)
                calls.append(True)
                return FakeResponse()

            slot = datetime(2026, 7, 28, 0, 38, tzinfo=timezone.utc)
            first = dispatch_slot(
                "secret-token",
                slot,
                state_file=state_file,
                opener=opener,
                sleep=lambda _seconds: None,
            )
            second = dispatch_slot(
                "secret-token",
                slot,
                state_file=state_file,
                opener=opener,
                sleep=lambda _seconds: None,
            )

            self.assertTrue(first)
            self.assertFalse(second)
            self.assertEqual(len(calls), 1)
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "healthy")
            self.assertEqual(state["last_dispatch_slot"], slot.isoformat())


if __name__ == "__main__":
    unittest.main()
