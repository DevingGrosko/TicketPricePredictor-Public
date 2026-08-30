from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(
            f"Expected exactly one match in {path}, found {count}: {old[:120]!r}"
        )
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


BLUEPRINT = "Flask_App/nfl_blueprint.py"
replace_once(
    BLUEPRINT,
    "from flask import Blueprint, jsonify, render_template, request",
    "from flask import Blueprint, jsonify, render_template, request, url_for",
)

replace_once(
    BLUEPRINT,
    '''    finally:
        model.engine.dispose()


class NFLGraphBuilder:
''',
    '''    finally:
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
    else:
        captured = captured.astimezone(timezone.utc)
    return f"{captured:%b} {captured.day}, {captured:%Y} · {captured:%H:%M} UTC"


class NFLGraphBuilder:
''',
)

replace_once(
    BLUEPRINT,
    '''

@nfl_blueprint.get("/nfl/graph")
def nfl_graph():
''',
    '''

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

    team = nfl_home_team(selected.title) or selection
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

    map_data = {
        "team": team,
        "game": str(selected.id),
        "venue": selected.venue,
        "sections": section_data,
        "selected_section": selected_section,
        "graph_url": url_for("nfl.nfl_graph"),
    }
    return render_template(
        "nfl_map.html",
        error=None,
        team=team,
        venue=selected.venue,
        game=str(selected.id),
        gameLabel=format_nfl_title(selected),
        section_count=len(section_data),
        priced_section_count=sum(
            item["price"] is not None for item in section_data
        ),
        latest_capture_label=format_nfl_capture_label(latest_capture),
        source_url=selected.source_url,
        map_data=map_data,
    )


@nfl_blueprint.get("/nfl/graph")
def nfl_graph():
''',
)

HOME = "Flask_App/templates/NFLHomeScreen.html"
replace_once(
    HOME,
    '''  <link rel="stylesheet" href="{{ url_for('static', filename='css/nfl-responsive.css') }}?v=1">
''',
    '''  <link rel="stylesheet" href="{{ url_for('static', filename='css/nfl-responsive.css') }}?v=1">
  <link rel="stylesheet" href="{{ url_for('static', filename='css/nfl-map.css') }}?v=1">
''',
)
replace_once(
    HOME,
    '''        <button class="button nfl-button nfl-button--primary submit-analysis" type="submit" disabled>Open price trend <span aria-hidden="true">&#8594;</span></button>
        <p class="nfl-form-note"><span aria-hidden="true">&#9432;</span> Prices are historical marketplace observations, not a guarantee of future availability.</p>
''',
    '''        <div class="nfl-form-actions">
          <button class="button nfl-button nfl-button--primary submit-analysis" type="submit" disabled>Open price trend <span aria-hidden="true">&#8594;</span></button>
          <button class="button nfl-map-launch" type="button" data-map-launch data-map-base="{{ url_for('nfl.nfl_map') }}" disabled>Explore stadium map <span aria-hidden="true">&#8594;</span></button>
        </div>
        <p class="nfl-form-note"><span aria-hidden="true">&#9432;</span> Prices are historical marketplace observations, not a guarantee of future availability.</p>
''',
)
replace_once(
    HOME,
    '''{% block scripts %}<script src="{{ url_for('static', filename='js/nfl.js') }}?v=2"></script>{% endblock %}''',
    '''{% block scripts %}<script src="{{ url_for('static', filename='js/nfl.js') }}?v=3"></script>{% endblock %}''',
)

Path("Flask_App/static/js/nfl.js").write_text(
    '''function replaceNflOptions(select, values, placeholder) {
  select.innerHTML = '';
  const empty = document.createElement('option');
  empty.value = '';
  empty.textContent = placeholder;
  select.appendChild(empty);

  values.forEach((value) => {
    const option = document.createElement('option');
    option.value = typeof value === 'object' ? value.value : value;
    option.textContent = typeof value === 'object' ? value.label : value;
    select.appendChild(option);
  });
  select.disabled = values.length === 0;
}

const nflForm = document.querySelector('.nfl-selection-form');
if (nflForm) {
  const teamSelect = nflForm.querySelector('.place-select');
  const gameSelect = nflForm.querySelector('.game-select');
  const sectionSelect = nflForm.querySelector('.section-select');
  const submit = nflForm.querySelector('.submit-analysis');
  const mapButton = nflForm.querySelector('[data-map-launch]');

  const updateActions = () => {
    submit.disabled = !(teamSelect.value && gameSelect.value && sectionSelect.value);
    if (mapButton) {
      mapButton.disabled = !(teamSelect.value && gameSelect.value);
    }
  };

  teamSelect.addEventListener('change', () => {
    replaceNflOptions(gameSelect, nflGamesData[teamSelect.value] || [], 'Select a game');
    replaceNflOptions(sectionSelect, [], 'Select a section');
    updateActions();
  });

  gameSelect.addEventListener('change', () => {
    const sections =
      (nflGameSectionsData[teamSelect.value] &&
        nflGameSectionsData[teamSelect.value][gameSelect.value]) || [];
    replaceNflOptions(sectionSelect, sections, 'Select a section');
    updateActions();
  });

  sectionSelect.addEventListener('change', updateActions);

  if (mapButton) {
    mapButton.addEventListener('click', () => {
      if (mapButton.disabled) return;
      const params = new URLSearchParams({
        team: teamSelect.value,
        game: gameSelect.value,
      });
      if (sectionSelect.value) params.set('section', sectionSelect.value);
      window.location.assign(`${mapButton.dataset.mapBase}?${params.toString()}`);
    });
  }

  nflForm.addEventListener('submit', (event) => {
    event.preventDefault();
    if (submit.disabled) return;
    const params = new URLSearchParams({
      team: teamSelect.value,
      game: gameSelect.value,
      section: sectionSelect.value,
    });
    window.location.assign(`/nfl/graph?${params.toString()}`);
  });
}
''',
    encoding="utf-8",
)

GRAPH = "Flask_App/templates/nfl_graph.html"
replace_once(
    GRAPH,
    '''  <link rel="stylesheet" href="{{ url_for('static', filename='css/nfl-responsive.css') }}?v=1">
''',
    '''  <link rel="stylesheet" href="{{ url_for('static', filename='css/nfl-responsive.css') }}?v=1">
  <link rel="stylesheet" href="{{ url_for('static', filename='css/nfl-map.css') }}?v=1">
''',
)
replace_once(
    GRAPH,
    '''          <a class="nfl-view-toggle" href="{{ url_for('nfl.nfl_graph', display=displayType, team=team, section=section, game=game) }}">Show {{ displayLabel }} view</a>
''',
    '''          <div class="nfl-chart-actions">
            <a class="nfl-view-toggle" href="{{ url_for('nfl.nfl_map', team=team, section=section, game=game) }}">View stadium map</a>
            <a class="nfl-view-toggle" href="{{ url_for('nfl.nfl_graph', display=displayType, team=team, section=section, game=game) }}">Show {{ displayLabel }} view</a>
          </div>
''',
)

TEST = "tests/test_nfl_blueprint.py"
replace_once(
    TEST,
    '''    nfl_home,
    nfl_home_team,
    nfl_matchup_teams,
''',
    '''    nfl_home,
    nfl_home_team,
    nfl_map,
    nfl_matchup_teams,
''',
)
replace_once(
    TEST,
    '''

if __name__ == "__main__":
    unittest.main()
''',
    '''

class NFLStadiumMapTests(unittest.TestCase):
    @patch("Flask_App.nfl_blueprint.render_template")
    def test_map_route_uses_latest_section_snapshot(self, render):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "nfl.db"
            old_db = os.environ.get("NFL_DATABASE_PATH")
            os.environ["NFL_DATABASE_PATH"] = str(db_path)
            try:
                def snapshot(source_id, base_price):
                    return EventSnapshot(
                        source_id=source_id,
                        title="Dallas Cowboys at New York Giants",
                        venue="MetLife Stadium",
                        sections=tuple(
                            SectionSnapshot(
                                section=f"Section {index}",
                                price=base_price + index,
                                listing_count=index + 1,
                                row="A",
                                quantity="2",
                                displayed_price=str(base_price + index),
                                alternate_price="",
                            )
                            for index in range(10)
                        ),
                    )

                first_capture = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
                game_id, _, _ = store_nfl_snapshot(
                    "https://www.vividseats.com/game/production/3333333",
                    first_capture + timedelta(days=10),
                    snapshot("3333333", 100),
                    first_capture,
                    db_path=db_path,
                )
                store_nfl_snapshot(
                    "https://www.vividseats.com/game/production/3333333",
                    first_capture + timedelta(days=10),
                    snapshot("3333333", 200),
                    first_capture + timedelta(hours=1),
                    db_path=db_path,
                )

                app = Flask(__name__)
                app.register_blueprint(nfl_blueprint)
                render.return_value = "map"
                path = (
                    f"/nfl/map?team=New%20York%20Giants&game={game_id}"
                    "&section=Section%201"
                )
                with app.test_request_context(path):
                    response = nfl_map()

                self.assertEqual(response, "map")
                self.assertEqual(render.call_args.args[0], "nfl_map.html")
                context = render.call_args.kwargs
                self.assertEqual(context["team"], "New York Giants")
                self.assertEqual(context["venue"], "MetLife Stadium")
                self.assertEqual(context["section_count"], 10)
                self.assertEqual(context["priced_section_count"], 10)
                by_name = {
                    item["name"]: item for item in context["map_data"]["sections"]
                }
                self.assertEqual(by_name["Section 1"]["price"], 201)
                self.assertEqual(by_name["Section 1"]["listing_count"], 2)
                self.assertEqual(
                    context["map_data"]["selected_section"], "Section 1"
                )
            finally:
                if old_db is None:
                    os.environ.pop("NFL_DATABASE_PATH", None)
                else:
                    os.environ["NFL_DATABASE_PATH"] = old_db


if __name__ == "__main__":
    unittest.main()
''',
)

STARTUP = "tests/test_flask_production_startup.py"
replace_once(
    STARTUP,
    '''                assert first.status_code == 201, first.get_data(as_text=True)
                assert first.get_json()["status"] == "stored"
''',
    '''                assert first.status_code == 201, first.get_data(as_text=True)
                assert first.get_json()["status"] == "stored"
                nfl_event_id = first.get_json()["event_id"]
''',
)
replace_once(
    STARTUP,
    '''                assert b"NFL market tracker" in nfl_page.data
                assert b"Dallas Cowboys" in nfl_page.data
''',
    '''                assert b"NFL market tracker" in nfl_page.data
                assert b"Dallas Cowboys" in nfl_page.data

                map_page = client.get(
                    f"/nfl/map?team=New%20York%20Giants&game={nfl_event_id}"
                )
                assert map_page.status_code == 200
                assert b"Interactive section explorer" in map_page.data
                assert b"MetLife Stadium" in map_page.data
''',
)

README = "README.md"
replace_once(
    README,
    '''- `/nfl`: NFL analysis.
- `/baseball`: alias for the baseball homepage.
''',
    '''- `/nfl`: NFL analysis.
- `/nfl/map`: interactive schematic section explorer for a selected game and stadium.
- `/baseball`: alias for the baseball homepage.
''',
)
replace_once(
    README,
    '''- NFL analysis is organized by designated home team, game, and section. Teams that share a venue, such as the Giants/Jets or Rams/Chargers, remain separate in the website.
''',
    '''- NFL analysis is organized by designated home team, game, and section. Teams that share a venue, such as the Giants/Jets or Rams/Chargers, remain separate in the website.
- Each tracked NFL game has an interactive stadium-bowl schematic generated from its collected section names and numbers, with direct links to price history and the provider's exact event map.
''',
)
