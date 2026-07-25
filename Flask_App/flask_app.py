from datetime import datetime, timedelta, timezone
import hmac
import os

from flask import Flask, jsonify, render_template, request
from sqlalchemy import select
from collector import (
    create_daily_backup,
    snapshot_from_payload,
    store_snapshot,
    write_capture_audit,
)
from graph_builder import GraphBuilder
from models import CreateModel, Event, Iteration, clean_event_title, event_has_complete_public_data

# Load .env ONLY in local dev (PythonAnywhere won’t need it)
try:
    from dotenv import load_dotenv
    load_dotenv()  # no-op if python-dotenv not installed
except Exception:
    pass

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 3 * 1024 * 1024

# Use an environment value in production; the fallback is only for local demos.
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'development-only-secret-key')


def authorized_collector_request():
    configured = os.environ.get("COLLECTOR_INGEST_TOKEN", "")
    supplied = request.headers.get("Authorization", "")
    if not configured:
        return False
    expected = f"Bearer {configured}"
    return hmac.compare_digest(supplied, expected)


@app.post("/api/collector/snapshot")
def ingest_collector_snapshot():
    """Validate and atomically store one snapshot from the GitHub collector."""
    if not authorized_collector_request():
        return jsonify({"status": "error", "error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "error": "invalid JSON body"}), 400

    try:
        url, event_date, captured_at, snapshot = snapshot_from_payload(payload)
        now = datetime.now(timezone.utc)
        if abs((captured_at.astimezone(timezone.utc) - now).total_seconds()) > 2 * 3600:
            raise ValueError("Snapshot capture time is outside the accepted two-hour clock window.")
        if event_date.astimezone(timezone.utc) <= captured_at.astimezone(timezone.utc):
            raise ValueError("The event had already started at the capture time.")
        if event_date.astimezone(timezone.utc) - captured_at.astimezone(timezone.utc) > timedelta(
            hours=72
        ):
            raise ValueError("The event is outside the 72-hour capture window.")

        SessionLocal = CreateModel().getSession()
        with SessionLocal() as session:
            event = session.query(Event).filter(Event.URL == url).first()
            duplicate = (
                event is not None
                and session.query(Iteration)
                .filter(
                    Iteration.event_id == event.id,
                    Iteration.captured_at >= captured_at,
                    Iteration.captured_at < captured_at + timedelta(seconds=1),
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
            "event_id": event_id,
            "iteration_id": iteration_id,
            "sections": len(snapshot.sections),
            "captured_at": captured_at.isoformat(),
        }
    ), 201


def format_event_title(event):
    event_date = event.event_date
    hour = event_date.hour % 12 or 12
    date_label = f"{event_date:%b} {event_date.day}, {event_date.year}"
    time_label = f"{hour}:{event_date.minute:02d} {event_date:%p}"
    return f"{clean_event_title(event.title)} — {date_label} · {time_label}"


def find_event(place, identifier):
    if not identifier:
        return None
    SessionLocal = CreateModel().getSession()
    with SessionLocal() as session:
        query = session.query(Event).filter(Event.Place == place)
        if str(identifier).isdigit():
            event = query.filter(Event.id == int(identifier)).first()
        else:
            event = query.filter(Event.title == identifier).order_by(Event.event_date).first()
        return event if event and event_has_complete_public_data(event) else None


@app.route("/", methods=['GET', 'POST'])
def home():
    SessionLocal = CreateModel().getSession()
    with SessionLocal() as session:
        data = [
            event
            for event in session.query(Event).order_by(Event.event_date).all()
            if event_has_complete_public_data(event)
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
        section_count = len({section for sections in event_dict.values() for section in sections})

    return render_template(
        "HomeScreen.html",
        event_dict=event_dict,
        games_dict=games_dict,
        game_sections_dict=game_sections_dict,
        venue_count=venue_count,
        event_count=event_count,
        section_count=section_count,
    )


@app.route("/graph", methods=['GET', 'POST'])
def graph():
    place = request.args.get("event")
    section = request.args.get("section")
    display_mode = normalize_display_mode(request.args.get("display") or request.args.get("id"))
    mode = request.args.get("mode", "multi")  # default to multi
    totalGames = request.args.get("total_games",0)

    new_graph = GraphBuilder()

    if mode == "single":
        selected_event = find_event(place, request.args.get("game"))
        game = str(selected_event.id) if selected_event else request.args.get("game", "")
        game_label = format_event_title(selected_event) if selected_event else "Unknown game"
        y, x = new_graph.singleGameGraph(place, selected_event.id, section, display_mode) if selected_event else ([], [])
        totalGames = 1 if y else 0
    else:
        # physical time to choose, if you choose a higher time and the event doesn't start there it won't include it in
        # the graph. Also, that time is where it will standardize and start the graph at
        y, x,total = new_graph.allEventsForStadium(place, section, 48, display_mode)
        totalGames = total
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
            totalGames=totalGames,
        )

    img = new_graph.create_plot(x, y, display_mode)

    return render_template("graph.html", img=img, chartX=x, chartY=y, displayMode=display_mode,
                           place=place, section=section, mode=mode, game=game, gameLabel=game_label,
                           displayType=toggle_display_mode(display_mode),
                           displayLabel=toggle_display_label(display_mode), totalGames=totalGames)

@app.route("/predict", methods=['GET', 'POST'])
def predict():
    place = request.args.get("event")
    section = request.args.get("section")
    display_mode = "percentage"

    new_graph = GraphBuilder()
    y, x, total = new_graph.allEventsForStadium(place, section, 48, display_mode)
    if not y or not x:
        return render_template(
            "lowestPrice.html",
            error="No ticket data is available for that selection.",
        )

    y_min = min(y)
    y_index = y.index(y_min)
    x_index = x[y_index]

    return render_template(
        "lowestPrice.html",
        time=x_index,
        place=place,
        section=section,
        totalGames=total,
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
    ev = session.execute(
        select(Event).where(Event.id == eventID)
    ).scalar_one_or_none()
    sorted_sections = ev.event_sections
    sorted_sections.sort()
    return sorted_sections


if __name__ == '__main__':
    app.run(debug=True)
