"""Two or four fixed stock headed WebKit observations; no ingestion or request replay.

Each target uses its own fresh browser process. Only the native response body
from an exact matching, unfiltered GET200 inventory request is read or saved.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from importlib.metadata import version
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit

if __package__:
    from .firefox_inventory_diagnostic import MAX_BYTES, sanitize_inventory, unfiltered_url, utc_stamp, validate_target, write_json
    from .firefox_link_canary import NHL, NFL
else:
    from firefox_inventory_diagnostic import MAX_BYTES, sanitize_inventory, unfiltered_url, utc_stamp, validate_target, write_json
    from firefox_link_canary import NHL, NFL

TARGETS = (NHL, NFL)
# Observed public team links and schedule-backed UTC dates, frozen for this
# diagnostic. The existing two-event control remains the default.
DALLAS = {
    "sport": "nfl", "production_id": "6489565",
    "event_url": "https://www.vividseats.com/dallas-cowboys-tickets-arlington-att-stadium-3-9-2026/production/6489565",
    "performer_url": "https://www.vividseats.com/dallas-cowboys-tickets--sports-nfl-football/performer/214",
    "expected_event_utc": "2026-10-09T00:15:00Z",
}
BUFFALO = {
    "sport": "nhl", "production_id": "7300510",
    "event_url": "https://www.vividseats.com/buffalo-sabres-tickets-keybank-center-10-8-2026/production/7300510",
    "performer_url": "https://www.vividseats.com/buffalo-sabres-tickets--sports-nhl-hockey/performer/129",
    "expected_event_utc": "2026-10-08T23:00:00Z",
}
FOUR_TARGETS = (NHL, DALLAS, BUFFALO, NFL)
PATHS = {"/hermes/api/v1/listings", "/hermes/api/v2/listings"}
QUERY_KEYS = {"productionId", "includeIpAddress", "currency", "localizeCurrency", "priceGroupId", "quantity", "recommended", "sf", "offset", "page", "sort", "scarcity"}
DENIALS = {401, 403, 429}
PUBLIC_PAGE_SCRIPT = """() => {
 const text = document.body?.innerText || '';
 const count = text.match(/([\\d,]+)\\s+listings\\b/i);
 let event = null;
 try {
  const n = JSON.parse(document.getElementById('__NEXT_DATA__').textContent);
  const p = n.props.pageProps, e = p.initialProductionDetailsData.data;
  event = {id:e.id,page_id:p.id,query_id:n.query?.id,utc_date:e.utcDate,
           title:e.name,venue:e.venue?.name,venue_id:e.venue?.id};
 } catch (_) {}
 return {event, listing_count:count ? Number(count[1].replaceAll(',', '')) : null,
  challenge_visible:/(verify (you are|you're) human|access denied|unusual activity|captcha|too many requests)/i.test(text),
  inventory_error:/(unable to load|could(?:n.t| not) (?:load|find)|no tickets (?:available|found)|something went wrong)/i.test(text)};
}"""


class DiagnosticOutcome(Exception):
    def __init__(self, category):
        self.category = category


def event_url_matches(url, production_id):
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.netloc == "www.vividseats.com"
            and not parsed.query and not parsed.fragment
            and parsed.path.rstrip("/").endswith("/production/" + production_id))


def validated_public_capture(payload, metadata, target):
    clean = sanitize_inventory(payload, target["production_id"])
    if (not isinstance(metadata, dict) or str(metadata.get("id")) != target["production_id"]
            or str(metadata.get("page_id")) != target["production_id"]
            or (metadata.get("query_id") is not None and str(metadata["query_id"]) != target["production_id"])
            or utc_stamp(metadata.get("utc_date")) != utc_stamp(target["expected_event_utc"])):
        raise ValueError("Public event identity or UTC date does not match the fixed target")
    global_row = clean["global"][0]
    for key, field in (("productionName", "title"), ("mapTitle", "venue")):
        if (not isinstance(metadata.get(field), str)
                or " ".join(str(global_row.get(key) or "").split()) != " ".join(metadata[field].split())):
            raise ValueError("Public event title or venue does not match inventory")
    if str(global_row.get("venueId")) != str(metadata.get("venue_id")):
        raise ValueError("Public venue ID does not match inventory")
    for row in clean["tickets"]:
        quantity = row.get("q")
        if isinstance(quantity, bool) or not re.fullmatch(r"[0-9]+", str(quantity)) or int(quantity) <= 0:
            raise ValueError("Invalid public listing quantity")
    return clean


class Responses:
    def __init__(self, target, report):
        self.target, self.report = target, report
        self.clicked_ms = None
        self.denied = False
        self.candidates = asyncio.Queue(maxsize=32)

    def observe(self, response):
        # Read only public URL/method/status/type/timing; never transport headers.
        request, parsed = response.request, urlsplit(response.url)
        if parsed.scheme != "https" or parsed.netloc != "www.vividseats.com":
            return
        if type(response.status) is not int:
            return
        if response.status in DENIALS:
            self.denied = True
        query = parse_qs(parsed.query, keep_blank_values=True)
        target_inventory = (parsed.path in PATHS and request.method == "GET"
                            and query.get("productionId") == [self.target["production_id"]])
        document = request.resource_type == "document" and (
            parsed.path == urlsplit(self.target["performer_url"]).path
            or event_url_matches(response.url, self.target["production_id"]))
        if not target_inventory and not document:
            return
        started = request.timing.get("startTime")
        after_click = (self.clicked_ms is not None and isinstance(started, (int, float))
                       and math.isfinite(started) and started >= self.clicked_ms)
        row = {"kind": "inventory" if target_inventory else "document", "path": parsed.path,
               "status": response.status, "phase": "event" if after_click else "performer"}
        if target_inventory:
            row["query"] = {key: values[0] for key, values in query.items() if key in QUERY_KEYS
                            and len(values) == 1 and re.fullmatch(r"[A-Za-z0-9.,_-]{1,40}", values[0])}
            row["unfiltered"] = unfiltered_url(response.url, self.target["production_id"])
        self.report["responses"] = (self.report["responses"] + [row])[-30:]
        if target_inventory and after_click and response.status == 200 and row["unfiltered"]:
            try:
                self.candidates.put_nowait(response)
            except asyncio.QueueFull:
                pass


async def native_link(page, target, evidence):
    links = page.locator(f"a[href*='/production/{target['production_id']}']")
    for index in range(min(await links.count(), 50)):
        link = links.nth(index)
        href = await link.get_attribute("href")
        # The browser exposes normal links as absolute hrefs in the DOM.
        href = await link.evaluate("node => node.href") if href else ""
        if (event_url_matches(href, target["production_id"])
                and await link.get_attribute("target") == "_blank"
                and await link.is_visible() and await link.is_enabled()):
            if evidence.denied:
                raise DiagnosticOutcome("access-denial-or-challenge")
            evidence.clicked_ms = time.time() * 1000
            async with page.expect_popup(timeout=10000) as opened:
                await link.click(timeout=10000)
            popup = await opened.value
            await popup.wait_for_load_state("domcontentloaded", timeout=10000)
            if not event_url_matches(popup.url, target["production_id"]):
                raise ValueError("Native popup did not reach the fixed public event")
            return popup
    return None


async def observe_target(playwright, target, report, inventory_path):
    browser, context = None, None
    evidence = Responses(target, report)
    try:
        report["phase"] = "launch"
        browser = await playwright.webkit.launch(headless=False)
        report["browser_version"] = browser.version
        context = await browser.new_context()
        context.set_default_timeout(10000)
        context.set_default_navigation_timeout(20000)
        context.on("response", evidence.observe)
        page = await context.new_page()
        report["phase"] = "performer"
        await page.goto(target["performer_url"], wait_until="load")
        popup = None
        while popup is None:
            state = await page.evaluate(PUBLIC_PAGE_SCRIPT)
            if evidence.denied or state["challenge_visible"]:
                raise DiagnosticOutcome("access-denial-or-challenge")
            popup = await native_link(page, target, evidence)
            if popup is None:
                await asyncio.sleep(0.25)
        report["visible_event_link_clicked"] = True
        report["event_opened_native_popup"] = True
        report["event_path"] = urlsplit(popup.url).path
        report["phase"] = "event"
        while True:
            state = await popup.evaluate(PUBLIC_PAGE_SCRIPT)
            report["dom"] = {key: state[key] for key in ("listing_count", "challenge_visible", "inventory_error")}
            if evidence.denied or state["challenge_visible"]:
                raise DiagnosticOutcome("access-denial-or-challenge")
            try:
                response = await asyncio.wait_for(evidence.candidates.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            if response.request.frame.page is not popup:
                report["non_popup_inventory_ignored"] = report.get("non_popup_inventory_ignored", 0) + 1
                continue
            await asyncio.wait_for(response.finished(), timeout=5)
            body = await asyncio.wait_for(response.body(), timeout=5)
            if not isinstance(body, bytes) or len(body) > MAX_BYTES:
                raise ValueError("Public inventory body is unavailable or oversized")
            metadata = state["event"]
            while metadata is None:
                await asyncio.sleep(0.25)
                state = await popup.evaluate(PUBLIC_PAGE_SCRIPT)
                if evidence.denied or state["challenge_visible"]:
                    raise DiagnosticOutcome("access-denial-or-challenge")
                metadata = state["event"]
            if evidence.denied:
                raise DiagnosticOutcome("access-denial-or-challenge")
            clean = validated_public_capture(json.loads(body), metadata, target)
            write_json(inventory_path, clean)
            report.update(status="captured", category="captured-full-native-inventory",
                          acquisition_method="original-response-playwright",
                          listing_count=len(clean["tickets"]), section_count=len({row["l"] for row in clean["tickets"]}),
                          event_date=utc_stamp(metadata["utc_date"]).isoformat(), metadata_identity_match=True,
                          metadata_time_match=True, captured_at=datetime.now(timezone.utc).isoformat())
            return
    finally:
        if context is not None:
            context.remove_listener("response", evidence.observe)
        if browser is not None:
            try:
                await asyncio.wait_for(browser.close(), timeout=5)
                report["browser_closed"] = True
            except Exception:
                report["browser_closed"] = False
                report["tooling_error"] = True


async def run_targets(playwright, directory, *, four_events=False):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "report.json").exists():
        raise ValueError("Preserve prior results and choose a new output directory")
    targets = FOUR_TARGETS if four_events else TARGETS
    report = {"status": "running", "all_captured": False, "database_calls": 0, "upload_calls": 0,
              "target_count": len(targets), "fixed_four_events": four_events,
              "engine": "playwright-webkit", "headless": False, "fresh_browser_per_target": True,
              "playwright_version": version("playwright"), "started_at": datetime.now(timezone.utc).isoformat(), "observations": []}
    stop = None
    for index, target in enumerate(targets):
        validate_target(target["event_url"], target["performer_url"], target["production_id"])
        row = {"sport": target["sport"], "production_id": target["production_id"], "status": "failed",
               "expected_event_utc": target["expected_event_utc"],
               "performer_path": urlsplit(target["performer_url"]).path,
               "responses": [], "started_at": datetime.now(timezone.utc).isoformat()}
        report["observations"].append(row)
        write_json(directory / "report.json", report)
        if stop:
            row.update(status="skipped", category=stop)
        else:
            try:
                await asyncio.wait_for(observe_target(playwright, target, row, directory / f"{index:02d}-{target['sport']}-inventory.json"), timeout=60)
            except DiagnosticOutcome as exc:
                row["status"] = "failed"
                row["category"] = exc.category
                if exc.category == "access-denial-or-challenge":
                    stop = "stopped-after-access-denial"
            except asyncio.TimeoutError:
                row["status"] = "failed"
                statuses = [item["status"] for item in row["responses"] if item["kind"] == "inventory" and item["phase"] == "event"]
                row["category"] = "provider-inventory-not-found" if statuses and statuses[-1] == 404 else "diagnostic-timeout"
                if row.get("phase") == "launch":
                    row["tooling_error"] = True
            except Exception as exc:
                row.update(status="failed", category="tooling-or-validation-error", error_type=type(exc).__name__, tooling_error=True)
                stop = "stopped-after-tooling-error"
        row["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(directory / "report.json", report)
    report.update(status="completed", all_captured=all(row["status"] == "captured" and row.get("browser_closed") is True for row in report["observations"]),
                  tooling_errors=sum(bool(row.get("tooling_error")) for row in report["observations"]),
                  finished_at=datetime.now(timezone.utc).isoformat())
    write_json(directory / "report.json", report)
    return report


async def execute(directory, *, four_events=False):
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        return await run_targets(playwright, directory, four_events=four_events)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("webkit-control"))
    parser.add_argument("--four-events", action="store_true", help="Observe the fixed Boston, Dallas, Buffalo and Saints cohort once each")
    args = parser.parse_args()
    report = asyncio.run(asyncio.wait_for(execute(args.directory, four_events=args.four_events),
                                        timeout=280 if args.four_events else 150))
    print(json.dumps({key: report[key] for key in ("status", "all_captured", "tooling_errors")}, sort_keys=True))
    return int(bool(report["tooling_errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
