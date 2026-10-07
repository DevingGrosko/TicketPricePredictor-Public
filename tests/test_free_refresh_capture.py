"""Offline, disposable SQLite checks. No credentials or production connections."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from tools.free_refresh_capture import models_for, parse_payload, require_write_statement, store_payload

NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


def payload(sport='mlb'):
    names = {'mlb': ('New York Mets at Washington Nationals', 'Nationals Park'),
             'nfl': ('Dallas Cowboys at New York Giants', 'MetLife Stadium'),
             'nhl': ('Toronto Maple Leafs at Montreal Canadiens', 'Bell Centre')}
    title, venue = names[sport]
    data = {'schema_version': 1, 'source_id': '1234567', 'title': title, 'venue': venue,
            'source_url': 'https://www.vividseats.com/--sports-mlb-baseball/test/production/1234567',
            'event_date': (NOW+timedelta(hours=12)).isoformat(),
            'captured_at': NOW.isoformat(), 'section_count': 10,
            'sections': [{'section': 'Section '+str(100+i), 'price': 75+i,
                          'listing_count': 2, 'price_source': 'p'} for i in range(10)]}
    if sport != 'mlb':
        data['event_type'] = sport
        data['source_url'] = 'https://www.vividseats.com/game-tickets/production/1234567'
        if sport == 'nhl':
            data['currency'] = 'CAD'
    return data


class GuardTests(unittest.TestCase):
    def test_raw_allowlist(self):
        for sql in ['SELECT 1', 'INSERT INTO tickets (iteration_id) VALUES (%s)',
                    'UPDATE event SET title=%s WHERE id=%s']:
            require_write_statement(sql, 'mlb')

    def test_block_destructive_qualified_or_cross_sport_writes(self):
        for sql in ['DROP TABLE event', 'DELETE FROM tickets', 'TRUNCATE event',
                    'UPDATE tickets SET price=0', 'CREATE TABLE example (x int)',
                    'INSERT INTO nfl_event (id) VALUES (1)',
                    'INSERT INTO other.event (id) VALUES (1)',
                    'INSERT INTO tickets (id) VALUES (1); SELECT 1',
                    'SELECT 1 INTO OUTFILE "/tmp/test"']:
            with self.subTest(sql=sql), self.assertRaises(Exception):
                require_write_statement(sql, 'mlb')

    def test_old_future_and_wrong_sport_payloads_rejected(self):
        for change in [{'captured_at': (NOW-timedelta(days=8)).isoformat()},
                       {'captured_at': (NOW+timedelta(hours=2)).isoformat()},
                       {'event_type': 'nhl'}, {'section_count': 11},
                       {'event_date': (NOW-timedelta(hours=1)).isoformat()}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_payload('mlb', {**payload(), **change}, NOW)

    def test_half_hour_slot(self):
        p = payload(); p['captured_at'] = (NOW+timedelta(minutes=31)).isoformat()
        parsed = parse_payload('mlb', p, NOW+timedelta(minutes=35))
        self.assertEqual(parsed[2].minute, 30)


class StorageTests(unittest.TestCase):
    def setup_db(self, sport):
        engine = create_engine('sqlite:///:memory:')
        Event, Iteration, Ticket = models_for(sport)
        Event.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        return engine, Event, Iteration, Ticket

    def test_atomic_and_duplicate_slots_preserve_all_sports(self):
        for sport in ('mlb', 'nfl', 'nhl'):
            with self.subTest(sport=sport):
                engine, Event, Iteration, Ticket = self.setup_db(sport)
                p = payload(sport)
                first = store_payload(engine, sport, p, now=NOW)
                changed = deepcopy(p); changed['sections'][0]['price'] = 999
                duplicate = store_payload(engine, sport, changed, now=NOW)
                self.assertEqual(first['status'], 'stored')
                self.assertEqual(duplicate['status'], 'duplicate')
                self.assertEqual(first['iteration_id'], duplicate['iteration_id'])
                with Session(engine) as session:
                    self.assertEqual(session.scalar(select(func.count()).select_from(Iteration)), 1)
                    self.assertEqual(session.scalar(select(func.count()).select_from(Ticket)), 10)
                    self.assertEqual(session.scalars(select(Ticket).order_by(Ticket.id)).first().price, 75)
                    if sport == 'nhl':
                        self.assertEqual(session.scalars(select(Event)).one().currency, 'CAD')

    def test_next_slot_adds_history_without_replacing_it(self):
        for sport in ('mlb', 'nfl', 'nhl'):
            with self.subTest(sport=sport):
                engine, Event, Iteration, Ticket = self.setup_db(sport)
                p = payload(sport); first = store_payload(engine, sport, p, now=NOW)
                p['captured_at'] = (NOW+timedelta(minutes=30)).isoformat()
                second = store_payload(engine, sport, p, now=NOW+timedelta(minutes=30))
                p['captured_at'] = (NOW+timedelta(minutes=59)).isoformat()
                duplicate = store_payload(engine, sport, p, now=NOW+timedelta(minutes=59))
                self.assertNotEqual(first['iteration_id'], second['iteration_id'])
                self.assertEqual(duplicate['iteration_id'], second['iteration_id'])
                self.assertEqual(duplicate['status'], 'duplicate')
                with Session(engine) as s:
                    self.assertEqual(s.scalar(select(func.count()).select_from(Event)), 1)
                    self.assertEqual(s.scalar(select(func.count()).select_from(Iteration)), 2)
                    self.assertEqual(s.scalar(select(func.count()).select_from(Ticket)), 20)

    def test_partial_failure_rolls_back_event_iteration_and_tickets(self):
        engine, Event, Iteration, Ticket = self.setup_db('mlb')
        def fail(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.startswith('INSERT INTO tickets'):
                raise RuntimeError('injected failure')
        event.listen(engine, 'before_cursor_execute', fail)
        with self.assertRaises(RuntimeError):
            store_payload(engine, 'mlb', payload(), now=NOW)
        event.remove(engine, 'before_cursor_execute', fail)
        with Session(engine) as s:
            for model in (Event, Iteration, Ticket):
                self.assertEqual(s.scalar(select(func.count()).select_from(model)), 0)

    def test_stale_replay_does_not_rewind_event_metadata(self):
        engine, Event, Iteration, Ticket = self.setup_db('mlb')
        p = payload(); store_payload(engine, 'mlb', p, now=NOW)
        replay = deepcopy(p); replay['captured_at'] = (NOW-timedelta(hours=1)).isoformat()
        replay['venue'] = 'Older label'
        store_payload(engine, 'mlb', replay, now=NOW)
        with Session(engine) as s:
            self.assertEqual(s.scalars(select(Event)).one().Place, p['venue'])


if __name__ == '__main__':
    unittest.main()
