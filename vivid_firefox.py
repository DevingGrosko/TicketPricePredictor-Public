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
from urllib.parse import parse_qs, urlsplit

from collector import as_utc, parse_iso_datetime, validated_vivid_url
from nfl_metadata import choose_best_geometry, extract_map_geometry_from_json, geometry_is_usable, geometry_section_count
from vivid_inventory import MAX_INVENTORY_BYTES, VividCaptureError, http_category, inventory_request, unfiltered_request, validate_inventory

MAX_MAP_BYTES = 8_000_000
MAX_MAP_RESPONSES = 24
MAP_SETTLE_SECONDS = 2.5
SAFE_HEADER_NAMES = {"accept", "brand-name", "if-none-match", "if-modified-since", "cache-control", "content-type"}
SAFE_QUERY_NAMES = {"productionId", "quantity", "recommended", "sf", "currency", "priceGroupId", "localizeCurrency", "includeIpAddress"}
FULL_INVENTORY_QUERY_NAMES = SAFE_QUERY_NAMES | {"offset", "page", "sort", "scarcity"}


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

    def _callback(self, generation, started_ms, events):
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
                mime = str(reply.get("mimeType") or "")
                if not inventory_request(url) and not data.get("navigation") and not self.owner._looks_like_map_response(url, mime):
                    return
                row = {"url": url, "request_id": request_id, "status": status, "mime": mime,
                       "method": request.get("method") if request.get("method") in {"GET", "POST"} else "other",
                       "navigation": bool(data.get("navigation")), "protocol": str(reply.get("protocol") or "").casefold(),
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
                       "responses": [], "body_read_retries": 0, "inventory_view_actions": [], "acquisition_method": "original-response-bidi"}
        self.owner.capture_diagnostics = diagnostics
        events = queue.Queue(maxsize=256)
        started_ms = time.time() * 1000
        with self._lock:
            self._generation += 1
            generation = self._generation
        callback_id, collector = None, None
        captured, stamp, ready_at, map_opened, failure = None, None, None, False, None
        map_bodies, body_requests, map_attempts = [], {}, set()
        try:
            try:
                collector = self.network.add_data_collector(data_types=["response"], max_encoded_data_size=MAX_INVENTORY_BYTES,
                                                            collector_type="blob", contexts=[self.owner.driver.current_window_handle]).get("collector")
                if not collector:
                    raise RuntimeError("Missing response collector")
                callback_id = self.network.add_event_handler("response_completed", self._callback(generation, started_ms, events))
            except Exception as exc:
                diagnostics["bidi_error_type"] = type(exc).__name__
                raise VividCaptureError("browser-bidi-unavailable", diagnostics) from exc
            try:
                if reload_page:
                    self.owner.driver.refresh()
                else:
                    self.owner.driver.get(url)
            except TimeoutException:
                diagnostics["navigation_timeout"] = True
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                if stamp is None:
                    stamp = event_datetime(self.owner.driver, production_id)
                while True:
                    try:
                        row = events.get_nowait()
                    except queue.Empty:
                        break
                    if (row["navigation"] and urlsplit(row["url"]).hostname in {"www.vividseats.com", "vividseats.com"}
                            and urlsplit(row["url"]).path == urlsplit(url).path):
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
            if collector is not None:
                try:
                    self.network.remove_data_collector(collector=collector)
                except Exception as exc:
                    diagnostics["bidi_cleanup_error_type"] = type(exc).__name__
