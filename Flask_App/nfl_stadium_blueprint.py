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
from sqlalchemy import select

from models import (
    CreateModel,
    Event,
    Iteration,
    Ticket,
    clean_event_title,
    event_datetime_eastern,
    event_has_complete_public_data,
    hours_before_event,
)
from Flask_App.nfl_blueprint import (
    CreateNFLModel,
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
    NHLEvent,
    NHLIteration,
    NHLTicket,
    format_nhl_title,
    nhl_display_venue,
    nhl_event_home_team,
)


nfl_stadium_blueprint = Blueprint("nfl_stadium", __name__)
MIN_DROP_SPAN = timedelta(hours=6)
LOW_SAMPLE_GAMES = 3
MLB_URL_MARKER = "--sports-mlb-baseball/"

_TIMELINE_BUCKETS = {
    "mlb": (
        (60.0, 72.0, "72h", "60–72 hours before"),
        (48.0, 60.0, "60h", "48–60 hours before"),
        (36.0, 48.0, "48h", "36–48 hours before"),
        (24.0, 36.0, "36h", "24–36 hours before"),
        (12.0, 24.0, "24h", "12–24 hours before"),
        (6.0, 12.0, "12h", "6–12 hours before"),
        (0.0, 6.0, "Game", "Final 6 hours"),
    ),
    "nfl": (
        (504.0, 720.0, "30d", "21–30 days before"),
        (336.0, 504.0, "21d", "14–21 days before"),
        (168.0, 336.0, "14d", "7–14 days before"),
        (72.0, 168.0, "7d", "3–7 days before"),
        (24.0, 72.0, "3d", "1–3 days before"),
        (12.0, 24.0, "24h", "12–24 hours before"),
        (6.0, 12.0, "12h", "6–12 hours before"),
        (0.0, 6.0, "Game", "Final 6 hours"),
    ),
    "nhl": (
        (504.0, 720.0, "30d", "21–30 days before"),
        (336.0, 504.0, "21d", "14–21 days before"),
        (168.0, 336.0, "14d", "7–14 days before"),
        (72.0, 168.0, "7d", "3–7 days before"),
        (24.0, 72.0, "3d", "1–3 days before"),
        (12.0, 24.0, "24h", "12–24 hours before"),
        (6.0, 12.0, "12h", "6–12 hours before"),
        (0.0, 6.0, "Game", "Final 6 hours"),
    ),
}

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
    rounded = round(value, 2)
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
        sections = {
            _clean(section)
            for event in venue_events
            for section in (getattr(event, "sections", None) or [])
            if _clean(section) and not is_parking_section(section)
        }
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
                "section_count": len(sections),
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
) -> list[Any]:
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
        .order_by(iteration_event_id, captured_at)
    )
    return list(session.execute(statement).all())


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
        detail_url_builder=lambda event, section: url_for(
            "nfl_stadium.nfl_section",
            venue=_clean(nfl_display_venue(event)),
            section=section,
        ),
        secondary_url_builder=None,
        event_label_builder=format_nfl_title,
    )


def _section_insights_for(
    events: list[Any],
    rows: Iterable[Any],
    now: datetime,
    *,
    currency: str,
    detail_url_builder: Callable[[Any, str], str | None],
    secondary_url_builder: Callable[[Any, str], str | None] | None,
    event_label_builder: Callable[[Any], str],
) -> tuple[list[dict[str, Any]], dict[int, set[datetime]]]:
    event_by_id = {event.id: event for event in events}
    completed_ids = {event.id for event in events if _event_completed(event, now)}
    histories: dict[tuple[str, int], list[tuple[datetime, float]]] = defaultdict(list)
    captures_by_event: dict[int, set[datetime]] = defaultdict(set)

    for row in rows:
        values = row._mapping if hasattr(row, "_mapping") else row
        event_id = int(values["event_id"])
        section = _clean(values["section"])
        captured = values["captured_at"]
        try:
            price = float(values["price"])
        except (TypeError, ValueError):
            continue
        if (
            not section
            or is_parking_section(section)
            or price <= 0
            or captured is None
        ):
            continue
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        histories[(section, event_id)].append((captured, price))
        captures_by_event[event_id].add(captured)

    per_game_prices: dict[str, list[float]] = defaultdict(list)
    per_game_drops: dict[str, list[tuple[float, float]]] = defaultdict(list)
    observation_count: dict[str, int] = defaultdict(int)
    latest_event_by_section: dict[str, Any] = {}

    for (section, event_id), points in histories.items():
        event = event_by_id.get(event_id)
        if event is None:
            continue

        ordered = sorted(points, key=lambda item: item[0])
        prices = [price for _, price in ordered]
        per_game_prices[section].append(float(median(prices)))
        observation_count[section] += len(ordered)

        latest_event = latest_event_by_section.get(section)
        if latest_event is None or event.event_date > latest_event.event_date:
            latest_event_by_section[section] = event

        if event_id not in completed_ids or len(ordered) < 2:
            continue
        first_time, first_price = ordered[0]
        last_time, last_price = ordered[-1]
        if last_time - first_time < MIN_DROP_SPAN or first_price <= 0:
            continue

        dollar_drop = first_price - last_price
        percent_drop = dollar_drop / first_price * 100
        per_game_drops[section].append((percent_drop, dollar_drop))

    insights = []
    for section, game_prices in per_game_prices.items():
        drop_rows = per_game_drops.get(section, [])
        average_percent_drop = (
            mean(row[0] for row in drop_rows) if drop_rows else None
        )
        average_dollar_drop = (
            mean(row[1] for row in drop_rows) if drop_rows else None
        )

        direction = "flat"
        direction_label = "No qualified movement"
        if average_percent_drop is not None and average_percent_drop > 0.05:
            direction = "down"
            direction_label = "Average decrease"
        elif average_percent_drop is not None and average_percent_drop < -0.05:
            direction = "up"
            direction_label = "Average increase"

        latest_event = latest_event_by_section.get(section)
        detail_url = (
            detail_url_builder(latest_event, section) if latest_event is not None else None
        )
        secondary_url = (
            secondary_url_builder(latest_event, section)
            if latest_event is not None and secondary_url_builder is not None
            else None
        )
        average_price = mean(game_prices)
        insights.append(
            {
                "name": section,
                "average_price": round(average_price, 2),
                "average_price_label": _currency_money(average_price, currency),
                "game_count": len(game_prices),
                "observation_count": observation_count[section],
                "drop_game_count": len(drop_rows),
                "average_percent_drop": (
                    round(average_percent_drop, 2)
                    if average_percent_drop is not None
                    else None
                ),
                "average_percent_drop_label": _percent_change(average_percent_drop),
                "average_dollar_drop": (
                    round(average_dollar_drop, 2)
                    if average_dollar_drop is not None
                    else None
                ),
                "average_dollar_drop_label": _currency_price_change(
                    average_dollar_drop,
                    currency,
                ),
                "drop_frequency": (
                    round(
                        sum(1 for percent, _ in drop_rows if percent > 0)
                        / len(drop_rows)
                        * 100
                    )
                    if drop_rows
                    else 0
                ),
                "direction": direction,
                "direction_label": direction_label,
                "is_low_price_sample": len(game_prices) < LOW_SAMPLE_GAMES,
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
    return insights, captures_by_event


def _rank_sections(
    sections: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cheapest = sorted(
        sections,
        key=lambda row: (row["average_price"], row["name"].casefold()),
    )[:5]
    biggest_drops = sorted(
        [
            row
            for row in sections
            if row["average_percent_drop"] is not None
            and row["average_percent_drop"] > 0
        ],
        key=lambda row: (-row["average_percent_drop"], row["name"].casefold()),
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
    section_getter: Callable[[Any], Iterable[str]],
    endpoint: str,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for event in events:
        venue = _clean(venue_getter(event))
        if venue:
            grouped[venue].append(event)

    result = []
    for venue, venue_events in grouped.items():
        sections = {
            _clean(section)
            for event in venue_events
            for section in section_getter(event)
            if _clean(section) and not is_parking_section(section)
        }
        team_label = _team_label(venue_events, team_getter)
        completed_count = sum(_event_completed(event, now) for event in venue_events)
        result.append(
            {
                "venue": venue,
                "game_count": len(venue_events),
                "completed_count": completed_count,
                "upcoming_count": len(venue_events) - completed_count,
                "section_count": len(sections),
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
            events = session.query(NFLEvent).order_by(NFLEvent.event_date).all()
            events = [
                event
                for event in events
                if _is_us_event(event) and _clean(nfl_display_venue(event))
            ]
            stadiums = _stadium_index(events, now)

            selected = _clean(selected_venue)
            if not selected:
                return {
                    **config,
                    "stadiums": stadiums,
                    "stadium_count": len(stadiums),
                    "selected_venue": "",
                    "error": None,
                }

            selected_events = [
                event for event in events if _clean(nfl_display_venue(event)) == selected
            ]
            if not selected_events:
                return {
                    **config,
                    "stadiums": stadiums,
                    "stadium_count": len(stadiums),
                    "selected_venue": "",
                    "error": f"{selected} is not in the tracked NFL stadium history.",
                }

            rows = _snapshot_rows(session, [event.id for event in selected_events])
            sections, captures_by_event = _section_insights(selected_events, rows, now)
    finally:
        model.engine.dispose()

    cheapest, biggest_drops = _rank_sections(sections)
    upcoming: list[tuple[datetime, dict[str, Any]]] = []
    completed: list[tuple[datetime, dict[str, Any]]] = []
    for event in selected_events:
        event_date = event_datetime_eastern(event.event_date)
        is_completed = _event_completed(event, now)
        team = _clean(nfl_event_home_team(event)) or selected
        map_url = url_for("nfl.nfl_map", team=team, game=str(event.id))
        public_sections = _public_sections(
            getattr(event, "sections", None) or []
        )
        game = {
            "id": event.id,
            "label": format_nfl_title(event),
            "status": "Completed" if is_completed else "Upcoming",
            "status_key": "completed" if is_completed else "upcoming",
            "snapshot_count": len(captures_by_event.get(event.id, set())),
            "section_count": len(public_sections),
            "sections": public_sections,
            "map_url": map_url,
            "direct_url": map_url,
            "base_url": "",
            "source_url": getattr(event, "source_url", ""),
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
        "observation_count": sum(row["observation_count"] for row in sections),
        "drop_section_count": sum(1 for row in sections if row["drop_game_count"]),
        "cheapest_sections": cheapest,
        "biggest_drops": biggest_drops,
        "all_sections": sections,
        "games": games,
        "method_price": (
            "Each game contributes one median observed section price. Those "
            "game-level values are then averaged, so a matchup with more "
            "snapshots does not receive extra weight."
        ),
        "method_drop": (
            "For completed games with at least six hours of observations, the "
            "first stored price is compared with the final stored price. Rankings "
            "use the average percentage decrease and also show the average dollar change."
        ),
    }


def mlb_team_for_venue(venue: Any) -> str:
    return _MLB_VENUE_TEAMS.get(_normalize(venue), "MLB home team")


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
    mapped = _MLB_VENUE_TEAMS.get(_normalize(getattr(event, "Place", "")))
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
            events = [
                event
                for event in session.query(Event).order_by(Event.event_date).all()
                if _is_public_mlb_event(event)
            ]
            stadiums = _generic_venue_index(
                events,
                now,
                venue_getter=lambda event: _clean(event.Place),
                team_getter=mlb_event_home_team,
                section_getter=lambda event: event.event_sections or [],
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

            selected_events = [event for event in events if _clean(event.Place) == selected]
            if not selected_events:
                return {
                    **config,
                    "stadiums": stadiums,
                    "stadium_count": len(stadiums),
                    "selected_venue": "",
                    "error": f"{selected} is not in the tracked MLB stadium history.",
                }

            rows = _snapshot_rows_for(
                session,
                [event.id for event in selected_events],
                Iteration,
                Ticket,
            )
            sections, captures_by_event = _section_insights_for(
                selected_events,
                rows,
                now,
                currency="USD",
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
    finally:
        model.engine.dispose()

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
            "snapshot_count": len(captures_by_event.get(event.id, set())),
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
        "observation_count": sum(row["observation_count"] for row in sections),
        "drop_section_count": sum(1 for row in sections if row["drop_game_count"]),
        "cheapest_sections": cheapest,
        "biggest_drops": biggest_drops,
        "all_sections": sections,
        "games": games,
        "method_price": (
            "Each game contributes one median observed section price inside the "
            "72-hour collection window. Those game-level values are then averaged."
        ),
        "method_drop": (
            "For completed games with at least six hours between the first and "
            "final stored observations, the dashboard calculates the percentage "
            "and dollar decrease for each game before averaging across games."
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
            events = [
                event
                for event in session.query(NHLEvent).order_by(NHLEvent.event_date).all()
                if _is_supported_nhl_event(event) and _clean(nhl_display_venue(event))
            ]
            stadiums = _generic_venue_index(
                events,
                now,
                venue_getter=nhl_display_venue,
                team_getter=nhl_event_home_team,
                section_getter=lambda event: event.sections or [],
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

            selected_events = [
                event for event in events if _clean(nhl_display_venue(event)) == selected
            ]
            if not selected_events:
                return {
                    **_nhl_page_config(),
                    "stadiums": stadiums,
                    "stadium_count": len(stadiums),
                    "selected_venue": "",
                    "error": f"{selected} is not in the tracked NHL arena history.",
                }

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

            rows = _snapshot_rows_for(
                session,
                [event.id for event in analysis_events],
                NHLIteration,
                NHLTicket,
            )
            sections, captures_by_event = _section_insights_for(
                analysis_events,
                rows,
                now,
                currency=analysis_currency,
                detail_url_builder=lambda _event, section: url_for(
                    "nfl_stadium.nhl_section",
                    venue=selected,
                    section=section,
                ),
                secondary_url_builder=None,
                event_label_builder=format_nhl_title,
            )
    finally:
        model.engine.dispose()

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
            "snapshot_count": len(captures_by_event.get(event.id, set())),
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
        "observation_count": sum(row["observation_count"] for row in sections),
        "drop_section_count": sum(1 for row in sections if row["drop_game_count"]),
        "cheapest_sections": cheapest,
        "biggest_drops": biggest_drops,
        "all_sections": sections,
        "games": games,
        "method_price": (
            "Each game contributes one median observed section price in the "
            "arena's stored currency. Those game-level values are then averaged."
        ),
        "method_drop": (
            "For completed games with at least six hours of observations, the "
            "first stored price is compared with the final stored price. Rankings "
            "use average percentage decrease and also show average currency movement."
        ),
    }


def _resolve_section(
    sections: Iterable[dict[str, Any]],
    requested: str,
) -> dict[str, Any] | None:
    requested_clean = _clean(requested)
    if not requested_clean or is_parking_section(requested_clean):
        return None

    rows = list(sections)
    for row in rows:
        if _clean(row.get("name")) == requested_clean:
            return row

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


def _selected_section_histories(
    events: Iterable[Any],
    rows: Iterable[Any],
    section_name: str,
) -> dict[int, list[tuple[datetime, float, float]]]:
    event_by_id = {event.id: event for event in events}
    normalized_section = _normalize(section_name)
    histories: dict[int, list[tuple[datetime, float, float]]] = defaultdict(list)

    for row in rows:
        values = row._mapping if hasattr(row, "_mapping") else row
        event_id = int(values["event_id"])
        event = event_by_id.get(event_id)
        if event is None or _normalize(values["section"]) != normalized_section:
            continue

        captured = values["captured_at"]
        if captured is None:
            continue
        try:
            price = float(values["price"])
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue

        lead_time = hours_before_event(event.event_date, captured)
        if lead_time <= 0:
            continue
        histories[event_id].append((captured, lead_time, price))

    for event_id in histories:
        histories[event_id].sort(key=lambda item: item[0])
    return histories


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
    buckets = _TIMELINE_BUCKETS[sport_key]
    bucket_game_values: list[list[float]] = [[] for _ in buckets]
    histories = _selected_section_histories(events, rows, section_name)
    event_by_id = {event.id: event for event in events}
    game_rows: list[tuple[datetime, dict[str, Any]]] = []

    for event_id, history in histories.items():
        event = event_by_id[event_id]
        per_bucket: list[list[float]] = [[] for _ in buckets]
        for _captured, lead_time, price in history:
            bucket_index = _timeline_bucket_index(sport_key, lead_time)
            if bucket_index is not None:
                per_bucket[bucket_index].append(price)
        for index, prices in enumerate(per_bucket):
            if prices:
                bucket_game_values[index].append(float(median(prices)))

        first_time, first_lead, first_price = history[0]
        last_time, last_lead, last_price = history[-1]
        dollar_drop = first_price - last_price if len(history) > 1 else None
        percent_drop = (
            dollar_drop / first_price * 100
            if dollar_drop is not None and first_price > 0
            else None
        )
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
                    "average_price": round(float(median([item[2] for item in history])), 2),
                    "average_price_label": _currency_money(
                        float(median([item[2] for item in history])),
                        currency,
                    ),
                    "latest_price_label": _currency_money(last_price, currency),
                    "movement_label": _currency_price_change(dollar_drop, currency),
                    "movement_percent_label": _percent_change(percent_drop),
                    "observation_count": len(history),
                    "coverage_label": (
                        f"{_lead_time_label(first_lead)} to "
                        f"{_lead_time_label(last_lead)} before"
                    ),
                    "game_url": game_url_builder(event, section_name),
                    "map_url": (
                        map_url_builder(event, section_name)
                        if map_url_builder is not None
                        else None
                    ),
                    "source_url": source_url_getter(event),
                    "first_captured_at": first_time,
                    "last_captured_at": last_time,
                },
            )
        )

    timeline_points = []
    for slot, ((lower, upper, short_label, label), game_values) in enumerate(
        zip(buckets, bucket_game_values)
    ):
        if not game_values:
            continue
        average_price = mean(game_values)
        timeline_points.append(
            {
                "slot": slot,
                "short_label": short_label,
                "label": label,
                "lower_hours": lower,
                "upper_hours": upper,
                "average_price": round(average_price, 2),
                "average_price_label": _currency_money(average_price, currency),
                "game_count": len(game_values),
            }
        )

    timeline_drop = None
    timeline_percent = None
    timeline_direction = "flat"
    timeline_direction_label = "Not enough points"
    if len(timeline_points) >= 2:
        first_point = timeline_points[0]
        last_point = timeline_points[-1]
        timeline_drop = first_point["average_price"] - last_point["average_price"]
        if first_point["average_price"] > 0:
            timeline_percent = timeline_drop / first_point["average_price"] * 100
        if timeline_drop > 0.005:
            timeline_direction = "down"
            timeline_direction_label = "Average price fell"
        elif timeline_drop < -0.005:
            timeline_direction = "up"
            timeline_direction_label = "Average price rose"
        else:
            timeline_direction_label = "Average price was flat"

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
            "slot_count": len(buckets),
            "currency": currency,
            "event_moment": _EVENT_MOMENT_LABELS[sport_key],
            "movement_label": _currency_price_change(timeline_drop, currency),
            "movement_percent_label": _percent_change(timeline_percent),
            "movement_direction": timeline_direction,
            "movement_direction_label": timeline_direction_label,
            "from_label": timeline_points[0]["label"] if timeline_points else "",
            "to_label": timeline_points[-1]["label"] if timeline_points else "",
        },
        [row for _date, row in upcoming + completed],
    )


def _event_section_name(event: Any, section_getter: Callable[[Any], Iterable[str]], requested: str) -> str | None:
    normalized = _normalize(requested)
    matches = [
        section
        for section in _public_sections(section_getter(event))
        if _normalize(section) == normalized
    ]
    return matches[0] if len(matches) == 1 else None


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
    selected_normalized = _normalize(section_name)

    for event in events:
        matched_name = _event_section_name(event, section_getter, section_name)
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
                    _normalize(row.get("name")) == selected_normalized
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

    averages_by_name = {_normalize(row["name"]): row for row in section_rows}
    if representative_event is not None:
        visible_sections = _public_sections(section_getter(representative_event))
    else:
        visible_sections = [row["name"] for row in section_rows]

    map_sections = []
    for name in visible_sections:
        aggregate = averages_by_name.get(_normalize(name))
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


def build_nfl_section_context(
    selected_venue: str,
    requested_section: str,
) -> dict[str, Any]:
    base_context = build_nfl_stadium_context(selected_venue)
    if base_context.get("error") or not base_context.get("selected_venue"):
        return _section_error_context(
            base_context,
            base_context.get("error") or "Choose an NFL team and section.",
            requested_section,
        )

    now = datetime.now(timezone.utc)
    model = CreateNFLModel()
    try:
        with model.getSession()() as session:
            events = [
                event
                for event in session.query(NFLEvent).order_by(NFLEvent.event_date).all()
                if _is_us_event(event)
                and _clean(nfl_display_venue(event)) == base_context["selected_venue"]
            ]
            rows = _snapshot_rows(session, [event.id for event in events])
    finally:
        model.engine.dispose()

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
        geometry_getter=lambda event: event.map_geometry,
        event_label_builder=format_nfl_title,
        game_url_builder=lambda event, section: url_for(
            "nfl.nfl_graph",
            team=_clean(nfl_event_home_team(event)) or base_context["selected_venue"],
            game=str(event.id),
            section=section,
            display="money",
        ),
        map_url_builder=lambda event, section: url_for(
            "nfl.nfl_map",
            team=_clean(nfl_event_home_team(event)) or base_context["selected_venue"],
            game=str(event.id),
            section=section,
        ),
        source_url_getter=lambda event: event.source_url or "",
    )


def build_mlb_section_context(
    selected_venue: str,
    requested_section: str,
) -> dict[str, Any]:
    base_context = build_mlb_stadium_context(selected_venue)
    if base_context.get("error") or not base_context.get("selected_venue"):
        return _section_error_context(
            base_context,
            base_context.get("error") or "Choose an MLB team and section.",
            requested_section,
        )

    now = datetime.now(timezone.utc)
    model = CreateModel()
    try:
        with model.getSession()() as session:
            events = [
                event
                for event in session.query(Event).order_by(Event.event_date).all()
                if _is_public_mlb_event(event)
                and _clean(event.Place) == base_context["selected_venue"]
            ]
            rows = _snapshot_rows_for(
                session,
                [event.id for event in events],
                Iteration,
                Ticket,
            )
    finally:
        model.engine.dispose()

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
            event=base_context["selected_venue"],
            game=str(event.id),
            section=section,
            mode="single",
            display="money",
        ),
        map_url_builder=None,
        source_url_getter=lambda event: event.URL or "",
        buying_window_url_builder=lambda section: url_for(
            "predict",
            event=base_context["selected_venue"],
            section=section,
        ),
    )


def build_nhl_section_context(
    selected_venue: str,
    requested_section: str,
) -> dict[str, Any]:
    base_context = build_nhl_arena_context(selected_venue)
    if base_context.get("error") or not base_context.get("selected_venue"):
        return _section_error_context(
            base_context,
            base_context.get("error") or "Choose an NHL team and section.",
            requested_section,
        )

    currency = base_context.get("currency_label") or "USD"
    now = datetime.now(timezone.utc)
    model = CreateNHLModel()
    try:
        with model.getSession()() as session:
            events = [
                event
                for event in session.query(NHLEvent).order_by(NHLEvent.event_date).all()
                if _is_supported_nhl_event(event)
                and _clean(nhl_display_venue(event)) == base_context["selected_venue"]
                and (_clean(event.currency).upper() or "USD") == currency
            ]
            rows = _snapshot_rows_for(
                session,
                [event.id for event in events],
                NHLIteration,
                NHLTicket,
            )
    finally:
        model.engine.dispose()

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
        geometry_getter=lambda event: event.map_geometry,
        event_label_builder=format_nhl_title,
        game_url_builder=lambda event, section: url_for(
            "nhl.nhl_graph",
            team=_clean(nhl_event_home_team(event)) or base_context["selected_venue"],
            game=str(event.id),
            section=section,
            display="money",
        ),
        map_url_builder=lambda event, section: url_for(
            "nhl.nhl_map",
            team=_clean(nhl_event_home_team(event)) or base_context["selected_venue"],
            game=str(event.id),
            section=section,
        ),
        source_url_getter=lambda event: event.source_url or "",
    )


@nfl_stadium_blueprint.app_context_processor
def inject_team_display_helpers() -> dict[str, Any]:
    return {
        "mlb_team_for_venue": mlb_team_for_venue,
        "is_parking_section": is_parking_section,
    }


@nfl_stadium_blueprint.get("/nfl/stadium")
def nfl_stadium():
    context = build_nfl_stadium_context(request.args.get("venue", ""))
    return render_template("nfl_stadium.html", **context)


@nfl_stadium_blueprint.get("/baseball/stadium")
def mlb_stadium():
    context = build_mlb_stadium_context(request.args.get("venue", ""))
    return render_template("nfl_stadium.html", **context)


@nfl_stadium_blueprint.get("/nhl/arena")
def nhl_arena():
    context = build_nhl_arena_context(request.args.get("venue", ""))
    return render_template("nfl_stadium.html", **context)


@nfl_stadium_blueprint.get("/nfl/stadium/section")
def nfl_section():
    context = build_nfl_section_context(
        request.args.get("venue") or request.args.get("team") or "",
        request.args.get("section", ""),
    )
    return render_template("venue_section.html", **context)


@nfl_stadium_blueprint.get("/baseball/stadium/section")
def mlb_section():
    context = build_mlb_section_context(
        request.args.get("venue") or request.args.get("team") or "",
        request.args.get("section", ""),
    )
    return render_template("venue_section.html", **context)


@nfl_stadium_blueprint.get("/nhl/arena/section")
def nhl_section():
    context = build_nhl_section_context(
        request.args.get("venue") or request.args.get("team") or "",
        request.args.get("section", ""),
    )
    return render_template("venue_section.html", **context)
