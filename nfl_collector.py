"""Collect Vivid Seats NFL ticket prices into the separate NFL pipeline.

NFL games are discovered from Vivid's league feed and tracked during the
final 30 days before kickoff. Each game is sampled every six hours from 30 to
14 days out, every three hours from 14 to 7 days out, and once per hour during
the final week before being uploaded to the NFL-only API and database.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
import hashlib
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
    event_metadata_is_still_rendering,
    post_snapshot_with_retry,
    queue_snapshot,
    replay_pending_snapshots,
    snapshot_to_payload,
    validated_vivid_url,
)
from nfl_metadata import (
    choose_best_geometry,
    eastern_iso,
    extract_map_geometry_from_json,
    extract_map_geometry_from_svg,
    geometry_section_count,
    sanitize_map_geometry,
)


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_HEALTH_OUTPUT = PROJECT_DIR / "nfl_remote_health.json"
DEFAULT_PENDING_DIR = PROJECT_DIR / "nfl_pending"
DEFAULT_SMOKE_OUTPUT = PROJECT_DIR / "nfl_smoke_result.json"
NFL_CAPTURE_WINDOW_HOURS = 30 * 24
NFL_THREE_HOUR_WINDOW_HOURS = 14 * 24
NFL_HOURLY_WINDOW_HOURS = 7 * 24
NFL_EARLY_CADENCE_HOURS = 6
NFL_MIDDLE_CADENCE_HOURS = 3
NFL_FINAL_CADENCE_HOURS = 1
DISCOVERY_HORIZON_DAYS = 30
SMOKE_HORIZON_DAYS = 45
MIN_USABLE_SECTIONS = 10
NFL_FEED_URL = "https://www.vividseats.com/nfl/"
MAP_GEOMETRY_SETTLE_SECONDS = 2.5
MAX_MAP_RESPONSE_BYTES = 8_000_000

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


@dataclass(frozen=True)
class NFLEventSnapshot(EventSnapshot):
    map_geometry: dict[str, Any] | None = None


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
                date_hint=date_hint_from_text(title, self.now) or date_hint_from_url(url),
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
        return NFLEventSnapshot(
            source_id=source_id,
            title=title,
            venue=venue,
            sections=sections,
            map_geometry=map_geometry,
        )


class VividNFLBrowser(VividBrowser):
    """Capture listings plus sanitized provider section polygons."""

    def __init__(self, headless: bool = False, timeout: int = 25):
        super().__init__(headless=headless, timeout=timeout)
        # The generic collector blocks SVGs to save bandwidth. NFL maps need
        # their public SVG response, so replace that list without the SVG rule.
        self.driver.execute_cdp_cmd(
            "Network.setBlockedURLs",
            {
                "urls": [
                    "*.jpg",
                    "*.jpeg",
                    "*.png",
                    "*.gif",
                    "*.webp",
                    "*.woff",
                    "*.woff2",
                    "*.ttf",
                    "*doubleclick.net*",
                    "*google-analytics.com*",
                    "*googletagmanager.com*",
                ]
            },
        )

    @staticmethod
    def _looks_like_map_response(url: str, mime_type: str) -> bool:
        normalized_url = str(url or "").casefold()
        normalized_mime = str(mime_type or "").casefold()
        if "svg" in normalized_mime:
            return True
        return any(
            marker in normalized_url
            for marker in (
                "seatmap",
                "seat-map",
                "seating-map",
                "venue-map",
                "mapdata",
                "map-data",
                "/map/",
                "map.svg",
            )
        )

    def _response_text(self, request_id: str) -> str:
        try:
            result = self.driver.execute_cdp_cmd(
                "Network.getResponseBody", {"requestId": request_id}
            )
            body = str(result.get("body") or "")
            if result.get("base64Encoded"):
                body = base64.b64decode(body).decode("utf-8", errors="replace")
            if len(body.encode("utf-8", errors="ignore")) > MAX_MAP_RESPONSE_BYTES:
                return ""
            return body
        except Exception:
            return ""

    @staticmethod
    def _geometry_from_response(
        body: str,
        mime_type: str,
        response_url: str,
        known_sections: list[str],
    ) -> dict[str, Any] | None:
        stripped = body.lstrip()
        if not stripped:
            return None
        if "svg" in mime_type.casefold() or stripped.startswith("<svg"):
            return extract_map_geometry_from_svg(
                body,
                known_sections,
                source="vivid-network-svg",
                source_url=response_url,
            )
        if stripped.startswith(("{", "[")):
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                return None
            return extract_map_geometry_from_json(
                payload,
                known_sections,
                source="vivid-network-json",
                source_url=response_url,
            )
        return None

    def _open_map_view(self) -> bool:
        try:
            elements = self.driver.find_elements(
                "xpath",
                "//button | //*[@role='tab'] | //a[@role='button']",
            )
        except Exception:
            return False
        for element in elements[:200]:
            try:
                label = " ".join(
                    (
                        element.text
                        or element.get_attribute("aria-label")
                        or element.get_attribute("title")
                        or ""
                    ).split()
                ).casefold()
                if not label or "map" not in label or "open in maps" in label:
                    continue
                if not element.is_displayed() or not element.is_enabled():
                    continue
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", element
                )
                element.click()
                return True
            except Exception:
                continue
        return False

    def _dom_map_geometry(
        self,
        known_sections: list[str],
        source_url: str,
    ) -> dict[str, Any] | None:
        script = r"""
const known = arguments[0] || [];
const sourceUrl = arguments[1] || '';
function clean(value) {
  return String(value || '').replace(/\s+/g, ' ').trim();
}
function normalize(value) {
  return clean(value).toLowerCase()
    .replace(/\b(section|sections|sec|seating|seat|area|zone|block)\b/g, ' ')
    .replace(/[^a-z0-9]+/g, ' ').trim();
}
function number(value) {
  const matches = clean(value).match(/\d+/g);
  return matches ? Number(matches[matches.length - 1]) : null;
}
const exact = new Map();
const byNumber = new Map();
known.forEach((name) => {
  const key = normalize(name);
  if (!exact.has(key)) exact.set(key, []);
  exact.get(key).push(name);
  const n = number(name);
  if (n !== null) {
    if (!byNumber.has(n)) byNumber.set(n, []);
    byNumber.get(n).push(name);
  }
});
function match(hints) {
  for (let index = 0; index < hints.length; index += 1) {
    const hint = hints[index];
    const key = normalize(hint);
    const exactMatches = exact.get(key) || [];
    if (exactMatches.length === 1) return exactMatches[0];
    const n = number(hint);
    const numeric = n === null ? [] : (byNumber.get(n) || []);
    if (numeric.length === 1) return numeric[0];
  }
  return null;
}
function viewBox(svg) {
  const base = svg.viewBox && svg.viewBox.baseVal;
  if (base && base.width > 0 && base.height > 0) {
    return [base.x, base.y, base.width, base.height];
  }
  const width = Number(svg.getAttribute('width')) || svg.clientWidth;
  const height = Number(svg.getAttribute('height')) || svg.clientHeight;
  return width > 0 && height > 0 ? [0, 0, width, height] : null;
}
function pathFor(element) {
  const tag = element.tagName.toLowerCase();
  if (tag === 'path') return clean(element.getAttribute('d'));
  if (tag === 'polygon' || tag === 'polyline') {
    const points = clean(element.getAttribute('points'));
    if (!points) return '';
    const pairs = points.split(/\s+/).map((pair) => pair.split(','));
    if (pairs.length < 3) return '';
    return `M ${pairs.map((pair) => `${pair[0]} ${pair[1]}`).join(' L ')} Z`;
  }
  if (tag === 'rect') {
    const x = Number(element.getAttribute('x') || 0);
    const y = Number(element.getAttribute('y') || 0);
    const width = Number(element.getAttribute('width'));
    const height = Number(element.getAttribute('height'));
    if (!(width > 0 && height > 0)) return '';
    return `M ${x} ${y} H ${x + width} V ${y + height} H ${x} Z`;
  }
  return '';
}
function relativeTransform(svg, element) {
  try {
    const rootMatrix = svg.getCTM();
    const elementMatrix = element.getCTM();
    if (!rootMatrix || !elementMatrix) return '';
    const matrix = rootMatrix.inverse().multiply(elementMatrix);
    const values = [matrix.a, matrix.b, matrix.c, matrix.d, matrix.e, matrix.f];
    if (!values.every(Number.isFinite)) return '';
    const identity = Math.abs(matrix.a - 1) < 1e-8
      && Math.abs(matrix.b) < 1e-8
      && Math.abs(matrix.c) < 1e-8
      && Math.abs(matrix.d - 1) < 1e-8
      && Math.abs(matrix.e) < 1e-8
      && Math.abs(matrix.f) < 1e-8;
    return identity ? '' : `matrix(${values.join(' ')})`;
  } catch (error) {
    return '';
  }
}
let best = null;
document.querySelectorAll('svg').forEach((root) => {
  const box = viewBox(root);
  if (!box) return;
  const rows = [];
  root.querySelectorAll('path, polygon, polyline, rect').forEach((element) => {
    const hints = [];
    let node = element;
    while (node && node !== root.parentElement) {
      ['aria-label', 'data-section', 'data-section-name', 'data-name',
       'data-testid', 'name', 'title', 'id', 'class'].forEach((attribute) => {
        const value = node.getAttribute && node.getAttribute(attribute);
        if (value) hints.push(value);
      });
      try {
        node.querySelectorAll(':scope > title, :scope > text').forEach((label) => {
          if (label.textContent) hints.push(label.textContent);
        });
      } catch (error) {
        // Older SVG DOM implementations may not support :scope.
      }
      if (node === root) break;
      node = node.parentElement;
    }
    const section = match(hints);
    const path = pathFor(element);
    if (!section || !path) return;
    rows.push({
      name: section,
      shapes: [{path, transform: relativeTransform(root, element)}],
    });
  });
  const unique = new Set(rows.map((row) => row.name)).size;
  if (!best || unique > best.unique) {
    best = {
      unique,
      source: 'vivid-dom-svg',
      source_url: sourceUrl,
      view_box: box,
      sections: rows,
    };
  }
});
return best;
"""
        try:
            raw = self.driver.execute_script(script, known_sections, source_url)
        except Exception:
            return None
        return sanitize_map_geometry(raw, known_sections)

    def capture(self, url: str) -> tuple[dict[str, Any], datetime]:
        from selenium.common.exceptions import TimeoutException

        self.driver.get_log("performance")
        try:
            self.driver.get(url)
        except TimeoutException:
            self.driver.execute_script("window.stop();")

        deadline = time.monotonic() + self.timeout
        listing_ids: set[str] = set()
        map_requests: dict[str, tuple[str, str]] = {}
        map_bodies: list[tuple[str, str, str]] = []
        event_date: datetime | None = None
        captured_payload: dict[str, Any] | None = None
        listings_ready_at: float | None = None
        map_view_opened = False

        while time.monotonic() < deadline:
            if event_date is None:
                try:
                    event_date = self._event_datetime(url)
                except Exception as exc:
                    if not event_metadata_is_still_rendering(exc):
                        raise

            for entry in self.driver.get_log("performance"):
                try:
                    message = json.loads(entry["message"])["message"]
                    method = message["method"]
                    params = message["params"]
                except (KeyError, TypeError, json.JSONDecodeError):
                    continue

                if method == "Network.responseReceived":
                    response = params.get("response") or {}
                    response_url = str(response.get("url") or "")
                    mime_type = str(response.get("mimeType") or "")
                    request_id = str(params.get("requestId") or "")
                    if "listings" in response_url.casefold():
                        listing_ids.add(request_id)
                    if self._looks_like_map_response(response_url, mime_type):
                        map_requests[request_id] = (response_url, mime_type)
                    continue

                if method != "Network.loadingFinished":
                    continue
                request_id = str(params.get("requestId") or "")
                if request_id in listing_ids:
                    payload = self._response_json(request_id)
                    if payload and payload.get("tickets") and payload.get("global"):
                        captured_payload = payload
                        listings_ready_at = listings_ready_at or time.monotonic()
                if request_id in map_requests:
                    response_url, mime_type = map_requests.pop(request_id)
                    body = self._response_text(request_id)
                    if body:
                        map_bodies.append((body, mime_type, response_url))

            if captured_payload is not None and event_date is not None:
                known_sections = sorted(
                    {
                        " ".join(str(ticket.get("l") or "").split())
                        for ticket in captured_payload.get("tickets") or []
                        if isinstance(ticket, dict) and str(ticket.get("l") or "").strip()
                    },
                    key=str.casefold,
                )
                candidates: list[Any] = [
                    extract_map_geometry_from_json(
                        captured_payload,
                        known_sections,
                        source="vivid-listings-json",
                        source_url=url,
                    )
                ]
                for body, mime_type, response_url in map_bodies:
                    candidates.append(
                        self._geometry_from_response(
                            body,
                            mime_type,
                            response_url,
                            known_sections,
                        )
                    )
                candidates.append(self._dom_map_geometry(known_sections, url))
                geometry = choose_best_geometry(candidates, known_sections)
                if geometry is not None:
                    captured_payload["_map_geometry"] = geometry
                    captured_payload["_map_geometry_diagnostics"] = {
                        "status": "captured",
                        "source": geometry.get("source"),
                        "mapped_sections": geometry_section_count(geometry),
                        "network_map_responses": len(map_bodies),
                    }
                    return captured_payload, event_date

                elapsed = time.monotonic() - (listings_ready_at or time.monotonic())
                if not map_view_opened and elapsed >= 0.5:
                    map_view_opened = self._open_map_view()
                if elapsed >= MAP_GEOMETRY_SETTLE_SECONDS:
                    captured_payload["_map_geometry_diagnostics"] = {
                        "status": "unavailable",
                        "network_map_responses": len(map_bodies),
                        "map_view_opened": map_view_opened,
                    }
                    return captured_payload, event_date

            time.sleep(0.15)

        if captured_payload is not None and event_date is not None:
            return captured_payload, event_date
        if captured_payload is not None:
            raise ValueError(
                f"Listings loaded, but the event date and time did not appear "
                f"within {self.timeout} seconds."
            )
        raise TimeoutError(f"No Vivid listings response appeared within {self.timeout} seconds.")

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


def nfl_capture_interval_hours(event_date: datetime, now: datetime) -> int | None:
    """Return the collection interval for a game at the supplied moment."""
    hours_until = (
        as_utc(event_date) - now.astimezone(timezone.utc)
    ).total_seconds() / 3600
    if not 0 < hours_until <= NFL_CAPTURE_WINDOW_HOURS:
        return None
    if hours_until <= NFL_HOURLY_WINDOW_HOURS:
        return NFL_FINAL_CADENCE_HOURS
    if hours_until <= NFL_THREE_HOUR_WINDOW_HOURS:
        return NFL_MIDDLE_CADENCE_HOURS
    return NFL_EARLY_CADENCE_HOURS


def nfl_capture_tier(event_date: datetime, now: datetime) -> str | None:
    interval = nfl_capture_interval_hours(event_date, now)
    return {
        NFL_FINAL_CADENCE_HOURS: "final_7_days_hourly",
        NFL_MIDDLE_CADENCE_HOURS: "days_8_to_14_every_3_hours",
        NFL_EARLY_CADENCE_HOURS: "days_15_to_30_every_6_hours",
    }.get(interval)


def nfl_capture_phase(cadence_key: str, interval_hours: int) -> int:
    """Assign a stable hourly phase so longer-window games are staggered."""
    if interval_hours <= 1:
        return 0
    digest = hashlib.sha256(str(cadence_key).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % interval_hours


def nfl_capture_is_due(
    event_date: datetime,
    capture_slot: datetime,
    cadence_key: str = "",
) -> bool:
    interval = nfl_capture_interval_hours(event_date, capture_slot)
    if interval is None:
        return False
    if interval == NFL_FINAL_CADENCE_HOURS:
        return True
    phase_key = cadence_key or as_utc(event_date).isoformat()
    phase = nfl_capture_phase(phase_key, interval)
    utc_hour = int(hourly_capture_slot(capture_slot).timestamp() // 3600)
    return utc_hour % interval == phase


def approximate_nfl_event_time(game: DiscoveredNFLGame) -> datetime | None:
    """Use a stable afternoon kickoff only for degraded feed-only pacing."""
    if game.date_hint is None:
        return None
    return datetime(
        game.date_hint.year,
        game.date_hint.month,
        game.date_hint.day,
        13,
        tzinfo=NEW_YORK,
    )


def adaptive_due_nfl_games(
    games: list[DiscoveredNFLGame],
    now: datetime,
    limit: int | None = None,
) -> list[DiscoveredNFLGame]:
    """Filter feed-only fallback games using the same staggered cadence."""
    capture_slot = hourly_capture_slot(now)
    eligible = upcoming_nfl_games(
        games,
        now,
        horizon_days=DISCOVERY_HORIZON_DAYS,
    )
    due = []
    for game in eligible:
        approximate_date = approximate_nfl_event_time(game)
        if approximate_date is None:
            continue
        if nfl_capture_is_due(approximate_date, capture_slot, game.url):
            due.append(game)
    return due if limit is None else due[:limit]


def nfl_is_within_capture_window(event_date: datetime, now: datetime) -> bool:
    hours_until = (as_utc(event_date) - now.astimezone(timezone.utc)).total_seconds() / 3600
    return 0 < hours_until <= NFL_CAPTURE_WINDOW_HOURS


def nfl_snapshot_to_payload(
    url: str,
    event_date: datetime,
    captured_at: datetime,
    snapshot: EventSnapshot,
    *,
    schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = snapshot_to_payload(url, event_date, captured_at, snapshot)
    payload["event_type"] = "nfl"
    section_names = [row.section for row in snapshot.sections]
    geometry = sanitize_map_geometry(
        getattr(snapshot, "map_geometry", None),
        section_names,
    )
    if geometry is not None:
        payload["map_geometry"] = geometry
    if schedule:
        payload["schedule"] = schedule
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
                "timezone": "America/New_York",
                "captured_at": eastern_iso(captured_at),
                "event_date": eastern_iso(event_date),
                "source_url": game.url,
                "source_id": snapshot.source_id,
                "title": snapshot.title,
                "venue": snapshot.venue,
                "section_count": len(snapshot.sections),
                "lowest_section_price": min(row.price for row in snapshot.sections),
                "highest_section_price": max(row.price for row in snapshot.sections),
                "map_geometry_source": (
                    snapshot.map_geometry.get("source")
                    if isinstance(snapshot, NFLEventSnapshot) and snapshot.map_geometry
                    else None
                ),
                "map_geometry_sections": geometry_section_count(
                    getattr(snapshot, "map_geometry", None)
                ),
                "map_geometry": getattr(snapshot, "map_geometry", None),
                "map_geometry_diagnostics": raw_payload.get(
                    "_map_geometry_diagnostics"
                ),
                "sections": [asdict(row) for row in snapshot.sections],
            }
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            print(
                f"NFL SMOKE TEST PASSED: {len(snapshot.sections)} sections for "
                f"{snapshot.title} at {snapshot.venue}; "
                f"{result['map_geometry_sections']} provider polygons captured.",
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
                "timezone": "America/New_York",
                "captured_at": eastern_iso(datetime.now(timezone.utc)),
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
    capture_slot = hourly_capture_slot(started_at)
    due_games = adaptive_due_nfl_games(discovered, capture_slot)
    in_window = upcoming_nfl_games(
        discovered,
        started_at,
        horizon_days=DISCOVERY_HORIZON_DAYS,
    )
    undated = sum(game.date_hint is None for game in discovered)
    print(
        f"{len(due_games)} of {len(in_window)} NFL games inside the 30-day "
        f"window are due in this staggered cadence slot "
        f"({undated} discovered links had no date hint).",
        flush=True,
    )

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
                    f"SKIP: NFL game is outside the exact 30-day window: {game.url}",
                    flush=True,
                )
                continue
            if not nfl_capture_is_due(event_date, capture_slot, game.url):
                skipped += 1
                print(
                    f"SKIP: NFL game is not due in this adaptive cadence slot: {game.url}",
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
                            "map_geometry_sections": geometry_section_count(
                                getattr(snapshot, "map_geometry", None)
                            ),
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
        "timezone": "America/New_York",
        "cadence": "adaptive: 6h from days 15-30, 3h from days 8-14, hourly in final 7 days",
        "capture_window_hours": NFL_CAPTURE_WINDOW_HOURS,
        "started_at": eastern_iso(started_at),
        "capture_slot": eastern_iso(capture_slot),
        "feed": NFL_FEED_URL,
        "discovered": len(discovered),
        "undated": undated,
        "in_window": len(in_window),
        "due": len(due_games),
        "skipped_by_cadence": len(in_window) - len(due_games),
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
