from datetime import datetime
import unittest

from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, String, Table, create_engine, select

from Flask_App.mysql_cutover_collation_safe import _pk_signature, _sample_rows


class CollationStableCutoverTests(unittest.TestCase):
    def setUp(self):
        source_metadata = MetaData()
        destination_metadata = MetaData()
        self.source_table = Table(
            "analytics_dirty_venue",
            source_metadata,
            Column("venue", String(300), primary_key=True),
            Column("revision", Integer, nullable=False),
            Column("dirty", Boolean, nullable=False),
            Column("updated_at", DateTime, nullable=False),
        )
        self.destination_table = Table(
            "analytics_dirty_venue",
            destination_metadata,
            Column("venue", String(300, collation="NOCASE"), primary_key=True),
            Column("revision", Integer, nullable=False),
            Column("dirty", Boolean, nullable=False),
            Column("updated_at", DateTime, nullable=False),
        )
        self.source_engine = create_engine("sqlite://")
        self.destination_engine = create_engine("sqlite://")
        source_metadata.create_all(self.source_engine)
        destination_metadata.create_all(self.destination_engine)

        timestamp = datetime(2026, 9, 5, 18, 0)
        self.rows = [
            {"venue": "Zebra Stadium", "revision": 1, "dirty": True, "updated_at": timestamp},
            {"venue": "alpha arena", "revision": 2, "dirty": False, "updated_at": timestamp},
            {"venue": "Madison Square Garden", "revision": 3, "dirty": True, "updated_at": timestamp},
            {"venue": "buffalo field", "revision": 4, "dirty": True, "updated_at": timestamp},
            {"venue": "Capital Dome", "revision": 5, "dirty": False, "updated_at": timestamp},
        ]
        with self.source_engine.begin() as connection:
            connection.execute(self.source_table.insert(), self.rows)
        with self.destination_engine.begin() as connection:
            connection.execute(self.destination_table.insert(), self.rows)

    def tearDown(self):
        self.source_engine.dispose()
        self.destination_engine.dispose()

    def test_signatures_and_samples_ignore_database_text_collation(self):
        with self.source_engine.connect() as source, self.destination_engine.connect() as destination:
            source_database_order = source.execute(
                select(self.source_table.c.venue).order_by(self.source_table.c.venue)
            ).scalars().all()
            destination_database_order = destination.execute(
                select(self.destination_table.c.venue).order_by(self.destination_table.c.venue)
            ).scalars().all()
            self.assertNotEqual(source_database_order, destination_database_order)

            self.assertEqual(
                _pk_signature(source, self.source_table),
                _pk_signature(destination, self.destination_table),
            )
            self.assertEqual(
                [row["digest"] for row in _sample_rows(source, self.source_table)],
                [row["digest"] for row in _sample_rows(destination, self.destination_table)],
            )


if __name__ == "__main__":
    unittest.main()
