"""Opt-in stock Firefox capture of completed, native public Vivid responses.

Firefox-specific Selenium imports are lazy: ordinary Chrome and web startup
continue to work with their existing dependencies. No request interception,
constructed requests, imported profiles, custom headers, or database calls.
"""
from __future__ import annotations

import base64
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from collector import as_utc, parse_iso_datetime, validated_vivid_url
from nfl_metadata import choose_best_geometry, extract_map_geometry_from_json, geometry_is_usable, geometry_section_count
from vivid_inventory import MAX_INVENTORY_BYTES, VividCaptureError, http_category, inventory_request, unfiltered_request, validate_inventory

MAX_MAP_BYTES = 8_000_000
MAX_MAP_RESPONSES = 24
MAP_SETTLE_SECONDS = 2.5
SAFE_HEADER_NAMES = {"accept", "brand-name", "if-none-match", "if-modified-since", "cache-control", "content-type"}
SAFE_QUERY_NAMES = {"productionId", "quantity", "recommended", "sf", "currency", "priceGroupId", "localizeCurrency", "includeIpAddress"}
FULL_INVENTORY_QUERY_NAMES = SAFE_QUERY_NAMES | {"offset", "page", "sort", "scarcity"}
LINK_SCROLL_SCRIPT = "arguments[0].scrollIntoView({block:'center', inline:'nearest'});"
NORMAL_CHALLENGE_SCRIPT = "return /(verify (you are|you're) human|access denied|unusual activity|captcha|too many requests)/i.test(document.body?.innerText || '');"


def _event_url_matches(url, production_id):
    if not isinstance(url, str):
        return False
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.netloc == "www.vividseats.com"
            and not parsed.query and not parsed.fragment
            and parsed.path.rstrip("/").endswith("/production/" + production_id))


def validated_performer_url(url):
    if not isinstance(url, str):
        raise ValueError("Normal navigation requires an explicit public performer URL")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "www.vividseats.com"
            or parsed.query or parsed.fragment or parsed.username or parsed.password
            or not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*performer/[0-9]{1,12}/?", parsed.path)):
        raise ValueError("Invalid public performer URL")
    return url


def configure_normal_navigation(browser, performer_urls, expected_event_dates=None):
    """Configure only explicit public routes on an already-owned Firefox browser."""
    session = getattr(browser, "_firefox_session", None)
    if not isinstance(session, FirefoxInventorySession):
        raise ValueError("Normal navigation requires the Firefox adapter")
    session.configure_normal_navigation(performer_urls, expected_event_dates)


def native_unfiltered_request(url: str, production_id: str) -> bool:
    if not inventory_request(url) or not unfiltered_request(url):
        return False
    query = parse_qs(urlsplit(url).query, keep_blank_values=True)
    if set(query) - FULL_INVENTORY_QUERY_NAMES or any(len(values) != 1 or not values[0] for values in query.values()):
        return False
    if query.get("productionId") != [production_id]:
        return False
    for key in ("quantity", "offset", "page"):
        if key in query and query[key] != ["0"]:
            return False
    for key in ("recommended", "sf", "scarcity"):
        if key in query and any(value.casefold() not in {"false", "0"} for value in query[key]):
            return False
    for key in ("includeIpAddress", "localizeCurrency"):
        if key in query and query[key][0].casefold() not in {"true", "false", "0", "1"}:
            return False
    if "priceGroupId" in query and not re.fullmatch(r"[0-9]{1,12}", query["priceGroupId"][0]):
        return False
    if "currency" in query and not re.fullmatch(r"[A-Z]{3}", query["currency"][0]):
        return False
    if "sort" in query and not re.fullmatch(r"[A-Za-z0-9_-]{1,30}", query["sort"][0]):
        return False
    return True


def validate_full_inventory(payload: dict, production_id: str) -> None:
    if not isinstance(payload, dict):
        raise VividCaptureError("unexpected-inventory-payload", {"production_id": production_id})
    validate_inventory(payload, production_id)
    count = payload["global"][0].get("listingCount")
    if (isinstance(count, bool) or not isinstance(count, (str, int))
            or not re.fullmatch(r"[0-9]+", str(count)) or int(count) != len(payload["tickets"])
            or any(not isinstance(row, dict) for row in payload["tickets"])):
        raise VividCaptureError("incomplete-inventory", {"production_id": production_id})
    for row in payload["tickets"]:
        section, price, quantity = row.get("l"), row.get("p"), row.get("q")
        valid = isinstance(section, str) and bool(section.strip()) and not isinstance(price, bool)
        try:
            numeric = Decimal(str(price))
            valid = valid and numeric.is_finite() and numeric >= 0
        except (InvalidOperation, TypeError, ValueError):
            valid = False
        if (not valid or isinstance(quantity, bool) or not isinstance(quantity, (str, int))
                or not re.fullmatch(r"[0-9]+", str(quantity)) or int(quantity) <= 0):
            raise VividCaptureError("invalid-inventory-ticket", {"production_id": production_id})


def decode_response(result: dict, maximum: int = MAX_INVENTORY_BYTES) -> str:
    value = result.get("bytes", {})
    if not isinstance(value, dict) or not isinstance(value.get("value"), str) or len(value["value"]) > maximum * 2:
        raise ValueError("Invalid or oversized BiDi body")
    if value.get("type") == "string":
        raw = value["value"].encode("utf-8")
    elif value.get("type") == "base64":
        raw = base64.b64decode(value["value"], validate=True)
    else:
        raise ValueError("Unsupported BiDi body encoding")
    if len(raw) > maximum:
        raise ValueError("Oversized BiDi body")
    return raw.decode("utf-8")


EVENT_METADATA_SCRIPT = """
const node = document.getElementById('__NEXT_DATA__');
if (!node) return null;
try {
 const p = JSON.parse(node.textContent).props?.pageProps;
 const e = p?.initialProductionDetailsData?.data;
 return e ? {id:e.id, page_id:p.id, utc_date:e.utcDate} : null;
} catch (_) { return null; }
"""


def event_datetime(driver, production_id: str) -> datetime | None:
    data = driver.execute_script(EVENT_METADATA_SCRIPT)
    if not isinstance(data, dict) or str(data.get("id") or "") != production_id:
        return None
    if data.get("page_id") is not None and str(data["page_id"]) != production_id:
        return None
    raw = str(data.get("utc_date") or "")
    if not re.search(r"(?:Z|[+-][0-9]{2}:?[0-9]{2})$", raw):
        return None
    parsed = parse_iso_datetime(raw)
    return as_utc(parsed) if parsed is not None else None


def _header_names(items):
    return sorted({row["name"].casefold() for row in items if isinstance(row, dict)
                   and isinstance(row.get("name"), str) and row["name"].casefold() in SAFE_HEADER_NAMES})


def _safe_response(row):
    query = parse_qs(urlsplit(row["url"]).query)
    result = {"path": urlsplit(row["url"]).path, "status": row["status"], "method": row["method"],
              "query": {key: values[0] for key, values in query.items() if key in SAFE_QUERY_NAMES and len(values) == 1
                        and re.fullmatch(r"[A-Za-z0-9.-]{1,24}", values[0])},
              "request_header_names": row["request_header_names"], "response_header_names": row["response_header_names"]}
    if row["protocol"] in {"h3", "h2", "http/1.1", "http/2", "http/3"}:
        result["protocol"] = row["protocol"]
    if isinstance(row["from_cache"], bool):
        result["from_cache"] = row["from_cache"]
    return result


class FirefoxInventorySession:
    """Driver/session delegate retained behind the existing browser class."""

    def __init__(self, owner, *, headless: bool, timeout: int):
        from selenium import webdriver, __version__ as selenium_version
        from selenium.webdriver.firefox.options import Options
        from selenium.webdriver.firefox.service import Service

        version = tuple(int(part) for part in selenium_version.split(".")[:2])
        if version < (4, 50):
            raise RuntimeError("Firefox capture requires requirements-collector.txt (Selenium4.50 or later).")
        binary = os.environ.get("FIREFOX_BINARY_PATH") or shutil.which("firefox")
        executable = os.environ.get("GECKODRIVER_PATH") or shutil.which("geckodriver")
        if not binary or not executable:
            raise RuntimeError("Firefox capture requires installed Firefox and geckodriver.")
        self.owner, self.timeout = owner, timeout
        self._generation, self._lock = 0, threading.Lock()
        self._normal_routes, self._expected_dates = {}, {}
        self._route_windows, self._route_base = set(), None
        options = Options()
        options.binary_location, options.enable_bidi = binary, True
        if headless:
            options.add_argument("-headless")
        service = Service(executable_path=executable, log_output=subprocess.DEVNULL)
        driver = None
        try:
            driver = webdriver.Firefox(options=options, service=service)
            owner.driver = driver
            config = getattr(driver.command_executor, "_client_config", None)
            if config is not None:
                config.timeout = timeout + 5
            else:
                driver.command_executor.set_timeout(timeout + 5)
            driver.set_page_load_timeout(timeout)
            driver.set_script_timeout(min(timeout, 20))
            self.network = driver.network
            self.runtime = {"engine": "firefox", "selenium": selenium_version, "headed": not headless,
                            "browser_version": driver.capabilities.get("browserVersion"),
                            "driver_version": driver.capabilities.get("moz:geckodriverVersion")}
        except Exception:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass
            try:
                service.stop()
            except Exception:
                pass
            raise

    def configure_normal_navigation(self, performer_urls, expected_event_dates=None):
        if not isinstance(performer_urls, dict) or not performer_urls:
            raise ValueError("Normal navigation requires explicit production routes")
        routes = {}
        for pid, performer in performer_urls.items():
            if not isinstance(pid, str) or not re.fullmatch(r"[0-9]{1,12}", pid):
                raise ValueError("Invalid route production ID")
            routes[pid] = validated_performer_url(performer)
        dates = expected_event_dates or {}
        if not isinstance(dates, dict) or set(dates) - set(routes):
            raise ValueError("Expected event dates must match configured routes")
        if any(not isinstance(stamp, datetime) or stamp.tzinfo is None for stamp in dates.values()):
            raise ValueError("Expected event dates must have explicit UTC offsets")
        self._normal_routes = routes
        self._expected_dates = {pid: as_utc(stamp) for pid, stamp in dates.items()}
        self._route_windows = getattr(self, "_route_windows", set())
        self._route_base = getattr(self, "_route_base", None)

    def _prepare_route_window(self):
        driver = self.owner.driver
        tracked = getattr(self, "_route_windows", set())
        for handle in tuple(tracked):
            if handle in driver.window_handles:
                driver.switch_to.window(handle)
                driver.close()
            tracked.discard(handle)
        base = getattr(self, "_route_base", None)
        if base is not None and base in driver.window_handles:
            driver.switch_to.window(base)
        self._route_base = driver.current_window_handle
        self._route_windows = tracked

    def _request_started_callback(self, generation, production_id, gate, requested):
        def before_request(event):
            try:
                data = event if isinstance(event, dict) else vars(event)
                request = data.get("request") or {}
                url = request.get("url")
                when = (request.get("timings") or {}).get("requestTime")
                with self._lock:
                    current = generation == self._generation
                if (not current or gate[0] is None or not isinstance(url, str)
                        or not isinstance(when, (int, float)) or not math.isfinite(when) or when < gate[0]):
                    return
                if (_event_url_matches(url, production_id) or (inventory_request(url)
                        and parse_qs(urlsplit(url).query).get("productionId") == [production_id])):
                    requested.set()
            except (AttributeError, TypeError, ValueError):
                return
        return before_request

    def _navigate_normal(self, performer, production_id, deadline, diagnostics, gate, requested, navigation_guard):
        from selenium.common.exceptions import ElementNotInteractableException, StaleElementReferenceException, TimeoutException
        driver = self.owner.driver
        try:
            driver.get(performer)
        except TimeoutException:
            diagnostics["performer_navigation_timeout"] = True
        diagnostics["navigation_mode"] = "performer"
        def stop_if_denied():
            status = navigation_guard.get("denial")
            if status is not None:
                diagnostics["performer_document_status"] = status
                raise VividCaptureError(http_category(status), diagnostics)
            if driver.execute_script(NORMAL_CHALLENGE_SCRIPT) is True:
                diagnostics["challenge_visible"] = True
                raise VividCaptureError("provider-access-denied", diagnostics)
        attempts = 0
        while time.monotonic() < deadline:
            stop_if_denied()
            candidates = driver.find_elements("css selector", f"a[href*='/production/{production_id}']")[:50]
            for link in candidates:
                before = set(driver.window_handles)
                try:
                    href = link.get_attribute("href")
                    if not isinstance(href, str) or not _event_url_matches(urljoin(performer, href), production_id):
                        continue
                    target = link.get_attribute("target")
                    if target not in (None, "", "_self", "_blank") or not link.is_displayed() or not link.is_enabled():
                        continue
                    driver.execute_script(LINK_SCROLL_SCRIPT, link)
                    time.sleep(0.25)
                    stop_if_denied()
                    gate[0] = time.time() * 1000
                    requested.clear()
                    attempts += 1
                    diagnostics["event_link_click_attempts"] = attempts
                    link.click()
                    diagnostics["visible_event_link_clicked"] = True
                    window_deadline = min(deadline, time.monotonic() + 10)
                    while time.monotonic() < window_deadline:
                        new = [handle for handle in driver.window_handles if handle not in before]
                        self._route_windows.update(new)
                        if target == "_blank":
                            for handle in new:
                                driver.switch_to.window(handle)
                                if _event_url_matches(driver.current_url, production_id):
                                    diagnostics["event_opened_new_window"] = True
                                    return
                        elif _event_url_matches(driver.current_url, production_id):
                            return
                        time.sleep(0.15)
                    raise VividCaptureError("event-link-navigation-timeout", diagnostics)
                except (ElementNotInteractableException, StaleElementReferenceException) as exc:
                    diagnostics["last_link_error_type"] = type(exc).__name__
                    new = set(driver.window_handles) - before
                    self._route_windows.update(new)
                    if requested.is_set() or new or _event_url_matches(driver.current_url, production_id):
                        raise VividCaptureError("event-link-navigation-error", diagnostics) from exc
                    if attempts >= 3:
                        raise VividCaptureError("event-link-not-interactable", diagnostics) from exc
                    # Reselect on the next pass; do not reuse a stale element or force a click.
                    break
            time.sleep(0.15)
        raise VividCaptureError("visible-event-link-not-found", diagnostics)

    def close(self):
        with self._lock:
            self._generation += 1
        driver = self.owner.driver
        service = getattr(driver, "service", None)
        try:
            driver.quit()
        finally:
            if service is not None:
                try:
                    service.stop()
                except Exception:
                    pass

    def _callback(self, generation, started_ms, events, performer=None, navigation_guard=None):
        def response(event):
            try:
                data = event if isinstance(event, dict) else vars(event)
                request, reply = data.get("request") or {}, data.get("response") or {}
                url, request_id = request.get("url"), request.get("request")
                status = reply.get("status")
                timing = request.get("timings") or {}
                request_time = timing.get("requestTime")
                with self._lock:
                    if generation != self._generation:
                        return
                # This additionally excludes in-flight requests from the previous
                # page when the new subscription observes their late completion.
                if (not isinstance(url, str) or not isinstance(request_id, str) or not isinstance(status, int)
                        or not isinstance(request_time, (int, float)) or not math.isfinite(request_time)
                        or request_time < started_ms):
                    return
                if (performer and navigation_guard is not None and data.get("navigation")
                        and urlsplit(url).netloc == "www.vividseats.com"
                        and urlsplit(url).path == urlsplit(performer).path and status in (401, 403, 429)):
                    navigation_guard["denial"] = status
                mime = str(reply.get("mimeType") or "")
                if not inventory_request(url) and not data.get("navigation") and not self.owner._looks_like_map_response(url, mime):
                    return
                row = {"url": url, "request_id": request_id, "status": status, "mime": mime,
                       "method": request.get("method") if request.get("method") in {"GET", "POST"} else "other",
                       "navigation": bool(data.get("navigation")), "protocol": str(reply.get("protocol") or "").casefold(),
                       "request_time": request_time,
                       "from_cache": reply.get("fromCache"), "request_header_names": _header_names(request.get("headers", [])),
                       "response_header_names": _header_names(reply.get("headers", []))}
                events.put_nowait(row)
            except (AttributeError, TypeError, ValueError, queue.Full):
                return
        return response

    def capture(self, url: str, *, reload_page: bool = False) -> tuple[dict[str, Any], datetime]:
        from selenium.common.exceptions import TimeoutException

        url = validated_vivid_url(url)
        production_id = urlsplit(url).path.rstrip("/").split("/")[-1]
        if not re.fullmatch(r"[0-9]{1,12}", production_id):
            raise ValueError("Vivid capture requires a numeric production ID")
        diagnostics = {"engine": "firefox", "runtime": dict(self.runtime), "production_id": production_id,
                       "responses": [], "body_read_retries": 0, "inventory_view_actions": [], "acquisition_method": "original-response-bidi",
                       "navigation_mode": "performer" if getattr(self, "_normal_routes", {}) else "direct"}
        self.owner.capture_diagnostics = diagnostics
        events = queue.Queue(maxsize=256)
        started_ms = time.time() * 1000
        with self._lock:
            self._generation += 1
            generation = self._generation
        performer = getattr(self, "_normal_routes", {}).get(production_id)
        if getattr(self, "_normal_routes", {}) and performer is None:
            raise VividCaptureError("event-route-not-configured", diagnostics)
        gate, requested = [None], threading.Event()
        navigation_guard = {}
        callback_id, request_callback_id, collector = None, None, None
        captured, stamp, ready_at, map_opened, failure = None, None, None, False, None
        map_bodies, body_requests, map_attempts = [], {}, set()
        try:
            try:
                options = {} if performer else {"contexts": [self.owner.driver.current_window_handle]}
                collector = self.network.add_data_collector(data_types=["response"], max_encoded_data_size=MAX_INVENTORY_BYTES,
                                                            collector_type="blob", **options).get("collector")
                if not collector:
                    raise RuntimeError("Missing response collector")
                callback_id = self.network.add_event_handler("response_completed", self._callback(generation, started_ms, events, performer, navigation_guard))
                if performer:
                    request_callback_id = self.network.add_event_handler("before_request_sent", self._request_started_callback(generation, production_id, gate, requested))
            except Exception as exc:
                diagnostics["bidi_error_type"] = type(exc).__name__
                raise VividCaptureError("browser-bidi-unavailable", diagnostics) from exc
            try:
                deadline = time.monotonic() + self.timeout
                if reload_page:
                    if not _event_url_matches(getattr(self.owner.driver, "current_url", None), production_id):
                        raise VividCaptureError("reload-event-identity-mismatch", diagnostics)
                    gate[0] = started_ms
                    self.owner.driver.refresh()
                elif performer:
                    self._prepare_route_window()
                    self._navigate_normal(performer, production_id, deadline, diagnostics, gate, requested, navigation_guard)
                else:
                    self.owner.driver.get(url)
            except TimeoutException:
                diagnostics["navigation_timeout"] = True
            if not performer:
                deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                if stamp is None:
                    stamp = event_datetime(self.owner.driver, production_id)
                    expected = getattr(self, "_expected_dates", {}).get(production_id)
                    if stamp is not None and expected is not None and stamp != expected:
                        raise VividCaptureError("event-metadata-time-mismatch", diagnostics)
                while True:
                    try:
                        row = events.get_nowait()
                    except queue.Empty:
                        break
                    if performer and (gate[0] is None or row["request_time"] < gate[0]):
                        diagnostics["preclick_responses_ignored"] = diagnostics.get("preclick_responses_ignored", 0) + 1
                        continue
                    if (row["navigation"] and urlsplit(row["url"]).hostname in {"www.vividseats.com", "vividseats.com"}
                            and _event_url_matches(row["url"], production_id)):
                        diagnostics["document_status"] = row["status"]
                        if row["status"] in (401, 403, 429):
                            raise VividCaptureError(http_category(row["status"]), diagnostics)
                    if inventory_request(row["url"]):
                        query = parse_qs(urlsplit(row["url"]).query, keep_blank_values=True)
                        if row["method"] != "GET" or query.get("productionId") != [production_id]:
                            diagnostics["unrelated_inventory_responses_ignored"] = diagnostics.get("unrelated_inventory_responses_ignored", 0) + 1
                            continue
                        diagnostics["responses"] = (diagnostics["responses"] + [_safe_response(row)])[-30:]
                        if row["status"] in (401, 403, 429):
                            raise VividCaptureError(http_category(row["status"]), diagnostics)
                        if row["status"] >= 400:
                            failure = (row["status"], time.monotonic())
                        elif row["status"] == 200:
                            if row["method"] == "GET" and native_unfiltered_request(row["url"], production_id):
                                body_requests[row["request_id"]] = {"attempts": 0, "next_read": 0.0}
                            else:
                                diagnostics["filtered_responses_rejected"] = diagnostics.get("filtered_responses_rejected", 0) + 1
                    elif row["status"] == 200 and self.owner._looks_like_map_response(row["url"], row["mime"]):
                        if len(map_attempts) >= MAX_MAP_RESPONSES or row["request_id"] in map_attempts:
                            continue
                        map_attempts.add(row["request_id"])
                        try:
                            body = decode_response(self.network.get_data(data_type="response", collector=collector,
                                                                        request=row["request_id"], disown=True), MAX_MAP_BYTES)
                            parsed = urlsplit(row["url"])
                            safe_url = f"{parsed.scheme}://{parsed.hostname}{parsed.path}"
                            map_bodies.append((body, row["mime"], safe_url))
                        except Exception:
                            diagnostics["map_body_read_errors"] = diagnostics.get("map_body_read_errors", 0) + 1
                for request_id, request in list(body_requests.items()):
                    if time.monotonic() < request["next_read"]:
                        continue
                    request["attempts"] += 1
                    request["next_read"] = time.monotonic() + min(1.0, request["attempts"] * 0.15)
                    try:
                        text = decode_response(self.network.get_data(data_type="response", collector=collector, request=request_id, disown=True))
                        payload = json.loads(text)
                    except Exception as exc:
                        diagnostics["body_read_retries"] += 1
                        diagnostics["last_body_error"] = type(exc).__name__
                        continue
                    del body_requests[request_id]
                    try:
                        validate_full_inventory(payload, production_id)
                    except VividCaptureError as exc:
                        if exc.category == "inventory-identity-mismatch":
                            diagnostics["identity_responses_rejected"] = diagnostics.get("identity_responses_rejected", 0) + 1
                            continue
                        raise VividCaptureError(exc.category, diagnostics) from exc
                    captured, ready_at = payload, ready_at or time.monotonic()
                    diagnostics["listing_count"] = len(payload["tickets"])
                if captured is None and failure and time.monotonic() - failure[1] >= 5 and not body_requests:
                    status = failure[0]
                    raise VividCaptureError(http_category(status), diagnostics, retryable=status >= 500)
                if captured is not None and stamp is not None:
                    sections = sorted({" ".join(str(row.get("l") or "").split()) for row in captured["tickets"] if str(row.get("l") or "").strip()}, key=str.casefold)
                    candidates = [extract_map_geometry_from_json(captured, sections, source="vivid-listings-json", source_url=url)]
                    candidates.extend(self.owner._geometry_from_response(body, mime, source, sections) for body, mime, source in map_bodies)
                    candidates.append(self.owner._dom_map_geometry(sections, url))
                    geometry = choose_best_geometry(candidates, sections)
                    elapsed = time.monotonic() - ready_at
                    if geometry_is_usable(geometry, sections) or elapsed >= MAP_SETTLE_SECONDS:
                        if geometry is not None:
                            captured["_map_geometry"] = geometry
                        captured["_map_geometry_diagnostics"] = {
                            "status": "captured" if geometry_is_usable(geometry, sections) else "partial" if geometry else "unavailable",
                            "source": geometry.get("source") if geometry else None, "mapped_sections": geometry_section_count(geometry),
                            "coverage_ratio": geometry.get("coverage_ratio") if geometry else 0,
                            "network_map_responses": len(map_bodies), "map_view_opened": map_opened,
                        }
                        return captured, stamp
                    if not map_opened and elapsed >= 0.5:
                        map_opened = self.owner._open_map_view()
                time.sleep(0.15)
            if captured is not None:
                raise VividCaptureError("event-metadata-timeout", diagnostics)
            if failure:
                status = failure[0]
                raise VividCaptureError(http_category(status), diagnostics, retryable=status >= 500)
            category = "filtered-inventory-only" if diagnostics.get("filtered_responses_rejected") else "provider-inventory-timeout"
            raise VividCaptureError(category, diagnostics, retryable=category == "provider-inventory-timeout")
        finally:
            with self._lock:
                self._generation += 1
            if callback_id is not None:
                try:
                    self.network.remove_event_handler("response_completed", callback_id)
                except Exception as exc:
                    diagnostics["bidi_unsubscribe_error_type"] = type(exc).__name__
            if request_callback_id is not None:
                try:
                    self.network.remove_event_handler("before_request_sent", request_callback_id)
                except Exception as exc:
                    diagnostics["bidi_request_unsubscribe_error_type"] = type(exc).__name__
            if collector is not None:
                try:
                    self.network.remove_data_collector(collector=collector)
                except Exception as exc:
                    diagnostics["bidi_cleanup_error_type"] = type(exc).__name__
