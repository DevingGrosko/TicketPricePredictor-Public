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
- `nfl_collector.py`: Vivid NFL parsing, event capture, and feed-only fallback.
- `nfl_schedule_collector.py`: complete-slate schedule seeding, Vivid resolution, coverage checks, and hourly collection.
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
- The NFL collector first loads the complete scheduled slate inside the exact seven-day window.
- Each scheduled matchup is matched to Vivid's NFL feed. A missing feed link is recovered through a targeted Vivid search for the two teams.
- Feed and search results are candidate links only. The title and kickoff parsed from the individual Vivid event page must match the scheduled matchup before anything is stored.
- The health report records the number scheduled, matched from the feed, recovered through search, unresolved, captured, queued, and uploaded, plus an explicit coverage percentage.
- If the structured schedule source is unavailable, the prior Vivid-only collector still runs so data collection continues, but the workflow is marked degraded instead of silently claiming complete coverage.
- The actual kickoff parsed from each event page remains authoritative for enforcing the final 168-hour storage window.
- Baseball and NFL use separate concurrency groups and pending queues.

The NFL smoke test resolves a scheduled matchup and captures one current game without writing to a production database.
