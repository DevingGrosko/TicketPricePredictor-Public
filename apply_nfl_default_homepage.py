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


replace_once(
    "Flask_App/flask_app.py",
    '''from Flask_App.nfl_blueprint import nfl_blueprint

app.register_blueprint(nfl_blueprint)
''',
    '''from Flask_App.nfl_blueprint import nfl_blueprint, nfl_home

app.register_blueprint(nfl_blueprint)


@app.get("/")
def landing():
    """Serve the NFL tracker as the site's default homepage."""
    return nfl_home()
''',
)

replace_once(
    "Flask_App/flask_app.py",
    '''@app.route("/", methods=["GET", "POST"])
def home():
''',
    '''@app.route("/baseball", methods=["GET", "POST"])
def home():
''',
)

replace_once(
    "Flask_App/templates/base.html",
    '''      <a class="brand" href="{{ url_for('home') }}" aria-label="TicketSignal home">''',
    '''      <a class="brand" href="{{ url_for('landing') }}" aria-label="TicketSignal home">''',
)

replace_once(
    "Flask_App/templates/base.html",
    '''        <a href="{{ url_for('home') }}">Baseball</a>
        <a href="{{ url_for('nfl.nfl_home') }}"{% if request.endpoint and request.endpoint.startswith('nfl.') %} class="is-active" aria-current="page"{% endif %}>NFL</a>
        <a href="{{ url_for('home') }}#how-it-works">How it works</a>''',
    '''        <a href="{{ url_for('home') }}"{% if request.endpoint in ('home', 'graph', 'predict') %} class="is-active" aria-current="page"{% endif %}>Baseball</a>
        <a href="{{ url_for('nfl.nfl_home') }}"{% if request.endpoint == 'landing' or (request.endpoint and request.endpoint.startswith('nfl.')) %} class="is-active" aria-current="page"{% endif %}>NFL</a>
        <a href="{{ url_for('landing') }}#how-it-works">How it works</a>''',
)

replace_once(
    "Flask_App/templates/base.html",
    '''        <a class="brand brand--footer" href="{{ url_for('home') }}">''',
    '''        <a class="brand brand--footer" href="{{ url_for('landing') }}">''',
)

replace_once(
    "Flask_App/templates/NFLHomeScreen.html",
    '''  <section class="nfl-data-bar" aria-label="NFL tracker methodology">''',
    '''  <section class="nfl-data-bar" id="how-it-works" aria-label="NFL tracker methodology">''',
)

replace_once(
    "tests/test_flask_production_startup.py",
    '''                nfl_page = client.get("/nfl")
                assert nfl_page.status_code == 200
                assert b"Dallas Cowboys" in nfl_page.data

                baseball_page = client.get("/")
                assert baseball_page.status_code == 200
''',
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
)

replace_once(
    "README.md",
    '''- `/`: baseball analysis.
- `/nfl`: NFL analysis.
''',
    '''- `/`: NFL analysis and the default public homepage.
- `/nfl`: direct NFL analysis route.
- `/baseball`: baseball analysis.
''',
)
