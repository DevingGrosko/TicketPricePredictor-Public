"""Human-in-the-loop diagnostics for Ticketmaster and SeatGeek pages.

This tool does not conceal automation, defeat bot challenges, spoof browser
fingerprints, rotate proxies, or replay anti-bot tokens. It supports two
legitimate collection paths:

1. Parse a HAR file exported from a normal browser session.
2. Open a visible Selenium browser, let the user navigate normally, and inspect
   only JSON responses that the browser itself received.

The output is a sanitized diagnostic bundle. It is intentionally not written
into the production ticket databases until a provider-specific parser has been
reviewed against real samples.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
import time
from typing import Any, Iterable
from urllib.parse import urlparse


DEFAULT_OUTPUT_DIR = Path("manual_provider_captures")
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_SANITIZED_PAYLOADS = 20
MAX_STRING_LENGTH = 4_000
MAX_CONTAINER_ITEMS = 1_000
MAX_RECURSION_DEPTH = 24

PROVIDER_CHOICES = ("auto", "ticketmaster", "seatgeek")
TICKETMASTER_HOST_MARKERS = (
    "ticketmaster.",
    ".ticketmaster.",
    "tmol.io",
)
SEATGEEK_HOST_MARKERS = (
    "seatgeek.com",
    ".seatgeek.com",
)

TICKET_RESPONSE_TERMS = (
    "inventory",
    "listing",
    "listings",
    "offer",
    "offers",
    "price",
    "pricing",
    "quickpick",
    "quickpicks",
    "section",
    "sections",
    "seat",
    "seats",
    "availability",
)

SENSITIVE_KEY_PATTERN = re.compile(
    r"(authorization|cookie|session|csrf|xsrf|token|secret|password|"
    r"email|phone|billing|shipping|postal|zipcode|zip_code|"
    r"customer|account|profile|payment|cardholder)",
    flags=re.IGNORECASE,
)

SECTION_KEYS = frozenset(
    {
        "section",
        "sectionname",
        "sectionlabel",
        "sectiondescription",
        "sectiontitle",
        "zone",
        "zonename",
        "zonelabel",
        "area",
        "areaname",
        "arealabel",
        "level",
        "levelname",
        "block",
        "blockname",
    }
)
ROW_KEYS = frozenset({"row", "rowname", "rowlabel", "rowdescription"})
PRICE_KEYS = frozenset(
    {
        "price",
        "currentprice",
        "displayprice",
        "listprice",
        "totalprice",
        "allinprice",
        "allinpriceamount",
        "amount",
        "facevalue",
        "minprice",
        "minimumprice",
        "lowestprice",
        "ticketprice",
        "offerprice",
    }
)
QUANTITY_KEYS = frozenset(
    {
        "quantity",
        "availablequantity",
        "availablecount",
        "listingcount",
        "ticketcount",
        "remainingquantity",
        "inventorycount",
    }
)
CURRENCY_KEYS = frozenset({"currency", "currencycode", "currencyiso"})


@dataclass(frozen=True)
class ProviderResponse:
    url: str
    status: int | None
    mime_type: str
    body: str


@dataclass(frozen=True)
class CandidateRecord:
    provider: str
    response_url: str
    json_path: str
    section: str
    row: str
    price_raw: Any
    price_numeric: float | None
    quantity_raw: Any
    quantity_numeric: int | None
    currency: str
    source_keys: tuple[str, ...]


def normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def detect_provider(url: str) -> str | None:
    host = (urlparse(str(url)).hostname or "").casefold()
    if any(marker in host for marker in TICKETMASTER_HOST_MARKERS):
        return "ticketmaster"
    if any(marker in host for marker in SEATGEEK_HOST_MARKERS):
        return "seatgeek"
    return None


def validate_event_url(url: str, requested_provider: str = "auto") -> str:
    parsed = urlparse(str(url).strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("The event URL must be an HTTPS URL.")

    detected = detect_provider(url)
    if detected is None:
        raise ValueError("Only Ticketmaster and SeatGeek event pages are supported.")
    if requested_provider != "auto" and detected != requested_provider:
        raise ValueError(
            f"URL belongs to {detected}, not the requested provider "
            f"{requested_provider}."
        )
    return detected


def response_belongs_to_provider(url: str, provider: str) -> bool:
    host = (urlparse(str(url)).hostname or "").casefold()
    if provider == "ticketmaster":
        return any(marker in host for marker in TICKETMASTER_HOST_MARKERS)
    if provider == "seatgeek":
        return any(marker in host for marker in SEATGEEK_HOST_MARKERS)
    return False


def response_looks_ticket_related(url: str, body: str, mime_type: str) -> bool:
    haystack = f"{url} {mime_type} {body[:100_000]}".casefold()
    return any(term in haystack for term in TICKET_RESPONSE_TERMS)


def decode_response_text(text: str, encoding: str | None) -> str:
    if not text:
        return ""
    if str(encoding or "").casefold() == "base64":
        try:
            return base64.b64decode(text).decode("utf-8", errors="replace")
        except Exception:
            return ""
    return text


def sanitize_json(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_RECURSION_DEPTH:
        return "[max-depth]"

    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= MAX_CONTAINER_ITEMS:
                output["[truncated]"] = True
                break
            if SENSITIVE_KEY_PATTERN.search(str(key)):
                continue
            output[str(key)] = sanitize_json(child, depth=depth + 1)
        return output

    if isinstance(value, list):
        values = value[:MAX_CONTAINER_ITEMS]
        output = [sanitize_json(child, depth=depth + 1) for child in values]
        if len(value) > MAX_CONTAINER_ITEMS:
            output.append("[truncated]")
        return output

    if isinstance(value, str):
        return (
            value
            if len(value) <= MAX_STRING_LENGTH
            else value[:MAX_STRING_LENGTH] + "…[truncated]"
        )

    if value is None or isinstance(value, (bool, int, float)):
        return value

    return str(value)


def _scalar_from_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        preferred = (
            "display",
            "displayvalue",
            "label",
            "name",
            "value",
            "amount",
            "formatted",
            "description",
            "code",
        )
        normalized = {normalized_key(key): child for key, child in value.items()}
        for key in preferred:
            if key in normalized:
                result = _scalar_from_value(normalized[key])
                if result not in (None, ""):
                    return result
    return None


def _flatten_candidate_values(
    value: dict[str, Any],
    *,
    depth: int = 0,
    output: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if output is None:
        output = {}
    if depth > 3:
        return output

    for key, child in value.items():
        normalized = normalized_key(key)
        scalar = _scalar_from_value(child)
        if scalar not in (None, "") and normalized not in output:
            output[normalized] = scalar
        if isinstance(child, dict):
            _flatten_candidate_values(child, depth=depth + 1, output=output)
    return output


def _first_value(mapping: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _text_value(value: Any) -> str:
    scalar = _scalar_from_value(value)
    if scalar is None:
        return ""
    return " ".join(str(scalar).split())[:500]


def _numeric_value(value: Any) -> float | None:
    scalar = _scalar_from_value(value)
    if scalar is None or isinstance(scalar, bool):
        return None
    if isinstance(scalar, (int, float)):
        return float(scalar)

    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(scalar))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _integer_value(value: Any) -> int | None:
    numeric = _numeric_value(value)
    if numeric is None or numeric < 0:
        return None
    return int(numeric)


def candidate_from_dict(
    value: dict[str, Any],
    *,
    provider: str,
    response_url: str,
    path: str,
) -> CandidateRecord | None:
    flattened = _flatten_candidate_values(value)
    section_raw = _first_value(flattened, SECTION_KEYS)
    price_raw = _first_value(flattened, PRICE_KEYS)
    if section_raw in (None, "") or price_raw in (None, ""):
        return None

    section = _text_value(section_raw)
    if not section:
        return None

    row_raw = _first_value(flattened, ROW_KEYS)
    quantity_raw = _first_value(flattened, QUANTITY_KEYS)
    currency_raw = _first_value(flattened, CURRENCY_KEYS)
    return CandidateRecord(
        provider=provider,
        response_url=response_url,
        json_path=path,
        section=section,
        row=_text_value(row_raw),
        price_raw=price_raw,
        price_numeric=_numeric_value(price_raw),
        quantity_raw=quantity_raw,
        quantity_numeric=_integer_value(quantity_raw),
        currency=_text_value(currency_raw).upper(),
        source_keys=tuple(sorted(str(key) for key in value.keys())[:50]),
    )


def extract_candidate_records(
    value: Any,
    *,
    provider: str,
    response_url: str,
) -> list[CandidateRecord]:
    records: list[CandidateRecord] = []
    seen: set[tuple[Any, ...]] = set()

    def walk(child: Any, path: str, depth: int) -> None:
        if depth > MAX_RECURSION_DEPTH:
            return

        if isinstance(child, dict):
            candidate = candidate_from_dict(
                child,
                provider=provider,
                response_url=response_url,
                path=path,
            )
            if candidate is not None:
                identity = (
                    candidate.section.casefold(),
                    candidate.row.casefold(),
                    candidate.price_numeric,
                    str(candidate.price_raw),
                    candidate.quantity_numeric,
                    candidate.currency,
                    candidate.response_url,
                )
                if identity not in seen:
                    seen.add(identity)
                    records.append(candidate)

            for key, nested in list(child.items())[:MAX_CONTAINER_ITEMS]:
                walk(nested, f"{path}.{key}", depth + 1)
            return

        if isinstance(child, list):
            for index, nested in enumerate(child[:MAX_CONTAINER_ITEMS]):
                walk(nested, f"{path}[{index}]", depth + 1)

    walk(value, "$", 0)
    return records


def parse_json_body(body: str) -> Any | None:
    stripped = str(body or "").lstrip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def analyze_responses(
    responses: Iterable[ProviderResponse],
    *,
    provider: str,
    event_url: str,
    capture_mode: str,
    include_sanitized_payloads: bool,
) -> dict[str, Any]:
    response_summaries: list[dict[str, Any]] = []
    candidate_records: list[CandidateRecord] = []
    sanitized_payloads: list[dict[str, Any]] = []
    examined = 0
    parsed_json = 0
    skipped_oversized = 0

    for response in responses:
        if not response_belongs_to_provider(response.url, provider):
            continue
        examined += 1
        body_bytes = len(response.body.encode("utf-8", errors="ignore"))
        if body_bytes > MAX_RESPONSE_BYTES:
            skipped_oversized += 1
            continue
        if not response_looks_ticket_related(
            response.url,
            response.body,
            response.mime_type,
        ):
            continue

        payload = parse_json_body(response.body)
        if payload is None:
            continue
        parsed_json += 1
        records = extract_candidate_records(
            payload,
            provider=provider,
            response_url=response.url,
        )
        candidate_records.extend(records)
        summary = {
            "url": response.url,
            "status": response.status,
            "mime_type": response.mime_type,
            "body_bytes": body_bytes,
            "candidate_count": len(records),
            "top_level_keys": (
                sorted(str(key) for key in payload.keys())[:50]
                if isinstance(payload, dict)
                else []
            ),
        }
        response_summaries.append(summary)

        if (
            include_sanitized_payloads
            and len(sanitized_payloads) < MAX_SANITIZED_PAYLOADS
            and (records or "inventory" in response.url.casefold())
        ):
            sanitized_payloads.append(
                {
                    "url": response.url,
                    "status": response.status,
                    "mime_type": response.mime_type,
                    "payload": sanitize_json(payload),
                }
            )

    deduplicated: list[CandidateRecord] = []
    seen_candidates: set[tuple[Any, ...]] = set()
    for record in candidate_records:
        identity = (
            record.section.casefold(),
            record.row.casefold(),
            record.price_numeric,
            str(record.price_raw),
            record.quantity_numeric,
            record.currency,
            record.response_url,
        )
        if identity in seen_candidates:
            continue
        seen_candidates.add(identity)
        deduplicated.append(record)

    return {
        "schema_version": 1,
        "capture_mode": capture_mode,
        "provider": provider,
        "event_url": event_url,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "responses_examined": examined,
        "json_responses_parsed": parsed_json,
        "oversized_responses_skipped": skipped_oversized,
        "candidate_count": len(deduplicated),
        "candidates": [asdict(record) for record in deduplicated],
        "response_summaries": response_summaries,
        "sanitized_payloads": sanitized_payloads,
        "notes": [
            "This bundle contains diagnostics from a user-accessible browser session.",
            "No cookies, request headers, browser fingerprints, or anti-bot tokens are exported.",
            "Candidate rows are heuristic until a provider-specific parser is validated.",
        ],
    }


def load_har_responses(path: Path, provider: str) -> list[ProviderResponse]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("log", {}).get("entries", [])
    if not isinstance(entries, list):
        raise ValueError("HAR file does not contain log.entries.")

    responses: list[ProviderResponse] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        request = entry.get("request") or {}
        response = entry.get("response") or {}
        content = response.get("content") or {}
        url = str(request.get("url") or "")
        if not response_belongs_to_provider(url, provider):
            continue
        body = decode_response_text(
            str(content.get("text") or ""),
            str(content.get("encoding") or ""),
        )
        if not body:
            continue
        status_raw = response.get("status")
        status = int(status_raw) if isinstance(status_raw, (int, float)) else None
        responses.append(
            ProviderResponse(
                url=url,
                status=status,
                mime_type=str(content.get("mimeType") or ""),
                body=body,
            )
        )
    return responses


def capture_visible_browser_responses(
    event_url: str,
    *,
    provider: str,
    timeout: int,
) -> list[ProviderResponse]:
    try:
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Selenium is not installed. Run: pip install -r requirements.txt"
        ) from exc

    options = webdriver.ChromeOptions()
    options.add_argument("--window-size=1450,1000")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    # Deliberately do not alter navigator.webdriver, the User-Agent, browser
    # fingerprints, proxy routing, TLS behavior, or challenge responses.
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(timeout)
    driver.execute_cdp_cmd("Network.enable", {})

    request_metadata: dict[str, tuple[str, int | None, str]] = {}
    captured: dict[str, ProviderResponse] = {}
    done = threading.Event()

    def wait_for_user() -> None:
        input(
            "\nUse the visible browser normally. Open the ticket map, scroll through "
            "available sections, and complete only ordinary user interactions. "
            "Press Enter here when the inventory has loaded.\n"
        )
        done.set()

    def drain_logs() -> None:
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
                if not response_belongs_to_provider(url, provider):
                    continue
                request_id = str(params.get("requestId") or "")
                status_raw = response.get("status")
                status = (
                    int(status_raw)
                    if isinstance(status_raw, (int, float))
                    else None
                )
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
            if metadata is None:
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

    try:
        driver.get_log("performance")
        try:
            driver.get(event_url)
        except TimeoutException:
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass

        thread = threading.Thread(target=wait_for_user, daemon=True)
        thread.start()
        while not done.wait(0.5):
            drain_logs()
        drain_logs()
        return list(captured.values())
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def default_output_path(provider: str, mode: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_OUTPUT_DIR / f"{provider}-{mode}-{stamp}.json"


def write_report(report: dict[str, Any], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    har = subparsers.add_parser(
        "har",
        help="Analyze a HAR exported from a normal Ticketmaster or SeatGeek tab.",
    )
    har.add_argument("har_file", type=Path)
    har.add_argument("--event-url", required=True)
    har.add_argument("--provider", choices=PROVIDER_CHOICES, default="auto")
    har.add_argument("--output", type=Path)
    har.add_argument("--include-sanitized-payloads", action="store_true")

    browser = subparsers.add_parser(
        "browser",
        help="Open a visible, non-stealth browser and capture received JSON.",
    )
    browser.add_argument("event_url")
    browser.add_argument("--provider", choices=PROVIDER_CHOICES, default="auto")
    browser.add_argument("--timeout", type=int, default=60)
    browser.add_argument("--output", type=Path)
    browser.add_argument("--include-sanitized-payloads", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    provider = validate_event_url(args.event_url, args.provider)

    if args.command == "har":
        if not args.har_file.exists():
            raise FileNotFoundError(args.har_file)
        responses = load_har_responses(args.har_file, provider)
        report = analyze_responses(
            responses,
            provider=provider,
            event_url=args.event_url,
            capture_mode="har",
            include_sanitized_payloads=args.include_sanitized_payloads,
        )
        report["source_har"] = str(args.har_file)
        output = args.output or default_output_path(provider, "har")
    else:
        responses = capture_visible_browser_responses(
            args.event_url,
            provider=provider,
            timeout=args.timeout,
        )
        report = analyze_responses(
            responses,
            provider=provider,
            event_url=args.event_url,
            capture_mode="visible-browser",
            include_sanitized_payloads=args.include_sanitized_payloads,
        )
        output = args.output or default_output_path(provider, "browser")

    written = write_report(report, output)
    print(
        f"Wrote {report['candidate_count']} candidate section-price records "
        f"from {report['json_responses_parsed']} JSON responses to {written}."
    )
    if report["candidate_count"] == 0:
        print(
            "No section-level records were recognized. Re-export the HAR with "
            "response content included after opening the seat map, or rerun with "
            "--include-sanitized-payloads so a provider parser can be developed.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
