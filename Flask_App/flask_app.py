from flask import Flask, render_template, request
from sqlalchemy import select
from graph_builder import GraphBuilder
from models import CreateModel, Event
import os

# Load .env ONLY in local dev (PythonAnywhere won’t need it)
try:
    from dotenv import load_dotenv
    load_dotenv()  # no-op if python-dotenv not installed
except Exception:
    pass

app = Flask(__name__)

# Use an environment value in production; the fallback is only for local demos.
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'development-only-secret-key')


@app.route("/", methods=['GET', 'POST'])
def home():
    SessionLocal = CreateModel().getSession()
    with SessionLocal() as session:
        data = session.query(Event).all()
        section_games = {}
        game_sections_dict = {}
        for event in data:
            if not event.Place:
                continue
            sections = sorted(set(event.event_sections or []))
            game_sections_dict.setdefault(event.Place, {})[event.title] = sections
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
            games_dict.setdefault(event.Place, [])
            if event.title not in games_dict[event.Place]:
                games_dict[event.Place].append(event.title)

        venue_count = len(event_dict)
        event_count = len({event.title for event in data})
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
        game = request.args.get("game")
        y, x = new_graph.singleGameGraph(place, game, section, display_mode)
        totalGames = 1 if y else 0
    else:
        # physical time to choose, if you choose a higher time and the event doesn't start there it won't include it in
        # the graph. Also, that time is where it will standardize and start the graph at
        y, x,total = new_graph.allEventsForStadium(place, section, 48, display_mode)
        totalGames = total
        game = ""

    if not x or not y:
        return render_template(
            "graph.html",
            error="No ticket data is available for that selection.",
            place=place,
            section=section,
            mode=mode,
            game=game,
            displayType=toggle_display_mode(display_mode),
            displayLabel=toggle_display_label(display_mode),
            totalGames=totalGames,
        )

    img = new_graph.create_plot(x, y, display_mode)

    return render_template("graph.html", img=img, place=place, section=section, mode=mode,
                           game=game,displayType=toggle_display_mode(display_mode),
                           displayLabel=toggle_display_label(display_mode),totalGames=totalGames)

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
