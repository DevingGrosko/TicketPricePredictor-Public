"""Synthetic SQLite regression tests; no staging credentials or network."""
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
import graph_builder as graphs
from models import Base, Event, Iteration, Ticket
from tools.staging_graph_benchmark import legacy_series


class ScalarGraphReadsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        model = SimpleNamespace(getSession=lambda: self.sessions)
        self.factory = patch.object(graphs, 'CreateModel', return_value=model)
        self.factory.start()
        self.addCleanup(self.factory.stop)
        self.addCleanup(self.engine.dispose)
        self.builder = graphs.GraphBuilder()
        with self.sessions() as session:
            for game_id in (1, 2, 3):
                game = Event(id=game_id, title='Away at Home',
                    event_date=datetime(2026, 7, 25 + game_id, 15),
                    event_sections=['Café 101', 'Other'], Place='Test Stadium',
                    URL=('https://example.com/game--sports-mlb-baseball/production/'
                         if game_id != 3 else 'https://example.com/concert/') + str(game_id))
                session.add(game)
                for n in range(12):
                    # Event wall clock is Eastern; captures are stored in UTC.
                    it = Iteration(id=game_id * 100 + n, event=game,
                        captured_at=game.event_date + timedelta(hours=4) -
                        timedelta(hours=12-n, microseconds=123456))
                    session.add(it)
                    session.add(Ticket(section='Café 101', price=(120-n*3),
                                       ticketsPerSection=None, iteration=it))
                    session.add(Ticket(section='Other', price=999,
                                       ticketsPerSection=4, iteration=it))
            session.commit()
        self.sql = []
        event.listen(self.engine, 'before_cursor_execute', self.record)

    def record(self, _connection, _cursor, statement, _parameters, _context, _many):
        self.sql.append(statement)

    def test_identical_values_with_one_query_instead_of_relationship_loads(self):
        old = legacy_series(self.builder, 'Café 101', 1)
        old_queries = len(self.sql)
        self.sql.clear()
        new = self.builder.eachEventGraphList('Café 101', 1)
        self.assertEqual(new, old)
        self.assertEqual(len(new[0]), 12)
        self.assertGreater(old_queries, 12)
        self.assertEqual(len(self.sql), 1)
        self.assertTrue(self.sql[0].startswith('SELECT'))
        self.assertNotIn('event_sections', self.sql[0])
        self.assertNotIn('ticketsPerSection', self.sql[0])

    def test_missing_or_wrong_sport_does_not_leak_other_history(self):
        for section, game in [('missing', 1), ('Café 101', 999), ('Café 101', 3)]:
            with self.subTest(section=section, game=game):
                self.assertEqual(self.builder.eachEventGraphList(section, game), ([], []))

    def test_same_timestamp_and_price_does_not_drop_distinct_ticket_rows(self):
        with self.sessions() as session:
            it = session.get(Iteration, 100)
            session.add(Ticket(section='Café 101', price=120,
                               ticketsPerSection=None, iteration=it))
            session.commit()
        expected = legacy_series(self.builder, 'Café 101', 1)
        actual = self.builder.eachEventGraphList('Café 101', 1)
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual[0]), 13)

    def test_single_game_dollars_and_percentages_unchanged(self):
        for mode in ('money', 'percentage'):
            with self.subTest(mode=mode):
                with patch.object(graphs.GraphBuilder, 'eachEventGraphList', legacy_series):
                    old = self.builder.singleGameGraph('Test Stadium', 1, 'Café 101', mode)
                self.assertEqual(self.builder.singleGameGraph('Test Stadium', 1, 'Café 101', mode), old)
                self.assertTrue(old[0])

    def test_multi_game_aggregation_and_sample_count_unchanged(self):
        for mode in ('money', 'percentage'):
            with self.subTest(mode=mode):
                with patch.object(graphs.GraphBuilder, 'eachEventGraphList', legacy_series):
                    old = self.builder.allEventsForStadium('Test Stadium', 'Café 101', 48, mode)
                actual = self.builder.allEventsForStadium('Test Stadium', 'Café 101', 48, mode)
                self.assertEqual(actual, old)
                self.assertEqual(actual[2], 2)
                self.assertTrue(actual[0])

    def test_zero_price_and_exact_section_values_preserved(self):
        with self.sessions() as session:
            it = session.get(Iteration, 100)
            session.add(Ticket(section="O'Brien; --", price=0,
                               ticketsPerSection=None, iteration=it))
            session.commit()
        self.assertEqual(self.builder.eachEventGraphList("O'Brien; --", 1),
                         legacy_series(self.builder, "O'Brien; --", 1))
        self.assertEqual(self.builder.eachEventGraphList("O'Brien; --", 1)[1], [0])
        self.assertEqual(self.builder.eachEventGraphList("' OR 1=1 --", 1), ([], []))

    def test_single_game_keeps_time_window_and_venue_filter(self):
        self.assertEqual(self.builder.singleGameGraph('Wrong venue', 1, 'Café 101', 'money'), ([], []))
        with self.sessions() as session:
            game = session.get(Event, 1)
            for delta in (100, -1):
                it = Iteration(event=game, captured_at=game.event_date + timedelta(hours=4-delta))
                session.add(Ticket(section='Café 101', price=200,
                                   ticketsPerSection=None, iteration=it))
            session.commit()
        with patch.object(graphs.GraphBuilder, 'eachEventGraphList', legacy_series):
            old = self.builder.singleGameGraph('Test Stadium', 1, 'Café 101', 'money')
        self.assertEqual(self.builder.singleGameGraph('Test Stadium', 1, 'Café 101', 'money'), old)
        self.assertEqual(len(old[0]), 12)


if __name__ == '__main__':
    unittest.main()
