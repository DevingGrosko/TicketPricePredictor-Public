"""Stadium-first NFL section analytics and discovery views."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean, median
from typing import Any, Iterable

from flask import Blueprint, render_template, request, url_for
from sqlalchemy import select

from models import event_datetime_eastern
from Flask_App.nfl_blueprint import (
    CreateNFLModel,
    NFLEvent,
    NFLIteration,
    NFLTicket,
    format_nfl_title,
    nfl_display_venue,
    nfl_event_home_team,
)


nfl_stadium_blueprint = Blueprint("nfl_stadium", __name__)
MIN_DROP_SPAN = timedelta(hours=6)
LOW_SAMPLE_GAMES = 3


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    rounded = round(value, 2)
    return f"${rounded:,.0f}" if rounded.is_integer() else f"${rounded:,.2f}"


def _percent(value: float | None) -> str:
    if value is None:
        return "—"
    sign = "−" if value > 0 else "+" if value < 0 else ""
    return f"{sign}{abs(value):.1f}%"


def _column(table: Any, *names: str) -> Any:
    for name in names:
        if name in table.c:
            return table.c[name]
    raise RuntimeError(f"Missing expected column in {table.name}: {', '.join(names)}")


def _is_us_event(event: NFLEvent) -> bool:
    country = _clean(getattr(event, "country", "")).casefold()
    return not country or country in {
        "us",
        "u.s.",
        "usa",
        "u.s.a.",
        "united states",
        "united states of america",
    }


def _event_completed(event: NFLEvent, now: datetime) -> bool:
    event_date = event_datetime_eastern(event.event_date)
    return event_date <= now.astimezone(event_date.tzinfo)


def _stadium_index(events: Iterable[NFLEvent]) -> list[dict[str, Any]]:
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
        result.append(
            {
                "venue": venue,
                "game_count": len(venue_events),
                "section_count": len(sections),
                "team_label": ", ".join(teams[:2]),
                "url": url_for("nfl_stadium.nfl_stadium", venue=venue),
            }
        )
    return sorted(result, key=lambda row: row["venue"].casefold())


def _snapshot_rows(session: Any, event_ids: list[int]) -> list[Any]:
    if not event_ids:
        return []

    iteration = NFLIteration.__table__
    ticket = NFLTicket.__table__
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

    price_by_section: dict[str, list[float]] = defaultdict(list)
    drops_by_section: dict[str, list[tuple[float, float]]] = defaultdict(list)
    observation_count: dict[str, int] = defaultdict(int)
    latest_event_by_section: dict[str, NFLEvent] = {}

    for (section, event_id), points in histories.items():
        event = event_by_id.get(event_id)
        if event is None:
            continue
        ordered = sorted(points, key=lambda item: item[0])
        prices = [price for _, price in ordered]
        price_by_section[section].append(float(median(prices)))
        observation_count[section] += len(ordered)

        current_latest = latest_event_by_section.get(section)
        if current_latest is None or event.event_date > current_latest.event_date:
            latest_event_by_section[section] = event

        if event_id not in completed_ids or len(ordered) < 2:
            continue
        first_time, first_price = ordered[0]
        last_time, last_price = ordered[-1]
        if last_time - first_time < MIN_DROP_SPAN or first_price <= 0:
            continue
        dollar_drop = first_price - last_price
        percent_drop = dollar_drop / first_price * 100
        drops_by_section[section].append((percent_drop, dollar_drop))

    insights = []
    for section, per_game_prices in price_by_section.items():
        drop_rows = drops_by_section.get(section, [])
        avg_percent = mean(row[0] for row in drop_rows) if drop_rows else None
        avg_dollars = mean(row[1] for row in drop_rows) if drop_rows else None
        direction = "flat"
        direction_label = "No qualified movement"
        if avg_percent is not None and avg_percent > 0.05:
            direction, direction_label = "down", "Average decrease"
        elif avg_percent is not None and avg_percent < -0.05:
            direction, direction_label = "up", "Average increase"

        latest_event = latest_event_by_section.get(section)
        map_url = None
        if latest_event is not None:
            team = _clean(nfl_event_home_team(latest_event)) or _clean(nfl_display_venue(latest_event))
            map_url = url_for(
                "nfl.nfl_map",
                team=team,
                game=str(latest_event.id),
                section=section,
            )

        average_price = mean(per_game_prices)
        insights.append(
            {
                "name": section,
                "average_price": round(average_price, 2),
                "average_price_label": _money(average_price),
                "game_count": len(per_game_prices),
                "observation_count": observation_count[section],
                "drop_game_count": len(drop_rows),
                "average_percent_drop": round(avg_percent, 2) if avg_percent is not None else None,
                "average_percent_drop_label": _percent(avg_percent),
                "average_dollar_drop": round(avg_dollars, 2) if avg_dollars is not None else None,
                "average_dollar_drop_label": _money(abs(avg_dollars)) if avg_dollars is not None else "—",
                "drop_frequency": round(sum(1 for percent, _ in drop_rows if percent > 0) / len(drop_rows) * 100) if drop_rows else 0,
                "direction": direction,
                "direction_label": direction_label,
                "is_low_price_sample": len(per_game_prices) < LOW_SAMPLE_GAMES,
                "is_low_drop_sample": 0 < len(drop_rows) < LOW_SAMPLE_GAMES,
                "map_url": map_url,
            }
        )

    insights.sort(key=lambda row: (row["average_price"], row["name"].casefold()))
    return insights, captures_by_event


def build_nfl_stadium_context(selected_venue: str = "") -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    model = CreateNFLModel()
    with model.getSession()() as session:
        events = session.query(NFLEvent).order_by(NFLEvent.event_date).all()
        events = [event for event in events if _is_us_event(event) and _clean(nfl_display_venue(event))]
        stadiums = _stadium_index(events)

        selected = _clean(selected_venue)
        if not selected:
            return {
                "stadiums": stadiums,
                "selected_venue": "",
                "error": None,
            }

        selected_events = [event for event in events if _clean(nfl_display_venue(event)) == selected]
        if not selected_events:
            return {
                "stadiums": stadiums,
                "selected_venue": "",
                "error": f"{selected} is not in the tracked NFL stadium history.",
            }

        rows = _snapshot_rows(session, [event.id for event in selected_events])
        sections, captures_by_event = _section_insights(selected_events, rows, now)

    cheapest = sorted(sections, key=lambda row: (row["average_price"], row["name"].casefold()))[:5]
    biggest_drops = sorted(
        [row for row in sections if row["average_percent_drop"] is not None and row["average_percent_drop"] > 0],
        key=lambda row: (-row["average_percent_drop"], row["name"].casefold()),
    )[:5]

    upcoming: list[tuple[datetime, dict[str, Any]]] = []
    completed: list[tuple[datetime, dict[str, Any]]] = []
    for event in selected_events:
        event_date = event_datetime_eastern(event.event_date)
        is_completed = _event_completed(event, now)
        team = _clean(nfl_event_home_team(event)) or selected
        row = {
            "id": event.id,
            "label": format_nfl_title(event),
            "status": "Completed" if is_completed else "Upcoming",
            "status_key": "completed" if is_completed else "upcoming",
            "snapshot_count": len(captures_by_event.get(event.id, set())),
            "section_count": len(getattr(event, "sections", None) or []),
            "map_url": url_for("nfl.nfl_map", team=team, game=str(event.id)),
            "source_url": getattr(event, "source_url", ""),
        }
        (completed if is_completed else upcoming).append((event_date, row))
    upcoming.sort(key=lambda item: item[0])
    completed.sort(key=lambda item: item[0], reverse=True)
    games = [row for _, row in upcoming + completed]

    return {
        "stadiums": stadiums,
        "selected_venue": selected,
        "error": None,
        "game_count": len(selected_events),
        "section_count": len(sections),
        "observation_count": sum(row["observation_count"] for row in sections),
        "drop_section_count": sum(1 for row in sections if row["drop_game_count"]),
        "cheapest_sections": cheapest,
        "biggest_drops": biggest_drops,
        "all_sections": sections,
        "games": games,
        "method_price": "Each game contributes one median section price; those game-level values are then averaged, so extra snapshots do not give one matchup more weight.",
        "method_drop": "Each completed qualifying game contributes one first-to-latest price change after at least six hours of tracking.",
    }


@nfl_stadium_blueprint.get("/nfl/stadium")
def nfl_stadium():
    context = build_nfl_stadium_context(request.args.get("venue", ""))
    return render_template("nfl_stadium.html", **context)
