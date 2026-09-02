"""Schedule-backed NHL ticket collection from the official NHL schedule and Vivid.

Only games played in the United States or Canada are in scope. The collector
checks the official schedule, opens Vivid only for games due in the current
cadence slot, and records explicit coverage and upload health.
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
from zoneinfo import ZoneInfo

from collector import (
    as_utc,
    post_snapshot_with_retry,
    queue_snapshot,
    replay_pending_snapshots,
    validated_vivid_url,
)
from nhl_collector import (
    DEFAULT_HEALTH_OUTPUT,
    DEFAULT_PENDING_DIR,
    DEFAULT_SMOKE_OUTPUT,
    NHL_CAPTURE_WINDOW_HOURS,
    NHL_DAILY_CADENCE_HOURS,
    NHL_FINAL_CADENCE_HOURS,
    NHLInventoryIncompleteError,
    NHL_SIX_HOUR_CADENCE_HOURS,
    NHL_TEAM_NAMES,
    NHL_TWELVE_HOUR_CADENCE_HOURS,
    DiscoveredNHLGame,
    NHLSnapshotParser,
    VividNFLBrowser,
    discover_nhl_games,
    extract_nhl_game_rows,
    hourly_capture_slot,
    nhl_capture_interval_hours,
    nhl_capture_is_due,
    nhl_capture_tier,
    nhl_is_within_capture_window,
    nhl_snapshot_to_payload,
    ordered_matchup_from_title,
)
from nfl_metadata import canonical_venue_name, eastern_iso, geometry_section_count


NHL_SCHEDULE_URL = "https://api-web.nhle.com/v1/schedule/{date}"
DEFAULT_SCHEDULE_HEALTH_OUTPUT = Path("nhl_schedule_health.json")
SCHEDULE_REQUEST_TIMEOUT_SECONDS = 20
SCHEDULE_REQUEST_ATTEMPTS = 3
MAX_SCHEDULE_PAGES = 8
VIVID_SEARCH_SETTLE_SECONDS = 2.0
VIVID_SEARCH_MAX_SECONDS = 18
EVENT_TIME_TOLERANCE_HOURS = 18
SMOKE_HORIZON_HOURS = 45 * 24

NHL_TEAM_BY_ABBREVIATION = {
    "ANA": "Anaheim Ducks",
    "BOS": "Boston Bruins",
    "BUF": "Buffalo Sabres",
    "CGY": "Calgary Flames",
    "CAR": "Carolina Hurricanes",
    "CHI": "Chicago Blackhawks",
    "COL": "Colorado Avalanche",
    "CBJ": "Columbus Blue Jackets",
    "DAL": "Dallas Stars",
    "DET": "Detroit Red Wings",
    "EDM": "Edmonton Oilers",
    "FLA": "Florida Panthers",
    "LAK": "Los Angeles Kings",
    "MIN": "Minnesota Wild",
    "MTL": "Montreal Canadiens",
    "NSH": "Nashville Predators",
    "NJD": "New Jersey Devils",
    "NYI": "New York Islanders",
    "NYR": "New York Rangers",
    "OTT": "Ottawa Senators",
    "PHI": "Philadelphia Flyers",
    "PIT": "Pittsburgh Penguins",
    "SJS": "San Jose Sharks",
    "SEA": "Seattle Kraken",
    "STL": "St. Louis Blues",
    "TBL": "Tampa Bay Lightning",
    "TOR": "Toronto Maple Leafs",
    "UTA": "Utah Mammoth",
    "VAN": "Vancouver Canucks",
    "VGK": "Vegas Golden Knights",
    "WSH": "Washington Capitals",
    "WPG": "Winnipeg Jets",
}
CANADIAN_TEAMS = frozenset(
    {
        "Calgary Flames",
        "Edmonton Oilers",
        "Montreal Canadiens",
        "Ottawa Senators",
        "Toronto Maple Leafs",
        "Vancouver Canucks",
        "Winnipeg Jets",
    }
)
CANADIAN_VENUE_TIMEZONES = frozenset(
    {
        "america/toronto",
        "america/montreal",
        "america/winnipeg",
        "america/edmonton",
        "america/vancouver",
        "america/halifax",
        "america/st_johns",
        "canada/eastern",
        "canada/central",
        "canada/mountain",
        "canada/pacific",
        "canada/atlantic",
        "canada/newfoundland",
    }
)
SUPPORTED_TIMEZONE_PREFIXES = ("america/", "us/", "canada/")


@dataclass(frozen=True)
class ScheduledNHLGame:
    schedule_id: str
    event_date: datetime
    away_team: str
    home_team: str
    venue: str
    name: str
    venue_timezone: str = ""
    country: str = ""
    neutral_site: bool = False
    game_type: int = 2
    season: int | None = None

    @property
    def matchup_key(self) -> tuple[str, str]:
        return self.away_team, self.home_team

    @property
    def local_date(self) -> date:
        if self.venue_timezone:
            try:
                return self.event_date.astimezone(ZoneInfo(self.venue_timezone)).date()
            except Exception:
                pass
        return self.event_date.astimezone(ZoneInfo("America/New_York")).date()

    def snapshot_metadata(self, provider_venue: str) -> dict[str, Any]:
        return {
            "schedule_id": self.schedule_id,
            "away_team": self.away_team,
            "home_team": self.home_team,
            "canonical_venue": canonical_venue_name(self.venue or provider_venue),
            "venue_timezone": self.venue_timezone,
            "country": self.country,
            "neutral_site": self.neutral_site,
            "game_type": self.game_type,
            "season": self.season,
            "provider_venue": " ".join(str(provider_venue or "").split()),
        }


@dataclass(frozen=True)
class ScheduleResolution:
    game: ScheduledNHLGame
    candidates: tuple[DiscoveredNHLGame, ...]
    source: str


class NHLProviderGapError(RuntimeError):
    """Vivid lacks enough trustworthy inventory for a scheduled game."""


JsonFetcher = Callable[[str, int], dict[str, Any]]


def _localized_default(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("default") or value.get("fr") or next(
            (item for item in value.values() if item),
            "",
        )
    return " ".join(str(value or "").split())


def _parse_datetime(raw: str) -> datetime:
    value = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def schedule_url(start_date: date) -> str:
    return NHL_SCHEDULE_URL.format(date=start_date.isoformat())


def fetch_json(url: str, timeout: int = SCHEDULE_REQUEST_TIMEOUT_SECONDS) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "TicketSignal-NHL-coverage/1.0",
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, SCHEDULE_REQUEST_ATTEMPTS + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("NHL schedule response is not a JSON object.")
            return payload
        except Exception as exc:
            last_error = exc
            if attempt < SCHEDULE_REQUEST_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))
    assert last_error is not None
    raise RuntimeError(
        f"NHL schedule request failed after {SCHEDULE_REQUEST_ATTEMPTS} attempts: "
        f"{type(last_error).__name__}: {last_error}"
    ) from last_error


def venue_timezone_is_supported(value: Any) -> bool:
    timezone_name = " ".join(str(value or "").split()).casefold()
    return not timezone_name or timezone_name.startswith(SUPPORTED_TIMEZONE_PREFIXES)


def infer_country(
    home_team: str,
    venue_timezone: str,
    neutral_site: bool,
) -> str:
    normalized_timezone = venue_timezone.casefold()
    if neutral_site:
        return (
            "Canada"
            if normalized_timezone in CANADIAN_VENUE_TIMEZONES
            else "USA"
        )
    return "Canada" if home_team in CANADIAN_TEAMS else "USA"


def game_type_label(game_type: int) -> str:
    return {
        1: "Preseason",
        2: "Regular season",
        3: "Playoffs",
    }.get(game_type, f"Game type {game_type}")


def parse_schedule_payload(
    payload: dict[str, Any],
    now: datetime,
    horizon_hours: int = NHL_CAPTURE_WINDOW_HOURS,
) -> list[ScheduledNHLGame]:
    now_utc = now.astimezone(timezone.utc)
    horizon = timedelta(hours=horizon_hours)
    games: dict[str, ScheduledNHLGame] = {}

    for day in payload.get("gameWeek") or []:
        if not isinstance(day, dict):
            continue
        for game in day.get("games") or []:
            if not isinstance(game, dict):
                continue
            try:
                event_date = _parse_datetime(game["startTimeUTC"])
            except (KeyError, TypeError, ValueError):
                continue

            delta = event_date - now_utc
            if delta <= timedelta(0) or delta > horizon:
                continue

            away_data = game.get("awayTeam") or {}
            home_data = game.get("homeTeam") or {}
            if not isinstance(away_data, dict) or not isinstance(home_data, dict):
                continue
            away_team = NHL_TEAM_BY_ABBREVIATION.get(
                str(away_data.get("abbrev") or "").upper()
            )
            home_team = NHL_TEAM_BY_ABBREVIATION.get(
                str(home_data.get("abbrev") or "").upper()
            )
            if away_team not in NHL_TEAM_NAMES or home_team not in NHL_TEAM_NAMES:
                continue

            venue = canonical_venue_name(_localized_default(game.get("venue")))
            venue_timezone = " ".join(
                str(game.get("venueTimezone") or "").split()
            )
            if not venue_timezone_is_supported(venue_timezone):
                continue

            game_type = int(game.get("gameType") or 0)
            if game_type not in {1, 2, 3}:
                continue
            game_state = str(game.get("gameState") or "").upper()
            if game_state in {"FINAL", "OFF"}:
                continue

            neutral_site = bool(game.get("neutralSite") is True)
            country = infer_country(home_team, venue_timezone, neutral_site)
            schedule_id = str(game.get("id") or "").strip()
            if not schedule_id:
                schedule_id = (
                    f"{event_date.isoformat()}:{away_team}:{home_team}"
                )
            season_raw = game.get("season")
            try:
                season = int(season_raw) if season_raw is not None else None
            except (TypeError, ValueError):
                season = None
            name = f"{away_team} at {home_team}"

            games[schedule_id] = ScheduledNHLGame(
                schedule_id=schedule_id,
                event_date=event_date,
                away_team=away_team,
                home_team=home_team,
                venue=venue,
                name=name,
                venue_timezone=venue_timezone,
                country=country,
                neutral_site=neutral_site,
                game_type=game_type,
                season=season,
            )

    return sorted(
        games.values(),
        key=lambda game: (game.event_date, game.away_team, game.home_team),
    )


def fetch_schedule_games(
    now: datetime,
    horizon_hours: int = NHL_CAPTURE_WINDOW_HOURS,
    *,
    fetcher: JsonFetcher = fetch_json,
) -> tuple[list[ScheduledNHLGame], list[str]]:
    eastern = ZoneInfo("America/New_York")
    current_date = now.astimezone(eastern).date()
    horizon_date = (now + timedelta(hours=horizon_hours)).astimezone(eastern).date()
    pages: list[str] = []
    games: dict[str, ScheduledNHLGame] = {}

    for _ in range(MAX_SCHEDULE_PAGES):
        url = schedule_url(current_date)
        payload = fetcher(url, SCHEDULE_REQUEST_TIMEOUT_SECONDS)
        pages.append(url)
        for game in parse_schedule_payload(payload, now, horizon_hours):
            games[game.schedule_id] = game

        next_start_raw = payload.get("nextStartDate")
        try:
            next_start = date.fromisoformat(str(next_start_raw))
        except (TypeError, ValueError):
            break
        if next_start <= current_date or next_start > horizon_date:
            break
        current_date = next_start

    return (
        sorted(
            games.values(),
            key=lambda game: (game.event_date, game.away_team, game.home_team),
        ),
        pages,
    )


def schedule_games_due(
    schedule: list[ScheduledNHLGame],
    capture_slot: datetime,
) -> list[ScheduledNHLGame]:
    return [
        game
        for game in schedule
        if nhl_capture_is_due(game.event_date, capture_slot, game.schedule_id)
    ]


def schedule_cadence_summary(
    schedule: list[ScheduledNHLGame],
    capture_slot: datetime,
) -> dict[str, dict[str, int]]:
    labels = ("1h", "6h", "12h", "24h")
    in_window = {label: 0 for label in labels}
    due = {label: 0 for label in labels}
    for game in schedule:
        interval = nhl_capture_interval_hours(game.event_date, capture_slot)
        if interval is None:
            continue
        label = f"{interval}h"
        in_window[label] = in_window.get(label, 0) + 1
        if nhl_capture_is_due(game.event_date, capture_slot, game.schedule_id):
            due[label] = due.get(label, 0) + 1
    return {"in_window": in_window, "due_now": due}

def _dedupe_candidates(
    rows: Iterable[DiscoveredNHLGame],
) -> tuple[DiscoveredNHLGame, ...]:
    by_url: dict[str, DiscoveredNHLGame] = {}
    for row in rows:
        by_url[row.url] = row
    return tuple(
        sorted(
            by_url.values(),
            key=lambda row: (row.date_hint or date.max, row.title.casefold(), row.url),
        )
    )


def candidates_for_schedule_game(
    game: ScheduledNHLGame,
    rows: Iterable[DiscoveredNHLGame],
) -> tuple[DiscoveredNHLGame, ...]:
    expected_order = game.matchup_key
    matches = [
        row
        for row in rows
        if ordered_matchup_from_title(row.title) == expected_order
    ]
    same_date = [row for row in matches if row.date_hint == game.local_date]
    undated = [row for row in matches if row.date_hint is None]
    # A later meeting between the same teams is not a fallback for this
    # game. Undated candidates remain eligible and are verified from the
    # captured provider metadata.
    return _dedupe_candidates(same_date or undated)


def _load_search_page(browser: VividNFLBrowser, url: str) -> None:
    from selenium.common.exceptions import TimeoutException

    browser.driver.get("about:blank")
    try:
        browser.driver.get(url)
    except TimeoutException:
        browser.driver.execute_script("window.stop();")


def search_vivid_for_game(
    browser: VividNFLBrowser,
    game: ScheduledNHLGame,
) -> tuple[DiscoveredNHLGame, ...]:
    queries = (
        f"{game.away_team} at {game.home_team}",
        f"{game.away_team} {game.home_team}",
    )
    found: list[DiscoveredNHLGame] = []

    for query in queries:
        search_url = (
            "https://www.vividseats.com/search?searchTerm=" + quote_plus(query)
        )
        _load_search_page(browser, search_url)
        deadline = time.monotonic() + min(browser.timeout, VIVID_SEARCH_MAX_SECONDS)
        last_new_at: float | None = None
        known: dict[str, DiscoveredNHLGame] = {}

        while time.monotonic() < deadline:
            try:
                browser.driver.execute_script(
                    "window.scrollTo(0, Math.min(document.body.scrollHeight, 2400));"
                )
            except Exception:
                pass
            rows = candidates_for_schedule_game(
                game,
                extract_nhl_game_rows(
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
    schedule: list[ScheduledNHLGame],
    feed_rows: list[DiscoveredNHLGame],
    *,
    headless: bool,
    timeout: int,
) -> tuple[list[ScheduleResolution], list[str]]:
    resolutions: list[ScheduleResolution] = []
    missing = [
        game
        for game in schedule
        if not candidates_for_schedule_game(game, feed_rows)
    ]
    search_browser: VividNFLBrowser | None = None
    search_errors: list[str] = []

    try:
        if missing:
            search_browser = VividNFLBrowser(headless=headless, timeout=timeout)

        for game in schedule:
            candidates = candidates_for_schedule_game(game, feed_rows)
            source = "vivid-nhl-feed"
            if not candidates and search_browser is not None:
                try:
                    candidates = search_vivid_for_game(search_browser, game)
                except Exception as exc:
                    search_errors.append(
                        f"{game.name}: Vivid search {type(exc).__name__}: {exc}"
                    )
                source = "vivid-search" if candidates else "unresolved"
            resolutions.append(
                ScheduleResolution(
                    game=game,
                    candidates=candidates,
                    source=source,
                )
            )
    finally:
        if search_browser is not None:
            try:
                search_browser.close()
            except Exception:
                pass

    return resolutions, search_errors


def validate_captured_match(
    scheduled: ScheduledNHLGame,
    event_date: datetime,
    title: str,
) -> datetime:
    if ordered_matchup_from_title(title) != scheduled.matchup_key:
        raise ValueError(
            "Captured title does not match scheduled NHL teams in away/home order: "
            f"{title}"
        )

    provider_utc = as_utc(event_date)
    try:
        provider_zone = ZoneInfo(
            scheduled.venue_timezone or "America/New_York"
        )
    except Exception:
        provider_zone = ZoneInfo("America/New_York")
    provider_local_date = provider_utc.astimezone(provider_zone).date()
    difference = abs((provider_utc - scheduled.event_date).total_seconds())
    if (
        provider_local_date != scheduled.local_date
        and difference > EVENT_TIME_TOLERANCE_HOURS * 3600
    ):
        raise ValueError(
            "Vivid event date does not match the NHL schedule: "
            f"provider {provider_local_date.isoformat()}, "
            f"schedule {scheduled.local_date.isoformat()}."
        )

    # The official NHL schedule is canonical. Some Vivid pages expose a
    # date-only startDate, which becomes local midnight and appears 19
    # hours earlier than a normal evening game on the same calendar date.
    return scheduled.event_date

def _capture_resolution(
    resolution: ScheduleResolution,
    *,
    headless: bool,
    timeout: int,
) -> tuple[str, datetime, Any]:
    provider_gap_errors: list[str] = []
    capture_errors: list[str] = []
    for candidate in resolution.candidates:
        browser: VividNFLBrowser | None = None
        try:
            url = validated_vivid_url(candidate.url)
            browser = VividNFLBrowser(headless=headless, timeout=timeout)
            raw_payload, provider_event_date = browser.capture(url)
            snapshot = NHLSnapshotParser.parse(raw_payload)
            event_date = validate_captured_match(
                resolution.game,
                provider_event_date,
                snapshot.title,
            )
            return url, event_date, snapshot
        except NHLInventoryIncompleteError as exc:
            provider_gap_errors.append(
                f"{candidate.url}: {type(exc).__name__}: {exc}"
            )
        except Exception as exc:
            capture_errors.append(
                f"{candidate.url}: {type(exc).__name__}: {exc}"
            )
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass

    if provider_gap_errors and not capture_errors:
        raise NHLProviderGapError("; ".join(provider_gap_errors))
    raise RuntimeError(
        "; ".join(capture_errors + provider_gap_errors)
        or "No Vivid candidate was available."
    )


def nhl_collection_should_fail(
    capture_errors: list[str],
    search_errors: list[str],
) -> bool:
    """Only operational failures should turn the workflow red."""

    return bool(capture_errors or search_errors)

def _write_health(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def nhl_should_skip_for_trigger(event_name: str | None = None) -> bool:
    """Reserve GitHub's scheduled recovery trigger for baseball only."""

    normalized = (
        event_name
        if event_name is not None
        else os.environ.get("GITHUB_EVENT_NAME", "")
    )
    return str(normalized).strip().lower() == "schedule"


def run_schedule_collector(
    endpoint: str,
    token: str,
    headless: bool,
    timeout: int,
    health_output: Path,
    pending_dir: Path,
) -> int:
    started_at = datetime.now(timezone.utc)
    if nhl_should_skip_for_trigger():
        report = {
            "status": "skipped",
            "event_type": "nhl",
            "timezone": "America/New_York",
            "started_at": eastern_iso(started_at),
            "reason": "scheduled GitHub recovery trigger is baseball-only",
        }
        _write_health(health_output, report)
        print(
            "Skipping NHL; the scheduled GitHub recovery trigger is "
            "reserved for baseball.",
            flush=True,
        )
        return 0

    replayed, endpoint_available, queue_errors = replay_pending_snapshots(
        endpoint,
        token,
        pending_dir,
    )
    capture_slot = hourly_capture_slot(started_at)

    try:
        schedule, schedule_sources = fetch_schedule_games(started_at)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        _write_health(
            health_output,
            {
                "status": "failure",
                "event_type": "nhl",
                "schedule_status": "unavailable",
                "started_at": eastern_iso(started_at),
                "error": message,
            },
        )
        print(
            f"NHL SCHEDULE FAILED: {message}",
            file=sys.stderr,
            flush=True,
        )
        return 1

    due_schedule = schedule_games_due(schedule, capture_slot)
    cadence_summary = schedule_cadence_summary(schedule, capture_slot)
    if due_schedule:
        feed_rows, feed_warnings = discover_nhl_games(headless, timeout)
        resolutions, search_errors = resolve_schedule_games(
            due_schedule,
            feed_rows,
            headless=headless,
            timeout=timeout,
        )
    else:
        feed_rows, feed_warnings = [], []
        resolutions, search_errors = [], []
        print(
            f"No NHL games are due in this cadence slot; "
            f"{len(schedule)} remain inside the 30-day window.",
            flush=True,
        )

    uploaded = 0
    captured = 0
    queued = 0
    capture_errors: list[str] = []
    unresolved: list[str] = []
    provider_gaps: list[str] = []
    uploads: list[dict[str, Any]] = []

    for index, resolution in enumerate(resolutions, start=1):
        game = resolution.game
        if not resolution.candidates:
            message = (
                f"{game.away_team} at {game.home_team} "
                f"({eastern_iso(game.event_date)})"
            )
            unresolved.append(message)
            provider_gaps.append(
                f"{message}: no exact-date Vivid event was available"
            )
            print(
                f"NHL PROVIDER GAP: {message}",
                file=sys.stderr,
                flush=True,
            )
            continue

        try:
            print(
                f"[{index}/{len(resolutions)}] Capturing NHL game "
                f"{game.away_team} at {game.home_team} via "
                f"{resolution.source}.",
                flush=True,
            )
            url, event_date, snapshot = _capture_resolution(
                resolution,
                headless=headless,
                timeout=timeout,
            )
            if not nhl_is_within_capture_window(
                event_date,
                datetime.now(timezone.utc),
            ):
                raise ValueError(
                    "Resolved NHL event is outside the exact 30-day window."
                )

            payload = nhl_snapshot_to_payload(
                url,
                event_date,
                capture_slot,
                snapshot,
                schedule=game.snapshot_metadata(snapshot.venue),
            )
            pending_path = queue_snapshot(payload, pending_dir)
            captured += 1

            if endpoint_available:
                try:
                    response = post_snapshot_with_retry(
                        endpoint,
                        token,
                        payload,
                    )
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
                            "game_type": game_type_label(game.game_type),
                            "cadence_hours": nhl_capture_interval_hours(
                                game.event_date,
                                capture_slot,
                            ),
                            "cadence_tier": nhl_capture_tier(
                                game.event_date,
                                capture_slot,
                            ),
                            "url": url,
                            "title": snapshot.title,
                            "provider_venue": snapshot.venue,
                            "canonical_venue": game.snapshot_metadata(
                                snapshot.venue
                            )["canonical_venue"],
                            "country": game.country,
                            "currency": snapshot.currency,
                            "sections": len(snapshot.sections),
                            "map_geometry_sections": geometry_section_count(
                                getattr(snapshot, "map_geometry", None)
                            ),
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
                    f"QUEUED: {len(snapshot.sections)} sections for "
                    f"{snapshot.title}.",
                    flush=True,
                )
        except NHLProviderGapError as exc:
            message = (
                f"{game.away_team} at {game.home_team}: "
                f"{type(exc).__name__}: {exc}"
            )
            provider_gaps.append(message)
            print(
                f"NHL PROVIDER GAP: {message}",
                file=sys.stderr,
                flush=True,
            )
        except Exception as exc:
            message = (
                f"{game.away_team} at {game.home_team}: "
                f"{type(exc).__name__}: {exc}"
            )
            capture_errors.append(message)
            print(
                f"NHL CAPTURE FAILED: {message}",
                file=sys.stderr,
                flush=True,
            )

    pending_count = len(list(pending_dir.glob("*.json")))
    expected = len(due_schedule)
    coverage = (
        100.0
        if expected == 0
        else round(captured / expected * 100, 2)
    )
    provider_supported_expected = max(0, expected - len(provider_gaps))
    provider_supported_coverage = (
        100.0
        if provider_supported_expected == 0
        else round(captured / provider_supported_expected * 100, 2)
    )
    all_errors = search_errors + capture_errors + queue_errors
    collection_failed = nhl_collection_should_fail(
        capture_errors,
        search_errors,
    )
    if collection_failed or provider_gaps:
        status = "degraded"
    elif pending_count:
        status = "queued"
    else:
        status = "healthy"

    report = {
        "status": status,
        "event_type": "nhl",
        "coverage_mode": "official-schedule-backed",
        "collection_scope": "United States and Canada",
        "schedule_status": "available",
        "schedule_sources": schedule_sources,
        "timezone": "America/New_York",
        "started_at": eastern_iso(started_at),
        "capture_slot": eastern_iso(capture_slot),
        "capture_window_hours": NHL_CAPTURE_WINDOW_HOURS,
        "cadence_policy": {
            "days_15_to_30_hours": NHL_DAILY_CADENCE_HOURS,
            "days_8_to_14_hours": NHL_TWELVE_HOUR_CADENCE_HOURS,
            "days_4_to_7_hours": NHL_SIX_HOUR_CADENCE_HOURS,
            "final_72_hours": NHL_FINAL_CADENCE_HOURS,
            "staggering": "deterministic per NHL game ID",
        },
        "scheduled_in_window": len(schedule),
        "scheduled_due": expected,
        "skipped_by_cadence": len(schedule) - expected,
        "cadence_tiers": cadence_summary,
        "feed_discovered": len(feed_rows),
        "feed_warnings": feed_warnings,
        "unresolved_count": len(unresolved),
        "unresolved": unresolved,
        "provider_gap_count": len(provider_gaps),
        "provider_gaps": provider_gaps,
        "provider_supported_expected": provider_supported_expected,
        "provider_supported_coverage_percent": provider_supported_coverage,
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
        "NHL schedule-backed collection finished: "
        f"{captured}/{expected} due games captured "
        f"({coverage:.2f}% overall; "
        f"{provider_supported_coverage:.2f}% of Vivid-supported games); "
        f"{len(schedule)} games remain inside the 30-day window, "
        f"{len(provider_gaps)} provider gaps, {queued} queued, "
        f"{replayed} replayed.",
        flush=True,
    )

    return 1 if collection_failed else 0

def run_schedule_smoke(
    requested_url: str,
    headless: bool,
    timeout: int,
    output: Path,
) -> int:
    if requested_url:
        from nhl_collector import run_smoke_capture

        return run_smoke_capture(
            requested_url,
            headless,
            timeout,
            output,
        )

    started_at = datetime.now(timezone.utc)
    schedule, schedule_sources = fetch_schedule_games(
        started_at,
        horizon_hours=SMOKE_HORIZON_HOURS,
    )
    if not schedule:
        raise RuntimeError(
            "No U.S. or Canadian NHL game exists inside the 45-day smoke horizon."
        )

    feed_rows, feed_warnings = discover_nhl_games(headless, timeout)
    search_browser: VividNFLBrowser | None = None
    errors = list(feed_warnings)
    try:
        search_browser = VividNFLBrowser(headless=headless, timeout=timeout)
        for game in schedule[:24]:
            candidates = candidates_for_schedule_game(game, feed_rows)
            source = "vivid-nhl-feed"
            if not candidates:
                try:
                    candidates = search_vivid_for_game(search_browser, game)
                except Exception as exc:
                    errors.append(
                        f"{game.name}: Vivid search {type(exc).__name__}: {exc}"
                    )
                source = "vivid-search" if candidates else "unresolved"
            if not candidates:
                continue

            resolution = ScheduleResolution(game, candidates, source)
            try:
                url, event_date, snapshot = _capture_resolution(
                    resolution,
                    headless=headless,
                    timeout=timeout,
                )
            except Exception as exc:
                errors.append(f"{game.name}: {type(exc).__name__}: {exc}")
                continue

            result = {
                "status": "success",
                "event_type": "nhl",
                "coverage_mode": "official-schedule-backed",
                "schedule_sources": schedule_sources,
                "timezone": "America/New_York",
                "schedule": game.snapshot_metadata(snapshot.venue),
                "schedule_id": game.schedule_id,
                "resolution_source": source,
                "captured_at": eastern_iso(datetime.now(timezone.utc)),
                "event_date": eastern_iso(event_date),
                "source_url": url,
                "source_id": snapshot.source_id,
                "title": snapshot.title,
                "provider_venue": snapshot.venue,
                "canonical_venue": canonical_venue_name(
                    game.venue or snapshot.venue
                ),
                "currency": snapshot.currency,
                "section_count": len(snapshot.sections),
                "map_geometry_sections": geometry_section_count(
                    getattr(snapshot, "map_geometry", None)
                ),
                "map_geometry": getattr(snapshot, "map_geometry", None),
                "lowest_section_price": min(
                    row.price for row in snapshot.sections
                ),
                "highest_section_price": max(
                    row.price for row in snapshot.sections
                ),
                "sections": [asdict(row) for row in snapshot.sections],
                "warnings": errors,
            }
            _write_health(output, result)
            print(
                f"NHL SCHEDULE SMOKE PASSED: {snapshot.title} resolved via "
                f"{source} with {len(snapshot.sections)} sections and "
                f"{result['map_geometry_sections']} provider polygons.",
                flush=True,
            )
            return 0
    finally:
        if search_browser is not None:
            try:
                search_browser.close()
            except Exception:
                pass

    _write_health(
        output,
        {
            "status": "failure",
            "event_type": "nhl",
            "coverage_mode": "official-schedule-backed",
            "timezone": "America/New_York",
            "captured_at": eastern_iso(datetime.now(timezone.utc)),
            "scheduled_candidates": len(schedule),
            "errors": errors,
        },
    )
    raise RuntimeError("No scheduled NHL game could be resolved and captured.")


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
        default=NHL_CAPTURE_WINDOW_HOURS,
    )
    args = parser.parse_args()

    if args.command == "schedule":
        games, sources = fetch_schedule_games(
            datetime.now(timezone.utc),
            horizon_hours=args.horizon_hours,
        )
        print(
            json.dumps(
                {
                    "sources": sources,
                    "timezone": "America/New_York",
                    "count": len(games),
                    "games": [
                        {
                            **asdict(game),
                            "event_date": eastern_iso(game.event_date),
                            "game_type_label": game_type_label(game.game_type),
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
