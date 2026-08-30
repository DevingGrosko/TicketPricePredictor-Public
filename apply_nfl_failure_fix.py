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


# ---------------------------------------------------------------------------
# NFL resolver: require away/home order, not merely the same unordered teams.
# ---------------------------------------------------------------------------
SCHEDULE = "nfl_schedule_collector.py"
replace_once(
    SCHEDULE,
    '''def matchup_key_from_title(title: str) -> tuple[str, str] | None:
    normalized = " ".join(str(title or "").split()).casefold()
    teams = [team for team in NFL_TEAM_NAMES if team.casefold() in normalized]
    if len(teams) != 2:
        return None
    return tuple(sorted(teams))
''',
    '''def ordered_matchup_from_title(title: str) -> tuple[str, str] | None:
    """Return provider teams in displayed order: away/first, home/second."""
    normalized = " ".join(str(title or "").split()).casefold()
    matches = sorted(
        (
            (normalized.find(team.casefold()), team)
            for team in NFL_TEAM_NAMES
            if team.casefold() in normalized
        ),
        key=lambda item: item[0],
    )
    if len(matches) != 2:
        return None
    return matches[0][1], matches[1][1]


def matchup_key_from_title(title: str) -> tuple[str, str] | None:
    """Return an unordered key for diagnostics and compatibility."""
    matchup = ordered_matchup_from_title(title)
    return tuple(sorted(matchup)) if matchup else None
''',
)

replace_once(
    SCHEDULE,
    '''    matches = [
        row for row in rows if matchup_key_from_title(row.title) == game.matchup_key
    ]
''',
    '''    expected_order = (game.away_team, game.home_team)
    matches = [
        row
        for row in rows
        if ordered_matchup_from_title(row.title) == expected_order
    ]
''',
)

replace_once(
    SCHEDULE,
    '''    if matchup_key_from_title(title) != scheduled.matchup_key:
        raise ValueError(
            f"Captured title does not match scheduled teams: {title}"
        )
''',
    '''    expected_order = (scheduled.away_team, scheduled.home_team)
    if ordered_matchup_from_title(title) != expected_order:
        raise ValueError(
            "Captured title does not match scheduled teams in away/home order: "
            f"{title}"
        )
''',
)

# Regression tests for the exact reverse-matchup failure.
SCHEDULE_TEST = "tests/test_nfl_schedule_collector.py"
replace_once(
    SCHEDULE_TEST,
    '''    candidates_for_schedule_game,
    matchup_key_from_title,
    parse_schedule_payload,
''',
    '''    candidates_for_schedule_game,
    matchup_key_from_title,
    ordered_matchup_from_title,
    parse_schedule_payload,
''',
)

replace_once(
    SCHEDULE_TEST,
    '''    def test_matchup_detection_ignores_promotional_prefixes(self):
        self.assertEqual(
            matchup_key_from_title(
                "Deals Available NFL Week 1 - Buffalo Bills at Houston Texans"
            ),
            self.game.matchup_key,
        )
''',
    '''    def test_matchup_detection_ignores_promotional_prefixes(self):
        title = "Deals Available NFL Week 1 - Buffalo Bills at Houston Texans"
        self.assertEqual(matchup_key_from_title(title), self.game.matchup_key)
        self.assertEqual(
            ordered_matchup_from_title(title),
            ("Buffalo Bills", "Houston Texans"),
        )

    def test_reverse_home_away_matchup_is_rejected(self):
        scheduled = ScheduledNFLGame(
            schedule_id="rams-home",
            event_date=datetime(2026, 9, 17, 0, 15, tzinfo=timezone.utc),
            away_team="San Francisco 49ers",
            home_team="Los Angeles Rams",
            venue="SoFi Stadium",
            name="San Francisco 49ers at Los Angeles Rams",
        )
        reverse = DiscoveredNFLGame(
            url=(
                "https://www.vividseats.com/san-francisco-49ers-tickets-"
                "santa-clara-levis-stadium/production/6495873"
            ),
            title="Los Angeles Rams at San Francisco 49ers",
            date_hint=datetime(2026, 12, 13).date(),
        )
        correct = DiscoveredNFLGame(
            url=(
                "https://www.vividseats.com/los-angeles-rams-tickets-"
                "inglewood-sofi-stadium/production/7000001"
            ),
            title="San Francisco 49ers at Los Angeles Rams",
            date_hint=datetime(2026, 9, 16).date(),
        )

        candidates = candidates_for_schedule_game(scheduled, [reverse, correct])
        self.assertEqual([row.url for row in candidates], [correct.url])
        self.assertEqual(candidates_for_schedule_game(scheduled, [reverse]), ())
''',
)

replace_once(
    SCHEDULE_TEST,
    '''        with self.assertRaisesRegex(ValueError, "scheduled teams"):
            validate_captured_match(
                self.game,
                self.game.event_date,
                "Dallas Cowboys at New York Giants",
            )

        with self.assertRaisesRegex(ValueError, "differs"):
''',
    '''        with self.assertRaisesRegex(ValueError, "scheduled teams"):
            validate_captured_match(
                self.game,
                self.game.event_date,
                "Dallas Cowboys at New York Giants",
            )

        with self.assertRaisesRegex(ValueError, "away/home order"):
            validate_captured_match(
                self.game,
                self.game.event_date,
                "Houston Texans at Buffalo Bills",
            )

        with self.assertRaisesRegex(ValueError, "differs"):
''',
)

# ---------------------------------------------------------------------------
# Restore baseball as the public root while retaining /baseball as an alias.
# ---------------------------------------------------------------------------
FLASK_APP = "Flask_App/flask_app.py"
replace_once(
    FLASK_APP,
    '''from Flask_App.nfl_blueprint import nfl_blueprint, nfl_home

app.register_blueprint(nfl_blueprint)


@app.get("/")
def landing():
    """Serve the NFL tracker as the site's default homepage."""
    return nfl_home()

MAX_SNAPSHOT_REPLAY_AGE = timedelta(days=7)
''',
    '''from Flask_App.nfl_blueprint import nfl_blueprint

app.register_blueprint(nfl_blueprint)

MAX_SNAPSHOT_REPLAY_AGE = timedelta(days=7)
''',
)

replace_once(
    FLASK_APP,
    '''@app.route("/baseball", methods=["GET", "POST"])
def home():
''',
    '''@app.route("/baseball", methods=["GET", "POST"])
@app.route("/", methods=["GET", "POST"])
def home():
''',
)

BASE = "Flask_App/templates/base.html"
for old, new in (
    ("href=\"{{ url_for('landing') }}\" aria-label=\"TicketSignal home\"", "href=\"{{ url_for('home') }}\" aria-label=\"TicketSignal home\""),
    ("{% if request.endpoint == 'landing' or (request.endpoint and request.endpoint.startswith('nfl.')) %}", "{% if request.endpoint and request.endpoint.startswith('nfl.') %}"),
    ("href=\"{{ url_for('landing') }}#how-it-works\"", "href=\"{{ url_for('home') }}#how-it-works\""),
    ("href=\"{{ url_for('landing') }}\">", "href=\"{{ url_for('home') }}\">"),
):
    replace_once(BASE, old, new)

README = "README.md"
replace_once(
    README,
    '''- `/`: NFL analysis and the default public homepage.
- `/nfl`: direct NFL analysis route.
- `/baseball`: baseball analysis.
''',
    '''- `/`: baseball analysis and the default public homepage.
- `/baseball`: direct baseball alias.
- `/nfl`: NFL analysis.
''',
)

STARTUP_TEST = "tests/test_flask_production_startup.py"
replace_once(
    STARTUP_TEST,
    '''                homepage = client.get("/")
                assert homepage.status_code == 200
                assert b"NFL market tracker" in homepage.data
                assert b"Dallas Cowboys" in homepage.data

                nfl_page = client.get("/nfl")
                assert nfl_page.status_code == 200
                assert b"Dallas Cowboys" in nfl_page.data

                baseball_page = client.get("/baseball")
                assert baseball_page.status_code == 200
''',
    '''                homepage = client.get("/")
                assert homepage.status_code == 200
                assert b"Ticket price intelligence" in homepage.data
                assert b"NFL market tracker" not in homepage.data

                baseball_alias = client.get("/baseball")
                assert baseball_alias.status_code == 200
                assert b"Ticket price intelligence" in baseball_alias.data

                nfl_page = client.get("/nfl")
                assert nfl_page.status_code == 200
                assert b"NFL market tracker" in nfl_page.data
                assert b"Dallas Cowboys" in nfl_page.data
''',
)
