from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = ROOT / "Flask_App/nfl_stadium_blueprint.py"


def _function_node(source: str, name: str) -> ast.FunctionDef:
    tree = ast.parse(source)
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one function named {name}, found {len(matches)}")
    return matches[0]


def _function_segment(source: str, name: str) -> str:
    node = _function_node(source, name)
    lines = source.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1 : node.end_lineno])


def _replace_in_function(
    source: str,
    function_name: str,
    old: str,
    new: str,
) -> str:
    node = _function_node(source, function_name)
    lines = source.splitlines(keepends=True)
    segment = "".join(lines[node.lineno - 1 : node.end_lineno])
    if old not in segment:
        raise RuntimeError(f"Pattern missing from {function_name}: {old!r}")
    segment = segment.replace(old, new, 1)
    return "".join(lines[: node.lineno - 1] + [segment] + lines[node.end_lineno :])


def _insert_after_section_assignment(
    source: str,
    function_name: str,
    statement: str,
) -> str:
    if statement in _function_segment(source, function_name):
        return source

    function = _function_node(source, function_name)
    matches: list[ast.Assign] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Tuple):
            continue
        names = [item.id for item in target.elts if isinstance(item, ast.Name)]
        if names == ["sections", "captures_by_event"]:
            matches.append(node)
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one sections/captures assignment in {function_name}, found {len(matches)}"
        )

    assignment = matches[0]
    lines = source.splitlines(keepends=True)
    assignment_line = lines[assignment.lineno - 1]
    indent = assignment_line[: len(assignment_line) - len(assignment_line.lstrip())]
    lines.insert(assignment.end_lineno, f"{indent}{statement}\n")
    return "".join(lines)


def _require_replace(text: str, old: str, new: str, *, label: str) -> str:
    if old not in text:
        raise RuntimeError(f"Expected {label} pattern was not found")
    return text.replace(old, new, 1)


def update_blueprint() -> None:
    text = BLUEPRINT.read_text()

    canonical_import = "from Flask_App.section_canonicalization import section_identity\n"
    if canonical_import not in text:
        marker = '\n\nnfl_stadium_blueprint = Blueprint("nfl_stadium", __name__)'
        if marker not in text:
            raise RuntimeError("Blueprint declaration marker not found")
        text = text.replace(marker, "\n" + canonical_import + marker, 1)

    helpers = '''\n\ndef _event_venue_for_sport(event: Any, sport_key: str) -> str:\n    if sport_key == "mlb":\n        return _clean(getattr(event, "Place", ""))\n    if sport_key == "nfl":\n        return _clean(nfl_display_venue(event))\n    if sport_key == "nhl":\n        return _clean(nhl_display_venue(event))\n    return ""\n\n\ndef _row_value(row: Any, name: str) -> Any:\n    if hasattr(row, name):\n        return getattr(row, name)\n    mapping = getattr(row, "_mapping", None)\n    if mapping is not None:\n        try:\n            return mapping[name]\n        except (KeyError, TypeError):\n            return None\n    if isinstance(row, dict):\n        return row.get(name)\n    return None\n\n\ndef _supported_area_count(\n    events: Iterable[Any],\n    rows: Iterable[Any],\n    sport_key: str,\n    minimum_games: int = LOW_SAMPLE_GAMES,\n) -> int:\n    """Count canonical ticket areas represented in enough distinct games."""\n\n    event_by_id = {int(event.id): event for event in events}\n    game_ids_by_area: dict[str, set[int]] = defaultdict(set)\n\n    for row in rows:\n        try:\n            event_id = int(_row_value(row, "event_id"))\n        except (TypeError, ValueError):\n            continue\n        event = event_by_id.get(event_id)\n        if event is None or _row_value(row, "captured_at") is None:\n            continue\n        try:\n            price = float(_row_value(row, "price"))\n        except (TypeError, ValueError):\n            continue\n        if price <= 0:\n            continue\n\n        identity = section_identity(\n            sport_key,\n            _event_venue_for_sport(event, sport_key),\n            _row_value(row, "section"),\n        )\n        if identity is not None:\n            game_ids_by_area[identity.key].add(event_id)\n\n    threshold = max(int(minimum_games), 1)\n    return sum(\n        len(game_ids) >= threshold for game_ids in game_ids_by_area.values()\n    )\n'''
    if "def _supported_area_count(" not in text:
        node = _function_node(text, "_snapshot_rows_for")
        lines = text.splitlines(keepends=True)
        lines.insert(node.end_lineno, helpers)
        text = "".join(lines)

    text = _insert_after_section_assignment(
        text,
        "build_nfl_stadium_context",
        'analyzed_area_count = _supported_area_count(selected_events, rows, "nfl")',
    )
    text = _insert_after_section_assignment(
        text,
        "build_mlb_stadium_context",
        'analyzed_area_count = _supported_area_count(selected_events, rows, "mlb")',
    )
    text = _insert_after_section_assignment(
        text,
        "build_nhl_arena_context",
        'analyzed_area_count = _supported_area_count(analysis_events, rows, "nhl")',
    )

    if '"analyzed_area_count": 0,' not in _function_segment(text, "_page_config"):
        text = _replace_in_function(
            text,
            "_page_config",
            '        "selected_team_label": "",\n',
            '        "selected_team_label": "",\n        "analyzed_area_count": 0,\n',
        )

    for function_name in (
        "build_nfl_stadium_context",
        "build_mlb_stadium_context",
        "build_nhl_arena_context",
    ):
        if '"analyzed_area_count": analyzed_area_count,' in _function_segment(
            text, function_name
        ):
            continue
        text = _replace_in_function(
            text,
            function_name,
            '        "section_count": len(sections),\n',
            '        "section_count": len(sections),\n        "analyzed_area_count": analyzed_area_count,\n',
        )

    ast.parse(text)
    BLUEPRINT.write_text(text)


def update_home_templates() -> None:
    path = ROOT / "Flask_App/templates/HomeScreen.html"
    text = path.read_text()
    text = _require_replace(
        text,
        '''        {% set section_ns = namespace(names=[]) %}\n        {% for sections in game_sections_dict.get(place, {}).values() %}\n          {% for section in sections %}\n            {% if not is_parking_section(section) and section not in section_ns.names %}{% set section_ns.names = section_ns.names + [section] %}{% endif %}\n          {% endfor %}\n        {% endfor %}\n''',
        "",
        label="MLB card section accumulator",
    )
    text = _require_replace(
        text,
        '            <div><dt>{{ section_ns.names|length }}</dt><dd>sections</dd></div>\n',
        "",
        label="MLB card section count",
    )
    path.write_text(text)

    path = ROOT / "Flask_App/templates/NFLHomeScreen.html"
    text = path.read_text()
    text = _require_replace(
        text,
        '        {% set section_ns = namespace(names=[]) %}\n',
        "",
        label="NFL card section namespace",
    )
    text = _require_replace(
        text,
        '''              {% for section in game_sections_dict.get(team, {}).get(game.value, []) %}\n                {% if not is_parking_section(section) and section not in section_ns.names %}{% set section_ns.names = section_ns.names + [section] %}{% endif %}\n              {% endfor %}\n''',
        "",
        label="NFL card section accumulator",
    )
    text = _require_replace(
        text,
        '            <div><dt>{{ section_ns.names|length }}</dt><dd>sections</dd></div>\n',
        "",
        label="NFL card section count",
    )
    path.write_text(text)

    path = ROOT / "Flask_App/templates/NHLHomeScreen.html"
    text = path.read_text()
    text = _require_replace(
        text,
        '        {% set section_ns = namespace(names=[]) %}\n',
        "",
        label="NHL card section namespace",
    )
    text = _require_replace(
        text,
        '''              {% for section in game_sections_dict.get(team, {}).get(game.value, []) %}\n                {% if not is_parking_section(section) and section not in section_ns.names %}{% set section_ns.names = section_ns.names + [section] %}{% endif %}\n              {% endfor %}\n''',
        "",
        label="NHL card section accumulator",
    )
    text = _require_replace(
        text,
        '            <div><dt>{{ section_ns.names|length }}</dt><dd>sections</dd></div>\n',
        "",
        label="NHL card section count",
    )
    path.write_text(text)


def update_report_template() -> None:
    path = ROOT / "Flask_App/templates/nfl_stadium.html"
    text = path.read_text()
    text = _require_replace(
        text,
        '              <div><dt>{{ stadium.section_count }}</dt><dd>sections</dd></div>\n',
        "",
        label="directory card section count",
    )
    text = _require_replace(
        text,
        '          <div><dt>{{ section_count }}</dt><dd>Sections</dd></div>\n',
        '          <div><dt>{{ analyzed_area_count }}</dt><dd>Areas analyzed</dd></div>\n',
        label="report section metric",
    )
    path.write_text(text)


def update_deploy_validation() -> None:
    path = ROOT / ".github/workflows/deploy-pythonanywhere.yml"
    text = path.read_text()
    if "Flask_App/section_canonicalization.py" not in text:
        text = _require_replace(
            text,
            "            Flask_App/nfl_blueprint.py \\\n            Flask_App/nfl_stadium_blueprint.py \\\n",
            "            Flask_App/nfl_blueprint.py \\\n            Flask_App/section_canonicalization.py \\\n            Flask_App/nfl_stadium_blueprint.py \\\n",
            label="deployment Python compile list",
        )
    path.write_text(text)


def main() -> None:
    update_blueprint()
    update_home_templates()
    update_report_template()
    update_deploy_validation()


if __name__ == "__main__":
    main()
