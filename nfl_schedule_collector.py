"""Schedule-backed NFL ticket collection with explicit coverage accounting.

The existing Vivid league feed is useful, but a lazy-loaded marketplace page is
not a reliable statement of the complete NFL slate. This collector first loads
a structured NFL schedule for the requested time window, then resolves every
scheduled matchup to a Vivid event. Feed matches are used first; missing games
are recovered through Vivid search. Every due game is either captured or listed
as unresolved in the health report.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Iterable
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from collector import (
    as_utc,
    post_snapshot_with_retry,
    queue_snapshot,
    replay_pending_snapshots,
    validated_vivid_url,
)
from nfl_collector import (
    DEFAULT_HEALTH_OUTPUT,
    DEFAULT_PENDING_DIR,
    DEFAULT_SMOKE_OUTPUT,
    NFL_CAPTURE_WINDOW_HOURS,
    NFL_TEAM_NAMES,
    NFLSnapshotParser,
    VividNFLBrowser,
    DiscoveredNFLGame,
    discover_nfl_games,
    extract_nfl_game_rows,
    hourly_capture_slot,
    nfl_capture_interval_hours,
    nfl_capture_is_due,
    nfl_capture_tier,
    nfl_is_within_capture_window,
    nfl_snapshot_to_payload,
    run_remote_collector as run_feed_only_collector,
    run_smoke_capture as run_feed_smoke_capture,
)


ESPN_SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
)
DEFAULT_SCHEDULE_HEALTH_OUTPUT = Path("nfl_schedule_health.json")
SCHEDULE_REQUEST_TIMEOUT_SECONDS = 20
SCHEDULE_REQUEST_ATTEMPTS = 3
VIVID_SEARCH_SETTLE_SECONDS = 2.0
VIVID_SEARCH_MAX_SECONDS = 18
EVENT_TIME_TOLERANCE_HOURS = 18
SMOKE_HORIZON_HOURS = 45 * 24


@dataclass(frozen=True)
class ScheduledNFLGame:
    schedule_id: str
    event_date: datetime
    away_team: str
    home_team: str
    venue: str
    name: str

    @property
    def matchup_key(self) -> tuple[str, str]:
        return tuple(sorted((self.away_team, self.home_team)))

    @property
    def local_date(self) -> date:
        from collector import NEW_YORK

        return self.event_date.astimezone(NEW_YORK).date()


@dataclass(frozen=True)
class ScheduleResolution:
    game: ScheduledNFLGame
    candidates: tuple[DiscoveredNFLGame, ...]
    source: str


JsonFetcher = Callable[[str, int], dict[str, Any]]


def _parse_datetime(raw: str) -> datetime:
    value = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def schedule_url(now: datetime, horizon_hours: int) -> str:
    from collector import NEW_YORK

    local_start = now.astimezone(NEW_YORK).date()
    # Include an extra calendar day because the exact UTC cutoff can cross an
    # Eastern-date boundary. parse_schedule_payload applies the precise limit.
    local_end = (now + timedelta(hours=horizon_hours, days=1)).astimezone(
        NEW_YORK
    ).date()
    dates = f"{local_start:%Y%m%d}-{local_end:%Y%m%d}"
    configured = os.environ.get("NFL_SCHEDULE_SCOREBOARD_URL", ESPN_SCOREBOARD_URL)
    separator = "&" if "?" in configured else "?"
    return f"{configured}{separator}dates={dates}&limit=1000"


def fetch_json(url: str, timeout: int = SCHEDULE_REQUEST_TIMEOUT_SECONDS) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "TicketSignal-NFL-coverage/1.0",
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, SCHEDULE_REQUEST_ATTEMPTS + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("NFL schedule response is not a JSON object.")
            return payload
        except Exception as exc:
            last_error = exc
            if attempt < SCHEDULE_REQUEST_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))
    assert last_error is not None
    raise RuntimeError(
        f"NFL schedule request failed after {SCHEDULE_REQUEST_ATTEMPTS} attempts: "
        f"{type(last_error).__name__}: {last_error}"
    ) from last_error


def parse_schedule_payload(
    payload: dict[str, Any],
    now: datetime,
    horizon_hours: int = NFL_CAPTURE_WINDOW_HOURS,
) -> list[ScheduledNFLGame]:
    now_utc = now.astimezone(timezone.utc)
    horizon = timedelta(hours=horizon_hours)
    games: dict[str, ScheduledNFLGame] = {}

    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        try:
            event_date = _parse_datetime(event["date"])
        except (KeyError, TypeError, ValueError):
            continue

        delta = event_date - now_utc
        if delta <= timedelta(0) or delta > horizon:
            continue

        competitions = event.get("competitions") or []
        competition = competitions[0] if competitions else {}
        if not isinstance(competition, dict):
            continue
        status = competition.get("status") or event.get("status") or {}
        status_type = status.get("type") if isinstance(status, dict) else {}
        if isinstance(status_type, dict) and status_type.get("completed") is True:
            continue

        by_side: dict[str, str] = {}
        for competitor in competition.get("competitors") or []:
            if not isinstance(competitor, dict):
                continue
            side = str(competitor.get("homeAway") or "").casefold()
            team = competitor.get("team") or {}
            if not isinstance(team, dict):
                continue
            name = " ".join(str(team.get("displayName") or "").split())
            if side in {"home", "away"} and name:
                by_side[side] = name

        away_team = by_side.get("away", "")
        home_team = by_side.get("home", "")
        if away_team not in NFL_TEAM_NAMES or home_team not in NFL_TEAM_NAMES:
            continue

        venue_data = competition.get("venue") or {}
        venue = (
            " ".join(str(venue_data.get("fullName") or "").split())
            if isinstance(venue_data, dict)
            else ""
        )
        schedule_id = str(event.get("id") or competition.get("id") or "").strip()
        if not schedule_id:
            schedule_id = f"{event_date.isoformat()}:{away_team}:{home_team}"
        name = " ".join(str(event.get("name") or "").split())
        if not name:
            name = f"{away_team} at {home_team}"

        games[schedule_id] = ScheduledNFLGame(
            schedule_id=schedule_id,
            event_date=event_date,
            away_team=away_team,
            home_team=home_team,
            venue=venue,
            name=name,
        )

    return sorted(
        games.values(),
        key=lambda game: (game.event_date, game.away_team, game.home_team),
    )


def fetch_schedule_games(
    now: datetime,
    horizon_hours: int = NFL_CAPTURE_WINDOW_HOURS,
    *,
    fetcher: JsonFetcher = fetch_json,
) -> tuple[list[ScheduledNFLGame], str]:
    url = schedule_url(now, horizon_hours)
    payload = fetcher(url, SCHEDULE_REQUEST_TIMEOUT_SECONDS)
    return parse_schedule_payload(payload, now, horizon_hours), url


def schedule_games_due(
    schedule: list[ScheduledNFLGame],
    capture_slot: datetime,
) -> list[ScheduledNFLGame]:
    return [
        game
        for game in schedule
        if nfl_capture_is_due(game.event_date, capture_slot, game.schedule_id)
    ]


def schedule_cadence_summary(
    schedule: list[ScheduledNFLGame],
    capture_slot: datetime,
) -> dict[str, dict[str, int]]:
    in_window = {"1h": 0, "3h": 0, "6h": 0}
    due = {"1h": 0, "3h": 0, "6h": 0}
    for game in schedule:
        interval = nfl_capture_interval_hours(game.event_date, capture_slot)
        if interval is None:
            continue
        label = f"{interval}h"
        in_window[label] += 1
        if nfl_capture_is_due(game.event_date, capture_slot, game.schedule_id):
            due[label] += 1
    return {"in_window": in_window, "due_now": due}


def matchup_key_from_title(title: str) -> tuple[str, str] | None:
    normalized = " ".join(str(title or "").split()).casefold()
    teams = [team for team in NFL_TEAM_NAMES if team.casefold() in normalized]
    if len(teams) != 2:
        return None
    return tuple(sorted(teams))


def _dedupe_candidates(rows: Iterable[DiscoveredNFLGame]) -> tuple[DiscoveredNFLGame, ...]:
    by_url: dict[str, DiscoveredNFLGame] = {}
    for row in rows:
        by_url[row.url] = row
    return tuple(
        sorted(
            by_url.values(),
            key=lambda row: (row.date_hint or date.max, row.title.casefold(), row.url),
        )
    )


def candidates_for_schedule_game(
    game: ScheduledNFLGame,
    rows: Iterable[DiscoveredNFLGame],
) -> tuple[DiscoveredNFLGame, ...]:
    matches = [
        row for row in rows if matchup_key_from_title(row.title) == game.matchup_key
    ]
    same_date = [row for row in matches if row.date_hint == game.local_date]
    return _dedupe_candidates(same_date or matches)


def _load_search_page(browser: VividNFLBrowser, url: str) -> None:
    from selenium.common.exceptions import TimeoutException

    browser.driver.get("about:blank")
    try:
        browser.driver.get(url)
    except TimeoutException:
        browser.driver.execute_script("window.stop();")


def search_vivid_for_game(
    browser: VividNFLBrowser,
    game: ScheduledNFLGame,
) -> tuple[DiscoveredNFLGame, ...]:
    queries = (
        f"{game.away_team} at {game.home_team}",
        f"{game.away_team} {game.home_team}",
    )
    found: list[DiscoveredNFLGame] = []

    for query in queries:
        search_url = (
            "https://www.vividseats.com/search?searchTerm=" + quote_plus(query)
        )
        _load_search_page(browser, search_url)
        deadline = time.monotonic() + min(browser.timeout, VIVID_SEARCH_MAX_SECONDS)
        last_new_at: float | None = None
        known: dict[str, DiscoveredNFLGame] = {}

        while time.monotonic() < deadline:
            try:
                browser.driver.execute_script(
                    "window.scrollTo(0, Math.min(document.body.scrollHeight, 2400));"
                )
            except Exception:
                pass
            rows = candidates_for_schedule_game(
                game,
                extract_nfl_game_rows(
                    browser.driver.page_source,
                    search_url,
                    datetime.now(timezone.utc),
                ),
            )
            previous = len(known)
            for row in rows:
                known[row.url] = row
            if len(known) > previous:
                last_new_at = time.monotonic()
            elif known and last_new_at is not None:
                if time.monotonic() - last_new_at >= VIVID_SEARCH_SETTLE_SECONDS:
                    found.extend(known.values())
                    break
            time.sleep(0.4)

        if known:
            found.extend(known.values())
            break

    return _dedupe_candidates(found)


def resolve_schedule_games(
    schedule: list[ScheduledNFLGame],
    feed_rows: list[DiscoveredNFLGame],
    *,
    headless: bool,
    timeout: int,
) -> tuple[list[ScheduleResolution], list[str]]:
    resolutions: list[ScheduleResolution] = []
    missing = [game for game in schedule if not candidates_for_schedule_game(game, feed_rows)]
    search_browser: VividNFLBrowser | None = None
    search_errors: list[str] = []

    try:
        if missing:
            search_browser = VividNFLBrowser(headless=headless, timeout=timeout)

        for game in schedule:
            candidates = candidates_for_schedule_game(game, feed_rows)
            source = "vivid-nfl-feed"
            if not candidates and search_browser is not None:
                try:
                    candidates = search_vivid_for_game(search_browser, game)
                except Exception as exc:
                    search_errors.append(
                        f"{game.name}: Vivid search {type(exc).__name__}: {exc}"
                    )
                source = "vivid-search" if candidates else "unresolved"
            resolutions.append(
                ScheduleResolution(game=game, candidates=candidates, source=source)
            )
    finally:
        if search_browser is not None:
            try:
                search_browser.close()
            except Exception:
                pass

    return resolutions, search_errors


def validate_captured_match(
    scheduled: ScheduledNFLGame,
    event_date: datetime,
    title: str,
) -> None:
    if matchup_key_from_title(title) != scheduled.matchup_key:
        raise ValueError(
            f"Captured title does not match scheduled teams: {title}"
        )
    difference = abs((as_utc(event_date) - scheduled.event_date).total_seconds())
    if difference > EVENT_TIME_TOLERANCE_HOURS * 3600:
        raise ValueError(
            "Vivid kickoff differs from the schedule by more than "
            f"{EVENT_TIME_TOLERANCE_HOURS} hours."
        )


def _capture_resolution(
    resolution: ScheduleResolution,
    *,
    headless: bool,
    timeout: int,
) -> tuple[str, datetime, Any]:
    errors: list[str] = []
    for candidate in resolution.candidates:
        browser: VividNFLBrowser | None = None
        try:
            url = validated_vivid_url(candidate.url)
            browser = VividNFLBrowser(headless=headless, timeout=timeout)
            raw_payload, event_date = browser.capture(url)
            snapshot = NFLSnapshotParser.parse(raw_payload)
            validate_captured_match(resolution.game, event_date, snapshot.title)
            return url, event_date, snapshot
        except Exception as exc:
            errors.append(f"{candidate.url}: {type(exc).__name__}: {exc}")
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
    raise RuntimeError("; ".join(errors) or "No Vivid candidate was available.")


def _write_health(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def _fallback_to_feed_collector(
    *,
    endpoint: str,
    token: str,
    headless: bool,
    timeout: int,
    health_output: Path,
    pending_dir: Path,
    schedule_error: str,
) -> int:
    code = run_feed_only_collector(
        endpoint,
        token,
        headless,
        timeout,
        health_output,
        pending_dir,
    )
    try:
        report = json.loads(health_output.read_text(encoding="utf-8"))
    except Exception:
        report = {}
    report.update(
        {
            "status": "degraded",
            "coverage_mode": "vivid-feed-fallback",
            "schedule_status": "unavailable",
            "schedule_error": schedule_error,
        }
    )
    _write_health(health_output, report)
    # Preserve ticket collection, but fail the check so schedule coverage loss
    # cannot pass silently.
    return code or 1


def run_schedule_collector(
    endpoint: str,
    token: str,
    headless: bool,
    timeout: int,
    health_output: Path,
    pending_dir: Path,
) -> int:
    started_at = datetime.now(timezone.utc)
    replayed, endpoint_available, queue_errors = replay_pending_snapshots(
        endpoint, token, pending_dir
    )

    capture_slot = hourly_capture_slot(started_at)
    try:
        schedule, schedule_source = fetch_schedule_games(started_at)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        print(f"NFL SCHEDULE FAILED: {message}", file=sys.stderr, flush=True)
        return _fallback_to_feed_collector(
            endpoint=endpoint,
            token=token,
            headless=headless,
            timeout=timeout,
            health_output=health_output,
            pending_dir=pending_dir,
            schedule_error=message,
        )

    due_schedule = schedule_games_due(schedule, capture_slot)
    cadence_summary = schedule_cadence_summary(schedule, capture_slot)
    if due_schedule:
        feed_rows, feed_errors = discover_nfl_games(headless, timeout)
        resolutions, search_errors = resolve_schedule_games(
            due_schedule,
            feed_rows,
            headless=headless,
            timeout=timeout,
        )
    else:
        feed_rows, feed_errors = [], []
        resolutions, search_errors = [], []
        print(
            f"No NFL games are due in this adaptive cadence slot; "
            f"{len(schedule)} remain inside the 30-day window.",
            flush=True,
        )

    uploaded = 0
    captured = 0
    queued = 0
    capture_errors: list[str] = []
    unresolved: list[str] = []
    uploads: list[dict[str, Any]] = []
    matched_from_feed = sum(
        resolution.source == "vivid-nfl-feed" for resolution in resolutions
    )
    recovered_from_search = sum(
        resolution.source == "vivid-search" for resolution in resolutions
    )

    for index, resolution in enumerate(resolutions, start=1):
        game = resolution.game
        if not resolution.candidates:
            message = f"{game.away_team} at {game.home_team} ({game.event_date.isoformat()})"
            unresolved.append(message)
            print(f"NFL COVERAGE MISSING: {message}", file=sys.stderr, flush=True)
            continue

        try:
            print(
                f"[{index}/{len(resolutions)}] Capturing scheduled NFL game "
                f"{game.away_team} at {game.home_team} via {resolution.source}.",
                flush=True,
            )
            url, event_date, snapshot = _capture_resolution(
                resolution,
                headless=headless,
                timeout=timeout,
            )
            if not nfl_is_within_capture_window(event_date, datetime.now(timezone.utc)):
                raise ValueError("Resolved Vivid event is outside the exact 720-hour window.")

            payload = nfl_snapshot_to_payload(
                url,
                event_date,
                capture_slot,
                snapshot,
            )
            pending_path = queue_snapshot(payload, pending_dir)
            captured += 1

            if endpoint_available:
                try:
                    response = post_snapshot_with_retry(endpoint, token, payload)
                except Exception as exc:
                    endpoint_available = False
                    queued += 1
                    queue_errors.append(
                        f"Snapshot retained as {pending_path.name}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                else:
                    pending_path.unlink(missing_ok=True)
                    uploaded += response["status"] == "stored"
                    uploads.append(
                        {
                            "schedule_id": game.schedule_id,
                            "cadence_hours": nfl_capture_interval_hours(
                                game.event_date, capture_slot
                            ),
                            "cadence_tier": nfl_capture_tier(
                                game.event_date, capture_slot
                            ),
                            "url": url,
                            "title": snapshot.title,
                            "venue": snapshot.venue,
                            "sections": len(snapshot.sections),
                            "resolution_source": resolution.source,
                            "result": response["status"],
                        }
                    )
                    print(
                        f"UPLOADED: {len(snapshot.sections)} sections for "
                        f"{snapshot.title} ({response['status']}).",
                        flush=True,
                    )
            else:
                queued += 1
                print(
                    f"QUEUED: {len(snapshot.sections)} sections for {snapshot.title}.",
                    flush=True,
                )
        except Exception as exc:
            message = (
                f"{game.away_team} at {game.home_team}: "
                f"{type(exc).__name__}: {exc}"
            )
            capture_errors.append(message)
            print(f"NFL CAPTURE FAILED: {message}", file=sys.stderr, flush=True)

    pending_count = len(list(pending_dir.glob("*.json")))
    expected = len(due_schedule)
    coverage = 100.0 if expected == 0 else round(captured / expected * 100, 2)
    all_errors = feed_errors + search_errors + capture_errors + queue_errors
    if unresolved or capture_errors or feed_errors or search_errors:
        status = "degraded"
    elif pending_count:
        status = "queued"
    else:
        status = "healthy"

    report = {
        "status": status,
        "event_type": "nfl",
        "coverage_mode": "schedule-backed",
        "schedule_status": "available",
        "schedule_source": schedule_source,
        "started_at": started_at.isoformat(),
        "capture_slot": capture_slot.isoformat(),
        "capture_window_hours": NFL_CAPTURE_WINDOW_HOURS,
        "cadence_policy": {
            "days_15_to_30_hours": 6,
            "days_8_to_14_hours": 3,
            "final_7_days_hours": 1,
            "staggering": "deterministic per schedule ID",
        },
        "scheduled_in_window": len(schedule),
        "scheduled_due": expected,
        "skipped_by_cadence": len(schedule) - expected,
        "cadence_tiers": cadence_summary,
        "feed_discovered": len(feed_rows),
        "matched_from_feed": matched_from_feed,
        "recovered_from_search": recovered_from_search,
        "unresolved_count": len(unresolved),
        "unresolved": unresolved,
        "coverage_percent": coverage,
        "captured": captured,
        "uploaded": uploaded,
        "queued": queued,
        "replayed": replayed,
        "pending": pending_count,
        "failed": len(capture_errors),
        "uploads": uploads,
        "errors": all_errors,
    }
    _write_health(health_output, report)
    print(
        "NFL schedule-backed collection finished: "
        f"{captured}/{expected} due games captured ({coverage:.2f}% coverage); "
        f"{len(schedule)} games remain inside the 30-day window, "
        f"{recovered_from_search} recovered through search, "
        f"{len(unresolved)} unresolved, {queued} queued, {replayed} replayed.",
        flush=True,
    )

    if expected > 0 and captured < expected:
        return 1
    if feed_errors or search_errors:
        return 1
    return 0


def run_schedule_smoke(
    requested_url: str,
    headless: bool,
    timeout: int,
    output: Path,
) -> int:
    if requested_url:
        return run_feed_smoke_capture(
            requested_url,
            headless,
            timeout,
            output,
        )

    started_at = datetime.now(timezone.utc)
    schedule, schedule_source = fetch_schedule_games(
        started_at,
        horizon_hours=SMOKE_HORIZON_HOURS,
    )
    if not schedule:
        raise RuntimeError("No scheduled NFL game exists inside the 45-day smoke horizon.")

    feed_rows, feed_errors = discover_nfl_games(headless, timeout)
    resolutions, search_errors = resolve_schedule_games(
        schedule,
        feed_rows,
        headless=headless,
        timeout=timeout,
    )
    errors = feed_errors + search_errors
    for resolution in resolutions:
        if not resolution.candidates:
            continue
        try:
            url, event_date, snapshot = _capture_resolution(
                resolution,
                headless=headless,
                timeout=timeout,
            )
        except Exception as exc:
            errors.append(
                f"{resolution.game.name}: {type(exc).__name__}: {exc}"
            )
            continue

        result = {
            "status": "success",
            "event_type": "nfl",
            "coverage_mode": "schedule-backed",
            "schedule_source": schedule_source,
            "schedule_id": resolution.game.schedule_id,
            "resolution_source": resolution.source,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "event_date": event_date.isoformat(),
            "source_url": url,
            "source_id": snapshot.source_id,
            "title": snapshot.title,
            "venue": snapshot.venue,
            "section_count": len(snapshot.sections),
            "lowest_section_price": min(row.price for row in snapshot.sections),
            "highest_section_price": max(row.price for row in snapshot.sections),
            "sections": [asdict(row) for row in snapshot.sections],
        }
        _write_health(output, result)
        print(
            f"NFL SCHEDULE SMOKE PASSED: {snapshot.title} resolved via "
            f"{resolution.source} with {len(snapshot.sections)} sections.",
            flush=True,
        )
        return 0

    _write_health(
        output,
        {
            "status": "failure",
            "event_type": "nfl",
            "coverage_mode": "schedule-backed",
            "scheduled_candidates": len(schedule),
            "errors": errors,
        },
    )
    raise RuntimeError("No scheduled NFL game could be resolved and captured.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("remote-run", "smoke", "schedule"))
    parser.add_argument("event_url", nargs="?", default="")
    parser.add_argument("--endpoint")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--health-output", type=Path, default=DEFAULT_HEALTH_OUTPUT)
    parser.add_argument("--pending-dir", type=Path, default=DEFAULT_PENDING_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_SMOKE_OUTPUT)
    parser.add_argument(
        "--horizon-hours",
        type=int,
        default=NFL_CAPTURE_WINDOW_HOURS,
    )
    args = parser.parse_args()

    if args.command == "schedule":
        games, source = fetch_schedule_games(
            datetime.now(timezone.utc),
            horizon_hours=args.horizon_hours,
        )
        print(
            json.dumps(
                {
                    "source": source,
                    "count": len(games),
                    "games": [
                        {
                            **asdict(game),
                            "event_date": game.event_date.isoformat(),
                        }
                        for game in games
                    ],
                },
                indent=2,
            )
        )
        return 0

    if args.command == "smoke":
        return run_schedule_smoke(
            args.event_url,
            args.headless,
            args.timeout,
            args.output,
        )

    if not args.endpoint:
        parser.error("remote-run requires --endpoint")
    token = os.environ.get("COLLECTOR_INGEST_TOKEN", "")
    if not token:
        parser.error("COLLECTOR_INGEST_TOKEN is required")
    return run_schedule_collector(
        args.endpoint,
        token,
        args.headless,
        args.timeout,
        args.health_output,
        args.pending_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
