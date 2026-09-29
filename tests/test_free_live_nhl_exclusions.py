"""Exact-event exclusion and recovery tests; no browsers, network or DB calls."""
from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import json
import tempfile

import nhl_schedule_collector as nhl
from tools.free_live_hardening import run_nhl
from tools.free_live_nhl_exclusions import apply_exclusions, exclusion_for

NOW = datetime(2026, 9, 29, 15, tzinfo=timezone.utc)
GAME = nhl.ScheduledNHLGame(
    '2026020182', datetime(2026, 10, 25, 23, tzinfo=timezone.utc),
    'Montreal Canadiens', 'Winnipeg Jets', 'Princess Auto Stadium',
    'Montreal Canadiens at Winnipeg Jets', 'America/Winnipeg', 'Canada',
    True, 2, 20262027,
)


def test_only_this_game_is_excluded_not_other_games_between_same_teams():
    assert exclusion_for(GAME)['schedule_id'] == '2026020182'
    for changes in (
        {'schedule_id': '2026020183'}, {'schedule_id': '2027020182'},
        {'away_team': 'Toronto Maple Leafs'}, {'home_team': 'Boston Bruins'},
        {'venue': 'Canada Life Centre'},
    ):
        assert exclusion_for(replace(GAME, **changes)) is None


def test_saved_backlog_is_cleared_without_mutating_other_work():
    other = replace(GAME, schedule_id='2026020183')
    backlog = {g.schedule_id: {'game': asdict(g), 'first_due': NOW.isoformat()}
               for g in (GAME, other)}
    other_entry = backlog[other.schedule_id]
    selected, excluded = apply_exclusions([GAME, other], backlog)
    assert selected == [other]
    assert [x['schedule_id'] for x in excluded] == [GAME.schedule_id]
    assert set(backlog) == {other.schedule_id}
    assert backlog[other.schedule_id] is other_entry


def test_cached_target_stays_excluded_when_absent_from_current_schedule():
    row = asdict(GAME); row['event_date'] = GAME.event_date.isoformat()
    backlog = {GAME.schedule_id: {'game': row}}
    selected, excluded = apply_exclusions([], backlog)
    assert selected == [] and backlog == {}
    assert excluded[0]['policy'] == 'user-approved-single-game'


def test_official_identity_is_not_overridden_by_cached_metadata():
    backlog = {GAME.schedule_id: {'game': asdict(GAME)}}
    changed = replace(GAME, venue='Canada Life Centre')
    selected, excluded = apply_exclusions([changed], backlog)
    assert selected == [changed] and excluded == []
    assert GAME.schedule_id in backlog


def test_known_vivid_title_explains_original_matching_failure():
    row = nhl.DiscoveredNHLGame(
        'https://www.vividseats.com/nhl-heritage-classic-tickets-princess-auto-stadium-10-2-2026--sports-nhl-hockey/production/6393129',
        'Sun Oct 25 6:00pm NHL Heritage Classic - Winnipeg Jets vs Montreal Canadiens',
        GAME.local_date,
    )
    assert nhl.ordered_matchup_from_title(row.title) == tuple(reversed(GAME.matchup_key))
    assert nhl.candidates_for_schedule_game(GAME, [row]) == ()


def run_fake(schedule, root, old_backlog=None, queued=False):
    if old_backlog:
        state = {'pending': old_backlog}
        (root / 'nhl-progress.json').write_text(json.dumps(state, default=str))
    pending = root / 'pending'; pending.mkdir(exist_ok=True)
    saved = pending / 'saved-capture.json'
    if queued:
        saved.write_text('{"saved":"leave untouched"}')
    with ExitStack() as stack:
        clock = stack.enter_context(patch('tools.free_live_hardening.datetime'))
        clock.now.return_value = NOW
        clock.fromisoformat.side_effect = datetime.fromisoformat
        replay = stack.enter_context(patch.object(nhl, 'replay_pending_snapshots', return_value=(0, True, [])))
        stack.enter_context(patch.object(nhl, 'fetch_schedule_games', return_value=(schedule, ['official-fixture'])))
        stack.enter_context(patch.object(nhl, 'schedule_games_due', side_effect=lambda rows, slot: rows))
        discovery = stack.enter_context(patch.object(nhl, 'discover_nhl_games', return_value=([], [])))
        resolver = stack.enter_context(patch.object(nhl, 'resolve_schedule_games', return_value=([], [])))
        capture = stack.enter_context(patch.object(nhl, '_capture_resolution'))
        status = run_nhl('', '', True, 1, root/'health.json', pending)
        result = json.loads((root/'health.json').read_text())
        state = json.loads((root/'nhl-progress.json').read_text())
        return status, result, state, discovery.call_count, capture.call_count


def test_live_entrypoint_clears_target_reports_exclusion_and_does_not_search():
    with tempfile.TemporaryDirectory() as path:
        row = asdict(GAME); row['event_date'] = GAME.event_date.isoformat()
        backlog = {GAME.schedule_id: {'game': row, 'first_due': NOW.isoformat()}}
        status, report, state, searches, captures = run_fake([GAME], Path(path), backlog)
        assert status == 0 and report['status'] == 'healthy'
        assert searches == 0 and captures == 0
        assert report['scheduled_in_window'] == 1
        assert report['scheduled_in_scope'] == 0
        assert report['scheduled_due'] == 0 and report['committed'] == 0
        assert report['excluded_games'][0]['schedule_id'] == GAME.schedule_id
        assert report['unfinished_games'] == [] and state['pending'] == {}


def test_other_unresolved_game_still_fails_and_remains_retryable():
    with tempfile.TemporaryDirectory() as path:
        other = replace(GAME, schedule_id='2026020183')
        status, report, state, searches, captures = run_fake([GAME, other], Path(path))
        assert status == 1 and report['status'] == 'degraded'
        assert searches == 1 and captures == 0
        assert report['unresolved'] == [other.schedule_id]
        assert report['unfinished_games'] == [other.schedule_id]
        assert set(state['pending']) == {other.schedule_id}


def test_exclusion_never_deletes_valid_pending_capture_files():
    with tempfile.TemporaryDirectory() as path:
        root = Path(path)
        status, report, _, _, _ = run_fake([GAME], root, queued=True)
        assert status == 1 and report['pending'] == 1
        assert (root/'pending/saved-capture.json').read_text() == '{"saved":"leave untouched"}'
