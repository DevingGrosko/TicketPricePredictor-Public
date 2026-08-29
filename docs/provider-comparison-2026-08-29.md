# Ticket marketplace scrape comparison — 2026-08-29

This document records one controlled diagnostic comparison from GitHub-hosted Ubuntu runners. Each provider was tested with ordinary headless Chrome and Selenium. The tests did not use stealth patches, modified fingerprints or user agents, proxies, imported sessions, CAPTCHA solving, or challenge-token replay. Nothing was written to the production databases.

## Access results

| Provider | Main-page result | Inventory result |
| --- | --- | --- |
| Ticketmaster | Blocked with HTTP 403/429 and “Your Browsing Activity Has Been Paused” | No provider inventory JSON and no section-price records |
| StubHub | Blocked; document responses included HTTP 403 and the challenge page title was “Just a moment...” | No provider inventory JSON and no section-price records |
| SeatGeek | Blocked; document responses included HTTP 403 | No provider inventory JSON and no section-price records |
| TickPick | Blocked; document responses included HTTP 403 and the challenge page title was “Just a moment...” | No provider inventory JSON and no section-price records |
| Gametime | Exact event page loaded normally with HTTP 200 responses and no block markers | Four JSON responses were parsed; the listing response contained 231 section/row listings |
| Vivid Seats | Exact event page loaded normally | Existing NFL parser returned 110 sections |

These are single-event diagnostics. Provider behavior can vary by URL, time, runner IP, and future site changes.

## Gametime exact-event finding

The exact Pittsburgh Steelers at New England Patriots event loaded successfully from a standard GitHub-hosted runner. The browser received:

```text
GET https://mobile.gametime.co/v3/listings/695d912f2f00ace2743f5521?all_in_pricing=true&quantity=2&jitter_cheapest=0
HTTP 200
```

The response contained 231 listings and directly exposed:

- section and section group
- row
- seat labels
- available ticket lots
- pre-fee price
- all-in total price
- listing ID and event ID

The response-level available filter reported a minimum all-in price of `33800` cents for quantity two. A provider-specific parser is required because the generic diagnostic parser selected a nested zero-valued savings field instead of `price.total`.

## Vivid exact-event finding

For the same Pittsburgh Steelers at New England Patriots event, the existing Vivid NFL parser returned:

```text
110 sections
minimum stored section price: $234
maximum stored section price: $1,319
```

The cheapest stored row was:

```text
section: Upper Level 325
row: 24
quantity: 1
p: 233.70
aip: 325.00
```

The current collector stores field `p` and retains `aip` only as an alternate diagnostic value. On this sample, `p` was the lower pre-fee-style value while `aip` was the all-in-style value. Because Vivid now presents all-in pricing to shoppers, the current historical series should not be described as the buyer’s final all-in price until the field choice is corrected and validated across additional events.

The Vivid parser also intentionally excludes obstructed-view and standing-room-only listings. Its minimum therefore represents the cheapest normal section found, not necessarily the absolute cheapest listing of any type.

## Practical ranking for GitHub-hosted automation

1. **Vivid Seats:** production-ready access, but the stored price field should be corrected from `p` to the validated all-in field.
2. **Gametime:** strongest second-source candidate. Ordinary GitHub-hosted Chrome reached a rich all-in, quantity-aware, section/row listing response without a challenge.
3. **Ticketmaster, StubHub, SeatGeek, TickPick:** not viable through the tested ordinary GitHub-hosted browser path because inventory was blocked before loading.

No experimental comparison workflow or parser in this research branch has been merged into production.
