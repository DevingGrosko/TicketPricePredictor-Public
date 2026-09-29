"""Explicit user-approved exclusions for the free NHL collector only.

Do not treat unknown provider failures as exclusions. Existing captured payloads
and database history are not deleted. See docs/free-live-nhl-exclusion.md.
"""
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from typing import Any

EXCLUDED_SCHEDULE_ID = '2026020182'
EXCLUSION_REASON = (
    'User-approved one-game exclusion: 2026 Heritage Classic at Princess Auto '
    'Stadium. Vivid lists Winnipeg Jets vs Montreal Canadiens (home first), '
    'while the ordinary matcher requires the official away/home order. '
    'Verified 2026-09-29; keep ordinary game identity checks unchanged.'
)


def exclusion_for(game: Any) -> dict[str, Any] | None:
    """Match this exact scheduled event, not a team, venue or event category."""
    row = dict(game) if isinstance(game, Mapping) else asdict(game)
    if (
        str(row.get('schedule_id')) != EXCLUDED_SCHEDULE_ID
        or row.get('away_team') != 'Montreal Canadiens'
        or row.get('home_team') != 'Winnipeg Jets'
        or row.get('venue') != 'Princess Auto Stadium'
    ):
        return None
    stamp = row.get('event_date')
    if isinstance(stamp, datetime):
        stamp = stamp.isoformat()
    return {
        'schedule_id': EXCLUDED_SCHEDULE_ID,
        'away_team': row['away_team'], 'home_team': row['home_team'],
        'venue': row['venue'], 'event_date': stamp,
        'reason': EXCLUSION_REASON,
        'policy': 'user-approved-single-game',
    }


def apply_exclusions(schedule: list, backlog: dict) -> tuple[list, list[dict]]:
    """Filter fresh work and clear only the same event's saved retry entry.

    The live official identity wins over cached metadata. No snapshot files,
    completion receipts, or database rows are touched.
    """
    active, excluded = [], {}
    official_ids = {str(game.schedule_id) for game in schedule}
    for game in schedule:
        decision = exclusion_for(game)
        if decision is None:
            active.append(game)
            continue
        identity = decision['schedule_id']
        excluded[identity] = decision
        backlog.pop(identity, None)
    for identity, entry in list(backlog.items()):
        if str(identity) != EXCLUDED_SCHEDULE_ID or str(identity) in official_ids:
            continue
        decision = exclusion_for(entry.get('game', {}))
        if decision is not None and decision['schedule_id'] == str(identity):
            excluded[str(identity)] = decision
            del backlog[identity]
    return active, [excluded[key] for key in sorted(excluded)]
