"""Older pending snapshots append history without rewinding current game metadata."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from collector import EventSnapshot, SectionSnapshot
from Flask_App.nfl_blueprint import CreateNFLModel, NFLEvent, NFLIteration, NFLTicket, store_nfl_snapshot
from Flask_App.nhl_blueprint import CreateNHLModel, NHLEvent, NHLIteration, NHLTicket, store_nhl_snapshot
from models import captured_datetime_for_storage, event_datetime_for_storage
from nfl_metadata import geometry_section_count


class SnapshotReplayMetadataTests(unittest.TestCase):
    def test_newer_metadata_survives_old_duplicate_and_previously_unseen_replay(self):
        start = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
        for sport, model_cls, event_cls, iteration_cls, ticket_cls, store in (
            ('nfl', CreateNFLModel, NFLEvent, NFLIteration, NFLTicket, store_nfl_snapshot),
            ('nhl', CreateNHLModel, NHLEvent, NHLIteration, NHLTicket, store_nhl_snapshot)):
            title = 'Dallas Cowboys at New York Giants' if sport == 'nfl' else 'Boston Bruins at Toronto Maple Leafs'
            old = EventSnapshot(source_id='1234567', title=title, venue='Old Venue',
                sections=tuple(SectionSnapshot(section=str(i), price=100+i, listing_count=2,
                    row='A', quantity='2', displayed_price=str(100+i), alternate_price='') for i in range(10)))
            new = replace(old, venue='Updated Venue')
            with self.subTest(sport=sport), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/f'{sport}.db'
                url = 'https://www.vividseats.com/game/production/1234567'
                def save(snapshot, captured, event_at, schedule_id='fixed-game'):
                    metadata = {'schedule_id':schedule_id, 'canonical_venue':snapshot.venue,
                                'neutral_site':snapshot is new, 'city':snapshot.venue,
                                'country':'USA' if sport == 'nfl' else 'Canada'}
                    kwargs = {'currency':'USD' if snapshot is new else 'CAD'} if sport == 'nhl' else {}
                    return store(url, event_at, snapshot, captured, db_path=path,
                                 schedule_metadata=metadata, **kwargs)
                first = save(old, start, start+timedelta(days=2))
                newest = save(new, start+timedelta(minutes=30), start+timedelta(days=3))
                duplicate = save(old, start+timedelta(minutes=29), start+timedelta(days=2))
                replay = save(old, start-timedelta(minutes=30), start+timedelta(days=2))
                self.assertEqual(duplicate[:2], first[:2])
                self.assertFalse(duplicate[2]); self.assertTrue(replay[2])
                self.assertNotEqual(first[1], newest[1])
                # Identity validation still runs even for an older duplicate.
                with self.assertRaisesRegex(ValueError, 'schedule ID changed'):
                    save(old, start, start+timedelta(days=2), 'different-game')
                model = model_cls(path)
                try:
                    with model.getSession()() as session:
                        event = session.query(event_cls).one()
                        self.assertEqual(event.event_date, event_datetime_for_storage(start+timedelta(days=3)))
                        self.assertEqual((event.venue,event.provider_venue,event.canonical_venue),
                                         ('Updated Venue',)*3)
                        self.assertTrue(event.neutral_site)
                        self.assertEqual(event.schedule_id,'fixed-game')
                        if sport == 'nfl': self.assertEqual(event.city,'Updated Venue')
                        else: self.assertEqual(event.currency,'USD')
                        captures = session.query(iteration_cls).order_by(iteration_cls.captured_at).all()
                        self.assertEqual([row.captured_at for row in captures], [
                            captured_datetime_for_storage(start+timedelta(minutes=offset))
                            for offset in (-30,0,30)])
                        self.assertEqual(session.query(ticket_cls).count(),30)
                finally:
                    model.engine.dispose()

    def test_new_smaller_map_replaces_old_map_and_unavailable_or_old_maps_preserve_it(self):
        start = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
        def geometry(count, source):
            return {'source':source, 'view_box':[0,0,100,100], 'sections':[
                {'name':str(i), 'shapes':[{'path':f'M {i*5} 0 L {i*5+4} 0 L {i*5+4} 4 L {i*5} 4 Z', 'transform':''}]}
                for i in range(count)]}
        old_map = geometry(10,'older-false-match')
        accurate_map = geometry(3,'corrected-provider-map')
        for sport, model_cls, event_cls, store in (
            ('nfl', CreateNFLModel, NFLEvent, store_nfl_snapshot),
            ('nhl', CreateNHLModel, NHLEvent, store_nhl_snapshot)):
            title = 'Dallas Cowboys at New York Giants' if sport == 'nfl' else 'Boston Bruins at Toronto Maple Leafs'
            venue = 'MetLife Stadium' if sport == 'nfl' else 'Scotiabank Arena'
            snapshot = EventSnapshot(source_id='1234567', title=title, venue=venue,
                sections=tuple(SectionSnapshot(section=str(i), price=100+i, listing_count=2,
                    row='A', quantity='2', displayed_price=str(100+i), alternate_price='') for i in range(10)))
            with self.subTest(sport=sport), tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/f'{sport}.db'
                def save(offset, map_geometry):
                    return store('https://www.vividseats.com/game/production/1234567',
                        start+timedelta(days=2), snapshot, start+timedelta(minutes=offset),
                        db_path=path, map_geometry=map_geometry)
                self.assertTrue(save(0,old_map)[2])
                self.assertTrue(save(30,accurate_map)[2])
                self.assertTrue(save(60,None)[2])
                self.assertTrue(save(90,{'sections':[]})[2])
                self.assertTrue(save(-30,old_map)[2])  # Previously unseen old replay must not restore the wrong map.
                self.assertFalse(save(0,old_map)[2])
                model=model_cls(path)
                try:
                    with model.getSession()() as session:
                        event=session.query(event_cls).one()
                        self.assertEqual(geometry_section_count(event.map_geometry),3)
                        self.assertEqual(event.map_source,'corrected-provider-map')
                        self.assertEqual(event.geometry_updated_at,
                            captured_datetime_for_storage(start+timedelta(minutes=30)))
                finally:
                    model.engine.dispose()


if __name__ == '__main__':
    unittest.main()
