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
)
from Flask_App.nfl_blueprint import (
    CreateNFLModel,
    NFLEvent,
    NFLIteration,
    NFLTicket,
    format_nfl_title,
    nfl_display_venue,
    nfl_event_home_team,
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
            if _clean(section)
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
            "nfl.nfl_map",
            team=_clean(nfl_event_home_team(event)) or _clean(nfl_display_venue(event)),
            game=str(event.id),
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
        if not section or price <= 0 or captured is None:
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
            if _clean(section)
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
        section_action_label="Latest map",
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
        game = {
            "id": event.id,
            "label": format_nfl_title(event),
            "status": "Completed" if is_completed else "Upcoming",
            "status_key": "completed" if is_completed else "upcoming",
            "snapshot_count": len(captures_by_event.get(event.id, set())),
            "section_count": len(getattr(event, "sections", None) or []),
            "sections": sorted(set(getattr(event, "sections", None) or [])),
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
                    "graph",
                    event=selected,
                    section=section,
                    mode="multi",
                    display="money",
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
        game = {
            "id": event.id,
            "label": format_mlb_title(event),
            "status": "Completed" if is_completed else "Upcoming",
            "status_key": "completed" if is_completed else "upcoming",
            "snapshot_count": len(captures_by_event.get(event.id, set())),
            "section_count": len(event.event_sections or []),
            "sections": sorted(set(event.event_sections or []), key=str.casefold),
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
        section_action_label="Latest arena map",
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
                detail_url_builder=lambda event, section: url_for(
                    "nhl.nhl_map",
                    team=_clean(nhl_event_home_team(event)) or selected,
                    game=str(event.id),
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
        game = {
            "id": event.id,
            "label": format_nhl_title(event),
            "status": "Completed" if is_completed else "Upcoming",
            "status_key": "completed" if is_completed else "upcoming",
            "snapshot_count": len(captures_by_event.get(event.id, set())),
            "section_count": len(event.sections or []),
            "sections": sorted(set(event.sections or []), key=str.casefold),
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


@nfl_stadium_blueprint.app_context_processor
def inject_team_display_helpers() -> dict[str, Any]:
    return {"mlb_team_for_venue": mlb_team_for_venue}


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
