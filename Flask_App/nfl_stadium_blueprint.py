"""Team-first venue summaries for NFL, NHL, and MLB ticket history.

Every section comparison is calculated at the game level first. A game with
more collection snapshots therefore contributes no more weight than a game
with fewer snapshots.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import re
from statistics import mean, median
from typing import Any, Callable, Iterable

from flask import Blueprint, render_template, request, url_for
from sqlalchemy import func, or_, select
from sqlalchemy.orm import defer, load_only

from models import (
    CreateModel,
    database_path,
    Event,
    Iteration,
    Ticket,
    clean_event_title,
    event_datetime_eastern,
    event_datetime_utc,
    event_has_complete_public_data,
    hours_before_event,
)
from Flask_App.nfl_blueprint import (
    CreateNFLModel,
    nfl_database_path,
    NFLEvent,
    NFLIteration,
    NFLTicket,
    format_nfl_title,
    geometry_is_usable,
    geometry_section_count,
    nfl_display_venue,
    nfl_event_home_team,
    sanitize_map_geometry,
)
from Flask_App.nhl_blueprint import (
    CreateNHLModel,
    nhl_database_path,
    NHLEvent,
    NHLIteration,
    NHLTicket,
    format_nhl_title,
    nhl_display_venue,
    nhl_event_home_team,
)

from Flask_App.section_canonicalization import section_identity
from Flask_App.database_config import dispose_ticket_engine
from Flask_App.materialized_analytics import (
    TIMELINE_BUCKETS,
    read_summary_rows,
    venue_revision,
)
from Flask_App.performance_cache import (
    PAGE_CACHE_TTL_SECONDS,
    cache_key,
    file_version,
    page_cache,
)


nfl_stadium_blueprint = Blueprint("nfl_stadium", __name__)
MIN_DROP_SPAN = timedelta(hours=6)
LOW_SAMPLE_GAMES = 3
MATERIAL_DROP_PERCENT = 20.0
MLB_URL_MARKER = "--sports-mlb-baseball/"

_TIMELINE_BUCKETS = TIMELINE_BUCKETS

_EVENT_MOMENT_LABELS = {
    "mlb": "first pitch",
    "nfl": "kickoff",
    "nhl": "puck drop",
}


_MLB_VENUE_TEAMS = {
    "american family field": "Milwaukee Brewers",
    "angel stadium": "Los Angeles Angels",
    "angel stadium of anaheim": "Los Angeles Angels",
    "busch stadium": "St. Louis Cardinals",
    "camden yards": "Baltimore Orioles",
    "chase field": "Arizona Diamondbacks",
    "citi field": "New York Mets",
    "citizens bank park": "Philadelphia Phillies",
    "comerica park": "Detroit Tigers",
    "coors field": "Colorado Rockies",
    "daikin park": "Houston Astros",
    "dodger stadium": "Los Angeles Dodgers",
    "dodgers stadium": "Los Angeles Dodgers",
    "fenway park": "Boston Red Sox",
    "george m steinbrenner field": "Tampa Bay Rays",
    "globe life field": "Texas Rangers",
    "great american ball park": "Cincinnati Reds",
    "guaranteed rate field": "Chicago White Sox",
    "kauffman stadium": "Kansas City Royals",
    "loandepot park": "Miami Marlins",
    "minute maid park": "Houston Astros",
    "nationals park": "Washington Nationals",
    "oakland coliseum": "Athletics",
    "oracle park": "San Francisco Giants",
    "oriole park at camden yards": "Baltimore Orioles",
    "petco park": "San Diego Padres",
    "pnc park": "Pittsburgh Pirates",
    "progressive field": "Cleveland Guardians",
    "rate field": "Chicago White Sox",
    "rogers centre": "Toronto Blue Jays",
    "sutter health park": "Athletics",
    "t mobile park": "Seattle Mariners",
    "target field": "Minnesota Twins",
    "tropicana field": "Tampa Bay Rays",
    "truist park": "Atlanta Braves",
    "wrigley field": "Chicago Cubs",
    "yankee stadium": "New York Yankees",
}

_MLB_TEAM_ALIASES = {
    "Arizona Diamondbacks": ("arizona diamondbacks", "diamondbacks", "d backs"),
    "Athletics": ("athletics", "oakland athletics", "a s"),
    "Atlanta Braves": ("atlanta braves", "braves"),
    "Baltimore Orioles": ("baltimore orioles", "orioles"),
    "Boston Red Sox": ("boston red sox", "red sox"),
    "Chicago Cubs": ("chicago cubs", "cubs"),
    "Chicago White Sox": ("chicago white sox", "white sox"),
    "Cincinnati Reds": ("cincinnati reds", "reds"),
    "Cleveland Guardians": ("cleveland guardians", "guardians"),
    "Colorado Rockies": ("colorado rockies", "rockies"),
    "Detroit Tigers": ("detroit tigers", "tigers"),
    "Houston Astros": ("houston astros", "astros"),
    "Kansas City Royals": ("kansas city royals", "royals"),
    "Los Angeles Angels": ("los angeles angels", "la angels", "angels"),
    "Los Angeles Dodgers": ("los angeles dodgers", "la dodgers", "dodgers"),
    "Miami Marlins": ("miami marlins", "marlins"),
    "Milwaukee Brewers": ("milwaukee brewers", "brewers"),
    "Minnesota Twins": ("minnesota twins", "twins"),
    "New York Mets": ("new york mets", "ny mets", "mets"),
    "New York Yankees": ("new york yankees", "ny yankees", "yankees"),
    "Philadelphia Phillies": ("philadelphia phillies", "phillies"),
    "Pittsburgh Pirates": ("pittsburgh pirates", "pirates"),
    "San Diego Padres": ("san diego padres", "padres"),
    "San Francisco Giants": ("san francisco giants", "sf giants", "giants"),
    "Seattle Mariners": ("seattle mariners", "mariners"),
    "St. Louis Cardinals": ("st louis cardinals", "cardinals"),
    "Tampa Bay Rays": ("tampa bay rays", "rays"),
    "Texas Rangers": ("texas rangers", "rangers"),
    "Toronto Blue Jays": ("toronto blue jays", "blue jays"),
    "Washington Nationals": ("washington nationals", "nationals", "nats"),
}

_NHL_COUNTRY_MARKERS = frozenset(
    {
        "",
        "ca",
        "canada",
        "us",
        "usa",
        "united states",
        "united states of america",
    }
)


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _normalize(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _clean(value).casefold()).strip()


def is_parking_section(value: Any) -> bool:
    # Parking products are inventory, not seating sections.
    normalized = _normalize(value)
    if not normalized:
        return False
    return bool(
        re.search(r"\bparking\b", normalized)
        or re.match(r"^(?:lot|garage)\b", normalized)
        or "park and ride" in normalized
    )


def _public_sections(values: Iterable[Any]) -> list[str]:
    return sorted(
        {
            _clean(value)
            for value in values
            if _clean(value) and not is_parking_section(value)
        },
        key=str.casefold,
    )


def _money(value: float | None) -> str:
    return _currency_money(value, "USD")


def _currency_money(value: float | None, currency: str) -> str:
    if value is None:
        return "—"
    # round(int, 2) returns an int; normalize first so zero-dollar values
    # can safely use float.is_integer() below.
    rounded = round(float(value), 2)
    code = _clean(currency).upper() or "USD"
    prefix = "$" if code == "USD" else "CA$" if code == "CAD" else f"{code} "
    rendered = f"{rounded:,.0f}" if rounded.is_integer() else f"{rounded:,.2f}"
    return f"{prefix}{rendered}"


def _price_change(value: float | None) -> str:
    return _currency_price_change(value, "USD")


def _currency_price_change(value: float | None, currency: str) -> str:
    """Format a first-to-latest price change from the buyer's perspective."""
    if value is None:
        return "—"
    if abs(value) < 0.005:
        return _currency_money(0, currency)
    sign = "−" if value > 0 else "+"
    return f"{sign}{_currency_money(abs(value), currency)}"


def _percent_change(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value) < 0.05:
        return "0.0%"
    sign = "−" if value > 0 else "+"
    return f"{sign}{abs(value):.1f}%"


def _column(table: Any, *names: str) -> Any:
    for name in names:
        if name in table.c:
            return table.c[name]
    raise RuntimeError(
        f"Missing expected column in {table.name}: {', '.join(names)}"
    )


def _event_completed(event: Any, now: datetime) -> bool:
    event_date = event_datetime_eastern(event.event_date)
    return event_date <= now.astimezone(event_date.tzinfo)


def _country_label(value: Any) -> str:
    return _clean(value).casefold().replace(".", "").strip()


def _is_us_event(event: NFLEvent) -> bool:
    country = _country_label(getattr(event, "country", ""))
    return not country or country in {
        "us",
        "usa",
        "united states",
        "united states of america",
    }


def _is_supported_nhl_event(event: NHLEvent) -> bool:
    return _country_label(getattr(event, "country", "")) in _NHL_COUNTRY_MARKERS



def _stadium_index(
    events: Iterable[NFLEvent],
    now: datetime,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[NFLEvent]] = defaultdict(list)
    for event in events:
        venue = _clean(nfl_display_venue(event))
        if venue and _is_us_event(event):
            grouped[venue].append(event)

    result = []
    for venue, venue_events in grouped.items():
        teams = sorted(
            {
                _clean(nfl_event_home_team(event))
                for event in venue_events
                if _clean(nfl_event_home_team(event))
            }
        )
        completed_count = sum(_event_completed(event, now) for event in venue_events)
        result.append(
            {
                "venue": venue,
                "game_count": len(venue_events),
                "completed_count": completed_count,
                "upcoming_count": len(venue_events) - completed_count,
                "team_label": " / ".join(teams[:2]),
                "url": url_for("nfl_stadium.nfl_stadium", venue=venue),
            }
        )
    return sorted(
        result,
        key=lambda row: (
            (row["team_label"] or row["venue"]).casefold(),
            row["venue"].casefold(),
        ),
    )



def _snapshot_rows(session: Any, event_ids: list[int]) -> list[Any]:
    return _snapshot_rows_for(
        session,
        event_ids,
        NFLIteration,
        NFLTicket,
    )



def _snapshot_rows_for(
    session: Any,
    event_ids: list[int],
    iteration_model: Any,
    ticket_model: Any,
    *,
    sections: Iterable[str] | None = None,
) -> list[Any]:
    """Fetch raw observations, optionally restricted to selected section labels."""

    if not event_ids:
        return []

    iteration = iteration_model.__table__
    ticket = ticket_model.__table__
    iteration_id = _column(iteration, "id")
    iteration_event_id = _column(iteration, "event_id")
    captured_at = _column(iteration, "captured_at", "created_at")
    ticket_iteration_id = _column(ticket, "iteration_id")
    ticket_section = _column(ticket, "section", "section_name")
    ticket_price = _column(ticket, "price")

    statement = (
        select(
            iteration_event_id.label("event_id"),
            captured_at.label("captured_at"),
            ticket_section.label("section"),
            ticket_price.label("price"),
        )
        .select_from(ticket.join(iteration, ticket_iteration_id == iteration_id))
        .where(iteration_event_id.in_(event_ids))
    )
    selected_sections = sorted(
        {_clean(section).casefold() for section in sections or [] if _clean(section)}
    )
    if selected_sections:
        statement = statement.where(func.lower(ticket_section).in_(selected_sections))
    return list(session.execute(statement).all())



def _event_chunks(events: Iterable[Any], size: int = 32) -> Iterable[list[Any]]:
    rows = list(events)
    for start in range(0, len(rows), max(size, 1)):
        yield rows[start : start + max(size, 1)]


def _capture_counts_for(
    session: Any,
    event_ids: list[int],
    iteration_model: Any,
) -> dict[int, int]:
    """Count stored snapshot iterations without scanning the ticket table."""

    if not event_ids:
        return {}
    iteration = iteration_model.__table__
    iteration_id = _column(iteration, "id")
    iteration_event_id = _column(iteration, "event_id")
    statement = (
        select(
            iteration_event_id.label("event_id"),
            func.count(iteration_id).label("capture_count"),
        )
        .where(iteration_event_id.in_(event_ids))
        .group_by(iteration_event_id)
    )
    return {
        int(row.event_id): int(row.capture_count)
        for row in session.execute(statement)
    }


def _bucket_summary_rows_for(
    session: Any,
    events: Iterable[Any],
    iteration_model: Any,
    ticket_model: Any,
    sport_key: str,
    *,
    sections: Iterable[str] | None = None,
) -> list[Any]:
    """Read precomputed game/section/time-window medians.

    The iteration and ticket model parameters remain for caller compatibility;
    user-facing requests no longer scan or sort raw ticket observations.
    """

    del iteration_model, ticket_model
    event_rows = list(events)
    section_keys: set[str] = set()
    if sections:
        venues = {
            _event_venue_for_sport(event, sport_key)
            for event in event_rows
            if _event_venue_for_sport(event, sport_key)
        }
        for venue in venues:
            for section in sections:
                identity = section_identity(sport_key, venue, section)
                if identity is not None:
                    section_keys.add(identity.key)
    return read_summary_rows(
        session,
        [int(event.id) for event in event_rows],
        section_keys=section_keys or None,
    )


def _event_venue_for_sport(event: Any, sport_key: str) -> str:
    if sport_key == "mlb":
        return _clean(getattr(event, "Place", ""))
    if sport_key == "nfl":
        return _clean(nfl_display_venue(event))
    if sport_key == "nhl":
        return _clean(nhl_display_venue(event))
    return ""


def _row_value(row: Any, name: str) -> Any:
    if hasattr(row, name):
        return getattr(row, name)
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        try:
            return mapping[name]
        except (KeyError, TypeError):
            return None
    if isinstance(row, dict):
        return row.get(name)
    return None


def _supported_area_count(
    events: Iterable[Any],
    rows: Iterable[Any],
    sport_key: str,
    minimum_games: int = LOW_SAMPLE_GAMES,
) -> int:
    """Count canonical ticket areas represented in enough distinct games."""

    event_by_id = {int(event.id): event for event in events}
    game_ids_by_area: dict[str, set[int]] = defaultdict(set)

    for row in rows:
        try:
            event_id = int(_row_value(row, "event_id"))
        except (TypeError, ValueError):
            continue
        event = event_by_id.get(event_id)
        if event is None or _row_value(row, "captured_at") is None:
            continue
        try:
            price = float(_row_value(row, "price"))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue

        identity = section_identity(
            sport_key,
            _event_venue_for_sport(event, sport_key),
            _row_value(row, "section"),
        )
        if identity is not None:
            game_ids_by_area[identity.key].add(event_id)

    threshold = max(int(minimum_games), 1)
    return sum(
        len(game_ids) >= threshold for game_ids in game_ids_by_area.values()
    )


def _section_insights(
    events: list[NFLEvent],
    rows: Iterable[Any],
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[int, set[datetime]]]:
    return _section_insights_for(
        events,
        rows,
        now,
        currency="USD",
        sport_key="nfl",
        detail_url_builder=lambda event, section: url_for(
            "nfl_stadium.nfl_section",
            venue=_clean(nfl_display_venue(event)),
            section=section,
        ),
        secondary_url_builder=None,
        event_label_builder=format_nfl_title,
    )


def _bucketed_game_prices(
    event: Any,
    points: Iterable[tuple[datetime, float]],
    sport_key: str,
) -> list[dict[str, Any]]:
    """Condense one game's observations into one robust value per time window."""
    per_bucket: list[list[tuple[float, float]]] = [
        [] for _ in _TIMELINE_BUCKETS[sport_key]
    ]
    for captured, price in points:
        lead_time = hours_before_event(event.event_date, captured)
        bucket_index = _timeline_bucket_index(sport_key, lead_time)
        if bucket_index is not None:
            per_bucket[bucket_index].append((lead_time, price))

    result: list[dict[str, Any]] = []
    for slot, observations in enumerate(per_bucket):
        if not observations:
            continue
        result.append(
            {
                "slot": slot,
                "lead_time": float(median(item[0] for item in observations)),
                "price": float(median(item[1] for item in observations)),
                "observation_count": len(observations),
            }
        )
    return result


def _aggregate_bucket_points(
    bucket_game_values: dict[int, list[float]],
    *,
    sport_key: str,
    currency: str,
    total_game_count: int,
) -> tuple[list[dict[str, Any]], float | None, int, int]:
    """Build chart points and a time-balanced headline from the same values."""
    all_points: list[dict[str, Any]] = []
    for slot, (lower, upper, short_label, label) in enumerate(
        _TIMELINE_BUCKETS[sport_key]
    ):
        game_values = bucket_game_values.get(slot, [])
        if not game_values:
            continue
        average_price = mean(game_values)
        all_points.append(
            {
                "slot": slot,
                "short_label": short_label,
                "label": label,
                "lower_hours": lower,
                "upper_hours": upper,
                "lead_time": (lower + upper) / 2,
                "average_price": round(average_price, 2),
                "average_price_label": _currency_money(average_price, currency),
                "game_count": len(game_values),
                "is_low_sample": len(game_values) < LOW_SAMPLE_GAMES,
            }
        )

    if not all_points:
        return [], None, 0, 0

    minimum_games = min(LOW_SAMPLE_GAMES, max(total_game_count, 1))
    supported = [
        point for point in all_points if point["game_count"] >= minimum_games
    ]
    # Do not reduce an otherwise useful chart to one isolated point. When at
    # least two well-supported windows exist, sparse windows are omitted.
    displayed = supported if len(supported) >= 2 else all_points
    balanced_average = mean(point["average_price"] for point in displayed)
    return displayed, balanced_average, len(all_points), minimum_games


def _maximum_bucket_drawdown(
    points: Iterable[dict[str, Any]],
    *,
    sport_key: str,
    value_key: str = "price",
) -> dict[str, Any] | None:
    """Return the largest earlier-high to later-low decline across time buckets."""
    ordered = sorted(points, key=lambda point: int(point["slot"]))
    best: dict[str, Any] | None = None
    for earlier_index, earlier in enumerate(ordered):
        earlier_price = float(earlier[value_key])
        if earlier_price <= 0:
            continue
        for later in ordered[earlier_index + 1 :]:
            span_hours = float(earlier["lead_time"]) - float(later["lead_time"])
            # A distinct later time bucket is enough. The within-bucket median
            # already suppresses transient snapshots, and allowing adjacent
            # windows preserves genuine late drops.
            if span_hours <= 0:
                continue
            later_price = float(later[value_key])
            dollar_drop = max(0.0, earlier_price - later_price)
            percent_drop = dollar_drop / earlier_price * 100
            candidate = {
                "percent": percent_drop,
                "dollar": dollar_drop,
                "peak_slot": int(earlier["slot"]),
                "low_slot": int(later["slot"]),
                "span_hours": span_hours,
            }
            if best is None or (
                candidate["percent"], candidate["dollar"]
            ) > (best["percent"], best["dollar"]):
                best = candidate
    return best


def _first_to_last_bucket_change(
    points: Iterable[dict[str, Any]],
    *,
    value_key: str = "price",
) -> dict[str, Any] | None:
    """Compare the first and final usable time-window medians for one game."""
    ordered = sorted(points, key=lambda point: int(point["slot"]))
    if len(ordered) < 2:
        return None

    first = ordered[0]
    last = ordered[-1]
    first_price = float(first[value_key])
    last_price = float(last[value_key])
    if first_price <= 0:
        return None

    dollar_drop = first_price - last_price
    return {
        "percent": dollar_drop / first_price * 100,
        "dollar": dollar_drop,
        "first_slot": int(first["slot"]),
        "last_slot": int(last["slot"]),
        "span_hours": float(first["lead_time"]) - float(last["lead_time"]),
    }


def _typical_drawdown_window_labels(
    rows: Iterable[dict[str, Any]],
    sport_key: str,
) -> tuple[str, str]:
    """Return the most common peak-to-low bucket pair among positive drops."""
    pairs = [
        (int(row["peak_slot"]), int(row["low_slot"]))
        for row in rows
        if row.get("percent", 0) > 0
    ]
    if not pairs:
        return "", ""

    peak_slot, low_slot = Counter(pairs).most_common(1)[0][0]
    return (
        _TIMELINE_BUCKETS[sport_key][peak_slot][3],
        _TIMELINE_BUCKETS[sport_key][low_slot][3],
    )



def _section_label_quality(value: str) -> tuple[int, int, str]:
    label = _clean(value)
    letters = "".join(character for character in label if character.isalpha())
    mixed_case = bool(letters) and not (letters.islower() or letters.isupper())
    return int(mixed_case), len(label), label.casefold()


def _preferred_section_label(
    candidates: Iterable[tuple[datetime, str]],
) -> str:
    rows = [
        (event_date, _clean(label))
        for event_date, label in candidates
        if _clean(label)
    ]
    if not rows:
        return "Unknown section"
    latest_date = max(event_date for event_date, _label in rows)
    latest_labels = [label for event_date, label in rows if event_date == latest_date]
    return max(latest_labels, key=_section_label_quality)


def _prepared_summary_rows(
    events: list[Any],
    rows: Iterable[Any],
    sport_key: str,
) -> dict[tuple[str, int], list[dict[str, Any]]]:
    event_by_id = {int(event.id): event for event in events}
    prepared: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        try:
            event_id = int(_row_value(row, "event_id"))
        except (TypeError, ValueError):
            continue
        event = event_by_id.get(event_id)
        if event is None:
            continue
        section_name = _clean(_row_value(row, "section"))
        section_key = _clean(_row_value(row, "section_key"))
        if not section_key:
            identity = section_identity(
                sport_key,
                _event_venue_for_sport(event, sport_key),
                section_name,
            )
            section_key = identity.key if identity is not None else ""
        if not section_key or not section_name or is_parking_section(section_name):
            continue
        try:
            slot = int(_row_value(row, "slot"))
            price = float(_row_value(row, "price"))
            observation_count = int(_row_value(row, "observation_count") or 1)
        except (TypeError, ValueError):
            continue
        if not 0 <= slot < len(_TIMELINE_BUCKETS[sport_key]) or price <= 0:
            continue
        lower, upper, _short, _label = _TIMELINE_BUCKETS[sport_key][slot]
        prepared[(section_key, event_id)].append(
            {
                "slot": slot,
                "lead_time": (lower + upper) / 2,
                "price": price,
                "observation_count": observation_count,
                "section_name": section_name,
                "first_captured_at": _row_value(row, "first_captured_at"),
                "last_captured_at": _row_value(row, "last_captured_at"),
            }
        )
    return prepared


def _section_insights_for(
    events: list[Any],
    rows: Iterable[Any],
    now: datetime,
    *,
    currency: str,
    sport_key: str,
    detail_url_builder: Callable[[Any, str], str | None],
    secondary_url_builder: Callable[[Any, str], str | None] | None,
    event_label_builder: Callable[[Any], str],
) -> tuple[list[dict[str, Any]], dict[int, set[datetime]]]:
    """Compatibility path for focused raw-row calculations in tests/tools."""

    event_by_id = {int(event.id): event for event in events}
    per_capture: dict[tuple[str, int, datetime], tuple[float, str]] = {}
    captures_by_event: dict[int, set[datetime]] = defaultdict(set)
    for row in rows:
        values = row._mapping if hasattr(row, "_mapping") else row
        try:
            event_id = int(values["event_id"])
            price = float(values["price"])
            section = _clean(values["section"])
            captured = values["captured_at"]
        except (KeyError, TypeError, ValueError):
            continue
        event = event_by_id.get(event_id)
        if event is None or not section or price <= 0 or captured is None:
            continue
        identity = section_identity(
            sport_key,
            _event_venue_for_sport(event, sport_key),
            section,
        )
        if identity is None:
            continue
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        key = (identity.key, event_id, captured)
        current = per_capture.get(key)
        if current is None or price < current[0]:
            per_capture[key] = (price, identity.raw_label)
        captures_by_event[event_id].add(captured)

    histories: dict[tuple[str, int], list[tuple[datetime, float, str]]] = defaultdict(list)
    for (section_key, event_id, captured), (price, label) in per_capture.items():
        histories[(section_key, event_id)].append((captured, price, label))

    prepared: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for key, points in histories.items():
        event = event_by_id.get(key[1])
        if event is None:
            continue
        ordered = sorted(points, key=lambda item: item[0])
        buckets = _bucketed_game_prices(
            event,
            [(captured, price) for captured, price, _label in ordered],
            sport_key,
        )
        if buckets:
            label = ordered[-1][2]
            prepared[key] = [{**point, "section_name": label} for point in buckets]

    insights, _analyzed = _finalize_section_insights(
        events,
        prepared,
        now,
        currency=currency,
        sport_key=sport_key,
        detail_url_builder=detail_url_builder,
        secondary_url_builder=secondary_url_builder,
        event_label_builder=event_label_builder,
    )
    return insights, captures_by_event


def _aggregated_section_insights_for(
    session: Any,
    events: list[Any],
    now: datetime,
    *,
    iteration_model: Any,
    ticket_model: Any,
    currency: str,
    sport_key: str,
    detail_url_builder: Callable[[Any, str], str | None],
    secondary_url_builder: Callable[[Any, str], str | None] | None,
    event_label_builder: Callable[[Any], str],
) -> tuple[list[dict[str, Any]], dict[int, int], int]:
    bucket_rows = _bucket_summary_rows_for(
        session,
        events,
        iteration_model,
        ticket_model,
        sport_key,
    )
    prepared = _prepared_summary_rows(events, bucket_rows, sport_key)
    insights, analyzed_area_count = _finalize_section_insights(
        events,
        prepared,
        now,
        currency=currency,
        sport_key=sport_key,
        detail_url_builder=detail_url_builder,
        secondary_url_builder=secondary_url_builder,
        event_label_builder=event_label_builder,
    )
    capture_counts = _capture_counts_for(
        session,
        [int(event.id) for event in events],
        iteration_model,
    )
    return insights, capture_counts, analyzed_area_count


def _finalize_section_insights(
    events: list[Any],
    prepared: dict[tuple[str, int], list[dict[str, Any]]],
    now: datetime,
    *,
    currency: str,
    sport_key: str,
    detail_url_builder: Callable[[Any, str], str | None],
    secondary_url_builder: Callable[[Any, str], str | None] | None,
    event_label_builder: Callable[[Any], str],
) -> tuple[list[dict[str, Any]], int]:
    event_by_id = {int(event.id): event for event in events}
    completed_ids = {int(event.id) for event in events if _event_completed(event, now)}
    bucket_values_by_section: dict[str, dict[int, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    game_ids_by_section: dict[str, set[int]] = defaultdict(set)
    per_game_drops: dict[str, list[dict[str, Any]]] = defaultdict(list)
    per_game_first_to_last: dict[str, list[dict[str, Any]]] = defaultdict(list)
    observation_count: dict[str, int] = defaultdict(int)
    latest_event_by_section: dict[str, Any] = {}
    display_candidates: dict[str, list[tuple[datetime, str]]] = defaultdict(list)

    for (section_key, event_id), bucket_points in prepared.items():
        event = event_by_id.get(int(event_id))
        if event is None or not bucket_points:
            continue
        ordered = sorted(bucket_points, key=lambda point: int(point["slot"]))
        game_ids_by_section[section_key].add(int(event_id))
        observation_count[section_key] += sum(
            int(point.get("observation_count") or 1) for point in ordered
        )
        for point in ordered:
            bucket_values_by_section[section_key][int(point["slot"])].append(
                float(point["price"])
            )
            section_name = _clean(point.get("section_name"))
            if section_name:
                display_candidates[section_key].append(
                    (event_datetime_eastern(event.event_date), section_name)
                )

        latest_event = latest_event_by_section.get(section_key)
        if latest_event is None or event.event_date > latest_event.event_date:
            latest_event_by_section[section_key] = event

        if int(event_id) in completed_ids:
            drawdown = _maximum_bucket_drawdown(ordered, sport_key=sport_key)
            if drawdown is not None:
                per_game_drops[section_key].append(drawdown)
            first_to_last = _first_to_last_bucket_change(ordered)
            if first_to_last is not None:
                per_game_first_to_last[section_key].append(first_to_last)

    insights = []
    for section_key, bucket_values in bucket_values_by_section.items():
        game_count = len(game_ids_by_section[section_key])
        timeline_points, balanced_price, available_bucket_count, minimum_games = (
            _aggregate_bucket_points(
                bucket_values,
                sport_key=sport_key,
                currency=currency,
                total_game_count=game_count,
            )
        )
        if balanced_price is None:
            continue

        section = _preferred_section_label(display_candidates[section_key])
        drop_rows = per_game_drops.get(section_key, [])
        typical_percent_drop = (
            float(median(row["percent"] for row in drop_rows)) if drop_rows else None
        )
        typical_dollar_drop = (
            float(median(row["dollar"] for row in drop_rows)) if drop_rows else None
        )
        material_drop_count = sum(
            row["percent"] >= MATERIAL_DROP_PERCENT for row in drop_rows
        )
        material_drop_frequency = (
            round(material_drop_count / len(drop_rows) * 100) if drop_rows else 0
        )
        drop_peak_label, drop_low_label = _typical_drawdown_window_labels(
            drop_rows, sport_key
        )

        first_to_last_rows = per_game_first_to_last.get(section_key, [])
        average_first_to_last_percent = (
            float(mean(row["percent"] for row in first_to_last_rows))
            if first_to_last_rows
            else None
        )
        average_first_to_last_dollar = (
            float(mean(row["dollar"] for row in first_to_last_rows))
            if first_to_last_rows
            else None
        )
        first_to_last_direction = "flat"
        if average_first_to_last_percent is not None and average_first_to_last_percent > 0.05:
            first_to_last_direction = "down"
        elif average_first_to_last_percent is not None and average_first_to_last_percent < -0.05:
            first_to_last_direction = "up"

        direction = (
            "down"
            if typical_percent_drop is not None and typical_percent_drop > 0.05
            else "flat"
        )
        direction_label = (
            "Typical maximum decline" if direction == "down" else "No typical decline"
        )
        latest_event = latest_event_by_section.get(section_key)
        detail_url = detail_url_builder(latest_event, section) if latest_event is not None else None
        secondary_url = (
            secondary_url_builder(latest_event, section)
            if latest_event is not None and secondary_url_builder is not None
            else None
        )
        percent_label = _percent_change(typical_percent_drop)
        dollar_label = _currency_price_change(typical_dollar_drop, currency)
        insights.append(
            {
                "section_key": section_key,
                "name": section,
                "average_price": round(balanced_price, 2),
                "average_price_label": _currency_money(balanced_price, currency),
                "typical_price": round(balanced_price, 2),
                "typical_price_label": _currency_money(balanced_price, currency),
                "price_bucket_count": len(timeline_points),
                "available_price_bucket_count": available_bucket_count,
                "price_bucket_game_threshold": minimum_games,
                "game_count": game_count,
                "observation_count": observation_count[section_key],
                "drop_game_count": len(drop_rows),
                "typical_max_drop_percent": (
                    round(typical_percent_drop, 2) if typical_percent_drop is not None else None
                ),
                "typical_max_drop_percent_label": percent_label,
                "typical_max_drop_dollar": (
                    round(typical_dollar_drop, 2) if typical_dollar_drop is not None else None
                ),
                "typical_max_drop_dollar_label": dollar_label,
                "average_percent_drop": (
                    round(typical_percent_drop, 2) if typical_percent_drop is not None else None
                ),
                "average_percent_drop_label": percent_label,
                "average_dollar_drop": (
                    round(typical_dollar_drop, 2) if typical_dollar_drop is not None else None
                ),
                "average_dollar_drop_label": dollar_label,
                "material_drop_threshold": MATERIAL_DROP_PERCENT,
                "material_drop_game_count": material_drop_count,
                "material_drop_frequency": material_drop_frequency,
                "drop_frequency": material_drop_frequency,
                "drop_peak_label": drop_peak_label,
                "drop_low_label": drop_low_label,
                "first_to_last_game_count": len(first_to_last_rows),
                "average_first_to_last_percent": (
                    round(average_first_to_last_percent, 2)
                    if average_first_to_last_percent is not None
                    else None
                ),
                "average_first_to_last_percent_label": _percent_change(
                    average_first_to_last_percent
                ),
                "average_first_to_last_dollar": (
                    round(average_first_to_last_dollar, 2)
                    if average_first_to_last_dollar is not None
                    else None
                ),
                "average_first_to_last_dollar_label": _currency_price_change(
                    average_first_to_last_dollar, currency
                ),
                "first_to_last_direction": first_to_last_direction,
                "direction": direction,
                "direction_label": direction_label,
                "is_low_price_sample": game_count < LOW_SAMPLE_GAMES or len(timeline_points) < 2,
                "is_low_drop_sample": 0 < len(drop_rows) < LOW_SAMPLE_GAMES,
                "detail_url": detail_url,
                "secondary_url": secondary_url,
                "map_url": detail_url,
                "latest_game_label": (
                    event_label_builder(latest_event) if latest_event is not None else ""
                ),
            }
        )

    insights.sort(key=lambda row: (row["average_price"], row["name"].casefold()))
    analyzed_area_count = sum(
        len(game_ids) >= LOW_SAMPLE_GAMES for game_ids in game_ids_by_section.values()
    )
    return insights, analyzed_area_count


def _rank_sections(
    sections: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cheapest = sorted(
        sections,
        key=lambda row: (row["typical_price"], row["name"].casefold()),
    )[:5]
    biggest_drops = sorted(
        [
            row
            for row in sections
            if row["drop_game_count"] >= LOW_SAMPLE_GAMES
            and row["typical_max_drop_percent"] is not None
            and row["typical_max_drop_percent"] > 0
        ],
        key=lambda row: (
            -row["typical_max_drop_percent"],
            row["name"].casefold(),
        ),
    )[:5]
    return cheapest, biggest_drops


def _team_label(events: Iterable[Any], team_getter: Callable[[Any], str | None]) -> str:
    teams = sorted({_clean(team_getter(event)) for event in events if _clean(team_getter(event))})
    return " / ".join(teams[:2])



def _generic_venue_index(
    events: Iterable[Any],
    now: datetime,
    *,
    venue_getter: Callable[[Any], str],
    team_getter: Callable[[Any], str | None],
    endpoint: str,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for event in events:
        venue = _clean(venue_getter(event))
        if venue:
            grouped[venue].append(event)

    result = []
    for venue, venue_events in grouped.items():
        team_label = _team_label(venue_events, team_getter)
        completed_count = sum(_event_completed(event, now) for event in venue_events)
        result.append(
            {
                "venue": venue,
                "game_count": len(venue_events),
                "completed_count": completed_count,
                "upcoming_count": len(venue_events) - completed_count,
                "team_label": team_label,
                "url": url_for(endpoint, venue=venue),
            }
        )
    return sorted(
        result,
        key=lambda row: (
            (row["team_label"] or row["venue"]).casefold(),
            row["venue"].casefold(),
        ),
    )



def _page_config(
    *,
    sport_key: str,
    sport_label: str,
    theme_class: str,
    theme_color: str,
    venue_noun: str,
    venue_plural: str,
    home_url: str,
    directory_url: str,
    window_label: str,
    section_action_label: str,
    secondary_action_label: str,
    game_action_label: str,
    currency_label: str,
) -> dict[str, Any]:
    return {
        "sport_key": sport_key,
        "sport_label": sport_label,
        "theme_class": theme_class,
        "theme_color": theme_color,
        "venue_noun": venue_noun,
        "venue_plural": venue_plural,
        "home_url": home_url,
        "directory_url": directory_url,
        "window_label": window_label,
        "section_action_label": section_action_label,
        "secondary_action_label": secondary_action_label,
        "game_action_label": game_action_label,
        "currency_label": currency_label,
        "currency_warning": "",
        "selected_team_label": "",
        "analyzed_area_count": 0,
    }


def _nfl_page_config() -> dict[str, Any]:
    return _page_config(
        sport_key="nfl",
        sport_label="NFL",
        theme_class="",
        theme_color="#013369",
        venue_noun="stadium",
        venue_plural="stadiums",
        home_url=url_for("nfl.nfl_home"),
        directory_url=url_for("nfl_stadium.nfl_stadium"),
        window_label="30D",
        section_action_label="Section details",
        secondary_action_label="",
        game_action_label="Open stadium map",
        currency_label="USD",
    )



def build_nfl_stadium_context(selected_venue: str = "") -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    model = CreateNFLModel()
    config = _nfl_page_config()
    try:
        with model.getSession()() as session:
            directory_events = (
                session.query(NFLEvent)
                .options(
                    load_only(
                        NFLEvent.id,
                        NFLEvent.title,
                        NFLEvent.event_date,
                        NFLEvent.source_url,
                        NFLEvent.venue,
                        NFLEvent.home_team,
                        NFLEvent.canonical_venue,
                        NFLEvent.provider_venue,
                        NFLEvent.country,
                    )
                )
                .order_by(NFLEvent.event_date)
                .all()
            )
            directory_events = [
                event
                for event in directory_events
                if _is_us_event(event) and _clean(nfl_display_venue(event))
            ]
            stadiums = _stadium_index(directory_events, now)

            selected = _clean(selected_venue)
            if not selected:
                return {
                    **config,
                    "stadiums": stadiums,
                    "stadium_count": len(stadiums),
                    "selected_venue": "",
                    "error": None,
                }

            selected_ids = [
                event.id
                for event in directory_events
                if _clean(nfl_display_venue(event)) == selected
            ]
            if not selected_ids:
                return {
                    **config,
                    "stadiums": stadiums,
                    "stadium_count": len(stadiums),
                    "selected_venue": "",
                    "error": f"{selected} is not in the tracked NFL stadium history.",
                }

            selected_events = (
                session.query(NFLEvent)
                .options(defer(NFLEvent.map_geometry))
                .filter(NFLEvent.id.in_(selected_ids))
                .order_by(NFLEvent.event_date)
                .all()
            )
            sections, capture_counts, analyzed_area_count = (
                _aggregated_section_insights_for(
                    session,
                    selected_events,
                    now,
                    iteration_model=NFLIteration,
                    ticket_model=NFLTicket,
                    currency="USD",
                    sport_key="nfl",
                    detail_url_builder=lambda event, section: url_for(
                        "nfl_stadium.nfl_section",
                        venue=_clean(nfl_display_venue(event)),
                        section=section,
                    ),
                    secondary_url_builder=None,
                    event_label_builder=format_nfl_title,
                )
            )
    finally:
        dispose_ticket_engine(model.engine)

    cheapest, biggest_drops = _rank_sections(sections)
    upcoming: list[tuple[datetime, dict[str, Any]]] = []
    completed: list[tuple[datetime, dict[str, Any]]] = []
    for event in selected_events:
        event_date = event_datetime_eastern(event.event_date)
        is_completed = _event_completed(event, now)
        team = _clean(nfl_event_home_team(event)) or selected
        map_url = url_for("nfl.nfl_map", team=team, game=str(event.id))
        public_sections = _public_sections(event.sections or [])
        game = {
            "id": event.id,
            "label": format_nfl_title(event),
            "status": "Completed" if is_completed else "Upcoming",
            "status_key": "completed" if is_completed else "upcoming",
            "snapshot_count": capture_counts.get(event.id, 0),
            "section_count": len(public_sections),
            "sections": public_sections,
            "map_url": map_url,
            "direct_url": map_url,
            "base_url": "",
            "source_url": event.source_url or "",
        }
        (completed if is_completed else upcoming).append((event_date, game))

    upcoming.sort(key=lambda item: item[0])
    completed.sort(key=lambda item: item[0], reverse=True)
    games = [game for _, game in upcoming + completed]
    return {
        **config,
        "stadiums": stadiums,
        "stadium_count": len(stadiums),
        "selected_venue": selected,
        "selected_team_label": _team_label(selected_events, nfl_event_home_team) or "NFL home team",
        "error": None,
        "game_count": len(selected_events),
        "completed_game_count": len(completed),
        "upcoming_game_count": len(upcoming),
        "section_count": len(sections),
        "analyzed_area_count": analyzed_area_count,
        "observation_count": sum(row["observation_count"] for row in sections),
        "drop_section_count": sum(1 for row in sections if row["drop_game_count"]),
        "cheapest_sections": cheapest,
        "biggest_drops": biggest_drops,
        "all_sections": sections,
        "games": games,
        "method_price": (
            "Observations are grouped into fixed time-to-kickoff windows. Each game "
            "contributes one median per window, and the headline is the mean of the "
            "same supported window averages shown on the chart."
        ),
        "method_drop": (
            "For each completed game, bucket medians are used to find the largest "
            "earlier-high to later-low decline. The section metric is the median of "
            "those per-game maximum drawdowns."
        ),
    }



def _mapped_mlb_team_for_venue(venue: Any) -> str | None:
    normalized = _normalize(venue)
    if not normalized:
        return None

    mapped = _MLB_VENUE_TEAMS.get(normalized)
    if mapped:
        return mapped

    # Provider venue metadata sometimes appends a city/state suffix. Match a
    # known stadium at either edge without accepting arbitrary partial words.
    for venue_name, team in _MLB_VENUE_TEAMS.items():
        if normalized.startswith(f"{venue_name} ") or normalized.endswith(
            f" {venue_name}"
        ):
            return team
    return None


def mlb_team_for_venue(venue: Any) -> str:
    return _mapped_mlb_team_for_venue(venue) or "MLB home team"


def _mlb_team_from_fragment(fragment: str) -> str | None:
    normalized = _normalize(fragment)
    matches: list[tuple[int, str]] = []
    for team, aliases in _MLB_TEAM_ALIASES.items():
        for alias in aliases:
            normalized_alias = _normalize(alias)
            if normalized == normalized_alias or normalized.endswith(f" {normalized_alias}"):
                matches.append((len(normalized_alias), team))
    return max(matches)[1] if matches else None


def mlb_event_home_team(event: Event) -> str:
    # Venue ownership is more reliable than provider title order for MLB.
    mapped = _mapped_mlb_team_for_venue(getattr(event, "Place", ""))
    if mapped:
        return mapped

    title = clean_event_title(_clean(getattr(event, "title", "")))
    parts = re.split(r"\s+(?:at|vs\.?|versus)\s+", title, maxsplit=1, flags=re.I)
    if len(parts) == 2:
        matched = _mlb_team_from_fragment(parts[1])
        if matched:
            return matched
    return "MLB home team"


def format_mlb_title(event: Event) -> str:
    event_date = event_datetime_eastern(event.event_date)
    hour = event_date.hour % 12 or 12
    return (
        f"{clean_event_title(event.title)} — {event_date:%b} {event_date.day}, "
        f"{event_date.year} · {hour}:{event_date.minute:02d} {event_date:%p} {event_date:%Z}"
    )


def _mlb_page_config() -> dict[str, Any]:
    return _page_config(
        sport_key="mlb",
        sport_label="MLB",
        theme_class="mlb-theme",
        theme_color="#102b24",
        venue_noun="stadium",
        venue_plural="stadiums",
        home_url=url_for("home"),
        directory_url=url_for("nfl_stadium.mlb_stadium"),
        window_label="72H",
        section_action_label="Multi-game trend",
        secondary_action_label="Buying window",
        game_action_label="Open game chart",
        currency_label="USD",
    )


def _is_public_mlb_event(event: Event) -> bool:
    return bool(
        event.Place
        and MLB_URL_MARKER in str(event.URL or "").casefold()
        and event_has_complete_public_data(event)
    )



def build_mlb_stadium_context(selected_venue: str = "") -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    model = CreateModel()
    config = _mlb_page_config()
    try:
        with model.getSession()() as session:
            directory_events = (
                session.query(Event)
                .options(
                    load_only(
                        Event.id,
                        Event.title,
                        Event.event_date,
                        Event.URL,
                        Event.Place,
                    )
                )
                .order_by(Event.event_date)
                .all()
            )
            directory_events = [
                event for event in directory_events if _is_public_mlb_event(event)
            ]
            stadiums = _generic_venue_index(
                directory_events,
                now,
                venue_getter=lambda event: _clean(event.Place),
                team_getter=mlb_event_home_team,
                endpoint="nfl_stadium.mlb_stadium",
            )

            selected = _clean(selected_venue)
            if not selected:
                return {
                    **config,
                    "stadiums": stadiums,
                    "stadium_count": len(stadiums),
                    "selected_venue": "",
                    "error": None,
                }

            selected_ids = [
                event.id for event in directory_events if _clean(event.Place) == selected
            ]
            if not selected_ids:
                return {
                    **config,
                    "stadiums": stadiums,
                    "stadium_count": len(stadiums),
                    "selected_venue": "",
                    "error": f"{selected} is not in the tracked MLB stadium history.",
                }

            selected_events = (
                session.query(Event)
                .filter(Event.id.in_(selected_ids))
                .order_by(Event.event_date)
                .all()
            )
            sections, capture_counts, analyzed_area_count = (
                _aggregated_section_insights_for(
                    session,
                    selected_events,
                    now,
                    iteration_model=Iteration,
                    ticket_model=Ticket,
                    currency="USD",
                    sport_key="mlb",
                    detail_url_builder=lambda _event, section: url_for(
                        "nfl_stadium.mlb_section",
                        venue=selected,
                        section=section,
                    ),
                    secondary_url_builder=lambda _event, section: url_for(
                        "predict",
                        event=selected,
                        section=section,
                    ),
                    event_label_builder=format_mlb_title,
                )
            )
    finally:
        dispose_ticket_engine(model.engine)

    cheapest, biggest_drops = _rank_sections(sections)
    upcoming: list[tuple[datetime, dict[str, Any]]] = []
    completed: list[tuple[datetime, dict[str, Any]]] = []
    for event in selected_events:
        event_date = event_datetime_eastern(event.event_date)
        is_completed = _event_completed(event, now)
        public_sections = _public_sections(event.event_sections or [])
        game = {
            "id": event.id,
            "label": format_mlb_title(event),
            "status": "Completed" if is_completed else "Upcoming",
            "status_key": "completed" if is_completed else "upcoming",
            "snapshot_count": capture_counts.get(event.id, 0),
            "section_count": len(public_sections),
            "sections": public_sections,
            "direct_url": "",
            "base_url": url_for(
                "graph",
                event=selected,
                game=str(event.id),
                mode="single",
                display="money",
            ),
            "source_url": event.URL or "",
        }
        (completed if is_completed else upcoming).append((event_date, game))

    upcoming.sort(key=lambda item: item[0])
    completed.sort(key=lambda item: item[0], reverse=True)
    games = [game for _, game in upcoming + completed]
    return {
        **config,
        "stadiums": stadiums,
        "stadium_count": len(stadiums),
        "selected_venue": selected,
        "selected_team_label": _team_label(selected_events, mlb_event_home_team) or mlb_team_for_venue(selected),
        "error": None,
        "game_count": len(selected_events),
        "completed_game_count": len(completed),
        "upcoming_game_count": len(upcoming),
        "section_count": len(sections),
        "analyzed_area_count": analyzed_area_count,
        "observation_count": sum(row["observation_count"] for row in sections),
        "drop_section_count": sum(1 for row in sections if row["drop_game_count"]),
        "cheapest_sections": cheapest,
        "biggest_drops": biggest_drops,
        "all_sections": sections,
        "games": games,
        "method_price": (
            "Observations are grouped into fixed time-to-first-pitch windows. Each "
            "game contributes one median per window, and the headline averages the "
            "same supported window points shown on the chart."
        ),
        "method_drop": (
            "For each completed game, bucket medians are used to find the largest "
            "earlier-high to later-low decline. The section metric is the median of "
            "those per-game maximum drawdowns."
        ),
    }



def _nhl_page_config(currency: str = "USD") -> dict[str, Any]:
    return _page_config(
        sport_key="nhl",
        sport_label="NHL",
        theme_class="nhl-theme",
        theme_color="#06191b",
        venue_noun="arena",
        venue_plural="arenas",
        home_url=url_for("nhl.nhl_home"),
        directory_url=url_for("nfl_stadium.nhl_arena"),
        window_label="30D",
        section_action_label="Section details",
        secondary_action_label="",
        game_action_label="Open arena map",
        currency_label=currency,
    )



def build_nhl_arena_context(selected_venue: str = "") -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    model = CreateNHLModel()
    try:
        with model.getSession()() as session:
            directory_events = (
                session.query(NHLEvent)
                .options(
                    load_only(
                        NHLEvent.id,
                        NHLEvent.title,
                        NHLEvent.event_date,
                        NHLEvent.source_url,
                        NHLEvent.venue,
                        NHLEvent.home_team,
                        NHLEvent.canonical_venue,
                        NHLEvent.provider_venue,
                        NHLEvent.country,
                        NHLEvent.currency,
                    )
                )
                .order_by(NHLEvent.event_date)
                .all()
            )
            directory_events = [
                event
                for event in directory_events
                if _is_supported_nhl_event(event) and _clean(nhl_display_venue(event))
            ]
            stadiums = _generic_venue_index(
                directory_events,
                now,
                venue_getter=nhl_display_venue,
                team_getter=nhl_event_home_team,
                endpoint="nfl_stadium.nhl_arena",
            )

            selected = _clean(selected_venue)
            if not selected:
                return {
                    **_nhl_page_config(),
                    "stadiums": stadiums,
                    "stadium_count": len(stadiums),
                    "selected_venue": "",
                    "error": None,
                }

            selected_ids = [
                event.id
                for event in directory_events
                if _clean(nhl_display_venue(event)) == selected
            ]
            if not selected_ids:
                return {
                    **_nhl_page_config(),
                    "stadiums": stadiums,
                    "stadium_count": len(stadiums),
                    "selected_venue": "",
                    "error": f"{selected} is not in the tracked NHL arena history.",
                }

            selected_events = (
                session.query(NHLEvent)
                .options(defer(NHLEvent.map_geometry))
                .filter(NHLEvent.id.in_(selected_ids))
                .order_by(NHLEvent.event_date)
                .all()
            )
            currency_counts = Counter(
                (_clean(event.currency).upper() or "USD") for event in selected_events
            )
            analysis_currency = currency_counts.most_common(1)[0][0]
            analysis_events = [
                event
                for event in selected_events
                if (_clean(event.currency).upper() or "USD") == analysis_currency
            ]
            config = _nhl_page_config(analysis_currency)
            omitted = len(selected_events) - len(analysis_events)
            if omitted:
                config["currency_warning"] = (
                    f"{omitted} game{'s were' if omitted != 1 else ' was'} omitted "
                    f"from section averages because its stored currency differs from {analysis_currency}."
                )

            sections, capture_counts, analyzed_area_count = (
                _aggregated_section_insights_for(
                    session,
                    analysis_events,
                    now,
                    iteration_model=NHLIteration,
                    ticket_model=NHLTicket,
                    currency=analysis_currency,
                    sport_key="nhl",
                    detail_url_builder=lambda _event, section: url_for(
                        "nfl_stadium.nhl_section",
                        venue=selected,
                        section=section,
                    ),
                    secondary_url_builder=None,
                    event_label_builder=format_nhl_title,
                )
            )
    finally:
        dispose_ticket_engine(model.engine)

    cheapest, biggest_drops = _rank_sections(sections)
    upcoming: list[tuple[datetime, dict[str, Any]]] = []
    completed: list[tuple[datetime, dict[str, Any]]] = []
    for event in selected_events:
        event_date = event_datetime_eastern(event.event_date)
        is_completed = _event_completed(event, now)
        team = _clean(nhl_event_home_team(event)) or selected
        map_url = url_for("nhl.nhl_map", team=team, game=str(event.id))
        public_sections = _public_sections(event.sections or [])
        game = {
            "id": event.id,
            "label": format_nhl_title(event),
            "status": "Completed" if is_completed else "Upcoming",
            "status_key": "completed" if is_completed else "upcoming",
            "snapshot_count": capture_counts.get(event.id, 0),
            "section_count": len(public_sections),
            "sections": public_sections,
            "direct_url": map_url,
            "base_url": "",
            "source_url": event.source_url or "",
            "event_currency": _clean(event.currency).upper() or "USD",
        }
        (completed if is_completed else upcoming).append((event_date, game))

    upcoming.sort(key=lambda item: item[0])
    completed.sort(key=lambda item: item[0], reverse=True)
    games = [game for _, game in upcoming + completed]
    return {
        **config,
        "stadiums": stadiums,
        "stadium_count": len(stadiums),
        "selected_venue": selected,
        "selected_team_label": _team_label(selected_events, nhl_event_home_team) or "NHL home team",
        "error": None,
        "game_count": len(selected_events),
        "completed_game_count": len(completed),
        "upcoming_game_count": len(upcoming),
        "section_count": len(sections),
        "analyzed_area_count": analyzed_area_count,
        "observation_count": sum(row["observation_count"] for row in sections),
        "drop_section_count": sum(1 for row in sections if row["drop_game_count"]),
        "cheapest_sections": cheapest,
        "biggest_drops": biggest_drops,
        "all_sections": sections,
        "games": games,
        "method_price": (
            "Observations are grouped into fixed time-to-puck-drop windows. Each "
            "game contributes one median per window, and the headline averages the "
            "same supported window points shown on the chart."
        ),
        "method_drop": (
            "For each completed game, bucket medians are used to find the largest "
            "earlier-high to later-low decline. The section metric is the median of "
            "those per-game maximum drawdowns."
        ),
    }



def _resolve_section(
    sections: Iterable[dict[str, Any]],
    requested: str,
    *,
    sport_key: str = "",
    venue: str = "",
) -> dict[str, Any] | None:
    requested_clean = _clean(requested)
    if not requested_clean or is_parking_section(requested_clean):
        return None

    rows = list(sections)
    for row in rows:
        if _clean(row.get("name")) == requested_clean:
            return row

    if sport_key and venue:
        identity = section_identity(sport_key, venue, requested_clean)
        if identity is not None:
            matches = [
                row
                for row in rows
                if _clean(row.get("section_key")) == identity.key
            ]
            if len(matches) == 1:
                return matches[0]

    normalized = _normalize(requested_clean)
    matches = [row for row in rows if _normalize(row.get("name")) == normalized]
    return matches[0] if len(matches) == 1 else None


def _timeline_bucket_index(sport_key: str, hours: float) -> int | None:
    for index, (lower, upper, _short, _label) in enumerate(
        _TIMELINE_BUCKETS[sport_key]
    ):
        if lower == 0:
            if 0 < hours <= upper:
                return index
        elif lower < hours <= upper:
            return index
    return None


def _lead_time_label(hours: float) -> str:
    if hours >= 24:
        days = hours / 24
        rendered = f"{days:.0f}" if abs(days - round(days)) < 0.05 else f"{days:.1f}"
        return f"{rendered}d"
    rendered = f"{hours:.0f}" if abs(hours - round(hours)) < 0.05 else f"{hours:.1f}"
    return f"{rendered}h"


def _section_timeline_context(
    events: list[Any],
    rows: Iterable[Any],
    section_name: str,
    now: datetime,
    *,
    sport_key: str,
    currency: str,
    event_label_builder: Callable[[Any], str],
    game_url_builder: Callable[[Any, str], str],
    map_url_builder: Callable[[Any, str], str | None] | None,
    source_url_getter: Callable[[Any], str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build one section page entirely from materialized bucket summaries."""

    event_by_id = {int(event.id): event for event in events}
    requested_keys = {
        identity.key
        for event in events
        if (
            identity := section_identity(
                sport_key,
                _event_venue_for_sport(event, sport_key),
                section_name,
            )
        )
        is not None
    }
    points_by_event: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        try:
            event_id = int(_row_value(row, "event_id"))
            slot = int(_row_value(row, "slot"))
            price = float(_row_value(row, "price"))
        except (TypeError, ValueError):
            continue
        event = event_by_id.get(event_id)
        if event is None or price <= 0 or not 0 <= slot < len(_TIMELINE_BUCKETS[sport_key]):
            continue
        row_key = _clean(_row_value(row, "section_key"))
        if requested_keys and row_key not in requested_keys:
            continue
        lower, upper, _short, _label = _TIMELINE_BUCKETS[sport_key][slot]
        points_by_event[event_id].append(
            {
                "slot": slot,
                "lead_time": (lower + upper) / 2,
                "price": price,
                "observation_count": int(_row_value(row, "observation_count") or 1),
                "section_name": _clean(_row_value(row, "section")) or section_name,
                "first_captured_at": _row_value(row, "first_captured_at"),
                "last_captured_at": _row_value(row, "last_captured_at"),
            }
        )

    bucket_game_values: dict[int, list[float]] = defaultdict(list)
    game_rows: list[tuple[datetime, dict[str, Any]]] = []
    for event_id, bucket_points in points_by_event.items():
        event = event_by_id[event_id]
        ordered = sorted(bucket_points, key=lambda point: int(point["slot"]))
        if not ordered:
            continue
        for point in ordered:
            bucket_game_values[int(point["slot"])].append(float(point["price"]))

        drawdown = _maximum_bucket_drawdown(ordered, sport_key=sport_key)
        balanced_game_price = mean(float(point["price"]) for point in ordered)
        first_times = [
            point["first_captured_at"]
            for point in ordered
            if point.get("first_captured_at") is not None
        ]
        last_times = [
            point["last_captured_at"]
            for point in ordered
            if point.get("last_captured_at") is not None
        ]
        first_time = min(first_times) if first_times else None
        last_time = max(last_times) if last_times else None
        first_lead = (
            hours_before_event(event.event_date, first_time)
            if first_time is not None
            else float(ordered[0]["lead_time"])
        )
        last_lead = (
            hours_before_event(event.event_date, last_time)
            if last_time is not None
            else float(ordered[-1]["lead_time"])
        )
        last_price = float(ordered[-1]["price"])
        game_section_name = ordered[-1].get("section_name") or section_name
        event_date = event_datetime_eastern(event.event_date)
        completed = _event_completed(event, now)
        game_rows.append(
            (
                event_date,
                {
                    "id": event.id,
                    "label": event_label_builder(event),
                    "status": "Completed" if completed else "Upcoming",
                    "status_key": "completed" if completed else "upcoming",
                    "average_price": round(balanced_game_price, 2),
                    "average_price_label": _currency_money(
                        balanced_game_price, currency
                    ),
                    "latest_price_label": _currency_money(last_price, currency),
                    "movement_label": _currency_price_change(
                        drawdown["dollar"] if drawdown is not None else None,
                        currency,
                    ),
                    "movement_percent_label": _percent_change(
                        drawdown["percent"] if drawdown is not None else None
                    ),
                    "max_drop_label": _currency_price_change(
                        drawdown["dollar"] if drawdown is not None else None,
                        currency,
                    ),
                    "max_drop_percent_label": _percent_change(
                        drawdown["percent"] if drawdown is not None else None
                    ),
                    "bucket_count": len(ordered),
                    "observation_count": sum(
                        int(point.get("observation_count") or 1)
                        for point in ordered
                    ),
                    "coverage_label": (
                        f"{_lead_time_label(first_lead)} to "
                        f"{_lead_time_label(last_lead)} before"
                    ),
                    "game_url": game_url_builder(event, game_section_name),
                    "map_url": (
                        map_url_builder(event, game_section_name)
                        if map_url_builder is not None
                        else None
                    ),
                    "source_url": source_url_getter(event),
                    "first_captured_at": first_time,
                    "last_captured_at": last_time,
                },
            )
        )

    timeline_points, balanced_price, available_bucket_count, minimum_games = (
        _aggregate_bucket_points(
            bucket_game_values,
            sport_key=sport_key,
            currency=currency,
            total_game_count=len(points_by_event),
        )
    )
    timeline_drawdown = _maximum_bucket_drawdown(
        timeline_points,
        sport_key=sport_key,
        value_key="average_price",
    )
    timeline_drop = timeline_drawdown["dollar"] if timeline_drawdown else None
    timeline_percent = timeline_drawdown["percent"] if timeline_drawdown else None
    has_decline = timeline_percent is not None and timeline_percent > 0.05
    timeline_direction = "down" if has_decline else "flat"
    timeline_direction_label = (
        "Largest decline on the average curve"
        if has_decline
        else "No clear decline on the average curve"
    )

    from_label = ""
    to_label = ""
    if timeline_drawdown is not None:
        from_label = _TIMELINE_BUCKETS[sport_key][
            timeline_drawdown["peak_slot"]
        ][3]
        to_label = _TIMELINE_BUCKETS[sport_key][
            timeline_drawdown["low_slot"]
        ][3]

    upcoming = sorted(
        [row for row in game_rows if row[1]["status_key"] == "upcoming"],
        key=lambda item: item[0],
    )
    completed = sorted(
        [row for row in game_rows if row[1]["status_key"] == "completed"],
        key=lambda item: item[0],
        reverse=True,
    )

    return (
        {
            "points": timeline_points,
            "slot_count": len(_TIMELINE_BUCKETS[sport_key]),
            "currency": currency,
            "event_moment": _EVENT_MOMENT_LABELS[sport_key],
            "balanced_price": (
                round(balanced_price, 2) if balanced_price is not None else None
            ),
            "balanced_price_label": _currency_money(balanced_price, currency),
            "price_bucket_count": len(timeline_points),
            "available_price_bucket_count": available_bucket_count,
            "minimum_games_per_bucket": minimum_games,
            "movement_label": _currency_price_change(timeline_drop, currency),
            "movement_percent_label": _percent_change(timeline_percent),
            "movement_direction": timeline_direction,
            "movement_direction_label": timeline_direction_label,
            "from_label": from_label,
            "to_label": to_label,
        },
        [row for _date, row in upcoming + completed],
    )


def _event_section_name(
    event: Any,
    section_getter: Callable[[Any], Iterable[str]],
    requested: str,
    *,
    sport_key: str,
    venue: str,
) -> str | None:
    requested_identity = section_identity(sport_key, venue, requested)
    if requested_identity is None:
        return None
    matches = []
    for section in _public_sections(section_getter(event)):
        identity = section_identity(sport_key, venue, section)
        if identity is not None and identity.key == requested_identity.key:
            matches.append(section)
    return matches[0] if matches else None


def _section_map_context(
    events: list[Any],
    section_rows: list[dict[str, Any]],
    section_name: str,
    *,
    sport_key: str,
    venue: str,
    section_getter: Callable[[Any], Iterable[str]],
    geometry_getter: Callable[[Any], Any] | None,
    event_label_builder: Callable[[Any], str],
    exact_map_url_builder: Callable[[Any, str], str | None] | None,
    source_url_getter: Callable[[Any], str],
) -> dict[str, Any]:
    candidates: list[tuple[int, int, int, datetime, Any, str, Any]] = []

    for event in events:
        matched_name = _event_section_name(
            event,
            section_getter,
            section_name,
            sport_key=sport_key,
            venue=venue,
        )
        if not matched_name:
            continue

        visible_sections = _public_sections(section_getter(event))
        sanitized_geometry = None
        provider_usable = False
        selected_has_shape = False
        if geometry_getter is not None:
            sanitized_geometry = sanitize_map_geometry(
                geometry_getter(event),
                visible_sections,
            )
            provider_usable = geometry_is_usable(
                sanitized_geometry,
                visible_sections,
            )
            selected_has_shape = bool(
                sanitized_geometry
                and any(
                    _normalize(row.get("name")) == _normalize(matched_name)
                    and bool(row.get("shapes"))
                    for row in sanitized_geometry.get("sections", [])
                )
            )

        candidates.append(
            (
                int(selected_has_shape),
                int(provider_usable),
                geometry_section_count(sanitized_geometry),
                event_datetime_eastern(event.event_date),
                event,
                matched_name,
                sanitized_geometry,
            )
        )

    representative_event = None
    map_section_name = section_name
    representative_geometry = None
    selected_has_provider_shape = False
    provider_usable = False
    if candidates:
        (
            selected_shape_score,
            provider_score,
            _geometry_count,
            _event_date,
            representative_event,
            map_section_name,
            representative_geometry,
        ) = max(candidates, key=lambda item: item[:4])
        selected_has_provider_shape = bool(selected_shape_score)
        provider_usable = bool(provider_score)

    averages_by_key = {
        _clean(row.get("section_key")): row
        for row in section_rows
        if _clean(row.get("section_key"))
    }
    if representative_event is not None:
        visible_sections = _public_sections(section_getter(representative_event))
    else:
        visible_sections = [row["name"] for row in section_rows]

    map_sections = []
    for name in visible_sections:
        identity = section_identity(sport_key, venue, name)
        aggregate = (
            averages_by_key.get(identity.key) if identity is not None else None
        )
        map_sections.append(
            {
                "name": name,
                "average_price": (
                    aggregate["average_price"] if aggregate is not None else None
                ),
                "average_price_label": (
                    aggregate["average_price_label"] if aggregate is not None else "—"
                ),
                "game_count": aggregate["game_count"] if aggregate is not None else 0,
            }
        )

    has_provider_geometry = bool(
        provider_usable and selected_has_provider_shape and representative_geometry
    )
    geometry = representative_geometry if has_provider_geometry else None

    representative_label = (
        event_label_builder(representative_event)
        if representative_event is not None
        else ""
    )
    exact_map_url = (
        exact_map_url_builder(representative_event, map_section_name)
        if representative_event is not None and exact_map_url_builder is not None
        else None
    )
    provider_url = (
        source_url_getter(representative_event)
        if representative_event is not None
        else ""
    )

    if has_provider_geometry:
        map_note = f"Highlighted on the stored seating map for {representative_label}."
        map_badge = "Provider layout"
    elif sport_key == "mlb":
        map_note = (
            "Approximate bowl layout generated from section labels. "
            "Open the provider event for exact placement."
        )
        map_badge = "Section schematic"
    else:
        map_note = (
            "A complete provider map was not stored for this section, so the "
            "highlight is shown on an approximate bowl layout."
        )
        map_badge = "Section schematic"

    return {
        "map_data": {
            "sport": sport_key,
            "venue": venue,
            "selected_section": map_section_name,
            "sections": map_sections,
            "geometry": geometry,
            "geometry_mode": "provider" if has_provider_geometry else "schematic",
        },
        "map_note": map_note,
        "map_badge": map_badge,
        "has_provider_geometry": has_provider_geometry,
        "representative_game_label": representative_label,
        "representative_map_url": exact_map_url,
        "representative_provider_url": provider_url,
    }


def _section_error_context(
    base_context: dict[str, Any],
    message: str,
    requested_section: str,
) -> dict[str, Any]:
    report_url = base_context.get("directory_url", "")
    endpoint = {
        "nfl": "nfl_stadium.nfl_stadium",
        "mlb": "nfl_stadium.mlb_stadium",
        "nhl": "nfl_stadium.nhl_arena",
    }.get(base_context.get("sport_key"))
    if endpoint and base_context.get("selected_venue"):
        report_url = url_for(endpoint, venue=base_context["selected_venue"])

    return {
        **base_context,
        "error": message,
        "selected_section": _clean(requested_section),
        "section_summary": None,
        "section_games": [],
        "timeline": {"points": [], "slot_count": 0},
        "map_data": {"sections": []},
        "report_url": report_url,
        "buying_window_url": "",
    }


def _build_section_detail_context(
    base_context: dict[str, Any],
    events: list[Any],
    rows: Iterable[Any],
    requested_section: str,
    now: datetime,
    *,
    sport_key: str,
    currency: str,
    report_endpoint: str,
    section_getter: Callable[[Any], Iterable[str]],
    geometry_getter: Callable[[Any], Any] | None,
    event_label_builder: Callable[[Any], str],
    game_url_builder: Callable[[Any, str], str],
    map_url_builder: Callable[[Any, str], str | None] | None,
    source_url_getter: Callable[[Any], str],
    buying_window_url_builder: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    if base_context.get("error") or not base_context.get("selected_venue"):
        return _section_error_context(
            base_context,
            base_context.get("error") or "Choose a team and section.",
            requested_section,
        )

    section_summary = _resolve_section(
        base_context.get("all_sections", []),
        requested_section,
        sport_key=sport_key,
        venue=base_context.get("selected_venue", ""),
    )
    if section_summary is None:
        return _section_error_context(
            base_context,
            "That seating section is not in this team's tracked history.",
            requested_section,
        )

    section_name = section_summary["name"]
    timeline, section_games = _section_timeline_context(
        events,
        rows,
        section_name,
        now,
        sport_key=sport_key,
        currency=currency,
        event_label_builder=event_label_builder,
        game_url_builder=game_url_builder,
        map_url_builder=map_url_builder,
        source_url_getter=source_url_getter,
    )
    section_summary = dict(section_summary)
    if timeline.get("balanced_price") is not None:
        section_summary.update(
            {
                "average_price": timeline["balanced_price"],
                "average_price_label": timeline["balanced_price_label"],
                "typical_price": timeline["balanced_price"],
                "typical_price_label": timeline["balanced_price_label"],
                "price_bucket_count": timeline["price_bucket_count"],
                "available_price_bucket_count": timeline[
                    "available_price_bucket_count"
                ],
                "price_bucket_game_threshold": timeline[
                    "minimum_games_per_bucket"
                ],
            }
        )
    map_context = _section_map_context(
        events,
        base_context.get("all_sections", []),
        section_name,
        sport_key=sport_key,
        venue=base_context["selected_venue"],
        section_getter=section_getter,
        geometry_getter=geometry_getter,
        event_label_builder=event_label_builder,
        exact_map_url_builder=map_url_builder,
        source_url_getter=source_url_getter,
    )

    report_url = url_for(
        report_endpoint,
        venue=base_context["selected_venue"],
    )
    buying_window_url = (
        buying_window_url_builder(section_name)
        if buying_window_url_builder is not None
        else ""
    )
    return {
        **base_context,
        **map_context,
        "error": None,
        "selected_section": section_name,
        "section_summary": section_summary,
        "section_games": section_games,
        "timeline": timeline,
        "report_url": report_url,
        "buying_window_url": buying_window_url,
        "event_moment": _EVENT_MOMENT_LABELS[sport_key],
    }


def _event_contains_section(
    event: Any,
    requested_section: str,
    section_getter: Callable[[Any], Iterable[str]],
    *,
    sport_key: str,
    venue: str,
) -> bool:
    requested = section_identity(sport_key, venue, requested_section)
    if requested is None:
        return False
    return any(
        identity is not None and identity.key == requested.key
        for section in section_getter(event)
        if (identity := section_identity(sport_key, venue, section)) is not None
    )


def _minimal_section_base_context(
    config: dict[str, Any],
    selected_venue: str,
    selected_team_label: str,
    sections: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        **config,
        "selected_venue": selected_venue,
        "selected_team_label": selected_team_label,
        "error": None,
        "all_sections": sections,
        "stadiums": [],
        "stadium_count": 0,
    }



def build_nfl_section_context(
    selected_venue: str,
    requested_section: str,
) -> dict[str, Any]:
    selected = _clean(selected_venue)
    config = _nfl_page_config()
    if not selected or not _clean(requested_section):
        return _section_error_context(
            {**config, "selected_venue": selected, "error": None},
            "Choose an NFL team and section.",
            requested_section,
        )

    now = datetime.now(timezone.utc)
    model = CreateNFLModel()
    try:
        with model.getSession()() as session:
            events = (
                session.query(NFLEvent)
                .options(defer(NFLEvent.map_geometry))
                .filter(
                    or_(
                        NFLEvent.canonical_venue == selected,
                        NFLEvent.provider_venue == selected,
                        NFLEvent.venue == selected,
                    )
                )
                .order_by(NFLEvent.event_date)
                .all()
            )
            events = [
                event
                for event in events
                if _is_us_event(event) and _clean(nfl_display_venue(event)) == selected
            ]
            if not events:
                return _section_error_context(
                    {**config, "selected_venue": "", "error": None},
                    f"{selected} is not in the tracked NFL stadium history.",
                    requested_section,
                )

            requested_identity = section_identity("nfl", selected, requested_section)
            rows = read_summary_rows(
                session,
                [event.id for event in events],
                section_keys=(requested_identity.key,) if requested_identity else (),
            )
            prepared = _prepared_summary_rows(events, rows, "nfl")
            sections, _analyzed = _finalize_section_insights(
                events,
                prepared,
                now,
                currency="USD",
                sport_key="nfl",
                detail_url_builder=lambda event, section: url_for(
                    "nfl_stadium.nfl_section",
                    venue=_clean(nfl_display_venue(event)),
                    section=section,
                ),
                secondary_url_builder=None,
                event_label_builder=format_nfl_title,
            )
            base_context = _minimal_section_base_context(
                config,
                selected,
                _team_label(events, nfl_event_home_team) or "NFL home team",
                sections,
            )

            geometry_by_id: dict[int, Any] = {}
            geometry_candidates = [
                event
                for event in events
                if _event_contains_section(
                    event,
                    requested_section,
                    lambda item: item.sections or [],
                    sport_key="nfl",
                    venue=selected,
                )
            ]
            if geometry_candidates:
                representative = max(
                    geometry_candidates,
                    key=lambda event: event_datetime_eastern(event.event_date),
                )
                geometry_by_id[representative.id] = session.execute(
                    select(NFLEvent.map_geometry).where(NFLEvent.id == representative.id)
                ).scalar_one_or_none()
    finally:
        dispose_ticket_engine(model.engine)

    return _build_section_detail_context(
        base_context,
        events,
        rows,
        requested_section,
        now,
        sport_key="nfl",
        currency="USD",
        report_endpoint="nfl_stadium.nfl_stadium",
        section_getter=lambda event: event.sections or [],
        geometry_getter=lambda event: geometry_by_id.get(event.id),
        event_label_builder=format_nfl_title,
        game_url_builder=lambda event, section: url_for(
            "nfl.nfl_graph",
            team=_clean(nfl_event_home_team(event)) or selected,
            game=str(event.id),
            section=section,
            display="money",
        ),
        map_url_builder=lambda event, section: url_for(
            "nfl.nfl_map",
            team=_clean(nfl_event_home_team(event)) or selected,
            game=str(event.id),
            section=section,
        ),
        source_url_getter=lambda event: event.source_url or "",
    )




def build_mlb_section_context(
    selected_venue: str,
    requested_section: str,
) -> dict[str, Any]:
    selected = _clean(selected_venue)
    config = _mlb_page_config()
    if not selected or not _clean(requested_section):
        return _section_error_context(
            {**config, "selected_venue": selected, "error": None},
            "Choose an MLB team and section.",
            requested_section,
        )

    now = datetime.now(timezone.utc)
    model = CreateModel()
    try:
        with model.getSession()() as session:
            events = (
                session.query(Event)
                .filter(Event.Place == selected)
                .order_by(Event.event_date)
                .all()
            )
            events = [event for event in events if _is_public_mlb_event(event)]
            if not events:
                return _section_error_context(
                    {**config, "selected_venue": "", "error": None},
                    f"{selected} is not in the tracked MLB stadium history.",
                    requested_section,
                )

            requested_identity = section_identity("mlb", selected, requested_section)
            rows = read_summary_rows(
                session,
                [event.id for event in events],
                section_keys=(requested_identity.key,) if requested_identity else (),
            )
            prepared = _prepared_summary_rows(events, rows, "mlb")
            sections, _analyzed = _finalize_section_insights(
                events,
                prepared,
                now,
                currency="USD",
                sport_key="mlb",
                detail_url_builder=lambda _event, section: url_for(
                    "nfl_stadium.mlb_section",
                    venue=selected,
                    section=section,
                ),
                secondary_url_builder=lambda _event, section: url_for(
                    "predict", event=selected, section=section
                ),
                event_label_builder=format_mlb_title,
            )
            base_context = _minimal_section_base_context(
                config,
                selected,
                _team_label(events, mlb_event_home_team) or mlb_team_for_venue(selected),
                sections,
            )
    finally:
        dispose_ticket_engine(model.engine)

    return _build_section_detail_context(
        base_context,
        events,
        rows,
        requested_section,
        now,
        sport_key="mlb",
        currency="USD",
        report_endpoint="nfl_stadium.mlb_stadium",
        section_getter=lambda event: event.event_sections or [],
        geometry_getter=None,
        event_label_builder=format_mlb_title,
        game_url_builder=lambda event, section: url_for(
            "graph",
            event=selected,
            game=str(event.id),
            section=section,
            mode="single",
            display="money",
        ),
        map_url_builder=None,
        source_url_getter=lambda event: event.URL or "",
        buying_window_url_builder=lambda section: url_for(
            "predict", event=selected, section=section
        ),
    )




def build_nhl_section_context(
    selected_venue: str,
    requested_section: str,
) -> dict[str, Any]:
    selected = _clean(selected_venue)
    if not selected or not _clean(requested_section):
        config = _nhl_page_config()
        return _section_error_context(
            {**config, "selected_venue": selected, "error": None},
            "Choose an NHL team and section.",
            requested_section,
        )

    now = datetime.now(timezone.utc)
    model = CreateNHLModel()
    try:
        with model.getSession()() as session:
            all_events = (
                session.query(NHLEvent)
                .options(defer(NHLEvent.map_geometry))
                .filter(
                    or_(
                        NHLEvent.canonical_venue == selected,
                        NHLEvent.provider_venue == selected,
                        NHLEvent.venue == selected,
                    )
                )
                .order_by(NHLEvent.event_date)
                .all()
            )
            all_events = [
                event
                for event in all_events
                if _is_supported_nhl_event(event)
                and _clean(nhl_display_venue(event)) == selected
            ]
            if not all_events:
                config = _nhl_page_config()
                return _section_error_context(
                    {**config, "selected_venue": "", "error": None},
                    f"{selected} is not in the tracked NHL arena history.",
                    requested_section,
                )

            currency_counts = Counter(
                (_clean(event.currency).upper() or "USD") for event in all_events
            )
            currency = currency_counts.most_common(1)[0][0]
            events = [
                event
                for event in all_events
                if (_clean(event.currency).upper() or "USD") == currency
            ]
            config = _nhl_page_config(currency)
            omitted = len(all_events) - len(events)
            if omitted:
                config["currency_warning"] = (
                    f"{omitted} game{'s were' if omitted != 1 else ' was'} omitted "
                    f"from section averages because its stored currency differs from {currency}."
                )

            requested_identity = section_identity("nhl", selected, requested_section)
            rows = read_summary_rows(
                session,
                [event.id for event in events],
                section_keys=(requested_identity.key,) if requested_identity else (),
            )
            prepared = _prepared_summary_rows(events, rows, "nhl")
            sections, _analyzed = _finalize_section_insights(
                events,
                prepared,
                now,
                currency=currency,
                sport_key="nhl",
                detail_url_builder=lambda _event, section: url_for(
                    "nfl_stadium.nhl_section",
                    venue=selected,
                    section=section,
                ),
                secondary_url_builder=None,
                event_label_builder=format_nhl_title,
            )
            base_context = _minimal_section_base_context(
                config,
                selected,
                _team_label(all_events, nhl_event_home_team) or "NHL home team",
                sections,
            )

            geometry_by_id: dict[int, Any] = {}
            geometry_candidates = [
                event
                for event in events
                if _event_contains_section(
                    event,
                    requested_section,
                    lambda item: item.sections or [],
                    sport_key="nhl",
                    venue=selected,
                )
            ]
            if geometry_candidates:
                representative = max(
                    geometry_candidates,
                    key=lambda event: event_datetime_eastern(event.event_date),
                )
                geometry_by_id[representative.id] = session.execute(
                    select(NHLEvent.map_geometry).where(NHLEvent.id == representative.id)
                ).scalar_one_or_none()
    finally:
        dispose_ticket_engine(model.engine)

    return _build_section_detail_context(
        base_context,
        events,
        rows,
        requested_section,
        now,
        sport_key="nhl",
        currency=currency,
        report_endpoint="nfl_stadium.nhl_arena",
        section_getter=lambda event: event.sections or [],
        geometry_getter=lambda event: geometry_by_id.get(event.id),
        event_label_builder=format_nhl_title,
        game_url_builder=lambda event, section: url_for(
            "nhl.nhl_graph",
            team=_clean(nhl_event_home_team(event)) or selected,
            game=str(event.id),
            section=section,
            display="money",
        ),
        map_url_builder=lambda event, section: url_for(
            "nhl.nhl_map",
            team=_clean(nhl_event_home_team(event)) or selected,
            game=str(event.id),
            section=section,
        ),
        source_url_getter=lambda event: event.source_url or "",
    )



def _sport_database_version(sport_key: str) -> str:
    paths = {
        "mlb": database_path,
        "nfl": nfl_database_path,
        "nhl": nhl_database_path,
    }
    return file_version(paths[sport_key]())


def _sport_venue_revision(sport_key: str, venue: str) -> int:
    models = {
        "mlb": CreateModel,
        "nfl": CreateNFLModel,
        "nhl": CreateNHLModel,
    }
    model = models[sport_key]()
    try:
        with model.getSession()() as session:
            return venue_revision(session, venue)
    finally:
        dispose_ticket_engine(model.engine)


def _cached_venue_context(sport_key: str, selected_venue: str) -> dict[str, Any]:
    builders: dict[str, Callable[[str], dict[str, Any]]] = {
        "mlb": build_mlb_stadium_context,
        "nfl": build_nfl_stadium_context,
        "nhl": build_nhl_arena_context,
    }
    selected = _clean(selected_venue)
    version: Any = (
        _sport_venue_revision(sport_key, selected)
        if selected
        else _sport_database_version(sport_key)
    )
    return page_cache.get_or_create(
        cache_key("venue-summary-v2", sport_key, version, selected),
        lambda: builders[sport_key](selected),
        ttl_seconds=PAGE_CACHE_TTL_SECONDS,
        tags=(sport_key, f"{sport_key}:{selected.casefold()}" if selected else sport_key),
    )


def _cached_section_context(
    sport_key: str,
    selected_venue: str,
    requested_section: str,
) -> dict[str, Any]:
    builders: dict[str, Callable[[str, str], dict[str, Any]]] = {
        "mlb": build_mlb_section_context,
        "nfl": build_nfl_section_context,
        "nhl": build_nhl_section_context,
    }
    selected = _clean(selected_venue)
    version: Any = (
        _sport_venue_revision(sport_key, selected)
        if selected
        else _sport_database_version(sport_key)
    )
    return page_cache.get_or_create(
        cache_key(
            "section-summary-v2",
            sport_key,
            version,
            selected,
            requested_section,
        ),
        lambda: builders[sport_key](selected, requested_section),
        ttl_seconds=PAGE_CACHE_TTL_SECONDS,
        tags=(sport_key, f"{sport_key}:{selected.casefold()}" if selected else sport_key),
    )


@nfl_stadium_blueprint.app_context_processor
def inject_team_display_helpers() -> dict[str, Any]:
    return {
        "mlb_team_for_venue": mlb_team_for_venue,
        "is_parking_section": is_parking_section,
    }


@nfl_stadium_blueprint.get("/nfl/stadium")
def nfl_stadium():
    context = _cached_venue_context("nfl", request.args.get("venue", ""))
    return render_template("nfl_stadium.html", **context)


@nfl_stadium_blueprint.get("/baseball/stadium")
def mlb_stadium():
    context = _cached_venue_context("mlb", request.args.get("venue", ""))
    return render_template("nfl_stadium.html", **context)


@nfl_stadium_blueprint.get("/nhl/arena")
def nhl_arena():
    context = _cached_venue_context("nhl", request.args.get("venue", ""))
    return render_template("nfl_stadium.html", **context)


@nfl_stadium_blueprint.get("/nfl/stadium/section")
def nfl_section():
    context = _cached_section_context(
        "nfl",
        request.args.get("venue") or request.args.get("team") or "",
        request.args.get("section", ""),
    )
    return render_template("venue_section.html", **context)


@nfl_stadium_blueprint.get("/baseball/stadium/section")
def mlb_section():
    context = _cached_section_context(
        "mlb",
        request.args.get("venue") or request.args.get("team") or "",
        request.args.get("section", ""),
    )
    return render_template("venue_section.html", **context)


@nfl_stadium_blueprint.get("/nhl/arena/section")
def nhl_section():
    context = _cached_section_context(
        "nhl",
        request.args.get("venue") or request.args.get("team") or "",
        request.args.get("section", ""),
    )
    return render_template("venue_section.html", **context)
