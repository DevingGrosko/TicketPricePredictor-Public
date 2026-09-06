from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hmac
import os
import re

from flask import Flask, jsonify, render_template, request
from sqlalchemy import select, text
from sqlalchemy.orm import load_only

from collector import (
    create_daily_backup,
    event_date_from_url,
    snapshot_from_payload,
    store_snapshot,
    write_capture_audit,
)
from graph_builder import ConcertGraphBuilder, GraphBuilder
from models import (
    ConcertEvent,
    CreateConcertModel,
    CreateModel,
    database_path,
    Event,
    Iteration,
    Ticket,
    captured_datetime_for_storage,
    clean_event_title,
    create_concert_daily_backup,
    event_datetime_eastern,
    event_has_complete_public_data,
    store_concert_snapshot,
    write_concert_audit,
)

from Flask_App.database_config import (
    configured_backend,
    dispose_ticket_engine,
    migration_pause_active,
)
from Flask_App.performance_cache import (
    OPTIONS_CACHE_TTL_SECONDS,
    PAGE_CACHE_TTL_SECONDS,
    cache_key,
    file_version,
    page_cache,
)
from Flask_App.section_canonicalization import is_excluded_ticket_area
from Flask_App.analytics_maintenance import backfill_sport
from Flask_App.materialized_analytics import (
    refresh_event_summary_safely,
    timeline_bucket_slot,
)

# Load .env only in local development. PythonAnywhere also keeps its values in
# this file, and the storage layer reloads it before resolving database paths.
try:
    from dotenv import load_dotenv

    load_dotenv(override=True)
except Exception:
    pass


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 3 * 1024 * 1024
app.config["SECRET_KEY"] = os.environ.get(
    "FLASK_SECRET_KEY",
    "development-only-secret-key",
)

from Flask_App.nfl_blueprint import nfl_blueprint
from Flask_App.nfl_stadium_blueprint import nfl_stadium_blueprint
from Flask_App.nhl_blueprint import nhl_blueprint

app.register_blueprint(nfl_blueprint)
app.register_blueprint(nfl_stadium_blueprint)
app.register_blueprint(nhl_blueprint)

MAX_SNAPSHOT_REPLAY_AGE = timedelta(days=7)
MAX_SNAPSHOT_CLOCK_SKEW = timedelta(minutes=5)
BASEBALL_CAPTURE_WINDOW_HOURS = 72
CONCERT_CAPTURE_WINDOW_HOURS = 7 * 24
CONCERT_URL_PATTERN = re.compile(
    r"--concerts-[a-z0-9-]+/production/\d+$",
    flags=re.IGNORECASE,
)


def authorized_collector_request():
    configured = os.environ.get("COLLECTOR_INGEST_TOKEN", "")
    supplied = request.headers.get("Authorization", "")
    if not configured:
        return False
    return hmac.compare_digest(supplied, f"Bearer {configured}")


@app.before_request
def pause_ticket_ingestion_during_database_migration():
    protected_paths = {
        "/api/collector/snapshot",
        "/api/nfl/snapshot",
        "/api/nhl/snapshot",
    }
    if (
        request.method == "POST"
        and request.path in protected_paths
        and migration_pause_active()
    ):
        return (
            jsonify(
                {
                    "status": "maintenance",
                    "message": "Ticket collection is paused during a database migration.",
                }
            ),
            503,
        )
    return None


@app.get("/api/database/status")
def database_backend_status():
    if not authorized_collector_request():
        return jsonify({"status": "unauthorized"}), 401

    from Flask_App.nfl_blueprint import CreateNFLModel
    from Flask_App.nhl_blueprint import CreateNHLModel

    models = {
        "mlb": CreateModel(),
        "nfl": CreateNFLModel(),
        "nhl": CreateNHLModel(),
    }
    databases = {}
    try:
        for sport, model in models.items():
            with model.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            databases[sport] = {
                "dialect": model.engine.dialect.name,
                "connected": True,
            }
    finally:
        for model in models.values():
            dispose_ticket_engine(model.engine)

    return jsonify(
        {
            "status": "ok",
            "backend": configured_backend(),
            "migration_paused": migration_pause_active(),
            "databases": databases,
        }
    )


@app.get("/api/analytics/quality")
def analytics_quality():
    """Authenticated, read-only audit of the same evidence used by report cards."""
    if not authorized_collector_request():
        return jsonify({"status": "unauthorized"}), 401
    from Flask_App.report_quality import quality_report
    try:
        result = quality_report(
            request.args.get("sport", ""), request.args.get("team", ""),
            request.args.get("venue", ""),
        )
    except ValueError as exc:
        return jsonify({"status": "error", "error": str(exc)}), 400
    return jsonify(result)


@app.post("/api/analytics/backfill")
def analytics_backfill():
    """Refresh a bounded batch of materialized event summaries."""
    if not authorized_collector_request():
        return jsonify({"status": "error", "error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "error": "invalid JSON body"}), 400

    sport = str(payload.get("sport") or request.args.get("sport") or "").casefold()
    try:
        limit = int(payload.get("limit") or request.args.get("limit") or 3)
        result = backfill_sport(sport, limit=limit)
    except ValueError as exc:
        return jsonify({"status": "error", "error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"status": "busy", "error": str(exc)}), 409
    except Exception as exc:
        # This endpoint is collector-authenticated and exists specifically so
        # deployment maintenance failures are visible and actionable.
        return jsonify(
            {
                "status": "error",
                "exception": type(exc).__name__,
                "error": str(exc),
            }
        ), 500

    return jsonify({"status": "ok", **result.as_dict()})


def concert_snapshot_from_payload(payload):
    """Apply concert-specific validation without importing collector modules
    that PythonAnywhere's restricted deploy command does not synchronize.
    """
    event_type = payload.get("event_type")
    if event_type not in {None, "concert"}:
        raise ValueError("Concert endpoint only accepts concert snapshots.")

    url, event_date, captured_at, snapshot = snapshot_from_payload(payload)
    if CONCERT_URL_PATTERN.search(url) is None:
        raise ValueError("Concert endpoint only accepts Vivid concert URLs.")

    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValueError("Concert snapshot is missing its title.")
    return url, event_date, captured_at, replace(snapshot, title=title)


def _refresh_mlb_materialized_summary(
    *,
    event_id: int,
    event_date: datetime,
    captured_at: datetime,
    venue: str,
) -> str:
    """Update only the newly affected MLB time window after raw commit."""

    slot = timeline_bucket_slot("mlb", event_date, captured_at)
    if slot is None:
        return "outside-analysis-window"

    model = None
    try:
        model = CreateModel()
        result = refresh_event_summary_safely(
            model.getSession(),
            sport_key="mlb",
            event_id=event_id,
            event_date=event_date,
            venue=venue,
            iteration_model=Iteration,
            ticket_model=Ticket,
            bucket_slots=(slot,),
        )
        return "updated" if result is not None else "deferred"
    except Exception:
        app.logger.exception(
            "Deferred MLB materialized analytics refresh for event %s", event_id
        )
        return "deferred"
    finally:
        if model is not None:
            dispose_ticket_engine(model.engine)


@app.post("/api/collector/snapshot")
def ingest_collector_snapshot():
    """Validate and store one baseball snapshot in Event-collection.db."""
    if not authorized_collector_request():
        return jsonify({"status": "error", "error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "error": "invalid JSON body"}), 400

    try:
        url, event_date, captured_at, snapshot = snapshot_from_payload(payload)
        if event_date_from_url(url) is None:
            raise ValueError("Baseball endpoint only accepts Vivid MLB event URLs.")

        now = datetime.now(timezone.utc)
        captured_at_utc = captured_at.astimezone(timezone.utc)
        event_date_utc = event_date.astimezone(timezone.utc)
        if captured_at_utc > now + MAX_SNAPSHOT_CLOCK_SKEW:
            raise ValueError("Snapshot capture time is in the future.")
        if now - captured_at_utc > MAX_SNAPSHOT_REPLAY_AGE:
            raise ValueError("Snapshot is older than the seven-day replay window.")
        if event_date_utc <= captured_at_utc:
            raise ValueError("The event had already started at the capture time.")
        if event_date_utc - captured_at_utc > timedelta(
            hours=BASEBALL_CAPTURE_WINDOW_HOURS
        ):
            raise ValueError("The baseball game is outside the 72-hour capture window.")

        SessionLocal = CreateModel().getSession()
        captured_at_storage = captured_datetime_for_storage(captured_at)
        with SessionLocal() as session:
            event = session.query(Event).filter(Event.URL == url).first()
            duplicate = (
                event is not None
                and session.query(Iteration)
                .filter(
                    Iteration.event_id == event.id,
                    Iteration.captured_at >= captured_at_storage,
                    Iteration.captured_at
                    < captured_at_storage + timedelta(seconds=1),
                )
                .first()
                is not None
            )
            if duplicate:
                return jsonify(
                    {
                        "status": "duplicate",
                        "event_id": event.id,
                        "captured_at": captured_at.isoformat(),
                    }
                )

        if configured_backend() == "sqlite":
            create_daily_backup(now=now)
        event_id, iteration_id = store_snapshot(
            url,
            event_date,
            snapshot,
            captured_at=captured_at,
        )
        write_capture_audit(
            url,
            event_date,
            snapshot,
            event_id,
            iteration_id,
            captured_at=captured_at,
        )
        analytics_status = _refresh_mlb_materialized_summary(
            event_id=event_id,
            event_date=event_date,
            captured_at=captured_at,
            venue=snapshot.venue,
        )
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"status": "error", "error": str(exc)}), 400

    return jsonify(
        {
            "status": "stored",
            "event_type": "baseball",
            "event_id": event_id,
            "iteration_id": iteration_id,
            "sections": len(snapshot.sections),
            "captured_at": captured_at.isoformat(),
            "analytics": analytics_status,
        }
    ), 201


@app.post("/api/concerts/snapshot")
def ingest_concert_snapshot():
    """Validate and store one hourly snapshot in Concert-collection.db."""
    if not authorized_collector_request():
        return jsonify({"status": "error", "error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "error": "invalid JSON body"}), 400

    try:
        url, event_date, captured_at, snapshot = concert_snapshot_from_payload(payload)
        now = datetime.now(timezone.utc)
        captured_at_utc = captured_at.astimezone(timezone.utc)
        event_date_utc = event_date.astimezone(timezone.utc)

        if captured_at_utc > now + MAX_SNAPSHOT_CLOCK_SKEW:
            raise ValueError("Snapshot capture time is in the future.")
        if now - captured_at_utc > MAX_SNAPSHOT_REPLAY_AGE:
            raise ValueError("Snapshot is older than the seven-day replay window.")
        if event_date_utc <= captured_at_utc:
            raise ValueError("The concert had already started at the capture time.")
        if event_date_utc - captured_at_utc > timedelta(
            hours=CONCERT_CAPTURE_WINDOW_HOURS
        ):
            raise ValueError("The concert is outside the seven-day capture window.")

        create_concert_daily_backup(now=now)
        event_id, iteration_id, stored = store_concert_snapshot(
            url,
            event_date,
            snapshot,
            captured_at,
        )
        if stored:
            write_concert_audit(
                url,
                event_date,
                snapshot,
                event_id,
                iteration_id,
                captured_at,
            )
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"status": "error", "error": str(exc)}), 400

    status = "stored" if stored else "duplicate"
    return jsonify(
        {
            "status": status,
            "event_type": "concert",
            "event_id": event_id,
            "iteration_id": iteration_id,
            "sections": len(snapshot.sections),
            "captured_at": captured_at.isoformat(),
        }
    ), 201 if stored else 200


def format_event_title(event):
    event_date = event_datetime_eastern(event.event_date)
    hour = event_date.hour % 12 or 12
    date_label = f"{event_date:%b} {event_date.day}, {event_date.year}"
    time_label = f"{hour}:{event_date.minute:02d} {event_date:%p}"
    return f"{clean_event_title(event.title)} — {date_label} · {time_label}"


def is_baseball_event(event):
    return event_date_from_url(str(event.URL or "")) is not None


def find_event(place, identifier):
    if not identifier:
        return None
    SessionLocal = CreateModel().getSession()
    with SessionLocal() as session:
        query = session.query(Event).filter(Event.Place == place)
        if str(identifier).isdigit():
            event = query.filter(Event.id == int(identifier)).first()
        else:
            event = (
                query.filter(Event.title == identifier)
                .order_by(Event.event_date)
                .first()
            )
        return (
            event
            if event
            and is_baseball_event(event)
            and event_has_complete_public_data(event)
            else None
        )


def _public_option_sections(values):
    return sorted(
        {
            " ".join(str(value or "").split())
            for value in values or []
            if " ".join(str(value or "").split())
            and not is_excluded_ticket_area(value)
        },
        key=str.casefold,
    )


def _baseball_home_context():
    """Build the lightweight landing page without loading section inventories."""

    model = CreateModel()
    try:
        with model.getSession()() as session:
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
                and event.Place
            ]

            games_dict = {}
            for event in data:
                games_dict.setdefault(event.Place, []).append(
                    {"value": str(event.id), "label": format_event_title(event)}
                )
            games_dict = dict(sorted(games_dict.items()))
            from Flask_App.nfl_stadium_blueprint import _generic_venue_index, mlb_event_home_team
            team_reports = _generic_venue_index(data, datetime.now(timezone.utc),
                venue_getter=lambda e: e.Place, team_getter=mlb_event_home_team,
                endpoint="nfl_stadium.mlb_stadium")
    finally:
        dispose_ticket_engine(model.engine)

    return {
        "team_reports": team_reports,
        "event_dict": {place: [] for place in games_dict},
        "games_dict": games_dict,
        "game_sections_dict": {},
        "venue_count": len(games_dict),
        "event_count": len(data),
        "section_count": 0,
    }


def _cached_baseball_home_context():
    version = file_version(database_path())
    return page_cache.get_or_create(
        cache_key("home", "mlb", version),
        _baseball_home_context,
        ttl_seconds=PAGE_CACHE_TTL_SECONDS,
        tags=("mlb",),
    )


def _baseball_options_context(place: str):
    selected = " ".join(str(place or "").split())
    if not selected:
        return {"games": [], "multi_sections": [], "sections_by_game": {}}

    model = CreateModel()
    try:
        with model.getSession()() as session:
            events = (
                session.query(Event)
                .filter(Event.Place == selected)
                .order_by(Event.event_date)
                .all()
            )
            events = [
                event
                for event in events
                if is_baseball_event(event) and event_has_complete_public_data(event)
            ]
    finally:
        dispose_ticket_engine(model.engine)

    games = []
    sections_by_game = {}
    games_by_section = {}
    for event in events:
        sections = _public_option_sections(event.event_sections)
        games.append({"value": str(event.id), "label": format_event_title(event)})
        sections_by_game[str(event.id)] = sections
        for section in sections:
            games_by_section.setdefault(section, set()).add(event.id)

    multi_sections = sorted(
        (section for section, game_ids in games_by_section.items() if len(game_ids) > 1),
        key=str.casefold,
    )
    return {
        "games": games,
        "multi_sections": multi_sections,
        "sections_by_game": sections_by_game,
    }


def _cached_baseball_options(place: str):
    version = file_version(database_path())
    return page_cache.get_or_create(
        cache_key("options", "mlb", version, place),
        lambda: _baseball_options_context(place),
        ttl_seconds=OPTIONS_CACHE_TTL_SECONDS,
        tags=("mlb",),
    )


@app.route("/baseball", methods=["GET", "POST"])
@app.route("/", methods=["GET", "POST"])
def home():
    return render_template("HomeScreen.html", **_cached_baseball_home_context())


@app.get("/api/baseball/options")
def baseball_options():
    return jsonify(_cached_baseball_options(request.args.get("venue", "")))



@app.route("/graph", methods=["GET", "POST"])
def graph():
    place = request.args.get("event")
    section = request.args.get("section")
    display_mode = normalize_display_mode(
        request.args.get("display") or request.args.get("id")
    )
    mode = request.args.get("mode", "multi")
    total_games = request.args.get("total_games", 0)

    builder = GraphBuilder()

    if mode == "single":
        selected_event = find_event(place, request.args.get("game"))
        game = (
            str(selected_event.id)
            if selected_event
            else request.args.get("game", "")
        )
        game_label = (
            format_event_title(selected_event)
            if selected_event
            else "Unknown game"
        )
        y, x = (
            builder.singleGameGraph(
                place,
                selected_event.id,
                section,
                display_mode,
            )
            if selected_event
            else ([], [])
        )
        total_games = 1 if y else 0
    else:
        y, x, total_games = builder.allEventsForStadium(
            place,
            section,
            48,
            display_mode,
        )
        game = ""
        game_label = ""

    if not x or not y:
        return render_template(
            "graph.html",
            error="No ticket data is available for that selection.",
            place=place,
            section=section,
            mode=mode,
            game=game,
            gameLabel=game_label,
            displayType=toggle_display_mode(display_mode),
            displayLabel=toggle_display_label(display_mode),
            totalGames=total_games,
        )

    return render_template(
        "graph.html",
        img=builder.create_plot(x, y, display_mode),
        chartX=x,
        chartY=y,
        displayMode=display_mode,
        place=place,
        section=section,
        mode=mode,
        game=game,
        gameLabel=game_label,
        displayType=toggle_display_mode(display_mode),
        displayLabel=toggle_display_label(display_mode),
        totalGames=total_games,
    )


@app.route("/predict", methods=["GET", "POST"])
def predict():
    place = request.args.get("event")
    section = request.args.get("section")

    builder = GraphBuilder()
    y, x, total = builder.allEventsForStadium(
        place,
        section,
        48,
        "percentage",
    )
    if not y or not x:
        return render_template(
            "lowestPrice.html",
            error="No ticket data is available for that selection.",
        )

    minimum_index = y.index(min(y))
    return render_template(
        "lowestPrice.html",
        time=x[minimum_index],
        place=place,
        section=section,
        totalGames=total,
    )


def format_concert_title(event):
    event_date = event_datetime_eastern(event.event_date)
    hour = event_date.hour % 12 or 12
    return (
        f"{event.title} — {event_date:%b} {event_date.day}, {event_date.year} "
        f"· {hour}:{event_date.minute:02d} {event_date:%p}"
    )


def find_concert(venue, identifier):
    if not identifier or not str(identifier).isdigit():
        return None
    model = CreateConcertModel()
    with model.getSession()() as session:
        return (
            session.query(ConcertEvent)
            .filter(
                ConcertEvent.venue == venue,
                ConcertEvent.id == int(identifier),
            )
            .first()
        )


@app.route("/concerts", methods=["GET"])
def concerts_home():
    model = CreateConcertModel()
    with model.getSession()() as session:
        concerts = (
            session.query(ConcertEvent)
            .order_by(ConcertEvent.event_date)
            .all()
        )
        concerts_dict = {}
        concert_sections_dict = {}
        for concert in concerts:
            concerts_dict.setdefault(concert.venue, []).append(
                {
                    "value": str(concert.id),
                    "label": format_concert_title(concert),
                }
            )
            concert_sections_dict.setdefault(concert.venue, {})[
                str(concert.id)
            ] = sorted(set(concert.sections or []))

        venue_count = len(concerts_dict)
        concert_count = len(concerts)
        section_count = len(
            {
                section
                for by_concert in concert_sections_dict.values()
                for sections in by_concert.values()
                for section in sections
            }
        )

    return render_template(
        "ConcertHomeScreen.html",
        concerts_dict=concerts_dict,
        concert_sections_dict=concert_sections_dict,
        venue_count=venue_count,
        concert_count=concert_count,
        section_count=section_count,
    )


@app.route("/concerts/graph", methods=["GET"])
def concerts_graph():
    venue = request.args.get("event")
    concert_id = request.args.get("concert")
    section = request.args.get("section")
    display_mode = normalize_display_mode(request.args.get("display"))
    selected = find_concert(venue, concert_id)
    label = format_concert_title(selected) if selected else "Unknown concert"

    builder = ConcertGraphBuilder()
    y, x = (
        builder.single_concert_graph(
            venue,
            selected.id,
            section,
            display_mode,
        )
        if selected
        else ([], [])
    )
    if not x or not y:
        return render_template(
            "concert_graph.html",
            error="No concert price data is available for that selection.",
            venue=venue,
            section=section,
            concert=concert_id or "",
            concertLabel=label,
            displayType=toggle_display_mode(display_mode),
            displayLabel=toggle_display_label(display_mode),
        )

    return render_template(
        "concert_graph.html",
        img=builder.create_plot(x, y, display_mode),
        chartX=x,
        chartY=y,
        displayMode=display_mode,
        venue=venue,
        section=section,
        concert=str(selected.id),
        concertLabel=label,
        displayType=toggle_display_mode(display_mode),
        displayLabel=toggle_display_label(display_mode),
    )


def normalize_display_mode(raw_mode):
    if raw_mode in {"percentage", "%"}:
        return "percentage"
    return "money"


def toggle_display_mode(display_mode):
    return "percentage" if display_mode == "money" else "money"


def toggle_display_label(display_mode):
    return "%" if display_mode == "money" else "$"


def sortSection(session, eventID) -> list:
    event = session.execute(
        select(Event).where(Event.id == eventID)
    ).scalar_one_or_none()
    sorted_sections = event.event_sections
    sorted_sections.sort()
    return sorted_sections


if __name__ == "__main__":
    app.run(debug=True)
