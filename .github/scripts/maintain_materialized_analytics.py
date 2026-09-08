"""Drain derived analytics in small, retryable batches; never treat HTTP 200 as done.

Uses only the standard library so collection/maintenance runners do not need the
web application's dependencies or database credentials. Raw data is untouched
by this client; all work uses the existing collector-authenticated backfill API.
"""

from __future__ import annotations

import json
import os
import sys
import time
from http.client import IncompleteRead, RemoteDisconnected
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


ENDPOINT = "https://bunnyjeff.pythonanywhere.com/api/analytics/backfill"
SPORTS = ("mlb", "nfl", "nhl")
TRANSIENT_HTTP = frozenset({409, 429, 502, 503, 504})
MAX_ATTEMPTS = 8
MAX_BATCHES = 1200
REQUEST_TIMEOUT_SECONDS = 90
TIME_BUDGET_SECONDS = 780  # Leave room under the workflow's 15-minute hard limit.
MAX_RESPONSE_BYTES = 16384


class MaintenanceError(RuntimeError):
    """Maintenance did not finish or the server response cannot prove completion."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward the collector credential to a redirect destination.
        return None


def _validate_result(result: Any, sport: str) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise MaintenanceError(f"{sport}: expected a JSON object")
    if result.get("status") != "ok" or result.get("sport") != sport:
        raise MaintenanceError(f"{sport}: unexpected maintenance status or sport")
    if type(result.get("complete")) is not bool:
        raise MaintenanceError(f"{sport}: missing boolean completion flag")
    for key in ("remaining", "team_reports_remaining"):
        value = result.get(key)
        if type(value) is not int or value < 0:
            # Requiring the team counter also rejects an old worker that knows
            # only about event summaries, including during a rolling reload.
            raise MaintenanceError(f"{sport}: missing or invalid {key}")
    if result["complete"] and (
        result["remaining"] != 0 or result["team_reports_remaining"] != 0
    ):
        raise MaintenanceError(f"{sport}: completion flag contradicts remaining work")
    return result


def _request_batch(
    sport: str,
    token: str,
    deadline: float,
    *,
    open_request: Callable[..., Any],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    log: Callable[[str], None],
) -> dict[str, Any]:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        remaining_time = deadline - clock()
        if remaining_time <= 0:
            raise MaintenanceError(f"{sport}: maintenance time budget exhausted")
        request = Request(
            ENDPOINT,
            data=json.dumps({"sport": sport, "limit": 1}).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with open_request(
                request, timeout=min(REQUEST_TIMEOUT_SECONDS, remaining_time)
            ) as response:
                if response.status != 200:
                    raise MaintenanceError(f"{sport}: unexpected HTTP {response.status}")
                body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise MaintenanceError(f"{sport}: oversized maintenance response")
            try:
                result = json.loads(body)
            except (ValueError, UnicodeError) as exc:
                raise MaintenanceError(f"{sport}: invalid JSON response") from exc
            return _validate_result(result, sport)
        except HTTPError as exc:
            code = exc.code
            exc.close()
            if code not in TRANSIENT_HTTP:
                # Log only the code, never an arbitrary response body or token.
                raise MaintenanceError(f"{sport}: non-retryable HTTP {code}") from exc
            reason = f"HTTP {code}"
        except (URLError, TimeoutError, ConnectionError, IncompleteRead, RemoteDisconnected):
            # A timeout may follow a committed batch. Retrying asks the server
            # for whatever remains; it does not replay a raw snapshot.
            reason = "connection failure or timeout"
        if attempt == MAX_ATTEMPTS:
            raise MaintenanceError(f"{sport}: {reason}; exhausted {MAX_ATTEMPTS} attempts")
        delay = min(5 * attempt, 30)
        if clock() + delay >= deadline:
            raise MaintenanceError(f"{sport}: maintenance time budget exhausted")
        log(f"{sport}: {reason}; retry {attempt + 1}/{MAX_ATTEMPTS} in {delay}s")
        sleep(delay)
    raise AssertionError("unreachable")


def run_maintenance(
    token: str,
    *,
    sports: tuple[str, ...] = SPORTS,
    max_batches: int = MAX_BATCHES,
    time_budget: float = TIME_BUDGET_SECONDS,
    open_request: Callable[..., Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> dict[str, dict[str, Any]]:
    if not token or not token.strip() or "\n" in token or "\r" in token:
        raise MaintenanceError("COLLECTOR_INGEST_TOKEN is missing or invalid")
    if not sports or any(sport not in SPORTS for sport in sports):
        raise MaintenanceError("unsupported maintenance sport")
    if max_batches < 1 or time_budget <= 0:
        raise MaintenanceError("maintenance limits must be positive")
    open_request = open_request or build_opener(NoRedirect()).open
    deadline = clock() + time_budget
    completed: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for sport in sports:
        try:
            for batch in range(1, max_batches + 1):
                result = _request_batch(
                    sport, token, deadline, open_request=open_request,
                    clock=clock, sleep=sleep, log=log,
                )
                log(
                    f"{sport} batch {batch}: complete={str(result['complete']).lower()} "
                    f"remaining={result['remaining']} "
                    f"team_reports_remaining={result['team_reports_remaining']}"
                )
                if result["complete"] is True:
                    completed[sport] = result
                    break
            else:
                raise MaintenanceError(f"{sport}: batch limit reached with unfinished work")
        except MaintenanceError as exc:
            log(str(exc))
            failures.append(sport)
            # Still attempt other sports so one bad cohort does not prevent
            # repair of the others. The shared deadline continues to apply.
    if failures:
        raise MaintenanceError("Incomplete analytics maintenance: " + ", ".join(failures))
    return completed


def main() -> int:
    try:
        run_maintenance(os.environ.get("COLLECTOR_INGEST_TOKEN", ""))
    except MaintenanceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("All sports complete: event and team summaries have no remaining work.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
