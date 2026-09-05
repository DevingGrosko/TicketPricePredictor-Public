from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from Flask_App.repair_legacy_prices import (
    _backup_database,
    _delete_for_event_ids,
    _fractional_rows,
)


class LegacyPriceRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "prices.db"
        self.connection = sqlite3.connect(self.database)
        self.addCleanup(self.connection.close)
        self.connection.executescript(
            """
            CREATE TABLE iterations (
                id INTEGER PRIMARY KEY,
                event_id INTEGER NOT NULL
            );
            CREATE TABLE tickets (
                id INTEGER PRIMARY KEY,
                section TEXT,
                price INTEGER,
                iteration_id INTEGER NOT NULL
            );
            CREATE TABLE section_bucket_summary (event_id INTEGER);
            CREATE TABLE section_summary_state (event_id INTEGER);

            INSERT INTO iterations VALUES (1, 10), (2, 11);
            INSERT INTO tickets VALUES
                (1, 'A', 49.26, 1),
                (2, 'B', 49.50, 1),
                (3, 'C', 50.0, 2),
                (4, 'D', 51, 2);
            INSERT INTO section_bucket_summary VALUES (10), (11);
            INSERT INTO section_summary_state VALUES (10), (11);
            """
        )
        self.connection.commit()

    def test_fractional_rows_use_current_half_up_rule(self) -> None:
        rows = _fractional_rows(self.connection, "tickets", "iterations")
        self.assertEqual(
            [(row["ticket_id"], row["new_price"]) for row in rows],
            [(1, 49), (2, 50)],
        )

    def test_summary_invalidation_is_event_scoped(self) -> None:
        _delete_for_event_ids(
            self.connection,
            "section_bucket_summary",
            [10],
        )
        _delete_for_event_ids(
            self.connection,
            "section_summary_state",
            [10],
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT event_id FROM section_bucket_summary ORDER BY event_id"
            ).fetchall(),
            [(11,)],
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT event_id FROM section_summary_state ORDER BY event_id"
            ).fetchall(),
            [(11,)],
        )

    def test_backup_is_readable_and_complete(self) -> None:
        backup_path = _backup_database(
            self.connection,
            self.database,
            "20260905T220000Z",
        )
        with closing(sqlite3.connect(backup_path)) as backup:
            count = backup.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        self.assertEqual(count, 4)


if __name__ == "__main__":
    unittest.main()
