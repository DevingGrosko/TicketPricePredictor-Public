"""Read three fixed official NHL identities; no browser, provider, or store calls."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from zoneinfo import ZoneInfo


SCHEDULE_IDS = frozenset({'2026020264', '2026020259', '2026020260'})


def audit(*, fetcher=None, now=None):
    import nhl_schedule_collector as official
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Official audit requires an aware retrieval timestamp')
    raw_rows = {}

    def read(url, timeout):
        value = (fetcher or official.fetch_json)(url, timeout)
        for day in value.get('gameWeek') or []:
            for game in day.get('games') or []:
                identity = str(game.get('id'))
                if identity in SCHEDULE_IDS:
                    raw_rows[identity] = {
                        'source_url': url,
                        'startTimeUTC': game.get('startTimeUTC'),
                        'venueTimezone': game.get('venueTimezone'),
                        'venueUTCOffset': game.get('venueUTCOffset'),
                        'easternUTCOffset': game.get('easternUTCOffset'),
                        'away_abbreviation': (game.get('awayTeam') or {}).get('abbrev'),
                        'home_abbreviation': (game.get('homeTeam') or {}).get('abbrev'),
                    }
        return value

    games, sources = official.fetch_schedule_games(now, fetcher=read)
    selected = {str(game.schedule_id): game for game in games if str(game.schedule_id) in SCHEDULE_IDS}
    if set(selected) != SCHEDULE_IDS or set(raw_rows) != SCHEDULE_IDS:
        raise ValueError('Official source did not contain all three exact scheduled identities')
    rows = []
    for identity in sorted(SCHEDULE_IDS):
        game = selected[identity]
        rows.append(dict(schedule_id=identity, away_team=game.away_team, home_team=game.home_team,
            event_date=game.event_date.isoformat(), venue=game.venue, venue_timezone=game.venue_timezone,
            local_datetime=game.event_date.astimezone(ZoneInfo(game.venue_timezone)).isoformat(),
            official_fields=raw_rows[identity]))
    return dict(status='success', retrieved_at=now.astimezone(timezone.utc).isoformat(),
        finished_at=datetime.now(timezone.utc).isoformat(), source_urls=sources, games=rows,
        browser_calls=0, vivid_requests=0, database_calls=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        value = audit()
        code = 0
    except Exception as exc:
        value = dict(status='failed', error_type=type(exc).__name__,
            finished_at=datetime.now(timezone.utc).isoformat(), browser_calls=0,
            vivid_requests=0, database_calls=0)
        code = 1
    text = json.dumps(value, indent=2)
    Path(args.output).write_text(text + '\n')
    print('NHL_OFFICIAL_SCHEDULE_AUDIT ' + text, flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
