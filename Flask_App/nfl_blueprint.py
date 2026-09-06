"""NFL-specific storage, API routes, and website views.

NFL history is intentionally isolated from both the existing baseball database
and the archived concert database. Games are accepted during the final 30 days
before kickoff, with the collector choosing a 6-hour, 3-hour, or hourly cadence.
"""

from __future__ import annotations

from collections import defaultdict
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
from typing import Any, Iterable
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

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
from Flask_App.database_config import (
    configured_backend,
    create_ticket_engine,
    dispose_ticket_engine,
    is_sqlite_engine,
)
from Flask_App.performance_cache import (
    OPTIONS_CACHE_TTL_SECONDS,
    PAGE_CACHE_TTL_SECONDS,
    cache_key,
    file_version,
    page_cache,
)
from Flask_App.section_canonicalization import is_excluded_ticket_area
from Flask_App.report_policy import is_preseason, report_venue
from Flask_App.materialized_analytics import (
    ensure_summary_schema,
    refresh_event_summary_safely,
    timeline_bucket_slot,
)

# Inlined for the restricted PythonAnywhere deployment.
EASTERN = ZoneInfo("America/New_York")
MAX_GEOMETRY_SECTIONS = 500
MAX_GEOMETRY_SHAPES = 1_500
MAX_PATH_LENGTH = 24_000
MAX_TRANSFORM_LENGTH = 1_000

_VENUE_ALIASES = {
    "us bank stadium": "U.S. Bank Stadium",
    "u s bank stadium": "U.S. Bank Stadium",
    "u.s. bank stadium": "U.S. Bank Stadium",
}

_SVG_PATH_PATTERN = re.compile(r"^[MmZzLlHhVvCcSsQqTtAaEe0-9+\-.,\s]+$")
_SVG_TRANSFORM_PATTERN = re.compile(
    r"^(?:\s*(?:matrix|translate|scale|rotate|skewX|skewY)"
    r"\s*\(\s*[-+0-9eE.,\s]+\)\s*)*$"
)
_NUMBER_PATTERN = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
_SECTION_WORD_PATTERN = re.compile(
    r"\b(?:section|sections|sec|seating|seat|area|zone|block)\b",
    flags=re.IGNORECASE,
)

_LABEL_KEYS = {
    "name",
    "label",
    "title",
    "id",
    "code",
    "section",
    "sectionname",
    "sectionlabel",
    "sectionid",
    "areaname",
    "displayname",
}
_PATH_KEYS = {"d", "path", "pathdata", "svgpath", "svg_path"}
_POINTS_KEYS = {"points", "polygon", "coordinates", "vertices"}
_VIEWBOX_KEYS = {"viewbox", "view_box", "bounds"}


def clean_text(value: Any, *, maximum: int = 300) -> str:
    return " ".join(str(value or "").split())[:maximum]


def canonical_venue_name(value: Any) -> str:
    cleaned = clean_text(value)
    normalized = re.sub(r"[^a-z0-9]+", " ", cleaned.casefold()).strip()
    return _VENUE_ALIASES.get(normalized, cleaned)


def eastern_iso(value: datetime) -> str:
    """Return an ISO timestamp with the correct Eastern UTC offset.

    Collector timestamps are timezone-aware.  A naive value is treated as UTC
    only for defensive compatibility with previously stored capture slots.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(EASTERN).isoformat()


def eastern_label(value: datetime, *, include_seconds: bool = False) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(EASTERN)
    clock = "%I:%M:%S %p" if include_seconds else "%I:%M %p"
    return f"{local:%b} {local.day}, {local:%Y} · {local.strftime(clock).lstrip('0')} {local:%Z}"


def normalize_section_name(value: Any) -> str:
    text = clean_text(value, maximum=180).casefold()
    text = _SECTION_WORD_PATTERN.sub(" ", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def section_number(value: Any) -> int | None:
    matches = re.findall(r"\d+", clean_text(value, maximum=180))
    return int(matches[-1]) if matches else None


def match_section_name(candidate: Any, known_sections: Iterable[str]) -> str | None:
    known = [clean_text(item, maximum=180) for item in known_sections]
    known = [item for item in known if item]
    if not known:
        return None

    normalized_candidate = normalize_section_name(candidate)
    if not normalized_candidate:
        return None

    exact = [item for item in known if normalize_section_name(item) == normalized_candidate]
    if len(exact) == 1:
        return exact[0]

    candidate_number = section_number(candidate)
    if candidate_number is not None:
        numeric = [item for item in known if section_number(item) == candidate_number]
        if len(numeric) == 1:
            return numeric[0]

        # Prefer a unique alphanumeric match such as C136 over a bare 136.
        candidate_letters = re.sub(r"[^a-z]+", "", normalized_candidate)
        if candidate_letters:
            narrowed = [
                item
                for item in numeric
                if re.sub(r"[^a-z]+", "", normalize_section_name(item))
                == candidate_letters
            ]
            if len(narrowed) == 1:
                return narrowed[0]

    contains = [
        item
        for item in known
        if normalized_candidate in normalize_section_name(item)
        or normalize_section_name(item) in normalized_candidate
    ]
    return contains[0] if len(contains) == 1 else None


def sanitize_svg_path(value: Any) -> str | None:
    path = clean_text(value, maximum=MAX_PATH_LENGTH)
    if not path or len(path) > MAX_PATH_LENGTH:
        return None
    if not _SVG_PATH_PATTERN.fullmatch(path):
        return None
    if "m" not in path.casefold() or len(_NUMBER_PATTERN.findall(path)) < 4:
        return None
    return path


def sanitize_svg_transform(value: Any) -> str:
    transform = clean_text(value, maximum=MAX_TRANSFORM_LENGTH)
    if not transform:
        return ""
    if len(transform) > MAX_TRANSFORM_LENGTH:
        return ""
    return transform if _SVG_TRANSFORM_PATTERN.fullmatch(transform) else ""


def parse_view_box(value: Any) -> list[float] | None:
    if isinstance(value, str):
        numbers = _NUMBER_PATTERN.findall(value)
    elif isinstance(value, (list, tuple)):
        numbers = list(value)
    elif isinstance(value, dict):
        numbers = [
            value.get("x", value.get("minX", value.get("left", 0))),
            value.get("y", value.get("minY", value.get("top", 0))),
            value.get("width", value.get("w")),
            value.get("height", value.get("h")),
        ]
    else:
        return None

    if len(numbers) != 4:
        return None
    try:
        parsed = [float(number) for number in numbers]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(number) for number in parsed):
        return None
    if parsed[2] <= 0 or parsed[3] <= 0:
        return None
    if max(abs(number) for number in parsed) > 10_000_000:
        return None
    return [round(number, 5) for number in parsed]


def _points_to_path(value: Any) -> str | None:
    points: list[tuple[float, float]] = []
    if isinstance(value, str):
        numbers = _NUMBER_PATTERN.findall(value)
        if len(numbers) % 2:
            return None
        try:
            points = [
                (float(numbers[index]), float(numbers[index + 1]))
                for index in range(0, len(numbers), 2)
            ]
        except ValueError:
            return None
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, dict):
                raw_x = item.get("x", item.get("lng", item.get("left")))
                raw_y = item.get("y", item.get("lat", item.get("top")))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                raw_x, raw_y = item[0], item[1]
            else:
                continue
            try:
                points.append((float(raw_x), float(raw_y)))
            except (TypeError, ValueError):
                continue
    if len(points) < 3 or not all(
        math.isfinite(coordinate) for point in points for coordinate in point
    ):
        return None
    path = "M " + " L ".join(f"{x:g} {y:g}" for x, y in points) + " Z"
    return sanitize_svg_path(path)


def _rect_to_path(value: dict[str, Any]) -> str | None:
    try:
        x = float(value.get("x", 0))
        y = float(value.get("y", 0))
        width = float(value["width"])
        height = float(value["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return sanitize_svg_path(
        f"M {x:g} {y:g} H {x + width:g} V {y + height:g} "
        f"H {x:g} Z"
    )


def _shape_rows(value: Any) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    values = value if isinstance(value, list) else [value]
    for raw in values:
        if not isinstance(raw, dict):
            continue
        path = sanitize_svg_path(raw.get("path", raw.get("d")))
        if path is None:
            for key in _POINTS_KEYS:
                if key in raw:
                    path = _points_to_path(raw[key])
                    if path:
                        break
        if path is None and {"width", "height"}.issubset(raw):
            path = _rect_to_path(raw)
        if path is None:
            continue
        rows.append(
            {
                "path": path,
                "transform": sanitize_svg_transform(raw.get("transform")),
            }
        )
    return rows


def _infer_view_box(paths: Iterable[str]) -> list[float] | None:
    coordinates: list[float] = []
    for path in paths:
        try:
            coordinates.extend(float(number) for number in _NUMBER_PATTERN.findall(path))
        except ValueError:
            continue
    if len(coordinates) < 8:
        return None
    pairs = list(zip(coordinates[0::2], coordinates[1::2]))
    if len(pairs) < 4:
        return None
    xs = [point[0] for point in pairs]
    ys = [point[1] for point in pairs]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    if max_x <= min_x or max_y <= min_y:
        return None
    margin_x = (max_x - min_x) * 0.03
    margin_y = (max_y - min_y) * 0.03
    return parse_view_box(
        [
            min_x - margin_x,
            min_y - margin_y,
            max_x - min_x + margin_x * 2,
            max_y - min_y + margin_y * 2,
        ]
    )


def sanitize_map_geometry(
    raw: Any,
    known_sections: Iterable[str],
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    known = [clean_text(item, maximum=180) for item in known_sections]
    known = [item for item in known if item]
    if not known:
        return None

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    raw_sections = raw.get("sections")
    if isinstance(raw_sections, dict):
        section_rows = [
            {"name": name, "shapes": shapes}
            for name, shapes in raw_sections.items()
        ]
    elif isinstance(raw_sections, list):
        section_rows = raw_sections
    else:
        section_rows = []

    total_shapes = 0
    for row in section_rows[:MAX_GEOMETRY_SECTIONS * 4]:
        if not isinstance(row, dict):
            continue
        matched = match_section_name(
            row.get("name", row.get("section", row.get("label"))),
            known,
        )
        if matched is None:
            continue
        shapes = _shape_rows(row.get("shapes", row))
        for shape in shapes:
            identity = (shape["path"], shape["transform"])
            if identity in {
                (current["path"], current["transform"])
                for current in grouped[matched]
            }:
                continue
            grouped[matched].append(shape)
            total_shapes += 1
            if total_shapes >= MAX_GEOMETRY_SHAPES:
                break
        if total_shapes >= MAX_GEOMETRY_SHAPES:
            break

    if not grouped:
        return None

    all_paths = [
        shape["path"]
        for shapes in grouped.values()
        for shape in shapes
    ]
    view_box = parse_view_box(raw.get("view_box", raw.get("viewBox")))
    if view_box is None:
        view_box = _infer_view_box(all_paths)
    if view_box is None:
        return None

    source = clean_text(raw.get("source") or "provider", maximum=100)
    source_url = clean_text(raw.get("source_url"), maximum=500)
    mapped_count = len(grouped)
    result = {
        "source": source,
        "view_box": view_box,
        "sections": [
            {"name": name, "shapes": grouped[name]}
            for name in sorted(grouped, key=str.casefold)
        ],
        "mapped_section_count": mapped_count,
        "known_section_count": len(set(known)),
        "coverage_ratio": round(mapped_count / max(1, len(set(known))), 4),
    }
    if source_url:
        result["source_url"] = source_url
    return result


def geometry_is_usable(
    geometry: Any,
    known_sections: Iterable[str],
    *,
    minimum_ratio: float = 0.6,
) -> bool:
    sanitized = sanitize_map_geometry(geometry, known_sections)
    if sanitized is None:
        return False
    known_count = len({clean_text(item, maximum=180) for item in known_sections if item})
    mapped = int(sanitized["mapped_section_count"])
    required = max(4, min(12, math.ceil(known_count * minimum_ratio)))
    return mapped >= required and sanitized["coverage_ratio"] >= minimum_ratio


def geometry_section_count(geometry: Any) -> int:
    if not isinstance(geometry, dict):
        return 0
    rows = geometry.get("sections")
    return len(rows) if isinstance(rows, list) else 0


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def extract_map_geometry_from_json(
    payload: Any,
    known_sections: Iterable[str],
    *,
    source: str = "vivid-json",
    source_url: str = "",
) -> dict[str, Any] | None:
    """Find section paths in an unknown provider JSON schema.

    The walker deliberately recognizes only labels, numeric view boxes, SVG path
    strings, rectangles, and polygon-like point arrays. Everything else is
    ignored.
    """
    known = list(known_sections)
    sections: list[dict[str, Any]] = []
    view_boxes: list[list[float]] = []
    nodes_seen = 0

    def walk(value: Any, inherited_hints: tuple[Any, ...] = (), depth: int = 0) -> None:
        nonlocal nodes_seen
        if depth > 12 or nodes_seen > 20_000 or len(sections) > MAX_GEOMETRY_SHAPES:
            return
        nodes_seen += 1

        if isinstance(value, dict):
            hints = list(inherited_hints)
            for key, child in value.items():
                normalized_key = _normalized_key(key)
                if normalized_key in _LABEL_KEYS and isinstance(child, (str, int)):
                    hints.append(child)
                if normalized_key in _VIEWBOX_KEYS:
                    parsed = parse_view_box(child)
                    if parsed:
                        view_boxes.append(parsed)

            matched = next(
                (
                    match
                    for hint in reversed(hints)
                    if (match := match_section_name(hint, known)) is not None
                ),
                None,
            )
            shapes: list[dict[str, str]] = []
            if matched:
                for key, child in value.items():
                    normalized_key = _normalized_key(key)
                    path = None
                    if normalized_key in _PATH_KEYS:
                        path = sanitize_svg_path(child)
                    elif normalized_key in _POINTS_KEYS:
                        path = _points_to_path(child)
                    if path:
                        shapes.append(
                            {
                                "path": path,
                                "transform": sanitize_svg_transform(value.get("transform")),
                            }
                        )
                if not shapes and {"width", "height"}.issubset(value):
                    path = _rect_to_path(value)
                    if path:
                        shapes.append(
                            {
                                "path": path,
                                "transform": sanitize_svg_transform(value.get("transform")),
                            }
                        )
                if shapes:
                    sections.append({"name": matched, "shapes": shapes})

            for key, child in value.items():
                next_hints = tuple(hints)
                if isinstance(key, str) and match_section_name(key, known):
                    next_hints = (*next_hints, key)
                walk(child, next_hints, depth + 1)
            return

        if isinstance(value, list):
            for child in value:
                walk(child, inherited_hints, depth + 1)

    walk(payload)
    raw = {
        "source": source,
        "source_url": source_url,
        "view_box": view_boxes[0] if view_boxes else None,
        "sections": sections,
    }
    return sanitize_map_geometry(raw, known)


def extract_map_geometry_from_svg(
    svg_text: str,
    known_sections: Iterable[str],
    *,
    source: str = "vivid-svg",
    source_url: str = "",
) -> dict[str, Any] | None:
    known = list(known_sections)
    if not svg_text or len(svg_text) > 8_000_000:
        return None
    try:
        root = ElementTree.fromstring(svg_text)
    except ElementTree.ParseError:
        return None

    view_box = parse_view_box(root.attrib.get("viewBox"))
    if view_box is None:
        view_box = parse_view_box(
            [0, 0, root.attrib.get("width"), root.attrib.get("height")]
        )

    sections: list[dict[str, Any]] = []

    def local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].casefold()

    def walk(
        element: ElementTree.Element,
        inherited_hints: tuple[Any, ...] = (),
        inherited_transforms: tuple[str, ...] = (),
    ) -> None:
        hints = list(inherited_hints)
        for key, value in element.attrib.items():
            if _normalized_key(key) in _LABEL_KEYS or _normalized_key(key).startswith("data"):
                hints.append(value)
        for child in list(element):
            if local_name(child.tag) in {"title", "text"} and child.text:
                hints.append(child.text)

        transform = sanitize_svg_transform(element.attrib.get("transform"))
        transforms = (*inherited_transforms, transform) if transform else inherited_transforms
        matched = next(
            (
                match
                for hint in reversed(hints)
                if (match := match_section_name(hint, known)) is not None
            ),
            None,
        )
        tag = local_name(element.tag)
        path = None
        if tag == "path":
            path = sanitize_svg_path(element.attrib.get("d"))
        elif tag in {"polygon", "polyline"}:
            path = _points_to_path(element.attrib.get("points"))
        elif tag == "rect":
            path = _rect_to_path(element.attrib)

        if matched and path:
            sections.append(
                {
                    "name": matched,
                    "shapes": [
                        {
                            "path": path,
                            "transform": " ".join(transforms),
                        }
                    ],
                }
            )
        for child in list(element):
            walk(child, tuple(hints), transforms)

    walk(root)
    return sanitize_map_geometry(
        {
            "source": source,
            "source_url": source_url,
            "view_box": view_box,
            "sections": sections,
        },
        known,
    )


def choose_best_geometry(
    candidates: Iterable[Any],
    known_sections: Iterable[str],
) -> dict[str, Any] | None:
    known = list(known_sections)
    sanitized = [
        geometry
        for candidate in candidates
        if (geometry := sanitize_map_geometry(candidate, known)) is not None
    ]
    if not sanitized:
        return None
    return max(
        sanitized,
        key=lambda geometry: (
            int(geometry.get("mapped_section_count") or 0),
            float(geometry.get("coverage_ratio") or 0),
            sum(len(row.get("shapes") or []) for row in geometry.get("sections") or []),
        ),
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

    if not is_sqlite_engine(engine):
        return
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

            international_predicate = (
                "country IS NOT NULL AND TRIM(country) <> '' AND "
                "LOWER(REPLACE(TRIM(country), '.', '')) NOT IN "
                "('us', 'usa', 'united states', 'united states of america')"
            )
            connection.exec_driver_sql(
                "DELETE FROM nfl_tickets WHERE iteration_id IN ("
                "SELECT id FROM nfl_iterations WHERE event_id IN ("
                f"SELECT id FROM nfl_event WHERE {international_predicate}))"
            )
            connection.exec_driver_sql(
                "DELETE FROM nfl_iterations WHERE event_id IN ("
                f"SELECT id FROM nfl_event WHERE {international_predicate})"
            )
            connection.exec_driver_sql(
                f"DELETE FROM nfl_event WHERE {international_predicate}"
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
    """Open the configured NFL database, preserving explicit SQLite test files."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or nfl_database_path()).expanduser().resolve()
        self.engine = create_ticket_engine(
            "nfl",
            sqlite_path=self.db_path,
            force_sqlite=db_path is not None,
        )
        if is_sqlite_engine(self.engine):
            NFLBase.metadata.create_all(self.engine)
            _ensure_nfl_schema(self.engine, self.db_path)
        ensure_summary_schema(self.engine)
        self.SessionLocal = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
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
    return report_venue(event.canonical_venue or canonical_venue_name(
        event.provider_venue or event.venue
    ))


def is_nfl_game_title(title: str) -> bool:
    normalized = " ".join(str(title or "").split()).casefold()
    has_matchup_separator = any(
        separator in normalized for separator in (" at ", " vs ", " vs. ", " versus ")
    )
    return nfl_matchup_teams(title) is not None and has_matchup_separator


def _clean_metadata_text(value: Any, maximum: int = 250) -> str:
    return " ".join(str(value or "").split())[:maximum]


_US_COUNTRY_MARKERS = frozenset(
    {"us", "usa", "united states", "united states of america"}
)


def _country_is_explicitly_non_us(value: Any) -> bool:
    country = _clean_metadata_text(value).casefold().replace(".", "")
    return bool(country) and country not in _US_COUNTRY_MARKERS


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
    city = _clean_metadata_text(raw.get("city"))
    country = _clean_metadata_text(raw.get("country"))
    if _country_is_explicitly_non_us(country):
        raise ValueError(
            "International NFL games are outside the U.S.-venue collection scope."
        )
    return {
        "schedule_id": schedule_id or None,
        "away_team": away_team,
        "home_team": home_team,
        "canonical_venue": canonical_venue,
        "city": city,
        "country": country,
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
    """Store raw NFL data first, then refresh only its affected time window.

    Materialized analytics are derived data. A refresh failure must never make a
    valid raw snapshot disappear, so the summary update uses a second, nonfatal
    transaction and can be repaired by the maintenance backfill.
    """

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
        SessionLocal = model.getSession()
        with SessionLocal() as session:
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
            session.flush()
            event_id = int(event.id)
            iteration_id = int(iteration.id)
            summary_event_date = event.event_date
            summary_venue = (
                event.canonical_venue or event.provider_venue or event.venue
            )
            slot = timeline_bucket_slot(
                "nfl", summary_event_date, iteration.captured_at
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

        if slot is not None:
            refresh_event_summary_safely(
                SessionLocal,
                sport_key="nfl",
                event_id=event_id,
                event_date=summary_event_date,
                venue=summary_venue,
                iteration_model=NFLIteration,
                ticket_model=NFLTicket,
                bucket_slots=(slot,),
            )
        return event_id, iteration_id, True
    finally:
        dispose_ticket_engine(model.engine)

def create_nfl_daily_backup(
    now: datetime | None = None,
    source: Path | None = None,
    backup_dir: Path = DEFAULT_NFL_BACKUP_DIR,
) -> Path:
    now = now or datetime.now(timezone.utc)
    source = Path(source or nfl_database_path()).expanduser().resolve()
    if not source.exists():
        model = CreateNFLModel(source)
        dispose_ticket_engine(model.engine)

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
        from types import SimpleNamespace
        candidate = SimpleNamespace(title=snapshot.title, event_date=event_date, **{
            key: schedule_metadata.get(key) for key in ("game_type", "schedule_id")})
        candidate.game_type = (payload.get("schedule") or {}).get("season_type")
        if is_preseason("nfl", candidate):
            return jsonify({"status": "ignored", "reason": "preseason"}), 200
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

        if configured_backend() == "sqlite":
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
            if event is None or is_preseason("nfl", event):
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
        dispose_ticket_engine(model.engine)


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
        dispose_ticket_engine(model.engine)


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
            dispose_ticket_engine(model.engine)

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



def _nfl_home_context() -> dict[str, Any]:
    """Expose the game directory without loading every section or map payload."""

    model = CreateNFLModel()
    now = datetime.now(timezone.utc)
    try:
        with model.getSession()() as session:
            all_games = [
                game
                for game in (
                    session.query(NFLEvent)
                    .options(
                        load_only(
                            NFLEvent.id,
                            NFLEvent.title,
                            NFLEvent.event_date,
                            NFLEvent.venue,
                            NFLEvent.home_team,
                            NFLEvent.canonical_venue,
                            NFLEvent.provider_venue,
                            NFLEvent.country,
                        )
                    )
                    .order_by(NFLEvent.event_date)
                    .all()
                )
                if not _country_is_explicitly_non_us(game.country)
            ]
            from Flask_App.nfl_stadium_blueprint import _generic_venue_index
            team_reports = _generic_venue_index(all_games, now,
                venue_getter=nfl_display_venue, team_getter=nfl_event_home_team,
                endpoint="nfl_stadium.nfl_stadium")
            all_games = [game for game in all_games if not is_preseason("nfl", game)]
            upcoming_games = [game for game in all_games if not _event_is_completed(game, now)]
            completed_games = [game for game in all_games if _event_is_completed(game, now)]
            games = upcoming_games + list(reversed(completed_games))

            games_dict: dict[str, list[dict[str, str]]] = {}
            stadium_game_counts: dict[str, int] = {}
            for game in games:
                home_team = nfl_event_home_team(game)
                if home_team is None:
                    continue
                completed = _event_is_completed(game, now)
                status = "Completed" if completed else "Upcoming"
                venue = nfl_display_venue(game)
                games_dict.setdefault(home_team, []).append(
                    {
                        "value": str(game.id),
                        "label": f"{status} · {format_nfl_title(game)} · {venue}",
                        "status": status.casefold(),
                        "venue": venue,
                    }
                )
                if venue:
                    stadium_game_counts[venue] = stadium_game_counts.get(venue, 0) + 1

            games_dict = dict(sorted(games_dict.items()))
            stadium_game_counts = dict(sorted(stadium_game_counts.items()))
    finally:
        dispose_ticket_engine(model.engine)

    return {
        "games_dict": games_dict,
        "team_reports": team_reports,
        "game_sections_dict": {},
        "team_count": len(games_dict),
        "game_count": sum(len(rows) for rows in games_dict.values()),
        "section_count": 0,
        "stadium_count": len(stadium_game_counts),
        "stadium_game_counts": stadium_game_counts,
        "upcoming_count": len(upcoming_games),
        "completed_count": len(completed_games),
    }


def _cached_nfl_home_context() -> dict[str, Any]:
    version = file_version(nfl_database_path())
    return page_cache.get_or_create(
        cache_key("home", "nfl", version),
        _nfl_home_context,
        ttl_seconds=PAGE_CACHE_TTL_SECONDS,
        tags=("nfl",),
    )


def _public_nfl_sections(values: Iterable[str]) -> list[str]:
    return sorted(
        {
            clean_text(value, maximum=180)
            for value in values or []
            if clean_text(value, maximum=180)
            and not is_excluded_ticket_area(value)
        },
        key=str.casefold,
    )


def _nfl_options_context(home_team: str) -> dict[str, Any]:
    selected = clean_text(home_team, maximum=180)
    if not selected:
        return {"games": [], "sections_by_game": {}}

    model = CreateNFLModel()
    now = datetime.now(timezone.utc)
    try:
        with model.getSession()() as session:
            events = (
                session.query(NFLEvent)
                .options(
                    load_only(
                        NFLEvent.id,
                        NFLEvent.title,
                        NFLEvent.event_date,
                        NFLEvent.sections,
                        NFLEvent.venue,
                        NFLEvent.home_team,
                        NFLEvent.canonical_venue,
                        NFLEvent.provider_venue,
                        NFLEvent.country,
                    )
                )
                .filter(NFLEvent.home_team == selected)
                .order_by(NFLEvent.event_date)
                .all()
            )
            events = [
                event
                for event in events
                if not _country_is_explicitly_non_us(event.country)
                and nfl_event_home_team(event) == selected
                and not is_preseason("nfl", event)
            ]
    finally:
        dispose_ticket_engine(model.engine)

    upcoming = [event for event in events if not _event_is_completed(event, now)]
    completed = [event for event in events if _event_is_completed(event, now)]
    ordered = upcoming + list(reversed(completed))
    games = []
    sections_by_game = {}
    for event in ordered:
        status = "Completed" if _event_is_completed(event, now) else "Upcoming"
        venue = nfl_display_venue(event)
        games.append(
            {
                "value": str(event.id),
                "label": f"{status} · {format_nfl_title(event)} · {venue}",
            }
        )
        sections_by_game[str(event.id)] = _public_nfl_sections(event.sections)
    return {"games": games, "sections_by_game": sections_by_game}


def _cached_nfl_options(home_team: str) -> dict[str, Any]:
    version = file_version(nfl_database_path())
    return page_cache.get_or_create(
        cache_key("options", "nfl", version, home_team),
        lambda: _nfl_options_context(home_team),
        ttl_seconds=OPTIONS_CACHE_TTL_SECONDS,
        tags=("nfl",),
    )



@nfl_blueprint.get("/nfl")
def nfl_home():
    return render_template("NFLHomeScreen.html", **_cached_nfl_home_context())


@nfl_blueprint.get("/api/nfl/options")
def nfl_options():
    return jsonify(_cached_nfl_options(request.args.get("team", "")))


@nfl_blueprint.get("/nfl/archive")
def nfl_archive():
    """Retain old bookmarks while serving the unified NFL history."""
    return nfl_home()



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
