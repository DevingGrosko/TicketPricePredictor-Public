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
- `Prediction.py`: model-training utilities for price forecasting experiments.
- `WebBrowsing.py`: lightweight browser helper; production collection internals are excluded from the public repo.
- `DataBaseSQL.py` and `SortTickets.py`: parsing and database-ingestion helpers.

## Data Policy

The production SQLite database, scraped JSON listing files, local browser profiles, and source-specific collection internals are intentionally excluded from this public repository.

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
