from datetime import datetime
from decimal import Decimal
import unittest

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table

from Flask_App.mysql_cutover import _normalized_row, _row_digest


class MySQLSemanticVerificationTests(unittest.TestCase):
    def setUp(self):
        metadata = MetaData()
        self.table = Table(
            "tickets",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("section", String(300)),
            Column("price", Integer),
            Column("ticketsPerSection", Integer, nullable=True),
            Column("captured", DateTime),
        )

    def test_sqlite_and_mysql_driver_representations_match(self):
        sqlite_row = {
            "id": 1.0,
            "section": 101,
            "price": 80.0,
            "ticketsPerSection": 2.0,
            "captured": datetime(2026, 9, 5, 12, 0),
        }
        mysql_row = {
            "id": 1,
            "section": "101",
            "price": 80,
            "ticketsPerSection": 2,
            "captured": datetime(2026, 9, 5, 12, 0),
        }
        self.assertEqual(
            _row_digest(self.table, sqlite_row),
            _row_digest(self.table, mysql_row),
        )

    def test_real_string_change_still_fails(self):
        source = {
            "id": 1,
            "section": "Section 101",
            "price": 80,
            "ticketsPerSection": None,
            "captured": datetime(2026, 9, 5, 12, 0),
        }
        destination = {**source, "section": "Section 10"}
        self.assertNotEqual(
            _row_digest(self.table, source),
            _row_digest(self.table, destination),
        )

    def test_non_integral_value_is_rejected_for_integer_column(self):
        row = {
            "id": 1,
            "section": "101",
            "price": Decimal("80.5"),
            "ticketsPerSection": 2,
            "captured": datetime(2026, 9, 5, 12, 0),
        }
        with self.assertRaises(RuntimeError):
            _normalized_row(self.table, row)


if __name__ == "__main__":
    unittest.main()
