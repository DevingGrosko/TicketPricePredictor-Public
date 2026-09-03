"""NHL-specific storage, API routes, charts, maps, and conservative compaction."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hmac
import json
import math
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
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    load_only,
    mapped_column,
    relationship,
    sessionmaker,
)

from collector import snapshot_from_payload
from graph_builder import GraphBuilder
from models import (
    captured_datetime_for_storage,
    event_datetime_eastern,
    event_datetime_for_storage,
    hours_before_event,
)
from Flask_App.performance_cache import (
    OPTIONS_CACHE_TTL_SECONDS,
    PAGE_CACHE_TTL_SECONDS,
    cache_key,
    file_version,
    invalidate_sport_cache,
    page_cache,
)
from Flask_App.section_canonicalization import is_excluded_ticket_area
from Flask_App.nfl_blueprint import (
    EASTERN,
    canonical_venue_name,
    eastern_iso,
    eastern_label,
    geometry_is_usable,
    geometry_section_count,
    sanitize_map_geometry,
)


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_NHL_DATABASE = PROJECT_DIR / "NHL-collection.db"
DEFAULT_NHL_AUDIT_DIR = PROJECT_DIR / "nhl_audit"
DEFAULT_NHL_BACKUP_DIR = PROJECT_DIR / "nhl_backups"
NHL_BACKUP_RETENTION_DAYS = 7
NHL_AUDIT_RETENTION_DAYS = 30
NHL_CAPTURE_WINDOW_HOURS = 30 * 24
MAX_SNAPSHOT_REPLAY_AGE = timedelta(days=7)
MAX_SNAPSHOT_CLOCK_SKEW = timedelta(minutes=5)
NHL_COMPACTION_DELAY = timedelta(days=14)
NHL_COMPACTION_FINAL_HOURLY_HOURS = 24
NHL_COMPACTION_MIDDLE_INTERVAL_HOURS = 3
NHL_COMPACTION_WEEK_INTERVAL_HOURS = 6
NHL_COMPACTION_DAILY_BOUNDARY_HOURS = 7 * 24
NHL_COMPACTION_LONG_RANGE_INTERVAL_HOURS = 24
NHL_URL_PATTERN = re.compile(r"/production/(\d+)(?:[/?#]|$)", flags=re.IGNORECASE)

NHL_TEAM_NAMES = frozenset(
    {
        "Anaheim Ducks",
        "Boston Bruins",
        "Buffalo Sabres",
        "Calgary Flames",
        "Carolina Hurricanes",
        "Chicago Blackhawks",
        "Colorado Avalanche",
        "Columbus Blue Jackets",
        "Dallas Stars",
        "Detroit Red Wings",
        "Edmonton Oilers",
        "Florida Panthers",
        "Los Angeles Kings",
        "Minnesota Wild",
        "Montreal Canadiens",
        "Nashville Predators",
        "New Jersey Devils",
        "New York Islanders",
        "New York Rangers",
        "Ottawa Senators",
        "Philadelphia Flyers",
        "Pittsburgh Penguins",
        "San Jose Sharks",
        "Seattle Kraken",
        "St. Louis Blues",
        "Tampa Bay Lightning",
        "Toronto Maple Leafs",
        "Utah Mammoth",
        "Vancouver Canucks",
        "Vegas Golden Knights",
        "Washington Capitals",
        "Winnipeg Jets",
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
    "prospect tournament",
)

SUPPORTED_COUNTRY_MARKERS = frozenset(
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

nhl_blueprint = Blueprint("nhl", __name__)
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY: set[str] = set()
_COMPACTION_LOCK = threading.Lock()


class NHLBase(DeclarativeBase):
    pass


class NHLEvent(NHLBase):
    __tablename__ = "nhl_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    event_date: Mapped[datetime] = mapped_column(DateTime(), nullable=False, index=True)
    sections: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
    )
    source_url: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    venue: Mapped[str] = mapped_column(String, nullable=False, index=True)
    schedule_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    away_team: Mapped[str | None] = mapped_column(String, nullable=True)
    home_team: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    canonical_venue: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    venue_timezone: Mapped[str | None] = mapped_column(String, nullable=True)
    country: Mapped[str | None] = mapped_column(String, nullable=True)
    neutral_site: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    game_type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    provider_venue: Mapped[str | None] = mapped_column(String, nullable=True)
    map_geometry: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    map_source: Mapped[str | None] = mapped_column(String, nullable=True)
    geometry_updated_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    compacted_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    original_iteration_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retained_iteration_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    iterations: Mapped[list["NHLIteration"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
    )


class NHLIteration(NHLBase):
    __tablename__ = "nhl_iterations"
    __table_args__ = (
        UniqueConstraint("event_id", "captured_at", name="uq_nhl_event_capture_slot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("nhl_event.id"),
        nullable=False,
        index=True,
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, index=True)

    event: Mapped[NHLEvent] = relationship(back_populates="iterations")
    tickets: Mapped[list["NHLTicket"]] = relationship(
        back_populates="iteration",
        cascade="all, delete-orphan",
    )


class NHLTicket(NHLBase):
    __tablename__ = "nhl_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    section: Mapped[str] = mapped_column(String, nullable=False, index=True)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    listing_count: Mapped[int] = mapped_column(Integer, nullable=False)
    iteration_id: Mapped[int] = mapped_column(
        ForeignKey("nhl_iterations.id"),
        nullable=False,
        index=True,
    )

    iteration: Mapped[NHLIteration] = relationship(back_populates="tickets")


def nhl_database_path() -> Path:
    configured = os.environ.get("NHL_DATABASE_PATH", str(DEFAULT_NHL_DATABASE))
    return Path(configured).expanduser().resolve()


def _ensure_nhl_schema(engine: Any, db_path: Path) -> None:
    key = str(db_path)
    with _SCHEMA_LOCK:
        if key in _SCHEMA_READY:
            return
        with engine.begin() as connection:
            current = {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(nhl_event)"
                ).fetchall()
            }
            additions = {
                "schedule_id": "VARCHAR",
                "away_team": "VARCHAR",
                "home_team": "VARCHAR",
                "canonical_venue": "VARCHAR",
                "venue_timezone": "VARCHAR",
                "country": "VARCHAR",
                "neutral_site": "BOOLEAN",
                "game_type": "INTEGER",
                "season": "INTEGER",
                "currency": "VARCHAR(3) DEFAULT 'USD'",
                "provider_venue": "VARCHAR",
                "map_geometry": "JSON",
                "map_source": "VARCHAR",
                "geometry_updated_at": "DATETIME",
                "compacted_at": "DATETIME",
                "original_iteration_count": "INTEGER",
                "retained_iteration_count": "INTEGER",
            }
            for column, sql_type in additions.items():
                if column not in current:
                    connection.exec_driver_sql(
                        f"ALTER TABLE nhl_event ADD COLUMN {column} {sql_type}"
                    )

            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_nhl_event_schedule_id "
                "ON nhl_event (schedule_id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_nhl_event_home_team "
                "ON nhl_event (home_team)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_nhl_event_canonical_venue "
                "ON nhl_event (canonical_venue)"
            )
            connection.exec_driver_sql(
                "UPDATE nhl_event SET currency = 'USD' "
                "WHERE currency IS NULL OR TRIM(currency) = ''"
            )

            rows = connection.execute(
                text(
                    "SELECT id, title, venue, provider_venue, canonical_venue, "
                    "away_team, home_team FROM nhl_event"
                )
            ).mappings()
            for row in rows:
                matchup = nhl_matchup_teams(row["title"])
                provider_venue = row["provider_venue"] or row["venue"]
                canonical_venue = row["canonical_venue"] or canonical_venue_name(
                    provider_venue
                )
                connection.execute(
                    text(
                        "UPDATE nhl_event SET provider_venue = :provider_venue, "
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


class CreateNHLModel:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or nhl_database_path()).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            echo=False,
            connect_args={"timeout": 30},
        )
        NHLBase.metadata.create_all(self.engine)
        _ensure_nhl_schema(self.engine, self.db_path)
        self.SessionLocal = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )

    def getSession(self):
        return self.SessionLocal


def hourly_capture_slot(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def nhl_matchup_teams(title: str) -> tuple[str, str] | None:
    normalized = " ".join(str(title or "").split()).casefold()
    if not normalized or any(marker in normalized for marker in NON_GAME_MARKERS):
        return None
    matches = sorted(
        (
            (normalized.find(name.casefold()), name)
            for name in NHL_TEAM_NAMES
            if name.casefold() in normalized
        ),
        key=lambda item: item[0],
    )
    if len(matches) != 2:
        return None
    return matches[0][1], matches[1][1]


def nhl_event_home_team(event: NHLEvent) -> str | None:
    matchup = nhl_matchup_teams(event.title)
    return event.home_team or (matchup[1] if matchup else None)


def nhl_event_away_team(event: NHLEvent) -> str | None:
    matchup = nhl_matchup_teams(event.title)
    return event.away_team or (matchup[0] if matchup else None)


def nhl_display_venue(event: NHLEvent) -> str:
    return event.canonical_venue or canonical_venue_name(
        event.provider_venue or event.venue
    )


def is_nhl_game_title(title: str) -> bool:
    normalized = " ".join(str(title or "").split()).casefold()
    separator = any(
        value in normalized for value in (" at ", " vs ", " vs. ", " versus ")
    )
    return nhl_matchup_teams(title) is not None and separator


def _clean_text(value: Any, maximum: int = 250) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _normalized_country(value: Any) -> str:
    return _clean_text(value).casefold().replace(".", "")


def _country_is_supported(value: Any) -> bool:
    return _normalized_country(value) in SUPPORTED_COUNTRY_MARKERS


def normalize_nhl_schedule_metadata(
    raw: Any,
    *,
    title: str,
    provider_venue: str,
    currency: str,
) -> dict[str, Any]:
    matchup = nhl_matchup_teams(title)
    if matchup is None:
        raise ValueError("NHL schedule metadata requires a valid matchup title.")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("NHL schedule metadata must be an object.")

    away_team = _clean_text(raw.get("away_team")) or matchup[0]
    home_team = _clean_text(raw.get("home_team")) or matchup[1]
    if (away_team, home_team) != matchup:
        raise ValueError(
            "NHL schedule metadata does not match the captured away/home order."
        )

    country = _clean_text(raw.get("country"))
    if _normalized_country(country) not in SUPPORTED_COUNTRY_MARKERS:
        raise ValueError(
            "NHL tracking currently accepts only U.S. and Canadian venues."
        )

    neutral_site = raw.get("neutral_site")
    if neutral_site is not None and not isinstance(neutral_site, bool):
        raise ValueError("NHL neutral_site metadata must be true, false, or null.")

    game_type_raw = raw.get("game_type")
    try:
        game_type = int(game_type_raw) if game_type_raw is not None else None
    except (TypeError, ValueError) as exc:
        raise ValueError("NHL game_type must be an integer.") from exc
    if game_type is not None and game_type not in {1, 2, 3}:
        raise ValueError("NHL game_type must be preseason, regular season, or playoffs.")

    season_raw = raw.get("season")
    try:
        season = int(season_raw) if season_raw is not None else None
    except (TypeError, ValueError) as exc:
        raise ValueError("NHL season must be an integer.") from exc

    normalized_currency = _clean_text(currency, maximum=3).upper()
    if not re.fullmatch(r"[A-Z]{3}", normalized_currency):
        raise ValueError("NHL snapshot currency must be a three-letter code.")

    return {
        "schedule_id": _clean_text(raw.get("schedule_id"), maximum=160) or None,
        "away_team": away_team,
        "home_team": home_team,
        "canonical_venue": canonical_venue_name(
            raw.get("canonical_venue") or provider_venue
        ),
        "venue_timezone": _clean_text(raw.get("venue_timezone")),
        "country": country,
        "neutral_site": neutral_site,
        "game_type": game_type,
        "season": season,
        "currency": normalized_currency,
        "provider_venue": _clean_text(provider_venue),
    }


def nhl_snapshot_from_payload(payload: dict[str, Any]):
    if payload.get("event_type") != "nhl":
        raise ValueError("NHL endpoint only accepts NHL snapshots.")

    url, event_date, captured_at, snapshot = snapshot_from_payload(payload)
    if NHL_URL_PATTERN.search(url) is None:
        raise ValueError("NHL snapshot URL is missing a Vivid production ID.")
    if not is_nhl_game_title(snapshot.title):
        raise ValueError("NHL endpoint only accepts actual NHL game matchups.")

    currency = _clean_text(payload.get("currency") or "USD", maximum=3).upper()
    schedule_metadata = normalize_nhl_schedule_metadata(
        payload.get("schedule"),
        title=snapshot.title,
        provider_venue=snapshot.venue,
        currency=currency,
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
    event: NHLEvent,
    snapshot: Any,
    metadata: dict[str, Any],
    map_geometry: dict[str, Any] | None,
    stored_capture: datetime,
) -> None:
    incoming_schedule_id = metadata.get("schedule_id")
    if (
        event.schedule_id
        and incoming_schedule_id
        and event.schedule_id != incoming_schedule_id
    ):
        raise ValueError("NHL schedule ID changed for an existing provider event.")

    event.schedule_id = event.schedule_id or incoming_schedule_id
    event.away_team = metadata.get("away_team") or event.away_team
    event.home_team = metadata.get("home_team") or event.home_team
    event.provider_venue = snapshot.venue
    event.canonical_venue = (
        metadata.get("canonical_venue")
        or event.canonical_venue
        or canonical_venue_name(snapshot.venue)
    )
    event.venue_timezone = metadata.get("venue_timezone") or event.venue_timezone
    event.country = metadata.get("country") or event.country
    if metadata.get("neutral_site") is not None:
        event.neutral_site = metadata["neutral_site"]
    event.game_type = metadata.get("game_type") or event.game_type
    event.season = metadata.get("season") or event.season
    event.currency = metadata.get("currency") or event.currency or "USD"

    if map_geometry is not None:
        previous_count = geometry_section_count(event.map_geometry)
        incoming_count = geometry_section_count(map_geometry)
        if incoming_count >= previous_count:
            event.map_geometry = map_geometry
            event.map_source = str(map_geometry.get("source") or "provider")
            event.geometry_updated_at = stored_capture


def store_nhl_snapshot(
    url: str,
    event_date: datetime,
    snapshot: Any,
    captured_at: datetime,
    *,
    db_path: str | Path | None = None,
    schedule_metadata: dict[str, Any] | None = None,
    map_geometry: dict[str, Any] | None = None,
    currency: str = "USD",
) -> tuple[int, int, bool]:
    model = CreateNHLModel(db_path)
    stored_event_date = event_datetime_for_storage(event_date)
    stored_captured_at = captured_datetime_for_storage(
        hourly_capture_slot(captured_at)
    )
    metadata = normalize_nhl_schedule_metadata(
        schedule_metadata,
        title=snapshot.title,
        provider_venue=snapshot.venue,
        currency=currency,
    )
    normalized_geometry = sanitize_map_geometry(
        map_geometry,
        [row.section for row in snapshot.sections],
    )

    try:
        with model.getSession()() as session:
            event = (
                session.query(NHLEvent)
                .filter(
                    (NHLEvent.source_url == url)
                    | (NHLEvent.source_id == snapshot.source_id)
                )
                .first()
            )
            if event is None:
                event = NHLEvent(
                    source_id=snapshot.source_id,
                    title=snapshot.title,
                    event_date=stored_event_date,
                    sections=[row.section for row in snapshot.sections],
                    source_url=url,
                    venue=snapshot.venue,
                    currency=metadata["currency"],
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
                metadata,
                normalized_geometry,
                stored_captured_at,
            )

            existing = (
                session.query(NHLIteration)
                .filter(
                    NHLIteration.event_id == event.id,
                    NHLIteration.captured_at == stored_captured_at,
                )
                .first()
            )
            if existing is not None:
                session.commit()
                return event.id, existing.id, False

            iteration = NHLIteration(event=event, captured_at=stored_captured_at)
            session.add(iteration)
            session.add_all(
                NHLTicket(
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
                    session.query(NHLEvent)
                    .filter(
                        (NHLEvent.source_url == url)
                        | (NHLEvent.source_id == snapshot.source_id)
                    )
                    .one()
                )
                existing = (
                    session.query(NHLIteration)
                    .filter(
                        NHLIteration.event_id == event.id,
                        NHLIteration.captured_at == stored_captured_at,
                    )
                    .one()
                )
                return event.id, existing.id, False
            return event.id, iteration.id, True
    finally:
        model.engine.dispose()


def create_nhl_daily_backup(
    now: datetime | None = None,
    source: Path | None = None,
    backup_dir: Path = DEFAULT_NHL_BACKUP_DIR,
) -> Path:
    now = now or datetime.now(timezone.utc)
    source = Path(source or nhl_database_path()).expanduser().resolve()
    if not source.exists():
        model = CreateNHLModel(source)
        model.engine.dispose()

    backup_dir.mkdir(parents=True, exist_ok=True)
    local_now = now.astimezone(EASTERN)
    target = backup_dir / f"NHL-collection-{local_now:%Y-%m-%d}.db"
    if not target.exists():
        temporary = target.with_suffix(".db.tmp")
        temporary.unlink(missing_ok=True)
        with sqlite3.connect(source) as source_db, sqlite3.connect(
            temporary
        ) as backup_db:
            source_db.backup(backup_db)
        temporary.replace(target)

    backups = sorted(backup_dir.glob("NHL-collection-*.db"), reverse=True)
    for expired in backups[NHL_BACKUP_RETENTION_DAYS:]:
        expired.unlink()
    return target


def write_nhl_audit(
    url: str,
    event_date: datetime,
    snapshot: Any,
    event_id: int,
    iteration_id: int,
    captured_at: datetime,
    audit_dir: Path = DEFAULT_NHL_AUDIT_DIR,
    *,
    schedule_metadata: dict[str, Any] | None = None,
    map_geometry: dict[str, Any] | None = None,
    currency: str = "USD",
) -> Path:
    audit_dir.mkdir(parents=True, exist_ok=True)
    normalized_capture = hourly_capture_slot(captured_at)
    local_capture = normalized_capture.astimezone(EASTERN)
    path = audit_dir / f"{local_capture:%Y-%m-%d}.jsonl"
    record = {
        "schema_version": 1,
        "event_type": "nhl",
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
        "currency": currency,
        "section_count": len(snapshot.sections),
        "map_geometry_source": (
            map_geometry.get("source")
            if isinstance(map_geometry, dict)
            else None
        ),
        "map_geometry_sections": geometry_section_count(map_geometry),
        "sections": [asdict(row) for row in snapshot.sections],
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    cutoff = normalized_capture - timedelta(days=NHL_AUDIT_RETENTION_DAYS)
    for candidate in audit_dir.glob("*.jsonl"):
        modified = datetime.fromtimestamp(
            candidate.stat().st_mtime,
            tz=timezone.utc,
        )
        if modified < cutoff:
            candidate.unlink()
    return path


def _compaction_target(hours_before: float) -> tuple[int, int] | None:
    if hours_before <= NHL_COMPACTION_FINAL_HOURLY_HOURS:
        return None
    if hours_before <= 72:
        interval = NHL_COMPACTION_MIDDLE_INTERVAL_HOURS
        boundary = NHL_COMPACTION_FINAL_HOURLY_HOURS
    elif hours_before <= NHL_COMPACTION_DAILY_BOUNDARY_HOURS:
        interval = NHL_COMPACTION_WEEK_INTERVAL_HOURS
        boundary = 72
    else:
        interval = NHL_COMPACTION_LONG_RANGE_INTERVAL_HOURS
        boundary = NHL_COMPACTION_DAILY_BOUNDARY_HOURS
    bucket = max(1, math.ceil((hours_before - boundary) / interval))
    target = boundary + bucket * interval
    return interval, target

def select_nhl_compaction_iteration_ids(
    event_date: datetime,
    iterations: list[NHLIteration],
) -> set[int]:
    """Retain final-day hourly detail and representative earlier observations."""

    if not iterations:
        return set()
    keep: set[int] = set()
    choices: dict[tuple[int, int], tuple[float, int]] = {}

    for iteration in iterations:
        lead = hours_before_event(event_date, iteration.captured_at)
        if not 0 < lead <= NHL_CAPTURE_WINDOW_HOURS:
            keep.add(iteration.id)
            continue
        target = _compaction_target(lead)
        if target is None:
            keep.add(iteration.id)
            continue
        distance = abs(lead - target[1])
        current = choices.get(target)
        if current is None or (distance, -iteration.id) < (
            current[0],
            -current[1],
        ):
            choices[target] = (distance, iteration.id)

    keep.update(iteration_id for _, iteration_id in choices.values())
    keep.add(iterations[0].id)
    keep.add(iterations[-1].id)
    return keep


def compact_completed_nhl_games(
    now: datetime | None = None,
    *,
    db_path: str | Path | None = None,
) -> dict[str, int]:
    """Compact games only after a 14-day review window.

    The retained shape is intentionally conservative:
    * all hourly observations in the final 24 hours;
    * one representative observation every 3 hours from 24 to 72 hours;
    * one representative observation every 6 hours from 72 to 168 hours;
    * one representative daily observation from 7 to 30 days.
    """

    now = now or datetime.now(timezone.utc)
    threshold = event_datetime_for_storage(now - NHL_COMPACTION_DELAY)
    model = CreateNHLModel(db_path)
    report = {
        "games_compacted": 0,
        "iterations_before": 0,
        "iterations_retained": 0,
        "iterations_deleted": 0,
    }

    with _COMPACTION_LOCK:
        try:
            with model.getSession()() as session:
                events = (
                    session.query(NHLEvent)
                    .filter(
                        NHLEvent.event_date <= threshold,
                        NHLEvent.compacted_at.is_(None),
                    )
                    .order_by(NHLEvent.event_date)
                    .all()
                )
                for event in events:
                    iterations = sorted(
                        event.iterations,
                        key=lambda item: (item.captured_at, item.id),
                    )
                    original_count = len(iterations)
                    keep_ids = select_nhl_compaction_iteration_ids(
                        event.event_date,
                        iterations,
                    )
                    for iteration in iterations:
                        if iteration.id not in keep_ids:
                            session.delete(iteration)

                    retained_count = len(keep_ids)
                    event.compacted_at = captured_datetime_for_storage(now)
                    event.original_iteration_count = original_count
                    event.retained_iteration_count = retained_count
                    report["games_compacted"] += 1
                    report["iterations_before"] += original_count
                    report["iterations_retained"] += retained_count
                    report["iterations_deleted"] += max(
                        0,
                        original_count - retained_count,
                    )

                if events:
                    session.commit()
        finally:
            model.engine.dispose()
    return report


def _authorized_request() -> bool:
    configured = os.environ.get("COLLECTOR_INGEST_TOKEN", "")
    supplied = request.headers.get("Authorization", "")
    return bool(configured) and hmac.compare_digest(
        supplied,
        f"Bearer {configured}",
    )


@nhl_blueprint.post("/api/nhl/snapshot")
def ingest_nhl_snapshot():
    if not _authorized_request():
        return jsonify({"status": "error", "error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "error": "invalid JSON body"}), 400

    compaction_report: dict[str, Any] = {"status": "not-run"}
    try:
        (
            url,
            event_date,
            captured_at,
            snapshot,
            schedule_metadata,
            map_geometry,
        ) = nhl_snapshot_from_payload(payload)
        now = datetime.now(timezone.utc)
        captured_at_utc = captured_at.astimezone(timezone.utc)
        event_date_utc = event_date.astimezone(timezone.utc)

        if captured_at_utc > now + MAX_SNAPSHOT_CLOCK_SKEW:
            raise ValueError("Snapshot capture time is in the future.")
        if now - captured_at_utc > MAX_SNAPSHOT_REPLAY_AGE:
            raise ValueError("Snapshot is older than the seven-day replay window.")
        if event_date_utc <= captured_at_utc:
            raise ValueError("The NHL game had already started at capture time.")
        if event_date_utc - captured_at_utc > timedelta(
            hours=NHL_CAPTURE_WINDOW_HOURS
        ):
            raise ValueError("The NHL game is outside the 30-day capture window.")

        create_nhl_daily_backup(now=now)
        try:
            compaction_report = {
                "status": "completed",
                **compact_completed_nhl_games(now=now),
            }
        except Exception as exc:
            # A maintenance failure should be visible without discarding a
            # valid live snapshot. The next request will retry compaction.
            compaction_report = {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }

        currency = schedule_metadata["currency"]
        event_id, iteration_id, stored = store_nhl_snapshot(
            url,
            event_date,
            snapshot,
            captured_at,
            schedule_metadata=schedule_metadata,
            map_geometry=map_geometry,
            currency=currency,
        )
        if stored:
            write_nhl_audit(
                url,
                event_date,
                snapshot,
                event_id,
                iteration_id,
                captured_at,
                schedule_metadata=schedule_metadata,
                map_geometry=map_geometry,
                currency=currency,
            )
            invalidate_sport_cache("nhl")
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"status": "error", "error": str(exc)}), 400

    status = "stored" if stored else "duplicate"
    return (
        jsonify(
            {
                "status": status,
                "event_type": "nhl",
                "timezone": "America/New_York",
                "event_id": event_id,
                "iteration_id": iteration_id,
                "sections": len(snapshot.sections),
                "map_geometry_sections": geometry_section_count(map_geometry),
                "currency": schedule_metadata["currency"],
                "captured_at": eastern_iso(hourly_capture_slot(captured_at)),
                "compaction": compaction_report,
            }
        ),
        201 if stored else 200,
    )


def game_type_label(value: int | None) -> str:
    return {
        1: "Preseason",
        2: "Regular season",
        3: "Playoffs",
    }.get(value, "NHL game")


def format_nhl_title(event: NHLEvent) -> str:
    event_date = event_datetime_eastern(event.event_date)
    hour = event_date.hour % 12 or 12
    return (
        f"{event.title} — {event_date:%b} {event_date.day}, {event_date.year} "
        f"· {hour}:{event_date.minute:02d} {event_date:%p} {event_date:%Z}"
    )


def find_nhl_game(team_or_venue: str, identifier: str | None) -> NHLEvent | None:
    if not identifier or not str(identifier).isdigit():
        return None
    model = CreateNHLModel()
    try:
        with model.getSession()() as session:
            event = (
                session.query(NHLEvent)
                .filter(NHLEvent.id == int(identifier))
                .first()
            )
            if event is None:
                return None
            accepted = {
                nhl_event_home_team(event),
                event.venue,
                event.provider_venue,
                nhl_display_venue(event),
            }
            return event if team_or_venue in accepted else None
    finally:
        model.engine.dispose()


def nhl_map_section_data(
    event_id: int,
) -> tuple[list[dict[str, Any]], datetime | None]:
    model = CreateNHLModel()
    try:
        with model.getSession()() as session:
            event = (
                session.query(NHLEvent)
                .filter(NHLEvent.id == event_id)
                .first()
            )
            if event is None:
                return [], None
            latest = (
                session.query(NHLIteration)
                .filter(NHLIteration.event_id == event_id)
                .order_by(
                    NHLIteration.captured_at.desc(),
                    NHLIteration.id.desc(),
                )
                .first()
            )
            tickets_by_section = {
                ticket.section: ticket
                for ticket in (latest.tickets if latest else [])
            }
            section_names = sorted(
                set(event.sections or []) | set(tickets_by_section),
                key=str.casefold,
            )
            return (
                [
                    {
                        "name": name,
                        "price": (
                            tickets_by_section[name].price
                            if name in tickets_by_section
                            else None
                        ),
                        "listing_count": (
                            tickets_by_section[name].listing_count
                            if name in tickets_by_section
                            else None
                        ),
                    }
                    for name in section_names
                ],
                latest.captured_at if latest else None,
            )
    finally:
        model.engine.dispose()


def format_nhl_capture_label(value: datetime | None) -> str:
    if value is None:
        return "No snapshots stored"
    captured = value
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    return eastern_label(captured)


class NHLGraphBuilder:
    def __init__(self):
        self.plotter = GraphBuilder()

    def single_game_graph(
        self,
        home_team: str,
        event_id: int,
        section: str,
        display_mode: str,
    ) -> tuple[list[float], list[float]]:
        model = CreateNHLModel()
        try:
            with model.getSession()() as session:
                event = (
                    session.query(NHLEvent)
                    .filter(NHLEvent.id == event_id)
                    .first()
                )
                if event is None or nhl_event_home_team(event) != home_team:
                    return [], []

                tickets = (
                    session.query(NHLTicket)
                    .join(NHLTicket.iteration)
                    .join(NHLIteration.event)
                    .filter(
                        NHLTicket.section == section,
                        NHLEvent.id == event_id,
                    )
                    .order_by(NHLIteration.captured_at.asc())
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

        pairs = [
            pair
            for pair in pairs
            if 0 < pair[0] <= NHL_CAPTURE_WINDOW_HOURS
        ]
        if not pairs:
            return [], []
        x, y = map(list, zip(*pairs))
        if display_mode != "money":
            y = self.plotter.standardize(y)
        return y, x

    def create_plot(
        self,
        x: list[float],
        y: list[float],
        display_mode: str,
    ) -> str:
        return self.plotter.create_plot(x, y, display_mode)


def _event_is_completed(event: NHLEvent, now: datetime) -> bool:
    return event_datetime_eastern(event.event_date) <= now.astimezone(EASTERN)



def _nhl_home_context() -> dict[str, Any]:
    """Build the league landing page without loading section or map payloads."""

    model = CreateNHLModel()
    now = datetime.now(timezone.utc)
    try:
        with model.getSession()() as session:
            all_games = (
                session.query(NHLEvent)
                .options(
                    load_only(
                        NHLEvent.id,
                        NHLEvent.title,
                        NHLEvent.event_date,
                        NHLEvent.venue,
                        NHLEvent.home_team,
                        NHLEvent.canonical_venue,
                        NHLEvent.provider_venue,
                        NHLEvent.country,
                        NHLEvent.currency,
                        NHLEvent.game_type,
                        NHLEvent.compacted_at,
                    )
                )
                .order_by(NHLEvent.event_date)
                .all()
            )
            all_games = [
                game for game in all_games if _country_is_supported(game.country)
            ]
            upcoming_games = [game for game in all_games if not _event_is_completed(game, now)]
            completed_games = [game for game in all_games if _event_is_completed(game, now)]
            games = upcoming_games + list(reversed(completed_games))

            games_dict: dict[str, list[dict[str, str]]] = {}
            arena_game_counts: dict[str, int] = {}
            currencies: set[str] = set()
            for game in games:
                home_team = nhl_event_home_team(game)
                if home_team is None:
                    continue
                status = "Completed" if _event_is_completed(game, now) else "Upcoming"
                venue = nhl_display_venue(game)
                games_dict.setdefault(home_team, []).append(
                    {
                        "value": str(game.id),
                        "label": (
                            f"{status} · {game_type_label(game.game_type)} · "
                            f"{format_nhl_title(game)} · {venue}"
                        ),
                        "status": status.casefold(),
                        "venue": venue,
                        "currency": game.currency or "USD",
                    }
                )
                if venue:
                    arena_game_counts[venue] = arena_game_counts.get(venue, 0) + 1
                currencies.add(game.currency or "USD")

            games_dict = dict(sorted(games_dict.items()))
            arena_game_counts = dict(sorted(arena_game_counts.items()))
            compacted_count = sum(game.compacted_at is not None for game in all_games)
    finally:
        model.engine.dispose()

    return {
        "games_dict": games_dict,
        "game_sections_dict": {},
        "team_count": len(games_dict),
        "game_count": sum(len(rows) for rows in games_dict.values()),
        "section_count": 0,
        "arena_count": len(arena_game_counts),
        "arena_game_counts": arena_game_counts,
        "upcoming_count": len(upcoming_games),
        "completed_count": len(completed_games),
        "compacted_count": compacted_count,
        "currency_label": ", ".join(sorted(currencies)) if currencies else "USD",
    }


def _cached_nhl_home_context() -> dict[str, Any]:
    version = file_version(nhl_database_path())
    return page_cache.get_or_create(
        cache_key("home", "nhl", version),
        _nhl_home_context,
        ttl_seconds=PAGE_CACHE_TTL_SECONDS,
        tags=("nhl",),
    )


def _public_nhl_sections(values: list[str]) -> list[str]:
    return sorted(
        {
            _clean_text(value, maximum=180)
            for value in values or []
            if _clean_text(value, maximum=180)
            and not is_excluded_ticket_area(value)
        },
        key=str.casefold,
    )


def _nhl_options_context(home_team: str) -> dict[str, Any]:
    selected = _clean_text(home_team, maximum=180)
    if not selected:
        return {"games": [], "sections_by_game": {}}

    model = CreateNHLModel()
    now = datetime.now(timezone.utc)
    try:
        with model.getSession()() as session:
            events = (
                session.query(NHLEvent)
                .options(
                    load_only(
                        NHLEvent.id,
                        NHLEvent.title,
                        NHLEvent.event_date,
                        NHLEvent.sections,
                        NHLEvent.venue,
                        NHLEvent.home_team,
                        NHLEvent.canonical_venue,
                        NHLEvent.provider_venue,
                        NHLEvent.country,
                        NHLEvent.currency,
                        NHLEvent.game_type,
                    )
                )
                .filter(NHLEvent.home_team == selected)
                .order_by(NHLEvent.event_date)
                .all()
            )
            events = [
                event
                for event in events
                if _country_is_supported(event.country)
                and nhl_event_home_team(event) == selected
            ]
    finally:
        model.engine.dispose()

    upcoming = [event for event in events if not _event_is_completed(event, now)]
    completed = [event for event in events if _event_is_completed(event, now)]
    ordered = upcoming + list(reversed(completed))
    games = []
    sections_by_game = {}
    for event in ordered:
        status = "Completed" if _event_is_completed(event, now) else "Upcoming"
        venue = nhl_display_venue(event)
        games.append(
            {
                "value": str(event.id),
                "label": (
                    f"{status} · {game_type_label(event.game_type)} · "
                    f"{format_nhl_title(event)} · {venue}"
                ),
            }
        )
        sections_by_game[str(event.id)] = _public_nhl_sections(event.sections)
    return {"games": games, "sections_by_game": sections_by_game}


def _cached_nhl_options(home_team: str) -> dict[str, Any]:
    version = file_version(nhl_database_path())
    return page_cache.get_or_create(
        cache_key("options", "nhl", version, home_team),
        lambda: _nhl_options_context(home_team),
        ttl_seconds=OPTIONS_CACHE_TTL_SECONDS,
        tags=("nhl",),
    )



@nhl_blueprint.get("/nhl")
def nhl_home():
    return render_template("NHLHomeScreen.html", **_cached_nhl_home_context())


@nhl_blueprint.get("/api/nhl/options")
def nhl_options():
    return jsonify(_cached_nhl_options(request.args.get("team", "")))


@nhl_blueprint.get("/nhl/map")
def nhl_map():
    selection = request.args.get("team") or request.args.get("event") or ""
    event_id = request.args.get("game")
    selected = find_nhl_game(selection, event_id)
    if selected is None:
        return render_template(
            "nhl_map.html",
            error="Choose a valid tracked NHL game before opening its arena map.",
        )

    team = nhl_event_home_team(selected) or selection
    section_data, latest_capture = nhl_map_section_data(selected.id)
    if not section_data:
        return render_template(
            "nhl_map.html",
            error="No section data has been collected for that NHL game yet.",
        )

    selected_section = request.args.get("section") or ""
    known_sections = {item["name"] for item in section_data}
    if selected_section not in known_sections:
        selected_section = ""

    geometry = sanitize_map_geometry(selected.map_geometry, known_sections)
    has_provider_geometry = geometry_is_usable(geometry, known_sections)
    venue = nhl_display_venue(selected)
    map_data = {
        "team": team,
        "game": str(selected.id),
        "venue": venue,
        "currency": selected.currency or "USD",
        "sections": section_data,
        "geometry": geometry,
        "geometry_mode": "provider" if has_provider_geometry else "schematic",
        "selected_section": selected_section,
        "graph_url": url_for("nhl.nhl_graph"),
    }
    return render_template(
        "nhl_map.html",
        error=None,
        team=team,
        venue=venue,
        country=selected.country or "",
        neutral_site=selected.neutral_site is True,
        game=str(selected.id),
        gameLabel=format_nhl_title(selected),
        section_count=len(section_data),
        priced_section_count=sum(
            item["price"] is not None for item in section_data
        ),
        latest_capture_label=format_nhl_capture_label(latest_capture),
        source_url=selected.source_url,
        currency=selected.currency or "USD",
        has_provider_geometry=has_provider_geometry,
        map_geometry_source=(geometry or {}).get("source", ""),
        map_geometry_sections=geometry_section_count(geometry),
        map_data=map_data,
    )


@nhl_blueprint.get("/nhl/graph")
def nhl_graph():
    selection = request.args.get("team") or request.args.get("event") or ""
    event_id = request.args.get("game")
    section = request.args.get("section") or ""
    display_mode = (
        "percentage"
        if request.args.get("display") == "percentage"
        else "money"
    )
    selected = find_nhl_game(selection, event_id)
    team = nhl_event_home_team(selected) if selected else selection
    venue = nhl_display_venue(selected) if selected else ""
    label = format_nhl_title(selected) if selected else "Unknown NHL game"
    currency = selected.currency if selected else "USD"

    builder = NHLGraphBuilder()
    y, x = (
        builder.single_game_graph(
            team,
            selected.id,
            section,
            display_mode,
        )
        if selected and team
        else ([], [])
    )
    toggle_mode = "percentage" if display_mode == "money" else "money"
    toggle_label = "%" if display_mode == "money" else "$"

    if not x or not y:
        return render_template(
            "nhl_graph.html",
            error="No NHL price data is available for that selection.",
            team=team,
            venue=venue,
            section=section,
            game=event_id or "",
            gameLabel=label,
            currency=currency,
            displayType=toggle_mode,
            displayLabel=toggle_label,
        )

    return render_template(
        "nhl_graph.html",
        img=builder.create_plot(x, y, display_mode),
        chartX=x,
        chartY=y,
        displayMode=display_mode,
        team=team,
        venue=venue,
        section=section,
        game=str(selected.id),
        gameLabel=label,
        currency=currency,
        displayType=toggle_mode,
        displayLabel=toggle_label,
    )
