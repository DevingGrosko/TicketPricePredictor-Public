# TicketSignal — Ticket Price Predictor

Full-stack Flask project for collecting ticket-listing data, storing historical price snapshots, and turning ticket-price history into approachable market trends and buying-window signals.

Live app: https://bunnyjeff.pythonanywhere.com/

## Features

- Separate baseball and concert experiences, collectors, API routes, SQLite databases, backups, and audit records.
- Responsive sports-analytics interface for exploring baseball trends by venue, game, and section.
- Dedicated concert interface for following one show and section across the final seven days.
- Baseball snapshots every 30 minutes during the final 72 hours.
- Concert snapshots once per hour during the final 168 hours.
- Matplotlib graph generation for price history and normalized percentage movement.
- Scikit-learn modeling utilities for baseball price-forecasting experiments.

## Tech stack

- Python
- Flask
- SQLAlchemy
- SQLite
- Matplotlib
- NumPy / scikit-learn
- Selenium
- HTML / JavaScript / Bootstrap

## Storage separation

Baseball and concerts never share tables or database files:

- `Event-collection.db` stores MLB events, iterations, and section observations.
- `Concert-collection.db` stores concert events, hourly iterations, and section observations.
- `/api/collector/snapshot` accepts only Vivid MLB URLs and enforces the 72-hour baseball window.
- `/api/concerts/snapshot` accepts only Vivid concert URLs and enforces the 168-hour concert window.
- `/` displays baseball analysis.
- `/concerts` displays concert analysis.

The database files, scraped JSON, local browser profiles, runtime audit records, and API credentials are intentionally excluded from this public repository.

## Project structure

- `Flask_App/`: Flask routes, templates, static JavaScript, and graph pages.
- `models.py`: baseball SQLAlchemy models.
- `concert_models.py`: independent concert SQLAlchemy models and storage helpers.
- `graph_builder.py`: baseball chart-building and time-series aggregation.
- `concert_graph_builder.py`: concert-only chart queries.
- `collector.py`: guarded MLB collection, audit logging, health reporting, and backups.
- `concert_collector.py`: seven-day hourly concert collection.
- `Prediction.py`: model-training utilities for baseball forecasting experiments.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

For local baseball data, provide an `Event-collection.db` matching `models.py`, or set `DATABASE_PATH` to its absolute location. Concert storage is initialized automatically at `Concert-collection.db`, or at `CONCERT_DATABASE_PATH` when configured.

```bash
flask --app Flask_App.flask_app run
```

## Collection workflows

PythonAnywhere dispatches the GitHub Actions workflow on its reliable 30-minute cadence.

- Baseball runs on every dispatch and records games in the final 72 hours.
- Concerts run only on the first dispatch of each UTC hour and record shows in the final seven days.
- The two jobs use separate concurrency groups and pending queues, so a longer concert run does not block baseball.
- The GitHub collector sends each category to its own authenticated Flask endpoint.

A live concert smoke test discovers and captures one show without writing to either production database.
