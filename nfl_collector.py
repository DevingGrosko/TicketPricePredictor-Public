"""Collect Vivid Seats NFL ticket prices into the separate NFL pipeline.

NFL games are discovered from Vivid's league feed, tracked during the final
seven days before kickoff, sampled once per UTC hour, and uploaded to the
NFL-only API and database.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
import html
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.parse import urljoin

from collector import (
    DISCOVERY_SETTLE_SECONDS,
    EventSnapshot,
    NEW_YORK,
    SectionSnapshot,
    VividBrowser,
    as_utc,
    post_snapshot_with_retry,
    queue_snapshot,
    replay_pending_snapshots,
    snapshot_to_payload,
    validated_vivid_url,
)


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_HEALTH_OUTPUT = PROJECT_DIR / "nfl_remote_health.json"
DEFAULT_PENDING_DIR = PROJECT_DIR / "nfl_pending"
DEFAULT_SMOKE_OUTPUT = PROJECT_DIR / "nfl_smoke_result.json"
NFL_CAPTURE_WINDOW_HOURS = 7 * 24
DISCOVERY_HORIZON_DAYS = 7
SMOKE_HORIZON_DAYS = 45
MIN_USABLE_SECTIONS = 10
NFL_FEED_URL = "https://www.vividseats.com/nfl/"

NFL_TEAM_NAMES = frozenset(
    {
        "Arizona Cardinals",
        "Atlanta Falcons",
        "Baltimore Ravens",
        "Buffalo Bills",
        "Carolina Panthers",
        "Chicago Bears",
        "Cincinnati Bengals",
        "Cleveland Browns",
        "Dallas Cowboys",
        "Denver Broncos",
        "Detroit Lions",
        "Green Bay Packers",
        "Houston Texans",
        "Indianapolis Colts",
        "Jacksonville Jaguars",
        "Kansas City Chiefs",
        "Las Vegas Raiders",
        "Los Angeles Chargers",
        "Los Angeles Rams",
        "Miami Dolphins",
        "Minnesota Vikings",
        "New England Patriots",
        "New Orleans Saints",
        "New York Giants",
        "New York Jets",
        "Philadelphia Eagles",
        "Pittsburgh Steelers",
        "San Francisco 49ers",
        "Seattle Seahawks",
        "Tampa Bay Buccaneers",
        "Tennessee Titans",
        "Washington Commanders",
    }
)

NON_GAME_MARKERS = (
    "parking",
    "tailgate",
    "season ticket",
    "season tickets",
    "training camp",
    "fan fest",
    "fan experience",
    "hospitality",
    "ticket package",
    "travel package",
    "vip package",
    "club access",
    "shuttle",
)

MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


@dataclass(frozen=True)
class DiscoveredNFLGame:
    url: str
    title: str
    date_hint: date | None


def is_nfl_game_title(title: str) -> bool:
    normalized = " ".join(str(title or "").split()).casefold()
    if not normalized or any(marker in normalized for marker in NON_GAME_MARKERS):
        return False
    team_count = sum(team.casefold() in normalized for team in NFL_TEAM_NAMES)
    separator = any(
        value in normalized for value in (" at ", " vs ", " vs. ", " versus ")
    )
    return team_count >= 2 and separator


def _infer_year(month: int, day: int, now: datetime) -> int:
    local_today = now.astimezone(NEW_YORK).date()
    candidate = date(local_today.year, month, day)
    if candidate < local_today - timedelta(days=14):
        return local_today.year + 1
    return local_today.year


def date_hint_from_text(value: str, now: datetime) -> date | None:
    text = " ".join(html.unescape(str(value or "")).split())
    month_names = "|".join(sorted(MONTHS, key=len, reverse=True))
    named = re.search(
        rf"\b({month_names})\.?\s+(\d{{1,2}})(?:,?\s+(\d{{4}}))?\b",
        text,
        flags=re.IGNORECASE,
    )
    if named:
        month = MONTHS[named.group(1).casefold()]
        day = int(named.group(2))
        year = int(named.group(3)) if named.group(3) else _infer_year(month, day, now)
        try:
            return date(year, month, day)
        except ValueError:
            return None

    numeric = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", text)
    if numeric:
        month, day = int(numeric.group(1)), int(numeric.group(2))
        raw_year = numeric.group(3)
        if raw_year:
            year = int(raw_year)
            if year < 100:
                year += 2000
        else:
            year = _infer_year(month, day, now)
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def date_hint_from_url(url: str) -> date | None:
    match = re.search(r"-(\d{1,2})-(\d{1,2})-(\d{4})(?:--|/|$)", url)
    if not match:
        return None
    month, day, year = map(int, match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


class _NFLAnchorParser(HTMLParser):
    def __init__(self, base_url: str, now: datetime):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.now = now
        self._href: str | None = None
        self._text: list[str] = []
        self.games: list[DiscoveredNFLGame] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attributes = dict(attrs)
        if self._href is not None:
            return
        if tag.casefold() != "a":
            return
        href = str(attributes.get("href") or "")
        if "/production/" not in href:
            return
        self._href = href
        self._text = [
            str(attributes.get("aria-label") or ""),
            str(attributes.get("title") or ""),
        ]

    def handle_data(self, data: str):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str):
        if self._href is None or tag.casefold() != "a":
            return

        raw_href = self._href
        title = " ".join(" ".join(self._text).split())
        self._href = None
        self._text = []

        if not is_nfl_game_title(title):
            return
        try:
            url = validated_vivid_url(html.unescape(urljoin(self.base_url, raw_href)))
        except ValueError:
            return
        self.games.append(
            DiscoveredNFLGame(
                url=url,
                title=title,
                date_hint=date_hint_from_url(url) or date_hint_from_text(title, self.now),
            )
        )


def extract_nfl_game_rows(
    page_html: str,
    base_url: str = NFL_FEED_URL,
    now: datetime | None = None,
) -> list[DiscoveredNFLGame]:
    parser = _NFLAnchorParser(base_url, now or datetime.now(timezone.utc))
    parser.feed(page_html)

    by_url: dict[str, DiscoveredNFLGame] = {}
    for row in parser.games:
        current = by_url.get(row.url)
        if current is None:
            by_url[row.url] = row
            continue
        better_date = current.date_hint is None and row.date_hint is not None
        better_title = len(row.title) > len(current.title)
        if better_date or better_title:
            by_url[row.url] = DiscoveredNFLGame(
                url=row.url,
                title=row.title if better_title else current.title,
                date_hint=row.date_hint or current.date_hint,
            )
    return sorted(
        by_url.values(),
        key=lambda row: (row.date_hint or date.max, row.title.casefold(), row.url),
    )


class NFLSnapshotParser:
    """Convert listings JSON into the lowest displayed price per stadium section."""

    @staticmethod
    def parse(payload: dict[str, Any]) -> EventSnapshot:
        global_rows = payload.get("global") or []
        if not global_rows or not isinstance(global_rows[0], dict):
            raise ValueError("Listings response does not contain event metadata.")

        metadata = global_rows[0]
        title = " ".join(str(metadata.get("productionName") or "").split())
        venue = " ".join(str(metadata.get("mapTitle") or "").split())
        source_id = str(metadata.get("productionId") or "").strip()
        if not title or not venue or not source_id:
            raise ValueError("Listings response is missing its title, venue, or source ID.")
        if not is_nfl_game_title(title):
            raise ValueError(f"NFL capture rejected non-game event: {title}")

        by_section: dict[str, dict[str, Any]] = {}
        for listing in payload.get("tickets") or []:
            if not isinstance(listing, dict):
                continue
            tags = set(listing.get("tags") or [])
            if "OBSTRUCTED_VIEW" in tags or "STANDING_ROOM_ONLY" in tags:
                continue

            section = " ".join(str(listing.get("l") or "").split())
            if not section:
                continue
            raw_price = listing.get("p")
            if raw_price in (None, ""):
                continue
            try:
                price = int(
                    Decimal(str(raw_price)).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                )
            except (InvalidOperation, TypeError, ValueError):
                continue

            normalized = section.casefold()
            candidate = {
                "section": section,
                "price": price,
                "listing_count": 1,
                "row": str(listing.get("r") or ""),
                "quantity": str(listing.get("q") or ""),
                "displayed_price": str(raw_price),
                "alternate_price": str(listing.get("aip") or ""),
                "price_source": "p",
            }
            current = by_section.get(normalized)
            if current is None:
                by_section[normalized] = candidate
                continue
            current["listing_count"] += 1
            if price < current["price"]:
                count = current["listing_count"]
                current.update(candidate)
                current["listing_count"] = count

        sections = tuple(
            SectionSnapshot(**row)
            for row in sorted(
                by_section.values(), key=lambda row: row["section"].casefold()
            )
        )
        if len(sections) < MIN_USABLE_SECTIONS:
            raise ValueError(
                f"NFL capture rejected: only {len(sections)} usable sections; "
                f"minimum is {MIN_USABLE_SECTIONS}."
            )
        return EventSnapshot(
            source_id=source_id,
            title=title,
            venue=venue,
            sections=sections,
        )


class VividNFLBrowser(VividBrowser):
    def discover_games(self, feed_url: str = NFL_FEED_URL) -> list[DiscoveredNFLGame]:
        from selenium.common.exceptions import TimeoutException

        self.driver.get("about:blank")
        try:
            self.driver.get(feed_url)
        except TimeoutException:
            self.driver.execute_script("window.stop();")

        deadline = time.monotonic() + self.timeout
        started_at = time.monotonic()
        last_scroll_at = 0.0
        best: dict[str, DiscoveredNFLGame] = {}
        last_new_at: float | None = None
        while time.monotonic() < deadline:
            now_monotonic = time.monotonic()
            if now_monotonic - last_scroll_at >= 1.0:
                try:
                    self.driver.execute_script(
                        "window.scrollTo(0, document.body.scrollHeight);"
                    )
                except Exception:
                    pass
                last_scroll_at = now_monotonic

            rows = extract_nfl_game_rows(
                self.driver.page_source,
                feed_url,
                datetime.now(timezone.utc),
            )
            previous_count = len(best)
            for row in rows:
                best[row.url] = row
            if len(best) > previous_count:
                last_new_at = time.monotonic()
            elif best and last_new_at is not None:
                local_today = (
                    datetime.now(timezone.utc).astimezone(NEW_YORK).date()
                )
                has_future_game = any(
                    row.date_hint is not None and row.date_hint >= local_today
                    for row in best.values()
                )
                if (
                    has_future_game
                    and time.monotonic() - last_new_at
                    >= max(4.0, DISCOVERY_SETTLE_SECONDS)
                    and time.monotonic() - started_at >= 6.0
                ):
                    return sorted(
                        best.values(),
                        key=lambda row: (
                            row.date_hint or date.max,
                            row.title.casefold(),
                            row.url,
                        ),
                    )
            time.sleep(0.5)

        if best:
            return sorted(
                best.values(),
                key=lambda row: (row.date_hint or date.max, row.title.casefold(), row.url),
            )
        raise TimeoutError(f"No NFL game links appeared within {self.timeout} seconds.")


def upcoming_nfl_games(
    games: list[DiscoveredNFLGame],
    now: datetime,
    horizon_days: int = DISCOVERY_HORIZON_DAYS,
    limit: int | None = None,
) -> list[DiscoveredNFLGame]:
    today = now.astimezone(NEW_YORK).date()
    horizon = today + timedelta(days=horizon_days)
    eligible = [
        game
        for game in games
        if game.date_hint is not None and today <= game.date_hint <= horizon
    ]
    eligible.sort(key=lambda row: (row.date_hint, row.title.casefold(), row.url))
    return eligible if limit is None else eligible[:limit]


def hourly_capture_slot(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def nfl_is_within_capture_window(event_date: datetime, now: datetime) -> bool:
    hours_until = (as_utc(event_date) - now.astimezone(timezone.utc)).total_seconds() / 3600
    return 0 < hours_until <= NFL_CAPTURE_WINDOW_HOURS


def nfl_snapshot_to_payload(
    url: str,
    event_date: datetime,
    captured_at: datetime,
    snapshot: EventSnapshot,
) -> dict[str, Any]:
    payload = snapshot_to_payload(url, event_date, captured_at, snapshot)
    payload["event_type"] = "nfl"
    return payload


def discover_nfl_games(headless: bool, timeout: int) -> tuple[list[DiscoveredNFLGame], list[str]]:
    browser: VividNFLBrowser | None = None
    try:
        browser = VividNFLBrowser(headless=headless, timeout=timeout)
        games = browser.discover_games(NFL_FEED_URL)
        print(f"NFL feed: discovered {len(games)} game links.", flush=True)
        for game in games:
            print(
                f"NFL DISCOVERED: {game.date_hint or 'unknown'} | "
                f"{game.title} | {game.url}",
                flush=True,
            )
        return games, []
    except Exception as exc:
        message = f"NFL feed: {type(exc).__name__}: {exc}"
        print(f"NFL DISCOVERY FAILED: {message}", file=sys.stderr, flush=True)
        return [], [message]
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception as exc:
                print(
                    f"NFL discovery cleanup warning: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )


def nfl_cycle_exit_code(
    discovered: int,
    due: int,
    captured: int,
    failures: int,
    discovery_failures: int,
) -> int:
    if discovered == 0 and discovery_failures:
        return 1
    if due > 0 and captured == 0 and failures > 0:
        return 1
    return 0


def run_smoke_capture(
    requested_url: str,
    headless: bool,
    timeout: int,
    output: Path,
) -> int:
    errors: list[str] = []
    if requested_url:
        candidates = [
            DiscoveredNFLGame(
                url=validated_vivid_url(requested_url),
                title="Requested NFL game",
                date_hint=None,
            )
        ]
    else:
        discovered, discovery_errors = discover_nfl_games(headless, timeout)
        errors.extend(discovery_errors)
        candidates = upcoming_nfl_games(
            discovered,
            datetime.now(timezone.utc),
            horizon_days=SMOKE_HORIZON_DAYS,
            limit=12,
        )

    for game in candidates:
        browser: VividNFLBrowser | None = None
        try:
            print(f"NFL SMOKE CAPTURE: trying {game.url}", flush=True)
            browser = VividNFLBrowser(headless=headless, timeout=timeout)
            raw_payload, event_date = browser.capture(game.url)
            captured_at = datetime.now(timezone.utc)
            if as_utc(event_date) <= captured_at:
                raise ValueError(
                    "NFL smoke candidate has already started; trying the next game."
                )
            if not requested_url and (
                as_utc(event_date) - captured_at
                > timedelta(days=SMOKE_HORIZON_DAYS, hours=1)
            ):
                raise ValueError(
                    "NFL smoke candidate is outside the automatic 45-day horizon."
                )
            snapshot = NFLSnapshotParser.parse(raw_payload)
            result = {
                "status": "success",
                "event_type": "nfl",
                "captured_at": captured_at.isoformat(),
                "event_date": event_date.isoformat(),
                "source_url": game.url,
                "source_id": snapshot.source_id,
                "title": snapshot.title,
                "venue": snapshot.venue,
                "section_count": len(snapshot.sections),
                "lowest_section_price": min(row.price for row in snapshot.sections),
                "highest_section_price": max(row.price for row in snapshot.sections),
                "sections": [asdict(row) for row in snapshot.sections],
            }
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            print(
                f"NFL SMOKE TEST PASSED: {len(snapshot.sections)} sections for "
                f"{snapshot.title} at {snapshot.venue}.",
                flush=True,
            )
            return 0
        except Exception as exc:
            message = f"{game.url}: {type(exc).__name__}: {exc}"
            errors.append(message)
            print(f"NFL SMOKE CAPTURE FAILED: {message}", file=sys.stderr, flush=True)
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "status": "failure",
                "event_type": "nfl",
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "candidate_count": len(candidates),
                "errors": errors,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    raise RuntimeError(f"No NFL game could be captured from {len(candidates)} candidates.")


def run_remote_collector(
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
    discovered, discovery_errors = discover_nfl_games(headless, timeout)
    due_games = upcoming_nfl_games(discovered, started_at)
    undated = sum(game.date_hint is None for game in discovered)
    print(
        f"{len(due_games)} NFL games fall within the seven-day date window "
        f"({undated} discovered links had no date hint).",
        flush=True,
    )

    capture_slot = hourly_capture_slot(started_at)
    uploaded = 0
    captured = 0
    queued = 0
    skipped = 0
    capture_errors: list[str] = []
    uploads: list[dict[str, Any]] = []

    for index, game in enumerate(due_games, start=1):
        browser: VividNFLBrowser | None = None
        try:
            print(f"[{index}/{len(due_games)}] Capturing NFL game {game.url}", flush=True)
            browser = VividNFLBrowser(headless=headless, timeout=timeout)
            raw_payload, event_date = browser.capture(game.url)
            if not nfl_is_within_capture_window(event_date, datetime.now(timezone.utc)):
                skipped += 1
                print(
                    f"SKIP: NFL game is outside the exact seven-day window: {game.url}",
                    flush=True,
                )
                continue

            snapshot = NFLSnapshotParser.parse(raw_payload)
            payload = nfl_snapshot_to_payload(
                game.url, event_date, capture_slot, snapshot
            )
            pending_path = queue_snapshot(payload, pending_dir)
            captured += 1

            if endpoint_available:
                try:
                    response = post_snapshot_with_retry(endpoint, token, payload)
                except Exception as exc:
                    endpoint_available = False
                    queued += 1
                    message = (
                        f"Snapshot retained as {pending_path.name}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    queue_errors.append(message)
                    print(f"QUEUED: {message}", file=sys.stderr, flush=True)
                else:
                    pending_path.unlink()
                    uploaded += response["status"] == "stored"
                    uploads.append(
                        {
                            "url": game.url,
                            "title": snapshot.title,
                            "venue": snapshot.venue,
                            "sections": len(snapshot.sections),
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
            message = f"{game.url}: {type(exc).__name__}: {exc}"
            capture_errors.append(message)
            print(f"NFL CAPTURE FAILED: {message}", file=sys.stderr, flush=True)
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass

    pending_count = len(list(pending_dir.glob("*.json")))
    if capture_errors or discovery_errors:
        status = "degraded"
    elif pending_count:
        status = "queued"
    else:
        status = "healthy"

    report = {
        "status": status,
        "event_type": "nfl",
        "storage": "separate NFL database",
        "cadence": "hourly",
        "capture_window_hours": NFL_CAPTURE_WINDOW_HOURS,
        "started_at": started_at.isoformat(),
        "capture_slot": capture_slot.isoformat(),
        "feed": NFL_FEED_URL,
        "discovered": len(discovered),
        "undated": undated,
        "due": len(due_games),
        "uploaded": uploaded,
        "captured": captured,
        "queued": queued,
        "replayed": replayed,
        "pending": pending_count,
        "skipped": skipped,
        "failed": len(capture_errors),
        "discovery_failed": len(discovery_errors),
        "uploads": uploads,
        "errors": discovery_errors + capture_errors + queue_errors,
    }
    health_output.parent.mkdir(parents=True, exist_ok=True)
    health_output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        f"NFL collection finished: {uploaded} stored, {queued} queued, "
        f"{replayed} replayed, {skipped} skipped, "
        f"{len(capture_errors)} capture failures, "
        f"{len(discovery_errors)} discovery failures.",
        flush=True,
    )
    return nfl_cycle_exit_code(
        discovered=len(discovered),
        due=len(due_games),
        captured=captured,
        failures=len(capture_errors),
        discovery_failures=len(discovery_errors),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("remote-run", "smoke", "feed"))
    parser.add_argument("event_url", nargs="?", default="")
    parser.add_argument("--endpoint")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--health-output", type=Path, default=DEFAULT_HEALTH_OUTPUT)
    parser.add_argument("--pending-dir", type=Path, default=DEFAULT_PENDING_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_SMOKE_OUTPUT)
    args = parser.parse_args()

    if args.command == "feed":
        print(NFL_FEED_URL)
        return 0
    if args.command == "smoke":
        return run_smoke_capture(args.event_url, args.headless, args.timeout, args.output)

    if not args.endpoint:
        parser.error("--endpoint is required for remote-run")
    token = os.getenv("COLLECTOR_INGEST_TOKEN", "").strip()
    if not token:
        print("COLLECTOR_INGEST_TOKEN is not configured.", file=sys.stderr)
        return 2
    return run_remote_collector(
        args.endpoint,
        token,
        args.headless,
        args.timeout,
        args.health_output,
        args.pending_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
