"""NFL-specific storage, API routes, and website views.

NFL history is intentionally isolated from both the existing baseball database
and the archived concert database. Each game receives at most one observation
per UTC hour during the final seven days before kickoff.
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
from typing import Any

from flask import Blueprint, jsonify, render_template, request
from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint, create_engine
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


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_NFL_DATABASE = PROJECT_DIR / "NFL-collection.db"
DEFAULT_NFL_AUDIT_DIR = PROJECT_DIR / "nfl_audit"
DEFAULT_NFL_BACKUP_DIR = PROJECT_DIR / "nfl_backups"
NFL_BACKUP_RETENTION_DAYS = 7
NFL_AUDIT_RETENTION_DAYS = 30
NFL_CAPTURE_WINDOW_HOURS = 7 * 24
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
    venue: Mapped[str] = mapped_column(String, nullable=False, index=True)

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
        self.SessionLocal = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )

    def getSession(self):
        return self.SessionLocal


def hourly_capture_slot(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def is_nfl_game_title(title: str) -> bool:
    normalized = " ".join(str(title or "").split()).casefold()
    if not normalized or any(marker in normalized for marker in NON_GAME_MARKERS):
        return False

    team_count = sum(name.casefold() in normalized for name in NFL_TEAM_NAMES)
    has_matchup_separator = any(
        separator in normalized for separator in (" at ", " vs ", " vs. ", " versus ")
    )
    return team_count >= 2 and has_matchup_separator


def nfl_snapshot_from_payload(payload: dict[str, Any]):
    if payload.get("event_type") != "nfl":
        raise ValueError("NFL endpoint only accepts NFL snapshots.")

    url, event_date, captured_at, snapshot = snapshot_from_payload(payload)
    if NFL_URL_PATTERN.search(url) is None:
        raise ValueError("NFL snapshot URL is missing a Vivid production ID.")
    if not is_nfl_game_title(snapshot.title):
        raise ValueError("NFL endpoint only accepts actual NFL game matchups.")
    return url, event_date, captured_at, snapshot


def store_nfl_snapshot(
    url: str,
    event_date: datetime,
    snapshot: Any,
    captured_at: datetime,
    *,
    db_path: str | Path | None = None,
) -> tuple[int, int, bool]:
    model = CreateNFLModel(db_path)
    stored_event_date = event_datetime_for_storage(event_date)
    stored_captured_at = captured_datetime_for_storage(hourly_capture_slot(captured_at))

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
    target = backup_dir / f"NFL-collection-{now:%Y-%m-%d}.db"
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
) -> Path:
    audit_dir.mkdir(parents=True, exist_ok=True)
    normalized_capture = hourly_capture_slot(captured_at)
    path = audit_dir / f"{normalized_capture:%Y-%m-%d}.jsonl"
    record = {
        "schema_version": 1,
        "event_type": "nfl",
        "captured_at": normalized_capture.isoformat(),
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
        url, event_date, captured_at, snapshot = nfl_snapshot_from_payload(payload)
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
            raise ValueError("The NFL game is outside the seven-day capture window.")

        create_nfl_daily_backup(now=now)
        event_id, iteration_id, stored = store_nfl_snapshot(
            url, event_date, snapshot, captured_at
        )
        if stored:
            write_nfl_audit(
                url,
                event_date,
                snapshot,
                event_id,
                iteration_id,
                captured_at,
            )
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"status": "error", "error": str(exc)}), 400

    status = "stored" if stored else "duplicate"
    return jsonify(
        {
            "status": status,
            "event_type": "nfl",
            "event_id": event_id,
            "iteration_id": iteration_id,
            "sections": len(snapshot.sections),
            "captured_at": hourly_capture_slot(captured_at).isoformat(),
        }
    ), 201 if stored else 200


def format_nfl_title(event: NFLEvent) -> str:
    event_date = event_datetime_eastern(event.event_date)
    hour = event_date.hour % 12 or 12
    return (
        f"{event.title} — {event_date:%b} {event_date.day}, {event_date.year} "
        f"· {hour}:{event_date.minute:02d} {event_date:%p}"
    )


def find_nfl_game(venue: str, identifier: str | None) -> NFLEvent | None:
    if not identifier or not str(identifier).isdigit():
        return None
    model = CreateNFLModel()
    try:
        with model.getSession()() as session:
            return (
                session.query(NFLEvent)
                .filter(
                    NFLEvent.venue == venue,
                    NFLEvent.id == int(identifier),
                )
                .first()
            )
    finally:
        model.engine.dispose()


class NFLGraphBuilder:
    def __init__(self):
        self.plotter = GraphBuilder()

    def single_game_graph(
        self,
        venue: str,
        event_id: int,
        section: str,
        display_mode: str,
    ) -> tuple[list[float], list[float]]:
        model = CreateNFLModel()
        try:
            with model.getSession()() as session:
                event = (
                    session.query(NFLEvent)
                    .filter(NFLEvent.venue == venue, NFLEvent.id == event_id)
                    .first()
                )
                if event is None:
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


@nfl_blueprint.get("/nfl")
def nfl_home():
    model = CreateNFLModel()
    try:
        with model.getSession()() as session:
            games = session.query(NFLEvent).order_by(NFLEvent.event_date).all()
            games_dict: dict[str, list[dict[str, str]]] = {}
            game_sections_dict: dict[str, dict[str, list[str]]] = {}
            for game in games:
                games_dict.setdefault(game.venue, []).append(
                    {"value": str(game.id), "label": format_nfl_title(game)}
                )
                game_sections_dict.setdefault(game.venue, {})[
                    str(game.id)
                ] = sorted(set(game.sections or []))

            stadium_count = len(games_dict)
            game_count = len(games)
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

    return render_template(
        "NFLHomeScreen.html",
        games_dict=games_dict,
        game_sections_dict=game_sections_dict,
        stadium_count=stadium_count,
        game_count=game_count,
        section_count=section_count,
    )


@nfl_blueprint.get("/nfl/graph")
def nfl_graph():
    venue = request.args.get("event") or ""
    event_id = request.args.get("game")
    section = request.args.get("section") or ""
    display_mode = "percentage" if request.args.get("display") == "percentage" else "money"
    selected = find_nfl_game(venue, event_id)
    label = format_nfl_title(selected) if selected else "Unknown NFL game"

    builder = NFLGraphBuilder()
    y, x = (
        builder.single_game_graph(venue, selected.id, section, display_mode)
        if selected
        else ([], [])
    )
    toggle_mode = "percentage" if display_mode == "money" else "money"
    toggle_label = "%" if display_mode == "money" else "$"

    if not x or not y:
        return render_template(
            "nfl_graph.html",
            error="No NFL price data is available for that selection.",
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
        venue=venue,
        section=section,
        game=str(selected.id),
        gameLabel=label,
        displayType=toggle_mode,
        displayLabel=toggle_label,
    )
