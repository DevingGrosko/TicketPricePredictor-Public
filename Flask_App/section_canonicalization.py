"""Conservative, venue-scoped identities for ticket-area counts.

Collectors retain provider labels exactly as received. This module folds only
low-risk formatting and audited naming variants when one public metric needs to
count the same area consistently across games.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Any
from Flask_App.report_policy import report_venue


@dataclass(frozen=True)
class SectionIdentity:
    key: str
    raw_label: str


_EXACT_EXCLUDED_KEYS = frozenset(
    {
        "gp atrium no admission",
        "optum field lounge no admission",
        "northwest sideline pass",
        "401 w washington st government center",
        "lexus club pass no admission",
    }
)

_TOKEN_REPLACEMENTS = {
    "sec": "section",
    "sect": "section",
    "granstand": "grandstand",
    "firld": "field",
}

_PARKING_RE = re.compile(r"\bparking\b|^(?:lot|garage)\b|\bpark and ride\b")
_NO_ADMISSION_RE = re.compile(r"\b(?:no|without) admission\b")
_ACCESS_PASS_RE = re.compile(
    r"\b(?:sideline|club|lounge|atrium|field access|player tunnel)\s+pass\b"
)
_STREET_LOCATION_RE = re.compile(
    r"^\d+\s+.+\b(?:st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|way)\b"
)


def clean_section_label(value: Any) -> str:
    """Normalize Unicode and whitespace while preserving provider wording."""

    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def _words(value: Any) -> list[str]:
    cleaned = clean_section_label(value).casefold().replace("&", " and ")
    return re.findall(r"[a-z0-9]+", cleaned)


def _normalized_phrase(value: Any) -> str:
    return " ".join(_words(value))


def is_excluded_ticket_area(value: Any) -> bool:
    """Return True for parking and audited non-admission/access products."""

    phrase = _normalized_phrase(value)
    return bool(
        not phrase
        or _PARKING_RE.search(phrase)
        or phrase in _EXACT_EXCLUDED_KEYS
        or _NO_ADMISSION_RE.search(phrase)
        or _ACCESS_PASS_RE.search(phrase)
        or _STREET_LOCATION_RE.search(phrase)
    )


def canonical_section_key(sport: Any, venue: Any, value: Any) -> str | None:
    """Build a conservative identity scoped to one sport and venue.

    Descriptor words remain part of the key, so ``Section 101``, ``Club 101``,
    and ``Suite 101`` remain separate ticket products.
    """

    cleaned = clean_section_label(value)
    if not cleaned or is_excluded_ticket_area(cleaned):
        return None

    tokens = [_TOKEN_REPLACEMENTS.get(token, token) for token in _words(cleaned)]
    expanded: list[str] = []
    for token in tokens:
        numbered_club = re.fullmatch(r"(\d+)club", token)
        if numbered_club:
            expanded.extend((str(int(numbered_club.group(1))), "club"))
            continue

        club_number = re.fullmatch(r"club(\d+)", token)
        if club_number:
            expanded.extend(("club", str(int(club_number.group(1)))))
            continue

        expanded.append(str(int(token)) if token.isdigit() else token)

    venue_phrase = _normalized_phrase(report_venue(venue))
    if (
        venue_phrase == "fenway park"
        and len(expanded) == 3
        and expanded[:2] == ["pavilion", "box"]
        and expanded[2].isdigit()
    ):
        expanded.insert(0, "aura")

    return "|".join(
        (
            _normalized_phrase(sport) or "unknown",
            venue_phrase or "unknown",
            " ".join(expanded),
        )
    )


def section_identity(sport: Any, venue: Any, value: Any) -> SectionIdentity | None:
    cleaned = clean_section_label(value)
    key = canonical_section_key(sport, venue, cleaned)
    if key is None:
        return None
    return SectionIdentity(key=key, raw_label=cleaned)
