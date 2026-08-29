# TicketSignal — Ticket Price Predictor

Full-stack Flask project for collecting ticket listings, storing historical price snapshots, and turning price history into market trends and buying-window signals.

Live app: https://bunnyjeff.pythonanywhere.com/

## Active tracking

- MLB games: every 30 minutes during the final 72 hours.
- NFL games: once per hour during the final 168 hours.
- Each category has its own collector job, API route, pending queue, audit records, backups, SQLAlchemy tables, and SQLite database.
- NFL analysis begins game by game and is organized by stadium, game, and section. Stadium-level aggregation can be added once enough comparable section history exists.
- Existing concert data and pages are preserved as an archive, but the automated concert collector is paused while NFL data is prioritized.

## Tech stack

- Python
- Flask
- SQLAlchemy
- SQLite
- Matplotlib
- NumPy / scikit-learn
- Selenium
- HTML / JavaScript

## Storage separation

- `Event-collection.db`: MLB events, 30-minute iterations, and section observations.
- `NFL-collection.db`: NFL games, hourly iterations, and section observations.
- `Concert-collection.db`: preserved concert history from the earlier experiment.

Active endpoints and pages:

- `/api/collector/snapshot`: authenticated MLB ingestion with a 72-hour window.
- `/api/nfl/snapshot`: authenticated NFL ingestion with a 168-hour window.
- `/`: baseball analysis.
- `/nfl`: NFL analysis.

Archived concert routes remain available at `/concerts` and `/api/concerts/snapshot`, but GitHub Actions no longer performs new concert collection.

Database files, scraped JSON, browser profiles, runtime audit records, and credentials are excluded from the repository.

## Project structure

- `Flask_App/flask_app.py`: baseball application and preserved concert routes.
- `Flask_App/nfl_blueprint.py`: NFL-only schema, storage helpers, API, graphs, and pages.
- `models.py`: baseball models plus preserved concert storage compatibility.
- `graph_builder.py`: baseball and archived concert chart helpers.
- `collector.py`: guarded MLB collection, delivery queue, health reporting, and backups.
- `nfl_collector.py`: league-wide NFL discovery and seven-day hourly collection.
- `concert_collector.py`: preserved concert collector code; no longer scheduled.
- `Prediction.py`: baseball forecasting experiments.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Provide an `Event-collection.db` matching `models.py`, or set `DATABASE_PATH`. NFL storage initializes automatically at `NFL-collection.db`, or at `NFL_DATABASE_PATH` when configured. `CONCERT_DATABASE_PATH` can point to the preserved concert archive.

```bash
flask --app Flask_App.flask_app run
```

## Collection workflows

PythonAnywhere dispatches GitHub Actions on its reliable 30-minute cadence.

- Baseball runs on every dispatch.
- NFL runs on the first dispatch of each UTC hour.
- NFL discovery starts from Vivid Seats' league page, filters out parking, packages, season tickets, and other non-game events, then captures only games whose calendar date is within seven days.
- League discovery scrolls through lazy-loaded results and does not settle until it has found at least one current or future NFL game.
- Canonical dates encoded in Vivid event URLs take precedence over surrounding feed text, and automatic smoke captures reject events that have already started.
- The exact kickoff time is checked after capture, so a game is stored only when it is within 168 hours.
- Baseball and NFL use separate concurrency groups and pending queues.

The NFL smoke test discovers and captures one current game without writing to a production database.
