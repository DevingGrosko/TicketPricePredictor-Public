"""Offline regression tests for the production maintenance workflow client."""

import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "analytics_maintenance_runner",
    ROOT / ".github/scripts/maintain_materialized_analytics.py",
)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class Response(io.BytesIO):
    status = 200

    def __init__(self, result):
        body = result if isinstance(result, bytes) else json.dumps(result).encode()
        super().__init__(body)


def result(sport="mlb", *, complete=True, events=0, teams=0):
    return {
        "status": "ok", "sport": sport, "complete": complete,
        "remaining": events, "team_reports_remaining": teams,
    }


def http_error(code):
    return HTTPError(runner.ENDPOINT, code, "test", {}, io.BytesIO(b"private body"))


class MaintenanceRunnerTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.delays = []
        self.messages = []
        self.open_request = Mock()

    def sleep(self, seconds):
        self.delays.append(seconds)
        self.now += seconds

    def run_client(self, responses, **kwargs):
        self.open_request.side_effect = responses
        return runner.run_maintenance(
            "test-token", sports=kwargs.pop("sports", ("mlb",)),
            open_request=self.open_request, clock=lambda: self.now,
            sleep=self.sleep, log=self.messages.append, **kwargs,
        )

    def test_200_incomplete_drains_events_then_team_reports(self):
        completed = self.run_client([
            Response(result(complete=False, events=2)),
            Response(result(complete=False, teams=2)),
            Response(result(complete=False, teams=1)),
            Response(result()),
        ])
        self.assertTrue(completed["mlb"]["complete"])
        self.assertEqual(self.open_request.call_count, 4)
        for call in self.open_request.call_args_list:
            request = call.args[0]
            self.assertEqual(json.loads(request.data), {"sport": "mlb", "limit": 1})
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.full_url, runner.ENDPOINT)
            self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
            self.assertEqual(call.kwargs["timeout"], 90)
        self.assertEqual(self.delays, [])

    def test_all_sports_must_finish(self):
        completed = self.run_client(
            [Response(result(sport)) for sport in runner.SPORTS], sports=runner.SPORTS,
        )
        self.assertEqual(set(completed), set(runner.SPORTS))

    def test_transient_http_statuses_retry_the_same_sport(self):
        for code in sorted(runner.TRANSIENT_HTTP):
            with self.subTest(code=code):
                self.open_request.reset_mock()
                self.run_client([http_error(code), Response(result())])
                self.assertEqual(self.open_request.call_count, 2)
                self.assertEqual(self.delays[-1], 5)
                self.assertNotIn("private body", " ".join(self.messages))

    def test_transport_failures_retry_and_can_observe_already_finished_work(self):
        from http.client import IncompleteRead, RemoteDisconnected
        for error in (
            URLError("test"), TimeoutError(), ConnectionResetError(),
            IncompleteRead(b"{partial"), RemoteDisconnected(),
        ):
            with self.subTest(error=type(error).__name__):
                self.open_request.reset_mock()
                self.run_client([error, Response(result())])
                self.assertEqual(self.open_request.call_count, 2)

    def test_permanent_errors_fail_without_retry(self):
        for code in (301, 400, 401, 403, 404, 500):
            with self.subTest(code=code):
                self.open_request.reset_mock()
                with self.assertRaises(runner.MaintenanceError):
                    self.run_client([http_error(code)])
                self.assertEqual(self.open_request.call_count, 1)

    def test_exhausted_retries_do_not_report_success(self):
        with self.assertRaises(runner.MaintenanceError):
            self.run_client([http_error(503) for _ in range(runner.MAX_ATTEMPTS)])
        self.assertEqual(self.open_request.call_count, 8)
        self.assertEqual(self.delays, [5, 10, 15, 20, 25, 30, 30])

    def test_invalid_or_inconsistent_200_responses_fail_closed(self):
        old_worker = result()
        del old_worker["team_reports_remaining"]
        examples = [
            b"<html>old page</html>", [], old_worker,
            {**result(), "status": "error"}, result("nfl"),
            {**result(), "complete": "true"}, {**result(), "complete": 1},
            {**result(), "remaining": True}, {**result(), "remaining": -1},
            {**result(), "team_reports_remaining": "0"},
            result(events=1), result(teams=1),
            b"x" * (runner.MAX_RESPONSE_BYTES + 1),
        ]
        for example in examples:
            with self.subTest(example=str(example)[:100]):
                self.open_request.reset_mock()
                with self.assertRaises(runner.MaintenanceError):
                    self.run_client([Response(example)])
                self.assertEqual(self.open_request.call_count, 1)

    def test_false_completion_never_counts_as_success_even_with_zero_counters(self):
        with self.assertRaises(runner.MaintenanceError):
            self.run_client([Response(result(complete=False))], max_batches=1)

    def test_batch_limit_marks_unfinished_work_as_failure(self):
        with self.assertRaises(runner.MaintenanceError):
            self.run_client(
                [Response(result(complete=False, teams=1)) for _ in range(3)],
                max_batches=3,
            )
        self.assertEqual(self.open_request.call_count, 3)

    def test_failure_in_one_sport_still_attempts_the_others(self):
        with self.assertRaisesRegex(runner.MaintenanceError, "Incomplete.*mlb"):
            self.run_client(
                [http_error(500), Response(result("nfl")), Response(result("nhl"))],
                sports=runner.SPORTS,
            )
        requested = [json.loads(call.args[0].data)["sport"] for call in self.open_request.call_args_list]
        self.assertEqual(requested, list(runner.SPORTS))

    def test_retry_cannot_overrun_the_time_budget(self):
        with self.assertRaises(runner.MaintenanceError):
            self.run_client([http_error(503)], time_budget=4)
        self.assertEqual(self.open_request.call_count, 1)
        self.assertEqual(self.delays, [])
        self.assertEqual(self.open_request.call_args.kwargs["timeout"], 4)

    def test_deadline_also_bounds_successful_but_unfinished_batches(self):
        def slow_response(request, timeout):
            self.now += 6
            return Response(result(complete=False, teams=1))
        self.open_request.side_effect = slow_response
        with self.assertRaises(runner.MaintenanceError):
            runner.run_maintenance(
                "test-token", sports=("mlb",), time_budget=10,
                open_request=self.open_request, clock=lambda: self.now,
                sleep=self.sleep, log=self.messages.append,
            )
        self.assertEqual(self.open_request.call_count, 2)
        self.assertEqual(self.open_request.call_args.kwargs["timeout"], 4)

    def test_missing_credential_never_makes_a_request(self):
        for token in ("", " ", "bad\ntoken"):
            with self.subTest(token=repr(token)):
                with self.assertRaises(runner.MaintenanceError):
                    runner.run_maintenance(token, open_request=self.open_request)
        self.open_request.assert_not_called()

    def test_redirects_are_not_followed(self):
        self.assertIsNone(runner.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid"))

    def test_cli_exit_status_is_failure_when_work_is_unfinished(self):
        with patch.object(runner, "run_maintenance", side_effect=runner.MaintenanceError("unfinished")):
            with patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(runner.main(), 1)

    def test_workflow_uses_one_shared_runner_and_does_not_cancel_active_maintenance(self):
        collector = (ROOT / ".github/workflows/collect-ticket-prices.yml").read_text()
        job = collector.split("  maintain-materialized-analytics:\n", 1)[1]
        self.assertIn("if: always()", job)
        for dependency in ("collect-baseball", "collect-nfl", "collect-nhl"):
            self.assertIn(dependency, job)
        self.assertIn("uses: ./.github/workflows/maintain-materialized-analytics.yml", job)
        shared = (ROOT / ".github/workflows/maintain-materialized-analytics.yml").read_text()
        self.assertIn("workflow_call:", shared)
        self.assertIn("cancel-in-progress: false", shared)
        self.assertIn("timeout-minutes: 15", shared)
        self.assertIn("python -u .github/scripts/maintain_materialized_analytics.py", shared)
        self.assertIn("if: github.event_name != 'pull_request'", shared)
        self.assertIn("COLLECTOR_INGEST_TOKEN: ${{ secrets.COLLECTOR_INGEST_TOKEN }}", shared)


if __name__ == "__main__":
    unittest.main()
