"""Opt-in stock WebKit capture of native public inventory, one browser per game.

Playwright is imported only when this engine is explicitly selected. Discovery
reads public rendered DOM; capture follows an observed team link into its native
popup. No headers, profiles, request interception, or constructed fetches.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
from importlib.metadata import version
import json
import math
import re
import time
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from collector import as_utc, parse_iso_datetime, validated_vivid_url
from nfl_metadata import canonical_venue_name, extract_map_geometry_from_json, geometry_is_usable, geometry_section_count
from vivid_firefox import native_unfiltered_request, validate_full_inventory, validated_performer_url
from vivid_inventory import INVENTORY_PATHS, MAX_INVENTORY_BYTES, VividCaptureError, http_category

DENIALS = {401, 403, 429}
_blocked_category = None
SAFE_QUERY = {"productionId", "includeIpAddress", "currency", "localizeCurrency", "priceGroupId",
              "quantity", "recommended", "sf", "offset", "page", "sort", "scarcity"}
PUBLIC_PAGE_SCRIPT = """() => {
 const text = document.body?.innerText || '';
 let event = null;
 try {
  const n = JSON.parse(document.getElementById('__NEXT_DATA__').textContent);
  const p = n.props.pageProps, e = p.initialProductionDetailsData.data;
  event = {id:e.id,page_id:p.id,query_id:n.query?.id,utc_date:e.utcDate,
           title:e.name,venue:e.venue?.name,venue_id:e.venue?.id};
 } catch (_) {}
 return {event, challenge_visible:/(verify (you are|you're) human|access denied|unusual activity|captcha|too many requests)/i.test(text)};
}"""


def stop_after_denial(diagnostics):
    if _blocked_category is not None:
        raise VividCaptureError(_blocked_category, diagnostics)


def remember_denial(category):
    global _blocked_category
    _blocked_category = category


def event_url_matches(url, production_id):
    if not isinstance(url, str):
        return False
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.netloc == "www.vividseats.com"
            and not parsed.query and not parsed.fragment
            and parsed.path.rstrip("/").endswith("/production/" + production_id))


def public_inventory(payload, production_id):
    """Validate every native row before returning the existing public parser keys."""
    validate_full_inventory(payload, production_id)
    global_keys = {"productionId", "productionName", "mapTitle", "currency", "currencyCode",
                   "productionDate", "eventDate", "venueName", "venueId", "venueTimeZone",
                   "venueCountry", "listingCount"}
    metadata = {key: value for key, value in payload["global"][0].items() if key in global_keys
                and isinstance(value, (str, int, float)) and not isinstance(value, bool)
                and len(str(value)) <= 500}
    rows = []
    for ticket in payload["tickets"]:
        if len(ticket["l"]) > 200:
            raise VividCaptureError("invalid-inventory-ticket", {"production_id": production_id})
        row = {"l": ticket["l"], "p": ticket["p"], "q": ticket["q"]}
        for key in ("r", "aip"):
            value = ticket.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool) and len(str(value)) <= 100:
                if key == "aip":
                    try:
                        if not math.isfinite(float(value)) or float(value) < 0:
                            raise ValueError()
                    except (TypeError, ValueError, OverflowError):
                        raise VividCaptureError("invalid-inventory-ticket", {"production_id": production_id}) from None
                row[key] = value
        tags = ticket.get("tags")
        if isinstance(tags, list) and all(isinstance(tag, str) and len(tag) <= 100 for tag in tags):
            row["tags"] = tags
        rows.append(row)
    return {"global": [metadata], "tickets": rows}


def _nhl_venue_identity(value):
    # These exact provider/official aliases were observed in the accepted NHL
    # capture set. Do not weaken the identity check with substring matching.
    value = canonical_venue_name(value).casefold()
    return {"sap center": "sap center at san jose", "bell centre": "centre bell"}.get(value, value)


def validated_nhl_context(context, expected):
    """Accept only explicit official NHL identity supplied by the schedule owner."""
    from nhl_collector import NHL_TEAM_NAMES
    fields = {"sport", "schedule_id", "event_date", "away_team", "home_team", "venue", "venue_timezone"}
    if (not isinstance(context, dict) or set(context) != fields or context.get("sport") != "nhl"
            or not isinstance(context.get("schedule_id"), str)
            or not re.fullmatch(r"[0-9]{10}", context["schedule_id"])
            or context.get("away_team") not in NHL_TEAM_NAMES or context.get("home_team") not in NHL_TEAM_NAMES
            or context["away_team"] == context["home_team"]
            or any(not isinstance(context.get(key), str) or not context[key].strip()
                   for key in ("event_date", "venue", "venue_timezone"))):
        raise ValueError("Trusted NHL context requires the complete official game identity")
    stamp = parse_iso_datetime(context["event_date"])
    if (stamp is None or stamp.tzinfo is None or not re.search(r"(?:Z|[+-][0-9]{2}:?[0-9]{2})$", context["event_date"])
            or as_utc(stamp) != expected):
        raise ValueError("Trusted NHL context must identify the configured official UTC")
    ZoneInfo(context["venue_timezone"])
    return dict(context)


def verified_event_date(metadata, payload, production_id, expected, *, official_game=None, diagnostics=None):
    if (not isinstance(metadata, dict) or str(metadata.get("id")) != production_id
            or str(metadata.get("page_id")) != production_id
            or (metadata.get("query_id") is not None and str(metadata["query_id"]) != production_id)):
        raise VividCaptureError("event-metadata-identity-mismatch", {"production_id": production_id})
    raw = metadata.get("utc_date")
    if not isinstance(raw, str) or not re.search(r"(?:Z|[+-][0-9]{2}:?[0-9]{2})$", raw):
        raise VividCaptureError("event-metadata-time-mismatch", {"production_id": production_id})
    stamp = parse_iso_datetime(raw)
    if stamp is None or stamp.tzinfo is None:
        raise VividCaptureError("event-metadata-time-mismatch", {"production_id": production_id})
    global_row = payload["global"][0]
    for field, key in (("title", "productionName"), ("venue", "mapTitle")):
        value = metadata.get(field)
        if (not isinstance(value, str) or not value.strip()
                or " ".join(value.split()) != " ".join(str(global_row.get(key) or "").split())):
            raise VividCaptureError("event-metadata-identity-mismatch", {"production_id": production_id})
    if not metadata.get("venue_id") or str(global_row.get("venueId")) != str(metadata["venue_id"]):
        raise VividCaptureError("event-metadata-identity-mismatch", {"production_id": production_id})
    if official_game is not None:
        try:
            context = validated_nhl_context(official_game, expected)
        except (ValueError, TypeError, KeyError) as exc:
            raise VividCaptureError("event-metadata-identity-mismatch", {"production_id": production_id}) from exc
        from nhl_collector import ordered_matchup_from_title
        if (ordered_matchup_from_title(metadata["title"]) != (context["away_team"], context["home_team"])
                or _nhl_venue_identity(metadata["venue"]) != _nhl_venue_identity(context["venue"])):
            raise VividCaptureError("event-metadata-identity-mismatch", {"production_id": production_id})
        zone = ZoneInfo(context["venue_timezone"])
        if as_utc(stamp).astimezone(zone).date() != expected.astimezone(zone).date():
            raise VividCaptureError("event-metadata-time-mismatch", {"production_id": production_id})
        if diagnostics is not None:
            diagnostics["event_time_validation"] = dict(policy="official-nhl-identity-calendar",
                schedule_id=context["schedule_id"], provider_utc=as_utc(stamp).isoformat(),
                official_utc=expected.isoformat(), venue_timezone=context["venue_timezone"],
                difference_seconds=(as_utc(stamp) - expected).total_seconds())
        return expected
    if as_utc(stamp) != expected:
        raise VividCaptureError("event-metadata-time-mismatch", {"production_id": production_id})
    return as_utc(stamp)


class PublicDOMDriver:
    """The small rendered-DOM interface used by existing discovery/search code."""

    def __init__(self, session):
        self.session = session

    def get(self, url):
        from selenium.common.exceptions import TimeoutException
        stop_after_denial({"engine": "webkit", "phase": "discovery"})
        page = self.session.discovery_page()
        try:
            response = page.goto(url, wait_until="load", timeout=self.session.timeout * 1000)
        except self.session.timeout_error as exc:
            raise TimeoutException("WebKit public discovery navigation timed out") from exc
        if response is not None and response.status in DENIALS:
            remember_denial(http_category(response.status))
            raise VividCaptureError(http_category(response.status), {"engine": "webkit", "phase": "discovery"})
        if page.evaluate(PUBLIC_PAGE_SCRIPT).get("challenge_visible"):
            remember_denial("provider-access-denied")
            raise VividCaptureError("provider-access-denied", {"engine": "webkit", "phase": "discovery"})

    @property
    def page_source(self):
        return self.session.discovery_page().content()

    def execute_script(self, script, *args):
        return self.session.discovery_page().evaluate(
            "args => (function() {" + script + "}).apply(null, args)", list(args))

    def save_screenshot(self, path):
        page = self.session.event_page or self.session.discovery_page()
        page.screenshot(path=path)
        return True

    @property
    def current_url(self):
        page = self.session.event_page or self.session.discovery_page()
        return page.url


class WebKitInventorySession:
    def __init__(self, owner, *, headless, timeout):
        stop_after_denial({"engine": "webkit", "phase": "launch"})
        from playwright.sync_api import sync_playwright, TimeoutError
        self.owner, self.headless, self.timeout = owner, headless, timeout
        self.timeout_error = TimeoutError
        self.playwright = sync_playwright().start()
        self.runtime = {"engine": "webkit", "playwright": version("playwright"), "headed": not headless}
        self.routes, self.expected_dates, self.official_games = {}, {}, {}
        self.event_browser = self.event_context = self.event_page = self.event_pid = None
        self.discovery_browser = self._discovery_page = None
        self.generation = 0
        owner.driver = PublicDOMDriver(self)

    def configure_normal_navigation(self, performer_urls, expected_event_dates, *, official_games=None):
        if (not isinstance(performer_urls, dict) or not performer_urls
                or not isinstance(expected_event_dates, dict) or set(expected_event_dates) != set(performer_urls)):
            raise ValueError("WebKit requires explicit public routes and official UTC dates")
        for pid, url in performer_urls.items():
            if not isinstance(pid, str) or not re.fullmatch(r"[0-9]{1,12}", pid):
                raise ValueError("Invalid route production ID")
            validated_performer_url(url)
            stamp = expected_event_dates[pid]
            if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("Expected event dates must have explicit UTC offsets")
        dates = {pid: as_utc(stamp) for pid, stamp in expected_event_dates.items()}
        if official_games is not None and (not isinstance(official_games, dict) or set(official_games) != set(performer_urls)):
            raise ValueError("Trusted NHL contexts must match the configured production IDs")
        contexts = {pid: validated_nhl_context(context, dates[pid]) for pid, context in (official_games or {}).items()}
        self.routes, self.expected_dates, self.official_games = dict(performer_urls), dates, contexts

    def new_browser(self):
        stop_after_denial({"engine": "webkit", "phase": "launch"})
        browser = self.playwright.webkit.launch(headless=self.headless, timeout=min(30000, self.timeout * 1000))
        try:
            context = browser.new_context()
            context.set_default_timeout(min(10000, self.timeout * 1000))
            context.set_default_navigation_timeout(min(20000, self.timeout * 1000))
            return browser, context
        except Exception:
            browser.close()
            raise

    def discovery_page(self):
        if self._discovery_page is None:
            self.discovery_browser, context = self.new_browser()
            self._discovery_page = context.new_page()
        return self._discovery_page

    def close_event(self):
        browser = self.event_browser
        self.event_browser = self.event_context = self.event_page = self.event_pid = None
        if browser is not None:
            browser.close()

    def close(self):
        try:
            self.close_event()
        finally:
            try:
                if self.discovery_browser is not None:
                    self.discovery_browser.close()
            finally:
                self.discovery_browser = self._discovery_page = None
                self.playwright.stop()

    def capture(self, url, *, reload_page=False):
        url = validated_vivid_url(url)
        pid = urlsplit(url).path.rstrip("/").split("/")[-1]
        diagnostics = {"engine": "webkit", "runtime": dict(self.runtime), "production_id": pid,
                       "responses": [], "inventory_view_actions": [], "acquisition_method": "original-response-playwright",
                       "navigation_mode": "performer", "fresh_browser": not reload_page}
        self.owner.capture_diagnostics = diagnostics
        stop_after_denial(diagnostics)
        if pid not in self.routes or pid not in self.expected_dates:
            raise VividCaptureError("event-route-not-configured", diagnostics)
        if not event_url_matches(url, pid):
            raise ValueError("WebKit requires an exact public production URL")
        if reload_page:
            if self.event_pid != pid or self.event_page is None or not event_url_matches(self.event_page.url, pid):
                raise VividCaptureError("reload-event-identity-mismatch", diagnostics)
        else:
            self.close_event()
            self.event_browser, self.event_context = self.new_browser()
            self.event_pid = pid
        diagnostics["runtime"]["browser_version"] = self.event_browser.version
        context = self.event_context
        self.generation += 1
        generation = self.generation
        candidates, finished = deque(maxlen=32), set()
        gate, denied, failure = [None], [None], [None]
        deadline = time.monotonic() + self.timeout
        retain_for_recovery = False

        def observe(response):
            if generation != self.generation:
                return
            parsed, request = urlsplit(response.url), response.request
            if parsed.scheme != "https" or parsed.netloc != "www.vividseats.com":
                return
            status = response.status
            if status in DENIALS:
                denied[0] = status
                remember_denial(http_category(status))
            query = parse_qs(parsed.query, keep_blank_values=True)
            target = parsed.path in INVENTORY_PATHS and request.method == "GET" and query.get("productionId") == [pid]
            started = request.timing.get("startTime")
            after = (gate[0] is not None and isinstance(started, (int, float))
                     and math.isfinite(started) and started >= gate[0])
            if request.resource_type == "document" and event_url_matches(response.url, pid) and after:
                diagnostics["document_status"] = status
            if not target or not after:
                return
            safe = {"path": parsed.path, "status": status, "method": request.method,
                    "query": {key: values[0] for key, values in query.items() if key in SAFE_QUERY
                              and len(values) == 1 and re.fullmatch(r"[A-Za-z0-9.,_-]{1,40}", values[0])}}
            diagnostics["responses"] = (diagnostics["responses"] + [safe])[-30:]
            if status >= 400:
                failure[0] = (status, time.monotonic())
            elif status == 200:
                if native_unfiltered_request(response.url, pid):
                    candidates.append(response)
                else:
                    diagnostics["filtered_responses_rejected"] = diagnostics.get("filtered_responses_rejected", 0) + 1

        def complete(request):
            if generation != self.generation or len(finished) >= 32:
                return
            started = request.timing.get("startTime")
            if (native_unfiltered_request(request.url, pid) and request.method == "GET"
                    and gate[0] is not None and isinstance(started, (int, float))
                    and math.isfinite(started) and started >= gate[0]):
                finished.add(request)

        def check(page):
            stop_after_denial(diagnostics)
            state = page.evaluate(PUBLIC_PAGE_SCRIPT)
            if denied[0] is not None:
                raise VividCaptureError(http_category(denied[0]), diagnostics)
            if state.get("challenge_visible"):
                remember_denial("provider-access-denied")
                raise VividCaptureError("provider-access-denied", diagnostics)
            return state

        context.on("response", observe)
        context.on("requestfinished", complete)
        try:
            if reload_page:
                gate[0] = time.time() * 1000
                self.event_page.reload(wait_until="domcontentloaded")
                diagnostics["reload_same_browser"] = True
            else:
                page = context.new_page()
                page.goto(self.routes[pid], wait_until="load")
                popup = None
                while popup is None and time.monotonic() < deadline:
                    check(page)
                    links = page.locator(f"a[href*='/production/{pid}']")
                    for index in range(min(links.count(), 50)):
                        link = links.nth(index)
                        href = link.evaluate("node => node.href")
                        if (event_url_matches(href, pid) and link.get_attribute("target") == "_blank"
                                and link.is_visible() and link.is_enabled()):
                            check(page)
                            gate[0] = time.time() * 1000
                            with page.expect_popup(timeout=min(10000, max(1, (deadline-time.monotonic()) * 1000))) as opened:
                                link.click()
                            popup = opened.value
                            popup.wait_for_load_state("domcontentloaded")
                            if not event_url_matches(popup.url, pid):
                                raise VividCaptureError("event-navigation-identity-mismatch", diagnostics)
                            break
                    if popup is None:
                        page.wait_for_timeout(100)
                if popup is None:
                    raise VividCaptureError("event-link-not-found", diagnostics)
                self.event_page = popup
                diagnostics.update(visible_event_link_clicked=True, event_opened_native_popup=True,
                                   event_path=urlsplit(popup.url).path)
            page = self.event_page
            payload = None
            while time.monotonic() < deadline:
                state = check(page)
                for response in list(candidates):
                    if response.request.frame.page is not page:
                        candidates.remove(response)
                        continue
                    if response.request not in finished:
                        continue
                    candidates.remove(response)
                    body = response.body()
                    if not isinstance(body, bytes) or len(body) > MAX_INVENTORY_BYTES:
                        raise VividCaptureError("unexpected-inventory-payload", diagnostics)
                    raw = json.loads(body)
                    try:
                        payload = public_inventory(raw, pid)
                    except VividCaptureError as exc:
                        raise VividCaptureError(exc.category, diagnostics) from exc
                    diagnostics["listing_count"] = len(payload["tickets"])
                    sections = sorted({row["l"] for row in payload["tickets"]}, key=str.casefold)
                    geometry = extract_map_geometry_from_json(raw, sections, source="vivid-listings-json", source_url=url)
                    if geometry is not None:
                        payload["_map_geometry"] = geometry
                    payload["_map_geometry_diagnostics"] = {
                        "status": "captured" if geometry_is_usable(geometry, sections) else "partial" if geometry else "unavailable",
                        "source": geometry.get("source") if geometry else None,
                        "mapped_sections": geometry_section_count(geometry),
                        "coverage_ratio": geometry.get("coverage_ratio") if geometry else 0,
                    }
                if payload is not None and state.get("event") is not None:
                    try:
                        stamp = verified_event_date(state["event"], payload, pid, self.expected_dates[pid],
                            official_game=self.official_games.get(pid), diagnostics=diagnostics)
                    except VividCaptureError as exc:
                        raise VividCaptureError(exc.category, diagnostics) from exc
                    return payload, stamp
                if (payload is None and failure[0] is not None and not candidates
                        and time.monotonic() - failure[0][1] >= 5):
                    status = failure[0][0]
                    retain_for_recovery = status == 404
                    raise VividCaptureError(http_category(status), diagnostics, retryable=status >= 500)
                page.wait_for_timeout(100)
            if payload is not None:
                raise VividCaptureError("event-metadata-timeout", diagnostics)
            if failure[0] is not None and not candidates:
                status = failure[0][0]
                retain_for_recovery = status == 404
                raise VividCaptureError(http_category(status), diagnostics, retryable=status >= 500)
            category = "filtered-inventory-only" if diagnostics.get("filtered_responses_rejected") else "provider-inventory-timeout"
            raise VividCaptureError(category, diagnostics, retryable=category == "provider-inventory-timeout")
        except self.timeout_error as exc:
            raise VividCaptureError("provider-inventory-timeout", diagnostics, retryable=True) from exc
        finally:
            self.generation += 1
            context.remove_listener("response", observe)
            context.remove_listener("requestfinished", complete)
            # A current-tier404 may receive the existing single same-browser
            # recovery reload. All other errors release this owned process.
            import sys
            if sys.exc_info()[0] is not None and not retain_for_recovery:
                self.close_event()
