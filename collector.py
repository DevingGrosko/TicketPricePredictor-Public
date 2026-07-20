"""Fast, link-driven Vivid Seats snapshot collector.

Typical use:
    python collector.py add <vivid-url> [<vivid-url> ...]
    python collector.py run
    python collector.py status
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

from sqlalchemy import func

from models import CreateModel, Event, Iteration, Ticket, clean_event_title


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_REGISTRY = PROJECT_DIR / "collector_events.json"
DEFAULT_AUDIT_DIR = PROJECT_DIR / "collector_audit"
DEFAULT_BACKUP_DIR = PROJECT_DIR / "collector_backups"
DEFAULT_HEALTH_FILE = PROJECT_DIR / "collector_health.json"
DEFAULT_STATE_FILE = PROJECT_DIR / "collector_state.json"
DEFAULT_CAPTURE_TIMEOUT = 25
CAPTURE_WINDOW_DAYS = 4
DISCOVERY_WINDOW_DAYS = 30
DISCOVERY_INTERVAL = timedelta(days=1)
MIN_USABLE_SECTIONS = 10
AUDIT_RETENTION_DAYS = 30
BACKUP_RETENTION_DAYS = 7
MAX_CAPTURE_FAILURES_PER_CYCLE = 2
MAX_CONSECUTIVE_FAILED_CYCLES = 2
FAILURE_COOLDOWN = timedelta(hours=6)
CPU_USAGE_STOP_FRACTION = Decimal("0.95")
CPU_MINIMUM_HEADROOM_SECONDS = Decimal("250")
CPU_API_TIMEOUT_SECONDS = 5
NEW_YORK = ZoneInfo("America/New_York")
VENUE_FEEDS = {
    "Nationals Park": "https://www.vividseats.com/nationals-park-tickets/venue/5597",
    "Citizens Bank Park": "https://www.vividseats.com/citizens-bank-park-tickets/venue/3125",
    "Yankee Stadium": "https://www.vividseats.com/yankee-stadium-tickets/venue/6135",
    "Fenway Park": "https://www.vividseats.com/fenway-park-tickets/venue/551",
    "Oriole Park at Camden Yards": "https://www.vividseats.com/camden-yards-tickets/venue/261",
}
EXCLUDED_VENUES = {
    "citi field",
    "george m. steinbrenner field",
    "steinbrenner field",
    "truist park",
}
EXCLUDED_URL_MARKERS = {
    "citi-field",
    "george-m-steinbrenner-field",
    "steinbrenner-field",
    "truist-park",
}


@dataclass(frozen=True)
class SectionSnapshot:
    section: str
    price: int
    listing_count: int
    row: str
    quantity: str
    displayed_price: str
    alternate_price: str
    price_source: str = "p"


@dataclass(frozen=True)
class EventSnapshot:
    source_id: str
    title: str
    venue: str
    sections: tuple[SectionSnapshot, ...]


class SnapshotParser:
    """Convert one Vivid listings response into one row per section."""

    @staticmethod
    def parse(payload: dict[str, Any]) -> EventSnapshot:
        global_rows = payload.get("global") or []
        if not global_rows or not isinstance(global_rows[0], dict):
            raise ValueError("Listings response does not contain event metadata.")

        metadata = global_rows[0]
        title = clean_event_title(str(metadata.get("productionName") or ""))
        venue = str(metadata.get("mapTitle") or "").strip()
        source_id = str(metadata.get("productionId") or "").strip()
        if not title or not venue:
            raise ValueError("Listings response is missing the event title or venue.")

        by_section: dict[str, dict[str, Any]] = {}
        for listing in payload.get("tickets") or []:
            if not isinstance(listing, dict):
                continue
            tags = set(listing.get("tags") or [])
            if "OBSTRUCTED_VIEW" in tags or "STANDING_ROOM_ONLY" in tags:
                continue

            section = str(listing.get("l") or "").strip()
            if not section or not section.split()[-1].isdigit():
                continue

            # `p` is the per-ticket USD price shown in Vivid's listings UI.
            # PythonAnywhere also receives `aip`, but that alternate value is
            # not the price Vivid displays and can be substantially higher.
            # Missing displayed prices are skipped instead of silently mixing
            # two different price definitions in the historical series.
            raw_price = listing.get("p")
            if raw_price in (None, ""):
                continue
            try:
                price = int(Decimal(str(raw_price)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            except (InvalidOperation, TypeError, ValueError):
                continue

            normalized = " ".join(section.lower().split())
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
            for row in sorted(by_section.values(), key=lambda row: row["section"].lower())
        )
        if not sections:
            raise ValueError("Listings response did not contain usable section prices.")
        if len(sections) < MIN_USABLE_SECTIONS:
            raise ValueError(
                f"Capture rejected: only {len(sections)} usable sections; "
                f"minimum is {MIN_USABLE_SECTIONS}."
            )

        return EventSnapshot(
            source_id=source_id,
            title=title,
            venue=venue,
            sections=sections,
        )


class VividBrowser:
    """Capture Vivid's listings JSON through a normal Chrome session."""

    def __init__(self, headless: bool = False, timeout: int = DEFAULT_CAPTURE_TIMEOUT):
        try:
            from selenium import webdriver
            from selenium.webdriver.remote.remote_connection import RemoteConnection
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Selenium is not installed. Run: pip install -r requirements.txt"
            ) from exc

        self.timeout = timeout
        # Selenium otherwise waits up to 120 seconds for a frozen local
        # ChromeDriver command, which caused failed cycles to consume hours of
        # wall time.  Keep the transport timeout close to our capture timeout.
        try:
            RemoteConnection.set_timeout(timeout + 5)
        except AttributeError:
            # Selenium 4.26+ no longer initializes the class-level client
            # configuration until ChromeDriver is constructed.  Calling the
            # deprecated setter before then raises instead of setting a
            # timeout.  The failure guards below still bound the cycle, and
            # requirements.txt pins the release that supports this setter.
            pass
        options = webdriver.ChromeOptions()
        profile_root = os.environ.get("VIVID_CHROME_PROFILE")
        if profile_root:
            options.add_argument(f"--user-data-dir={profile_root}")
            options.add_argument("--profile-directory=Default")
        elif sys.platform == "darwin":
            options.add_argument(
                f"--user-data-dir={Path.home() / 'Library/Application Support/Google/ChromeSelenium'}"
            )
            options.add_argument("--profile-directory=Default")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-notifications")
        options.add_argument("--no-first-run")
        options.add_argument("--window-size=1400,1000")
        options.page_load_strategy = "none"
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
        options.add_experimental_option(
            "prefs",
            {
                "profile.managed_default_content_settings.images": 2,
                "profile.default_content_setting_values.notifications": 2,
            },
        )
        if headless:
            options.add_argument("--headless" if sys.platform.startswith("linux") else "--headless=new")

        driver_path = os.environ.get("CHROMEDRIVER_PATH")
        if not driver_path and Path("/usr/local/bin/chromedriver").exists():
            driver_path = "/usr/local/bin/chromedriver"

        if driver_path:
            from selenium.webdriver.chrome.service import Service

            self.driver = webdriver.Chrome(service=Service(driver_path), options=options)
        else:
            self.driver = webdriver.Chrome(options=options)
        self.driver.set_page_load_timeout(timeout)
        self.driver.execute_cdp_cmd("Network.enable", {})
        self.driver.execute_cdp_cmd(
            "Network.setBlockedURLs",
            {
                "urls": [
                    "*.jpg", "*.jpeg", "*.png", "*.gif", "*.webp", "*.svg",
                    "*.woff", "*.woff2", "*.ttf", "*doubleclick.net*",
                    "*google-analytics.com*", "*googletagmanager.com*",
                ]
            },
        )

    def close(self) -> None:
        self.driver.quit()

    def capture(self, url: str) -> tuple[dict[str, Any], datetime]:
        from selenium.common.exceptions import TimeoutException

        self.driver.get_log("performance")
        try:
            self.driver.get(url)
        except TimeoutException:
            self.driver.execute_script("window.stop();")

        deadline = time.monotonic() + self.timeout
        candidate_ids: set[str] = set()
        event_date: datetime | None = None
        captured_payload: dict[str, Any] | None = None

        while time.monotonic() < deadline:
            if event_date is None:
                try:
                    event_date = self._event_datetime(url)
                except ValueError:
                    # page_load_strategy="none" returns before Vivid has
                    # necessarily rendered its event metadata. Keep polling
                    # while the listings request is loading.
                    pass

            for entry in self.driver.get_log("performance"):
                try:
                    message = json.loads(entry["message"])["message"]
                    method = message["method"]
                    params = message["params"]
                except (KeyError, TypeError, json.JSONDecodeError):
                    continue

                if method == "Network.responseReceived":
                    response_url = str(params.get("response", {}).get("url", "")).lower()
                    if "listings" in response_url:
                        candidate_ids.add(params.get("requestId", ""))

                if method == "Network.loadingFinished" and params.get("requestId") in candidate_ids:
                    payload = self._response_json(params["requestId"])
                    if payload and payload.get("tickets") and payload.get("global"):
                        captured_payload = payload

            if captured_payload is not None and event_date is not None:
                return captured_payload, event_date

            time.sleep(0.15)

        if captured_payload is not None:
            raise ValueError(
                f"Listings loaded, but the event date and time did not appear "
                f"within {self.timeout} seconds."
            )
        raise TimeoutError(f"No Vivid listings response appeared within {self.timeout} seconds.")

    def discover_event_urls(self, venue_url: str) -> set[str]:
        """Load a Vivid venue page and return its MLB event links."""
        from selenium.common.exceptions import TimeoutException

        try:
            self.driver.get(venue_url)
        except TimeoutException:
            self.driver.execute_script("window.stop();")

        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            links = extract_mlb_event_urls(self.driver.page_source, venue_url)
            if links:
                return links
            time.sleep(0.5)
        raise TimeoutError(f"No MLB event links appeared within {self.timeout} seconds.")

    def _response_json(self, request_id: str) -> dict[str, Any] | None:
        try:
            result = self.driver.execute_cdp_cmd(
                "Network.getResponseBody", {"requestId": request_id}
            )
            body = result.get("body", "")
            if result.get("base64Encoded"):
                body = base64.b64decode(body).decode("utf-8")
            value = json.loads(body)
            return value if isinstance(value, dict) else None
        except Exception:
            return None

    def _event_datetime(self, url: str) -> datetime:
        scripts = self.driver.find_elements("css selector", 'script[type="application/ld+json"]')
        for script in scripts:
            try:
                data = json.loads(script.get_attribute("textContent") or "")
            except json.JSONDecodeError:
                continue
            start_date = find_nested_value(data, "startDate")
            if start_date:
                parsed = parse_iso_datetime(str(start_date))
                if parsed:
                    return parsed

        selectors = ["time[datetime]", '[itemprop="startDate"]', 'meta[property="event:start_time"]']
        for selector in selectors:
            for element in self.driver.find_elements("css selector", selector):
                raw = element.get_attribute("datetime") or element.get_attribute("content")
                parsed = parse_iso_datetime(raw or "")
                if parsed:
                    return parsed

        date_match = re.search(r"-(\d{1,2})-(\d{1,2})-(\d{4})--", url)
        bodies = self.driver.find_elements("tag name", "body")
        page_text = bodies[0].text if bodies else ""
        time_match = re.search(r"\b(\d{1,2}:\d{2}\s*[ap]m)\b", page_text, re.IGNORECASE)
        if date_match and time_match:
            month, day, year = map(int, date_match.groups())
            clock = datetime.strptime(time_match.group(1).replace(" ", "").upper(), "%I:%M%p")
            return datetime(year, month, day, clock.hour, clock.minute, tzinfo=ZoneInfo("America/New_York"))

        raise ValueError("Could not determine the event date and time from the Vivid page.")


def find_nested_value(value: Any, key: str) -> Any | None:
    if isinstance(value, dict):
        if value.get(key):
            return value[key]
        for child in value.values():
            found = find_nested_value(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_nested_value(child, key)
            if found is not None:
                return found
    return None


def parse_iso_datetime(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("America/New_York"))
    return parsed


def validated_vivid_url(raw: str) -> str:
    value = raw.strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in {"vividseats.com", "www.vividseats.com"}:
        raise ValueError(f"Not a Vivid Seats HTTPS URL: {value}")
    if "/production/" not in parsed.path:
        raise ValueError(f"Vivid event URL is missing a production ID: {value}")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def event_date_from_url(url: str) -> datetime | None:
    match = re.search(r"-(\d{1,2})-(\d{1,2})-(\d{4})--sports-mlb-baseball/production/", url)
    if not match:
        return None
    month, day, year = map(int, match.groups())
    try:
        return datetime(year, month, day, tzinfo=NEW_YORK)
    except ValueError:
        return None


def registry_row_is_excluded(row: dict[str, Any]) -> bool:
    venue = " ".join(str(row.get("venue") or "").lower().split())
    if venue in EXCLUDED_VENUES:
        return True
    url = str(row.get("url") or "").lower()
    return any(marker in url for marker in EXCLUDED_URL_MARKERS)


def extract_mlb_event_urls(page_html: str, base_url: str = "https://www.vividseats.com") -> set[str]:
    links: set[str] = set()
    pattern = r'''href=["']([^"']+--sports-mlb-baseball/production/\d+)[^"']*["']'''
    for raw in re.findall(pattern, page_html, flags=re.IGNORECASE):
        candidate = html.unescape(urljoin(base_url, raw))
        try:
            links.add(validated_vivid_url(candidate))
        except ValueError:
            continue
    return links


def load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"events": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ValueError(f"Invalid collector registry: {path}")
    return data


def save_registry(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def add_urls(urls: list[str], registry_path: Path) -> int:
    registry = load_registry(registry_path)
    existing = {row["url"] for row in registry["events"]}
    added = 0
    for raw in urls:
        url = validated_vivid_url(raw)
        if registry_row_is_excluded({"url": url}):
            print(f"Excluded venue; not registered: {url}")
            continue
        if url in existing:
            print(f"Already registered: {url}")
            continue
        registry["events"].append(
            {"url": url, "active": True, "added_at": datetime.now(timezone.utc).isoformat()}
        )
        existing.add(url)
        added += 1
        print(f"Added: {url}")
    save_registry(registry_path, registry)
    return added


def discovery_due(registry: dict[str, Any], now: datetime) -> bool:
    last_raw = registry.get("last_discovery_at")
    last = parse_iso_datetime(str(last_raw or ""))
    return last is None or now - as_utc(last) >= DISCOVERY_INTERVAL


def discover_events(registry_path: Path, headless: bool, timeout: int) -> tuple[int, int]:
    """Refresh the registry from each venue page for the rolling 30-day window."""
    registry = load_registry(registry_path)
    now = datetime.now(timezone.utc)
    today = now.astimezone(NEW_YORK).date()
    horizon = today + timedelta(days=DISCOVERY_WINDOW_DAYS)
    existing = {row["url"] for row in registry["events"]}
    added = 0
    failures = 0

    browser = VividBrowser(headless=headless, timeout=timeout)
    try:
        for venue, venue_url in VENUE_FEEDS.items():
            try:
                discovered = browser.discover_event_urls(venue_url)
                eligible = sorted(
                    url for url in discovered
                    if (hint := event_date_from_url(url)) is not None
                    and today <= hint.date() <= horizon
                )
                venue_added = 0
                for url in eligible:
                    if url in existing:
                        continue
                    registry["events"].append(
                        {
                            "url": url,
                            "active": True,
                            "venue": venue,
                            "event_date_hint": event_date_from_url(url).date().isoformat(),
                            "added_at": now.isoformat(),
                        }
                    )
                    existing.add(url)
                    added += 1
                    venue_added += 1
                print(f"{venue}: found {len(eligible)} upcoming games; added {venue_added}.", flush=True)
            except Exception as exc:
                failures += 1
                print(f"DISCOVERY FAILED: {venue}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    finally:
        try:
            browser.close()
        except Exception as exc:
            print(
                f"Browser cleanup warning: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )

    registry["last_discovery_at"] = now.isoformat()
    save_registry(registry_path, registry)
    print(f"Discovery finished: {added} added, {failures} venue failures.", flush=True)
    return added, failures


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("America/New_York"))
    return value.astimezone(timezone.utc)


def collection_interval(hours_until_event: float) -> timedelta | None:
    if hours_until_event > CAPTURE_WINDOW_DAYS * 24:
        return None
    if hours_until_event <= 24:
        return timedelta(minutes=15)
    if hours_until_event <= 72:
        return timedelta(hours=1)
    return timedelta(hours=4)


def is_due(session: Any, url: str, now: datetime, force: bool = False) -> tuple[bool, str]:
    event = session.query(Event).filter(Event.URL == url).first()
    if event is None:
        hint = event_date_from_url(url)
        if hint and hint.date() > now.astimezone(NEW_YORK).date() + timedelta(days=CAPTURE_WINDOW_DAYS):
            return False, "outside four-day capture window"
        return True, "new event"
    event_time = as_utc(event.event_date)
    hours_until = (event_time - now).total_seconds() / 3600
    if hours_until <= 0:
        return False, "event has started"
    if force:
        return True, "forced"
    interval = collection_interval(hours_until)
    if interval is None:
        return False, "outside four-day capture window"
    latest = session.query(func.max(Iteration.captured_at)).filter(Iteration.event_id == event.id).scalar()
    if latest is None:
        return True, "no snapshots"
    next_capture = as_utc(latest) + interval
    if now >= next_capture:
        return True, "scheduled"
    return False, f"next capture after {next_capture.astimezone().strftime('%Y-%m-%d %H:%M')}"


def store_snapshot(url: str, event_date: datetime, snapshot: EventSnapshot) -> tuple[int, int]:
    SessionLocal = CreateModel().getSession()
    with SessionLocal() as session:
        event = session.query(Event).filter(Event.URL == url).first()
        section_names = [row.section for row in snapshot.sections]
        if event is None:
            event = Event(
                title=snapshot.title,
                event_date=event_date,
                event_sections=section_names,
                URL=url,
                Place=snapshot.venue,
            )
            session.add(event)
        else:
            event.title = snapshot.title
            event.event_date = event_date
            event.Place = snapshot.venue
            known = set(event.event_sections or [])
            event.event_sections = list(event.event_sections or []) + [s for s in section_names if s not in known]

        iteration = Iteration(event=event)
        session.add(iteration)
        session.add_all(
            Ticket(
                section=row.section,
                price=row.price,
                ticketsPerSection=row.listing_count,
                iteration=iteration,
            )
            for row in snapshot.sections
        )
        session.commit()
        return event.id, iteration.id


def database_path() -> Path:
    configured = os.environ.get("DATABASE_PATH", str(PROJECT_DIR / "Event-collection.db"))
    return Path(configured).expanduser().resolve()


def create_daily_backup(
    now: datetime | None = None,
    source: Path | None = None,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
) -> Path:
    """Create one consistent SQLite backup per day and retain the latest week."""
    now = now or datetime.now(NEW_YORK)
    source = source or database_path()
    if not source.exists():
        raise FileNotFoundError(f"Cannot back up missing database: {source}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"Event-collection-{now.astimezone(NEW_YORK):%Y-%m-%d}.db"
    if not target.exists():
        temporary = target.with_suffix(".db.tmp")
        temporary.unlink(missing_ok=True)
        with sqlite3.connect(source) as source_db, sqlite3.connect(temporary) as backup_db:
            source_db.backup(backup_db)
        temporary.replace(target)

    backups = sorted(backup_dir.glob("Event-collection-*.db"), reverse=True)
    for expired in backups[BACKUP_RETENTION_DAYS:]:
        expired.unlink()
    return target


def write_capture_audit(
    url: str,
    event_date: datetime,
    snapshot: EventSnapshot,
    event_id: int,
    iteration_id: int,
    captured_at: datetime | None = None,
    audit_dir: Path = DEFAULT_AUDIT_DIR,
) -> Path:
    """Append the winning listing behind every stored section price."""
    captured_at = captured_at or datetime.now(timezone.utc)
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / f"{captured_at.astimezone(NEW_YORK):%Y-%m-%d}.jsonl"
    record = {
        "schema_version": 1,
        "captured_at": captured_at.isoformat(),
        "event_date": event_date.isoformat(),
        "event_id": event_id,
        "iteration_id": iteration_id,
        "source_id": snapshot.source_id,
        "title": snapshot.title,
        "venue": snapshot.venue,
        "url": url,
        "currency": "USD",
        "section_count": len(snapshot.sections),
        "sections": [asdict(row) for row in snapshot.sections],
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    cutoff = captured_at - timedelta(days=AUDIT_RETENTION_DAYS)
    for candidate in audit_dir.glob("*.jsonl"):
        modified = datetime.fromtimestamp(candidate.stat().st_mtime, tz=timezone.utc)
        if modified < cutoff:
            candidate.unlink()
    return path


def write_health(status: str, health_file: Path = DEFAULT_HEALTH_FILE, **details: Any) -> None:
    health_file.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **details,
    }
    temporary = health_file.with_suffix(health_file.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(health_file)


def show_health(health_file: Path = DEFAULT_HEALTH_FILE) -> None:
    if not health_file.exists():
        print("No collector health record exists yet.")
        return
    value = json.loads(health_file.read_text(encoding="utf-8"))
    print(json.dumps(value, indent=2))


def load_runtime_state(state_file: Path = DEFAULT_STATE_FILE) -> dict[str, Any]:
    if not state_file.exists():
        return {"consecutive_failed_cycles": 0, "cooldown_until": None}
    try:
        value = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"consecutive_failed_cycles": 0, "cooldown_until": None}
    return {
        "consecutive_failed_cycles": int(value.get("consecutive_failed_cycles", 0)),
        "cooldown_until": value.get("cooldown_until"),
        "last_failure": value.get("last_failure"),
    }


def save_runtime_state(state: dict[str, Any], state_file: Path = DEFAULT_STATE_FILE) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_file.with_suffix(state_file.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(state_file)


def circuit_cooldown_until(state: dict[str, Any], now: datetime) -> datetime | None:
    cooldown = parse_iso_datetime(str(state.get("cooldown_until") or ""))
    if cooldown is None or as_utc(cooldown) <= now:
        return None
    return as_utc(cooldown)


def open_failure_circuit(
    state: dict[str, Any],
    now: datetime,
    reason: str,
    state_file: Path = DEFAULT_STATE_FILE,
) -> datetime:
    cooldown_until = now + FAILURE_COOLDOWN
    state.update(
        {
            "consecutive_failed_cycles": MAX_CONSECUTIVE_FAILED_CYCLES,
            "cooldown_until": cooldown_until.isoformat(),
            "last_failure": reason,
        }
    )
    save_runtime_state(state, state_file)
    return cooldown_until


def record_cycle_result(
    state: dict[str, Any],
    now: datetime,
    succeeded: int,
    failed: int,
    reason: str | None = None,
    state_file: Path = DEFAULT_STATE_FILE,
) -> datetime | None:
    if succeeded:
        state.update({"consecutive_failed_cycles": 0, "cooldown_until": None})
        save_runtime_state(state, state_file)
        return None
    if not failed:
        return None

    failed_cycles = int(state.get("consecutive_failed_cycles", 0)) + 1
    state["consecutive_failed_cycles"] = failed_cycles
    state["last_failure"] = reason
    if failed_cycles >= MAX_CONSECUTIVE_FAILED_CYCLES:
        return open_failure_circuit(state, now, reason or "collection failures", state_file)
    save_runtime_state(state, state_file)
    return None


def pythonanywhere_cpu_usage() -> dict[str, Any] | None:
    """Read the account CPU quota without exposing the API token in logs."""
    token = os.environ.get("API_TOKEN")
    username = os.environ.get("PYTHONANYWHERE_USERNAME") or os.environ.get("USER")
    if not token or not username:
        return None
    host = os.environ.get("PYTHONANYWHERE_HOST", "www.pythonanywhere.com")
    request = urllib.request.Request(
        f"https://{host}/api/v0/user/{username}/cpu/",
        headers={"Authorization": f"Token {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=CPU_API_TIMEOUT_SECONDS) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def cpu_budget_allows_capture(usage: dict[str, Any] | None) -> tuple[bool, str]:
    if not usage:
        return True, "CPU quota API unavailable"
    try:
        limit = Decimal(str(usage["daily_cpu_limit_seconds"]))
        used = Decimal(str(usage["daily_cpu_total_usage_seconds"]))
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return True, "CPU quota response invalid"

    stop_at = min(
        limit * CPU_USAGE_STOP_FRACTION,
        max(Decimal("0"), limit - CPU_MINIMUM_HEADROOM_SECONDS),
    )
    remaining = max(Decimal("0"), limit - used)
    if used >= stop_at:
        return False, (
            f"CPU safety stop: {used:.0f}/{limit:.0f} seconds used; "
            f"{remaining:.0f} seconds remain"
        )
    return True, f"CPU budget healthy: {used:.0f}/{limit:.0f} seconds used"


def browser_failure_requires_immediate_cooldown(exc: Exception) -> bool:
    fatal_names = {
        "InvalidSessionIdException",
        "NoSuchWindowException",
        "ReadTimeoutError",
        "SessionNotCreatedException",
    }
    return type(exc).__name__ in fatal_names


def select_due_urls(due_urls: list[str]) -> tuple[list[str], int]:
    """Run every due event, ordered by the soonest game first."""
    ordered = sorted(
        due_urls,
        key=lambda url: event_date_from_url(url) or datetime.max.replace(tzinfo=NEW_YORK),
    )
    return ordered, 0


def show_audit(event_id: int | None, section: str | None, limit: int) -> None:
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    normalized_section = " ".join((section or "").lower().split())
    for path in sorted(DEFAULT_AUDIT_DIR.glob("*.jsonl"), reverse=True):
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            record = json.loads(line)
            if event_id is not None and record.get("event_id") != event_id:
                continue
            for price in record.get("sections", []):
                candidate = " ".join(str(price.get("section", "")).lower().split())
                if normalized_section and candidate != normalized_section:
                    continue
                matches.append((record, price))
                if len(matches) >= limit:
                    break
            if len(matches) >= limit:
                break
        if len(matches) >= limit:
            break

    if not matches:
        print("No matching audited prices were found.")
        return
    for record, price in matches:
        print(
            f"{record['captured_at']} | event {record['event_id']} | "
            f"{price['section']} | ${price['price']} {record['currency']} | "
            f"row {price['row'] or '?'} | quantity {price['quantity'] or '?'} | "
            f"{price['listing_count']} listings checked | source {price['price_source']}"
        )


def prune_finished_events(registry_path: Path, registry: dict[str, Any], now: datetime) -> int:
    """Remove finished links from the queue without deleting their database history."""
    SessionLocal = CreateModel().getSession()
    today = now.astimezone(NEW_YORK).date()
    kept: list[dict[str, Any]] = []
    removed = 0
    with SessionLocal() as session:
        for row in registry["events"]:
            event = session.query(Event).filter(Event.URL == row["url"]).first()
            finished = event is not None and as_utc(event.event_date) <= now
            hint = event_date_from_url(row["url"])
            # The URL date is an independent hard stop.  This retires stale
            # links even when older database rows contain a bad event time.
            expired_url = hint is not None and hint.date() < today
            excluded_venue = registry_row_is_excluded(row)
            if finished or expired_url or excluded_venue:
                removed += 1
            else:
                kept.append(row)
    if removed:
        registry["events"] = kept
        save_registry(registry_path, registry)
        print(
            f"Retired {removed} finished or excluded links; historical database data was kept.",
            flush=True,
        )
    return removed


def retire_url(registry_path: Path, url: str) -> bool:
    """Remove one link from the collection queue while preserving database history."""
    registry = load_registry(registry_path)
    kept = [row for row in registry["events"] if row["url"] != url]
    if len(kept) == len(registry["events"]):
        return False
    registry["events"] = kept
    save_registry(registry_path, registry)
    return True


def run_collector(registry_path: Path, force: bool, headless: bool, timeout: int) -> int:
    registry = load_registry(registry_path)
    now = datetime.now(timezone.utc)
    backup_path = create_daily_backup(now=now)
    print(f"Database backup ready: {backup_path}")
    prune_finished_events(registry_path, registry, now)
    active_urls = [row["url"] for row in registry["events"] if row.get("active", True)]
    if not active_urls:
        print("No active events. Add one with: python collector.py add <vivid-url>")
        write_health("idle", reason="no active events", backup=str(backup_path))
        return 0

    SessionLocal = CreateModel().getSession()
    due_urls: list[str] = []
    with SessionLocal() as session:
        for url in active_urls:
            due, reason = is_due(session, url, now, force=force)
            print(f"{'DUE' if due else 'SKIP'}: {url} ({reason})")
            if due:
                due_urls.append(url)

    if not due_urls:
        print("Nothing is due right now.")
        write_health(
            "healthy",
            due=0,
            succeeded=0,
            failed=0,
            audit_failed=0,
            backup=str(backup_path),
        )
        return 0

    state = load_runtime_state()
    cooldown_until = circuit_cooldown_until(state, now)
    if cooldown_until is not None:
        reason = f"Failure circuit open until {cooldown_until.astimezone(NEW_YORK):%Y-%m-%d %H:%M %Z}"
        print(f"PAUSED: {reason}", flush=True)
        write_health(
            "paused",
            reason=reason,
            due=len(due_urls),
            backup=str(backup_path),
            next_retry_at=cooldown_until.isoformat(),
        )
        return 0

    cpu_usage = pythonanywhere_cpu_usage()
    cpu_allowed, cpu_reason = cpu_budget_allows_capture(cpu_usage)
    print(cpu_reason, flush=True)
    if not cpu_allowed:
        write_health(
            "paused",
            reason=cpu_reason,
            due=len(due_urls),
            backup=str(backup_path),
            cpu=cpu_usage,
        )
        return 0

    selected_urls, deferred = select_due_urls(due_urls)

    try:
        browser = VividBrowser(headless=headless, timeout=timeout)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        cooldown_until = open_failure_circuit(state, now, reason)
        print(
            f"BROWSER START FAILED: {reason}\n"
            f"Circuit opened until {cooldown_until.astimezone(NEW_YORK):%Y-%m-%d %H:%M %Z}.",
            file=sys.stderr,
            flush=True,
        )
        write_health(
            "paused",
            reason=reason,
            due=len(due_urls),
            failed=1,
            deferred=len(due_urls),
            backup=str(backup_path),
            next_retry_at=cooldown_until.isoformat(),
        )
        return 1

    failures = 0
    audit_failures = 0
    retired = 0
    succeeded = 0
    last_failure: str | None = None
    stopped_early = False
    try:
        for index, url in enumerate(selected_urls, start=1):
            print(f"[{index}/{len(selected_urls)}] Capturing {url}")
            try:
                payload, event_date = browser.capture(url)
                if as_utc(event_date) <= datetime.now(timezone.utc):
                    retire_url(registry_path, url)
                    retired += 1
                    print(f"RETIRED: event has already started; no snapshot was stored for {url}")
                    continue
                snapshot = SnapshotParser.parse(payload)
                event_id, iteration_id = store_snapshot(url, event_date, snapshot)
                try:
                    audit_path = write_capture_audit(
                        url,
                        event_date,
                        snapshot,
                        event_id,
                        iteration_id,
                    )
                except Exception as exc:
                    audit_failures += 1
                    print(
                        f"AUDIT FAILED: event {event_id}, iteration {iteration_id}: "
                        f"{type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                else:
                    print(f"Audit recorded: {audit_path}")
                print(
                    f"Saved {len(snapshot.sections)} sections for {snapshot.title} "
                    f"(event {event_id}, iteration {iteration_id})."
                )
                succeeded += 1
            except Exception as exc:
                failures += 1
                last_failure = f"{type(exc).__name__}: {exc}"
                print(f"FAILED: {url}\n  {last_failure}", file=sys.stderr, flush=True)
                if browser_failure_requires_immediate_cooldown(exc):
                    stopped_early = True
                    cooldown_until = open_failure_circuit(state, now, last_failure)
                    print(
                        "Browser session is unhealthy; stopping this cycle immediately. "
                        f"Next retry after {cooldown_until.astimezone(NEW_YORK):%Y-%m-%d %H:%M %Z}.",
                        file=sys.stderr,
                        flush=True,
                    )
                    break
                if failures >= MAX_CAPTURE_FAILURES_PER_CYCLE:
                    remaining = len(selected_urls) - index
                    print(
                        f"FAILURE LIMIT: stopping after {failures} failed captures; "
                        f"{remaining} due events will retry next cycle.",
                        file=sys.stderr,
                        flush=True,
                    )
                    break
    finally:
        try:
            browser.close()
        except Exception as exc:
            print(
                f"Browser cleanup warning: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )

    if not stopped_early:
        cooldown_until = record_cycle_result(
            state,
            now,
            succeeded=succeeded,
            failed=failures,
            reason=last_failure,
        )
    deferred += len(selected_urls) - succeeded - failures - retired
    status = "healthy" if failures == 0 and audit_failures == 0 else "degraded"
    if cooldown_until is not None:
        status = "paused"
    write_health(
        status,
        due=len(due_urls),
        succeeded=succeeded,
        retired=retired,
        failed=failures,
        deferred=deferred,
        audit_failed=audit_failures,
        backup=str(backup_path),
        cpu=cpu_usage,
        next_retry_at=cooldown_until.isoformat() if cooldown_until else None,
    )
    print(
        f"Finished: {succeeded} succeeded, {retired} retired, {failures} failed, "
        f"{deferred} deferred, {audit_failures} audit failures."
    )
    return 1 if failures or audit_failures else 0


def show_status(registry_path: Path) -> None:
    registry = load_registry(registry_path)
    SessionLocal = CreateModel().getSession()
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        for row in registry["events"]:
            url = row["url"]
            event = session.query(Event).filter(Event.URL == url).first()
            due, reason = is_due(session, url, now)
            label = event.title if event else "Not captured yet"
            state = "active" if row.get("active", True) else "inactive"
            print(f"{label}\n  {state}; {'due' if due else reason}\n  {url}")


def watch_collector(
    registry_path: Path,
    check_every: int,
    timeout: int,
    discover_automatically: bool = False,
) -> int:
    """Keep checking for due events for an always-on cloud task."""
    print(f"Collector service started; checking every {check_every} seconds.", flush=True)
    while True:
        started = datetime.now().astimezone()
        print(f"\n[{started:%Y-%m-%d %H:%M:%S %Z}] Checking events...", flush=True)
        try:
            registry = load_registry(registry_path)
            if discover_automatically and discovery_due(registry, datetime.now(timezone.utc)):
                print("Refreshing the next 30 days of stadium schedules...", flush=True)
                discover_events(registry_path, headless=True, timeout=timeout)
            run_collector(registry_path, force=False, headless=True, timeout=timeout)
        except Exception as exc:
            print(f"SERVICE ERROR: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            write_health("error", error=f"{type(exc).__name__}: {exc}")
        elapsed = (datetime.now().astimezone() - started).total_seconds()
        time.sleep(max(0, check_every - elapsed))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect Vivid Seats price snapshots.")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_parser = subparsers.add_parser("add", help="Register one or more Vivid event links.")
    add_parser.add_argument("urls", nargs="+")
    run_parser = subparsers.add_parser("run", help="Capture every event currently due.")
    run_parser.add_argument("--force", action="store_true")
    run_parser.add_argument("--headless", action="store_true")
    run_parser.add_argument("--timeout", type=int, default=DEFAULT_CAPTURE_TIMEOUT)
    watch_parser = subparsers.add_parser(
        "watch", help="Continuously check for due events (for an always-on task)."
    )
    watch_parser.add_argument("--check-every", type=int, default=900)
    watch_parser.add_argument("--timeout", type=int, default=DEFAULT_CAPTURE_TIMEOUT)
    watch_parser.add_argument(
        "--discover-events",
        action="store_true",
        help="Also run the more expensive daily venue discovery inside this service.",
    )
    discover_parser = subparsers.add_parser(
        "discover", help="Find the next 30 days of MLB games at the configured stadiums."
    )
    discover_parser.add_argument("--headless", action="store_true")
    discover_parser.add_argument("--timeout", type=int, default=DEFAULT_CAPTURE_TIMEOUT)
    subparsers.add_parser("status", help="Show events and next-capture state.")
    subparsers.add_parser("health", help="Show the latest collector health record.")
    audit_parser = subparsers.add_parser(
        "audit", help="Show the winning listings behind stored section prices."
    )
    audit_parser.add_argument("--event-id", type=int)
    audit_parser.add_argument("--section")
    audit_parser.add_argument("--limit", type=int, default=20)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "add":
        add_urls(args.urls, args.registry)
        return 0
    if args.command == "run":
        return run_collector(args.registry, args.force, args.headless, args.timeout)
    if args.command == "watch":
        if args.check_every < 60:
            raise ValueError("--check-every must be at least 60 seconds")
        return watch_collector(
            args.registry,
            args.check_every,
            args.timeout,
            discover_automatically=args.discover_events,
        )
    if args.command == "discover":
        _, failures = discover_events(args.registry, args.headless, args.timeout)
        return 1 if failures == len(VENUE_FEEDS) else 0
    if args.command == "status":
        show_status(args.registry)
        return 0
    if args.command == "health":
        show_health()
        return 0
    if args.command == "audit":
        if args.limit < 1:
            raise ValueError("--limit must be at least 1")
        show_audit(args.event_id, args.section, args.limit)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
