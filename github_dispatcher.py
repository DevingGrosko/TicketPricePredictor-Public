"""Reliable 30-minute GitHub Actions dispatcher for PythonAnywhere.

GitHub's ``schedule`` event is intentionally best-effort and may be delayed or
dropped.  This lightweight service keeps the clock on PythonAnywhere while the
resource-intensive Selenium collection continues to run on GitHub-hosted
runners.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_FILE = PROJECT_DIR / "dispatcher_state.json"
DEFAULT_LOCK_FILE = PROJECT_DIR / "dispatcher.lock"
REPOSITORY = "DevingGrosko/TicketPricePredictor-Public"
WORKFLOW = "collect-ticket-prices.yml"
BRANCH = "main"
DISPATCH_URL = (
    f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/"
    f"{WORKFLOW}/dispatches"
)
INTERVAL = timedelta(minutes=30)
OFFSET = timedelta(minutes=8)
API_TIMEOUT_SECONDS = 20
RETRY_DELAYS_SECONDS = (10, 30, 60)


def capture_slot(now: datetime) -> datetime:
    """Return the :08/:38 UTC slot containing ``now``."""
    if now.tzinfo is None:
        raise ValueError("Dispatcher timestamps must be timezone-aware.")
    now_utc = now.astimezone(timezone.utc)
    epoch = int(now_utc.timestamp())
    interval_seconds = int(INTERVAL.total_seconds())
    offset_seconds = int(OFFSET.total_seconds())
    slot_epoch = ((epoch - offset_seconds) // interval_seconds) * interval_seconds
    slot_epoch += offset_seconds
    return datetime.fromtimestamp(slot_epoch, timezone.utc)


def next_slot(now: datetime) -> datetime:
    return capture_slot(now) + INTERVAL


def read_state(path: Path = DEFAULT_STATE_FILE) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_state(value: dict[str, Any], path: Path = DEFAULT_STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def dispatch_workflow(
    token: str,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> None:
    if not token:
        raise ValueError("GITHUB_ACTIONS_TOKEN is not configured.")
    body = json.dumps(
        {
            "ref": BRANCH,
            "inputs": {"dispatch_source": "pythonanywhere"},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        DISPATCH_URL,
        data=body,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "TicketPricePredictor-Dispatcher/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with opener(request, timeout=API_TIMEOUT_SECONDS) as response:
        status = getattr(response, "status", response.getcode())
        if status != 204:
            raise RuntimeError(f"GitHub dispatch returned HTTP {status}.")


def dispatch_slot(
    token: str,
    slot: datetime,
    *,
    state_file: Path = DEFAULT_STATE_FILE,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    slot_text = slot.astimezone(timezone.utc).isoformat()
    state = read_state(state_file)
    if state.get("last_dispatch_slot") == slot_text:
        print(f"Slot {slot_text} was already dispatched.", flush=True)
        return False

    last_error = ""
    for attempt, delay in enumerate((0, *RETRY_DELAYS_SECONDS), start=1):
        if delay:
            print(f"Retrying GitHub dispatch in {delay} seconds.", flush=True)
            sleep(delay)
        try:
            dispatch_workflow(token, opener=opener)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            print(
                f"Dispatch attempt {attempt} failed: {last_error}",
                file=sys.stderr,
                flush=True,
            )
            continue

        now = datetime.now(timezone.utc)
        write_state(
            {
                "status": "healthy",
                "updated_at": now.isoformat(),
                "last_dispatch_slot": slot_text,
                "last_dispatch_at": now.isoformat(),
                "next_dispatch_slot": (slot + INTERVAL).isoformat(),
                "consecutive_failures": 0,
                "last_error": None,
            },
            state_file,
        )
        print(f"Dispatched GitHub collector for slot {slot_text}.", flush=True)
        return True

    failures = int(state.get("consecutive_failures") or 0) + 1
    write_state(
        {
            **state,
            "status": "degraded",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "consecutive_failures": failures,
            "last_error": last_error,
        },
        state_file,
    )
    raise RuntimeError(f"GitHub dispatch failed after retries: {last_error}")


def run_service(
    token: str,
    *,
    state_file: Path = DEFAULT_STATE_FILE,
    lock_file: Path = DEFAULT_LOCK_FILE,
) -> int:
    stopping = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another dispatcher process is already running.", file=sys.stderr)
            return 1

        print("GitHub dispatcher started; target slots are :08 and :38 UTC.", flush=True)
        while not stopping:
            now = datetime.now(timezone.utc)
            slot = capture_slot(now)
            try:
                dispatch_slot(token, slot, state_file=state_file)
            except Exception as exc:
                print(f"Dispatcher cycle failed: {type(exc).__name__}: {exc}", file=sys.stderr)

            wake_at = next_slot(datetime.now(timezone.utc))
            while not stopping:
                remaining = (wake_at - datetime.now(timezone.utc)).total_seconds()
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 30))

    print("GitHub dispatcher stopped.", flush=True)
    return 0


def show_status(path: Path = DEFAULT_STATE_FILE) -> int:
    state = read_state(path)
    if not state:
        print("No dispatcher health record exists yet.")
        return 1
    print(json.dumps(state, indent=2))
    return 0 if state.get("status") == "healthy" else 1


def main() -> int:
    load_dotenv(PROJECT_DIR / ".env", override=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "once", "status"))
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    args = parser.parse_args()

    if args.command == "status":
        return show_status(args.state_file)

    token = os.getenv("GITHUB_ACTIONS_TOKEN", "").strip()
    if not token:
        print("GITHUB_ACTIONS_TOKEN is not configured.", file=sys.stderr)
        return 2
    if args.command == "once":
        dispatch_slot(token, capture_slot(datetime.now(timezone.utc)), state_file=args.state_file)
        return 0
    return run_service(token, state_file=args.state_file)


if __name__ == "__main__":
    raise SystemExit(main())
