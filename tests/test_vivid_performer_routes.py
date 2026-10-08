from datetime import datetime, timezone
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from vivid_performer_routes import _routes, configure_schedule_navigation, performer_url


class PerformerRoutesTests(unittest.TestCase):
    def test_observed_directory_covers_every_supported_team(self):
        from nfl_collector import NFL_TEAM_NAMES
        from nhl_collector import NHL_TEAM_NAMES
        self.assertEqual(set(_routes()['nfl']['teams']), NFL_TEAM_NAMES)
        self.assertEqual(set(_routes()['nhl']['teams']), NHL_TEAM_NAMES)
        self.assertTrue(performer_url('nfl', 'New Orleans Saints').endswith('/performer/597'))
        self.assertTrue(performer_url('nhl', 'Boston Bruins').endswith('/performer/104'))

    def test_direct_default_preserves_existing_browser_behavior(self):
        with patch.dict(os.environ, {}, clear=True):
            configure_schedule_navigation(object(), 'nfl', None, 'unused')

    def test_schedule_selects_exact_known_home_team_and_official_time(self):
        game = SimpleNamespace(home_team='New Orleans Saints', event_date=datetime(2026,10,11,17,tzinfo=timezone.utc))
        browser = SimpleNamespace(_firefox_session=object())
        url = 'https://www.vividseats.com/en/new-orleans-saints-tickets-new-orleans-caesars-superdome-3-7-2027/production/6493143'
        with patch.dict(os.environ, {'TICKETSIGNAL_FIREFOX_NAVIGATION':'performer'}), \
             patch('vivid_firefox.configure_normal_navigation') as configure:
            configure_schedule_navigation(browser, 'nfl', game, url)
        configure.assert_called_once_with(browser,
            performer_urls={'6493143':performer_url('nfl','New Orleans Saints')},
            expected_event_dates={'6493143':game.event_date})

    def test_invalid_mode_engine_identity_or_date_cannot_start_navigation(self):
        browser = SimpleNamespace(_firefox_session=object())
        game = SimpleNamespace(home_team='Boston Bruins', event_date=datetime(2026,10,8,23,tzinfo=timezone.utc))
        url = 'https://www.vividseats.com/boston-bruins-game/production/7302493'
        cases = [('unknown', browser, game, url), ('performer', object(), game, url),
            ('performer', browser, game, url.replace('www.vividseats.com','example.com')),
            ('performer', browser, game, url+'?quantity=2'),
            ('performer', browser, SimpleNamespace(home_team='Boston Bruins',event_date=datetime(2026,10,8)), url),
            ('performer', browser, SimpleNamespace(home_team='Unknown Team',event_date=game.event_date), url)]
        for mode, owner, scheduled, target in cases:
            with self.subTest(mode=mode,target=target), \
                 patch.dict(os.environ, {'TICKETSIGNAL_FIREFOX_NAVIGATION':mode}), \
                 patch('vivid_firefox.configure_normal_navigation') as configure:
                with self.assertRaises(ValueError):
                    configure_schedule_navigation(owner,'nhl',scheduled,target)
                configure.assert_not_called()


if __name__ == '__main__':
    unittest.main()
