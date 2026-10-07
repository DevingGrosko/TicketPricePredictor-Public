"""Capture one official scheduled game without storing or uploading a snapshot."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import re
import signal
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collector import validated_vivid_url
from vivid_inventory import _attempt_diagnostics


def run(sport, schedule_id, event_url, timeout, output):
    module = importlib.import_module(sport + "_schedule_collector")
    output = Path(output)
    browsers = []
    result = {"status": "failure", "event_type": sport, "schedule_id": schedule_id,
              "source_url": validated_vivid_url(event_url), "phase": "official-schedule"}
    original_browser = module.VividNFLBrowser

    def diagnostics():
        rows = []
        for browser in browsers:
            raw = getattr(browser, "capture_diagnostics", {})
            row = _attempt_diagnostics(raw if isinstance(raw, dict) else {})
            if isinstance(raw, dict) and isinstance(raw.get("inventory_recovery"), dict):
                row["inventory_recovery"] = raw["inventory_recovery"]
            rows.append(row)
        return rows

    def save():
        rows = diagnostics()
        result.update(captured_at=datetime.now(timezone.utc).isoformat(),
                      capture_browser_count=len(browsers), captures=rows,
                      capture_diagnostics=rows[-1] if rows else {})
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    def interrupted(_signal, _frame):
        result["error_type"] = "ProcessTimeout"
        save()
        raise TimeoutError("Bounded schedule smoke interrupted")

    previous = signal.signal(signal.SIGTERM, interrupted)

    def browser_factory(**kwargs):
        browser = original_browser(**kwargs)
        browsers.append(browser)
        close = browser.close

        def capture_failure_page():
            raw = getattr(browser, "capture_diagnostics", {})
            if isinstance(raw, dict) and any(row.get("status", 0) >= 400 for row in raw.get("responses", [])):
                try:
                    browser.driver.save_screenshot(str(output.with_suffix(".png")))
                except Exception:
                    pass
            close()

        browser.close = capture_failure_page
        return browser

    try:
        now = datetime.now(timezone.utc)
        games, sources = module.fetch_schedule_games(now, horizon_hours=168 if sport == "nfl" else 72)
        game = next((game for game in games if str(game.schedule_id) == schedule_id), None)
        if game is None:
            raise ValueError("Requested official game is not in the current half-hour tier")
        candidate_type = module.DiscoveredNFLGame if sport == "nfl" else module.DiscoveredNHLGame
        candidate = candidate_type(result["source_url"], game.name, game.local_date)
        resolution = module.ScheduleResolution(game, (candidate,), "explicit-official-schedule")
        result.update(phase="capture", schedule_sources=sources,
                      event_date=game.event_date.isoformat(), title=game.name)
        with patch.object(module, "VividNFLBrowser", browser_factory):
            url, event_date, snapshot = module._capture_resolution(resolution, headless=False, timeout=timeout)
        result.update(status="success", phase="complete", source_url=url, source_id=snapshot.source_id,
                      event_date=event_date.isoformat(), title=snapshot.title, venue=snapshot.venue,
                      currency=getattr(snapshot, "currency", "USD"), section_count=len(snapshot.sections),
                      inventory_listing_count=getattr(snapshot, "inventory_listing_count", None),
                      map_geometry=getattr(snapshot, "map_geometry", None))
    except Exception as exc:
        result["error_type"] = result.get("error_type", type(exc).__name__)
        # Record fixed exception labels rather than arbitrary browser messages.
        result["reported_exception_types"] = sorted(set(re.findall(
            r"TypeError|AttributeError|NameError|ImportError|ModuleNotFoundError|VividCaptureError", str(exc))))
    finally:
        save()
        signal.signal(signal.SIGTERM, previous)
    print(json.dumps({key: value for key, value in result.items() if key != "map_geometry"}), flush=True)
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sport", choices=("nfl", "nhl"), required=True)
    parser.add_argument("--schedule-id", required=True)
    parser.add_argument("--event-url", required=True)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.sport, args.schedule_id, args.event_url, args.timeout, args.output))
