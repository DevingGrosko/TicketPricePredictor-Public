"""GitHub-hosted diagnostic probe for Ticketmaster and SeatGeek inventory.

This runs an ordinary headless Chrome session. It does not hide WebDriver,
change the browser fingerprint or User-Agent, use proxies, solve challenges,
replay tokens, or bypass access controls. The probe performs only ordinary page
navigation, limited scrolling, and non-purchasing inventory-view interactions.
It writes a sanitized report and screenshot so we can determine whether a
GitHub-hosted runner is actually served section-level inventory JSON.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlparse

from manual_provider_capture import (
    ProviderResponse,
    analyze_responses,
    decode_response_text,
    default_output_path,
    response_belongs_to_provider,
    validate_event_url,
    write_report,
)


DEFAULT_SCREENSHOT = Path("provider_probe.png")
DEFAULT_DURATION_SECONDS = 55
MAX_DURATION_SECONDS = 180
MAX_BODY_TEXT_CHARS = 120_000
SETTLE_AFTER_RESPONSE_SECONDS = 6

BLOCK_MARKERS = (
    "pardon the interruption",
    "are you a real fan",
    "verify you are human",
    "verify that you are human",
    "unusual activity",
    "access denied",
    "request blocked",
    "temporarily blocked",
    "too many requests",
    "captcha",
    "security challenge",
    "automated access",
    "bot detection",
    "forbidden",
)

# These interactions are limited to revealing inventory. Purchase, login,
# checkout, and account flows are deliberately excluded.
SAFE_ACTION_TERMS = (
    "view tickets",
    "see tickets",
    "find tickets",
    "select tickets",
    "view seats",
    "seat map",
    "map view",
    "list view",
    "show prices",
    "show tickets",
)
UNSAFE_ACTION_TERMS = (
    "checkout",
    "buy now",
    "place order",
    "sign in",
    "log in",
    "create account",
    "continue to payment",
)


@dataclass(frozen=True)
class ProbeState:
    event_url: str
    final_url: str
    page_title: str
    main_document_statuses: tuple[int, ...]
    provider_statuses: tuple[int, ...]
    blocked_markers: tuple[str, ...]
    safe_clicks: tuple[str, ...]
    scroll_steps: int
    screenshot: str
    elapsed_seconds: float

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_markers) or any(
            status in {403, 429} for status in self.main_document_statuses
        )


def detect_block_markers(text: str) -> tuple[str, ...]:
    normalized = " ".join(str(text or "").casefold().split())
    return tuple(marker for marker in BLOCK_MARKERS if marker in normalized)


def classify_probe(report: dict[str, Any], state: ProbeState) -> str:
    if state.blocked:
        return "blocked"
    if int(report.get("candidate_count") or 0) > 0:
        return "section_inventory_found"
    if int(report.get("json_responses_parsed") or 0) > 0:
        return "provider_json_found_no_section_records"
    if state.main_document_statuses and all(
        status >= 400 for status in state.main_document_statuses
    ):
        return "page_request_failed"
    return "page_loaded_no_inventory_json"


def _safe_click_text(value: str) -> bool:
    normalized = " ".join(str(value or "").casefold().split())
    if not normalized or any(term in normalized for term in UNSAFE_ACTION_TERMS):
        return False
    return any(term in normalized for term in SAFE_ACTION_TERMS)


def _drain_network_logs(
    driver: Any,
    provider: str,
    request_metadata: dict[str, tuple[str, int | None, str]],
    captured: dict[str, ProviderResponse],
    main_document_statuses: list[int],
    provider_statuses: list[int],
) -> int:
    completed_bodies = 0
    for entry in driver.get_log("performance"):
        try:
            message = json.loads(entry["message"])["message"]
            method = message["method"]
            params = message["params"]
        except (KeyError, TypeError, json.JSONDecodeError):
            continue

        if method == "Network.responseReceived":
            response = params.get("response") or {}
            url = str(response.get("url") or "")
            status_raw = response.get("status")
            status = int(status_raw) if isinstance(status_raw, (int, float)) else None
            resource_type = str(params.get("type") or "")
            if resource_type == "Document" and status is not None:
                main_document_statuses.append(status)

            if not response_belongs_to_provider(url, provider):
                continue
            if status is not None:
                provider_statuses.append(status)
            request_id = str(params.get("requestId") or "")
            request_metadata[request_id] = (
                url,
                status,
                str(response.get("mimeType") or ""),
            )
            continue

        if method != "Network.loadingFinished":
            continue
        request_id = str(params.get("requestId") or "")
        metadata = request_metadata.get(request_id)
        if metadata is None or request_id in captured:
            continue

        try:
            result = driver.execute_cdp_cmd(
                "Network.getResponseBody",
                {"requestId": request_id},
            )
        except Exception:
            continue
        body = decode_response_text(
            str(result.get("body") or ""),
            "base64" if result.get("base64Encoded") else "",
        )
        if not body:
            continue
        url, status, mime_type = metadata
        captured[request_id] = ProviderResponse(
            url=url,
            status=status,
            mime_type=mime_type,
            body=body,
        )
        completed_bodies += 1
    return completed_bodies


def _perform_safe_inventory_clicks(driver: Any, clicked: set[str]) -> list[str]:
    newly_clicked: list[str] = []
    try:
        elements = driver.find_elements(
            "xpath",
            "//button | //a[@role='button'] | //a",
        )
    except Exception:
        return newly_clicked

    for element in elements[:250]:
        try:
            text = " ".join(
                (
                    element.text
                    or element.get_attribute("aria-label")
                    or element.get_attribute("title")
                    or ""
                ).split()
            )
            identity = text.casefold()
            if identity in clicked or not _safe_click_text(text):
                continue
            if not element.is_displayed() or not element.is_enabled():
                continue
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});",
                element,
            )
            element.click()
            clicked.add(identity)
            newly_clicked.append(text[:160])
            # One inventory-view transition per pass is enough; the page may
            # replace the DOM immediately after the click.
            break
        except Exception:
            continue
    return newly_clicked


def run_probe(
    event_url: str,
    *,
    provider: str,
    duration: int,
    output: Path,
    screenshot: Path,
    include_sanitized_payloads: bool,
) -> tuple[dict[str, Any], int]:
    try:
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Selenium is not installed. Run: pip install -r requirements.txt"
        ) from exc

    if duration < 10 or duration > MAX_DURATION_SECONDS:
        raise ValueError(
            f"duration must be between 10 and {MAX_DURATION_SECONDS} seconds"
        )

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1440,1200")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    options.page_load_strategy = "eager"
    # No stealth flags, modified User-Agent, webdriver masking, proxy routing,
    # fingerprint changes, challenge handling, or persistent session import.

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(min(duration, 60))
    driver.execute_cdp_cmd("Network.enable", {})

    request_metadata: dict[str, tuple[str, int | None, str]] = {}
    captured: dict[str, ProviderResponse] = {}
    main_document_statuses: list[int] = []
    provider_statuses: list[int] = []
    clicked: set[str] = set()
    click_labels: list[str] = []
    blocked: set[str] = set()
    scroll_steps = 0
    started = time.monotonic()
    last_response_at: float | None = None

    try:
        driver.get_log("performance")
        try:
            driver.get(event_url)
        except TimeoutException:
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass

        deadline = started + duration
        next_scroll_at = started
        next_click_at = started + 2
        next_text_check_at = started

        while time.monotonic() < deadline:
            now = time.monotonic()
            new_bodies = _drain_network_logs(
                driver,
                provider,
                request_metadata,
                captured,
                main_document_statuses,
                provider_statuses,
            )
            if new_bodies:
                last_response_at = now

            if now >= next_text_check_at:
                try:
                    body_text = driver.find_element("tag name", "body").text
                except Exception:
                    body_text = ""
                blocked.update(
                    detect_block_markers(body_text[:MAX_BODY_TEXT_CHARS])
                )
                try:
                    blocked.update(detect_block_markers(driver.title))
                except Exception:
                    pass
                next_text_check_at = now + 1

            if blocked or any(status in {403, 429} for status in main_document_statuses):
                break

            if now >= next_click_at:
                labels = _perform_safe_inventory_clicks(driver, clicked)
                click_labels.extend(labels)
                next_click_at = now + 4

            if now >= next_scroll_at:
                try:
                    driver.execute_script(
                        "window.scrollBy(0, Math.max(500, window.innerHeight * 0.7));"
                    )
                    scroll_steps += 1
                except Exception:
                    pass
                next_scroll_at = now + 1.2

            if (
                captured
                and last_response_at is not None
                and now - last_response_at >= SETTLE_AFTER_RESPONSE_SECONDS
            ):
                # Once provider responses stop arriving, extending the probe no
                # longer improves the diagnostic result.
                break
            time.sleep(0.25)

        _drain_network_logs(
            driver,
            provider,
            request_metadata,
            captured,
            main_document_statuses,
            provider_statuses,
        )

        screenshot.parent.mkdir(parents=True, exist_ok=True)
        try:
            driver.save_screenshot(str(screenshot))
        except Exception:
            pass

        try:
            final_url = str(driver.current_url or "")
        except Exception:
            final_url = ""
        try:
            page_title = str(driver.title or "")
        except Exception:
            page_title = ""
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    state = ProbeState(
        event_url=event_url,
        final_url=final_url,
        page_title=page_title,
        main_document_statuses=tuple(main_document_statuses[-50:]),
        provider_statuses=tuple(provider_statuses[-250:]),
        blocked_markers=tuple(sorted(blocked)),
        safe_clicks=tuple(click_labels),
        scroll_steps=scroll_steps,
        screenshot=str(screenshot),
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
    report = analyze_responses(
        captured.values(),
        provider=provider,
        event_url=event_url,
        capture_mode="github-hosted-standard-browser",
        include_sanitized_payloads=include_sanitized_payloads,
    )
    outcome = classify_probe(report, state)
    report["outcome"] = outcome
    report["probe_state"] = asdict(state)
    report["runner_constraints"] = {
        "standard_headless_chrome": True,
        "webdriver_hidden": False,
        "user_agent_modified": False,
        "proxy_rotation": False,
        "captcha_solver": False,
        "challenge_token_replay": False,
        "session_cookie_import": False,
    }
    write_report(report, output)

    if outcome == "section_inventory_found":
        return report, 0
    if outcome == "blocked":
        return report, 3
    return report, 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event_url")
    parser.add_argument(
        "--provider",
        choices=("auto", "ticketmaster", "seatgeek"),
        default="auto",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=DEFAULT_DURATION_SECONDS,
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--screenshot", type=Path, default=DEFAULT_SCREENSHOT)
    parser.add_argument("--include-sanitized-payloads", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    provider = validate_event_url(args.event_url, args.provider)
    output = args.output or default_output_path(provider, "github-probe")
    report, exit_code = run_probe(
        args.event_url,
        provider=provider,
        duration=args.duration,
        output=output,
        screenshot=args.screenshot,
        include_sanitized_payloads=args.include_sanitized_payloads,
    )
    print(
        f"Provider probe outcome={report['outcome']} "
        f"candidates={report['candidate_count']} "
        f"json={report['json_responses_parsed']} output={output}"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
