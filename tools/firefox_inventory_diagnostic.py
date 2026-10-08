"""Observe one public event with stock headed Firefox, without database writes.

Use passive BiDi response events/body collection. If Firefox cannot supply the
body, make at most one same-origin fetch of an unfiltered URL that the page
itself actually requested. Never import profiles or construct inventory URLs.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import parse_qs, urljoin, urlsplit

EVENT = "https://www.vividseats.com/boston-bruins-tickets-td-garden-10-8-2026/production/7302493"
PERFORMER = "https://www.vividseats.com/boston-bruins-tickets--sports-nhl-hockey/performer/104"
PRODUCTION_ID = "7302493"
PATHS = {"/hermes/api/v1/listings", "/hermes/api/v2/listings"}
MAX_BYTES = 16 * 1024 * 1024
SAFE_QUERY = {"productionId", "includeIpAddress", "currency", "localizeCurrency", "priceGroupId",
              "quantity", "recommended", "sf", "offset", "page", "sort"}
SAFE_HEADERS = {"accept", "brand-name", "if-none-match", "if-modified-since", "cache-control",
                "content-type", "x-user-id", "x-performer-id"}


def validate_target(event_url, performer_url, production_id):
    if not isinstance(production_id, str) or not re.fullmatch(r"[0-9]{1,12}", production_id):
        raise ValueError("invalid-production-id")
    for url, suffix in ((event_url, "/production/" + production_id), (performer_url, None)):
        parsed = urlsplit(url)
        invalid_url = parsed.scheme != "https" or parsed.netloc != "www.vividseats.com" or parsed.query or parsed.fragment
        wrong_path = not parsed.path.rstrip("/").endswith(suffix) if suffix else not re.search(r"/performer/[0-9]+$", parsed.path.rstrip("/"))
        if invalid_url or wrong_path:
            raise ValueError("invalid-public-target-url")
    return event_url, performer_url, production_id


def unfiltered_url(url, production_id=PRODUCTION_ID):
    try:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (parsed.scheme != "https" or parsed.netloc != "www.vividseats.com" or parsed.path not in PATHS
                or query.get("productionId") != [production_id]):
            return False
        if set(query) - (SAFE_QUERY | {"scarcity"}) or any(len(values) != 1 or not values[0] for values in query.values()):
            return False
        for key in ("quantity", "offset", "page"):
            if key in query and query[key] != ["0"]:
                return False
        for key in ("recommended", "sf", "scarcity"):
            if key in query and any(value.casefold() not in {"false", "0"} for value in query[key]):
                return False
        return not any(key in query for key in ("limit", "pageSize"))
    except (TypeError, ValueError):
        return False


def safe_request(url, production_id=PRODUCTION_ID):
    parsed = urlsplit(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    return {"path": parsed.path, "query": {
        key: values for key, values in query.items() if key in SAFE_QUERY
        and len(values) <= 2 and all(re.fullmatch(r"[A-Za-z0-9_,. -]{0,60}", value) for value in values)
    }, "unfiltered": unfiltered_url(url, production_id)}


def header_names(headers):
    return sorted({item["name"].casefold() for item in headers if isinstance(item, dict)
                   and isinstance(item.get("name"), str) and item["name"].casefold() in SAFE_HEADERS})


def sanitize_inventory(payload, production_id=PRODUCTION_ID):
    if not isinstance(payload, dict):
        raise ValueError("invalid-payload")
    metadata, tickets = payload.get("global"), payload.get("tickets")
    if not isinstance(metadata, list) or not metadata or not isinstance(metadata[0], dict):
        raise ValueError("invalid-global")
    if str(metadata[0].get("productionId", "")) != production_id:
        raise ValueError("identity-mismatch")
    if not isinstance(tickets, list) or not tickets:
        raise ValueError("empty-inventory")
    count = metadata[0].get("listingCount")
    if isinstance(count, bool) or not isinstance(count, (int, str)) or not re.fullmatch(r"[0-9]+", str(count)) or int(count) != len(tickets):
        raise ValueError("incomplete-inventory-count")
    keys = {"productionId", "productionName", "mapTitle", "currency", "currencyCode", "productionDate", "eventDate", "venueName",
            "venueId", "venueTimeZone", "listingCount"}
    clean_global = {key: value for key, value in metadata[0].items() if key in keys
                    and isinstance(value, (str, int, float)) and not isinstance(value, bool) and len(str(value)) <= 500}
    clean_tickets = []
    for ticket in tickets:
        if not isinstance(ticket, dict):
            raise ValueError("invalid-ticket")
        section, price = ticket.get("l"), ticket.get("p")
        if not isinstance(section, str) or not section.strip() or len(section) > 200 or isinstance(price, bool):
            raise ValueError("invalid-ticket-price-or-section")
        try:
            numeric_price = float(price)
        except (ValueError, TypeError):
            raise ValueError("invalid-price") from None
        if not math.isfinite(numeric_price) or numeric_price < 0:
            raise ValueError("invalid-price")
        row = {"l": section, "p": price}
        for key in ("r", "q"):
            value = ticket.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool) and len(str(value)) <= 100:
                row[key] = value
        alternate = ticket.get("aip")
        if alternate is not None:
            if isinstance(alternate, bool) or not isinstance(alternate, (str, int, float)):
                raise ValueError("invalid-alternate-price")
            try:
                numeric_alternate = float(alternate)
            except (TypeError, ValueError):
                raise ValueError("invalid-alternate-price") from None
            if not math.isfinite(numeric_alternate) or numeric_alternate < 0:
                raise ValueError("invalid-alternate-price")
            row["aip"] = alternate
        tags = ticket.get("tags")
        if isinstance(tags, list) and all(isinstance(value, (str, int)) and len(str(value)) <= 100 for value in tags):
            row["tags"] = tags
        clean_tickets.append(row)
    return {"global": [clean_global], "tickets": clean_tickets}


def decode_bidi_body(result):
    value = result.get("bytes", {})
    if not isinstance(value, dict) or not isinstance(value.get("value"), str) or len(value["value"]) > MAX_BYTES * 2:
        raise ValueError("invalid-or-oversized-body")
    if value.get("type") == "base64":
        raw = base64.b64decode(value["value"], validate=True)
    elif value.get("type") == "string":
        raw = value["value"].encode("utf-8")
    else:
        raise ValueError("invalid-body")
    if len(raw) > MAX_BYTES:
        raise ValueError("oversized-body")
    return json.loads(raw)


class Evidence:
    def __init__(self, production_id=PRODUCTION_ID, performer_url=PERFORMER):
        self.lock, self.rows, self.inventory = threading.Lock(), [], []
        self.denial, self.phase = False, "page"
        self.event_click_started_ms = None
        self.production_id, self.performer_url = production_id, performer_url

    def response(self, event):
        params = event if isinstance(event, dict) else vars(event)
        request, response = params.get("request") or {}, params.get("response") or {}
        if not isinstance(request, dict) or not isinstance(response, dict):
            return
        url, status = request.get("url", ""), response.get("status")
        if not isinstance(url, str) or not isinstance(status, int):
            return
        parsed = urlsplit(url)
        if parsed.hostname not in {"www.vividseats.com", "vividseats.com"}:
            return
        with self.lock:
            phase = self.phase
            request_time = (request.get("timings") or {}).get("requestTime")
            if (self.event_click_started_ms is not None and isinstance(request_time, (int, float))
                    and request_time < self.event_click_started_ms):
                phase = "performer"
            self.denial |= status in (401, 403, 429)
            if parsed.path.rstrip("/").endswith("/production/" + self.production_id):
                self.rows.append({"kind": "document", "status": status, "phase": phase})
            elif parsed.path == urlsplit(self.performer_url).path:
                self.rows.append({"kind": "performer-document", "status": status, "phase": phase})
            elif parsed.path in PATHS:
                row = {"kind": "inventory", "status": status, "phase": phase, **safe_request(url, self.production_id),
                       "request_header_names": header_names(request.get("headers", [])),
                       "response_header_names": header_names(response.get("headers", []))}
                protocol = response.get("protocol")
                if isinstance(protocol, str) and protocol.casefold() in {"h3", "h2", "http/1.1", "http/2", "http/3"}:
                    row["protocol"] = protocol
                if isinstance(response.get("fromCache"), bool):
                    row["from_cache"] = response["fromCache"]
                timings = request.get("timings") or {}
                row["timings_ms"] = {key: value for key, value in timings.items()
                                     if key in {"requestTime", "responseStart", "responseEnd", "connectStart", "connectEnd", "tlsStart"}
                                     and isinstance(value, (int, float)) and math.isfinite(value)}
                self.rows.append(row)
                self.inventory.append({"url": url, "request": request.get("request"), "status": status, "phase": phase})

    def snapshot(self):
        with self.lock:
            return list(self.rows[-30:]), list(self.inventory), self.denial


DOM_SCRIPT = r"""
const text = document.body ? document.body.innerText : '';
const count = text.match(/([\d,]+)\s+listings\b/i);
const visible = e => !!e.getClientRects().length;
return {ready_state: document.readyState, listing_count: count ? Number(count[1].replaceAll(',', '')) : null,
 inventory_error: /(?:unable to load|could(?:n.t| not) (?:load|find)|no tickets (?:available|found)|something went wrong)/i.test(text),
 challenge_visible: /(?:verify (?:you are|you're) human|access denied|unusual activity|captcha|too many requests)/i.test(text),
 quantity_modal_visible: [...document.querySelectorAll('[role="dialog"],[aria-modal="true"],dialog[open]')].some(e => visible(e) && /how many tickets/i.test(e.innerText)),
 webdriver: navigator.webdriver === true};
"""

FETCH_SCRIPT = """
const url = arguments[0], productionId = arguments[1], done = arguments[arguments.length - 1];
const actual = performance.getEntriesByType('resource').some(e => e.name === url);
const u = new URL(url), q = u.searchParams;
const filtered = ['quantity','offset','page'].some(k => q.has(k) && q.getAll(k).some(v => v !== '0')) ||
 ['recommended','sf'].some(k => q.has(k) && q.getAll(k).some(v => !['0','false'].includes(v.toLowerCase()))) ||
 ['limit','pageSize'].some(k => q.has(k));
if (!actual || u.origin !== location.origin || !['/hermes/api/v1/listings','/hermes/api/v2/listings'].includes(u.pathname) ||
 q.getAll('productionId').length !== 1 || q.get('productionId') !== productionId || filtered) {
 done({error: 'unobserved-or-filtered-url'}); return;
}
const abort = new AbortController(), timer = setTimeout(() => abort.abort(), 15000);
fetch(url, {signal: abort.signal}).then(async response => {
 if (response.status !== 200) { done({status: response.status}); return; }
 const reader = response.body.getReader(), chunks = []; let size = 0;
 while (true) {
  const {value, done: ended} = await reader.read(); if (ended) break;
  size += value.byteLength; if (size > 16777216) { await reader.cancel(); done({status: 200, error: 'oversized-body'}); return; }
  chunks.push(value);
 }
 const bytes = new Uint8Array(size); let offset = 0;
 for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
 try { done({status: 200, payload: JSON.parse(new TextDecoder().decode(bytes))}); }
 catch (_) { done({status: 200, error: 'invalid-json'}); }
}).catch(() => done({error: 'fetch-failed'})).finally(() => clearTimeout(timer));
"""


def prepare_quantity(driver, state):
    def visible(elements):
        return [element for element in elements if element.is_displayed() and element.is_enabled()]
    for dialog in visible(driver.find_elements("css selector", '[role="dialog"],[aria-modal="true"],dialog[open]')):
        if "how many tickets" not in dialog.text.casefold():
            continue
        controls = visible(dialog.find_elements("css selector", 'label,[role="checkbox"],input[type="checkbox"],button'))
        for wanted in ("any quantity", "2"):
            for control in controls:
                if any((value or "").strip().casefold() == wanted for value in
                       [control.text, control.get_attribute("aria-label"), control.get_attribute("value")]):
                    control.click()
                    state["needs_clear"] = wanted == "2"
                    state["actions"].append("quantity-modal-all" if wanted != "2" else "quantity-modal-two")
                    return
        return
    if state["needs_clear"]:
        clear = visible(driver.find_elements("css selector", '[data-testid="clear-filters-button"]'))
        if clear:
            clear[0].click()
            state["needs_clear"] = False
            state["actions"].append("quantity-filter-all")


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


METADATA_SCRIPT = """
try {
 const p = JSON.parse(document.getElementById('__NEXT_DATA__').textContent).props.pageProps;
 const e = p.initialProductionDetailsData.data;
 return {id:e.id, page_id:p.id, utc_date:e.utcDate, title:e.name, venue:e.venue?.name};
} catch (_) { return null; }
"""


def utc_stamp(value):
    if not isinstance(value, str) or not value:
        raise ValueError("explicit-utc-required")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
        raise ValueError("explicit-utc-required")
    return stamp.astimezone(timezone.utc)


def save_capture(args, report, payload, method, driver=None):
    production_id = getattr(args, "production_id", PRODUCTION_ID)
    clean = sanitize_inventory(payload, production_id)
    expected = getattr(args, "expected_event_utc", None)
    if expected:
        metadata = driver.execute_script(METADATA_SCRIPT)
        identity = (isinstance(metadata, dict) and str(metadata.get("id")) == production_id
                    and str(metadata.get("page_id")) == production_id)
        report["metadata_identity_match"] = identity
        try:
            actual = utc_stamp(metadata["utc_date"]) if identity else None
            matching_time = actual is not None and actual == utc_stamp(expected)
        except (TypeError, ValueError, KeyError):
            matching_time = False
        report["metadata_time_match"] = matching_time
        if not matching_time:
            report["category"] = "event-metadata-unavailable-or-mismatch"
            return False
        report["event_date"] = actual.isoformat()
        for field in ("title", "venue"):
            value = metadata.get(field)
            if isinstance(value, str) and len(value) <= 300:
                report["event_" + field] = value
    write_json(args.inventory_output, clean)
    report["acquisition_method"] = method
    report["captured_listing_count"] = len(clean["tickets"])
    report["captured_section_count"] = len({row["l"] for row in clean["tickets"]})
    report["category"] = "captured-full-observed-unfiltered-inventory"
    return True


def visible_event_link(driver, production_id=PRODUCTION_ID, performer_url=PERFORMER):
    for link in driver.find_elements("css selector", f"a[href*='/production/{production_id}']")[:50]:
        try:
            href = link.get_attribute("href") or ""
            parsed = urlsplit(urljoin(performer_url, href))
            if (parsed.scheme != "https" or parsed.netloc != urlsplit(performer_url).netloc
                    or parsed.query or parsed.fragment
                    or not parsed.path.rstrip("/").endswith("/production/" + production_id)
                    or link.get_attribute("target") not in (None, "", "_self", "_blank")
                    or not link.is_displayed() or not link.is_enabled()):
                continue
            return link
        except Exception:
            continue
    return None


def navigate_performer(driver, evidence, report, deadline, *, production_id=PRODUCTION_ID, performer_url=PERFORMER):
    evidence.phase = "performer"
    driver.get(performer_url)
    while time.monotonic() < deadline:
        if evidence.snapshot()[2] or driver.execute_script(DOM_SCRIPT)["challenge_visible"]:
            report["category"] = "access-denial-or-challenge"
            return False
        link = visible_event_link(driver, production_id, performer_url)
        if link is not None:
            report["performer_time_origin"] = driver.execute_script("return performance.timeOrigin;")
            new_window = link.get_attribute("target") == "_blank"
            report["selected_event_path"] = urlsplit(urljoin(performer_url, link.get_attribute("href"))).path
            owned_before = set(driver.window_handles)
            evidence.event_click_started_ms = time.time() * 1000
            evidence.phase = "page"
            link.click()
            report["visible_event_link_clicked"] = True
            if new_window:
                report["event_link_target"] = "_blank"
                window_deadline = min(deadline, time.monotonic() + 10)
                observed_new = False
                while time.monotonic() < window_deadline:
                    windows = [handle for handle in driver.window_handles if handle not in owned_before]
                    observed_new = observed_new or bool(windows)
                    for handle in windows:
                        driver.switch_to.window(handle)
                        parsed = urlsplit(driver.current_url)
                        if (parsed.scheme == "https" and parsed.netloc == urlsplit(performer_url).netloc
                                and parsed.path.rstrip("/").endswith("/production/" + production_id)):
                            report["event_opened_new_window"] = True
                            report["owned_new_window_count"] = len(windows)
                            return True
                    time.sleep(0.25)
                report["category"] = "new-window-event-identity-not-confirmed" if observed_new else "new-event-window-not-found"
                return False
            return True
        time.sleep(0.5)
    report["category"] = "visible-event-link-not-found"
    return False


def binary_version(binary):
    result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=5, check=True)
    line = result.stdout.splitlines()[0]
    return line if re.fullmatch(r"[A-Za-z0-9 .()_-]{1,100}", line) else "unavailable"


def run(args, report):
    import selenium
    from selenium import webdriver
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.firefox.service import Service
    logging.getLogger("selenium").setLevel(logging.CRITICAL)
    firefox, gecko = shutil.which("firefox"), shutil.which("geckodriver")
    if not firefox or not gecko:
        raise RuntimeError("installed-firefox-or-geckodriver-missing")
    report["runtime"] = {"selenium": selenium.__version__, "installed_firefox": binary_version(firefox),
                         "installed_geckodriver": binary_version(gecko), "headed": True}
    write_json(args.output, report)
    options = Options()
    options.binary_location, options.enable_bidi = firefox, True
    driver, collector = None, None
    started = time.monotonic()
    event_url, performer_url, production_id = validate_target(getattr(args, "event_url", EVENT), getattr(args, "performer_url", PERFORMER), getattr(args, "production_id", PRODUCTION_ID))
    evidence, state = Evidence(production_id, performer_url), {"needs_clear": False, "actions": []}
    report["production_id"] = production_id
    navigation = getattr(args, "navigation", "direct")
    report["navigation_mode"] = navigation
    try:
        driver = webdriver.Firefox(options=options, service=Service(executable_path=gecko, log_output=subprocess.DEVNULL))
        driver.command_executor._client_config.timeout = 25
        driver.set_page_load_timeout(30)
        driver.set_script_timeout(20)
        report["runtime"].update({"browser_version": driver.capabilities.get("browserVersion"),
                                  "geckodriver_version": driver.capabilities.get("moz:geckodriverVersion")})
        network = driver.network
        network.add_event_handler("response_completed", evidence.response)
        try:
            result = network.add_data_collector(data_types=["response"], max_encoded_data_size=MAX_BYTES,
                                                collector_type="blob")
            collector = result.get("collector")
            report["bidi_body_collection"] = "available" if collector else "unavailable"
            report["bidi_collector_scope"] = "owned-browser-session"
        except Exception as exc:
            report["bidi_body_collection"] = "unsupported-or-unavailable"
            report["bidi_collection_error_type"] = type(exc).__name__
        write_json(args.output, report)
        try:
            if navigation == "performer":
                if not navigate_performer(driver, evidence, report, min(started + args.timeout - 25, time.monotonic() + 35), production_id=production_id, performer_url=performer_url):
                    return False
            else:
                driver.get(event_url)
        except selenium.common.exceptions.TimeoutException:
            report["navigation_timeout"] = True
        deadline, candidate, stable_since = started + args.timeout, None, time.monotonic()
        body_attempts = set()
        while time.monotonic() < deadline - 20:
            report["dom"] = driver.execute_script(DOM_SCRIPT)
            rows, inventory, denial = evidence.snapshot()
            report["responses"] = rows
            if denial or report["dom"]["challenge_visible"]:
                report["category"] = "access-denial-or-challenge"
                return False
            # The resource-timing buffer can fill before inventory completes.
            # Passive BiDi events retain the actual request URL and body ID.
            urls = [row["url"] for row in inventory if row["phase"] == "page" and unfiltered_url(row["url"], production_id)]
            if not urls:
                observed = driver.execute_script("return performance.getEntriesByType('resource').map(e => e.name);")
                urls = [url for url in observed if unfiltered_url(url, production_id)]
            candidate = urls[-1] if urls else None
            # A completed native whole-market response is sufficient even while
            # a quantity dialog overlays the page. Do not change that UI first.
            original = [row for row in inventory if row["url"] == candidate and row["phase"] == "page"]
            if collector and candidate:
                for row in reversed(original):
                    if row["status"] != 200 or not row["request"] or row["request"] in body_attempts:
                        continue
                    body_attempts.add(row["request"])
                    try:
                        payload = decode_bidi_body(network.get_data(data_type="response", collector=collector,
                                                                    request=row["request"], disown=True))
                        report["observed_request"] = safe_request(candidate, production_id)
                        report["original_inventory_statuses"] = [item["status"] for item in original]
                        return save_capture(args, report, payload, "original-response-bidi", driver)
                    except Exception as exc:
                        report["bidi_body_read_error_type"] = type(exc).__name__
            if navigation == "performer":
                # This comparison only observes the normal navigation's native
                # requests. It never changes quantity or replays an HTTP call.
                if candidate and original and time.monotonic() - stable_since >= 5:
                    report["original_inventory_statuses"] = [item["status"] for item in original]
                    report["observed_request"] = safe_request(candidate, production_id)
                    report["category"] = "native-inventory-unavailable"
                    return False
                time.sleep(0.5)
                continue
            before = len(state["actions"])
            prepare_quantity(driver, state)
            if len(state["actions"]) != before:
                stable_since = time.monotonic()
            if candidate and not state["needs_clear"] and not report["dom"]["quantity_modal_visible"] and time.monotonic() - stable_since >= 5:
                break
            time.sleep(0.5)
        report["quantity_actions"] = state["actions"]
        if navigation == "performer":
            report["category"] = "no-complete-native-inventory-response"
            return False
        if state["needs_clear"] or report.get("dom", {}).get("quantity_modal_visible"):
            report["category"] = "quantity-control-not-cleared"
            return False
        if not candidate:
            report["category"] = "no-observed-unfiltered-inventory-url"
            return False
        report["observed_request"] = safe_request(candidate, production_id)
        rows, inventory, denial = evidence.snapshot()
        report["responses"] = rows
        if denial:
            report["category"] = "access-denial-or-challenge"
            return False
        payload = None
        original = [row for row in inventory if row["url"] == candidate and row["phase"] == "page"]
        report["original_inventory_statuses"] = [row["status"] for row in original]
        if collector:
            for row in reversed(original):
                if row["status"] != 200 or not row["request"] or row["request"] in body_attempts:
                    continue
                body_attempts.add(row["request"])
                try:
                    payload = decode_bidi_body(network.get_data(data_type="response", collector=collector,
                                                                request=row["request"], disown=True))
                    report["acquisition_method"] = "original-response-bidi"
                    break
                except Exception as exc:
                    report["bidi_body_read_error_type"] = type(exc).__name__
        if payload is None:
            evidence.phase = "one-observed-url-fetch"
            report["acquisition_method"] = "one-same-origin-fetch-of-observed-url"
            report["follow_up_fetch_attempted"] = True
            write_json(args.output, report)
            result = driver.execute_async_script(FETCH_SCRIPT, candidate, production_id)
            report["follow_up_status"] = result.get("status")
            if result.get("error"):
                report["follow_up_error"] = result["error"]
            if result.get("status") != 200 or "payload" not in result:
                report["category"] = "follow-up-inventory-unavailable"
                return False
            payload = result["payload"]
        return save_capture(args, report, payload, report["acquisition_method"], driver)
    finally:
        report["responses"] = evidence.snapshot()[0]
        if navigation == "performer":
            report["event_document_response_count"] = sum(row["kind"] == "document" and row["phase"] == "page" for row in report["responses"])
            if driver is not None and report.get("visible_event_link_clicked"):
                try:
                    report["event_page_reached"] = urlsplit(driver.current_url).path.rstrip("/").endswith("/production/" + production_id)
                    origin_before = report.pop("performer_time_origin", None)
                    origin_after = driver.execute_script("return performance.timeOrigin;")
                    same_document = (isinstance(origin_before, (int, float)) and isinstance(origin_after, (int, float))
                                     and origin_before == origin_after)
                    report["navigation_transition"] = ("new-window-document" if report["event_page_reached"] and report.get("event_opened_new_window")
                        else "client-side" if report["event_page_reached"] and same_document
                        else "document" if report["event_page_reached"] else "unconfirmed")
                except Exception as exc:
                    report["navigation_probe_error_type"] = type(exc).__name__
            report.pop("performer_time_origin", None)
        report["quantity_actions"] = state["actions"]
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        write_json(args.output, report)
        if driver is not None:
            try:
                driver.quit()
                report["browser_closed"] = True
            except Exception:
                report["browser_close_failed"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=75, help="Observation bound (30..85 seconds).")
    parser.add_argument("--navigation", choices=("direct", "performer"), default="direct")
    parser.add_argument("--event-url", default=EVENT)
    parser.add_argument("--performer-url", default=PERFORMER)
    parser.add_argument("--production-id", default=PRODUCTION_ID)
    parser.add_argument("--expected-event-utc")
    parser.add_argument("--output", type=Path, default=Path("firefox_inventory_result.json"))
    parser.add_argument("--inventory-output", type=Path, default=Path("firefox_public_inventory.json"))
    args = parser.parse_args()
    if not 30 <= args.timeout <= 85:
        parser.error("--timeout must be between 30 and 85 seconds")
    try:
        validate_target(args.event_url, args.performer_url, args.production_id)
        if args.expected_event_utc:
            utc_stamp(args.expected_event_utc)
    except (TypeError, ValueError):
        parser.error("URLs, production ID and explicit UTC date must match a public Vivid target")
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "production_id": args.production_id,
              "status": "started", "profile": "fresh-diagnostic-owned", "database_calls": 0}
    write_json(args.output, report)
    success = False
    try:
        success = run(args, report)
    except Exception as exc:
        report["category"], report["error_type"] = "diagnostic-error", type(exc).__name__
    report["status"] = "captured" if success else "failed"
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    write_json(args.output, report)
    print(json.dumps({key: report.get(key) for key in ("status", "category", "captured_listing_count", "elapsed_seconds")}, sort_keys=True))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
