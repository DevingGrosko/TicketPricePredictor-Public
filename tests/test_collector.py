import json
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from collector import (
    EventSnapshot,
    SectionSnapshot,
    SnapshotParser,
    VENUE_FEEDS,
    add_urls,
    browser_failure_requires_immediate_cooldown,
    cpu_budget_allows_capture,
    collection_interval,
    create_daily_backup,
    event_date_from_url,
    extract_mlb_event_urls,
    load_registry,
    load_runtime_state,
    record_cycle_result,
    registry_row_is_excluded,
    retire_url,
    run_collector,
    select_due_urls,
    write_capture_audit,
)
from models import clean_event_title, event_has_complete_public_data


class SnapshotParserTests(unittest.TestCase):
    def test_removes_trailing_giveaway_from_event_title(self):
        payload = {
            "global": [{
                "productionName": "Pirates at Yankees (Yankees T-Shirt Night)",
                "mapTitle": "Yankee Stadium",
                "productionId": "123",
            }],
            "tickets": [
                {"l": f"Section {index}", "p": "50.00"}
                for index in range(10)
            ],
        }

        snapshot = SnapshotParser.parse(payload)

        self.assertEqual(snapshot.title, "Pirates at Yankees")

    def test_uses_the_displayed_price_when_alternate_price_is_higher(self):
        tickets = [
            {"l": f"Baseline {100 + index}", "p": "80.00", "aip": "110.00"}
            for index in range(9)
        ]
        tickets.extend([
            {"l": "Baseline 113", "r": "20", "q": "4", "p": "50.00", "aip": "71.82"},
            {"l": "Baseline 113", "r": "14", "q": "2", "p": "44.00", "aip": "63.66"},
        ])
        payload = {
            "global": [{
                "productionName": "Test game",
                "mapTitle": "Test Park",
                "productionId": "123",
            }],
            "tickets": tickets,
        }

        snapshot = SnapshotParser.parse(payload)
        section = next(row for row in snapshot.sections if row.section == "Baseline 113")

        self.assertEqual(section.price, 44)
        self.assertEqual(section.listing_count, 2)
        self.assertEqual(section.row, "14")
        self.assertEqual(section.quantity, "2")
        self.assertEqual(section.displayed_price, "44.00")
        self.assertEqual(section.alternate_price, "63.66")
        self.assertEqual(section.price_source, "p")

    def test_rejects_an_incomplete_payload_instead_of_storing_it(self):
        payload = {
            "global": [{"productionName": "Test", "mapTitle": "Park"}],
            "tickets": [{"l": "Baseline 113", "p": "44.00"}],
        }

        with self.assertRaisesRegex(ValueError, "only 1 usable sections"):
            SnapshotParser.parse(payload)

    def test_does_not_use_alternate_price_when_displayed_price_is_missing(self):
        payload = {
            "global": [{"productionName": "Test", "mapTitle": "Park"}],
            "tickets": [
                {"l": f"Baseline {100 + index}", "p": "50.00"}
                for index in range(10)
            ] + [{"l": "Baseline 113", "aip": "63.66"}],
        }

        snapshot = SnapshotParser.parse(payload)

        self.assertNotIn("Baseline 113", {row.section for row in snapshot.sections})

    def test_registered_urls_are_deduplicated(self):
        url = "https://www.vividseats.com/example-tickets/production/1234567"
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "events.json"
            self.assertEqual(add_urls([url, url], registry), 1)
            self.assertEqual(len(load_registry(registry)["events"]), 1)


class ScheduleTests(unittest.TestCase):
    def test_known_incomplete_july_games_are_not_public(self):
        self.assertFalse(
            event_has_complete_public_data(
                SimpleNamespace(event_date=datetime(2026, 7, 19, 13, 35))
            )
        )
        self.assertTrue(
            event_has_complete_public_data(
                SimpleNamespace(event_date=datetime(2026, 7, 20, 13, 35))
            )
        )
        self.assertEqual(
            clean_event_title("Pirates at Yankees (Yankees T-Shirt Night)"),
            "Pirates at Yankees",
        )

    def test_excluded_parks_are_never_discovered_or_collected(self):
        self.assertNotIn("Citi Field", VENUE_FEEDS)
        self.assertNotIn("Truist Park", VENUE_FEEDS)
        self.assertTrue(registry_row_is_excluded({"venue": "Citi Field", "url": ""}))
        self.assertTrue(registry_row_is_excluded({"venue": "Truist Park", "url": ""}))
        self.assertTrue(
            registry_row_is_excluded(
                {
                    "venue": "",
                    "url": "https://www.vividseats.com/game-tickets-george-m-steinbrenner-field-7-20-2026--sports-mlb-baseball/production/123",
                }
            )
        )
        self.assertFalse(
            registry_row_is_excluded(
                {
                    "venue": "Citizens Bank Park",
                    "url": "https://www.vividseats.com/game-tickets-citizens-bank-park-7-20-2026--sports-mlb-baseball/production/456",
                }
            )
        )

    def test_collection_frequency_increases_near_game_time(self):
        self.assertIsNone(collection_interval(200))
        self.assertEqual(collection_interval(96), timedelta(hours=4))
        self.assertEqual(collection_interval(72), timedelta(hours=1))
        self.assertEqual(collection_interval(24), timedelta(minutes=15))
        self.assertEqual(collection_interval(1), timedelta(minutes=15))

    def test_extracts_and_dates_mlb_links_from_venue_page(self):
        page = '''
        <a href="/new-york-mets-tickets-citi-field-8-1-2026--sports-mlb-baseball/production/123">Game</a>
        <a href="/concert-tickets/production/999">Concert</a>
        '''
        urls = extract_mlb_event_urls(page)
        self.assertEqual(len(urls), 1)
        url = urls.pop()
        self.assertEqual(event_date_from_url(url).date().isoformat(), "2026-08-01")

    def test_retire_url_removes_only_the_finished_link(self):
        finished = "https://www.vividseats.com/finished-tickets/production/123"
        upcoming = "https://www.vividseats.com/upcoming-tickets/production/456"
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "events.json"
            registry.write_text(json.dumps({"events": [{"url": finished}, {"url": upcoming}]}))
            self.assertTrue(retire_url(registry, finished))
            self.assertEqual(load_registry(registry)["events"], [{"url": upcoming}])
            self.assertFalse(retire_url(registry, finished))

    @patch("collector.store_snapshot")
    @patch("collector.write_health")
    @patch("collector.create_daily_backup", return_value=Path("/tmp/test-backup.db"))
    @patch("collector.is_due", return_value=(True, "new event"))
    @patch("collector.prune_finished_events", return_value=0)
    @patch("collector.VividBrowser")
    @patch("collector.CreateModel")
    def test_started_event_is_retired_without_storing_snapshot(
        self, create_model, vivid_browser, _prune, _is_due, _backup, _health, store_snapshot
    ):
        class DummySession:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        create_model.return_value.getSession.return_value = DummySession
        browser = vivid_browser.return_value
        browser.capture.return_value = (
            {"global": [{"productionName": "Finished game", "mapTitle": "Test Park"}], "tickets": [{}]},
            datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        url = "https://www.vividseats.com/finished-tickets/production/123"

        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "events.json"
            registry.write_text(json.dumps({"events": [{"url": url, "active": True}]}))
            self.assertEqual(run_collector(registry, False, True, 25), 0)
            self.assertEqual(load_registry(registry)["events"], [])

        store_snapshot.assert_not_called()
        browser.close.assert_called_once()


class GuardrailTests(unittest.TestCase):
    def test_browser_start_and_driver_hangs_trigger_immediate_cooldown(self):
        SessionNotCreatedException = type("SessionNotCreatedException", (Exception,), {})
        ReadTimeoutError = type("ReadTimeoutError", (Exception,), {})

        self.assertTrue(browser_failure_requires_immediate_cooldown(SessionNotCreatedException()))
        self.assertTrue(browser_failure_requires_immediate_cooldown(ReadTimeoutError()))
        self.assertFalse(browser_failure_requires_immediate_cooldown(TimeoutError()))

    @patch("collector.write_health")
    @patch("collector.open_failure_circuit")
    @patch("collector.VividBrowser")
    @patch("collector.CreateModel")
    @patch("collector.is_due", return_value=(True, "scheduled"))
    @patch("collector.prune_finished_events", return_value=0)
    @patch("collector.create_daily_backup", return_value=Path("/tmp/test-backup.db"))
    @patch(
        "collector.pythonanywhere_cpu_usage",
        return_value={
            "daily_cpu_limit_seconds": 5000,
            "daily_cpu_total_usage_seconds": 100,
        },
    )
    def test_browser_start_failure_pauses_without_retrying(
        self,
        _cpu,
        _backup,
        _prune,
        _is_due,
        create_model,
        vivid_browser,
        open_circuit,
        write_health,
    ):
        class DummySession:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        SessionNotCreatedException = type("SessionNotCreatedException", (Exception,), {})
        create_model.return_value.getSession.return_value = DummySession
        vivid_browser.side_effect = SessionNotCreatedException("Chrome failed to start")
        open_circuit.return_value = datetime.now(timezone.utc) + timedelta(hours=6)

        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "events.json"
            registry.write_text(
                json.dumps(
                    {
                        "events": [
                            {
                                "url": "https://www.vividseats.com/game-tickets/production/123",
                                "active": True,
                            }
                        ]
                    }
                )
            )
            result = run_collector(registry, False, True, 25)

        self.assertEqual(result, 1)
        vivid_browser.assert_called_once()
        open_circuit.assert_called_once()
        self.assertEqual(write_health.call_args.args[0], "paused")

    def test_stops_before_pythonanywhere_cpu_quota_is_exhausted(self):
        allowed, reason = cpu_budget_allows_capture(
            {
                "daily_cpu_limit_seconds": 5000,
                "daily_cpu_total_usage_seconds": 4850,
            }
        )

        self.assertFalse(allowed)
        self.assertIn("CPU safety stop", reason)

        allowed, reason = cpu_budget_allows_capture(
            {
                "daily_cpu_limit_seconds": 5000,
                "daily_cpu_total_usage_seconds": 4800,
            }
        )

        self.assertTrue(allowed)
        self.assertIn("healthy", reason)

    def test_runs_every_due_event_in_soonest_game_order(self):
        urls = [
            f"https://www.vividseats.com/team-tickets-7-{day}-2026--sports-mlb-baseball/production/{day}"
            for day in (22, 19, 21, 20)
        ]

        selected, deferred = select_due_urls(urls)

        self.assertEqual([event_date_from_url(url).day for url in selected], [19, 20, 21, 22])
        self.assertEqual(deferred, 0)

    @patch("collector.record_cycle_result", return_value=None)
    @patch("collector.write_health")
    @patch("collector.pythonanywhere_cpu_usage", return_value=None)
    @patch("collector.create_daily_backup", return_value=Path("/tmp/test-backup.db"))
    @patch("collector.prune_finished_events", return_value=0)
    @patch("collector.is_due", return_value=(True, "scheduled"))
    @patch("collector.CreateModel")
    @patch("collector.VividBrowser")
    def test_stops_a_broken_cycle_after_two_capture_failures(
        self,
        vivid_browser,
        create_model,
        _is_due,
        _prune,
        _backup,
        _cpu,
        write_health,
        _record_result,
    ):
        class DummySession:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        create_model.return_value.getSession.return_value = DummySession
        vivid_browser.return_value.capture.side_effect = TimeoutError("no listings")
        urls = [
            f"https://www.vividseats.com/team-tickets-7-{day}-2026--sports-mlb-baseball/production/{day}"
            for day in (19, 20, 21, 22)
        ]

        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "events.json"
            registry.write_text(
                json.dumps({"events": [{"url": url, "active": True} for url in urls]})
            )
            result = run_collector(registry, False, True, 25)

        self.assertEqual(result, 1)
        self.assertEqual(vivid_browser.return_value.capture.call_count, 2)
        self.assertEqual(write_health.call_args.kwargs["deferred"], 2)

    def test_opens_six_hour_circuit_after_two_failed_cycles(self):
        now = datetime(2026, 7, 19, 15, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state = load_runtime_state(state_file)
            self.assertIsNone(
                record_cycle_result(
                    state,
                    now,
                    succeeded=0,
                    failed=1,
                    reason="timeout",
                    state_file=state_file,
                )
            )
            cooldown = record_cycle_result(
                load_runtime_state(state_file),
                now,
                succeeded=0,
                failed=1,
                reason="timeout",
                state_file=state_file,
            )

            saved = load_runtime_state(state_file)

        self.assertEqual(cooldown, now + timedelta(hours=6))
        self.assertEqual(saved["cooldown_until"], cooldown.isoformat())

    def test_writes_auditable_winning_listing_details(self):
        section = SectionSnapshot(
            section="Baseline 113",
            price=44,
            listing_count=22,
            row="14",
            quantity="2",
            displayed_price="44.00",
            alternate_price="63.66",
        )
        snapshot = EventSnapshot("5966528", "Mets at Phillies", "Citizens Bank Park", (section,))

        with tempfile.TemporaryDirectory() as directory:
            path = write_capture_audit(
                "https://www.vividseats.com/example/production/5966528",
                datetime(2026, 7, 19, 13, 35, tzinfo=timezone.utc),
                snapshot,
                event_id=39,
                iteration_id=6194,
                captured_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
                audit_dir=Path(directory),
            )
            record = json.loads(path.read_text().strip())

        self.assertEqual(record["currency"], "USD")
        self.assertEqual(record["sections"][0]["price"], 44)
        self.assertEqual(record["sections"][0]["row"], "14")
        self.assertEqual(record["sections"][0]["listing_count"], 22)

    def test_creates_a_consistent_daily_sqlite_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE sample (value INTEGER)")
                connection.execute("INSERT INTO sample VALUES (44)")

            backup = create_daily_backup(
                now=datetime(2026, 7, 19, tzinfo=timezone.utc),
                source=source,
                backup_dir=root / "backups",
            )
            with sqlite3.connect(backup) as connection:
                value = connection.execute("SELECT value FROM sample").fetchone()[0]

        self.assertEqual(value, 44)


if __name__ == "__main__":
    unittest.main()
