"""Temporary standard-browser probe for major ticket marketplaces.

This reuses the existing sanitized provider probe but extends domain handling to
StubHub, TickPick, and Gametime. It does not use stealth, proxy rotation,
CAPTCHA solving, imported sessions, or any access-control bypass.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import urlparse

import hosted_provider_probe as hosted
import manual_provider_capture as manual


PROVIDER_HOST_MARKERS = {
    "ticketmaster": ("ticketmaster.", ".ticketmaster.", "tmol.io"),
    "seatgeek": ("seatgeek.com", ".seatgeek.com"),
    "stubhub": ("stubhub.com", ".stubhub.com", "stubhub.net", ".stubhub.net", "vggcdn.net"),
    "tickpick": ("tickpick.com", ".tickpick.com"),
    "gametime": ("gametime.co", ".gametime.co"),
}
PROVIDERS = ("auto", *PROVIDER_HOST_MARKERS)


def detect_provider(url: str) -> str | None:
    host = (urlparse(str(url)).hostname or "").casefold()
    for provider, markers in PROVIDER_HOST_MARKERS.items():
        if any(marker in host for marker in markers):
            return provider
    return None


def validate_event_url(url: str, requested_provider: str = "auto") -> str:
    parsed = urlparse(str(url).strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("The event URL must be an HTTPS URL.")
    detected = detect_provider(url)
    if detected is None:
        raise ValueError("Unsupported ticket marketplace URL.")
    if requested_provider != "auto" and detected != requested_provider:
        raise ValueError(
            f"URL belongs to {detected}, not requested provider {requested_provider}."
        )
    return detected


def response_belongs_to_provider(url: str, provider: str) -> bool:
    host = (urlparse(str(url)).hostname or "").casefold()
    markers = PROVIDER_HOST_MARKERS.get(provider, ())
    return any(marker in host for marker in markers)


def analyze_responses(*args, **kwargs):
    original = manual.response_belongs_to_provider
    manual.response_belongs_to_provider = response_belongs_to_provider
    try:
        return manual.analyze_responses(*args, **kwargs)
    finally:
        manual.response_belongs_to_provider = original


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event_url")
    parser.add_argument("--provider", choices=PROVIDERS, default="auto")
    parser.add_argument("--duration", type=int, default=55)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path, required=True)
    parser.add_argument("--include-sanitized-payloads", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    provider = validate_event_url(args.event_url, args.provider)

    hosted.validate_event_url = validate_event_url
    hosted.response_belongs_to_provider = response_belongs_to_provider
    hosted.analyze_responses = analyze_responses

    report, exit_code = hosted.run_probe(
        args.event_url,
        provider=provider,
        duration=args.duration,
        output=args.output,
        screenshot=args.screenshot,
        include_sanitized_payloads=args.include_sanitized_payloads,
    )
    print(
        f"provider={provider} outcome={report['outcome']} "
        f"candidates={report['candidate_count']} "
        f"json={report['json_responses_parsed']}"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
