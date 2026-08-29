from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Expected one match in {path}, found {count}: {old[:80]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


BLUEPRINT = "Flask_App/nfl_blueprint.py"

replace_once(
    BLUEPRINT,
    '''def is_nfl_game_title(title: str) -> bool:
    normalized = " ".join(str(title or "").split()).casefold()
    if not normalized or any(marker in normalized for marker in NON_GAME_MARKERS):
        return False

    team_count = sum(name.casefold() in normalized for name in NFL_TEAM_NAMES)
    has_matchup_separator = any(
        separator in normalized for separator in (" at ", " vs ", " vs. ", " versus ")
    )
    return team_count >= 2 and has_matchup_separator
''',
    '''def nfl_matchup_teams(title: str) -> tuple[str, str] | None:
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


def is_nfl_game_title(title: str) -> bool:
    normalized = " ".join(str(title or "").split()).casefold()
    has_matchup_separator = any(
        separator in normalized for separator in (" at ", " vs ", " vs. ", " versus ")
    )
    return nfl_matchup_teams(title) is not None and has_matchup_separator
''',
)

replace_once(
    BLUEPRINT,
    '''def find_nfl_game(venue: str, identifier: str | None) -> NFLEvent | None:
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
''',
    '''def find_nfl_game(team_or_venue: str, identifier: str | None) -> NFLEvent | None:
    """Find a game in its home-team bucket, while accepting legacy venue links."""
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
            home_team = nfl_home_team(event.title)
            if team_or_venue not in {home_team, event.venue}:
                return None
            return event
    finally:
        model.engine.dispose()
''',
)

replace_once(
    BLUEPRINT,
    '''    def single_game_graph(
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
''',
    '''    def single_game_graph(
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
                if event is None or nfl_home_team(event.title) != home_team:
                    return [], []
''',
)

replace_once(
    BLUEPRINT,
    '''@nfl_blueprint.get("/nfl")
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
''',
    '''@nfl_blueprint.get("/nfl")
def nfl_home():
    model = CreateNFLModel()
    try:
        with model.getSession()() as session:
            games = session.query(NFLEvent).order_by(NFLEvent.event_date).all()
            games_dict: dict[str, list[dict[str, str]]] = {}
            game_sections_dict: dict[str, dict[str, list[str]]] = {}
            for game in games:
                home_team = nfl_home_team(game.title)
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

    return render_template(
        "NFLHomeScreen.html",
        games_dict=games_dict,
        game_sections_dict=game_sections_dict,
        team_count=team_count,
        game_count=game_count,
        section_count=section_count,
    )
''',
)

replace_once(
    BLUEPRINT,
    '''@nfl_blueprint.get("/nfl/graph")
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
''',
    '''@nfl_blueprint.get("/nfl/graph")
def nfl_graph():
    selection = request.args.get("team") or request.args.get("event") or ""
    event_id = request.args.get("game")
    section = request.args.get("section") or ""
    display_mode = "percentage" if request.args.get("display") == "percentage" else "money"
    selected = find_nfl_game(selection, event_id)
    team = nfl_home_team(selected.title) if selected else selection
    venue = selected.venue if selected else ""
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
''',
)

HOME_TEMPLATE = "Flask_App/templates/NFLHomeScreen.html"
for old, new in (
    (
        "Choose a stadium, matchup, and section to inspect the adaptive market history.",
        "Choose a home team, matchup, and section to inspect the adaptive market history.",
    ),
    ("<div><dt>{{ stadium_count }}</dt><dd>Stadiums</dd></div>", "<div><dt>{{ team_count }}</dt><dd>Home teams</dd></div>"),
    ("<div><span>Scope</span><strong>One game at a time</strong></div>", "<div><span>Scope</span><strong>One home team at a time</strong></div>"),
    (
        "Select the stadium, game, and section you care about.",
        "Select the home team, game, and section you care about.",
    ),
    ('<label for="nfl-place"><span>1</span> Stadium</label>', '<label for="nfl-team"><span>1</span> Home team</label>'),
    ('<select id="nfl-place" class="place-select">', '<select id="nfl-team" class="place-select">'),
    ('<option value="">Select a stadium</option>', '<option value="">Select a team</option>'),
    ('{% for place in games_dict %}<option value="{{ place }}">{{ place }}</option>{% endfor %}', '{% for team in games_dict %}<option value="{{ team }}">{{ team }}</option>{% endfor %}'),
    ("filename='js/nfl.js') }}?v=1", "filename='js/nfl.js') }}?v=2"),
):
    replace_once(HOME_TEMPLATE, old, new)

GRAPH_TEMPLATE = "Flask_App/templates/nfl_graph.html"
for old, new in (
    (
        '<p><strong>{{ venue }}</strong><span>&middot;</span>{{ section }}</p>',
        '<p><strong>{{ team }}</strong><span>&middot;</span>{{ venue }}<span>&middot;</span>{{ section }}</p>',
    ),
    (
        "url_for('nfl.nfl_graph', display=displayType, event=venue, section=section, game=game)",
        "url_for('nfl.nfl_graph', display=displayType, team=team, section=section, game=game)",
    ),
    (
        'aria-label="Interactive NFL ticket price trend for {{ venue }}, {{ section }}"',
        'aria-label="Interactive NFL ticket price trend for {{ team }}, {{ section }}"',
    ),
    (
        'alt="NFL ticket price trend for {{ venue }}, {{ section }}"',
        'alt="NFL ticket price trend for {{ team }}, {{ section }}"',
    ),
):
    replace_once(GRAPH_TEMPLATE, old, new)

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

  const updateSubmit = () => {
    submit.disabled = !(teamSelect.value && gameSelect.value && sectionSelect.value);
  };

  teamSelect.addEventListener('change', () => {
    replaceNflOptions(gameSelect, nflGamesData[teamSelect.value] || [], 'Select a game');
    replaceNflOptions(sectionSelect, [], 'Select a section');
    updateSubmit();
  });

  gameSelect.addEventListener('change', () => {
    const sections =
      (nflGameSectionsData[teamSelect.value] &&
        nflGameSectionsData[teamSelect.value][gameSelect.value]) || [];
    replaceNflOptions(sectionSelect, sections, 'Select a section');
    updateSubmit();
  });

  sectionSelect.addEventListener('change', updateSubmit);

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

TEST_FILE = "tests/test_nfl_blueprint.py"
replace_once(
    TEST_FILE,
    '''    CreateNFLModel,
    nfl_blueprint,
    store_nfl_snapshot,
)''',
    '''    CreateNFLModel,
    find_nfl_game,
    nfl_blueprint,
    nfl_home,
    nfl_home_team,
    nfl_matchup_teams,
    store_nfl_snapshot,
)''',
)

replace_once(
    TEST_FILE,
    '''

if __name__ == "__main__":
    unittest.main()
''',
    '''

class NFLTeamGroupingTests(unittest.TestCase):
    def test_provider_titles_resolve_to_distinct_home_teams(self):
        self.assertEqual(
            nfl_matchup_teams("Dallas Cowboys at New York Giants"),
            ("Dallas Cowboys", "New York Giants"),
        )
        self.assertEqual(
            nfl_home_team("Dallas Cowboys at New York Giants"),
            "New York Giants",
        )
        self.assertEqual(
            nfl_home_team("Buffalo Bills at New York Jets"),
            "New York Jets",
        )

    @patch("Flask_App.nfl_blueprint.render_template")
    def test_shared_stadium_games_are_split_into_home_team_groups(self, render):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "nfl.db"
            old_db = os.environ.get("NFL_DATABASE_PATH")
            os.environ["NFL_DATABASE_PATH"] = str(db_path)
            try:
                sections = tuple(
                    SectionSnapshot(
                        section=f"Section {index}",
                        price=100 + index,
                        listing_count=2,
                        row="A",
                        quantity="2",
                        displayed_price=str(100 + index),
                        alternate_price="",
                    )
                    for index in range(10)
                )
                captured_at = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
                giants_id, _, _ = store_nfl_snapshot(
                    "https://www.vividseats.com/game/production/1111111",
                    captured_at + timedelta(days=10),
                    EventSnapshot(
                        source_id="1111111",
                        title="Dallas Cowboys at New York Giants",
                        venue="MetLife Stadium",
                        sections=sections,
                    ),
                    captured_at,
                    db_path=db_path,
                )
                store_nfl_snapshot(
                    "https://www.vividseats.com/game/production/2222222",
                    captured_at + timedelta(days=11),
                    EventSnapshot(
                        source_id="2222222",
                        title="Buffalo Bills at New York Jets",
                        venue="MetLife Stadium",
                        sections=sections,
                    ),
                    captured_at,
                    db_path=db_path,
                )

                app = Flask(__name__)
                app.register_blueprint(nfl_blueprint)
                render.return_value = "ok"
                with app.test_request_context("/nfl"):
                    response = nfl_home()

                self.assertEqual(response, "ok")
                context = render.call_args.kwargs
                self.assertEqual(
                    list(context["games_dict"]),
                    ["New York Giants", "New York Jets"],
                )
                self.assertEqual(context["team_count"], 2)
                self.assertEqual(context["game_count"], 2)
                self.assertIsNotNone(find_nfl_game("New York Giants", str(giants_id)))
                self.assertIsNone(find_nfl_game("New York Jets", str(giants_id)))
                # Existing venue-based bookmarks remain valid.
                self.assertIsNotNone(find_nfl_game("MetLife Stadium", str(giants_id)))
            finally:
                if old_db is None:
                    os.environ.pop("NFL_DATABASE_PATH", None)
                else:
                    os.environ["NFL_DATABASE_PATH"] = old_db


if __name__ == "__main__":
    unittest.main()
''',
)

replace_once(
    "README.md",
    "- NFL analysis begins game by game and is organized by stadium, game, and section. Stadium-level aggregation can be added once enough comparable section history exists.",
    "- NFL analysis is organized by designated home team, game, and section. Teams that share a venue, such as the Giants/Jets or Rams/Chargers, remain separate in the website.",
)
