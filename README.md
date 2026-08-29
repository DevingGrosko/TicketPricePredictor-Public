# TicketSignal — Ticket Price Predictor

Full-stack Flask project for collecting ticket listings, storing historical price snapshots, and turning price history into market trends and buying-window signals.

Live app: https://bunnyjeff.pythonanywhere.com/

## Active tracking

- MLB games: every 30 minutes during the final 72 hours.
- NFL games: every 6 hours from days 30–15, every 3 hours from days 14–8, and hourly during the final 7 days.
- Each category has its own collector job, API route, pending queue, audit records, backups, SQLAlchemy tables, and SQLite database.
- NFL analysis is organized by designated home team, game, and section. Teams that share a venue, such as the Giants/Jets or Rams/Chargers, remain separate in the website.
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
- `NFL-collection.db`: NFL games, adaptive-cadence iterations, and section observations.
- `Concert-collection.db`: preserved concert history from the earlier experiment.

Active endpoints and pages:

- `/api/collector/snapshot`: authenticated MLB ingestion with a 72-hour window.
- `/api/nfl/snapshot`: authenticated NFL ingestion with a 720-hour window.
- `/`: NFL analysis and the default public homepage.
- `/nfl`: direct NFL analysis route.
- `/baseball`: baseball analysis.

Archived concert routes remain available at `/concerts` and `/api/concerts/snapshot`, but GitHub Actions no longer performs new concert collection.

Database files, scraped JSON, browser profiles, runtime audit records, and credentials are excluded from the repository.

## Project structure

- `Flask_App/flask_app.py`: baseball application and preserved concert routes.
- `Flask_App/nfl_blueprint.py`: NFL-only schema, storage helpers, API, graphs, and pages.
- `models.py`: baseball models plus preserved concert storage compatibility.
- `graph_builder.py`: baseball and archived concert chart helpers.
- `collector.py`: guarded MLB collection, delivery queue, health reporting, and backups.
- `nfl_collector.py`: Vivid NFL parsing, event capture, and feed-only fallback.
- `nfl_schedule_collector.py`: 30-day schedule seeding, Vivid resolution, staggered adaptive cadence, and coverage checks.
- `manual_provider_capture.py`: human-in-the-loop Ticketmaster and SeatGeek response diagnostics.
- `hosted_provider_probe.py`: standard GitHub-hosted Chrome probe with block classification and sanitized response capture.
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
- NFL evaluates its schedule on the first dispatch of each UTC hour.
- The NFL collector first loads the complete scheduled slate inside the exact 30-day window.
- Each game receives a deterministic phase so 6-hour and 3-hour captures are spread across hourly runs instead of arriving in one large batch.
- Games 15–30 days out run every 6 hours, games 8–14 days out run every 3 hours, and games in the final 7 days run every hour.
- Each scheduled matchup is matched to Vivid's NFL feed. A missing feed link is recovered through a targeted Vivid search for the two teams.
- Feed and search results are candidate links only. The title and kickoff parsed from the individual Vivid event page must match the scheduled matchup before anything is stored.
- The health report records the number scheduled, matched from the feed, recovered through search, unresolved, captured, queued, and uploaded, plus an explicit coverage percentage.
- If the structured schedule source is unavailable, the prior Vivid-only collector still runs so data collection continues, but the workflow is marked degraded instead of silently claiming complete coverage.
- The actual kickoff parsed from each event page remains authoritative for enforcing the final 720-hour storage window.
- Baseball and NFL use separate concurrency groups and pending queues.

The NFL smoke test resolves a scheduled matchup and captures one current game without writing to a production database.

## GitHub-hosted Ticketmaster and SeatGeek probe

The **Hosted Ticketmaster or SeatGeek probe** workflow runs a standard headless Chrome session on a GitHub-hosted runner. It is separate from the Vivid production collector and does not write to any production database.

To run it:

1. Open the repository's **Actions** tab.
2. Select **Hosted Ticketmaster or SeatGeek probe**.
3. Choose **Run workflow**.
4. Paste a full provider event URL and select the provider.
5. Download the generated `provider-probe-*` artifact.

The artifact contains:

- `provider_probe_report.json`: sanitized response summaries, candidate section-price records, HTTP statuses, safe inventory-view clicks, and the final outcome.
- `provider_probe.png`: the final browser screenshot.

Possible outcomes are:

- `section_inventory_found`
- `provider_json_found_no_section_records`
- `page_loaded_no_inventory_json`
- `page_request_failed`
- `blocked`

The workflow uses ordinary Chrome navigation, limited scrolling, and non-purchasing inventory-view interactions. It does not hide WebDriver, change the User-Agent or browser fingerprint, rotate proxies, import session cookies, solve CAPTCHAs, or replay challenge tokens. A successful section-level response can be used to build a provider-specific parser; a blocked result is recorded rather than bypassed.

## Manual Ticketmaster and SeatGeek diagnostics

`manual_provider_capture.py` is an exploratory, human-in-the-loop tool for determining whether a provider page exposes section-level inventory to a browser session that the user can access normally. It does not alter browser fingerprints, hide WebDriver, rotate proxies, solve challenges, replay anti-bot tokens, or send data to the production database.

The preferred mode is a HAR exported from an ordinary Chrome session:

1. Open the event in normal Chrome.
2. Open **Developer Tools → Network** and enable **Preserve log**.
3. Reload the event, open the interactive seat map, and scroll through the available inventory.
4. Right-click the Network request list and select **Save all as HAR with content**.
5. Run:

```bash
python manual_provider_capture.py har ~/Downloads/event.har \
  --event-url "https://www.ticketmaster.com/..." \
  --include-sanitized-payloads
```

A visible Selenium diagnostic is also available when the provider page loads normally in an unmodified automated browser:

```bash
python manual_provider_capture.py browser \
  "https://seatgeek.com/..." \
  --include-sanitized-payloads
```

The output is written under `manual_provider_captures/`. It removes common authentication, session, customer, payment, and contact fields; it does not export cookies or request headers. Section/row/price records are heuristic candidates until a provider-specific parser is validated against real captures. Nothing from this tool is inserted into `NFL-collection.db` automatically.
