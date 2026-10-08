from datetime import datetime, timezone
from pathlib import Path
import unittest
import os
import subprocess
import tempfile
import textwrap

from nfl_schedule_collector import fetch_schedule_games


class NFLScheduleSourceResilienceTests(unittest.TestCase):
    def test_season_scoreboard_is_primary_source(self):
        calls = []

        def fetcher(url, timeout):
            calls.append((url, timeout))
            return {"events": []}

        games, source = fetch_schedule_games(
            datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
            fetcher=fetcher,
        )

        self.assertEqual(games, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(source, calls[0][0])
        self.assertIn("dates=2026", source)
        self.assertIn("limit=1000", source)
        self.assertNotIn("20260917-", source)

    def test_january_uses_previous_nfl_season_year(self):
        calls = []

        def fetcher(url, timeout):
            calls.append(url)
            return {"events": []}

        fetch_schedule_games(
            datetime(2027, 1, 10, 12, tzinfo=timezone.utc),
            fetcher=fetcher,
        )

        self.assertEqual(len(calls), 1)
        self.assertIn("dates=2026", calls[0])

    def test_range_query_remains_secondary_fallback(self):
        calls = []

        def fetcher(url, timeout):
            calls.append(url)
            if len(calls) == 1:
                raise RuntimeError("season endpoint unavailable")
            return {"events": []}

        games, source = fetch_schedule_games(
            datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
            fetcher=fetcher,
        )

        self.assertEqual(games, [])
        self.assertEqual(len(calls), 2)
        self.assertEqual(source, calls[1])
        self.assertIn("dates=20260917-20261018", source)


class TicketCollectionWorkflowCadenceTests(unittest.TestCase):
    def test_github_recovery_schedule_uses_the_same_half_hour_owner_gate(self):
        workflow = Path(".github/workflows/collect-ticket-prices.yml").read_text(
            encoding="utf-8"
        )

        for sport in ('nfl', 'nhl'):
            self.assertIn('python -m tools.single_capture_owner slot-gate --sport ' + sport, workflow)
        self.assertNotIn('Skipping NFL on the best-effort GitHub baseball recovery schedule.', workflow)
        self.assertNotIn('Skipping NHL on the best-effort GitHub baseball recovery schedule.', workflow)

    def test_late_half_hour_dispatch_evaluates_both_leagues(self):
        workflow = Path('.github/workflows/collect-ticket-prices.yml').read_text()
        for sport in ('NFL', 'NHL'):
            step = workflow.split(f'Select the half-hour {sport} slot', 1)[1].split('      - if:', 1)[0]
            shell = textwrap.dedent(step.split('        run: |\n', 1)[1])
            with self.subTest(sport=sport), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)/'output'
                calls = Path(directory)/'calls'
                stub = 'python() { printf "%s\\n" "$*" > "$CALL_LOG"; echo run=true >> "$GITHUB_OUTPUT"; }; '
                subprocess.run(['bash', '-c', "date() { printf '37\\n'; }; " + stub + shell],
                    env={**os.environ, 'EVENT_NAME':'workflow_dispatch',
                         'DISPATCH_SOURCE':'pythonanywhere', 'GITHUB_OUTPUT':str(output),
                         'CALL_LOG':str(calls), 'MANUAL_REPAIR':'false', 'DELIVERY_ONLY':'false'},
                    text=True, capture_output=True, check=True)
                self.assertEqual(output.read_text().strip(), 'run=true')
                self.assertEqual(calls.read_text().strip(), '-m tools.single_capture_owner slot-gate --sport ' + sport.lower())


if __name__ == "__main__":
    unittest.main()
