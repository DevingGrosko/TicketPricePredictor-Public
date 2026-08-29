from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hmac
import os
import re

from flask import Flask, jsonify, render_template, request
from sqlalchemy import select

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
    Event,
    Iteration,
    captured_datetime_for_storage,
    clean_event_title,
    create_concert_daily_backup,
    event_datetime_eastern,
    event_has_complete_public_data,
    store_concert_snapshot,
    write_concert_audit,
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

from Flask_App.nfl_blueprint import nfl_blueprint, nfl_home

app.register_blueprint(nfl_blueprint)


@app.get("/")
def landing():
    """Serve the NFL tracker as the site's default homepage."""
    return nfl_home()

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


@app.route("/baseball", methods=["GET", "POST"])
def home():
    SessionLocal = CreateModel().getSession()
    with SessionLocal() as session:
        data = [
            event
            for event in session.query(Event).order_by(Event.event_date).all()
            if is_baseball_event(event) and event_has_complete_public_data(event)
        ]
        section_games = {}
        game_sections_dict = {}
        for event in data:
            if not event.Place:
                continue
            sections = sorted(set(event.event_sections or []))
            game_sections_dict.setdefault(event.Place, {})[str(event.id)] = sections
            for section in sections:
                section_games.setdefault((event.Place, section), set()).add(event.id)

        event_dict = {}
        for (place, section), game_ids in section_games.items():
            if len(game_ids) > 1:
                event_dict.setdefault(place, []).append(section)
        event_dict = {
            place: sorted(sections)
            for place, sections in event_dict.items()
        }

        games_dict = {}
        for event in data:
            if not event.Place:
                continue
            games_dict.setdefault(event.Place, []).append(
                {"value": str(event.id), "label": format_event_title(event)}
            )

        venue_count = len(event_dict)
        event_count = len(data)
        section_count = len(
            {section for sections in event_dict.values() for section in sections}
        )

    return render_template(
        "HomeScreen.html",
        event_dict=event_dict,
        games_dict=games_dict,
        game_sections_dict=game_sections_dict,
        venue_count=venue_count,
        event_count=event_count,
        section_count=section_count,
    )


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
