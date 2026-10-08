"""Four fixed, read-only normal-link captures, each in its own Firefox process."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

if __package__:
    from .firefox_inventory_diagnostic import sanitize_inventory, utc_stamp, validate_target, write_json
else:
    from firefox_inventory_diagnostic import sanitize_inventory, utc_stamp, validate_target, write_json

NHL = {"sport": "nhl", "production_id": "7302493",
       "event_url": "https://www.vividseats.com/boston-bruins-tickets-td-garden-10-8-2026/production/7302493",
       "performer_url": "https://www.vividseats.com/boston-bruins-tickets--sports-nhl-hockey/performer/104",
       "expected_event_utc": "2026-10-08T23:00:00Z"}
NFL = {"sport": "nfl", "production_id": "6493143",
       "event_url": "https://www.vividseats.com/new-orleans-saints-tickets-new-orleans-caesars-superdome-3-7-2027/production/6493143",
       "performer_url": "https://www.vividseats.com/en/new-orleans-saints-tickets--sports-nfl-football/performer/597",
       "expected_event_utc": "2026-10-11T17:00:00Z"}
NHL_SECOND = {"sport": "nhl", "production_id": "7301789",
       "event_url": "https://www.vividseats.com/boston-bruins-tickets-td-garden-10-10-2026/production/7301789",
       "performer_url": NHL["performer_url"], "expected_event_utc": "2026-10-10T17:00:00Z"}
PLAN = (NHL, NFL, NHL_SECOND, NHL)


def capture_process(command):
    # A new process group contains only this diagnostic and the drivers/browsers
    # it starts. Deadline cleanup never selects unrelated browser processes.
    process = subprocess.Popen(command, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        return process.wait(timeout=120)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        return 124


def run_link_canary(directory, *, capture=capture_process):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    report_path = directory / "report.json"
    if report_path.exists():
        raise ValueError("Canary output already exists; preserve it and choose a new directory")
    report = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
              "database_calls": 0, "fresh_owned_process_per_capture": True, "observations": []}
    write_json(report_path, report)
    helper = Path(__file__).with_name("firefox_inventory_diagnostic.py")
    for index, target in enumerate(PLAN):
        validate_target(target["event_url"], target["performer_url"], target["production_id"])
        utc_stamp(target["expected_event_utc"])
        observation_dir = directory / f"{index:02d}-{target['sport']}-{target['production_id']}"
        observation_dir.mkdir()
        result_path, inventory_path = observation_dir / "result.json", observation_dir / "inventory.json"
        command = [sys.executable, str(helper), "--navigation", "performer", "--timeout", "75",
                   "--event-url", target["event_url"], "--performer-url", target["performer_url"],
                   "--production-id", target["production_id"], "--expected-event-utc", target["expected_event_utc"],
                   "--output", str(result_path), "--inventory-output", str(inventory_path)]
        entry = {"index": index, **target, "status": "failed", "result_file": str(result_path.relative_to(directory)),
                 "inventory_file": str(inventory_path.relative_to(directory))}
        try:
            entry["process_exit_code"] = capture(command)
            result = json.loads(result_path.read_text())
            entry["diagnostics"] = result
            if (entry["process_exit_code"] != 0 or result.get("status") != "captured"
                    or result.get("production_id") != target["production_id"] or result.get("navigation_mode") != "performer"
                    or result.get("acquisition_method") != "original-response-bidi" or not result.get("browser_closed")
                    or not result.get("visible_event_link_clicked") or not result.get("event_page_reached")
                    or result.get("quantity_actions") != [] or result.get("follow_up_fetch_attempted")
                    or not result.get("metadata_identity_match") or not result.get("metadata_time_match")
                    or utc_stamp(result.get("event_date")) != utc_stamp(target["expected_event_utc"])
                    or 200 not in result.get("original_inventory_statuses", [])):
                raise ValueError("Capture did not satisfy the fixed native-link control")
            inventory = sanitize_inventory(json.loads(inventory_path.read_text()), target["production_id"])
            if result.get("captured_listing_count") != len(inventory["tickets"]):
                raise ValueError("Capture count does not match saved inventory")
            entry["status"] = "captured"
            entry["listing_count"] = len(inventory["tickets"])
        except Exception as exc:
            entry["error_type"] = type(exc).__name__
        report["observations"].append(entry)
        write_json(report_path, report)
        print(json.dumps({key: entry.get(key) for key in ("index", "sport", "production_id", "status", "listing_count", "process_exit_code")}, sort_keys=True), flush=True)
    report["status"] = "passed" if all(row["status"] == "captured" for row in report["observations"]) else "failed"
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_json(report_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("firefox-link-canary"))
    args = parser.parse_args()
    result = run_link_canary(args.directory)
    print("FIREFOX_LINK_CANARY " + result["status"], flush=True)
    return int(result["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
