"""Move concert snapshots written by the temporary shared pipeline.

PR #27 briefly sent concert snapshots to the baseball endpoint and database.
This idempotent migration copies every Vivid concert event into the dedicated
concert database, then removes it from the baseball database. It is safe to run
again after a partial failure because concert capture slots are unique.
"""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
from pathlib import Path
import re
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import selectinload, sessionmaker

from collector import (
    EventSnapshot,
    SectionSnapshot,
    create_daily_backup,
    database_path,
)
from concert_models import (
    concert_database_path,
    create_concert_daily_backup,
    store_concert_snapshot,
)
from models import Event, Iteration


CONCERT_URL_MARKER = "--concerts-"
PRODUCTION_ID_PATTERN = re.compile(r"/production/(\d+)")


def is_legacy_concert_url(url: str | None) -> bool:
    value = str(url or "").lower()
    return CONCERT_URL_MARKER in value and PRODUCTION_ID_PATTERN.search(value) is not None


def _source_id(url: str) -> str:
    match = PRODUCTION_ID_PATTERN.search(url)
    if match is None:
        raise ValueError(f"Concert URL is missing its production ID: {url}")
    return match.group(1)


def migrate_legacy_concert_rows(
    *,
    baseball_path: str | Path | None = None,
    concert_path: str | Path | None = None,
    lock_path: str | Path | None = None,
    make_backups: bool = True,
) -> dict[str, int]:
    """Copy legacy concert history to its own database and delete old rows.

    The concert database is committed first. Only after every iteration for an
    event is present there is the event deleted from the baseball database.
    A rerun therefore repairs interrupted work without duplicating snapshots.
    """
    baseball_db = Path(baseball_path or database_path()).expanduser().resolve()
    concert_db = Path(concert_path or concert_database_path()).expanduser().resolve()
    result = {
        "legacy_events_found": 0,
        "events_migrated": 0,
        "iterations_migrated": 0,
        "tickets_migrated": 0,
    }
    if not baseball_db.exists():
        return result

    migration_lock = Path(
        lock_path or baseball_db.with_suffix(baseball_db.suffix + ".concert-migration.lock")
    )
    migration_lock.parent.mkdir(parents=True, exist_ok=True)

    with migration_lock.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)

        engine = create_engine(
            f"sqlite:///{baseball_db}",
            echo=False,
            connect_args={"timeout": 30},
        )
        SessionLocal = sessionmaker(
            bind=engine,
            autoflush=False,
            expire_on_commit=False,
        )
        try:
            with SessionLocal() as session:
                legacy_count = (
                    session.query(Event)
                    .filter(Event.URL.like(f"%{CONCERT_URL_MARKER}%"))
                    .count()
                )
            result["legacy_events_found"] = legacy_count
            if legacy_count == 0:
                return result

            if make_backups:
                now = datetime.now(timezone.utc)
                create_daily_backup(now=now, source=baseball_db)
                create_concert_daily_backup(now=now, source=concert_db)

            with SessionLocal() as session:
                legacy_events = (
                    session.query(Event)
                    .options(
                        selectinload(Event.iterations).selectinload(Iteration.tickets)
                    )
                    .filter(Event.URL.like(f"%{CONCERT_URL_MARKER}%"))
                    .order_by(Event.id)
                    .all()
                )

                for event in legacy_events:
                    if not is_legacy_concert_url(event.URL):
                        continue

                    migrated_iterations = 0
                    migrated_tickets = 0
                    for iteration in sorted(
                        event.iterations,
                        key=lambda row: (row.captured_at, row.id),
                    ):
                        sections = tuple(
                            SectionSnapshot(
                                section=ticket.section,
                                price=ticket.price,
                                listing_count=max(
                                    1, int(ticket.ticketsPerSection or 1)
                                ),
                                row="",
                                quantity="",
                                displayed_price=str(ticket.price),
                                alternate_price="",
                                price_source="p",
                            )
                            for ticket in iteration.tickets
                        )
                        if not sections:
                            continue

                        snapshot = EventSnapshot(
                            source_id=_source_id(event.URL),
                            title=event.title,
                            venue=event.Place or "Unknown venue",
                            sections=sections,
                        )
                        _event_id, _iteration_id, stored = store_concert_snapshot(
                            event.URL,
                            event.event_date,
                            snapshot,
                            iteration.captured_at,
                            db_path=concert_db,
                        )
                        if stored:
                            migrated_iterations += 1
                            migrated_tickets += len(sections)

                    # Every available iteration is now present in concert
                    # storage. Deleting the parent cascades through the old
                    # baseball iterations and ticket rows.
                    session.delete(event)
                    session.commit()
                    result["events_migrated"] += 1
                    result["iterations_migrated"] += migrated_iterations
                    result["tickets_migrated"] += migrated_tickets
        finally:
            engine.dispose()
            try:
                fcntl.flock(lock, fcntl.LOCK_UN)
            except OSError:
                pass

    return result
