from datetime import datetime, timedelta, timezone

from tools.free_live_verify import capture_freshness


NOW = datetime(2026, 10, 7, 2, tzinfo=timezone.utc)


def game(lead_hours, age_minutes=0):
    return {'event_at': (NOW + timedelta(hours=lead_hours)).isoformat(),
            'captured_through': (NOW - timedelta(minutes=age_minutes)).isoformat()}


def test_newest_sport_capture_cannot_hide_another_overdue_game():
    catalogs = {'nfl': {'captured_through': NOW.isoformat(),
                       'games': {'fresh': game(24, 10), 'overdue': game(24, 80)}},
                'nhl': {'games': {}}}
    report = capture_freshness(catalogs, NOW)['nfl']
    assert report['active_published_games'] == 2
    assert report['fresh_games'] == 1
    assert report['stale_games'] == [{'game': 'overdue',
                                      'captured_through': game(24, 80)['captured_through'],
                                      'expected_interval_minutes': 30}]


def test_completed_games_and_slower_tiers_are_not_false_failures():
    catalogs = {'nfl': {'games': {'completed': game(-1, 5000), 'slow': game(400, 700)}},
                'nhl': {'games': {'slow': game(400, 2800), 'middle': game(100, 700)}}}
    report = capture_freshness(catalogs, NOW)
    assert report['nfl']['active_published_games'] == 1
    assert report['nfl']['stale_games'] == []
    assert report['nhl']['fresh_games'] == 2
    assert report['nhl']['stale_games'] == []


def test_missing_or_future_capture_is_reported():
    missing = game(12)
    missing['captured_through'] = None
    catalogs = {'nfl': {'games': {}},
                'nhl': {'games': {'missing': missing, 'future': game(12, -10)}}}
    assert len(capture_freshness(catalogs, NOW)['nhl']['stale_games']) == 2
