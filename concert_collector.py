"""Collect Vivid Seats concert prices through the existing snapshot API.

This module deliberately leaves the MLB collector unchanged. It discovers
concerts at a curated set of arenas, captures section-level prices during the
final 72 hours, and sends the snapshots to the same authenticated endpoint.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from collector import (
    CAPTURE_WINDOW_HOURS,
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
DEFAULT_HEALTH_OUTPUT = PROJECT_DIR / "concert_remote_health.json"
DEFAULT_PENDING_DIR = PROJECT_DIR / "concert_pending"
DEFAULT_SMOKE_OUTPUT = PROJECT_DIR / "concert_smoke_result.json"
DISCOVERY_HORIZON_DAYS = 3
SMOKE_HORIZON_DAYS = 45
MIN_USABLE_SECTIONS = 10

# Start with four major arenas whose Vivid venue feeds have been verified.
CONCERT_VENUE_FEEDS = {
    "Madison Square Garden": "https://www.vividseats.com/madison-square-garden-tickets/venue/973",
    "Barclays Center": "https://www.vividseats.com/barclays-center-tickets/venue/9671",
    "Capital One Arena": "https://www.vividseats.com/capital-one-arena-tickets/venue/1034",
    "TD Garden": "https://www.vividseats.com/td-garden-tickets/venue/573",
}

CONCERT_ROUTE = r"--concerts-[a-z0-9-]+/production/\d+"
CONCERT_DATE_ROUTE = (
    r"-(\d{1,2})-(\d{1,2})-(\d{4})--concerts-[a-z0-9-]+/production/\d+"
)


def event_date_from_concert_url(url: str) -> datetime | None:
    """Read the Eastern calendar date encoded in a Vivid concert URL."""
    match = re.search(CONCERT_DATE_ROUTE, url, flags=re.IGNORECASE)
    if not match:
        return None
    month, day, year = map(int, match.groups())
    try:
        return datetime(year, month, day, tzinfo=NEW_YORK)
    except ValueError:
        return None


def extract_concert_event_urls(
    page_html: str,
    base_url: str = "https://www.vividseats.com",
) -> set[str]:
    """Extract canonical concert-production links from a venue page."""
    pattern = rf'''href=["']([^"']+{CONCERT_ROUTE})[^"']*["']'''
    links: set[str] = set()
    for raw in re.findall(pattern, page_html, flags=re.IGNORECASE):
        candidate = html.unescape(urljoin(base_url, raw))
        try:
            links.add(validated_vivid_url(candidate))
        except ValueError:
            continue
    return links


class ConcertSnapshotParser:
    """Convert a Vivid listings response into one lowest price per section."""

    @staticmethod
    def parse(payload: dict[str, Any]) -> EventSnapshot:
        global_rows = payload.get("global") or []
        if not global_rows or not isinstance(global_rows[0], dict):
            raise ValueError("Listings response does not contain event metadata.")

        metadata = global_rows[0]
        title = str(metadata.get("productionName") or "").strip()
        venue = str(metadata.get("mapTitle") or "").strip()
        source_id = str(metadata.get("productionId") or "").strip()
        if not title or not venue or not source_id:
            raise ValueError("Listings response is missing its title, venue, or source ID.")

        by_section: dict[str, dict[str, Any]] = {}
        for listing in payload.get("tickets") or []:
            if not isinstance(listing, dict):
                continue
            if "OBSTRUCTED_VIEW" in set(listing.get("tags") or []):
                continue

            # Concert layouts often use GA, Pit, Floor, Lawn, or Standing Room.
            # Unlike the MLB parser, section names do not need to end in a number.
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
                listing_count = current["listing_count"]
                current.update(candidate)
                current["listing_count"] = listing_count

        sections = tuple(
            SectionSnapshot(**row)
            for row in sorted(
                by_section.values(), key=lambda row: row["section"].casefold()
            )
        )
        if len(sections) < MIN_USABLE_SECTIONS:
            raise ValueError(
                f"Concert capture rejected: only {len(sections)} usable sections; "
                f"minimum is {MIN_USABLE_SECTIONS}."
            )
        return EventSnapshot(
            source_id=source_id,
            title=title,
            venue=venue,
            sections=sections,
        )


class VividConcertBrowser(VividBrowser):
    """Reuse the existing browser capture logic with concert-only discovery."""

    def discover_event_urls(self, venue_url: str) -> set[str]:
        from selenium.common.exceptions import TimeoutException

        self.driver.get("about:blank")
        try:
            self.driver.get(venue_url)
        except TimeoutException:
            self.driver.execute_script("window.stop();")

        deadline = time.monotonic() + self.timeout
        best_links: set[str] = set()
        last_new_link_at: float | None = None
        while time.monotonic() < deadline:
            links = extract_concert_event_urls(self.driver.page_source, venue_url)
            new_links = links - best_links
            if new_links:
                best_links.update(new_links)
                last_new_link_at = time.monotonic()
            elif (
                best_links
                and last_new_link_at is not None
                and time.monotonic() - last_new_link_at >= DISCOVERY_SETTLE_SECONDS
            ):
                return best_links
            time.sleep(0.5)

        if best_links:
            return best_links
        raise TimeoutError(
            f"No concert event links appeared within {self.timeout} seconds."
        )


def upcoming_concerts(
    urls: set[str], now: datetime, horizon_days: int, limit: int | None = None
) -> list[str]:
    """Return ordered concert URLs inside a rolling calendar-date window."""
    today = now.astimezone(NEW_YORK).date()
    horizon = today + timedelta(days=horizon_days)
    eligible = [
        url
        for url in urls
        if (hint := event_date_from_concert_url(url)) is not None
        and today <= hint.date() <= horizon
    ]
    eligible.sort(key=lambda url: (event_date_from_concert_url(url), url))
    return eligible if limit is None else eligible[:limit]


def concert_cycle_exit_code(
    due: int, captured: int, failures: int, discovery_failures: int
) -> int:
    """Fail only when discovery completely fails or every due capture fails."""
    all_discovery_failed = discovery_failures >= len(CONCERT_VENUE_FEEDS)
    all_due_captures_failed = due > 0 and captured == 0 and failures > 0
    return 1 if all_discovery_failed or all_due_captures_failed else 0


def discover_concerts(headless: bool, timeout: int) -> tuple[set[str], list[str]]:
    discovered: set[str] = set()
    errors: list[str] = []
    for venue, venue_url in CONCERT_VENUE_FEEDS.items():
        browser: VividConcertBrowser | None = None
        try:
            browser = VividConcertBrowser(headless=headless, timeout=timeout)
            links = browser.discover_event_urls(venue_url)
            discovered.update(links)
            print(f"{venue}: discovered {len(links)} concert event links.", flush=True)
        except Exception as exc:
            message = f"{venue}: {type(exc).__name__}: {exc}"
            errors.append(message)
            print(f"CONCERT DISCOVERY FAILED: {message}", file=sys.stderr, flush=True)
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception as exc:
                    print(
                        f"{venue} cleanup warning: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
    return discovered, errors


def run_smoke_capture(
    requested_url: str, headless: bool, timeout: int, output: Path
) -> int:
    """Capture one concert without writing anything to the production database."""
    errors: list[str] = []
    if requested_url:
        candidates = [validated_vivid_url(requested_url)]
    else:
        discovered, discovery_errors = discover_concerts(headless, timeout)
        errors.extend(discovery_errors)
        candidates = upcoming_concerts(
            discovered, datetime.now(timezone.utc), SMOKE_HORIZON_DAYS, limit=12
        )

    for url in candidates:
        browser: VividConcertBrowser | None = None
        try:
            print(f"CONCERT SMOKE CAPTURE: trying {url}", flush=True)
            browser = VividConcertBrowser(headless=headless, timeout=timeout)
            raw_payload, event_date = browser.capture(url)
            snapshot = ConcertSnapshotParser.parse(raw_payload)
            result = {
                "status": "success",
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
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            print(
                f"CONCERT SMOKE TEST PASSED: {len(snapshot.sections)} sections for "
                f"{snapshot.title} at {snapshot.venue}.",
                flush=True,
            )
            return 0
        except Exception as exc:
            message = f"{url}: {type(exc).__name__}: {exc}"
            errors.append(message)
            print(f"CONCERT SMOKE CAPTURE FAILED: {message}", file=sys.stderr, flush=True)
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
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "candidate_count": len(candidates),
                "errors": errors,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    raise RuntimeError(f"No concert could be captured from {len(candidates)} candidates.")


def run_remote_collector(
    endpoint: str,
    token: str,
    headless: bool,
    timeout: int,
    health_output: Path,
    pending_dir: Path,
) -> int:
    """Discover, capture, and upload concerts inside the final 72 hours."""
    started_at = datetime.now(timezone.utc)
    replayed, endpoint_available, queue_errors = replay_pending_snapshots(
        endpoint, token, pending_dir
    )
    discovered, discovery_errors = discover_concerts(headless, timeout)
    due_urls = upcoming_concerts(
        discovered, started_at, DISCOVERY_HORIZON_DAYS
    )
    print(
        f"{len(due_urls)} candidate concerts fall within the rolling date window.",
        flush=True,
    )

    capture_slot = started_at.replace(
        minute=0 if started_at.minute < 30 else 30,
        second=0,
        microsecond=0,
    )
    uploaded = 0
    captured = 0
    queued = 0
    skipped = 0
    capture_errors: list[str] = []
    uploads: list[dict[str, Any]] = []

    for index, url in enumerate(due_urls, start=1):
        browser: VividConcertBrowser | None = None
        try:
            print(f"[{index}/{len(due_urls)}] Capturing concert {url}", flush=True)
            browser = VividConcertBrowser(headless=headless, timeout=timeout)
            raw_payload, event_date = browser.capture(url)
            hours_until = (
                as_utc(event_date) - datetime.now(timezone.utc)
            ).total_seconds() / 3600
            if hours_until <= 0:
                skipped += 1
                print(f"SKIP: concert has started: {url}", flush=True)
                continue
            if hours_until > CAPTURE_WINDOW_HOURS:
                skipped += 1
                print(f"SKIP: concert is outside the exact 72-hour window: {url}", flush=True)
                continue

            snapshot = ConcertSnapshotParser.parse(raw_payload)
            payload = snapshot_to_payload(url, event_date, capture_slot, snapshot)
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
                    uploaded += 1
                    uploads.append(
                        {
                            "url": url,
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
            message = f"{url}: {type(exc).__name__}: {exc}"
            capture_errors.append(message)
            print(f"CONCERT CAPTURE FAILED: {message}", file=sys.stderr, flush=True)
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
        "started_at": started_at.isoformat(),
        "capture_slot": capture_slot.isoformat(),
        "discovered": len(discovered),
        "due": len(due_urls),
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
        f"Concert collection finished: {uploaded} uploaded, {queued} queued, "
        f"{replayed} replayed, {skipped} skipped, "
        f"{len(capture_errors)} capture failures, "
        f"{len(discovery_errors)} discovery failures.",
        flush=True,
    )
    return concert_cycle_exit_code(
        due=len(due_urls),
        captured=captured,
        failures=len(capture_errors),
        discovery_failures=len(discovery_errors),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("remote-run", "smoke", "venues"))
    parser.add_argument("event_url", nargs="?", default="")
    parser.add_argument("--endpoint")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--health-output", type=Path, default=DEFAULT_HEALTH_OUTPUT)
    parser.add_argument("--pending-dir", type=Path, default=DEFAULT_PENDING_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_SMOKE_OUTPUT)
    args = parser.parse_args()

    if args.command == "venues":
        print(json.dumps(CONCERT_VENUE_FEEDS, indent=2))
        return 0
    if args.command == "smoke":
        return run_smoke_capture(
            args.event_url, args.headless, args.timeout, args.output
        )

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
