"""Persistent MLB team-report payloads built from event-level materialized analytics.

The public team page reads one compact JSON row instead of rebuilding section
rankings on every request. Raw snapshots and event-level bucket summaries remain
the source of truth; this layer can always be rebuilt from them.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from flask import render_template, request, url_for
from sqlalchemy import Column, DateTime, Integer, JSON, MetaData, String, Table, delete, insert, select
from sqlalchemy.orm import load_only

from models import (
    CreateModel,
    Event,
    event_datetime_utc,
    event_has_complete_public_data,
)
from Flask_App.database_config import dispose_ticket_engine
from Flask_App.materialized_analytics import read_summary_rows, venue_revision
from Flask_App.report_policy import latest_season_events, report_venue, season_key, venue_aliases


TEAM_REPORT_SCHEMA_VERSION = 1
MLB_URL_MARKER = "--sports-mlb-baseball/"

_METADATA = MetaData()
TEAM_REPORT_SUMMARY = Table(
    "team_report_summary",
    _METADATA,
    Column("sport", String(16), primary_key=True),
    Column("venue", String(191), primary_key=True),
    Column("season", Integer, primary_key=True),
    Column("summary_version", Integer, nullable=False),
    Column("source_revision", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("refreshed_at", DateTime(), nullable=False),
)


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _ensure_schema(bind: Any) -> None:
    _METADATA.create_all(bind)


def _mlb_public_event(event: Event) -> bool:
    return bool(
        event.Place
        and MLB_URL_MARKER in str(event.URL or "").casefold()
        and event_has_complete_public_data(event)
    )


def _venue_revision(session: Any, venue: str) -> int:
    return sum(venue_revision(session, alias) for alias in venue_aliases(venue))


def _completed_count(
    events: Iterable[Event],
    now: datetime | None = None,
) -> int:
    current = now or datetime.now(timezone.utc)
    return sum(
        1
        for event in events
        if event.event_date and event_datetime_utc(event.event_date) <= current
    )


def _group_latest_venues(events: Iterable[Event]) -> dict[str, tuple[list[Event], int]]:
    grouped: dict[str, list[Event]] = defaultdict(list)
    for event in events:
        if _mlb_public_event(event):
            canonical = report_venue(event.Place)
            if canonical:
                grouped[canonical].append(event)

    result: dict[str, tuple[list[Event], int]] = {}
    for venue, rows in grouped.items():
        cohort, year = latest_season_events(rows, "mlb")
        if cohort and year is not None:
            result[venue] = (cohort, int(year))
    return result


def stale_mlb_team_report_venues(session: Any, events: Iterable[Event]) -> list[str]:
    """Return canonical venues whose latest-season team payload is absent/stale."""
    _ensure_schema(session.connection())
    grouped = _group_latest_venues(events)
    if not grouped:
        return []

    rows = session.execute(
        select(TEAM_REPORT_SUMMARY).where(
            TEAM_REPORT_SUMMARY.c.sport == "mlb",
            TEAM_REPORT_SUMMARY.c.venue.in_(sorted(grouped)),
        )
    ).mappings().all()
    stored = {(str(row["venue"]), int(row["season"])): row for row in rows}
    now = datetime.now(timezone.utc)

    stale: list[str] = []
    for venue, (venue_events, year) in sorted(grouped.items()):
        row = stored.get((venue, year))
        revision = _venue_revision(session, venue)
        payload = dict(row["payload"] or {}) if row is not None else {}
        if (
            row is None
            or int(row["summary_version"]) != TEAM_REPORT_SCHEMA_VERSION
            or int(row["source_revision"]) != revision
            or int(payload.get("completed_game_count") or 0)
            != _completed_count(venue_events, now)
        ):
            stale.append(venue)
    return stale


def _full_events_for_venue(
    session: Any,
    venue: str,
    candidate_events: Iterable[Event] | None = None,
) -> tuple[list[Event], int | None]:
    canonical = report_venue(venue)
    if candidate_events is None:
        rows = (
            session.query(Event)
            .filter(Event.Place.in_(venue_aliases(canonical)))
            .order_by(Event.event_date)
            .all()
        )
    else:
        ids = [
            int(event.id)
            for event in candidate_events
            if report_venue(getattr(event, "Place", "")) == canonical
        ]
        if not ids:
            return [], None
        rows = (
            session.query(Event)
            .filter(Event.id.in_(ids))
            .order_by(Event.event_date)
            .all()
        )
    rows = [event for event in rows if _mlb_public_event(event)]
    cohort, year = latest_season_events(rows, "mlb")
    return cohort, int(year) if year is not None else None


def _compact_payload(session: Any, venue: str, events: list[Event], year: int) -> dict[str, Any]:
    # Reuse the exact ranking implementation already used by the public report.
    # This work now happens when data changes rather than when a visitor clicks.
    from Flask_App.nfl_stadium_blueprint import (
        _finalize_section_insights,
        _prepared_summary_rows,
        _rank_sections,
        format_mlb_title,
    )

    now = datetime.now(timezone.utc)
    bucket_rows = read_summary_rows(session, [int(event.id) for event in events])
    prepared = _prepared_summary_rows(events, bucket_rows, "mlb")
    sections, _analyzed = _finalize_section_insights(
        events,
        prepared,
        now,
        currency="USD",
        sport_key="mlb",
        detail_url_builder=lambda _event, _section: None,
        secondary_url_builder=None,
        event_label_builder=format_mlb_title,
    )
    cheapest, biggest_drops = _rank_sections(sections)

    def compact(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "section_key": _clean(row.get("section_key")),
            "name": _clean(row.get("name")),
            "game_count": int(row.get("game_count") or 0),
            "ranking_price": row.get("ranking_price"),
            "ranking_price_label": row.get("ranking_price_label"),
            "ranking_price_games": int(row.get("ranking_price_games") or 0),
            "ranking_drop_percent": row.get("ranking_drop_percent"),
            "ranking_drop_label": row.get("ranking_drop_label"),
            "ranking_drop_games": int(row.get("ranking_drop_games") or 0),
            "ranking_total_games": int(row.get("ranking_total_games") or 0),
            "ranking_required_games": int(row.get("ranking_required_games") or 0),
            "ranking_price_eligible": bool(row.get("ranking_price_eligible")),
            "ranking_drop_eligible": bool(row.get("ranking_drop_eligible")),
        }

    compact_sections = [compact(row) for row in sections]
    by_key = {row["section_key"]: row for row in compact_sections}
    return {
        "venue": report_venue(venue),
        "season": int(year),
        "game_count": len(events),
        "completed_game_count": _completed_count(events, now),
        "all_sections": compact_sections,
        "cheapest_section_keys": [row["section_key"] for row in cheapest],
        "biggest_drop_keys": [row["section_key"] for row in biggest_drops],
        "section_count": len(compact_sections),
        "ranking_total_games": max(
            (int(row.get("ranking_total_games") or 0) for row in by_key.values()),
            default=0,
        ),
    }


def refresh_mlb_team_report_in_session(
    session: Any,
    venue: str,
    *,
    candidate_events: Iterable[Event] | None = None,
) -> dict[str, Any] | None:
    """Rebuild one canonical MLB venue's latest-season report in this transaction."""
    _ensure_schema(session.connection())
    canonical = report_venue(venue)
    events, year = _full_events_for_venue(
        session,
        canonical,
        candidate_events=candidate_events,
    )
    if not events or year is None:
        return None

    payload = _compact_payload(session, canonical, events, year)
    revision = _venue_revision(session, canonical)
    refreshed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.execute(
        delete(TEAM_REPORT_SUMMARY).where(
            TEAM_REPORT_SUMMARY.c.sport == "mlb",
            TEAM_REPORT_SUMMARY.c.venue == canonical,
            TEAM_REPORT_SUMMARY.c.season == year,
        )
    )
    session.execute(
        insert(TEAM_REPORT_SUMMARY).values(
            sport="mlb",
            venue=canonical,
            season=year,
            summary_version=TEAM_REPORT_SCHEMA_VERSION,
            source_revision=revision,
            payload=payload,
            refreshed_at=refreshed_at,
        )
    )
    return payload


def refresh_mlb_team_report(venue: str) -> bool:
    """Refresh one MLB team report after a successful event-summary update."""
    model = CreateModel()
    try:
        with model.getSession()() as session:
            payload = refresh_mlb_team_report_in_session(session, venue)
            session.commit()
            return payload is not None
    finally:
        dispose_ticket_engine(model.engine)


def read_mlb_team_report(
    session: Any,
    venue: str,
    season: int,
    *,
    current_completed_count: int | None = None,
) -> dict[str, Any] | None:
    """Read a fresh persistent report, returning None when maintenance is behind."""
    _ensure_schema(session.connection())
    canonical = report_venue(venue)
    row = session.execute(
        select(TEAM_REPORT_SUMMARY).where(
            TEAM_REPORT_SUMMARY.c.sport == "mlb",
            TEAM_REPORT_SUMMARY.c.venue == canonical,
            TEAM_REPORT_SUMMARY.c.season == int(season),
        )
    ).mappings().one_or_none()
    if row is None:
        return None
    if int(row["summary_version"]) != TEAM_REPORT_SCHEMA_VERSION:
        return None
    if int(row["source_revision"]) != _venue_revision(session, canonical):
        return None
    payload = dict(row["payload"] or {})
    if (
        current_completed_count is not None
        and int(payload.get("completed_game_count") or 0)
        != int(current_completed_count)
    ):
        return None
    return payload


def render_materialized_mlb_team_report():
    """Fast replacement view for /baseball/stadium using one persistent payload."""
    from Flask_App.nfl_stadium_blueprint import (
        _directory_events,
        _empty_report,
        _generic_venue_index,
        _mlb_page_config,
        _select_report,
        build_mlb_stadium_context,
        mlb_event_home_team,
    )

    selected_venue = request.args.get("venue", "")
    selected_team = request.args.get("team", "")
    now = datetime.now(timezone.utc)
    model = CreateModel()
    try:
        with model.getSession()() as session:
            directory_events = _directory_events(session, "mlb")
            stadiums = _generic_venue_index(
                directory_events,
                now,
                venue_getter=lambda event: _clean(event.Place),
                team_getter=mlb_event_home_team,
                endpoint="nfl_stadium.mlb_stadium",
            )
            choice = _select_report(directory_events, "mlb", selected_venue, selected_team)
            config = _mlb_page_config()
            if not choice.get("events"):
                context = _empty_report(config, stadiums, choice)
                return render_template("nfl_stadium.html", **context)

            selected = choice["selected_venue"]
            try:
                year = int(str(choice.get("report_season") or ""))
            except ValueError:
                year = max((season_key("mlb", event) for event in choice["events"]), default=0)
            completed_now = _completed_count(choice["events"], now)
            payload = (
                read_mlb_team_report(
                    session,
                    selected,
                    year,
                    current_completed_count=completed_now,
                )
                if year
                else None
            )
    finally:
        dispose_ticket_engine(model.engine)

    if payload is None and selected:
        # A newly-completed game changes the ranking cohort even without another
        # price snapshot. Rebuild once at that boundary, then all later readers
        # use the persistent row again.
        try:
            refreshed = refresh_mlb_team_report(selected)
        except Exception:
            refreshed = False
        if refreshed:
            model = CreateModel()
            try:
                with model.getSession()() as session:
                    payload = read_mlb_team_report(
                        session,
                        selected,
                        year,
                        current_completed_count=completed_now,
                    )
            finally:
                dispose_ticket_engine(model.engine)

    if payload is None:
        # Safe fallback: correctness wins if derived maintenance ever falls behind.
        context = build_mlb_stadium_context(selected_venue, selected_team)
        return render_template("nfl_stadium.html", **context)

    def decorate(row: dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        value["detail_url"] = url_for(
            "nfl_stadium.mlb_section",
            team=choice["selected_team"],
            venue=selected,
            section=value["name"],
        )
        return value

    all_sections = [decorate(row) for row in payload.get("all_sections", [])]
    by_key = {row["section_key"]: row for row in all_sections}
    cheapest = [
        by_key[key]
        for key in payload.get("cheapest_section_keys", [])
        if key in by_key
    ]
    biggest_drops = [
        by_key[key]
        for key in payload.get("biggest_drop_keys", [])
        if key in by_key
    ]
    game_count = int(payload.get("game_count") or len(choice["events"]))
    completed = int(payload.get("completed_game_count") or 0)
    context = {
        **_mlb_page_config(),
        "stadiums": stadiums,
        "stadium_count": len(stadiums),
        "selected_venue": selected,
        "selected_team": choice["selected_team"],
        "selected_team_label": choice.get("selected_team_label") or choice["selected_team"],
        "report_season": choice.get("report_season", ""),
        "venue_options": choice.get("venue_options", []),
        "error": None,
        "game_count": game_count,
        "completed_game_count": completed,
        "upcoming_game_count": max(0, game_count - completed),
        "section_count": int(payload.get("section_count") or len(all_sections)),
        "analyzed_area_count": 0,
        "observation_count": 0,
        "drop_section_count": len(biggest_drops),
        "cheapest_sections": cheapest,
        "biggest_drops": biggest_drops,
        "all_sections": all_sections,
        "games": [],
    }
    return render_template("nfl_stadium.html", **context)