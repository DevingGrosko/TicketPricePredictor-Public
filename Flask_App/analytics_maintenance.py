"""Backfill and maintain materialized ticket analytics outside page requests."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from threading import Lock
from typing import Any, Callable

from sqlalchemy import delete, select
from sqlalchemy.orm import defer, load_only

from models import (
    CreateModel,
    Event,
    Iteration,
    Ticket,
    event_has_complete_public_data,
)
from Flask_App.database_config import dispose_ticket_engine
from Flask_App.materialized_analytics import (
    SECTION_BUCKET_SUMMARY,
    SECTION_SUMMARY_STATE,
    refresh_event_summary,
    stale_event_ids,
)
from Flask_App.performance_cache import invalidate_sport_cache
from Flask_App.report_policy import is_retired_mlb_home_event


MLB_URL_MARKER = "--sports-mlb-baseball/"
_BACKFILL_LOCKS = {sport: Lock() for sport in ("mlb", "nfl", "nhl")}


@dataclass(frozen=True)
class BackfillResult:
    sport: str
    processed: int
    remaining: int
    total_events: int
    complete: bool
    event_ids: tuple[int, ...]
    retired_events_removed: int = 0

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["event_ids"] = list(self.event_ids)
        return payload


def _remove_retired_mlb_history() -> int:
    """Delete only retired MLB home events and their dependent stored data.

    The cleanup is one transaction. Opposing-team home games remain because
    ``is_retired_mlb_home_event`` requires a Rays home identity rather than a
    generic Rays mention in the matchup title.
    """
    model = CreateModel()
    removed = 0
    try:
        with model.getSession()() as session:
            candidates = (
                session.query(Event)
                .options(
                    load_only(
                        Event.id,
                        Event.title,
                        Event.URL,
                        Event.Place,
                    )
                )
                .order_by(Event.id)
                .all()
            )
            retired_ids = sorted(
                int(event.id)
                for event in candidates
                if MLB_URL_MARKER in str(event.URL or "").casefold()
                and is_retired_mlb_home_event(event)
            )
            if not retired_ids:
                return 0

            iteration_ids = list(
                session.execute(
                    select(Iteration.__table__.c.id).where(
                        Iteration.__table__.c.event_id.in_(retired_ids)
                    )
                ).scalars()
            )

            # Materialized summaries do not have database foreign keys to raw
            # events, so remove them explicitly before deleting the source rows.
            session.execute(
                delete(SECTION_BUCKET_SUMMARY).where(
                    SECTION_BUCKET_SUMMARY.c.event_id.in_(retired_ids)
                )
            )
            session.execute(
                delete(SECTION_SUMMARY_STATE).where(
                    SECTION_SUMMARY_STATE.c.event_id.in_(retired_ids)
                )
            )
            if iteration_ids:
                session.execute(
                    delete(Ticket.__table__).where(
                        Ticket.__table__.c.iteration_id.in_(iteration_ids)
                    )
                )
            session.execute(
                delete(Iteration.__table__).where(
                    Iteration.__table__.c.event_id.in_(retired_ids)
                )
            )
            session.execute(
                delete(Event.__table__).where(Event.__table__.c.id.in_(retired_ids))
            )
            session.commit()
            removed = len(retired_ids)
    finally:
        dispose_ticket_engine(model.engine)

    if removed:
        invalidate_sport_cache("mlb")
    return removed


def _mlb_spec() -> tuple[Any, Any, Any, Any, list[Any], Callable[[Any], str]]:
    model = CreateModel()
    with model.getSession()() as session:
        events = (
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
            .order_by(Event.id)
            .all()
        )
    events = [
        event
        for event in events
        if event_has_complete_public_data(event)
        and MLB_URL_MARKER in str(event.URL or "").casefold()
        and str(event.Place or "").strip()
    ]
    return model, Event, Iteration, Ticket, events, lambda event: str(event.Place or "")


def _nfl_spec() -> tuple[Any, Any, Any, Any, list[Any], Callable[[Any], str]]:
    from Flask_App.nfl_blueprint import (
        CreateNFLModel,
        NFLEvent,
        NFLIteration,
        NFLTicket,
        _country_is_explicitly_non_us,
        nfl_display_venue,
    )

    model = CreateNFLModel()
    with model.getSession()() as session:
        events = (
            session.query(NFLEvent)
            .options(
                load_only(
                    NFLEvent.id,
                    NFLEvent.event_date,
                    NFLEvent.venue,
                    NFLEvent.provider_venue,
                    NFLEvent.canonical_venue,
                    NFLEvent.country,
                )
            )
            .order_by(NFLEvent.id)
            .all()
        )
    events = [
        event
        for event in events
        if not _country_is_explicitly_non_us(event.country)
        and str(nfl_display_venue(event) or "").strip()
    ]
    return (
        model,
        NFLEvent,
        NFLIteration,
        NFLTicket,
        events,
        nfl_display_venue,
    )


def _nhl_spec() -> tuple[Any, Any, Any, Any, list[Any], Callable[[Any], str]]:
    from Flask_App.nhl_blueprint import (
        CreateNHLModel,
        NHLEvent,
        NHLIteration,
        NHLTicket,
        _country_is_supported,
        nhl_display_venue,
    )

    model = CreateNHLModel()
    with model.getSession()() as session:
        events = (
            session.query(NHLEvent)
            .options(
                load_only(
                    NHLEvent.id,
                    NHLEvent.event_date,
                    NHLEvent.venue,
                    NHLEvent.provider_venue,
                    NHLEvent.canonical_venue,
                    NHLEvent.country,
                )
            )
            .order_by(NHLEvent.id)
            .all()
        )
    events = [
        event
        for event in events
        if _country_is_supported(event.country)
        and str(nhl_display_venue(event) or "").strip()
    ]
    return (
        model,
        NHLEvent,
        NHLIteration,
        NHLTicket,
        events,
        nhl_display_venue,
    )


def _sport_spec(
    sport: str,
) -> tuple[Any, Any, Any, Any, list[Any], Callable[[Any], str]]:
    normalized = str(sport or "").strip().casefold()
    if normalized == "mlb":
        return _mlb_spec()
    if normalized == "nfl":
        return _nfl_spec()
    if normalized == "nhl":
        return _nhl_spec()
    raise ValueError("sport must be one of: mlb, nfl, nhl")


def backfill_sport(sport: str, *, limit: int = 3) -> BackfillResult:
    """Refresh a small batch of stale event summaries.

    This function is intentionally batched so a production maintenance request
    remains bounded. Deploy and collector workflows can call it repeatedly
    until ``complete`` is true.
    """

    normalized = str(sport or "").strip().casefold()
    if normalized not in _BACKFILL_LOCKS:
        raise ValueError("sport must be one of: mlb, nfl, nhl")
    batch_limit = min(max(int(limit), 1), 20)
    lock = _BACKFILL_LOCKS[normalized]
    if not lock.acquire(blocking=False):
        raise RuntimeError(f"A {normalized.upper()} analytics backfill is already running.")

    model = None
    try:
        retired_events_removed = (
            _remove_retired_mlb_history() if normalized == "mlb" else 0
        )
        model, _event_model, iteration_model, ticket_model, events, venue_getter = (
            _sport_spec(normalized)
        )
        event_by_id = {int(event.id): event for event in events}
        with model.getSession()() as session:
            stale = stale_event_ids(
                session,
                events,
                sport_key=normalized,
                venue_getter=venue_getter,
                iteration_model=iteration_model,
            )
            selected_ids = stale[:batch_limit]
            processed: list[int] = []
            for event_id in selected_ids:
                event = event_by_id[event_id]
                refresh_event_summary(
                    session,
                    sport_key=normalized,
                    event_id=event_id,
                    event_date=event.event_date,
                    venue=venue_getter(event),
                    iteration_model=iteration_model,
                    ticket_model=ticket_model,
                    mark_complete=True,
                )
                session.commit()
                processed.append(event_id)

            remaining_ids = stale_event_ids(
                session,
                events,
                sport_key=normalized,
                venue_getter=venue_getter,
                iteration_model=iteration_model,
            )
        return BackfillResult(
            sport=normalized,
            processed=len(processed),
            remaining=len(remaining_ids),
            total_events=len(events),
            complete=not remaining_ids,
            event_ids=tuple(processed),
            retired_events_removed=retired_events_removed,
        )
    finally:
        if model is not None:
            dispose_ticket_engine(model.engine)
        lock.release()
