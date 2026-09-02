"""Collect Vivid Seats NHL section prices and provider map geometry.

NHL games are sampled during the final 30 days before puck drop. Collection is
daily from 30 to 14 days, every 12 hours from 14 to 7 days, every 6 hours from
7 days to 72 hours, and hourly throughout the final 72 hours. Collection stops
at the scheduled start time.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
import hashlib
import html
import json
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
    as_utc,
    snapshot_to_payload,
    validated_vivid_url,
)
from nfl_collector import VividNFLBrowser
from nfl_metadata import (
    choose_best_geometry,
    extract_map_geometry_from_json,
    geometry_section_count,
    sanitize_map_geometry,
)


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_HEALTH_OUTPUT = PROJECT_DIR / "nhl_remote_health.json"
DEFAULT_PENDING_DIR = PROJECT_DIR / "nhl_pending"
DEFAULT_SMOKE_OUTPUT = PROJECT_DIR / "nhl_smoke_result.json"
NHL_CAPTURE_WINDOW_HOURS = 30 * 24
NHL_HOURLY_WINDOW_HOURS = 72
NHL_SIX_HOUR_WINDOW_HOURS = 7 * 24
NHL_TWELVE_HOUR_WINDOW_HOURS = 14 * 24
NHL_FINAL_CADENCE_HOURS = 1
NHL_SIX_HOUR_CADENCE_HOURS = 6
NHL_TWELVE_HOUR_CADENCE_HOURS = 12
NHL_DAILY_CADENCE_HOURS = 24
DISCOVERY_HORIZON_DAYS = 30
SMOKE_HORIZON_DAYS = 45
MIN_USABLE_SECTIONS = 8
NHL_FEED_URLS = (
    "https://www.vividseats.com/nhl-hockey/",
    "https://www.vividseats.com/nhl/",
)

NHL_TEAM_NAMES = frozenset(
    {
        "Anaheim Ducks",
        "Boston Bruins",
        "Buffalo Sabres",
        "Calgary Flames",
        "Carolina Hurricanes",
        "Chicago Blackhawks",
        "Colorado Avalanche",
        "Columbus Blue Jackets",
        "Dallas Stars",
        "Detroit Red Wings",
        "Edmonton Oilers",
        "Florida Panthers",
        "Los Angeles Kings",
        "Minnesota Wild",
        "Montreal Canadiens",
        "Nashville Predators",
        "New Jersey Devils",
        "New York Islanders",
        "New York Rangers",
        "Ottawa Senators",
        "Philadelphia Flyers",
        "Pittsburgh Penguins",
        "San Jose Sharks",
        "Seattle Kraken",
        "St. Louis Blues",
        "Tampa Bay Lightning",
        "Toronto Maple Leafs",
        "Utah Mammoth",
        "Vancouver Canucks",
        "Vegas Golden Knights",
        "Washington Capitals",
        "Winnipeg Jets",
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
    "prospect tournament",
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
class DiscoveredNHLGame:
    url: str
    title: str
    date_hint: date | None


@dataclass(frozen=True)
class NHLEventSnapshot(EventSnapshot):
    map_geometry: dict[str, Any] | None = None
    currency: str = "USD"


def ordered_matchup_from_title(title: str) -> tuple[str, str] | None:
    """Return the two NHL teams in provider display order."""

    normalized = " ".join(str(title or "").split()).casefold()
    if not normalized or any(marker in normalized for marker in NON_GAME_MARKERS):
        return None
    matches = sorted(
        (
            (normalized.find(team.casefold()), team)
            for team in NHL_TEAM_NAMES
            if team.casefold() in normalized
        ),
        key=lambda item: item[0],
    )
    if len(matches) != 2:
        return None
    return matches[0][1], matches[1][1]


def is_nhl_game_title(title: str) -> bool:
    normalized = " ".join(str(title or "").split()).casefold()
    separator = any(
        value in normalized for value in (" at ", " vs ", " vs. ", " versus ")
    )
    return ordered_matchup_from_title(title) is not None and separator


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


class _NHLAnchorParser(HTMLParser):
    def __init__(self, base_url: str, now: datetime):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.now = now
        self._href: str | None = None
        self._text: list[str] = []
        self.games: list[DiscoveredNHLGame] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attributes = dict(attrs)
        if self._href is not None or tag.casefold() != "a":
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

        if not is_nhl_game_title(title):
            return
        try:
            url = validated_vivid_url(html.unescape(urljoin(self.base_url, raw_href)))
        except ValueError:
            return
        self.games.append(
            DiscoveredNHLGame(
                url=url,
                title=title,
                date_hint=date_hint_from_text(title, self.now)
                or date_hint_from_url(url),
            )
        )


def extract_nhl_game_rows(
    page_html: str,
    base_url: str = NHL_FEED_URLS[0],
    now: datetime | None = None,
) -> list[DiscoveredNHLGame]:
    parser = _NHLAnchorParser(base_url, now or datetime.now(timezone.utc))
    parser.feed(page_html)

    by_url: dict[str, DiscoveredNHLGame] = {}
    for row in parser.games:
        current = by_url.get(row.url)
        if current is None:
            by_url[row.url] = row
            continue
        better_date = current.date_hint is None and row.date_hint is not None
        better_title = len(row.title) > len(current.title)
        if better_date or better_title:
            by_url[row.url] = DiscoveredNHLGame(
                url=row.url,
                title=row.title if better_title else current.title,
                date_hint=row.date_hint or current.date_hint,
            )
    return sorted(
        by_url.values(),
        key=lambda row: (row.date_hint or date.max, row.title.casefold(), row.url),
    )


def _currency_code(metadata: dict[str, Any], payload: dict[str, Any]) -> str:
    candidates = (
        metadata.get("currencyCode"),
        metadata.get("currency"),
        payload.get("currencyCode"),
        payload.get("currency"),
    )
    for value in candidates:
        if isinstance(value, dict):
            value = value.get("code") or value.get("default")
        code = str(value or "").strip().upper()
        if re.fullmatch(r"[A-Z]{3}", code):
            return code
    # Vivid's U.S. browser session returns numeric prices in USD when no code is
    # supplied. Keeping the field explicit prevents future mixed-currency
    # aggregation if the provider begins returning another code.
    return "USD"


class NHLInventoryIncompleteError(ValueError):
    """Vivid exposed too few priced sections for a trustworthy snapshot."""


class NHLSnapshotParser:
    """Convert one Vivid listings response to one lowest price per arena section."""

    @staticmethod
    def parse(payload: dict[str, Any]) -> NHLEventSnapshot:
        global_rows = payload.get("global") or []
        if not global_rows or not isinstance(global_rows[0], dict):
            raise ValueError("Listings response does not contain event metadata.")

        metadata = global_rows[0]
        title = " ".join(str(metadata.get("productionName") or "").split())
        venue = " ".join(str(metadata.get("mapTitle") or "").split())
        source_id = str(metadata.get("productionId") or "").strip()
        if not title or not venue or not source_id:
            raise ValueError("Listings response is missing its title, venue, or source ID.")
        if not is_nhl_game_title(title):
            raise ValueError(f"NHL capture rejected non-game event: {title}")

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
            raise NHLInventoryIncompleteError(
        f"NHL capture rejected: only {len(sections)} usable sections; "
        f"minimum is {MIN_USABLE_SECTIONS}."
    )

        section_names = [row.section for row in sections]
        map_geometry = choose_best_geometry(
            (
                payload.get("_map_geometry"),
                extract_map_geometry_from_json(
                    payload,
                    section_names,
                    source="vivid-listings-json",
                ),
            ),
            section_names,
        )
        return NHLEventSnapshot(
            source_id=source_id,
            title=title,
            venue=venue,
            sections=sections,
            map_geometry=map_geometry,
            currency=_currency_code(metadata, payload),
        )


def discover_nhl_games(headless: bool, timeout: int) -> tuple[list[DiscoveredNHLGame], list[str]]:
    """Discover NHL event links while tolerating one unusable league-feed URL."""

    browser: VividNFLBrowser | None = None
    errors: list[str] = []
    best: dict[str, DiscoveredNHLGame] = {}
    try:
        browser = VividNFLBrowser(headless=headless, timeout=timeout)
        for feed_url in NHL_FEED_URLS:
            try:
                browser.driver.get("about:blank")
                browser.driver.get(feed_url)
                deadline = time.monotonic() + timeout
                started_at = time.monotonic()
                last_scroll_at = 0.0
                last_new_at: float | None = None
                while time.monotonic() < deadline:
                    current_time = time.monotonic()
                    if current_time - last_scroll_at >= 1.0:
                        try:
                            browser.driver.execute_script(
                                "window.scrollTo(0, document.body.scrollHeight);"
                            )
                        except Exception:
                            pass
                        last_scroll_at = current_time

                    rows = extract_nhl_game_rows(
                        browser.driver.page_source,
                        feed_url,
                        datetime.now(timezone.utc),
                    )
                    previous_count = len(best)
                    for row in rows:
                        best[row.url] = row
                    if len(best) > previous_count:
                        last_new_at = time.monotonic()
                    elif best and last_new_at is not None:
                        if (
                            time.monotonic() - last_new_at
                            >= max(4.0, DISCOVERY_SETTLE_SECONDS)
                            and time.monotonic() - started_at >= 6.0
                        ):
                            break
                    time.sleep(0.5)
                if best:
                    break
            except Exception as exc:
                errors.append(
                    f"{feed_url}: {type(exc).__name__}: {exc}"
                )
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass

    rows = sorted(
        best.values(),
        key=lambda row: (row.date_hint or date.max, row.title.casefold(), row.url),
    )
    if not rows and not errors:
        errors.append("No NHL event links appeared on either Vivid league feed.")
    return rows, errors


def hourly_capture_slot(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def nhl_capture_interval_hours(event_date: datetime, now: datetime) -> int | None:
    hours_until = (
        as_utc(event_date) - now.astimezone(timezone.utc)
    ).total_seconds() / 3600
    if not 0 < hours_until <= NHL_CAPTURE_WINDOW_HOURS:
        return None
    if hours_until <= NHL_HOURLY_WINDOW_HOURS:
        return NHL_FINAL_CADENCE_HOURS
    if hours_until <= NHL_SIX_HOUR_WINDOW_HOURS:
        return NHL_SIX_HOUR_CADENCE_HOURS
    if hours_until <= NHL_TWELVE_HOUR_WINDOW_HOURS:
        return NHL_TWELVE_HOUR_CADENCE_HOURS
    return NHL_DAILY_CADENCE_HOURS


def nhl_capture_tier(event_date: datetime, now: datetime) -> str | None:
    interval = nhl_capture_interval_hours(event_date, now)
    return {
        NHL_FINAL_CADENCE_HOURS: "final_72_hours_hourly",
        NHL_SIX_HOUR_CADENCE_HOURS: "days_4_to_7_every_6_hours",
        NHL_TWELVE_HOUR_CADENCE_HOURS: "days_8_to_14_every_12_hours",
        NHL_DAILY_CADENCE_HOURS: "days_15_to_30_daily",
    }.get(interval)

def nhl_capture_phase(cadence_key: str, interval_hours: int) -> int:
    if interval_hours <= 1:
        return 0
    digest = hashlib.sha256(str(cadence_key).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % interval_hours


def nhl_capture_is_due(
    event_date: datetime,
    capture_slot: datetime,
    cadence_key: str = "",
) -> bool:
    interval = nhl_capture_interval_hours(event_date, capture_slot)
    if interval is None:
        return False
    if interval == NHL_FINAL_CADENCE_HOURS:
        return True
    phase_key = cadence_key or as_utc(event_date).isoformat()
    phase = nhl_capture_phase(phase_key, interval)
    utc_hour = int(hourly_capture_slot(capture_slot).timestamp() // 3600)
    return utc_hour % interval == phase


def nhl_is_within_capture_window(event_date: datetime, now: datetime) -> bool:
    hours_until = (
        as_utc(event_date) - now.astimezone(timezone.utc)
    ).total_seconds() / 3600
    return 0 < hours_until <= NHL_CAPTURE_WINDOW_HOURS


def nhl_snapshot_to_payload(
    url: str,
    event_date: datetime,
    captured_at: datetime,
    snapshot: NHLEventSnapshot,
    *,
    schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = snapshot_to_payload(url, event_date, captured_at, snapshot)
    payload["event_type"] = "nhl"
    payload["currency"] = snapshot.currency
    section_names = [row.section for row in snapshot.sections]
    geometry = sanitize_map_geometry(
        getattr(snapshot, "map_geometry", None),
        section_names,
    )
    if geometry is not None:
        payload["map_geometry"] = geometry
    if schedule is not None:
        payload["schedule"] = schedule
    return payload


def run_smoke_capture(
    url: str,
    headless: bool,
    timeout: int,
    output: Path = DEFAULT_SMOKE_OUTPUT,
) -> int:
    browser: VividNFLBrowser | None = None
    try:
        browser = VividNFLBrowser(headless=headless, timeout=timeout)
        validated = validated_vivid_url(url)
        raw_payload, event_date = browser.capture(validated)
        snapshot = NHLSnapshotParser.parse(raw_payload)
        result = {
            "status": "success",
            "event_type": "nhl",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "event_date": event_date.isoformat(),
            "source_url": validated,
            "source_id": snapshot.source_id,
            "title": snapshot.title,
            "venue": snapshot.venue,
            "currency": snapshot.currency,
            "section_count": len(snapshot.sections),
            "map_geometry_sections": geometry_section_count(snapshot.map_geometry),
            "sections": [asdict(row) for row in snapshot.sections],
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(
            f"NHL SMOKE PASSED: {snapshot.title} with {len(snapshot.sections)} "
            f"sections and {result['map_geometry_sections']} provider polygons."
        )
        return 0
    except Exception as exc:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "status": "failure",
                    "event_type": "nhl",
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "source_url": url,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"NHL SMOKE FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("smoke",))
    parser.add_argument("event_url")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--output", type=Path, default=DEFAULT_SMOKE_OUTPUT)
    args = parser.parse_args()

    return run_smoke_capture(
        args.event_url,
        args.headless,
        args.timeout,
        args.output,
    )


if __name__ == "__main__":
    raise SystemExit(main())
