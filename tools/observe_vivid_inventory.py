"""One ordinary, read-only event observation; no reloads or request replay."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import signal
import sys
import time
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collector import validated_vivid_url
from nfl_collector import VividNFLBrowser
from nhl_collector import NHLSnapshotParser
from vivid_inventory import INVENTORY_PATHS, VividCaptureError, http_category


class Observation:
    def __init__(self, driver):
        self.driver = driver
        self.started = time.monotonic()
        self.origin = None
        self.active = False
        self.events = []
        self.page_states = []
        self.next_probe = 0
        self.reloads = 0
        self.phase = "event"
        self.homepage_document_status = None

    def __getattr__(self, name):
        return getattr(self.driver, name)

    def get(self, url):
        self.active = True
        self.phase = "homepage" if urlsplit(url).path == "/" else "event"
        return self.driver.get(url)

    def refresh(self):
        self.reloads += 1
        return self.driver.refresh()

    def get_log(self, kind):
        entries = self.driver.get_log(kind)
        if not self.active or kind != "performance":
            return entries
        for entry in entries:
            try:
                message = json.loads(entry["message"])["message"]
                method, params = message["method"], message["params"]
                if self.origin is None and isinstance(params.get("timestamp"), (int, float)):
                    self.origin = params["timestamp"]
                if method not in ("Network.requestWillBeSent", "Network.responseReceived"):
                    continue
                source = params.get("request" if method == "Network.requestWillBeSent" else "response") or {}
                url = urlsplit(source.get("url", ""))
                if url.hostname not in {"www.vividseats.com", "vividseats.com"}:
                    continue
                document = params.get("type") == "Document"
                if not document and url.path not in INVENTORY_PATHS:
                    continue
                query = parse_qs(url.query)
                allowed = {"productionId", "quantity", "recommended", "sf", "includeIpAddress", "currency", "localizeCurrency"}
                safe_query = {key: value[0] for key, value in query.items() if key in allowed
                              and len(value) == 1 and re.fullmatch(r"\d{1,12}|true|false|[A-Z]{3}", value[0])}
                event = {"kind": method.split(".")[-1], "path": url.path,
                         "query": safe_query, "resource_type": params.get("type"),
                         "phase": self.phase,
                         "observed_seconds": round(time.monotonic() - self.started, 3)}
                if self.origin is not None and isinstance(params.get("timestamp"), (int, float)):
                    event["network_seconds"] = round(params["timestamp"] - self.origin, 3)
                if method == "Network.responseReceived":
                    event["status"] = int(source.get("status", 0))
                    if document and self.phase == "homepage":
                        self.homepage_document_status = event["status"]
                else:
                    event["method"] = source.get("method")
                self.events.append(event)
                if event.get("status") in (401, 403, 429):
                    raise VividCaptureError(http_category(event["status"]), {"response": event})
            except (KeyError, ValueError, TypeError):
                continue
        if time.monotonic() >= self.next_probe:
            self.next_probe = time.monotonic() + 1
            state = self.driver.execute_script(r"""
const text = document.body ? document.body.innerText : '';
const count = text.match(/([\d,]+)\s+listings\b/i);
return {ready_state:document.readyState,
  inventory_error:/sorry, there was an error|something went wrong/i.test(text),
  quantity_modal:/how many tickets/i.test(text),
  listings:count ? Number(count[1].replace(/,/g,'')) : null,
  challenge:/captcha|verify you are human|access denied|request blocked/i.test(text)};
""")
            state["observed_seconds"] = round(time.monotonic() - self.started, 3)
            state["phase"] = self.phase
            self.page_states.append(state)
            if state.get("challenge"):
                raise VividCaptureError("visible-provider-challenge", {})
        return entries


def initialize_homepage(observed):
    """Use ordinary navigation; fail closed before the event on access denial."""
    observed.get("https://www.vividseats.com/")
    observed.get_log("performance")
    if observed.homepage_document_status != 200:
        raise VividCaptureError("homepage-initialization-failed", {})
    if not observed.page_states or observed.page_states[-1].get("ready_state") != "complete":
        raise VividCaptureError("homepage-not-loaded", {})


def run(event_url, seconds, output, full_renderer=False, homepage_first=False):
    from selenium import webdriver
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"status": "failure", "source_url": validated_vivid_url(event_url),
              "observation_seconds": seconds, "full_renderer": full_renderer,
              "homepage_first": homepage_first}
    browser = None
    observed = None
    original_chrome = webdriver.Chrome

    def full_chrome(*args, **kwargs):
        options = kwargs["options"]
        options.page_load_strategy = "normal"
        prefs = dict(options.experimental_options.get("prefs", {}))
        prefs.pop("profile.managed_default_content_settings.images", None)
        options.add_experimental_option("prefs", prefs)
        return original_chrome(*args, **kwargs)

    def save():
        report.update(captured_at=datetime.now(timezone.utc).isoformat(),
                      network=observed.events if observed else [],
                      page_states=observed.page_states if observed else [],
                      reloads=observed.reloads if observed else 0,
                      capture_diagnostics=getattr(browser, "capture_diagnostics", {}) if browser else {})
        output.write_text(json.dumps(report, indent=2) + "\n")

    def interrupted(_signal, _frame):
        report["error_type"] = "ProcessTimeout"
        save()
        raise TimeoutError("Observation process bound")

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        if homepage_first and not full_renderer:
            raise ValueError("Homepage initialization diagnostic requires the normal full renderer")
        with patch.object(webdriver, "Chrome", full_chrome) if full_renderer else nullcontext():
            browser = VividNFLBrowser(headless=False, timeout=seconds + 15)
        if full_renderer:
            browser.driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": []})
        observed = Observation(browser.driver)
        browser.driver = observed
        if homepage_first:
            initialize_homepage(observed)
            report["homepage_initialized"] = True
        raw, at = browser.capture(event_url, inventory_404_settle_seconds=seconds)
        snapshot = NHLSnapshotParser.parse(raw)
        report.update(status="success", source_id=snapshot.source_id, title=snapshot.title,
                      event_date=at.isoformat(), currency=snapshot.currency,
                      inventory_listing_count=len(raw["tickets"]), section_count=len(snapshot.sections),
                      map_geometry=snapshot.map_geometry)
    except Exception as exc:
        report.update(error_type=report.get("error_type", type(exc).__name__),
                      category=getattr(exc, "category", "observation-error"))
    finally:
        save()
        if browser:
            try:
                browser.driver.save_screenshot(str(output.with_suffix(".png")))
            except Exception:
                pass
            browser.close()
        signal.signal(signal.SIGTERM, previous)
    print(json.dumps({key: value for key, value in report.items() if key != "map_geometry"}), flush=True)
    return 0 if report["status"] == "success" else 1


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--event-url", required=True)
    cli.add_argument("--seconds", type=int, default=60)
    cli.add_argument("--output", type=Path, required=True)
    cli.add_argument("--full-renderer", action="store_true")
    cli.add_argument("--homepage-first", action="store_true")
    args = cli.parse_args()
    raise SystemExit(run(args.event_url, args.seconds, args.output, args.full_renderer, args.homepage_first))
