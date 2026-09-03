"""Conservative, venue-scoped section-label canonicalization.

The collectors intentionally retain provider labels exactly as received. This
module supplies a read-time identity for cross-game analysis so harmless case,
punctuation, spacing, leading-zero, and audited naming variants share one
history without merging semantically different ticket products.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import re
import unicodedata
from typing import Any, Iterable


@dataclass(frozen=True)
class SectionIdentity:
    """Canonical identity plus the cleaned provider label used to build it."""

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

_ACRONYMS = {
    "ada": "ADA",
    "audi": "Audi",
    "bmw": "BMW",
    "citi": "Citi",
    "delta": "Delta",
    "gp": "GP",
    "mlb": "MLB",
    "nfl": "NFL",
    "nhl": "NHL",
    "ny": "NY",
    "sro": "SRO",
    "td": "TD",
    "vip": "VIP",
}

_TOKEN_REPLACEMENTS = {
    "sec": "section",
    "sect": "section",
    "granstand": "grandstand",
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
    """Normalize Unicode and whitespace without discarding the raw wording."""

    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def _words(value: Any) -> list[str]:
    cleaned = clean_section_label(value).casefold()
    cleaned = cleaned.replace("&", " and ")
    return re.findall(r"[a-z0-9]+", cleaned)


def _normalized_phrase(value: Any) -> str:
    return " ".join(_words(value))


def _venue_key(value: Any) -> str:
    return _normalized_phrase(value)


def is_excluded_ticket_area(value: Any) -> bool:
    """Return True for parking and audited non-admission/access products."""

    phrase = _normalized_phrase(value)
    if not phrase:
        return True
    if _PARKING_RE.search(phrase):
        return True
    if phrase in _EXACT_EXCLUDED_KEYS:
        return True
    if _NO_ADMISSION_RE.search(phrase):
        return True
    if _ACCESS_PASS_RE.search(phrase):
        return True
    if _STREET_LOCATION_RE.search(phrase):
        return True
    return False


def canonical_section_key(sport: Any, venue: Any, value: Any) -> str | None:
    """Build a conservative identity key scoped to one sport and venue.

    Descriptor words remain part of the key. Consequently ``Club 101``,
    ``Suite 101``, and ``Section 101`` remain separate even though they share a
    number. Only low-risk formatting and audited naming variants are folded.
    """

    cleaned = clean_section_label(value)
    if not cleaned or is_excluded_ticket_area(cleaned):
        return None

    tokens = [_TOKEN_REPLACEMENTS.get(token, token) for token in _words(cleaned)]
    if not tokens:
        return None

    expanded: list[str] = []
    for token in tokens:
        match = re.fullmatch(r"(\d+)club", token)
        if match:
            expanded.extend((str(int(match.group(1))), "club"))
            continue
        match = re.fullmatch(r"club(\d+)", token)
        if match:
            expanded.extend(("club", str(int(match.group(1)))))
            continue
        expanded.append(token)
    tokens = expanded

    tokens = [str(int(token)) if token.isdigit() else token for token in tokens]

    venue_phrase = _venue_key(venue)
    if (
        venue_phrase == "fenway park"
        and len(tokens) == 3
        and tokens[0:2] == ["pavilion", "box"]
        and tokens[2].isdigit()
    ):
        tokens.insert(0, "aura")

    return "|".join(
        (
            _normalized_phrase(sport) or "unknown",
            venue_phrase or "unknown",
            " ".join(tokens),
        )
    )


def section_identity(sport: Any, venue: Any, value: Any) -> SectionIdentity | None:
    cleaned = clean_section_label(value)
    key = canonical_section_key(sport, venue, cleaned)
    if key is None:
        return None
    return SectionIdentity(key=key, raw_label=cleaned)


def _semantic_tokens(key: str) -> list[str]:
    return key.rsplit("|", 1)[-1].split()


def _display_score(label: str, key: str) -> tuple[int, int, int, int, str]:
    """Prefer readable/canonical provider spellings inside one alias group."""

    phrase = _normalized_phrase(label)
    semantic = " ".join(_semantic_tokens(key))
    mixed_case = int(not (label.isupper() or label.islower()))
    correct_grandstand = int("grandstand" in phrase and "granstand" not in phrase)
    fenway_aura = int(
        semantic.startswith("aura pavilion box ") and phrase.startswith("aura ")
    )
    spaced_club = int(
        bool(re.search(r"\b\d+\s+club\b|\bclub\s+\d+\b", phrase))
    )
    return (
        fenway_aura,
        correct_grandstand,
        spaced_club,
        mixed_case,
        f"{-len(label):06d}:{label.casefold()}",
    )


def _title_token(token: str) -> str:
    if token in _ACRONYMS:
        return _ACRONYMS[token]
    if token.isdigit():
        return token
    return token.capitalize()


def preferred_section_label(key: str, raw_labels: Iterable[Any]) -> str:
    """Choose one stable display label for a canonical identity."""

    labels = sorted(
        {
            clean_section_label(value)
            for value in raw_labels
            if clean_section_label(value)
        },
        key=str.casefold,
    )
    semantic_tokens = _semantic_tokens(key)
    semantic = " ".join(semantic_tokens)

    if semantic_tokens and all(token.isdigit() for token in semantic_tokens):
        return " ".join(semantic_tokens)
    if semantic.startswith("aura pavilion box "):
        return " ".join(_title_token(token) for token in semantic_tokens)
    if semantic.startswith("section ") and all(
        token.isdigit() for token in semantic_tokens[1:]
    ):
        return " ".join(_title_token(token) for token in semantic_tokens)
    if "grandstand" in semantic and any(
        "granstand" in _normalized_phrase(label) for label in labels
    ):
        return " ".join(_title_token(token) for token in semantic_tokens)
    if re.fullmatch(r"(?:\d+ club|club \d+)", semantic):
        return " ".join(_title_token(token) for token in semantic_tokens)

    if not labels:
        return " ".join(_title_token(token) for token in semantic_tokens)
    chosen = max(labels, key=lambda label: _display_score(label, key))
    if chosen.isupper() or chosen.islower():
        parts = re.split(r"(\s+)", chosen.casefold())
        return "".join(
            part if part.isspace() else _ACRONYMS.get(part, part.capitalize())
            for part in parts
        )
    return chosen


def canonical_section_label(sport: Any, venue: Any, value: Any) -> str | None:
    """Return a public display label for one raw provider section value."""

    identity = section_identity(sport, venue, value)
    if identity is None:
        return None
    return preferred_section_label(identity.key, [identity.raw_label])


def canonicalize_section_labels(
    sport: Any,
    venue: Any,
    values: Iterable[Any],
) -> list[str]:
    """Return unique public labels after conservative venue-scoped folding."""

    groups: dict[str, list[str]] = defaultdict(list)
    for value in values or []:
        identity = section_identity(sport, venue, value)
        if identity is not None:
            groups[identity.key].append(identity.raw_label)
    return sorted(
        (
            preferred_section_label(key, labels)
            for key, labels in groups.items()
        ),
        key=str.casefold,
    )


def canonical_label_lookup(
    sport: Any,
    venue: Any,
    values: Iterable[Any],
) -> dict[str, str]:
    """Map every canonical key represented by values to its display label."""

    groups: dict[str, list[str]] = defaultdict(list)
    for value in values or []:
        identity = section_identity(sport, venue, value)
        if identity is not None:
            groups[identity.key].append(identity.raw_label)
    return {
        key: preferred_section_label(key, labels)
        for key, labels in groups.items()
    }
