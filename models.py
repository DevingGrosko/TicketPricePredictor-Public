from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
from zoneinfo import ZoneInfo

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    selectinload,
    sessionmaker,
)


PROJECT_DIR = Path(__file__).resolve().parent
INCOMPLETE_PUBLIC_EVENT_DATES = frozenset(
    {
        date(2026, 7, 18),
        date(2026, 7, 19),
    }
)
NEW_YORK = ZoneInfo("America/New_York")

DEFAULT_CONCERT_DATABASE = PROJECT_DIR / "Concert-collection.db"
DEFAULT_CONCERT_AUDIT_DIR = PROJECT_DIR / "concert_audit"
DEFAULT_CONCERT_BACKUP_DIR = PROJECT_DIR / "concert_backups"
CONCERT_BACKUP_RETENTION_DAYS = 7
CONCERT_URL_MARKER = "--concerts-"
PRODUCTION_ID_PATTERN = re.compile(r"/production/(\d+)")

_LEGACY_MIGRATION_THREAD_LOCK = threading.Lock()
_LEGACY_MIGRATION_ATTEMPTED = False

_BASEBALL_INDEX_LOCK = threading.Lock()
_BASEBALL_INDEX_READY: set[str] = set()


def event_datetime_utc(value: datetime) -> datetime:
    """Interpret persisted event wall time as Eastern and return aware UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=NEW_YORK)
    return value.astimezone(timezone.utc)


def captured_datetime_utc(value: datetime) -> datetime:
    """Interpret persisted capture wall time as UTC and return aware UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def event_datetime_for_storage(value: datetime) -> datetime:
    """Store event times as naive Eastern wall time for SQLite compatibility."""
    if value.tzinfo is None:
        return value
    return value.astimezone(NEW_YORK).replace(tzinfo=None)


def captured_datetime_for_storage(value: datetime) -> datetime:
    """Store capture times as naive UTC wall time for SQLite compatibility."""
    return captured_datetime_utc(value).replace(tzinfo=None)


def event_datetime_eastern(value: datetime) -> datetime:
    """Return an aware Eastern event time for labels and calendar-date checks."""
    if value.tzinfo is None:
        return value.replace(tzinfo=NEW_YORK)
    return value.astimezone(NEW_YORK)


def hours_before_event(event_date: datetime, captured_at: datetime) -> float:
    """Calculate lead time without mixing Eastern and UTC wall clocks."""
    return (
        event_datetime_utc(event_date) - captured_datetime_utc(captured_at)
    ).total_seconds() / 3600


def clean_event_title(title: str) -> str:
    """Remove Vivid's trailing promotion label from a matchup name."""
    title = re.sub(r"\s*\([^)]*\)\s*$", "", title or "").strip()
    return re.sub(
        r"\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*\d{1,2}$",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip()


def event_has_complete_public_data(event: "Event") -> bool:
    """Keep known incomplete collection days out of public views and analysis."""
    return bool(
        event.event_date
        and event_datetime_eastern(event.event_date).date()
        not in INCOMPLETE_PUBLIC_EVENT_DATES
    )


# ---------------------------------------------------------------------------
# Baseball storage. This is intentionally unchanged and remains backed by
# Event-collection.db.
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "event"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(nullable=False)
    event_date: Mapped[datetime] = mapped_column(nullable=False)
    event_sections: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
    )
    URL: Mapped[str] = mapped_column(nullable=True)
    Place: Mapped[str] = mapped_column(nullable=True)

    iterations: Mapped[list["Iteration"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
    )


class Iteration(Base):
    __tablename__ = "iterations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(),
        default=lambda: captured_datetime_for_storage(datetime.now(timezone.utc)),
        nullable=False,
    )

    event: Mapped[Event] = relationship(back_populates="iterations")
    tickets: Mapped[list["Ticket"]] = relationship(
        back_populates="iteration",
        cascade="all, delete-orphan",
    )


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    section: Mapped[str] = mapped_column(String)
    price: Mapped[int] = mapped_column(Integer)
    # Temporary nullable=True because the earliest observations lack this field.
    ticketsPerSection: Mapped[int] = mapped_column(Integer, nullable=True)

    iteration_id: Mapped[int] = mapped_column(ForeignKey("iterations.id"))
    iteration: Mapped["Iteration"] = relationship(back_populates="tickets")


def database_path() -> Path:
    configured = os.environ.get("DATABASE_PATH", str(PROJECT_DIR / "Event-collection.db"))
    return Path(configured).expanduser().resolve()


def _ensure_baseball_performance_indexes(engine, db_path: Path) -> None:
    """Create the indexes used by venue and section history queries once."""

    key = str(db_path)
    with _BASEBALL_INDEX_LOCK:
        if key in _BASEBALL_INDEX_READY:
            return
        with engine.begin() as connection:
            tables = {
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if not {"event", "iterations", "tickets"}.issubset(tables):
                return
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_event_place ON event (Place)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_event_url ON event (URL)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_iterations_event_captured "
                "ON iterations (event_id, captured_at)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_tickets_iteration_id "
                "ON tickets (iteration_id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_tickets_section_iteration "
                "ON tickets (section, iteration_id)"
            )
            connection.exec_driver_sql("PRAGMA optimize")
        _BASEBALL_INDEX_READY.add(key)


class CreateModel:
    def __init__(self):
        db_path = database_path()
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            echo=False,
            connect_args={"timeout": 30},
        )
        if not db_path.exists():
            raise FileNotFoundError(f"Database file missing: {db_path}")
        _ensure_baseball_performance_indexes(self.engine, db_path)

        self.SessionLocal = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )

    def getSession(self):
        return self.SessionLocal


# ---------------------------------------------------------------------------
# Concert storage. These tables use a different DeclarativeBase and a different
# SQLite file, so concert history cannot enter the baseball database.
# ---------------------------------------------------------------------------


class ConcertBase(DeclarativeBase):
    pass


class ConcertEvent(ConcertBase):
    __tablename__ = "concert_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        unique=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    event_date: Mapped[datetime] = mapped_column(
        DateTime(),
        nullable=False,
        index=True,
    )
    sections: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
    )
    source_url: Mapped[str] = mapped_column(
        String,
        nullable=False,
        unique=True,
        index=True,
    )
    venue: Mapped[str] = mapped_column(String, nullable=False, index=True)

    iterations: Mapped[list["ConcertIteration"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
    )


class ConcertIteration(ConcertBase):
    __tablename__ = "concert_iterations"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "captured_at",
            name="uq_concert_event_capture_slot",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("concert_event.id"),
        nullable=False,
        index=True,
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(),
        default=lambda: captured_datetime_for_storage(datetime.now(timezone.utc)),
        nullable=False,
        index=True,
    )

    event: Mapped[ConcertEvent] = relationship(back_populates="iterations")
    tickets: Mapped[list["ConcertTicket"]] = relationship(
        back_populates="iteration",
        cascade="all, delete-orphan",
    )


class ConcertTicket(ConcertBase):
    __tablename__ = "concert_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    section: Mapped[str] = mapped_column(String, nullable=False, index=True)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    listing_count: Mapped[int] = mapped_column(Integer, nullable=False)
    iteration_id: Mapped[int] = mapped_column(
        ForeignKey("concert_iterations.id"),
        nullable=False,
        index=True,
    )

    iteration: Mapped[ConcertIteration] = relationship(back_populates="tickets")


def concert_database_path() -> Path:
    configured = os.environ.get(
        "CONCERT_DATABASE_PATH",
        str(DEFAULT_CONCERT_DATABASE),
    )
    return Path(configured).expanduser().resolve()


def _open_concert_database(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        echo=False,
        connect_args={"timeout": 30},
    )
    ConcertBase.metadata.create_all(engine)
    SessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    return engine, SessionLocal


def _load_runtime_environment() -> None:
    """Load server-owned paths before resolving either database file."""
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_DIR / ".env", override=True)
    except Exception:
        pass


def _concert_source_id(url: str) -> str:
    match = PRODUCTION_ID_PATTERN.search(url)
    if match is None:
        raise ValueError(f"Concert URL is missing its production ID: {url}")
    return match.group(1)


def is_legacy_concert_url(url: str | None) -> bool:
    value = str(url or "").lower()
    return bool(
        CONCERT_URL_MARKER in value
        and PRODUCTION_ID_PATTERN.search(value) is not None
    )


def migrate_legacy_concert_rows(
    *,
    baseball_path: str | Path | None = None,
    concert_path: str | Path | None = None,
    lock_path: str | Path | None = None,
) -> dict[str, int]:
    """Physically move shared-era concert rows out of Event-collection.db.

    Concert rows are committed to the independent concert database before the
    corresponding baseball-database parent is deleted. The unique hourly slot
    makes the operation idempotent after an interruption.
    """
    baseball_db = Path(baseball_path or database_path()).expanduser().resolve()
    concert_db = Path(concert_path or concert_database_path()).expanduser().resolve()
    report = {
        "legacy_events_found": 0,
        "events_migrated": 0,
        "iterations_migrated": 0,
        "tickets_migrated": 0,
    }
    if not baseball_db.exists():
        return report

    migration_lock = Path(
        lock_path
        or baseball_db.with_suffix(baseball_db.suffix + ".concert-migration.lock")
    )
    migration_lock.parent.mkdir(parents=True, exist_ok=True)

    with migration_lock.open("w", encoding="utf-8") as lock_handle:
        fcntl_module = None
        try:
            import fcntl as fcntl_module

            fcntl_module.flock(lock_handle, fcntl_module.LOCK_EX)
        except ImportError:
            pass

        baseball_engine = create_engine(
            f"sqlite:///{baseball_db}",
            echo=False,
            connect_args={"timeout": 30},
        )
        BaseballSession = sessionmaker(
            bind=baseball_engine,
            autoflush=False,
            expire_on_commit=False,
        )
        concert_engine, ConcertSession = _open_concert_database(concert_db)

        try:
            with BaseballSession() as baseball_session:
                legacy_events = (
                    baseball_session.query(Event)
                    .options(
                        selectinload(Event.iterations).selectinload(Iteration.tickets)
                    )
                    .filter(Event.URL.like(f"%{CONCERT_URL_MARKER}%"))
                    .order_by(Event.id)
                    .all()
                )
                report["legacy_events_found"] = len(legacy_events)

                for legacy in legacy_events:
                    if not is_legacy_concert_url(legacy.URL):
                        continue

                    with ConcertSession() as concert_session:
                        source_id = _concert_source_id(legacy.URL)
                        concert = (
                            concert_session.query(ConcertEvent)
                            .filter(
                                (ConcertEvent.source_url == legacy.URL)
                                | (ConcertEvent.source_id == source_id)
                            )
                            .first()
                        )
                        if concert is None:
                            concert = ConcertEvent(
                                source_id=source_id,
                                title=legacy.title,
                                event_date=legacy.event_date,
                                sections=list(legacy.event_sections or []),
                                source_url=legacy.URL,
                                venue=legacy.Place or "Unknown venue",
                            )
                            concert_session.add(concert)
                            concert_session.flush()
                        else:
                            concert.title = legacy.title
                            concert.event_date = legacy.event_date
                            concert.source_url = legacy.URL
                            concert.venue = legacy.Place or concert.venue

                        known_sections = set(concert.sections or [])
                        new_sections = list(concert.sections or [])
                        inserted_iterations = 0
                        inserted_tickets = 0

                        for old_iteration in sorted(
                            legacy.iterations,
                            key=lambda row: (row.captured_at, row.id),
                        ):
                            existing = (
                                concert_session.query(ConcertIteration)
                                .filter(
                                    ConcertIteration.event_id == concert.id,
                                    ConcertIteration.captured_at
                                    == old_iteration.captured_at,
                                )
                                .first()
                            )
                            if existing is not None:
                                continue

                            new_iteration = ConcertIteration(
                                event=concert,
                                captured_at=old_iteration.captured_at,
                            )
                            concert_session.add(new_iteration)
                            for old_ticket in old_iteration.tickets:
                                if old_ticket.section not in known_sections:
                                    known_sections.add(old_ticket.section)
                                    new_sections.append(old_ticket.section)
                                concert_session.add(
                                    ConcertTicket(
                                        section=old_ticket.section,
                                        price=old_ticket.price,
                                        listing_count=max(
                                            1,
                                            int(old_ticket.ticketsPerSection or 1),
                                        ),
                                        iteration=new_iteration,
                                    )
                                )
                                inserted_tickets += 1
                            inserted_iterations += 1

                        concert.sections = new_sections
                        concert_session.commit()

                    # Copy-before-delete: old rows are removed only after the
                    # independent concert transaction has committed.
                    baseball_session.delete(legacy)
                    baseball_session.commit()
                    report["events_migrated"] += 1
                    report["iterations_migrated"] += inserted_iterations
                    report["tickets_migrated"] += inserted_tickets
        finally:
            baseball_engine.dispose()
            concert_engine.dispose()
            if fcntl_module is not None:
                try:
                    fcntl_module.flock(lock_handle, fcntl_module.LOCK_UN)
                except OSError:
                    pass

    return report


def _migrate_legacy_shared_storage_once() -> None:
    global _LEGACY_MIGRATION_ATTEMPTED

    if _LEGACY_MIGRATION_ATTEMPTED:
        return
    with _LEGACY_MIGRATION_THREAD_LOCK:
        if _LEGACY_MIGRATION_ATTEMPTED:
            return
        _LEGACY_MIGRATION_ATTEMPTED = True
        _load_runtime_environment()
        try:
            report = migrate_legacy_concert_rows()
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
    """Open only Concert-collection.db and initialize its schema."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        migrate_legacy: bool = True,
    ):
        if db_path is None and migrate_legacy:
            _migrate_legacy_shared_storage_once()
        self.db_path = Path(db_path or concert_database_path()).expanduser().resolve()
        self.engine, self.SessionLocal = _open_concert_database(self.db_path)

    def getSession(self):
        return self.SessionLocal


def store_concert_snapshot(
    url: str,
    event_date: datetime,
    snapshot,
    captured_at: datetime,
    *,
    db_path: str | Path | None = None,
) -> tuple[int, int, bool]:
    """Store one hourly concert snapshot in the independent concert database."""
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
                .one()
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
        CreateConcertModel(source, migrate_legacy=False)

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
    snapshot,
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
