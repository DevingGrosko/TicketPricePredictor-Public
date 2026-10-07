"""Validate completed public Vivid inventory responses without session replay."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import time
from urllib.parse import parse_qs, urlsplit

MAX_INVENTORY_BYTES = 16 * 1024 * 1024
INVENTORY_PATHS = {"/hermes/api/v1/listings", "/hermes/api/v2/listings"}
CURRENT_INVENTORY_COOLDOWN_SECONDS = 15


class VividCaptureError(RuntimeError):
    """A classified failure whose diagnostics contain no headers or raw bodies."""

    def __init__(self, category: str, diagnostics: dict, *, retryable: bool = False):
        self.category = category
        self.diagnostics = diagnostics
        self.retryable = retryable
        super().__init__(category + ": " + json.dumps(diagnostics, sort_keys=True))


def inventory_request(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.hostname in {"www.vividseats.com", "vividseats.com"} and parsed.path in INVENTORY_PATHS


def unfiltered_request(url: str) -> bool:
    """Quantity/recommendation/pagination subsets are not whole-market snapshots."""
    query = parse_qs(urlsplit(url).query)
    for key in ("quantity", "offset", "page"):
        if key in query and query[key] != ["0"]:
            return False
    for key in ("recommended", "sf"):
        if key in query and any(value.casefold() not in {"false", "0"} for value in query[key]):
            return False
    return not any(key in query for key in ("limit", "pageSize"))


def read_inventory(driver, request_id: str) -> dict:
    result = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id})
    body = result.get("body", "")
    if not isinstance(body, str) or len(body) > MAX_INVENTORY_BYTES * 2:
        raise ValueError("Inventory response exceeds the size limit")
    raw = base64.b64decode(body, validate=True) if result.get("base64Encoded") else body.encode("utf-8")
    if len(raw) > MAX_INVENTORY_BYTES:
        raise ValueError("Inventory response exceeds the size limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Inventory response is not a JSON object")
    return value


def validate_inventory(payload: dict, production_id: str) -> None:
    metadata, tickets = payload.get("global"), payload.get("tickets")
    if not isinstance(metadata, list) or not metadata or not isinstance(metadata[0], dict) or not isinstance(tickets, list):
        raise VividCaptureError("unexpected-inventory-payload", {"production_id": production_id})
    if str(metadata[0].get("productionId") or "") != production_id:
        raise VividCaptureError("inventory-identity-mismatch", {"production_id": production_id})
    if not tickets:
        raise VividCaptureError("empty-inventory", {"production_id": production_id})


def http_category(status: int) -> str:
    if status == 429:
        return "provider-rate-limited"
    if status in (401, 403):
        return "provider-access-denied"
    if status == 404:
        return "provider-inventory-not-found"
    if status >= 500:
        return "provider-server-error"
    return "provider-http-error"


def _attempt_diagnostics(raw: dict) -> dict:
    """Retain response evidence without headers, bodies, or session fields."""
    result = {}
    production_id = str(raw.get("production_id") or "")
    if production_id.isdigit() and len(production_id) <= 12:
        result["production_id"] = production_id
    if isinstance(raw.get("document_status"), int):
        result["document_status"] = raw["document_status"]
    result["responses"] = [
        {"path": row["path"], "status": row["status"]}
        for row in raw.get("responses", []) or []
        if isinstance(row, dict) and row.get("path") in INVENTORY_PATHS
        and isinstance(row.get("status"), int)
    ][:10]
    for key in ("body_read_retries", "inventory_modal_seen", "inventory_clear_seen",
                "navigation_timeout", "filtered_responses_rejected"):
        if isinstance(raw.get(key), (int, bool)):
            result[key] = raw[key]
    result["inventory_view_actions"] = [
        action for action in raw.get("inventory_view_actions", []) or []
        if action in {"quantity-modal-all", "quantity-modal-two", "quantity-filter-all"}
    ][:10]
    return result


class CurrentInventoryRecovery:
    """Allow one ordinary reload per current-tier game, across its candidates.

    A 404 remains globally nonretryable. This exception is restricted to a
    future scheduled game, a successful document, and inventory HTTP 404.
    """

    def __init__(self, event_date: datetime, maximum_lead_hours: float, *, now=None, sleep=None):
        self.event_date = event_date
        self.maximum_lead_hours = maximum_lead_hours
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep or time.sleep
        self.used = False
        self.attempts: list[dict] = []
        self.recovered = False

    def _eligible(self) -> bool:
        event_date = self.event_date
        if event_date.tzinfo is None:
            event_date = event_date.replace(tzinfo=timezone.utc)
        return 0 < (event_date - self.now()).total_seconds() <= self.maximum_lead_hours * 3600

    def _attach(self, browser) -> dict:
        raw = getattr(browser, "capture_diagnostics", {})
        diagnostics = dict(raw) if isinstance(raw, dict) else {}
        if self.used:
            diagnostics = _attempt_diagnostics(diagnostics)
            diagnostics["inventory_recovery"] = {
                "cooldown_seconds": CURRENT_INVENTORY_COOLDOWN_SECONDS,
                "recovered": self.recovered,
                "attempts": list(self.attempts),
            }
        browser.capture_diagnostics = diagnostics
        return diagnostics

    def capture(self, browser, url: str):
        try:
            result = browser.capture(url)
        except Exception as first:
            raw = getattr(first, "diagnostics", {})
            evidence = _attempt_diagnostics(raw if isinstance(raw, dict) else {})
            statuses = [row["status"] for row in evidence["responses"]]
            if (self.used or not self._eligible()
                or getattr(first, "category", None) != "provider-inventory-not-found"
                or evidence.get("production_id") != urlsplit(url).path.rstrip("/").split("/")[-1]
                or evidence.get("document_status") != 200 or 404 not in statuses
                or any(status in (401, 403, 429) for status in statuses)):
                raise
            self.used = True
            self.attempts.append({"attempt": 1, "status": "failed", "category": first.category,
                                  "diagnostics": evidence})
            self.sleep(CURRENT_INVENTORY_COOLDOWN_SECONDS)
            if not self._eligible():
                diagnostics = self._attach(browser)
                raise VividCaptureError(first.category, diagnostics) from first
            try:
                result = browser.capture(url, reload_page=True)
            except Exception as second:
                raw = getattr(second, "diagnostics", {})
                self.attempts.append({"attempt": 2, "status": "failed",
                                      "category": getattr(second, "category", "provider-recovery-failed"),
                                      "diagnostics": _attempt_diagnostics(raw if isinstance(raw, dict) else {})})
                diagnostics = self._attach(browser)
                # An exhausted reload must not fall into a broad transport retry.
                raise VividCaptureError(getattr(second, "category", "provider-recovery-failed"), diagnostics) from second
            self.recovered = True
            self.attempts.append({"attempt": 2, "status": "captured",
                                  "diagnostics": _attempt_diagnostics(getattr(browser, "capture_diagnostics", {}) or {})})
        self._attach(browser)
        return result


class InventoryView:
    """Use ordinary quantity controls, then require the unfiltered response.

    No request is constructed here. If a control cannot be operated normally,
    capture fails rather than reading a quantity-specific subset as all prices.
    """

    def __init__(self):
        self.selected_quantity = False
        self.actions: list[str] = []
        self.modal_seen = False
        self.clear_seen = False

    @staticmethod
    def visible(elements):
        return [element for element in elements if element.is_displayed() and element.is_enabled()]

    def prepare(self, driver) -> None:
        dialogs = self.visible(driver.find_elements("css selector", '[role="dialog"], [aria-modal="true"], dialog[open]'))
        for dialog in dialogs:
            if "how many tickets" not in dialog.text.casefold():
                continue
            self.modal_seen = True
            controls = self.visible(dialog.find_elements("css selector", 'label, [role="checkbox"], input[type="checkbox"], button'))
            for wanted in ("any quantity", "2"):
                target = next((control for control in controls if (
                    control.text.strip().casefold() == wanted
                    or (control.get_attribute("aria-label") or "").strip().casefold() == wanted
                    or (control.get_attribute("value") or "").strip().casefold() == wanted
                )), None)
                if target is not None:
                    target.click()
                    self.selected_quantity = wanted == "2"
                    self.actions.append("quantity-modal-all" if wanted != "2" else "quantity-modal-two")
                    return
            return

        # The observed public UI has quantity options 1..8 and a separate Clear
        # control. Clear resets quantity to zero (Any Quantity) and recommendation
        # and section filters. Do not infer All from a quantity-specific payload.
        filtered_url = not unfiltered_request(driver.current_url)
        if not self.selected_quantity and not filtered_url:
            return
        clear = self.visible(driver.find_elements("css selector", '[data-testid="clear-filters-button"]'))
        self.clear_seen = self.clear_seen or bool(clear)
        target = clear[0] if clear else None
        if target is not None:
            target.click()
            self.selected_quantity = False
            self.actions.append("quantity-filter-all")
