from pathlib import Path
import re
from textwrap import dedent


def load(path: str) -> str:
    return Path(path).read_text()


def save(path: str, text: str) -> None:
    Path(path).write_text(text)


def replace_exact(path: str, old: str, new: str, expected: int = 1) -> None:
    text = load(path)
    found = text.count(old)
    if found != expected:
        raise RuntimeError(
            f"{path}: expected {expected} matches, found {found}: {old[:90]!r}"
        )
    save(path, text.replace(old, new, expected))


def remove_between(path: str, start_marker: str, end_marker: str) -> None:
    text = load(path)
    start = text.find(start_marker)
    if start < 0:
        raise RuntimeError(f"{path}: missing start marker {start_marker!r}")
    end = text.find(end_marker, start)
    if end < 0:
        raise RuntimeError(f"{path}: missing end marker {end_marker!r}")
    end += len(end_marker)
    save(path, text[:start] + text[end:])


blueprint = "Flask_App/nfl_stadium_blueprint.py"
parking_helpers = "\n".join(
    [
        "def is_parking_section(value: Any) -> bool:",
        "    # Parking products are inventory, not seating sections.",
        "    normalized = _normalize(value)",
        "    if not normalized:",
        "        return False",
        "    return bool(",
        '        re.search(r"\\bparking\\b", normalized)',
        '        or re.match(r"^(?:lot|garage)\\b", normalized)',
        '        or "park and ride" in normalized',
        "    )",
        "",
        "",
        "def _public_sections(values: Iterable[Any]) -> list[str]:",
        "    return sorted(",
        "        {",
        "            _clean(value)",
        "            for value in values",
        "            if _clean(value) and not is_parking_section(value)",
        "        },",
        "        key=str.casefold,",
        "    )",
        "",
        "",
    ]
)
replace_exact(
    blueprint,
    "def _money(value: float | None) -> str:\n",
    parking_helpers + "def _money(value: float | None) -> str:\n",
)
replace_exact(
    blueprint,
    "            if _clean(section)\n        }",
    "            if _clean(section) and not is_parking_section(section)\n        }",
    expected=2,
)
replace_exact(
    blueprint,
    "        if not section or price <= 0 or captured is None:\n"
    "            continue\n",
    "        if (\n"
    "            not section\n"
    "            or is_parking_section(section)\n"
    "            or price <= 0\n"
    "            or captured is None\n"
    "        ):\n"
    "            continue\n",
)
replace_exact(
    blueprint,
    '        map_url = url_for("nfl.nfl_map", team=team, game=str(event.id))\n'
    "        game = {\n",
    '        map_url = url_for("nfl.nfl_map", team=team, game=str(event.id))\n'
    "        public_sections = _public_sections(\n"
    '            getattr(event, "sections", None) or []\n'
    "        )\n"
    "        game = {\n",
)
replace_exact(
    blueprint,
    '            "section_count": len(getattr(event, "sections", None) or []),\n'
    '            "sections": sorted(set(getattr(event, "sections", None) or [])),\n',
    '            "section_count": len(public_sections),\n'
    '            "sections": public_sections,\n',
)
replace_exact(
    blueprint,
    "        is_completed = _event_completed(event, now)\n"
    "        game = {\n",
    "        is_completed = _event_completed(event, now)\n"
    "        public_sections = _public_sections(event.event_sections or [])\n"
    "        game = {\n",
    expected=1,
)
replace_exact(
    blueprint,
    '            "section_count": len(event.event_sections or []),\n'
    '            "sections": sorted(set(event.event_sections or []), key=str.casefold),\n',
    '            "section_count": len(public_sections),\n'
    '            "sections": public_sections,\n',
)
replace_exact(
    blueprint,
    '        map_url = url_for("nhl.nhl_map", team=team, game=str(event.id))\n'
    "        game = {\n",
    '        map_url = url_for("nhl.nhl_map", team=team, game=str(event.id))\n'
    "        public_sections = _public_sections(event.sections or [])\n"
    "        game = {\n",
)
replace_exact(
    blueprint,
    '            "section_count": len(event.sections or []),\n'
    '            "sections": sorted(set(event.sections or []), key=str.casefold),\n',
    '            "section_count": len(public_sections),\n'
    '            "sections": public_sections,\n',
)
replace_exact(
    blueprint,
    '    return {"mlb_team_for_venue": mlb_team_for_venue}\n',
    "    return {\n"
    '        "mlb_team_for_venue": mlb_team_for_venue,\n'
    '        "is_parking_section": is_parking_section,\n'
    "    }\n",
)

option_helper = "\n".join(
    [
        "function isParkingOption(value) {",
        "  const raw = typeof value === 'object' ? (value.label || value.value) : value;",
        "  const normalized = String(raw || '')",
        "    .toLowerCase()",
        "    .replace(/[^a-z0-9]+/g, ' ')",
        "    .trim();",
        "  return /\\bparking\\b/.test(normalized)",
        "    || /^(lot|garage)\\b/.test(normalized)",
        "    || normalized.includes('park and ride');",
        "}",
        "",
        "",
    ]
)
for path, function_name in (
    ("Flask_App/static/js/script.js", "replaceOptions"),
    ("Flask_App/static/js/nfl.js", "replaceNflOptions"),
    ("Flask_App/static/js/nhl.js", "replaceNhlOptions"),
):
    replace_exact(
        path,
        f"function {function_name}(select, values, placeholder) {{\n",
        option_helper + f"function {function_name}(select, values, placeholder) {{\n",
    )
    replace_exact(
        path,
        "  values.forEach((value) => {\n",
        "  const visibleValues = values.filter((value) => !isParkingOption(value));\n"
        "  visibleValues.forEach((value) => {\n",
    )
    replace_exact(
        path,
        "  select.disabled = values.length === 0;\n",
        "  select.disabled = visibleValues.length === 0;\n",
    )

map_helper = "\n".join(
    [
        "  function isParkingSectionName(value) {",
        "    const normalized = String(value || '')",
        "      .toLowerCase()",
        "      .replace(/[^a-z0-9]+/g, ' ')",
        "      .trim();",
        "    return /\\bparking\\b/.test(normalized)",
        "      || /^(lot|garage)\\b/.test(normalized)",
        "      || normalized.includes('park and ride');",
        "  }",
        "",
        "",
    ]
)
for path in (
    "Flask_App/static/js/nfl-map.js",
    "Flask_App/static/js/nhl-map.js",
):
    replace_exact(
        path,
        "  const sections = Array.isArray(mapData.sections)\n",
        map_helper + "  const sections = Array.isArray(mapData.sections)\n",
    )
    replace_exact(
        path,
        "      })).filter((section) => section.name)\n",
        "      })).filter(\n"
        "        (section) => section.name && !isParkingSectionName(section.name),\n"
        "      )\n",
    )

landing_pages = {
    "Flask_App/templates/NFLHomeScreen.html": {
        "lede": "Choose a team to see its cheapest sections, biggest average drops, and individual game histories.",
        "picker_old": "Teams with more completed games provide stronger comparisons. Every report shows its sample size, and small samples are marked rather than presented as established patterns.",
        "picker_new": "Open a team report, or jump directly to one matchup below.",
        "detail_heading_old": "Already know the matchup?",
        "detail_heading_new": "Explore one matchup.",
        "detail_old": "Use the detailed flow to inspect one home team, matchup, and section. The team report remains the fastest way to compare patterns across several games.",
        "detail_new": "Choose a game and section for its exact price history.",
        "script_old": "js/nfl.js') }}?v=3",
        "script_new": "js/nfl.js') }}?v=4",
    },
    "Flask_App/templates/HomeScreen.html": {
        "lede": "Choose a team to see its cheapest sections, biggest average drops, and individual game histories.",
        "picker_old": "Each report compares sections across that team's tracked home games. Results based on fewer than three games are marked as small samples.",
        "picker_new": "Open a team report, or use the detailed tools below.",
        "detail_heading_old": "Go deeper into one section or game.",
        "detail_heading_new": "Explore a game or section.",
        "detail_old": "The team report is the fastest summary. These tools preserve the original multi-game trend, single-game chart, and historical buying-window views.",
        "detail_new": "Open a multi-game trend, one matchup, or a historical buying window.",
        "script_old": "js/script.js') }}?v=4",
        "script_new": "js/script.js') }}?v=5",
    },
    "Flask_App/templates/NHLHomeScreen.html": {
        "lede": "Choose a team to see its cheapest sections, biggest average drops, and individual game histories.",
        "picker_old": "Arena reports show the supporting game count and preserve USD or CAD rather than combining unlike currencies.",
        "picker_new": "Open a team report, or jump directly to one matchup below.",
        "detail_heading_old": "Already know the matchup?",
        "detail_heading_new": "Explore one matchup.",
        "detail_old": "Choose one home team, matchup, and section. The team dashboard remains the fastest way to compare patterns across several games.",
        "detail_new": "Choose a game and section for its exact price history.",
        "script_old": "js/nhl.js') }}?v=1",
        "script_new": "js/nhl.js') }}?v=2",
    },
}

visible_sections_block = "\n".join(
    [
        "  {% set visible_sections = namespace(names=[]) %}",
        "  {% for by_game in game_sections_dict.values() %}",
        "    {% for sections in by_game.values() %}",
        "      {% for section in sections %}",
        "        {% if not is_parking_section(section) and section not in visible_sections.names %}",
        "          {% set visible_sections.names = visible_sections.names + [section] %}",
        "        {% endif %}",
        "      {% endfor %}",
        "    {% endfor %}",
        "  {% endfor %}",
        "",
    ]
)

for path, values in landing_pages.items():
    text = load(path)
    if '<div class="nfl-page' not in text:
        raise RuntimeError(f"{path}: root page marker missing")
    text = text.replace(
        '<div class="nfl-page',
        visible_sections_block + '<div class="nfl-page',
        1,
    )
    text = text.replace(
        "nfl-hero nfl-hero--stadium-first",
        "nfl-hero nfl-hero--stadium-first nfl-hero--compact",
        1,
    )

    preview_start = text.find('\n    <div class="nfl-stadium-preview')
    preview_end = text.find("\n  </section>", preview_start)
    if preview_start < 0 or preview_end < 0:
        raise RuntimeError(f"{path}: hero preview block missing")
    text = text[:preview_start] + text[preview_end:]

    bar_start = text.find('\n  <section class="nfl-data-bar"')
    bar_end = text.find("\n  </section>", bar_start)
    if bar_start < 0 or bar_end < 0:
        raise RuntimeError(f"{path}: three-step strip missing")
    bar_end += len("\n  </section>")
    text = text[:bar_start] + text[bar_end:]

    text, count = re.subn(
        r'<p class="nfl-hero__lede">.*?</p>',
        f'<p class="nfl-hero__lede">{values["lede"]}</p>',
        text,
        count=1,
    )
    if count != 1:
        raise RuntimeError(f"{path}: hero text replacement failed")

    for old, new in (
        ("Choose the team you want to see.", "Choose a team."),
        (values["picker_old"], values["picker_new"]),
        (values["detail_heading_old"], values["detail_heading_new"]),
        (values["detail_old"], values["detail_new"]),
        (values["script_old"], values["script_new"]),
        ("css/nfl-stadium.css') }}?v=2", "css/nfl-stadium.css') }}?v=3"),
    ):
        if old not in text:
            raise RuntimeError(f"{path}: expected text missing: {old[:80]!r}")
        text = text.replace(old, new, 1)

    text = text.replace(
        "{{ section_count }}</dt><dd>Sections compared",
        "{{ visible_sections.names|length }}</dt><dd>Sections compared",
        1,
    )
    text = text.replace(
        "{% if section not in section_ns.names %}",
        "{% if not is_parking_section(section) and section not in section_ns.names %}",
    )
    save(path, text)

dashboard = "Flask_App/templates/nfl_stadium.html"
replacements = [
    (
        "          <h1>Choose a team to compare its home {{ venue_noun }}.</h1>\n"
        "          <p>Open a team to see average section prices, the largest historical declines, every tracked section, and the individual games behind the results.</p>\n",
        "          <h1>Choose a team.</h1>\n"
        "          <p>See its cheapest sections, biggest price drops, and game history.</p>\n",
    ),
    (
        "        <dl>\n"
        "          <div><dt>{{ stadium_count }}</dt><dd>Tracked {{ venue_plural }}</dd></div>\n"
        "          <div><dt>{{ window_label }}</dt><dd>Maximum window</dd></div>\n"
        "        </dl>\n",
        "",
    ),
    (
        '            <div class="nfl-stadium-card__status">\n'
        "              <span>{{ stadium.completed_count }} completed</span>\n"
        "              <span>{{ stadium.upcoming_count }} upcoming</span>\n"
        "            </div>\n",
        "",
    ),
    (
        "          <p>Compare section-level prices and movement across the complete tracked history at {{ selected_venue }}, then open the exact games behind each average.</p>\n",
        "          <p>Compare sections across tracked home games at {{ selected_venue }}.</p>\n",
    ),
    (
        '        <dl class="nfl-stadium-dashboard-metrics">\n'
        "          <div><dt>{{ game_count }}</dt><dd>Games tracked</dd></div>\n"
        "          <div><dt>{{ section_count }}</dt><dd>Sections compared</dd></div>\n"
        "          <div><dt>{{ observation_count }}</dt><dd>Price observations</dd></div>\n"
        "          <div><dt>{{ drop_section_count }}</dt><dd>Sections with drop data</dd></div>\n"
        "        </dl>\n",
        '        <dl class="nfl-stadium-dashboard-metrics">\n'
        "          <div><dt>{{ game_count }}</dt><dd>Games</dd></div>\n"
        "          <div><dt>{{ section_count }}</dt><dd>Sections</dd></div>\n"
        "          <div><dt>{{ completed_game_count }}</dt><dd>Completed</dd></div>\n"
        "        </dl>\n",
    ),
    (
        '      <nav class="nfl-dashboard-nav" aria-label="{{ venue_noun|capitalize }} report sections">\n'
        '        <a href="#overview" class="is-active">Overview</a>\n'
        '        <a href="#all-sections">All sections</a>\n'
        '        <a href="#single-games">Single games</a>\n'
        '        <a href="#methodology">Methodology</a>\n'
        "      </nav>\n",
        '      <nav class="nfl-dashboard-nav" aria-label="{{ venue_noun|capitalize }} report sections">\n'
        '        <a href="#overview" class="is-active">Top sections</a>\n'
        '        <a href="#all-sections">All sections</a>\n'
        '        <a href="#single-games">Games</a>\n'
        "      </nav>\n",
    ),
    (
        '        <div class="nfl-dashboard-section__heading">\n'
        "          <div>\n"
        '            <span class="nfl-section-kicker">Fast read</span>\n'
        "            <h2>Where prices are lowest—and where they fall.</h2>\n"
        "          </div>\n"
        "          <p>Rankings are calculated at the game level first. A game with more snapshots does not overpower the rest of the {{ venue_noun }} history.</p>\n"
        "        </div>\n",
        '        <div class="nfl-dashboard-section__heading">\n'
        "          <div>\n"
        '            <span class="nfl-section-kicker">Top sections</span>\n'
        "            <h2>Cheapest sections and biggest drops.</h2>\n"
        "          </div>\n"
        "        </div>\n",
    ),
    (
        "<div><span>Lowest average price</span><h3>Five cheapest sections</h3></div>",
        "<div><h3>Cheapest sections</h3></div>",
    ),
    (
        "<div><span>Largest average decline</span><h3>Five strongest price drops</h3></div>",
        "<div><h3>Biggest price drops</h3></div>",
    ),
    (
        "                    <small>{{ section.game_count }} game{% if section.game_count != 1 %}s{% endif %} · {{ section.observation_count }} observations{% if section.is_low_price_sample %} · small sample{% endif %}</small>\n",
        "                    <small>{{ section.game_count }} game{% if section.game_count != 1 %}s{% endif %}{% if section.is_low_price_sample %} · small sample{% endif %}</small>\n",
    ),
    (
        '        <div class="nfl-sample-note">\n'
        '          <span aria-hidden="true">&#9432;</span>\n'
        "          <p><strong>Read the sample before the rank.</strong> A section is marked “small sample” until it appears in at least three games. Rankings remain visible for newer datasets but should not yet be treated as stable patterns.</p>\n"
        "        </div>\n",
        '        <div class="nfl-sample-note">\n'
        '          <span aria-hidden="true">&#9432;</span>\n'
        "          <p>“Small sample” means fewer than three games.</p>\n"
        "        </div>\n",
    ),
    (
        '        <div class="nfl-dashboard-section__heading">\n'
        '          <div><span class="nfl-section-kicker">Across-game comparison</span><h2>Every tracked section.</h2></div>\n'
        "          <p>Search or sort the complete history. Average price uses one typical value per game; movement uses completed games with usable first and final observations.</p>\n"
        "        </div>\n",
        '        <div class="nfl-dashboard-section__heading">\n'
        '          <div><span class="nfl-section-kicker">Compare</span><h2>All sections.</h2></div>\n'
        "        </div>\n",
    ),
    (
        '              <tr><th>Section</th><th>Average price</th><th>Average movement</th><th>Drop frequency</th><th>Games</th><th><span class="sr-only">Open details</span></th></tr>\n',
        '              <tr><th>Section</th><th>Average price</th><th>Average movement</th><th>Games</th><th><span class="sr-only">Open details</span></th></tr>\n',
    ),
    (
        "                  <td><strong>{% if section.drop_game_count %}{{ section.drop_frequency }}%{% else %}—{% endif %}</strong><small>{{ section.drop_game_count }} qualifying game{% if section.drop_game_count != 1 %}s{% endif %}</small></td>\n",
        "",
    ),
    (
        '        <div class="nfl-dashboard-section__heading">\n'
        '          <div><span class="nfl-section-kicker">Underlying game history</span><h2>Open one exact game.</h2></div>\n'
        "          <p>Move from the across-game summary into the observations for one matchup and one section.</p>\n"
        "        </div>\n",
        '        <div class="nfl-dashboard-section__heading">\n'
        '          <div><span class="nfl-section-kicker">Games</span><h2>Open one matchup.</h2></div>\n'
        "        </div>\n",
    ),
    (
        '{% for section in game.sections %}<option value="{{ section }}">{{ section }}</option>{% endfor %}',
        '{% for section in game.sections if not is_parking_section(section) %}<option value="{{ section }}">{{ section }}</option>{% endfor %}',
    ),
    ("css/nfl-stadium.css') }}?v=2", "css/nfl-stadium.css') }}?v=3"),
    ("js/nfl-stadium.js') }}?v=2", "js/nfl-stadium.js') }}?v=3"),
]
for old, new in replacements:
    replace_exact(dashboard, old, new)
replace_exact(dashboard, 'colspan="6"', 'colspan="5"')
remove_between(
    dashboard,
    '\n      <section class="nfl-dashboard-section nfl-methodology" id="methodology">',
    "\n      </section>",
)

base = "Flask_App/templates/base.html"
replace_exact(base, ">Baseball</a>", ">MLB</a>")
replace_exact(
    base,
    '        <a href="{{ url_for(\'home\') }}#how-it-works">How it works</a>\n',
    "",
)
replace_exact(
    base,
    "Baseball, NFL, and NHL histories are collected and stored separately.",
    "MLB, NFL, and NHL histories are collected and stored separately.",
)

css = "Flask_App/static/css/nfl-stadium.css"
replace_exact(
    css,
    ".nfl-hero--stadium-first {\n"
    "  min-height: 650px;\n"
    "}\n",
    ".nfl-hero--stadium-first {\n"
    "  min-height: 650px;\n"
    "}\n\n"
    ".nfl-hero--compact {\n"
    "  min-height: 0;\n"
    "  grid-template-columns: minmax(0, 780px);\n"
    "  justify-content: start;\n"
    "  padding-top: 62px;\n"
    "  padding-bottom: 58px;\n"
    "}\n\n"
    ".nfl-hero--compact .nfl-hero__copy {\n"
    "  max-width: 780px;\n"
    "}\n",
)
replace_exact(
    css,
    "  grid-template-columns: repeat(4, minmax(0, 1fr));\n",
    "  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));\n",
)
replace_exact(css, "  min-width: 870px;\n", "  min-width: 760px;\n")

map_counts = "\n".join(
    [
        "    {% else %}",
        "      {% set visible_map_sections = namespace(total=0, priced=0) %}",
        "      {% for item in map_data.sections %}",
        "        {% if not is_parking_section(item.name) %}",
        "          {% set visible_map_sections.total = visible_map_sections.total + 1 %}",
        "          {% if item.price is not none %}",
        "            {% set visible_map_sections.priced = visible_map_sections.priced + 1 %}",
        "          {% endif %}",
        "        {% endif %}",
        "      {% endfor %}",
        "",
    ]
)
for path, script_name in (
    ("Flask_App/templates/nfl_map.html", "nfl-map.js"),
    ("Flask_App/templates/nhl_map.html", "nhl-map.js"),
):
    replace_exact(path, "    {% else %}\n", map_counts)
    replace_exact(
        path,
        "{{ section_count }} sections",
        "{{ visible_map_sections.total }} sections",
    )
    replace_exact(
        path,
        "<strong>{{ priced_section_count }}</strong>",
        "<strong>{{ visible_map_sections.priced }}</strong>",
    )
    replace_exact(
        path,
        f"js/{script_name}') }}?v=2",
        f"js/{script_name}') }}?v=3",
    )

Path("tests/test_navigation_and_parking_filter.py").write_text(
    dedent(
        """
        from pathlib import Path
        import unittest

        from Flask_App.nfl_stadium_blueprint import (
            _public_sections,
            is_parking_section,
        )


        class ParkingSectionFilterTests(unittest.TestCase):
            def test_parking_inventory_is_detected(self):
                for value in (
                    "Parking",
                    "Parking Pass",
                    "VIP Parking - Lot A",
                    "Lot B",
                    "Garage 3",
                    "Park and Ride",
                ):
                    with self.subTest(value=value):
                        self.assertTrue(is_parking_section(value))

            def test_real_seating_sections_are_kept(self):
                for value in (
                    "Section 101",
                    "Club Level",
                    "Upper 512",
                    "Park Level 200",
                    "Field Box 14",
                ):
                    with self.subTest(value=value):
                        self.assertFalse(is_parking_section(value))

            def test_public_sections_remove_parking(self):
                self.assertEqual(
                    _public_sections(
                        [
                            "Section 101",
                            "Parking Pass",
                            "Lot C",
                            "Club 2",
                        ]
                    ),
                    ["Club 2", "Section 101"],
                )


        class SimplifiedNavigationTests(unittest.TestCase):
            def test_landing_pages_skip_preview_and_three_step_strip(self):
                root = Path(__file__).resolve().parents[1]
                for relative in (
                    "Flask_App/templates/HomeScreen.html",
                    "Flask_App/templates/NFLHomeScreen.html",
                    "Flask_App/templates/NHLHomeScreen.html",
                ):
                    text = (root / relative).read_text()
                    with self.subTest(relative=relative):
                        self.assertNotIn('class="nfl-data-bar"', text)
                        self.assertNotIn('class="nfl-stadium-preview', text)
                        self.assertIn("nfl-hero--compact", text)

            def test_dashboard_keeps_only_core_navigation(self):
                root = Path(__file__).resolve().parents[1]
                text = (root / "Flask_App/templates/nfl_stadium.html").read_text()
                self.assertIn('href="#overview"', text)
                self.assertIn('href="#all-sections"', text)
                self.assertIn('href="#single-games"', text)
                self.assertNotIn('href="#methodology"', text)
                self.assertNotIn("nfl-methodology", text)

            def test_primary_nav_is_shorter(self):
                root = Path(__file__).resolve().parents[1]
                text = (root / "Flask_App/templates/base.html").read_text()
                self.assertIn(">MLB</a>", text)
                self.assertNotIn(">How it works</a>", text)


        if __name__ == "__main__":
            unittest.main()
        """
    ).lstrip()
)
