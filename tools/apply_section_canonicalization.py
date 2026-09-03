from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = ROOT / "Flask_App/nfl_stadium_blueprint.py"


def replace_function(source: str, name: str, replacement: str) -> str:
    tree = ast.parse(source)
    nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    if len(nodes) != 1:
        raise RuntimeError(f"Expected one function {name}, found {len(nodes)}")
    node = nodes[0]
    lines = source.splitlines(keepends=True)
    return "".join(
        lines[: node.lineno - 1]
        + [replacement.rstrip() + "\n"]
        + lines[node.end_lineno :]
    )


def function_segment(source: str, name: str) -> tuple[list[str], ast.FunctionDef, str]:
    tree = ast.parse(source)
    nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(nodes) != 1:
        raise RuntimeError(f"Expected one function {name}, found {len(nodes)}")
    node = nodes[0]
    lines = source.splitlines(keepends=True)
    return lines, node, "".join(lines[node.lineno - 1 : node.end_lineno])


def replace_in_function(
    source: str,
    function_name: str,
    old: str,
    new: str,
    *,
    required: bool = True,
) -> str:
    lines, node, segment = function_segment(source, function_name)
    if old not in segment:
        if required:
            raise RuntimeError(f"Pattern missing in {function_name}: {old!r}")
        return source
    segment = segment.replace(old, new)
    return "".join(lines[: node.lineno - 1] + [segment] + lines[node.end_lineno :])


def add_sport_key_to_call(source: str, function_name: str, sport_key: str) -> str:
    lines, node, segment = function_segment(source, function_name)
    needle = "_generic_venue_index(\n                events,\n                now,\n"
    if needle not in segment:
        raise RuntimeError(f"Generic index call not found in {function_name}")
    segment = segment.replace(
        needle,
        needle + f'                sport_key="{sport_key}",\n',
        1,
    )
    return "".join(lines[: node.lineno - 1] + [segment] + lines[node.end_lineno :])


def update_blueprint() -> None:
    text = BLUEPRINT.read_text()

    if "from types import SimpleNamespace" not in text:
        text = text.replace(
            "from statistics import mean, median\n",
            "from statistics import mean, median\nfrom types import SimpleNamespace\n",
            1,
        )

    canonical_import = '''from Flask_App.section_canonicalization import (\n    canonical_section_key,\n    canonical_section_label,\n    canonicalize_section_labels,\n    preferred_section_label,\n    section_identity,\n)\n'''
    if "from Flask_App.section_canonicalization import" not in text:
        marker = '\n\nnfl_stadium_blueprint = Blueprint("nfl_stadium", __name__)'
        if marker not in text:
            raise RuntimeError("Blueprint marker missing")
        text = text.replace(marker, "\n\n" + canonical_import + marker, 1)

    text = replace_function(
        text,
        "_public_sections",
        '''def _public_sections(\n    values: Iterable[Any],\n    *,\n    sport_key: str = "",\n    venue: str = "",\n) -> list[str]:\n    """Return conservative venue-scoped canonical ticket-area labels."""\n\n    return canonicalize_section_labels(\n        sport_key or "unknown",\n        venue or "unknown",\n        values,\n    )''',
    )

    text = replace_function(
        text,
        "_stadium_index",
        '''def _stadium_index(\n    events: Iterable[NFLEvent],\n    now: datetime,\n) -> list[dict[str, Any]]:\n    grouped: dict[str, list[NFLEvent]] = defaultdict(list)\n    for event in events:\n        venue = _clean(nfl_display_venue(event))\n        if venue and _is_us_event(event):\n            grouped[venue].append(event)\n\n    result = []\n    for venue, venue_events in grouped.items():\n        sections = canonicalize_section_labels(\n            "nfl",\n            venue,\n            [\n                section\n                for event in venue_events\n                for section in (getattr(event, "sections", None) or [])\n            ],\n        )\n        teams = sorted(\n            {\n                _clean(nfl_event_home_team(event))\n                for event in venue_events\n                if _clean(nfl_event_home_team(event))\n            }\n        )\n        completed_count = sum(_event_completed(event, now) for event in venue_events)\n        result.append(\n            {\n                "venue": venue,\n                "game_count": len(venue_events),\n                "completed_count": completed_count,\n                "upcoming_count": len(venue_events) - completed_count,\n                "section_count": len(sections),\n                "team_label": " / ".join(teams[:2]),\n                "url": url_for("nfl_stadium.nfl_stadium", venue=venue),\n            }\n        )\n    return sorted(\n        result,\n        key=lambda row: (\n            (row["team_label"] or row["venue"]).casefold(),\n            row["venue"].casefold(),\n        ),\n    )''',
    )

    text = replace_function(
        text,
        "_generic_venue_index",
        '''def _generic_venue_index(\n    events: Iterable[Any],\n    now: datetime,\n    *,\n    sport_key: str,\n    venue_getter: Callable[[Any], str],\n    team_getter: Callable[[Any], str | None],\n    section_getter: Callable[[Any], Iterable[str]],\n    endpoint: str,\n) -> list[dict[str, Any]]:\n    grouped: dict[str, list[Any]] = defaultdict(list)\n    for event in events:\n        venue = _clean(venue_getter(event))\n        if venue:\n            grouped[venue].append(event)\n\n    result = []\n    for venue, venue_events in grouped.items():\n        sections = canonicalize_section_labels(\n            sport_key,\n            venue,\n            [section for event in venue_events for section in section_getter(event)],\n        )\n        team_label = _team_label(venue_events, team_getter)\n        completed_count = sum(_event_completed(event, now) for event in venue_events)\n        result.append(\n            {\n                "venue": venue,\n                "game_count": len(venue_events),\n                "completed_count": completed_count,\n                "upcoming_count": len(venue_events) - completed_count,\n                "section_count": len(sections),\n                "team_label": team_label,\n                "url": url_for(endpoint, venue=venue),\n            }\n        )\n    return sorted(\n        result,\n        key=lambda row: (\n            (row["team_label"] or row["venue"]).casefold(),\n            row["venue"].casefold(),\n        ),\n    )''',
    )

    text = add_sport_key_to_call(text, "build_mlb_stadium_context", "mlb")
    text = add_sport_key_to_call(text, "build_nhl_arena_context", "nhl")

    helpers = '''\n\ndef _event_venue_for_sport(event: Any, sport_key: str) -> str:\n    if sport_key == "mlb":\n        return _clean(getattr(event, "Place", ""))\n    if sport_key == "nfl":\n        return _clean(nfl_display_venue(event))\n    if sport_key == "nhl":\n        return _clean(nhl_display_venue(event))\n    return ""\n\n\ndef _row_value(row: Any, name: str) -> Any:\n    if hasattr(row, name):\n        return getattr(row, name)\n    mapping = getattr(row, "_mapping", None)\n    if mapping is not None and name in mapping:\n        return mapping[name]\n    if isinstance(row, dict):\n        return row[name]\n    raise KeyError(name)\n\n\ndef _canonicalize_snapshot_rows(\n    events: Iterable[Any],\n    rows: Iterable[Any],\n    sport_key: str,\n) -> list[Any]:\n    """Canonicalize labels and keep one cheapest price per capture/area."""\n\n    event_by_id = {int(event.id): event for event in events}\n    grouped: dict[tuple[int, Any, str], dict[str, Any]] = {}\n    raw_labels_by_key: dict[str, set[str]] = defaultdict(set)\n\n    for row in rows:\n        event_id = int(_row_value(row, "event_id"))\n        event = event_by_id.get(event_id)\n        if event is None:\n            continue\n        raw_section = _clean(_row_value(row, "section"))\n        identity = section_identity(\n            sport_key,\n            _event_venue_for_sport(event, sport_key),\n            raw_section,\n        )\n        if identity is None:\n            continue\n        captured_at = _row_value(row, "captured_at")\n        price = _row_value(row, "price")\n        if price is None:\n            continue\n        raw_labels_by_key[identity.key].add(identity.raw_label)\n        group_key = (event_id, captured_at, identity.key)\n        current = grouped.get(group_key)\n        if current is None or float(price) < float(current["price"]):\n            grouped[group_key] = {\n                "event_id": event_id,\n                "captured_at": captured_at,\n                "canonical_key": identity.key,\n                "price": price,\n            }\n\n    display_by_key = {\n        key: preferred_section_label(key, labels)\n        for key, labels in raw_labels_by_key.items()\n    }\n    result = [\n        SimpleNamespace(\n            event_id=value["event_id"],\n            captured_at=value["captured_at"],\n            section=display_by_key[value["canonical_key"]],\n            canonical_section_key=value["canonical_key"],\n            price=value["price"],\n        )\n        for value in grouped.values()\n    ]\n    return sorted(\n        result,\n        key=lambda row: (row.event_id, row.captured_at, row.section.casefold()),\n    )\n'''

    if "def _canonicalize_snapshot_rows(" not in text:
        tree = ast.parse(text)
        nodes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_snapshot_rows_for"
        ]
        if len(nodes) != 1:
            raise RuntimeError("_snapshot_rows_for not found")
        node = nodes[0]
        lines = text.splitlines(keepends=True)
        text = "".join(lines[: node.end_lineno] + [helpers] + lines[node.end_lineno :])

    if "rows = _canonicalize_snapshot_rows(events, rows, sport_key)" not in text:
        tree = ast.parse(text)
        nodes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_section_insights_for"
        ]
        if len(nodes) != 1:
            raise RuntimeError("_section_insights_for not found")
        node = nodes[0]
        arg_names = [arg.arg for arg in node.args.args] + [
            arg.arg for arg in node.args.kwonlyargs
        ]
        if "sport_key" not in arg_names:
            raise RuntimeError("_section_insights_for has no sport_key parameter")
        first = node.body[0] if node.body else None
        insert_after = (
            first.end_lineno
            if isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
            else node.lineno
        )
        lines = text.splitlines(keepends=True)
        lines.insert(
            insert_after,
            "    rows = _canonicalize_snapshot_rows(events, rows, sport_key)\n",
        )
        text = "".join(lines)

    helper_marker = '"is_parking_section": is_parking_section,'
    if '"canonical_section_key": canonical_section_key,' not in text:
        if helper_marker not in text:
            raise RuntimeError("Context helper marker missing")
        text = text.replace(
            helper_marker,
            helper_marker
            + '\n        "canonical_section_key": canonical_section_key,'
            + '\n        "canonical_section_label": canonical_section_label,'
            + '\n        "canonical_section_labels": canonicalize_section_labels,',
            1,
        )

    text = replace_in_function(
        text,
        "build_nfl_stadium_context",
        'public_sections = _public_sections(\n            getattr(event, "sections", None) or []\n        )',
        'public_sections = _public_sections(\n            getattr(event, "sections", None) or [],\n            sport_key="nfl",\n            venue=selected,\n        )',
        required=False,
    )
    text = replace_in_function(
        text,
        "build_mlb_stadium_context",
        "public_sections = _public_sections(event.event_sections or [])",
        'public_sections = _public_sections(\n            event.event_sections or [],\n            sport_key="mlb",\n            venue=selected,\n        )',
        required=False,
    )
    text = replace_in_function(
        text,
        "build_nhl_arena_context",
        'public_sections = _public_sections(\n            getattr(event, "sections", None) or []\n        )',
        'public_sections = _public_sections(\n            getattr(event, "sections", None) or [],\n            sport_key="nhl",\n            venue=selected,\n        )',
        required=False,
    )

    ast.parse(text)
    BLUEPRINT.write_text(text)


def update_templates() -> None:
    path = ROOT / "Flask_App/templates/HomeScreen.html"
    text = path.read_text()
    text = text.replace(
        "{% for by_game in game_sections_dict.values() %}",
        "{% for place, by_game in game_sections_dict.items() %}",
        1,
    )
    text = text.replace(
        '''        {% if not is_parking_section(section) and section not in visible_sections.names %}\n          {% set visible_sections.names = visible_sections.names + [section] %}\n        {% endif %}''',
        '''        {% set canonical = canonical_section_key('mlb', place, section) %}\n        {% if canonical and canonical not in visible_sections.names %}\n          {% set visible_sections.names = visible_sections.names + [canonical] %}\n        {% endif %}''',
        1,
    )
    text = text.replace(
        '''            {% if not is_parking_section(section) and section not in section_ns.names %}{% set section_ns.names = section_ns.names + [section] %}{% endif %}''',
        '''            {% set canonical = canonical_section_key('mlb', place, section) %}\n            {% if canonical and canonical not in section_ns.names %}{% set section_ns.names = section_ns.names + [canonical] %}{% endif %}''',
    )
    text = text.replace("<dd>sections</dd>", "<dd>tracked areas</dd>")
    text = text.replace("<dd>Sections compared</dd>", "<dd>Tracked areas</dd>")
    path.write_text(text)

    path = ROOT / "Flask_App/templates/NFLHomeScreen.html"
    text = path.read_text()
    text = text.replace(
        '''                {% if not is_parking_section(section) and section not in section_ns.names %}{% set section_ns.names = section_ns.names + [section] %}{% endif %}''',
        '''                {% set canonical = canonical_section_key('nfl', venue, section) %}\n                {% if canonical and canonical not in section_ns.names %}{% set section_ns.names = section_ns.names + [canonical] %}{% endif %}''',
    )
    text = text.replace("<dd>sections</dd>", "<dd>tracked areas</dd>")
    path.write_text(text)

    path = ROOT / "Flask_App/templates/NHLHomeScreen.html"
    text = path.read_text()
    text = text.replace(
        '''                {% if not is_parking_section(section) and section not in section_ns.names %}{% set section_ns.names = section_ns.names + [section] %}{% endif %}''',
        '''                {% set canonical = canonical_section_key('nhl', venue, section) %}\n                {% if canonical and canonical not in section_ns.names %}{% set section_ns.names = section_ns.names + [canonical] %}{% endif %}''',
    )
    text = text.replace("<dd>sections</dd>", "<dd>tracked areas</dd>")
    path.write_text(text)

    path = ROOT / "Flask_App/templates/nfl_stadium.html"
    text = path.read_text()
    text = text.replace(
        "<div><dt>{{ stadium.section_count }}</dt><dd>sections</dd></div>",
        "<div><dt>{{ stadium.section_count }}</dt><dd>tracked areas</dd></div>",
    )
    text = text.replace(
        "<div><dt>{{ section_count }}</dt><dd>Sections</dd></div>",
        "<div><dt>{{ section_count }}</dt><dd>Tracked areas</dd></div>",
    )
    path.write_text(text)


def update_deploy_allowlist() -> None:
    path = ROOT / ".github/workflows/deploy-pythonanywhere.yml"
    text = path.read_text()
    if "Flask_App/section_canonicalization.py" in text:
        return

    lines = text.splitlines()
    output: list[str] = []
    inserted = 0
    for line in lines:
        output.append(line)
        if "Flask_App/nfl_stadium_blueprint.py" not in line:
            continue
        indent = line[: len(line) - len(line.lstrip())]
        stripped = line.strip()
        if stripped.endswith("\\"):
            output.append(indent + "Flask_App/section_canonicalization.py \\")
        elif stripped.startswith("- "):
            output.append(indent + "- Flask_App/section_canonicalization.py")
        else:
            output.append(indent + "Flask_App/section_canonicalization.py")
        inserted += 1
    if not inserted:
        raise RuntimeError("Deploy workflow does not mention nfl_stadium_blueprint.py")
    path.write_text("\n".join(output) + "\n")


def main() -> None:
    update_blueprint()
    update_templates()
    update_deploy_allowlist()


if __name__ == "__main__":
    main()
