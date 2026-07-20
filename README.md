# TicketSignal — Ticket Price Predictor

Full-stack Flask project for collecting ticket-listing data, storing historical price snapshots, and turning ticket-price history into approachable market trends and buying-window signals.

Live app: https://bunnyjeff.pythonanywhere.com/

## Features

- Responsive sports-analytics interface for exploring trends by venue, event, and section.
- Multi-game market trends, single-game history, and historical buying-window analysis.
- SQLite/SQLAlchemy schema for events, scrape iterations, and ticket snapshots.
- Matplotlib graph generation for price history and normalized percentage movement.
- Data ingestion workflow for importing ticket-listing snapshots into SQLite.
- Scikit-learn modeling utilities for polynomial/Ridge regression price forecasting experiments.

## Tech Stack

- Python
- Flask
- SQLAlchemy
- SQLite
- Matplotlib
- NumPy / scikit-learn
- Selenium
- HTML / JavaScript / Bootstrap

## Project Structure

- `Flask_App/`: Flask routes, templates, static JavaScript, and graph pages.
- `models.py`: SQLAlchemy models for events, scrape iterations, and tickets.
- `graph_builder.py`: chart-building and time-series aggregation logic.
- `collector.py`: guarded Vivid snapshot collection, audit logging, health reporting, and backups.
- `Prediction.py`: model-training utilities for price forecasting experiments.
- `WebBrowsing.py`: lightweight browser helper.
- `DataBaseSQL.py` and `SortTickets.py`: parsing and database-ingestion helpers.

## Data Policy

The production SQLite database, scraped JSON listing files, local browser profiles, runtime audit records, and API credentials are intentionally excluded from this public repository.

The live deployed app uses a private database on PythonAnywhere, while this repo shows the application code and architecture.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

To run locally, provide an `Event-collection.db` SQLite database matching the schema in `models.py`. It can live in the project root, or you can keep it private elsewhere and set `DATABASE_PATH` to its absolute path. Then start the Flask app:

```bash
flask --app Flask_App.flask_app run
```

## Safe collector operation

The PythonAnywhere always-on command should run the capture service without automatic discovery:

```bash
cd /home/bunnyjeff/TicketPricePredictor && /home/bunnyjeff/venv/bin/python -u collector.py watch --check-every 900 --timeout 25
```

Automatic venue discovery is deliberately separate because it launches additional browser work. Run `collector.py discover --headless` only when schedules need refreshing.

The collector protects the account by:

- using up to 97% of the daily CPU allowance while preserving at least 150 CPU seconds of headroom;
- processing every due event during each 15-minute cycle when captures are healthy;
- capturing each game every 30 minutes during its final 72 hours and never earlier;
- stopping the cycle after two capture failures so a broken browser cannot exhaust the CPU allowance;
- opening a six-hour circuit breaker immediately when Chrome fails to start or ChromeDriver hangs;
- opening the same circuit after two fully failed cycles;
- retiring links whose URL date is already in the past;
- keeping daily database backups and per-price audit records.

New-game collection is currently disabled for Citi Field, Truist Park, and George M. Steinbrenner Field. Their historical database records remain available to the website; exclusion only removes their links from the future collection queue.

Use `python collector.py health` to inspect the current state. A `paused` status is a deliberate safety stop, not an always-on task crash.
