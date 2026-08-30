"""NFL-specific storage, API routes, and website views.

NFL history is intentionally isolated from both the existing baseball database
and the archived concert database. Games are accepted during the final 30 days
before kickoff, with the collector choosing a 6-hour, 3-hour, or hourly cadence.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any

from flask import Blueprint, jsonify, render_template, request, url_for
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from collector import snapshot_from_payload
from graph_builder import GraphBuilder
from models import (
    captured_datetime_for_storage,
    event_datetime_eastern,
    event_datetime_for_storage,
    hours_before_event,
)
from nfl_metadata import (
    EASTERN,
    canonical_venue_name,
    eastern_iso,
    eastern_label,
    geometry_is_usable,
    geometry_section_count,
    sanitize_map_geometry,
)


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_NFL_DATABASE = PROJECT_DIR / "NFL-collection.db"
DEFAULT_NFL_AUDIT_DIR = PROJECT_DIR / "nfl_audit"
DEFAULT_NFL_BACKUP_DIR = PROJECT_DIR / "nfl_backups"
NFL_BACKUP_RETENTION_DAYS = 7
NFL_AUDIT_RETENTION_DAYS = 30
NFL_CAPTURE_WINDOW_HOURS = 30 * 24
MAX_SNAPSHOT_REPLAY_AGE = timedelta(days=7)
MAX_SNAPSHOT_CLOCK_SKEW = timedelta(minutes=5)
NFL_URL_PATTERN = re.compile(r"/production/(\d+)$", flags=re.IGNORECASE)

NFL_TEAM_NAMES = frozenset(
    {
        "Arizona Cardinals",
        "Atlanta Falcons",
        "Baltimore Ravens",
        "Buffalo Bills",
        "Carolina Panthers",
        "Chicago Bears",
        "Cincinnati Bengals",
        "Cleveland Browns",
        "Dallas Cowboys",
        "Denver Broncos",
        "Detroit Lions",
        "Green Bay Packers",
        "Houston Texans",
        "Indianapolis Colts",
        "Jacksonville Jaguars",
        "Kansas City Chiefs",
        "Las Vegas Raiders",
        "Los Angeles Chargers",
        "Los Angeles Rams",
        "Miami Dolphins",
        "Minnesota Vikings",
        "New England Patriots",
        "New Orleans Saints",
        "New York Giants",
        "New York Jets",
        "Philadelphia Eagles",
        "Pittsburgh Steelers",
        "San Francisco 49ers",
        "Seattle Seahawks",
        "Tampa Bay Buccaneers",
        "Tennessee Titans",
        "Washington Commanders",
    }
)

NON_GAME_MARKERS = (
    "parking",
    "tailgate",
    "season ticket",
    "season tickets",
    "training camp",
    "fan fest",
    "fan experience",
    "hospitality",
    "ticket package",
    "travel package",
    "vip package",
    "club access",
    "shuttle",
)

nfl_blueprint = Blueprint("nfl", __name__)
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY: set[str] = set()


class NFLBase(DeclarativeBase):
    pass


class NFLEvent(NFLBase):
    __tablename__ = "nfl_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    event_date: Mapped[datetime] = mapped_column(DateTime(), nullable=False, index=True)
    sections: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON), default=list, nullable=False
    )
    source_url: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    # `venue` is retained as the original provider field for legacy bookmarks.
    venue: Mapped[str] = mapped_column(String, nullable=False, index=True)
    schedule_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    away_team: Mapped[str | None] = mapped_column(String, nullable=True)
    home_team: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    canonical_venue: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    city: Mapped[str | None] = mapped_column(String, nullable=True)
    country: Mapped[str | None] = mapped_column(String, nullable=True)
    neutral_site: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    provider_venue: Mapped[str | None] = mapped_column(String, nullable=True)
    map_geometry: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    map_source: Mapped[str | None] = mapped_column(String, nullable=True)
    geometry_updated_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)

    iterations: Mapped[list["NFLIteration"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )


class NFLIteration(NFLBase):
    __tablename__ = "nfl_iterations"
    __table_args__ = (
        UniqueConstraint("event_id", "captured_at", name="uq_nfl_event_capture_slot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("nfl_event.id"), nullable=False, index=True
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, index=True)

    event: Mapped[NFLEvent] = relationship(back_populates="iterations")
    tickets: Mapped[list["NFLTicket"]] = relationship(
        back_populates="iteration", cascade="all, delete-orphan"
    )


class NFLTicket(NFLBase):
    __tablename__ = "nfl_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    section: Mapped[str] = mapped_column(String, nullable=False, index=True)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    listing_count: Mapped[int] = mapped_column(Integer, nullable=False)
    iteration_id: Mapped[int] = mapped_column(
        ForeignKey("nfl_iterations.id"), nullable=False, index=True
    )

    iteration: Mapped[NFLIteration] = relationship(back_populates="tickets")


def nfl_database_path() -> Path:
    configured = os.environ.get("NFL_DATABASE_PATH", str(DEFAULT_NFL_DATABASE))
    return Path(configured).expanduser().resolve()


def _ensure_nfl_schema(engine: Any, db_path: Path) -> None:
    """Add nullable NFL metadata columns to an existing isolated SQLite DB."""
    key = str(db_path)
    with _SCHEMA_LOCK:
        if key in _SCHEMA_READY:
            return
        with engine.begin() as connection:
            current = {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(nfl_event)"
                ).fetchall()
            }
            additions = {
                "schedule_id": "VARCHAR",
                "away_team": "VARCHAR",
                "home_team": "VARCHAR",
                "canonical_venue": "VARCHAR",
                "city": "VARCHAR",
                "country": "VARCHAR",
                "neutral_site": "BOOLEAN",
                "provider_venue": "VARCHAR",
                "map_geometry": "JSON",
                "map_source": "VARCHAR",
                "geometry_updated_at": "DATETIME",
            }
            for column, sql_type in additions.items():
                if column not in current:
                    connection.exec_driver_sql(
                        f"ALTER TABLE nfl_event ADD COLUMN {column} {sql_type}"
                    )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_nfl_event_schedule_id "
                "ON nfl_event (schedule_id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_nfl_event_home_team "
                "ON nfl_event (home_team)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_nfl_event_canonical_venue "
                "ON nfl_event (canonical_venue)"
            )

            rows = connection.execute(
                text(
                    "SELECT id, title, venue, provider_venue, canonical_venue, "
                    "away_team, home_team FROM nfl_event"
                )
            ).mappings()
            for row in rows:
                matchup = nfl_matchup_teams(row["title"])
                provider_venue = row["provider_venue"] or row["venue"]
                canonical_venue = row["canonical_venue"] or canonical_venue_name(
                    provider_venue
                )
                connection.execute(
                    text(
                        "UPDATE nfl_event SET provider_venue = :provider_venue, "
                        "canonical_venue = :canonical_venue, "
                        "away_team = COALESCE(away_team, :away_team), "
                        "home_team = COALESCE(home_team, :home_team) "
                        "WHERE id = :event_id"
                    ),
                    {
                        "event_id": row["id"],
                        "provider_venue": provider_venue,
                        "canonical_venue": canonical_venue,
                        "away_team": matchup[0] if matchup else None,
                        "home_team": matchup[1] if matchup else None,
                    },
                )
        _SCHEMA_READY.add(key)


class CreateNFLModel:
    """Open only the independent NFL SQLite database."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or nfl_database_path()).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            echo=False,
            connect_args={"timeout": 30},
        )
        NFLBase.metadata.create_all(self.engine)
        _ensure_nfl_schema(self.engine, self.db_path)
        self.SessionLocal = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )

    def getSession(self):
        return self.SessionLocal


def hourly_capture_slot(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def nfl_matchup_teams(title: str) -> tuple[str, str] | None:
    """Return the two NFL teams in title order: away/first, then home/second."""
    normalized = " ".join(str(title or "").split()).casefold()
    if not normalized or any(marker in normalized for marker in NON_GAME_MARKERS):
        return None

    matches = sorted(
        (
            (normalized.find(name.casefold()), name)
            for name in NFL_TEAM_NAMES
            if name.casefold() in normalized
        ),
        key=lambda item: item[0],
    )
    if len(matches) != 2:
        return None
    return matches[0][1], matches[1][1]


def nfl_home_team(title: str) -> str | None:
    """Use the second team in a provider matchup title as the home-team bucket."""
    matchup = nfl_matchup_teams(title)
    return matchup[1] if matchup else None


def nfl_event_home_team(event: NFLEvent) -> str | None:
    return event.home_team or nfl_home_team(event.title)


def nfl_event_away_team(event: NFLEvent) -> str | None:
    matchup = nfl_matchup_teams(event.title)
    return event.away_team or (matchup[0] if matchup else None)


def nfl_display_venue(event: NFLEvent) -> str:
    return event.canonical_venue or canonical_venue_name(
        event.provider_venue or event.venue
    )


def is_nfl_game_title(title: str) -> bool:
    normalized = " ".join(str(title or "").split()).casefold()
    has_matchup_separator = any(
        separator in normalized for separator in (" at ", " vs ", " vs. ", " versus ")
    )
    return nfl_matchup_teams(title) is not None and has_matchup_separator


def _clean_metadata_text(value: Any, maximum: int = 250) -> str:
    return " ".join(str(value or "").split())[:maximum]


def normalize_nfl_schedule_metadata(
    raw: Any,
    *,
    title: str,
    provider_venue: str,
) -> dict[str, Any]:
    matchup = nfl_matchup_teams(title)
    if matchup is None:
        raise ValueError("NFL schedule metadata requires a valid matchup title.")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("NFL schedule metadata must be an object.")

    away_team = _clean_metadata_text(raw.get("away_team")) or matchup[0]
    home_team = _clean_metadata_text(raw.get("home_team")) or matchup[1]
    if (away_team, home_team) != matchup:
        raise ValueError(
            "NFL schedule metadata does not match the captured away/home order."
        )

    neutral_site = raw.get("neutral_site")
    if neutral_site is not None and not isinstance(neutral_site, bool):
        raise ValueError("NFL neutral_site metadata must be true, false, or null.")

    schedule_id = _clean_metadata_text(raw.get("schedule_id"), maximum=160)
    canonical_venue = canonical_venue_name(
        raw.get("canonical_venue") or provider_venue
    )
    return {
        "schedule_id": schedule_id or None,
        "away_team": away_team,
        "home_team": home_team,
        "canonical_venue": canonical_venue,
        "city": _clean_metadata_text(raw.get("city")),
        "country": _clean_metadata_text(raw.get("country")),
        "neutral_site": neutral_site,
        "provider_venue": _clean_metadata_text(provider_venue),
    }


def nfl_snapshot_from_payload(payload: dict[str, Any]):
    if payload.get("event_type") != "nfl":
        raise ValueError("NFL endpoint only accepts NFL snapshots.")

    url, event_date, captured_at, snapshot = snapshot_from_payload(payload)
    if NFL_URL_PATTERN.search(url) is None:
        raise ValueError("NFL snapshot URL is missing a Vivid production ID.")
    if not is_nfl_game_title(snapshot.title):
        raise ValueError("NFL endpoint only accepts actual NFL game matchups.")
    schedule_metadata = normalize_nfl_schedule_metadata(
        payload.get("schedule"),
        title=snapshot.title,
        provider_venue=snapshot.venue,
    )
    map_geometry = sanitize_map_geometry(
        payload.get("map_geometry"),
        [row.section for row in snapshot.sections],
    )
    return (
        url,
        event_date,
        captured_at,
        snapshot,
        schedule_metadata,
        map_geometry,
    )


def _apply_event_metadata(
    event: NFLEvent,
    snapshot: Any,
    schedule_metadata: dict[str, Any],
    map_geometry: dict[str, Any] | None,
    stored_capture: datetime,
) -> None:
    incoming_schedule_id = schedule_metadata.get("schedule_id")
    if event.schedule_id and incoming_schedule_id and event.schedule_id != incoming_schedule_id:
        raise ValueError("NFL schedule ID changed for an existing provider event.")

    event.schedule_id = event.schedule_id or incoming_schedule_id
    event.away_team = schedule_metadata.get("away_team") or event.away_team
    event.home_team = schedule_metadata.get("home_team") or event.home_team
    event.provider_venue = snapshot.venue
    event.canonical_venue = (
        schedule_metadata.get("canonical_venue")
        or event.canonical_venue
        or canonical_venue_name(snapshot.venue)
    )
    event.city = schedule_metadata.get("city") or event.city
    event.country = schedule_metadata.get("country") or event.country
    if schedule_metadata.get("neutral_site") is not None:
        event.neutral_site = schedule_metadata["neutral_site"]

    if map_geometry is not None:
        previous_count = geometry_section_count(event.map_geometry)
        incoming_count = geometry_section_count(map_geometry)
        if incoming_count >= previous_count:
            event.map_geometry = map_geometry
            event.map_source = str(map_geometry.get("source") or "provider")
            event.geometry_updated_at = stored_capture


def store_nfl_snapshot(
    url: str,
    event_date: datetime,
    snapshot: Any,
    captured_at: datetime,
    *,
    db_path: str | Path | None = None,
    schedule_metadata: dict[str, Any] | None = None,
    map_geometry: dict[str, Any] | None = None,
) -> tuple[int, int, bool]:
    model = CreateNFLModel(db_path)
    stored_event_date = event_datetime_for_storage(event_date)
    stored_captured_at = captured_datetime_for_storage(hourly_capture_slot(captured_at))
    normalized_metadata = normalize_nfl_schedule_metadata(
        schedule_metadata,
        title=snapshot.title,
        provider_venue=snapshot.venue,
    )
    normalized_geometry = sanitize_map_geometry(
        map_geometry,
        [row.section for row in snapshot.sections],
    )

    try:
        with model.getSession()() as session:
            event = (
                session.query(NFLEvent)
                .filter(
                    (NFLEvent.source_url == url)
                    | (NFLEvent.source_id == snapshot.source_id)
                )
                .first()
            )
            if event is None:
                event = NFLEvent(
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

            _apply_event_metadata(
                event,
                snapshot,
                normalized_metadata,
                normalized_geometry,
                stored_captured_at,
            )

            existing = (
                session.query(NFLIteration)
                .filter(
                    NFLIteration.event_id == event.id,
                    NFLIteration.captured_at == stored_captured_at,
                )
                .first()
            )
            if existing is not None:
                session.commit()
                return event.id, existing.id, False

            iteration = NFLIteration(event=event, captured_at=stored_captured_at)
            session.add(iteration)
            session.add_all(
                NFLTicket(
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
                    session.query(NFLEvent)
                    .filter(
                        (NFLEvent.source_url == url)
                        | (NFLEvent.source_id == snapshot.source_id)
                    )
                    .one()
                )
                existing = (
                    session.query(NFLIteration)
                    .filter(
                        NFLIteration.event_id == event.id,
                        NFLIteration.captured_at == stored_captured_at,
                    )
                    .one()
                )
                return event.id, existing.id, False
            return event.id, iteration.id, True
    finally:
        model.engine.dispose()


def create_nfl_daily_backup(
    now: datetime | None = None,
    source: Path | None = None,
    backup_dir: Path = DEFAULT_NFL_BACKUP_DIR,
) -> Path:
    now = now or datetime.now(timezone.utc)
    source = Path(source or nfl_database_path()).expanduser().resolve()
    if not source.exists():
        model = CreateNFLModel(source)
        model.engine.dispose()

    backup_dir.mkdir(parents=True, exist_ok=True)
    local_now = now.astimezone(EASTERN)
    target = backup_dir / f"NFL-collection-{local_now:%Y-%m-%d}.db"
    if not target.exists():
        temporary = target.with_suffix(".db.tmp")
        temporary.unlink(missing_ok=True)
        with sqlite3.connect(source) as source_db, sqlite3.connect(temporary) as backup_db:
            source_db.backup(backup_db)
        temporary.replace(target)

    backups = sorted(backup_dir.glob("NFL-collection-*.db"), reverse=True)
    for expired in backups[NFL_BACKUP_RETENTION_DAYS:]:
        expired.unlink()
    return target


def write_nfl_audit(
    url: str,
    event_date: datetime,
    snapshot: Any,
    event_id: int,
    iteration_id: int,
    captured_at: datetime,
    audit_dir: Path = DEFAULT_NFL_AUDIT_DIR,
    *,
    schedule_metadata: dict[str, Any] | None = None,
    map_geometry: dict[str, Any] | None = None,
) -> Path:
    audit_dir.mkdir(parents=True, exist_ok=True)
    normalized_capture = hourly_capture_slot(captured_at)
    local_capture = normalized_capture.astimezone(EASTERN)
    path = audit_dir / f"{local_capture:%Y-%m-%d}.jsonl"
    record = {
        "schema_version": 2,
        "event_type": "nfl",
        "timezone": "America/New_York",
        "captured_at": eastern_iso(normalized_capture),
        "event_date": eastern_iso(event_date),
        "event_id": event_id,
        "iteration_id": iteration_id,
        "source_id": snapshot.source_id,
        "title": snapshot.title,
        "provider_venue": snapshot.venue,
        "canonical_venue": (schedule_metadata or {}).get("canonical_venue")
        or canonical_venue_name(snapshot.venue),
        "schedule": schedule_metadata,
        "url": url,
        "currency": "USD",
        "section_count": len(snapshot.sections),
        "map_geometry_source": (
            map_geometry.get("source") if isinstance(map_geometry, dict) else None
        ),
        "map_geometry_sections": geometry_section_count(map_geometry),
        "sections": [asdict(row) for row in snapshot.sections],
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    cutoff = normalized_capture - timedelta(days=NFL_AUDIT_RETENTION_DAYS)
    for candidate in audit_dir.glob("*.jsonl"):
        modified = datetime.fromtimestamp(candidate.stat().st_mtime, tz=timezone.utc)
        if modified < cutoff:
            candidate.unlink()
    return path


def _authorized_request() -> bool:
    configured = os.environ.get("COLLECTOR_INGEST_TOKEN", "")
    supplied = request.headers.get("Authorization", "")
    return bool(configured) and hmac.compare_digest(supplied, f"Bearer {configured}")


@nfl_blueprint.post("/api/nfl/snapshot")
def ingest_nfl_snapshot():
    if not _authorized_request():
        return jsonify({"status": "error", "error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "error": "invalid JSON body"}), 400

    try:
        (
            url,
            event_date,
            captured_at,
            snapshot,
            schedule_metadata,
            map_geometry,
        ) = nfl_snapshot_from_payload(payload)
        now = datetime.now(timezone.utc)
        captured_at_utc = captured_at.astimezone(timezone.utc)
        event_date_utc = event_date.astimezone(timezone.utc)

        if captured_at_utc > now + MAX_SNAPSHOT_CLOCK_SKEW:
            raise ValueError("Snapshot capture time is in the future.")
        if now - captured_at_utc > MAX_SNAPSHOT_REPLAY_AGE:
            raise ValueError("Snapshot is older than the seven-day replay window.")
        if event_date_utc <= captured_at_utc:
            raise ValueError("The NFL game had already started at the capture time.")
        if event_date_utc - captured_at_utc > timedelta(hours=NFL_CAPTURE_WINDOW_HOURS):
            raise ValueError("The NFL game is outside the 30-day capture window.")

        create_nfl_daily_backup(now=now)
        event_id, iteration_id, stored = store_nfl_snapshot(
            url,
            event_date,
            snapshot,
            captured_at,
            schedule_metadata=schedule_metadata,
            map_geometry=map_geometry,
        )
        if stored:
            write_nfl_audit(
                url,
                event_date,
                snapshot,
                event_id,
                iteration_id,
                captured_at,
                schedule_metadata=schedule_metadata,
                map_geometry=map_geometry,
            )
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"status": "error", "error": str(exc)}), 400

    status = "stored" if stored else "duplicate"
    return jsonify(
        {
            "status": status,
            "event_type": "nfl",
            "timezone": "America/New_York",
            "event_id": event_id,
            "iteration_id": iteration_id,
            "sections": len(snapshot.sections),
            "map_geometry_sections": geometry_section_count(map_geometry),
            "captured_at": eastern_iso(hourly_capture_slot(captured_at)),
        }
    ), 201 if stored else 200


def format_nfl_title(event: NFLEvent) -> str:
    event_date = event_datetime_eastern(event.event_date)
    hour = event_date.hour % 12 or 12
    return (
        f"{event.title} — {event_date:%b} {event_date.day}, {event_date.year} "
        f"· {hour}:{event_date.minute:02d} {event_date:%p} {event_date:%Z}"
    )


def find_nfl_game(team_or_venue: str, identifier: str | None) -> NFLEvent | None:
    """Find a game in its team bucket while preserving old venue bookmarks."""
    if not identifier or not str(identifier).isdigit():
        return None
    model = CreateNFLModel()
    try:
        with model.getSession()() as session:
            event = (
                session.query(NFLEvent)
                .filter(NFLEvent.id == int(identifier))
                .first()
            )
            if event is None:
                return None
            accepted = {
                nfl_event_home_team(event),
                event.venue,
                event.provider_venue,
                nfl_display_venue(event),
            }
            if team_or_venue not in accepted:
                return None
            return event
    finally:
        model.engine.dispose()


def nfl_map_section_data(
    event_id: int,
) -> tuple[list[dict[str, Any]], datetime | None]:
    """Return every known section plus its most recent stored market values."""
    model = CreateNFLModel()
    try:
        with model.getSession()() as session:
            event = (
                session.query(NFLEvent)
                .filter(NFLEvent.id == event_id)
                .first()
            )
            if event is None:
                return [], None

            latest = (
                session.query(NFLIteration)
                .filter(NFLIteration.event_id == event_id)
                .order_by(
                    NFLIteration.captured_at.desc(),
                    NFLIteration.id.desc(),
                )
                .first()
            )
            tickets_by_section = {
                ticket.section: ticket for ticket in (latest.tickets if latest else [])
            }
            section_names = sorted(
                set(event.sections or []) | set(tickets_by_section),
                key=str.casefold,
            )
            section_data = []
            for name in section_names:
                ticket = tickets_by_section.get(name)
                section_data.append(
                    {
                        "name": name,
                        "price": ticket.price if ticket is not None else None,
                        "listing_count": (
                            ticket.listing_count if ticket is not None else None
                        ),
                    }
                )
            return section_data, latest.captured_at if latest else None
    finally:
        model.engine.dispose()


def format_nfl_capture_label(value: datetime | None) -> str:
    if value is None:
        return "No snapshots stored"
    captured = value
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    return eastern_label(captured)


class NFLGraphBuilder:
    def __init__(self):
        self.plotter = GraphBuilder()

    def single_game_graph(
        self,
        home_team: str,
        event_id: int,
        section: str,
        display_mode: str,
    ) -> tuple[list[float], list[float]]:
        model = CreateNFLModel()
        try:
            with model.getSession()() as session:
                event = (
                    session.query(NFLEvent)
                    .filter(NFLEvent.id == event_id)
                    .first()
                )
                if event is None or nfl_event_home_team(event) != home_team:
                    return [], []

                tickets = (
                    session.query(NFLTicket)
                    .join(NFLTicket.iteration)
                    .join(NFLIteration.event)
                    .filter(
                        NFLTicket.section == section,
                        NFLEvent.id == event_id,
                    )
                    .order_by(NFLIteration.captured_at.asc())
                    .all()
                )
                pairs = [
                    (
                        round(
                            hours_before_event(
                                ticket.iteration.event.event_date,
                                ticket.iteration.captured_at,
                            ),
                            3,
                        ),
                        ticket.price,
                    )
                    for ticket in tickets
                ]
        finally:
            model.engine.dispose()

        pairs = [pair for pair in pairs if 0 < pair[0] <= NFL_CAPTURE_WINDOW_HOURS]
        if not pairs:
            return [], []

        x, y = map(list, zip(*pairs))
        if display_mode != "money":
            y = self.plotter.standardize(y)
        return y, x

    def create_plot(self, x: list[float], y: list[float], display_mode: str) -> str:
        return self.plotter.create_plot(x, y, display_mode)


def _event_is_completed(event: NFLEvent, now: datetime) -> bool:
    return event_datetime_eastern(event.event_date) <= now.astimezone(EASTERN)


def _nfl_home_context(archive_mode: bool) -> dict[str, Any]:
    model = CreateNFLModel()
    now = datetime.now(timezone.utc)
    try:
        with model.getSession()() as session:
            all_games = session.query(NFLEvent).order_by(NFLEvent.event_date).all()
            completed_count = sum(_event_is_completed(game, now) for game in all_games)
            upcoming_count = len(all_games) - completed_count
            games = [
                game
                for game in all_games
                if _event_is_completed(game, now) == archive_mode
            ]
            if archive_mode:
                games.reverse()

            games_dict: dict[str, list[dict[str, str]]] = {}
            game_sections_dict: dict[str, dict[str, list[str]]] = {}
            for game in games:
                home_team = nfl_event_home_team(game)
                if home_team is None:
                    continue
                games_dict.setdefault(home_team, []).append(
                    {"value": str(game.id), "label": format_nfl_title(game)}
                )
                game_sections_dict.setdefault(home_team, {})[
                    str(game.id)
                ] = sorted(set(game.sections or []))

            games_dict = dict(sorted(games_dict.items()))
            game_sections_dict = {
                team: game_sections_dict[team] for team in games_dict
            }
            team_count = len(games_dict)
            game_count = sum(len(team_games) for team_games in games_dict.values())
            section_count = len(
                {
                    section
                    for by_game in game_sections_dict.values()
                    for sections in by_game.values()
                    for section in sections
                }
            )
    finally:
        model.engine.dispose()

    return {
        "games_dict": games_dict,
        "game_sections_dict": game_sections_dict,
        "team_count": team_count,
        "game_count": game_count,
        "section_count": section_count,
        "archive_mode": archive_mode,
        "upcoming_count": upcoming_count,
        "completed_count": completed_count,
    }


@nfl_blueprint.get("/nfl")
def nfl_home():
    return render_template("NFLHomeScreen.html", **_nfl_home_context(False))


@nfl_blueprint.get("/nfl/archive")
def nfl_archive():
    return render_template("NFLHomeScreen.html", **_nfl_home_context(True))


@nfl_blueprint.get("/nfl/map")
def nfl_map():
    selection = request.args.get("team") or request.args.get("event") or ""
    event_id = request.args.get("game")
    selected = find_nfl_game(selection, event_id)
    if selected is None:
        return render_template(
            "nfl_map.html",
            error="Choose a valid tracked NFL game before opening its stadium map.",
        )

    team = nfl_event_home_team(selected) or selection
    section_data, latest_capture = nfl_map_section_data(selected.id)
    if not section_data:
        return render_template(
            "nfl_map.html",
            error="No section data has been collected for that NFL game yet.",
        )

    selected_section = request.args.get("section") or ""
    known_sections = {item["name"] for item in section_data}
    if selected_section not in known_sections:
        selected_section = ""

    geometry = sanitize_map_geometry(selected.map_geometry, known_sections)
    has_provider_geometry = geometry_is_usable(geometry, known_sections)
    venue = nfl_display_venue(selected)
    map_data = {
        "team": team,
        "game": str(selected.id),
        "venue": venue,
        "sections": section_data,
        "geometry": geometry,
        "geometry_mode": "provider" if has_provider_geometry else "schematic",
        "selected_section": selected_section,
        "graph_url": url_for("nfl.nfl_graph"),
    }
    return render_template(
        "nfl_map.html",
        error=None,
        team=team,
        venue=venue,
        city=selected.city or "",
        country=selected.country or "",
        neutral_site=selected.neutral_site is True,
        game=str(selected.id),
        gameLabel=format_nfl_title(selected),
        section_count=len(section_data),
        priced_section_count=sum(
            item["price"] is not None for item in section_data
        ),
        latest_capture_label=format_nfl_capture_label(latest_capture),
        source_url=selected.source_url,
        has_provider_geometry=has_provider_geometry,
        map_geometry_source=(geometry or {}).get("source", ""),
        map_geometry_sections=geometry_section_count(geometry),
        map_data=map_data,
    )


@nfl_blueprint.get("/nfl/graph")
def nfl_graph():
    selection = request.args.get("team") or request.args.get("event") or ""
    event_id = request.args.get("game")
    section = request.args.get("section") or ""
    display_mode = "percentage" if request.args.get("display") == "percentage" else "money"
    selected = find_nfl_game(selection, event_id)
    team = nfl_event_home_team(selected) if selected else selection
    venue = nfl_display_venue(selected) if selected else ""
    label = format_nfl_title(selected) if selected else "Unknown NFL game"

    builder = NFLGraphBuilder()
    y, x = (
        builder.single_game_graph(team, selected.id, section, display_mode)
        if selected and team
        else ([], [])
    )
    toggle_mode = "percentage" if display_mode == "money" else "money"
    toggle_label = "%" if display_mode == "money" else "$"

    if not x or not y:
        return render_template(
            "nfl_graph.html",
            error="No NFL price data is available for that selection.",
            team=team,
            venue=venue,
            section=section,
            game=event_id or "",
            gameLabel=label,
            displayType=toggle_mode,
            displayLabel=toggle_label,
        )

    return render_template(
        "nfl_graph.html",
        img=builder.create_plot(x, y, display_mode),
        chartX=x,
        chartY=y,
        displayMode=display_mode,
        team=team,
        venue=venue,
        section=section,
        game=str(selected.id),
        gameLabel=label,
        displayType=toggle_mode,
        displayLabel=toggle_label,
    )
