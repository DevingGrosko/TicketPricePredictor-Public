"""One browser-only capture using exact pre-free source; never invokes ingestion."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import sys
import time
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

SOURCE_COMMIT = "029f1cdb93ed6fd70f56f6ab6e0f32f173a6e22e"
SOURCE_HASHES = {
    "collector.py": "f1b3ffd294d8cda2debeb63a08899088bca5e8de23d8ca4b4b8f94c777ba2aad",
    "nfl_collector.py": "f3d6934da85385531897c56f060b4ad4d7955a35f779db64ce4fd438da07dd60",
    "nhl_collector.py": "7ccd1af4f3d612c45a840a37a8ded3f40dd292be90e494b536d1ed1fd9f53e6e",
    "nfl_metadata.py": "186b7558e717c33d1c13a48b4db121940a04cef285477a9a0b344e28ad1262af",
    "models.py": "97d1e7cadf2c927fa58b22e74e417d57655b4cf1d10c1a65ea74a9c79d6503e5",
    "Flask_App/database_config.py": "829af19f28b730478659d9bbf7c351fac2f3b749869244cfc86d12bf6667b4f5",
    "Flask_App/report_policy.py": "488bcac5806c9ee11b5da0f6d0e65ba952dd97121a33289fe2f9d60972b54904",
}
EVENT = "https://www.vividseats.com/boston-bruins-tickets-td-garden-10-8-2026/production/7302493"
QUERY_PATTERNS = {
    "productionId": r"[0-9]{1,12}", "priceGroupId": r"[0-9]{1,12}",
    "quantity": r"[0-9]{1,12}", "currency": r"[A-Z]{3}",
    "includeIpAddress": r"true|false|0|1", "localizeCurrency": r"true|false|0|1",
}


def import_historical(source):
    for name, expected in SOURCE_HASHES.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != expected:
            raise ValueError("historical-source-integrity-failed")
    sys.path.insert(0, str(source))
    from nfl_collector import VividNFLBrowser
    from nhl_collector import NHLSnapshotParser
    return VividNFLBrowser, NHLSnapshotParser


class PassiveLogs:
    """Observe the original log reads; return each original list unchanged."""
    def __init__(self, driver):
        self.driver = driver
        self.responses = []

    def __getattr__(self, name):
        return getattr(self.driver, name)

    def get_log(self, kind):
        entries = self.driver.get_log(kind)
        if kind == "performance":
            for entry in entries:
                try:
                    message = json.loads(entry["message"])["message"]
                    if message.get("method") != "Network.responseReceived":
                        continue
                    params = message["params"]
                    response = params["response"]
                    url = urlsplit(response["url"])
                    document = params.get("type") == "Document" and url.path == urlsplit(EVENT).path
                    inventory = url.path in {"/hermes/api/v1/listings", "/hermes/api/v2/listings"}
                    if url.hostname not in {"www.vividseats.com", "vividseats.com"} or not (document or inventory):
                        continue
                    query = parse_qs(url.query)
                    safe = {key: values[0] for key, values in query.items()
                            if key in QUERY_PATTERNS and len(values) == 1
                            and re.fullmatch(QUERY_PATTERNS[key], values[0])}
                    self.responses.append({"path": url.path, "status": int(response["status"]),
                                           "resource_type": params.get("type"), "query": safe})
                except (KeyError, ValueError, TypeError):
                    continue
        return entries


def run(source, output, *, headless, timeout, chrome_binary=None):
    if not 10 <= timeout <= 60:
        raise ValueError("timeout-must-be-between-10-and-60-seconds")
    if output.exists():
        raise ValueError("refusing-existing-control-output")
    logging.getLogger("selenium").setLevel(logging.CRITICAL)
    logging.getLogger("urllib3").setLevel(logging.CRITICAL)
    browser = observed = None
    report = {"source_commit": SOURCE_COMMIT, "source_integrity_verified": False,
              "status": "failure", "source_url": EVENT, "headless": headless,
              "timeout_seconds": timeout, "database_or_ingestion_calls": False,
              "started_at": datetime.now(timezone.utc).isoformat()}
    start = time.monotonic()
    try:
        browser_class, parser = import_historical(source)
        report["source_integrity_verified"] = True
        if chrome_binary is not None:
            if not chrome_binary.is_absolute() or not chrome_binary.is_file():
                raise ValueError("explicit-chrome-binary-is-unavailable")
            from selenium import webdriver
            original_options = webdriver.ChromeOptions

            def selected_browser_options():
                options = original_options()
                options.binary_location = str(chrome_binary)
                return options
        with patch('selenium.webdriver.ChromeOptions', selected_browser_options) if chrome_binary else nullcontext():
            browser = browser_class(headless=headless, timeout=timeout)
        import selenium
        capabilities = browser.driver.capabilities
        runtime = {"selenium": selenium.__version__, "platform": sys.platform}
        for key, raw in {"chrome": capabilities.get("browserVersion"),
                         "chromedriver": (capabilities.get("chrome") or {}).get("chromedriverVersion")}.items():
            value = str(raw or "").split(" ")[0]
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,5}", value):
                runtime[key] = value
        report["runtime"] = runtime
        observed = PassiveLogs(browser.driver)
        browser.driver = observed
        raw, event_date = browser.capture(EVENT)
        snapshot = parser.parse(raw)
        report.update(status="success", source_id=str(snapshot.source_id),
                      production_identity_matches=str(snapshot.source_id) == "7302493",
                      inventory_listing_count=len(raw.get("tickets") or []),
                      section_count=len(snapshot.sections), event_date=event_date.isoformat())
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    finally:
        report["responses"] = observed.responses if observed else []
        report["seconds"] = round(time.monotonic() - start, 3)
        if browser is not None:
            try:
                browser.close()
                report["browser_closed"] = True
            except Exception:
                report["browser_closed"] = False
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n")
        print("PRE_FREE_CONTROL_REPORT " + json.dumps(report), flush=True)
    return 0 if report["status"] == "success" and report.get("production_identity_matches") else 1


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--source-directory", type=Path, default=Path(__file__).resolve().parent)
    cli.add_argument("--output", type=Path, required=True)
    cli.add_argument("--headless", action="store_true")
    cli.add_argument("--timeout", type=int, default=45)
    cli.add_argument("--chrome-binary", type=Path)
    args = cli.parse_args()
    raise SystemExit(run(args.source_directory.resolve(), args.output, headless=args.headless,
                         timeout=args.timeout, chrome_binary=args.chrome_binary))
