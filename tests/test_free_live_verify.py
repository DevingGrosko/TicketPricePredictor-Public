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



def scheduled(sport, identity='scheduled-game', lead_hours=24):
    from nfl_schedule_collector import ScheduledNFLGame
    from nhl_schedule_collector import ScheduledNHLGame
    cls = ScheduledNFLGame if sport == 'nfl' else ScheduledNHLGame
    return cls(identity, NOW+timedelta(hours=lead_hours),
        'Dallas Cowboys' if sport == 'nfl' else 'Boston Bruins',
        'New York Giants' if sport == 'nfl' else 'Toronto Maple Leafs',
        'MetLife Stadium' if sport == 'nfl' else 'Scotiabank Arena', 'Game')


def test_recent_faster_tier_transition_does_not_call_on_time_old_capture_stale():
    for sport, lead, age in [('nfl', 336, 7*60), ('nfl', 168, 2*60),
                             ('nhl', 336, 30*60), ('nhl', 168, 15*60), ('nhl', 72, 5*60)]:
        catalogs = {'nfl': {'games': {}}, 'nhl': {'games': {}}}
        catalogs[sport]['games']['transition'] = game(lead, age)
        assert capture_freshness(catalogs, NOW)[sport]['stale_games'] == []
        interval = {'nfl': {336:3,168:0.5}, 'nhl': {336:12,168:6,72:0.5}}[sport][lead]
        later = NOW+timedelta(hours=interval*2, minutes=16)
        assert len(capture_freshness(catalogs, later)[sport]['stale_games']) == 1


def test_tier_transition_does_not_forgive_a_capture_already_stale_before_it():
    catalogs = {'nfl': {'games': {'old': game(168, 7*60)}},
                'nhl': {'games': {'old': game(72, 13*60)}}}
    result = capture_freshness(catalogs, NOW)
    assert len(result['nfl']['stale_games']) == 1
    assert len(result['nhl']['stale_games']) == 1


def test_official_schedule_matches_every_captured_game_and_detects_missing_new_game():
    catalogs = {'nfl': {'games': {'price-id': {**game(24,10), 'schedule_id':'nfl-game'}}},
                'nhl': {'games': {'price-id': {**game(24,10), 'schedule_id':'nhl-game'}}}}
    schedules = {sport: [scheduled(sport, sport+'-game')] for sport in ('nfl','nhl')}
    result = capture_freshness(catalogs, NOW, schedules)
    for sport in ('nfl','nhl'):
        assert result[sport]['schedule_coverage']['expected_scheduled_games'] == 1
        assert result[sport]['schedule_coverage']['missing_due_games'] == []
    schedules['nfl'].append(scheduled('nfl','never-captured'))
    result = capture_freshness(catalogs, NOW, schedules)
    missing = result['nfl']['schedule_coverage']['missing_due_games']
    assert [row['schedule_id'] for row in missing] == ['never-captured']


def test_empty_catalog_does_not_prove_empty_official_schedule():
    catalogs = {'nfl': {'games': {}}, 'nhl': {'games': {}}}
    result = capture_freshness(catalogs, NOW, {'nfl':[scheduled('nfl')], 'nhl':[]})
    assert len(result['nfl']['schedule_coverage']['missing_due_games']) == 1
    assert result['nhl']['schedule_coverage']['expected_scheduled_games'] == 0
    assert result['nhl']['schedule_coverage']['missing_due_games'] == []


def test_recently_entered_game_gets_its_first_cadence_phase_before_being_missing():
    from tools.free_live_verify import first_capture_deadline
    new = scheduled('nhl', lead_hours=720)
    catalogs = {'nfl': {'games': {}}, 'nhl': {'games': {}}}
    schedules = {'nfl':[], 'nhl':[new]}
    result = capture_freshness(catalogs, NOW, schedules)['nhl']['schedule_coverage']
    assert result['missing_due_games'] == []
    assert len(result['awaiting_first_capture']) == 1
    after = first_capture_deadline('nhl', new)+timedelta(seconds=1)
    assert len(capture_freshness(catalogs, after, schedules)['nhl']['schedule_coverage']['missing_due_games']) == 1


def test_same_schedule_id_at_old_event_time_does_not_hide_rescheduled_game():
    catalogs = {'nfl': {'games': {'old': {**game(-24,1000), 'schedule_id':'rescheduled'}}},
                'nhl': {'games': {}}}
    result = capture_freshness(catalogs, NOW, {'nfl':[scheduled('nfl','rescheduled')], 'nhl':[]})
    assert len(result['nfl']['schedule_coverage']['missing_due_games']) == 1


def test_official_lookup_keeps_one_game_exclusion_and_reports_unavailable_source(monkeypatch):
    import nfl_schedule_collector as nfl
    import nhl_schedule_collector as nhl
    from tools.free_live_verify import official_schedules
    excluded = nhl.ScheduledNHLGame('2026020182', NOW+timedelta(hours=100),
        'Montreal Canadiens', 'Winnipeg Jets', 'Princess Auto Stadium', 'Heritage Classic')
    monkeypatch.setattr(nfl, 'fetch_schedule_games', lambda now, **kwargs: ([], 'fixture'))
    monkeypatch.setattr(nhl, 'fetch_schedule_games', lambda now, **kwargs: ([excluded], ['fixture']))
    schedules, errors, exclusions = official_schedules(NOW)
    assert schedules == {'nfl':[], 'nhl':[]}
    assert errors == {}
    assert exclusions['nhl'] == {'2026020182'}
    def unavailable(now, **kwargs):
        raise TimeoutError('arbitrary provider details')
    monkeypatch.setattr(nfl, 'fetch_schedule_games', unavailable)
    schedules, errors, exclusions = official_schedules(NOW)
    assert 'nfl' not in schedules
    assert errors['nfl'] == 'Official NFL schedule unavailable: TimeoutError'


def mock_published_site(monkeypatch, schedules, errors=None):
    import io
    import json
    import hashlib
    from types import SimpleNamespace
    from tools import free_live_verify as verifier
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(verifier, 'datetime', Clock)
    catalogs = {sport:{'captured_through':NOW.isoformat(), 'games':{}} for sport in ('mlb','nfl','nhl')}
    blobs = {}
    sports = {}
    for sport, catalog in catalogs.items():
        raw = json.dumps(catalog).encode()
        name = 'catalog-'+hashlib.sha256(raw).hexdigest()+'.json'
        blobs[name] = raw
        sports[sport] = '/native/'+name
    manifest = {'generated_at':'expected', 'live_updates_enabled':True, 'sports':sports}
    def open_url(request, timeout):
        raw = json.dumps(manifest).encode() if request.full_url.endswith('/original-manifest.json') else blobs[request.full_url.rsplit('/',1)[-1]]
        return io.BytesIO(raw)
    monkeypatch.setattr(verifier, 'build_opener', lambda *args: SimpleNamespace(open=open_url))
    monkeypatch.setattr(verifier, 'official_schedules', lambda now: (schedules, errors or {}, {}))
    return verifier


def test_verifier_fails_after_publication_for_missing_game(monkeypatch):
    import pytest
    verifier = mock_published_site(monkeypatch, {'nfl':[scheduled('nfl')], 'nhl':[]})
    with pytest.raises(RuntimeError, match='missing price captures'):
        verifier.verify('expected')


def test_verifier_reports_clear_unknown_coverage_warning_when_feed_unavailable(monkeypatch, capsys):
    import pytest
    verifier = mock_published_site(monkeypatch, {'nhl':[]}, {'nfl':'Official NFL schedule unavailable: TimeoutError'})
    with pytest.raises(RuntimeError, match='coverage unavailable'):
        verifier.verify('expected')
    assert 'FREE_LIVE_COVERAGE_WARNING' in capsys.readouterr().out



def test_error_json_cannot_be_mistaken_for_a_legitimately_empty_schedule(monkeypatch):
    import nfl_schedule_collector as nfl
    import nhl_schedule_collector as nhl
    from tools.free_live_verify import official_schedules
    monkeypatch.setattr(nfl, 'fetch_json', lambda *args: {'error':'temporary upstream failure'})
    monkeypatch.setattr(nhl, 'fetch_json', lambda *args: {'error':'temporary upstream failure'})
    schedules, errors, excluded = official_schedules(NOW)
    assert schedules == {}
    assert set(errors) == {'nfl','nhl'}


def test_public_version_recovers_from_network_error_and_old_manifest_without_redeploy(tmp_path):
    import json
    from urllib.error import URLError
    from tools.free_live_verify import wait_for_publication
    elapsed, waits, calls = [0], [], []
    outcomes = [URLError('private network detail'), {'generated_at':'previous'}, {'generated_at':'expected'}]
    def read(path, timeout):
        calls.append((path, timeout))
        value = outcomes.pop(0)
        if isinstance(value, Exception):
            raise value
        return value
    def sleep(seconds):
        waits.append(seconds); elapsed[0] += seconds
    report_path, output_path = tmp_path/'version.json', tmp_path/'step-output'
    value, result = wait_for_publication(read, 'expected', report_path=report_path, output_path=output_path,
                                         clock=lambda:elapsed[0], sleep=sleep)
    assert value['generated_at'] == 'expected'
    assert result['version_verified'] is True
    assert result['attempts'] == 3
    assert result['wait_seconds'] == 30
    assert waits == [15,15]
    assert all(path == '/original-manifest.json' and timeout <= 20 for path,timeout in calls)
    assert json.loads(report_path.read_text())['version_verified'] is True
    assert output_path.read_text().splitlines()[-1] == 'version_verified=true'
    assert 'private network detail' not in report_path.read_text()


def test_unavailable_version_has_hard_deadline_and_false_report(tmp_path):
    import json
    import pytest
    from urllib.error import URLError
    from tools.free_live_verify import wait_for_publication
    elapsed, waits, calls = [0], [], []
    def read(path, timeout):
        calls.append(timeout)
        raise URLError('withheld error details')
    def sleep(seconds):
        waits.append(seconds); elapsed[0] += seconds
    report_path = tmp_path/'version.json'
    with pytest.raises(RuntimeError, match='Expected publication not available'):
        wait_for_publication(read, 'expected', timeout=31, report_path=report_path,
                             clock=lambda:elapsed[0], sleep=sleep)
    result = json.loads(report_path.read_text())
    assert result['version_verified'] is False
    assert result['wait_seconds'] == 31
    assert calls == [20,16,1]
    assert waits == [15,15,1]
    assert max(waits) <= 15
    for timeout in (601, True, 0):
        with pytest.raises(ValueError):
            wait_for_publication(read, 'expected', timeout=timeout)


def test_schedule_failure_preserves_proved_version_for_artifact_cleanup(monkeypatch, tmp_path):
    import json
    import pytest
    verifier = mock_published_site(monkeypatch, {'nfl':[scheduled('nfl')], 'nhl':[]})
    report_path, output_path = tmp_path/'version.json', tmp_path/'step-output'
    with pytest.raises(RuntimeError, match='missing price captures'):
        verifier.verify('expected', report_path=report_path, output_path=output_path)
    assert json.loads(report_path.read_text())['version_verified'] is True
    assert output_path.read_text().splitlines()[-1] == 'version_verified=true'


def test_feed_unavailable_preserves_version_true_but_remains_a_failure(monkeypatch, tmp_path):
    import json
    import pytest
    verifier = mock_published_site(monkeypatch, {'nhl':[]}, {'nfl':'Official NFL schedule unavailable: TimeoutError'})
    report_path = tmp_path/'version.json'
    with pytest.raises(RuntimeError, match='coverage unavailable'):
        verifier.verify('expected', report_path=report_path)
    assert json.loads(report_path.read_text())['version_verified'] is True
