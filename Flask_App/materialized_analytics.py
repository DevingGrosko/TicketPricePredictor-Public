"""Incremental materialized ticket analytics shared by MLB, NFL, and NHL.

Raw snapshots remain the source of truth. This module stores one canonical,
game-scoped median per event, seating area, and fixed time window. Web requests
can then read compact rows instead of sorting the entire ticket history.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import logging
from statistics import median
from threading import RLock
from typing import Any, Callable, Iterable

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    delete,
    func,
    insert,
    or_,
    select,
    update,
)

from models import captured_datetime_utc, event_datetime_utc
from Flask_App.section_canonicalization import section_identity


SUMMARY_SCHEMA_VERSION = 1


LOGGER = logging.getLogger(__name__)

TIMELINE_BUCKETS: dict[str, tuple[tuple[float, float, str, str], ...]] = {
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

_METADATA = MetaData()

SECTION_BUCKET_SUMMARY = Table(
    "section_bucket_summary",
    _METADATA,
    Column("event_id", Integer, primary_key=True),
    Column("section_key", String(600), primary_key=True),
    Column("bucket_slot", Integer, primary_key=True),
    Column("section_name", String(300), nullable=False),
    Column("median_price", Float, nullable=False),
    Column("observation_count", Integer, nullable=False),
    Column("first_captured_at", DateTime(), nullable=False),
    Column("last_captured_at", DateTime(), nullable=False),
    Column("refreshed_at", DateTime(), nullable=False),
    Index("ix_section_bucket_summary_event", "event_id"),
    Index("ix_section_bucket_summary_key_event", "section_key", "event_id"),
)

SECTION_SUMMARY_STATE = Table(
    "section_summary_state",
    _METADATA,
    Column("event_id", Integer, primary_key=True),
    Column("summary_version", Integer, nullable=False),
    Column("event_signature", String(64), nullable=False),
    Column("source_iteration_id", Integer, nullable=True),
    Column("source_iteration_count", Integer, nullable=False),
    Column("complete", Boolean, nullable=False),
    Column("refreshed_at", DateTime(), nullable=False),
)

DIRTY_VENUE = Table(
    "analytics_dirty_venue",
    _METADATA,
    Column("venue", String(300), primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("dirty", Boolean, nullable=False),
    Column("updated_at", DateTime(), nullable=False),
    Index("ix_analytics_dirty_venue_dirty", "dirty", "updated_at"),
)

_SCHEMA_LOCK = RLock()
_SCHEMA_READY: set[str] = set()


@dataclass(frozen=True)
class SummaryRefreshResult:
    event_id: int
    bucket_count: int
    row_count: int
    source_iteration_count: int
    complete: bool
    venue_revision: int


def _bind_key(bind: Any) -> str:
    engine = getattr(bind, "engine", bind)
    return str(getattr(engine, "url", engine))


def ensure_summary_schema(bind: Any) -> None:
    """Create the compact analytics tables once for a database engine."""

    key = _bind_key(bind)
    with _SCHEMA_LOCK:
        if key in _SCHEMA_READY:
            return
        _METADATA.create_all(bind)
        _SCHEMA_READY.add(key)


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def event_signature(sport_key: str, event_date: datetime, venue: str) -> str:
    payload = "|".join(
        (
            str(SUMMARY_SCHEMA_VERSION),
            _clean(sport_key).casefold(),
            event_datetime_utc(event_date).isoformat(),
            _clean(venue).casefold(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def timeline_bucket_slot(
    sport_key: str,
    event_date: datetime,
    captured_at: datetime,
) -> int | None:
    hours = (
        event_datetime_utc(event_date) - captured_datetime_utc(captured_at)
    ).total_seconds() / 3600
    for slot, (lower, upper, _short, _label) in enumerate(
        TIMELINE_BUCKETS[sport_key]
    ):
        if lower == 0:
            if 0 < hours <= upper:
                return slot
        elif lower < hours <= upper:
            return slot
    return None


def _row_value(row: Any, name: str) -> Any:
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return mapping[name]
    if isinstance(row, dict):
        return row[name]
    return getattr(row, name)


def _display_label_score(label: str) -> tuple[int, int, str]:
    letters = "".join(character for character in label if character.isalpha())
    mixed_case = bool(letters) and not (letters.islower() or letters.isupper())
    return int(mixed_case), len(label), label.casefold()


def _preferred_label(candidates: Iterable[tuple[datetime, str]]) -> str:
    rows = [
        (captured, _clean(label))
        for captured, label in candidates
        if _clean(label)
    ]
    if not rows:
        return "Unknown section"
    latest = max(captured for captured, _label in rows)
    latest_labels = [label for captured, label in rows if captured == latest]
    return max(latest_labels, key=_display_label_score)


def _source_iteration_state(
    session: Any,
    iteration_model: Any,
    event_id: int,
) -> tuple[int | None, int]:
    table = iteration_model.__table__
    row = session.execute(
        select(
            func.max(table.c.id).label("last_id"),
            func.count(table.c.id).label("row_count"),
        ).where(table.c.event_id == int(event_id))
    ).one()
    return (
        int(row.last_id) if row.last_id is not None else None,
        int(row.row_count or 0),
    )


def _mark_venue_dirty(session: Any, venue: str) -> int:
    normalized = _clean(venue)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    existing = session.execute(
        select(DIRTY_VENUE.c.revision).where(DIRTY_VENUE.c.venue == normalized)
    ).scalar_one_or_none()
    if existing is None:
        revision = 1
        session.execute(
            insert(DIRTY_VENUE).values(
                venue=normalized,
                revision=revision,
                dirty=True,
                updated_at=now,
            )
        )
    else:
        revision = int(existing) + 1
        session.execute(
            update(DIRTY_VENUE)
            .where(DIRTY_VENUE.c.venue == normalized)
            .values(revision=revision, dirty=True, updated_at=now)
        )
    return revision


def refresh_event_summary(
    session: Any,
    *,
    sport_key: str,
    event_id: int,
    event_date: datetime,
    venue: str,
    iteration_model: Any,
    ticket_model: Any,
    bucket_slots: Iterable[int] | None = None,
    mark_complete: bool | None = None,
) -> SummaryRefreshResult:
    """Rebuild one event or selected event buckets from raw observations.

    Equivalent labels are canonicalized per venue. If aliases appear in the
    same capture, only the cheapest alias price contributes, preventing one
    physical area from being double-weighted.
    """

    ensure_summary_schema(session.connection())
    event_id = int(event_id)
    selected_slots = (
        {int(slot) for slot in bucket_slots}
        if bucket_slots is not None
        else set(range(len(TIMELINE_BUCKETS[sport_key])))
    )
    selected_slots = {
        slot
        for slot in selected_slots
        if 0 <= slot < len(TIMELINE_BUCKETS[sport_key])
    }
    if not selected_slots:
        _last_id, count = _source_iteration_state(session, iteration_model, event_id)
        revision = _mark_venue_dirty(session, venue)
        return SummaryRefreshResult(event_id, 0, 0, count, False, revision)

    signature = event_signature(sport_key, event_date, venue)
    last_iteration_id, source_iteration_count = _source_iteration_state(
        session, iteration_model, event_id
    )
    existing_state = session.execute(
        select(SECTION_SUMMARY_STATE).where(
            SECTION_SUMMARY_STATE.c.event_id == event_id
        )
    ).mappings().one_or_none()

    # An incremental refresh is safe only when exactly one new raw iteration
    # follows a complete summary for the same event metadata. Schedule/venue
    # changes, missed maintenance, or compaction require a full rebuild.
    if bucket_slots is not None:
        incremental_is_safe = bool(
            existing_state
            and existing_state["complete"]
            and int(existing_state["summary_version"]) == SUMMARY_SCHEMA_VERSION
            and existing_state["event_signature"] == signature
            and source_iteration_count
            == int(existing_state["source_iteration_count"]) + 1
        )
        if not incremental_is_safe:
            selected_slots = set(range(len(TIMELINE_BUCKETS[sport_key])))

    iteration = iteration_model.__table__
    ticket = ticket_model.__table__
    captured_at_column = iteration.c.get("captured_at")
    if captured_at_column is None:
        captured_at_column = iteration.c.get("created_at")
    section_column = ticket.c.get("section")
    if section_column is None:
        section_column = ticket.c.get("section_name")
    if captured_at_column is None or section_column is None:
        raise RuntimeError("Snapshot tables are missing captured_at or section columns.")

    event_utc = event_datetime_utc(event_date).replace(tzinfo=None)
    maximum_lead_hours = max(
        upper for _lower, upper, _short, _label in TIMELINE_BUCKETS[sport_key]
    )
    statement = (
        select(
            iteration.c.id.label("iteration_id"),
            captured_at_column.label("captured_at"),
            section_column.label("section"),
            ticket.c.price.label("price"),
        )
        .select_from(ticket.join(iteration, ticket.c.iteration_id == iteration.c.id))
        .where(
            iteration.c.event_id == event_id,
            ticket.c.price > 0,
            # Old databases can contain observations from before the current
            # analysis horizon. They can never enter a displayed time bucket,
            # so excluding them in SQL avoids transferring and normalizing a
            # potentially very large amount of irrelevant history.
            captured_at_column >= event_utc - timedelta(hours=maximum_lead_hours),
            captured_at_column < event_utc,
        )
    )

    if len(selected_slots) < len(TIMELINE_BUCKETS[sport_key]):
        conditions = []
        for slot in selected_slots:
            lower, upper, _short, _label = TIMELINE_BUCKETS[sport_key][slot]
            conditions.append(
                and_(
                    captured_at_column >= event_utc - timedelta(hours=upper),
                    captured_at_column < event_utc - timedelta(hours=lower),
                )
            )
        statement = statement.where(or_(*conditions))

    per_capture: dict[
        tuple[int, str, int], tuple[float, str, datetime]
    ] = {}
    identity_cache: dict[str, Any] = {}
    result = session.execute(statement.execution_options(stream_results=True))
    for row in result.yield_per(10_000):
        captured = _row_value(row, "captured_at")
        if captured is None:
            continue
        slot = timeline_bucket_slot(sport_key, event_date, captured)
        if slot is None or slot not in selected_slots:
            continue
        raw_section = _clean(_row_value(row, "section"))
        if raw_section not in identity_cache:
            identity_cache[raw_section] = section_identity(
                sport_key, venue, raw_section
            )
        identity = identity_cache[raw_section]
        if identity is None:
            continue
        try:
            price = float(_row_value(row, "price"))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        normalized_capture = captured_datetime_utc(captured).replace(tzinfo=None)
        key = (slot, identity.key, int(_row_value(row, "iteration_id")))
        current = per_capture.get(key)
        candidate = (price, identity.raw_label, normalized_capture)
        if current is None or price < current[0]:
            per_capture[key] = candidate
        elif (
            price == current[0]
            and _display_label_score(identity.raw_label)
            > _display_label_score(current[1])
        ):
            per_capture[key] = candidate

    grouped_values: dict[tuple[int, str], list[float]] = defaultdict(list)
    grouped_labels: dict[
        tuple[int, str], list[tuple[datetime, str]]
    ] = defaultdict(list)
    grouped_times: dict[tuple[int, str], list[datetime]] = defaultdict(list)
    for (slot, section_key, _iteration_id), (
        price,
        label,
        captured,
    ) in per_capture.items():
        group = (slot, section_key)
        grouped_values[group].append(price)
        grouped_labels[group].append((captured, label))
        grouped_times[group].append(captured)

    session.execute(
        delete(SECTION_BUCKET_SUMMARY).where(
            SECTION_BUCKET_SUMMARY.c.event_id == event_id,
            SECTION_BUCKET_SUMMARY.c.bucket_slot.in_(sorted(selected_slots)),
        )
    )

    refreshed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    rows_to_insert = []
    for (slot, section_key), values in grouped_values.items():
        times = grouped_times[(slot, section_key)]
        rows_to_insert.append(
            {
                "event_id": event_id,
                "section_key": section_key,
                "bucket_slot": slot,
                "section_name": _preferred_label(
                    grouped_labels[(slot, section_key)]
                ),
                "median_price": float(median(values)),
                "observation_count": len(values),
                "first_captured_at": min(times),
                "last_captured_at": max(times),
                "refreshed_at": refreshed_at,
            }
        )
    if rows_to_insert:
        session.execute(insert(SECTION_BUCKET_SUMMARY), rows_to_insert)

    full_refresh = len(selected_slots) == len(TIMELINE_BUCKETS[sport_key])
    if mark_complete is None:
        if full_refresh or source_iteration_count <= 1:
            complete = True
        else:
            complete = bool(
                existing_state
                and existing_state["complete"]
                and int(existing_state["summary_version"])
                == SUMMARY_SCHEMA_VERSION
                and existing_state["event_signature"] == signature
            )
    else:
        complete = bool(mark_complete)

    session.execute(
        delete(SECTION_SUMMARY_STATE).where(
            SECTION_SUMMARY_STATE.c.event_id == event_id
        )
    )
    session.execute(
        insert(SECTION_SUMMARY_STATE).values(
            event_id=event_id,
            summary_version=SUMMARY_SCHEMA_VERSION,
            event_signature=signature,
            source_iteration_id=last_iteration_id,
            source_iteration_count=source_iteration_count,
            complete=complete,
            refreshed_at=refreshed_at,
        )
    )
    revision = _mark_venue_dirty(session, venue)
    return SummaryRefreshResult(
        event_id=event_id,
        bucket_count=len(selected_slots),
        row_count=len(rows_to_insert),
        source_iteration_count=source_iteration_count,
        complete=complete,
        venue_revision=revision,
    )


def refresh_event_summary_safely(
    session_factory: Callable[[], Any],
    **kwargs: Any,
) -> SummaryRefreshResult | None:
    """Refresh one event in a separate transaction without risking raw data.

    Snapshot storage commits first. If this derived-data refresh fails, the
    previous materialized rows remain intact and the maintenance backfill will
    detect the stale event later.
    """

    try:
        with session_factory() as session:
            result = refresh_event_summary(session, **kwargs)
            session.commit()
            return result
    except Exception:
        LOGGER.exception(
            "Deferred materialized analytics refresh for %s event %s",
            kwargs.get("sport_key", "unknown"),
            kwargs.get("event_id", "unknown"),
        )
        return None


def read_summary_rows(
    session: Any,
    event_ids: Iterable[int],
    *,
    section_keys: Iterable[str] | None = None,
) -> list[Any]:
    ensure_summary_schema(session.connection())
    ids = sorted({int(event_id) for event_id in event_ids})
    if not ids:
        return []
    statement = select(
        SECTION_BUCKET_SUMMARY.c.event_id,
        SECTION_BUCKET_SUMMARY.c.section_key,
        SECTION_BUCKET_SUMMARY.c.section_name.label("section"),
        SECTION_BUCKET_SUMMARY.c.bucket_slot.label("slot"),
        SECTION_BUCKET_SUMMARY.c.median_price.label("price"),
        SECTION_BUCKET_SUMMARY.c.observation_count,
        SECTION_BUCKET_SUMMARY.c.first_captured_at,
        SECTION_BUCKET_SUMMARY.c.last_captured_at,
    ).where(SECTION_BUCKET_SUMMARY.c.event_id.in_(ids))
    keys = sorted({_clean(key) for key in section_keys or [] if _clean(key)})
    if keys:
        statement = statement.where(SECTION_BUCKET_SUMMARY.c.section_key.in_(keys))
    return list(
        session.execute(
            statement.order_by(
                SECTION_BUCKET_SUMMARY.c.event_id,
                SECTION_BUCKET_SUMMARY.c.section_key,
                SECTION_BUCKET_SUMMARY.c.bucket_slot,
            )
        ).all()
    )


def stale_event_ids(
    session: Any,
    events: Iterable[Any],
    *,
    sport_key: str,
    venue_getter: Callable[[Any], str],
    iteration_model: Any,
) -> list[int]:
    """Return events whose compact summary is missing or behind raw data."""

    ensure_summary_schema(session.connection())
    event_rows = list(events)
    if not event_rows:
        return []
    ids = [int(event.id) for event in event_rows]
    iteration = iteration_model.__table__
    source_rows = session.execute(
        select(
            iteration.c.event_id,
            func.max(iteration.c.id).label("last_id"),
            func.count(iteration.c.id).label("row_count"),
        )
        .where(iteration.c.event_id.in_(ids))
        .group_by(iteration.c.event_id)
    ).all()
    source = {
        int(row.event_id): (
            int(row.last_id) if row.last_id is not None else None,
            int(row.row_count or 0),
        )
        for row in source_rows
    }
    states = {
        int(row["event_id"]): row
        for row in session.execute(
            select(SECTION_SUMMARY_STATE).where(
                SECTION_SUMMARY_STATE.c.event_id.in_(ids)
            )
        ).mappings()
    }

    stale = []
    for event in event_rows:
        event_id = int(event.id)
        last_id, count = source.get(event_id, (None, 0))
        state = states.get(event_id)
        signature = event_signature(
            sport_key, event.event_date, venue_getter(event)
        )
        if (
            state is None
            or not state["complete"]
            or int(state["summary_version"]) != SUMMARY_SCHEMA_VERSION
            or state["event_signature"] != signature
            or state["source_iteration_id"] != last_id
            or int(state["source_iteration_count"]) != count
        ):
            stale.append(event_id)
    return stale


def dirty_venues(session: Any, limit: int = 10) -> list[tuple[str, int]]:
    ensure_summary_schema(session.connection())
    rows = session.execute(
        select(DIRTY_VENUE.c.venue, DIRTY_VENUE.c.revision)
        .where(DIRTY_VENUE.c.dirty.is_(True))
        .order_by(DIRTY_VENUE.c.updated_at, DIRTY_VENUE.c.venue)
        .limit(max(int(limit), 1))
    ).all()
    return [(str(row.venue), int(row.revision)) for row in rows]


def dirty_venue_count(session: Any) -> int:
    ensure_summary_schema(session.connection())
    return int(
        session.execute(
            select(func.count())
            .select_from(DIRTY_VENUE)
            .where(DIRTY_VENUE.c.dirty.is_(True))
        ).scalar_one()
    )


def venue_revision(session: Any, venue: str) -> int:
    ensure_summary_schema(session.connection())
    value = session.execute(
        select(DIRTY_VENUE.c.revision).where(
            DIRTY_VENUE.c.venue == _clean(venue)
        )
    ).scalar_one_or_none()
    return int(value or 0)


def mark_venue_clean(session: Any, venue: str, revision: int) -> bool:
    ensure_summary_schema(session.connection())
    result = session.execute(
        update(DIRTY_VENUE)
        .where(
            DIRTY_VENUE.c.venue == _clean(venue),
            DIRTY_VENUE.c.revision == int(revision),
        )
        .values(dirty=False)
    )
    return bool(result.rowcount)
