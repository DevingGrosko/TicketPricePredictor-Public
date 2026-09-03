from __future__ import annotations

import ast
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def replace_function(source: str, name: str, replacement: str) -> str:
    tree = ast.parse(source)
    nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    if len(nodes) != 1:
        raise RuntimeError(f"Expected one {name}, found {len(nodes)}")
    node = nodes[0]
    lines = source.splitlines(keepends=True)
    return "".join(
        lines[: node.lineno - 1]
        + [replacement.rstrip() + "\n\n"]
        + lines[node.end_lineno :]
    )


def function_segment(source: str, name: str):
    tree = ast.parse(source)
    nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(nodes) != 1:
        raise RuntimeError(f"Expected one {name}, found {len(nodes)}")
    node = nodes[0]
    lines = source.splitlines(keepends=True)
    return lines, node, "".join(lines[node.lineno - 1 : node.end_lineno])


def replace_in_function(
    source: str,
    name: str,
    old: str,
    new: str,
    count: int = 1,
) -> str:
    lines, node, segment = function_segment(source, name)
    if old not in segment:
        raise RuntimeError(f"Pattern missing in {name}: {old[:100]!r}")
    segment = segment.replace(old, new, count)
    return "".join(lines[: node.lineno - 1] + [segment] + lines[node.end_lineno :])


def insert_before_function(source: str, name: str, addition: str) -> str:
    tree = ast.parse(source)
    nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(nodes) != 1:
        raise RuntimeError(name)
    node = nodes[0]
    lines = source.splitlines(keepends=True)
    return "".join(
        lines[: node.lineno - 1]
        + [addition.rstrip() + "\n\n"]
        + lines[node.lineno - 1 :]
    )


def write_cache_module() -> None:
    (ROOT / "Flask_App/performance_cache.py").write_text(
        '''"""Cross-worker cache for expensive read-only website views.

Keys include SQLite file signatures, so a newly stored snapshot naturally
produces a cache miss in every web worker. Values are written atomically to
``/tmp`` so separate PythonAnywhere workers can share a warm result.
"""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import pickle
import tempfile
import threading
import time
from typing import Any, Callable, TypeVar


T = TypeVar("T")
DEFAULT_TTL_SECONDS = max(30, int(os.environ.get("TICKETSIGNAL_CACHE_TTL", "600")))
_CACHE_VERSION = "page-performance-v1"
_CACHE_DIR = Path(
    os.environ.get(
        "TICKETSIGNAL_CACHE_DIR",
        str(Path(tempfile.gettempdir()) / "ticketsignal-page-cache"),
    )
)
_MEMORY: dict[str, tuple[float, Any]] = {}
_LOCK = threading.RLock()
_KEY_LOCKS: dict[str, threading.Lock] = {}


def database_signature(path: str | Path) -> tuple[tuple[str, int, int], ...]:
    """Return a cheap version token for a SQLite database and any WAL files."""

    base = Path(path).expanduser().resolve()
    rows: list[tuple[str, int, int]] = []
    for candidate in (base, Path(f"{base}-wal"), Path(f"{base}-shm")):
        try:
            stat = candidate.stat()
        except OSError:
            continue
        rows.append((candidate.name, stat.st_mtime_ns, stat.st_size))
    return tuple(rows) or ((base.name, 0, 0),)


def _digest(namespace: str, key: Any) -> str:
    payload = pickle.dumps((_CACHE_VERSION, namespace, key), protocol=5)
    return sha256(payload).hexdigest()


def _cache_file(digest: str) -> Path:
    return _CACHE_DIR / f"{digest}.pickle"


def _read_disk(digest: str, now: float) -> Any | None:
    path = _cache_file(digest)
    try:
        with path.open("rb") as handle:
            expires_at, value = pickle.load(handle)
    except (OSError, EOFError, pickle.PickleError, ValueError, TypeError):
        return None
    if float(expires_at) <= now:
        try:
            path.unlink()
        except OSError:
            pass
        return None
    with _LOCK:
        _MEMORY[digest] = (float(expires_at), value)
    return value


def _write_disk(digest: str, expires_at: float, value: Any) -> None:
    temporary = None
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        temporary = _CACHE_DIR / (
            f".{digest}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        with temporary.open("wb") as handle:
            pickle.dump((expires_at, value), handle, protocol=5)
        os.replace(temporary, _cache_file(digest))
    except (OSError, pickle.PickleError, TypeError):
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def get_or_build(
    namespace: str,
    key: Any,
    builder: Callable[[], T],
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> T:
    """Return a cached value, building it once per process on a miss."""

    digest = _digest(namespace, key)
    now = time.time()
    with _LOCK:
        memory = _MEMORY.get(digest)
        if memory is not None and memory[0] > now:
            return memory[1]
        key_lock = _KEY_LOCKS.setdefault(digest, threading.Lock())

    with key_lock:
        now = time.time()
        with _LOCK:
            memory = _MEMORY.get(digest)
            if memory is not None and memory[0] > now:
                return memory[1]
        disk = _read_disk(digest, now)
        if disk is not None:
            return disk

        value = builder()
        expires_at = time.time() + max(int(ttl_seconds), 1)
        with _LOCK:
            _MEMORY[digest] = (expires_at, value)
        _write_disk(digest, expires_at, value)
        return value


def invalidate_all() -> None:
    """Clear this worker's cache and shared on-disk entries after an ingest."""

    with _LOCK:
        _MEMORY.clear()
    try:
        for path in _CACHE_DIR.glob("*.pickle"):
            try:
                path.unlink()
            except OSError:
                pass
    except OSError:
        pass
'''
    )


def update_models() -> None:
    path = ROOT / "models.py"
    text = path.read_text()
    marker = '''def database_path() -> Path:\n    configured = os.environ.get("DATABASE_PATH", str(PROJECT_DIR / "Event-collection.db"))\n    return Path(configured).expanduser().resolve()\n\n\n'''
    addition = marker + '''_BASEBALL_INDEX_LOCK = threading.Lock()\n_BASEBALL_INDEX_READY: set[str] = set()\n\n\ndef _ensure_baseball_indexes(engine: Any, db_path: Path) -> None:\n    """Add indexes used by venue and section report queries to existing DBs."""\n\n    key = str(db_path)\n    with _BASEBALL_INDEX_LOCK:\n        if key in _BASEBALL_INDEX_READY:\n            return\n        with engine.begin() as connection:\n            connection.exec_driver_sql(\n                "CREATE INDEX IF NOT EXISTS ix_iterations_event_id "\n                "ON iterations (event_id)"\n            )\n            connection.exec_driver_sql(\n                "CREATE INDEX IF NOT EXISTS ix_iterations_event_captured "\n                "ON iterations (event_id, captured_at)"\n            )\n            connection.exec_driver_sql(\n                "CREATE INDEX IF NOT EXISTS ix_tickets_iteration_id "\n                "ON tickets (iteration_id)"\n            )\n            connection.exec_driver_sql(\n                "CREATE INDEX IF NOT EXISTS ix_tickets_section_iteration "\n                "ON tickets (section, iteration_id)"\n            )\n        _BASEBALL_INDEX_READY.add(key)\n\n\n'''
    if "_ensure_baseball_indexes" not in text:
        if marker not in text:
            raise RuntimeError("database_path marker missing")
        text = text.replace(marker, addition, 1)
        text = text.replace(
            '''        if not db_path.exists():\n            raise FileNotFoundError(f"Database file missing: {db_path}")\n\n        self.SessionLocal = sessionmaker(''',
            '''        if not db_path.exists():\n            raise FileNotFoundError(f"Database file missing: {db_path}")\n        _ensure_baseball_indexes(self.engine, db_path)\n\n        self.SessionLocal = sessionmaker(''',
            1,
        )
    path.write_text(text)


def update_flask_app() -> None:
    path = ROOT / "Flask_App/flask_app.py"
    text = path.read_text()
    text = text.replace("from dataclasses import replace\n", "from dataclasses import replace\nimport gzip\n", 1)
    text = text.replace("from sqlalchemy import select\n", "from sqlalchemy import select\nfrom sqlalchemy.orm import load_only\n", 1)
    text = text.replace("    Event,\n    Iteration,\n", "    Event,\n    Iteration,\n    database_path,\n", 1)
    text = text.replace(
        "from Flask_App.nfl_stadium_blueprint import nfl_stadium_blueprint\n",
        "from Flask_App.nfl_stadium_blueprint import is_parking_section, nfl_stadium_blueprint\n",
        1,
    )
    perf_import = '''from Flask_App.performance_cache import (\n    database_signature,\n    get_or_build as cache_get_or_build,\n    invalidate_all as invalidate_page_cache,\n)\n'''
    if perf_import not in text:
        text = text.replace(
            "from Flask_App.nhl_blueprint import nhl_blueprint\n",
            "from Flask_App.nhl_blueprint import nhl_blueprint\n" + perf_import,
            1,
        )

    text = replace_in_function(
        text,
        "ingest_collector_snapshot",
        '''    return jsonify(\n        {\n            "status": "stored",''',
        '''    invalidate_page_cache()\n\n    return jsonify(\n        {\n            "status": "stored",''',
    )
    text = replace_in_function(
        text,
        "ingest_concert_snapshot",
        '''    status = "stored" if stored else "duplicate"\n''',
        '''    if stored:\n        invalidate_page_cache()\n    status = "stored" if stored else "duplicate"\n''',
    )

    text = replace_function(
        text,
        "home",
        '''@app.route("/baseball", methods=["GET", "POST"])
@app.route("/", methods=["GET", "POST"])
def home():
    signature = database_signature(database_path())

    def render_home() -> str:
        SessionLocal = CreateModel().getSession()
        with SessionLocal() as session:
            data = [
                event
                for event in (
                    session.query(Event)
                    .options(
                        load_only(
                            Event.id,
                            Event.title,
                            Event.event_date,
                            Event.URL,
                            Event.Place,
                        )
                    )
                    .order_by(Event.event_date)
                    .all()
                )
                if is_baseball_event(event)
                and event_has_complete_public_data(event)
            ]

            games_dict: dict[str, list[dict[str, str]]] = {}
            for event in data:
                if not event.Place:
                    continue
                games_dict.setdefault(event.Place, []).append(
                    {"value": str(event.id), "label": format_event_title(event)}
                )
            games_dict = dict(sorted(games_dict.items()))

        return render_template(
            "HomeScreen.html",
            games_dict=games_dict,
            venue_count=len(games_dict),
            event_count=len(data),
        )

    return cache_get_or_build("html:mlb-home", signature, render_home)
''',
    )

    api_code = '''@app.get("/api/baseball/sections")
def baseball_sections_api():
    place = " ".join((request.args.get("place") or "").split())
    game_id = request.args.get("game") or ""
    mode = request.args.get("mode") or "single"
    signature = database_signature(database_path())

    def load_sections() -> list[str]:
        if not place:
            return []
        SessionLocal = CreateModel().getSession()
        with SessionLocal() as session:
            query = (
                session.query(Event)
                .options(
                    load_only(
                        Event.id,
                        Event.event_date,
                        Event.event_sections,
                        Event.URL,
                        Event.Place,
                    )
                )
                .filter(Event.Place == place)
            )
            if game_id.isdigit():
                query = query.filter(Event.id == int(game_id))
            events = [
                event
                for event in query.order_by(Event.event_date).all()
                if is_baseball_event(event)
                and event_has_complete_public_data(event)
            ]

        if game_id.isdigit():
            values = events[0].event_sections if len(events) == 1 else []
            return sorted(
                {
                    " ".join(str(section or "").split())
                    for section in values or []
                    if str(section or "").strip()
                    and not is_parking_section(section)
                },
                key=str.casefold,
            )

        games_by_section: dict[str, set[int]] = {}
        for event in events:
            for section in set(event.event_sections or []):
                cleaned = " ".join(str(section or "").split())
                if not cleaned or is_parking_section(cleaned):
                    continue
                games_by_section.setdefault(cleaned, set()).add(event.id)
        minimum_games = 2 if mode in {"multi", "timing"} else 1
        return sorted(
            (
                section
                for section, game_ids in games_by_section.items()
                if len(game_ids) >= minimum_games
            ),
            key=str.casefold,
        )

    sections = cache_get_or_build(
        "api:mlb-sections",
        (signature, place, game_id, mode),
        load_sections,
    )
    return jsonify({"sections": sections})
'''
    if "def baseball_sections_api" not in text:
        text = insert_before_function(text, "graph", api_code)

    gzip_code = '''@app.after_request
def compress_text_responses(response):
    """Compress sizeable HTML/JSON responses when the client accepts gzip."""

    accepted = request.headers.get("Accept-Encoding", "").casefold()
    if (
        "gzip" not in accepted
        or response.direct_passthrough
        or response.status_code < 200
        or response.status_code >= 300
        or response.headers.get("Content-Encoding")
    ):
        return response
    mimetype = response.mimetype or ""
    if not (
        mimetype.startswith("text/")
        or mimetype in {"application/json", "application/javascript", "image/svg+xml"}
    ):
        return response
    data = response.get_data()
    if len(data) < 1024:
        return response
    compressed = gzip.compress(data, compresslevel=5)
    if len(compressed) >= len(data):
        return response
    response.set_data(compressed)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(compressed))
    response.headers["Vary"] = "Accept-Encoding"
    return response
'''
    if "def compress_text_responses" not in text:
        text = text.replace(
            "\n\ndef authorized_collector_request():",
            "\n\n" + gzip_code + "\n\ndef authorized_collector_request():",
            1,
        )
    ast.parse(text)
    path.write_text(text)


def update_mlb_home() -> None:
    path = ROOT / "Flask_App/templates/HomeScreen.html"
    text = path.read_text()
    text = re.sub(
        r'\n  \{% set visible_sections = namespace\(names=\[\]\) %\}.*?\n<div class="nfl-page mlb-page">',
        '\n<div class="nfl-page mlb-page">',
        text,
        count=1,
        flags=re.S,
    )
    text = text.replace(
        '        <div><dt>{{ visible_sections.names|length }}</dt><dd>Sections compared</dd></div>\n',
        "",
    )
    text = text.replace("{% for place in event_dict %}", "{% for place in games_dict %}")
    text = text.replace("  const placesData = {{ event_dict | tojson }};\n", "")
    text = text.replace("  const gameSectionsData = {{ game_sections_dict | tojson }};\n", "")
    path.write_text(text)

    (ROOT / "Flask_App/static/js/script.js").write_text(
        r'''document.addEventListener("DOMContentLoaded", () => {
  const tabs = Array.from(document.querySelectorAll(".analysis-tab"));
  const panels = Array.from(document.querySelectorAll(".analysis-panel"));

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      const target = tab.dataset.target;
      tabs.forEach((item) => {
        const active = item === tab;
        item.classList.toggle("is-active", active);
        item.setAttribute("aria-selected", active ? "true" : "false");
      });
      panels.forEach((panel) => {
        const active = panel.id === target;
        panel.classList.toggle("is-active", active);
        panel.hidden = !active;
      });
    });
  });

  const responseCache = new Map();

  async function loadSections(place, options = {}) {
    const params = new URLSearchParams({ place });
    if (options.game) params.set("game", options.game);
    if (options.mode) params.set("mode", options.mode);
    const url = `/api/baseball/sections?${params.toString()}`;
    if (!responseCache.has(url)) {
      responseCache.set(
        url,
        fetch(url, { headers: { Accept: "application/json" } }).then((response) => {
          if (!response.ok) throw new Error(`Section request failed: ${response.status}`);
          return response.json();
        })
      );
    }
    return responseCache.get(url);
  }

  function replaceOptions(select, rows, placeholder) {
    select.innerHTML = "";
    const first = document.createElement("option");
    first.value = "";
    first.textContent = placeholder;
    select.append(first);
    rows.forEach((row) => {
      const option = document.createElement("option");
      option.value = typeof row === "string" ? row : row.value;
      option.textContent = typeof row === "string" ? row : row.label;
      select.append(option);
    });
    select.disabled = rows.length === 0;
  }

  document.querySelectorAll(".selection-form").forEach((form) => {
    const analysis = form.dataset.analysis || "market";
    const place = form.querySelector(".place-select");
    const game = form.querySelector(".game-select");
    const section = form.querySelector(".section-select");
    const submit = form.querySelector(".submit-analysis");
    if (!place || !section || !submit) return;

    const syncSubmit = () => {
      submit.disabled = !place.value || !section.value ||
        (analysis === "game" && (!game || !game.value));
    };

    place.addEventListener("change", async () => {
      replaceOptions(section, [], "Select a section");
      if (game) replaceOptions(game, [], "Select a game");
      syncSubmit();
      if (!place.value) return;

      if (analysis === "game") {
        const games = (gamesData || {})[place.value] || [];
        replaceOptions(game, games, "Select a game");
        return;
      }

      section.disabled = true;
      section.innerHTML = '<option value="">Loading sections…</option>';
      try {
        const payload = await loadSections(place.value, {
          mode: analysis === "timing" ? "timing" : "multi",
        });
        replaceOptions(section, payload.sections || [], "Select a section");
      } catch (_error) {
        replaceOptions(section, [], "Sections unavailable");
      }
      syncSubmit();
    });

    if (game) {
      game.addEventListener("change", async () => {
        replaceOptions(section, [], "Select a section");
        syncSubmit();
        if (!place.value || !game.value) return;
        section.disabled = true;
        section.innerHTML = '<option value="">Loading sections…</option>';
        try {
          const payload = await loadSections(place.value, {
            game: game.value,
            mode: "single",
          });
          replaceOptions(section, payload.sections || [], "Select a section");
        } catch (_error) {
          replaceOptions(section, [], "Sections unavailable");
        }
        syncSubmit();
      });
    }
    section.addEventListener("change", syncSubmit);

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (submit.disabled) return;
      const params = new URLSearchParams({
        event: place.value,
        section: section.value,
      });
      if (analysis === "game") {
        params.set("game", game.value);
        params.set("mode", "single");
        params.set("display", "money");
        window.location.assign(`/graph?${params.toString()}`);
      } else if (analysis === "timing") {
        window.location.assign(`/predict?${params.toString()}`);
      } else {
        params.set("mode", "multi");
        params.set("display", "money");
        window.location.assign(`/graph?${params.toString()}`);
      }
    });
  });
});
'''
    )


def patch_league_blueprint(filename: str, league: str) -> None:
    path = ROOT / f"Flask_App/{filename}"
    text = path.read_text()
    perf = '''from Flask_App.performance_cache import (\n    database_signature,\n    get_or_build as cache_get_or_build,\n    invalidate_all as invalidate_page_cache,\n)\nfrom Flask_App.section_canonicalization import is_excluded_ticket_area\n'''
    if "from Flask_App.performance_cache import" not in text:
        marker = "from graph_builder import GraphBuilder\n"
        if marker not in text:
            raise RuntimeError(f"{league} import marker missing")
        text = text.replace(marker, marker + perf, 1)

    status_line = '    status = "stored" if stored else "duplicate"\n'
    if "invalidate_page_cache()" not in text:
        if status_line not in text:
            raise RuntimeError(f"{league} status marker missing")
        text = text.replace(
            status_line,
            "    if stored:\n        invalidate_page_cache()\n" + status_line,
            1,
        )

    home_name = f"{league}_home"
    context_name = f"_{league}_home_context"
    db_func = f"{league}_database_path"
    template = "NFLHomeScreen.html" if league == "nfl" else "NHLHomeScreen.html"
    text = replace_function(
        text,
        home_name,
        f'''@{league}_blueprint.get("/{league}")
def {home_name}():
    signature = database_signature({db_func}())
    return cache_get_or_build(
        "html:{league}-home",
        signature,
        lambda: render_template("{template}", **{context_name}()),
    )
''',
    )

    api_name = f"{league}_sections_api"
    api_code = f'''@{league}_blueprint.get("/api/{league}/sections")
def {api_name}():
    selection = request.args.get("team") or request.args.get("event") or ""
    game_id = request.args.get("game") or ""
    signature = database_signature({db_func}())

    def load_sections() -> list[str]:
        event = find_{league}_game(selection, game_id)
        if event is None:
            return []
        return sorted(
            {{
                " ".join(str(section or "").split())
                for section in (event.sections or [])
                if str(section or "").strip()
                and not is_excluded_ticket_area(section)
            }},
            key=str.casefold,
        )

    sections = cache_get_or_build(
        "api:{league}-sections",
        (signature, selection, game_id),
        load_sections,
    )
    return jsonify({{"sections": sections}})
'''
    if f"def {api_name}" not in text:
        text = insert_before_function(text, f"{league}_map", api_code)
    ast.parse(text)
    path.write_text(text)


def update_league_homes() -> None:
    patch_league_blueprint("nfl_blueprint.py", "nfl")
    patch_league_blueprint("nhl_blueprint.py", "nhl")

    for template_name, constant in (
        ("NFLHomeScreen.html", "nflGameSectionsData"),
        ("NHLHomeScreen.html", "nhlGameSectionsData"),
    ):
        path = ROOT / f"Flask_App/templates/{template_name}"
        text = path.read_text()
        text = re.sub(
            rf"\n  const {constant} = .*?;\n",
            "\n",
            text,
            count=1,
        )
        path.write_text(text)

    (ROOT / "Flask_App/static/js/nfl.js").write_text(
        r'''document.addEventListener("DOMContentLoaded", () => {
  const form = document.querySelector(".nfl-selection-form");
  if (!form) return;
  const team = form.querySelector(".place-select");
  const game = form.querySelector(".game-select");
  const section = form.querySelector(".section-select");
  const submit = form.querySelector(".submit-analysis");
  const mapButton = form.querySelector("[data-map-launch]");

  function setOptions(select, rows, placeholder) {
    select.innerHTML = "";
    const first = document.createElement("option");
    first.value = "";
    first.textContent = placeholder;
    select.append(first);
    rows.forEach((row) => {
      const option = document.createElement("option");
      option.value = typeof row === "string" ? row : row.value;
      option.textContent = typeof row === "string" ? row : row.label;
      select.append(option);
    });
    select.disabled = rows.length === 0;
  }

  function sync() {
    const ready = Boolean(team.value && game.value && section.value);
    submit.disabled = !ready;
    mapButton.disabled = !ready;
  }

  team.addEventListener("change", () => {
    setOptions(game, (nflGamesData || {})[team.value] || [], "Select a game");
    setOptions(section, [], "Select a section");
    sync();
  });

  game.addEventListener("change", async () => {
    setOptions(section, [], "Select a section");
    sync();
    if (!team.value || !game.value) return;
    section.disabled = true;
    section.innerHTML = '<option value="">Loading sections…</option>';
    try {
      const params = new URLSearchParams({ team: team.value, game: game.value });
      const response = await fetch(`/api/nfl/sections?${params.toString()}`);
      if (!response.ok) throw new Error("request failed");
      const payload = await response.json();
      setOptions(section, payload.sections || [], "Select a section");
    } catch (_error) {
      setOptions(section, [], "Sections unavailable");
    }
    sync();
  });

  section.addEventListener("change", sync);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (submit.disabled) return;
    const params = new URLSearchParams({
      team: team.value,
      game: game.value,
      section: section.value,
      display: "money",
    });
    window.location.assign(`/nfl/graph?${params.toString()}`);
  });
  mapButton.addEventListener("click", () => {
    if (mapButton.disabled) return;
    const params = new URLSearchParams({
      team: team.value,
      game: game.value,
      section: section.value,
    });
    window.location.assign(`${mapButton.dataset.mapBase}?${params.toString()}`);
  });
});
'''
    )

    (ROOT / "Flask_App/static/js/nhl.js").write_text(
        r'''document.addEventListener("DOMContentLoaded", () => {
  const form = document.querySelector(".nhl-selection-form");
  if (!form) return;
  const team = form.querySelector(".place-select");
  const game = form.querySelector(".game-select");
  const section = form.querySelector(".section-select");
  const submit = form.querySelector(".submit-analysis");
  const mapButton = form.querySelector("[data-map-launch]");

  function setOptions(select, rows, placeholder) {
    select.innerHTML = "";
    const first = document.createElement("option");
    first.value = "";
    first.textContent = placeholder;
    select.append(first);
    rows.forEach((row) => {
      const option = document.createElement("option");
      option.value = typeof row === "string" ? row : row.value;
      option.textContent = typeof row === "string" ? row : row.label;
      select.append(option);
    });
    select.disabled = rows.length === 0;
  }

  function sync() {
    const ready = Boolean(team.value && game.value && section.value);
    submit.disabled = !ready;
    mapButton.disabled = !ready;
  }

  team.addEventListener("change", () => {
    setOptions(game, (nhlGamesData || {})[team.value] || [], "Select a game");
    setOptions(section, [], "Select a section");
    sync();
  });

  game.addEventListener("change", async () => {
    setOptions(section, [], "Select a section");
    sync();
    if (!team.value || !game.value) return;
    section.disabled = true;
    section.innerHTML = '<option value="">Loading sections…</option>';
    try {
      const params = new URLSearchParams({ team: team.value, game: game.value });
      const response = await fetch(`/api/nhl/sections?${params.toString()}`);
      if (!response.ok) throw new Error("request failed");
      const payload = await response.json();
      setOptions(section, payload.sections || [], "Select a section");
    } catch (_error) {
      setOptions(section, [], "Sections unavailable");
    }
    sync();
  });

  section.addEventListener("change", sync);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (submit.disabled) return;
    const params = new URLSearchParams({
      team: team.value,
      game: game.value,
      section: section.value,
      display: "money",
    });
    window.location.assign(`/nhl/graph?${params.toString()}`);
  });
  mapButton.addEventListener("click", () => {
    if (mapButton.disabled) return;
    const params = new URLSearchParams({
      team: team.value,
      game: game.value,
      section: section.value,
    });
    window.location.assign(`${mapButton.dataset.mapBase}?${params.toString()}`);
  });
});
'''
    )


def update_venue_reports() -> None:
    path = ROOT / "Flask_App/nfl_stadium_blueprint.py"
    text = path.read_text()
    text = text.replace(
        "from sqlalchemy import select\n",
        "from sqlalchemy import and_, case, func, select\n",
        1,
    )
    text = text.replace(
        "    CreateModel,\n    Event,\n",
        "    CreateModel,\n    Event,\n    database_path,\n",
        1,
    )
    text = text.replace(
        "    nfl_display_venue,\n",
        "    nfl_database_path,\n    nfl_display_venue,\n",
        1,
    )
    text = text.replace(
        "    nhl_display_venue,\n",
        "    nhl_database_path,\n    nhl_display_venue,\n",
        1,
    )
    perf_import = '''from Flask_App.performance_cache import (\n    database_signature,\n    get_or_build as cache_get_or_build,\n)\n'''
    if perf_import not in text:
        marker = "from Flask_App.section_canonicalization import section_identity\n"
        if marker not in text:
            raise RuntimeError("section identity import marker missing")
        text = text.replace(marker, marker + perf_import, 1)

    summary_helpers = '''def _bucket_summary_rows_for(
    session: Any,
    events: list[Any],
    iteration_model: Any,
    ticket_model: Any,
    sport_key: str,
    *,
    section_name: str = "",
) -> list[Any]:
    """Return one exact median per event/section/time bucket from SQLite."""

    if not events:
        return []
    iteration = iteration_model.__table__
    ticket = ticket_model.__table__
    iteration_id = _column(iteration, "id")
    iteration_event_id = _column(iteration, "event_id")
    captured_at = _column(iteration, "captured_at", "created_at")
    ticket_iteration_id = _column(ticket, "iteration_id")
    ticket_section = _column(ticket, "section", "section_name")
    ticket_price = _column(ticket, "price")

    event_utc = {
        int(event.id): event_datetime_eastern(event.event_date)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
        for event in events
    }
    event_utc_expression = case(
        *(
            (iteration_event_id == event_id, event_time)
            for event_id, event_time in event_utc.items()
        ),
        else_=None,
    )
    lead_hours = (
        func.julianday(event_utc_expression) - func.julianday(captured_at)
    ) * 24.0
    bucket_expression = case(
        *(
            (
                and_(
                    lead_hours > (0.0 if lower == 0 else lower),
                    lead_hours <= upper,
                ),
                slot,
            )
            for slot, (lower, upper, _short, _label) in enumerate(
                _TIMELINE_BUCKETS[sport_key]
            )
        ),
        else_=None,
    )

    statement = (
        select(
            iteration_event_id.label("event_id"),
            ticket_section.label("section"),
            bucket_expression.label("bucket_slot"),
            func.group_concat(ticket_price, ",").label("prices"),
            func.count(ticket_price).label("observation_count"),
        )
        .select_from(ticket.join(iteration, ticket_iteration_id == iteration_id))
        .where(
            iteration_event_id.in_(list(event_utc)),
            ticket_price > 0,
            bucket_expression.is_not(None),
        )
    )
    if section_name:
        statement = statement.where(ticket_section == section_name)
    statement = statement.group_by(
        iteration_event_id,
        ticket_section,
        bucket_expression,
    )

    rows: list[Any] = []
    for result in session.execute(statement):
        values = result._mapping
        event_id = int(values["event_id"])
        slot = int(values["bucket_slot"])
        prices = [
            float(value)
            for value in str(values["prices"] or "").split(",")
            if value
        ]
        if not prices:
            continue
        lower, upper, _short, _label = _TIMELINE_BUCKETS[sport_key][slot]
        synthetic_capture = event_utc[event_id] - timedelta(
            hours=(lower + upper) / 2
        )
        rows.append(
            {
                "event_id": event_id,
                "captured_at": synthetic_capture,
                "section": _clean(values["section"]),
                "price": float(median(prices)),
                "observation_count": int(
                    values["observation_count"] or len(prices)
                ),
            }
        )
    return rows


def _capture_times_for(
    session: Any,
    event_ids: list[int],
    iteration_model: Any,
) -> dict[int, set[datetime]]:
    if not event_ids:
        return {}
    iteration = iteration_model.__table__
    event_id = _column(iteration, "event_id")
    captured_at = _column(iteration, "captured_at", "created_at")
    statement = (
        select(event_id.label("event_id"), captured_at.label("captured_at"))
        .where(event_id.in_(event_ids))
        .distinct()
    )
    captures: dict[int, set[datetime]] = defaultdict(set)
    for row in session.execute(statement):
        captures[int(row.event_id)].add(row.captured_at)
    return captures
'''
    if "def _bucket_summary_rows_for" not in text:
        text = insert_before_function(text, "_section_insights", summary_helpers)

    text = replace_in_function(
        text,
        "_section_insights_for",
        '''    captures_by_event: dict[int, set[datetime]] = defaultdict(set)\n\n    for row in rows:\n''',
        '''    captures_by_event: dict[int, set[datetime]] = defaultdict(set)\n    raw_counts_by_history: dict[tuple[str, int], int] = defaultdict(int)\n\n    for row in rows:\n''',
    )
    text = replace_in_function(
        text,
        "_section_insights_for",
        '''        histories[(section, event_id)].append((captured, price))\n        captures_by_event[event_id].add(captured)\n''',
        '''        histories[(section, event_id)].append((captured, price))\n        raw_count = _row_value(row, "observation_count")\n        try:\n            raw_counts_by_history[(section, event_id)] += max(int(raw_count or 1), 1)\n        except (TypeError, ValueError):\n            raw_counts_by_history[(section, event_id)] += 1\n        captures_by_event[event_id].add(captured)\n''',
    )
    text = replace_in_function(
        text,
        "_section_insights_for",
        "        observation_count[section] += len(ordered)\n",
        "        observation_count[section] += raw_counts_by_history.get((section, event_id), len(ordered))\n",
    )

    text = replace_in_function(
        text,
        "build_nfl_stadium_context",
        '''            rows = _snapshot_rows(session, [event.id for event in selected_events])\n            sections, captures_by_event = _section_insights(selected_events, rows, now)\n''',
        '''            event_ids = [event.id for event in selected_events]\n            rows = _bucket_summary_rows_for(\n                session, selected_events, NFLIteration, NFLTicket, "nfl"\n            )\n            sections, _synthetic_captures = _section_insights(selected_events, rows, now)\n            captures_by_event = _capture_times_for(session, event_ids, NFLIteration)\n''',
    )
    text = replace_in_function(
        text,
        "build_mlb_stadium_context",
        '''            rows = _snapshot_rows_for(\n                session,\n                [event.id for event in selected_events],\n                Iteration,\n                Ticket,\n            )\n            sections, captures_by_event = _section_insights_for(\n''',
        '''            event_ids = [event.id for event in selected_events]\n            rows = _bucket_summary_rows_for(\n                session, selected_events, Iteration, Ticket, "mlb"\n            )\n            sections, _synthetic_captures = _section_insights_for(\n''',
    )
    text = replace_in_function(
        text,
        "build_mlb_stadium_context",
        '''                event_label_builder=format_mlb_title,\n            )\n            analyzed_area_count = _supported_area_count(selected_events, rows, "mlb")\n''',
        '''                event_label_builder=format_mlb_title,\n            )\n            captures_by_event = _capture_times_for(session, event_ids, Iteration)\n            analyzed_area_count = _supported_area_count(selected_events, rows, "mlb")\n''',
    )
    text = replace_in_function(
        text,
        "build_nhl_arena_context",
        '''            rows = _snapshot_rows_for(\n                session,\n                [event.id for event in analysis_events],\n                NHLIteration,\n                NHLTicket,\n            )\n            sections, captures_by_event = _section_insights_for(\n''',
        '''            event_ids = [event.id for event in analysis_events]\n            rows = _bucket_summary_rows_for(\n                session, analysis_events, NHLIteration, NHLTicket, "nhl"\n            )\n            sections, _synthetic_captures = _section_insights_for(\n''',
    )
    text = replace_in_function(
        text,
        "build_nhl_arena_context",
        '''                event_label_builder=format_nhl_title,\n            )\n            analyzed_area_count = _supported_area_count(analysis_events, rows, "nhl")\n''',
        '''                event_label_builder=format_nhl_title,\n            )\n            captures_by_event = _capture_times_for(session, event_ids, NHLIteration)\n            analyzed_area_count = _supported_area_count(analysis_events, rows, "nhl")\n''',
    )

    text = replace_in_function(
        text,
        "build_nfl_section_context",
        "    base_context = build_nfl_stadium_context(selected_venue)\n",
        "    base_context = cached_nfl_stadium_context(selected_venue)\n",
    )
    text = replace_in_function(
        text,
        "build_mlb_section_context",
        "    base_context = build_mlb_stadium_context(selected_venue)\n",
        "    base_context = cached_mlb_stadium_context(selected_venue)\n",
    )
    text = replace_in_function(
        text,
        "build_nhl_section_context",
        "    base_context = build_nhl_arena_context(selected_venue)\n",
        "    base_context = cached_nhl_arena_context(selected_venue)\n",
    )
    text = replace_in_function(
        text,
        "build_nfl_section_context",
        '''            rows = _snapshot_rows(session, [event.id for event in events])\n''',
        '''            rows = _bucket_summary_rows_for(\n                session,\n                events,\n                NFLIteration,\n                NFLTicket,\n                "nfl",\n                section_name=_clean(requested_section),\n            )\n''',
    )
    text = replace_in_function(
        text,
        "build_mlb_section_context",
        '''            rows = _snapshot_rows_for(\n                session,\n                [event.id for event in events],\n                Iteration,\n                Ticket,\n            )\n''',
        '''            rows = _bucket_summary_rows_for(\n                session,\n                events,\n                Iteration,\n                Ticket,\n                "mlb",\n                section_name=_clean(requested_section),\n            )\n''',
    )
    text = replace_in_function(
        text,
        "build_nhl_section_context",
        '''            rows = _snapshot_rows_for(\n                session,\n                [event.id for event in events],\n                NHLIteration,\n                NHLTicket,\n            )\n''',
        '''            rows = _bucket_summary_rows_for(\n                session,\n                events,\n                NHLIteration,\n                NHLTicket,\n                "nhl",\n                section_name=_clean(requested_section),\n            )\n''',
    )

    cache_helpers = '''def _sport_database_signature(sport_key: str) -> tuple[Any, ...]:
    path = {
        "mlb": database_path,
        "nfl": nfl_database_path,
        "nhl": nhl_database_path,
    }[sport_key]()
    return database_signature(path)


def cached_nfl_stadium_context(selected_venue: str = "") -> dict[str, Any]:
    selected = _clean(selected_venue)
    return cache_get_or_build(
        "context:nfl-stadium",
        (_sport_database_signature("nfl"), selected),
        lambda: build_nfl_stadium_context(selected),
    )


def cached_mlb_stadium_context(selected_venue: str = "") -> dict[str, Any]:
    selected = _clean(selected_venue)
    return cache_get_or_build(
        "context:mlb-stadium",
        (_sport_database_signature("mlb"), selected),
        lambda: build_mlb_stadium_context(selected),
    )


def cached_nhl_arena_context(selected_venue: str = "") -> dict[str, Any]:
    selected = _clean(selected_venue)
    return cache_get_or_build(
        "context:nhl-arena",
        (_sport_database_signature("nhl"), selected),
        lambda: build_nhl_arena_context(selected),
    )


def cached_section_context(
    sport_key: str,
    selected_venue: str,
    requested_section: str,
) -> dict[str, Any]:
    venue = _clean(selected_venue)
    section = _clean(requested_section)
    builders = {
        "nfl": build_nfl_section_context,
        "mlb": build_mlb_section_context,
        "nhl": build_nhl_section_context,
    }
    return cache_get_or_build(
        f"context:{sport_key}-section",
        (_sport_database_signature(sport_key), venue, section),
        lambda: builders[sport_key](venue, section),
    )
'''
    if "def cached_nfl_stadium_context" not in text:
        text = insert_before_function(text, "build_nfl_section_context", cache_helpers)

    text = replace_in_function(
        text,
        "nfl_stadium",
        '    context = build_nfl_stadium_context(request.args.get("venue", ""))\n',
        '    context = cached_nfl_stadium_context(request.args.get("venue", ""))\n',
    )
    text = replace_in_function(
        text,
        "mlb_stadium",
        '    context = build_mlb_stadium_context(request.args.get("venue", ""))\n',
        '    context = cached_mlb_stadium_context(request.args.get("venue", ""))\n',
    )
    text = replace_in_function(
        text,
        "nhl_arena",
        '    context = build_nhl_arena_context(request.args.get("venue", ""))\n',
        '    context = cached_nhl_arena_context(request.args.get("venue", ""))\n',
    )
    text = replace_function(
        text,
        "nfl_section",
        '''@nfl_stadium_blueprint.get("/nfl/stadium/section")
def nfl_section():
    context = cached_section_context(
        "nfl",
        request.args.get("venue") or request.args.get("team") or "",
        request.args.get("section", ""),
    )
    return render_template("venue_section.html", **context)
''',
    )
    text = replace_function(
        text,
        "mlb_section",
        '''@nfl_stadium_blueprint.get("/baseball/stadium/section")
def mlb_section():
    context = cached_section_context(
        "mlb",
        request.args.get("venue") or request.args.get("team") or "",
        request.args.get("section", ""),
    )
    return render_template("venue_section.html", **context)
''',
    )
    text = replace_function(
        text,
        "nhl_section",
        '''@nfl_stadium_blueprint.get("/nhl/arena/section")
def nhl_section():
    context = cached_section_context(
        "nhl",
        request.args.get("venue") or request.args.get("team") or "",
        request.args.get("section", ""),
    )
    return render_template("venue_section.html", **context)
''',
    )
    ast.parse(text)
    path.write_text(text)


def write_tests() -> None:
    (ROOT / "tests/test_performance_pass.py").write_text(
        '''from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from Flask_App.performance_cache import database_signature, get_or_build, invalidate_all
from Flask_App.nfl_stadium_blueprint import _bucket_summary_rows_for
from models import Base, Event, Iteration, Ticket, _ensure_baseball_indexes


class PageCacheTests(unittest.TestCase):
    def tearDown(self):
        invalidate_all()

    def test_cache_reuses_value_and_database_signature_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            path.write_bytes(b"one")
            first_signature = database_signature(path)
            calls = []
            value = get_or_build(
                "test", first_signature, lambda: calls.append(1) or "a"
            )
            again = get_or_build(
                "test", first_signature, lambda: calls.append(2) or "b"
            )
            self.assertEqual((value, again), ("a", "a"))
            self.assertEqual(calls, [1])
            path.write_bytes(b"two-two")
            self.assertNotEqual(first_signature, database_signature(path))


class BaseballIndexTests(unittest.TestCase):
    def test_existing_database_receives_report_indexes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseball.db"
            engine = create_engine(f"sqlite:///{path}")
            Base.metadata.create_all(engine)
            _ensure_baseball_indexes(engine, path)
            with sqlite3.connect(path) as connection:
                indexes = {
                    row[1] for row in connection.execute("PRAGMA index_list(tickets)")
                }
                iteration_indexes = {
                    row[1]
                    for row in connection.execute("PRAGMA index_list(iterations)")
                }
            self.assertIn("ix_tickets_section_iteration", indexes)
            self.assertIn("ix_iterations_event_captured", iteration_indexes)
            engine.dispose()


class BucketSummaryTests(unittest.TestCase):
    def test_sql_summary_returns_exact_bucket_median(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseball.db"
            engine = create_engine(f"sqlite:///{path}")
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine, expire_on_commit=False)
            event_time = datetime(2026, 9, 4, 19, 0)
            with Session() as session:
                event = Event(
                    title="Away at Home",
                    event_date=event_time,
                    event_sections=["101"],
                    URL=(
                        "https://www.vividseats.com/"
                        "x--sports-mlb-baseball/production/1"
                    ),
                    Place="Park",
                )
                session.add(event)
                session.flush()
                for index, price in enumerate((80, 100, 900)):
                    iteration = Iteration(
                        event_id=event.id,
                        captured_at=datetime(2026, 9, 3, 17, index),
                    )
                    session.add(iteration)
                    session.flush()
                    session.add(
                        Ticket(
                            section="101",
                            price=price,
                            iteration_id=iteration.id,
                        )
                    )
                session.commit()
                rows = _bucket_summary_rows_for(
                    session, [event], Iteration, Ticket, "mlb"
                )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["price"], 100.0)
            self.assertEqual(rows[0]["observation_count"], 3)
            engine.dispose()


class LazyPayloadTests(unittest.TestCase):
    def test_home_templates_do_not_embed_full_section_inventory(self):
        root = Path(__file__).resolve().parents[1]
        expectations = {
            "HomeScreen.html": "gameSectionsData",
            "NFLHomeScreen.html": "nflGameSectionsData",
            "NHLHomeScreen.html": "nhlGameSectionsData",
        }
        for name, marker in expectations.items():
            text = (root / "Flask_App/templates" / name).read_text()
            self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()
'''
    )


def main() -> None:
    write_cache_module()
    update_models()
    update_flask_app()
    update_mlb_home()
    update_league_homes()
    update_venue_reports()
    write_tests()

    for path in (
        ROOT / "models.py",
        ROOT / "Flask_App/flask_app.py",
        ROOT / "Flask_App/nfl_blueprint.py",
        ROOT / "Flask_App/nhl_blueprint.py",
        ROOT / "Flask_App/nfl_stadium_blueprint.py",
        ROOT / "Flask_App/performance_cache.py",
        ROOT / "tests/test_performance_pass.py",
    ):
        ast.parse(path.read_text())


if __name__ == "__main__":
    main()
