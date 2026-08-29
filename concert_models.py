"""Separate SQLAlchemy storage for concert ticket-price history."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from models import captured_datetime_for_storage, event_datetime_for_storage


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_CONCERT_DATABASE = PROJECT_DIR / "Concert-collection.db"
DEFAULT_CONCERT_AUDIT_DIR = PROJECT_DIR / "concert_audit"
DEFAULT_CONCERT_BACKUP_DIR = PROJECT_DIR / "concert_backups"
CONCERT_BACKUP_RETENTION_DAYS = 7
_LEGACY_MIGRATION_LOCK = threading.Lock()
_LEGACY_MIGRATION_ATTEMPTED = False


class ConcertBase(DeclarativeBase):
    pass


class ConcertEvent(ConcertBase):
    __tablename__ = "concert_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    event_date: Mapped[datetime] = mapped_column(DateTime(), nullable=False, index=True)
    sections: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON), default=list, nullable=False
    )
    source_url: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    venue: Mapped[str] = mapped_column(String, nullable=False, index=True)

    iterations: Mapped[list["ConcertIteration"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )


class ConcertIteration(ConcertBase):
    __tablename__ = "concert_iterations"
    __table_args__ = (
        UniqueConstraint(
            "event_id", "captured_at", name="uq_concert_event_capture_slot"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("concert_event.id"), nullable=False, index=True
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(),
        default=lambda: captured_datetime_for_storage(datetime.now(timezone.utc)),
        nullable=False,
        index=True,
    )

    event: Mapped[ConcertEvent] = relationship(back_populates="iterations")
    tickets: Mapped[list["ConcertTicket"]] = relationship(
        back_populates="iteration", cascade="all, delete-orphan"
    )


class ConcertTicket(ConcertBase):
    __tablename__ = "concert_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    section: Mapped[str] = mapped_column(String, nullable=False, index=True)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    listing_count: Mapped[int] = mapped_column(Integer, nullable=False)
    iteration_id: Mapped[int] = mapped_column(
        ForeignKey("concert_iterations.id"), nullable=False, index=True
    )

    iteration: Mapped[ConcertIteration] = relationship(back_populates="tickets")


def concert_database_path() -> Path:
    configured = os.environ.get("CONCERT_DATABASE_PATH", str(DEFAULT_CONCERT_DATABASE))
    return Path(configured).expanduser().resolve()


def _migrate_legacy_shared_storage_once() -> None:
    """Move shared-era concert rows lazily after the Flask app has loaded.

    Import-time migration made PythonAnywhere workers unavailable while a large
    SQLite backup was being copied. This version runs only when concert storage
    is first used, reads the server's .env before selecting database paths, and
    relies on copy-before-delete/idempotency rather than another startup backup.
    """
    global _LEGACY_MIGRATION_ATTEMPTED

    if _LEGACY_MIGRATION_ATTEMPTED:
        return
    with _LEGACY_MIGRATION_LOCK:
        if _LEGACY_MIGRATION_ATTEMPTED:
            return
        _LEGACY_MIGRATION_ATTEMPTED = True
        try:
            try:
                from dotenv import load_dotenv

                load_dotenv(PROJECT_DIR / ".env", override=True)
            except Exception:
                pass

            from legacy_concert_migration import migrate_legacy_concert_rows

            report = migrate_legacy_concert_rows(make_backups=False)
        except Exception as exc:
            _LEGACY_MIGRATION_ATTEMPTED = False
            print(
                f"Legacy concert migration warning: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return

        if report["events_migrated"]:
            print(
                "Migrated legacy concert data out of the baseball database: "
                f"{report['events_migrated']} events, "
                f"{report['iterations_migrated']} iterations, "
                f"{report['tickets_migrated']} ticket rows.",
                flush=True,
            )


class CreateConcertModel:
    """Open the concert-only SQLite database, creating its schema if needed."""

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            _migrate_legacy_shared_storage_once()
        self.db_path = Path(db_path or concert_database_path()).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            echo=False,
            connect_args={"timeout": 30},
        )
        ConcertBase.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )

    def getSession(self):
        return self.SessionLocal


def store_concert_snapshot(
    url: str,
    event_date: datetime,
    snapshot: Any,
    captured_at: datetime,
    *,
    db_path: str | Path | None = None,
) -> tuple[int, int, bool]:
    """Store one hourly concert snapshot and report whether it was newly inserted."""
    model = CreateConcertModel(db_path)
    stored_event_date = event_datetime_for_storage(event_date)
    stored_captured_at = captured_datetime_for_storage(captured_at)

    with model.getSession()() as session:
        event = (
            session.query(ConcertEvent)
            .filter(
                (ConcertEvent.source_url == url)
                | (ConcertEvent.source_id == snapshot.source_id)
            )
            .first()
        )
        if event is None:
            event = ConcertEvent(
                source_id=snapshot.source_id,
                title=snapshot.title,
                event_date=stored_event_date,
                sections=[row.section for row in snapshot.sections],
                source_url=url,
                venue=snapshot.venue,
            )
            session.add(event)
            session.flush()
        else:
            event.title = snapshot.title
            event.event_date = stored_event_date
            event.source_url = url
            event.source_id = snapshot.source_id
            event.venue = snapshot.venue
            known_sections = set(event.sections or [])
            event.sections = list(event.sections or []) + [
                row.section
                for row in snapshot.sections
                if row.section not in known_sections
            ]

        existing = (
            session.query(ConcertIteration)
            .filter(
                ConcertIteration.event_id == event.id,
                ConcertIteration.captured_at == stored_captured_at,
            )
            .first()
        )
        if existing is not None:
            session.commit()
            return event.id, existing.id, False

        iteration = ConcertIteration(
            event=event,
            captured_at=stored_captured_at,
        )
        session.add(iteration)
        session.add_all(
            ConcertTicket(
                section=row.section,
                price=row.price,
                listing_count=row.listing_count,
                iteration=iteration,
            )
            for row in snapshot.sections
        )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            event = (
                session.query(ConcertEvent)
                .filter(ConcertEvent.source_url == url)
                .first()
            )
            existing = (
                session.query(ConcertIteration)
                .filter(
                    ConcertIteration.event_id == event.id,
                    ConcertIteration.captured_at == stored_captured_at,
                )
                .one()
            )
            return event.id, existing.id, False
        return event.id, iteration.id, True


def create_concert_daily_backup(
    now: datetime | None = None,
    source: Path | None = None,
    backup_dir: Path = DEFAULT_CONCERT_BACKUP_DIR,
) -> Path:
    """Create one independent concert database backup per day."""
    now = now or datetime.now(timezone.utc)
    source = Path(source or concert_database_path()).expanduser().resolve()
    if not source.exists():
        CreateConcertModel(source)

    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"Concert-collection-{now:%Y-%m-%d}.db"
    if not target.exists():
        temporary = target.with_suffix(".db.tmp")
        temporary.unlink(missing_ok=True)
        with sqlite3.connect(source) as source_db, sqlite3.connect(temporary) as backup_db:
            source_db.backup(backup_db)
        temporary.replace(target)

    backups = sorted(backup_dir.glob("Concert-collection-*.db"), reverse=True)
    for expired in backups[CONCERT_BACKUP_RETENTION_DAYS:]:
        expired.unlink()
    return target


def write_concert_audit(
    url: str,
    event_date: datetime,
    snapshot: Any,
    event_id: int,
    iteration_id: int,
    captured_at: datetime,
    audit_dir: Path = DEFAULT_CONCERT_AUDIT_DIR,
) -> Path:
    """Write concert audit records separately from baseball audit records."""
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / f"{captured_at.astimezone(timezone.utc):%Y-%m-%d}.jsonl"
    record = {
        "schema_version": 1,
        "event_type": "concert",
        "captured_at": captured_at.isoformat(),
        "event_date": event_date.isoformat(),
        "event_id": event_id,
        "iteration_id": iteration_id,
        "source_id": snapshot.source_id,
        "title": snapshot.title,
        "venue": snapshot.venue,
        "url": url,
        "currency": "USD",
        "section_count": len(snapshot.sections),
        "sections": [asdict(row) for row in snapshot.sections],
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    cutoff = captured_at - timedelta(days=30)
    for candidate in audit_dir.glob("*.jsonl"):
        modified = datetime.fromtimestamp(candidate.stat().st_mtime, tz=timezone.utc)
        if modified < cutoff:
            candidate.unlink()
    return path
