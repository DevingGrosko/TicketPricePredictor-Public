# Section canonicalization implementation

This change implements the conservative findings from the 2026-09-03 production section-label audit.

## What is canonicalized

- case, punctuation, and whitespace variants
- `Sec` / `Sect` versus `Section`
- leading zeros in numeric tokens
- the audited `Granstand` typo
- concatenated numeric club labels such as `72CLUB`
- Fenway Park's historical `Pavilion Box N` to `Aura Pavilion Box N` rename

Canonical identities are scoped by sport and venue. Descriptor words remain part of the identity, so `Section 101`, `Club 101`, `Suite 101`, and `Upper 101` are not merged.

## Aggregation behavior

Raw provider labels remain unchanged in storage. Read-time insight rows are mapped to a canonical identity. If two aliases occur in the same capture, the lower price is used because the product reports the cheapest available ticket for that area. Across games, all aliases then contribute to one section history, ranking, and timeline.

## Public filtering

Parking products and audited access-only/no-admission labels are excluded from public ticket-area analysis. Standing-room inventory remains a distinct valid ticket area.

## Presentation

Venue cards now describe these counts as `tracked areas`, not physical stadium sections.
