"""Real saved public observations remain valid at their original capture time."""
from datetime import datetime, timedelta, timezone
import json
import socket
import unittest
from unittest.mock import patch

from collector import clean_event_title
from tools.shared_capture import identity, saved_observations

MANIFEST = 'docs/shared-observations/manifest-0400.json'
SHA256 = '6b894990f80fb18f766427767944d0096f74e44bd0683e3630eda9e76999e287'
EXPECTED = {'nfl': {'6493143': 211, '6489565': 200}, 'nhl': {'7302493': 60, '7300510': 77}}


class SavedCollectorPayloadTests(unittest.TestCase):
    def setUp(self):
        def denied(*args, **kwargs):
            raise AssertionError('Saved observation checks must remain offline')
        for guard in (patch.object(socket.socket, 'connect', denied),
                      patch('nfl_collector.VividNFLBrowser.__init__', denied)):
            guard.start(); self.addCleanup(guard.stop)

    def verify_original_observations(self, sport):
        from Flask_App import nfl_blueprint, nhl_blueprint
        api = nfl_blueprint if sport == 'nfl' else nhl_blueprint
        parse = getattr(api, sport+'_snapshot_from_payload')
        observations = saved_observations(MANIFEST, SHA256, sport)
        self.assertEqual({value['source_id'] for value in observations}, set(EXPECTED[sport]))
        for value in observations:
            with self.subTest(sport=sport, production_id=value['source_id']):
                original = json.dumps(value, sort_keys=True)
                url, event, observed, snapshot, metadata, geometry = parse(value)
                self.assertIsNotNone(event.tzinfo); self.assertIsNotNone(observed.tzinfo)
                self.assertGreater(event-observed, timedelta(0))
                self.assertLessEqual(event-observed, timedelta(days=30))
                self.assertEqual(observed.astimezone(timezone.utc).date(), datetime(2026, 10, 8).date())
                self.assertEqual(snapshot.source_id, value['source_id'])
                self.assertEqual(url.rstrip('/').split('/')[-1], value['source_id'])
                self.assertEqual(len(snapshot.sections), EXPECTED[sport][value['source_id']])
                self.assertEqual(snapshot.title, clean_event_title(value['title']))
                self.assertEqual(snapshot.venue, value['venue'])
                self.assertEqual([(row.section, row.price, row.listing_count) for row in snapshot.sections],
                                 [(row['section'], row['price'], row['listing_count']) for row in value['sections']])
                self.assertEqual(identity(sport, value)[0], snapshot.source_id)
                self.assertIsNone(geometry)  # These native inventories did not contain a map.
                self.assertEqual(json.dumps(value, sort_keys=True), original)
                # Wrong source identity/count must not be silently accepted.
                for change in ({'source_id': '1'}, {'section_count': value['section_count']+1}):
                    with self.assertRaises(ValueError): parse({**value, **change})

    def test_nfl_original_observations(self):
        self.verify_original_observations('nfl')

    def test_nhl_original_observations(self):
        self.verify_original_observations('nhl')


if __name__ == '__main__':
    unittest.main()
