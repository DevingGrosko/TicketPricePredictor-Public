"""Shared NFL schedule metadata, Eastern-time, and seating-map helpers.

The collector is allowed to observe public provider map responses, but only a
small, sanitized geometry representation reaches storage or the browser.  No
provider HTML, scripts, styles, or arbitrary SVG markup are persisted.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import math
import re
from typing import Any, Iterable
from xml.etree import ElementTree
from zoneinfo import ZoneInfo


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
