"""Comparable report cohorts. This module never changes stored observations."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from math import ceil
import re
from statistics import mean, median
from typing import Any
import unicodedata

MIN_RANK_GAMES = 3
MIN_RANK_COVERAGE = 0.60


def clean(value: Any) -> str:
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).split())


def normalized(value: Any) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', clean(value).casefold()).strip()


def preseason_title(value: Any) -> bool:
    return bool(re.search(r'\b(?:pre[\s-]*season|exhibition|spring[\s-]*training)\b', clean(value), re.I))


def is_preseason(sport: str, event: Any) -> bool:
    if preseason_title(getattr(event, 'title', '')):
        return True
    kind = getattr(event, 'game_type', None)
    if sport == 'nhl':
        if str(kind) == '1':
            return True
        # NHL official game IDs encode the game type after the season year.
        schedule_id = str(getattr(event, 'schedule_id', '') or '')
        if kind is None and re.fullmatch(r'20\d{2}01\d{4}', schedule_id):
            return True
    if sport == 'nfl':
        if str(kind) == '1':
            return True
        value = getattr(event, 'event_date', None)
        if isinstance(value, datetime):
            # Historical NFL rows lack season_type. July/August are preseason;
            # the verified 2026 regular-season opener is September 9.
            if value.month in (7, 8):
                return True
            if value.year == 2026 and value.month == 9 and value.day < 9:
                return True
    return False


def season_key(sport: str, event: Any) -> int:
    value = event.event_date
    return value.year - int(sport in ('nfl', 'nhl') and value.month < 7)


def season_label(sport: str, year: int | None) -> str:
    if year is None:
        return ''
    return f'{year}–{str(year + 1)[-2:]}' if sport == 'nhl' else str(year)


# Only aliases for the SAME building. A team's alternate arenas are NOT aliases.
_VENUE_GROUPS = (
    ('Dodger Stadium', ('Dodgers Stadium', 'Uniqlo Field at Dodger Stadium')),
    ('Oriole Park at Camden Yards', ('Camden Yards',)),
    ('Angel Stadium', ('Angel Stadium of Anaheim',)),
    ('U.S. Bank Stadium', ('US Bank Stadium', 'U S Bank Stadium')),
)
_VENUE_CANONICAL = {
    normalized(alias): canonical
    for canonical, aliases in _VENUE_GROUPS
    for alias in (canonical, *aliases)
}


def report_venue(value: Any) -> str:
    return _VENUE_CANONICAL.get(normalized(value), clean(value))


def venue_aliases(value: Any) -> tuple[str, ...]:
    canonical = report_venue(value)
    for name, aliases in _VENUE_GROUPS:
        if name == canonical:
            return (name, *aliases)
    return (clean(value),)


def latest_season_events(events: list[Any], sport: str) -> tuple[list[Any], int | None]:
    eligible = [e for e in events if not is_preseason(sport, e)]
    # A new season with preseason-only data must not inherit last year's rankings.
    year = max((season_key(sport, e) for e in events), default=None)
    return [e for e in eligible if season_key(sport, e) == year], year


def ranking_slots(sport: str, bucket_count: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
    # Last three buckets = 12–24h / 6–12h / final 6h in every sport.
    # Last five = final 48h for MLB and final 7 days for NFL/NHL.
    return tuple(range(bucket_count - 3, bucket_count)), tuple(range(bucket_count - 5, bucket_count))


def add_ranking_evidence(
    sections: list[dict[str, Any]], prepared: dict, events: list[Any],
    *, sport: str, now: datetime, buckets: tuple, event_utc: Any,
) -> None:
    """Add fixed-window evidence without changing exploratory chart statistics.

    Missing windows are never interpolated. Every included game has the same
    windows; each game receives equal weight. Drops use >=3 observations per
    window and the median of per-game maximum declines. Coverage is measured
    against ALL completed games in the report's latest season and venue.
    """
    cohort, year = latest_season_events(events, sport)
    completed = {int(e.id) for e in cohort if event_utc(e.event_date) <= now}
    denominator = len(completed)
    required = max(MIN_RANK_GAMES, ceil(MIN_RANK_COVERAGE * denominator))
    price_slots, drop_slots = ranking_slots(sport, len(buckets))
    prices = defaultdict(list)
    drops = defaultdict(list)
    ambiguous = set()
    numeric_families = defaultdict(set)
    labels = [row['name'] for row in sections]
    for event in cohort:
        labels.extend(getattr(event, 'sections', None) or getattr(event, 'event_sections', None) or [])
    for name in labels:
        label = normalized(name)
        number = re.search(r'\b\d+[a-z]?\b$', label)
        if number:
            numeric_families[number.group()].add(re.sub(r'\b\d+[a-z]?\b$', '', label).strip())
    for row in sections:
        label = normalized(row['name'])
        if re.fullmatch(r'\d+[a-z]?', label) and len(numeric_families[label]) > 1:
            ambiguous.add(row['section_key'])

    for (key, event_id), points in prepared.items():
        if int(event_id) not in completed:
            continue
        by_slot = {int(p['slot']): p for p in points}
        # Duplicate alias summaries must be rebuilt, not averaged as medians.
        if len(by_slot) != len(points):
            continue
        if all(s in by_slot for s in price_slots):
            weights = [buckets[s][1] - buckets[s][0] for s in price_slots]
            prices[key].append(sum(float(by_slot[s]['price']) * w for s, w in zip(price_slots, weights)) / sum(weights))
        if all(s in by_slot and int(by_slot[s].get('observation_count', 0)) >= 3 for s in drop_slots):
            values = [float(by_slot[s]['price']) for s in drop_slots]
            best = max((max(0.0, 1.0 - low / high) * 100 for i, high in enumerate(values) if high > 0 for low in values[i+1:]), default=0.0)
            drops[key].append(best)

    for row in sections:
        key = row['section_key']
        p, d = prices[key], drops[key]
        row.update({
            'ranking_price': mean(p) if p else None,
            'ranking_price_games': len(p),
            'ranking_drop_percent': median(d) if d else None,
            'ranking_drop_games': len(d),
            'ranking_drop_material_games': sum(value >= 20 for value in d),
            'ranking_total_games': denominator,
            'ranking_required_games': required,
            'ranking_price_eligible': len(p) >= required and key not in ambiguous,
            'ranking_drop_eligible': len(d) >= required and key not in ambiguous,
            'ranking_ambiguous_label': key in ambiguous,
            'ranking_season': season_label(sport, year),
        })
