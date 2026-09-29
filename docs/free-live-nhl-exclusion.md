# One-game NHL collection exclusion

## Scope and authorization

The user approved omitting a one-off problematic game rather than weakening the
normal NHL matcher. Only schedule ID `2026020182`, Montreal Canadiens at
Winnipeg Jets at Princess Auto Stadium, is excluded from the free collector.
Other games involving either team, other outdoor games, and future seasons
remain subject to the existing collection and validation rules.

This change does not delete database history or valid saved capture payloads.
It clears only this game's unresolved-work entry. Health reports list it under
`excluded_games` with `policy: user-approved-single-game`, separately from
captured, committed, failed and unresolved work. Ordinary unresolved games
still remain retryable and make the collector report degraded coverage.

## Root cause observed on September 29, 2026

Read-only GitHub Actions audit:
https://github.com/DevingGrosko/TicketPricePredictor-Public/actions/runs/36588747770
Audit job: 109475963167; source ref: 6be41c6a82ba8161784cb005f1974d82e5498f7d.

At 15:15 UTC, the official NHL schedule and game-center response identified:
- ID: 2026020182; season: 20262027; regular season.
- Montreal Canadiens (away) at Winnipeg Jets (home).
- Start: 2026-10-25T23:00:00Z, at Princess Auto Stadium, Winnipeg.
- The schedule marks the game as a neutral-site event.

Official event page:
https://www.nhl.com/events/nhl-heritage-classic/
Official game response:
https://api-web.nhle.com/v1/gamecenter/2026020182/landing

Vivid's live search returned a real ticket listing for the same event:
https://www.vividseats.com/nhl-heritage-classic-tickets-princess-auto-stadium-10-2-2026--sports-nhl-hockey/production/6393129

Its displayed date was October 25 and its title was
`NHL Heritage Classic - Winnipeg Jets vs Montreal Canadiens`.
The parsed team order was therefore Winnipeg, Montreal, while the official
away/home order is Montreal, Winnipeg. `candidates_for_schedule_game` requires
ordered equality, so it rejected this candidate before any price capture.

This was NOT a missing listing, a cron authentication problem, a database
failure, or an insufficient-inventory rejection. The search completed without
errors. The ordinary matcher deliberately protects against mixing home/away
fixtures; it was not adapted to this special-event title format.

The listing's URL also contains a misleading date slug (`10-2-2026`). In the
observed search parse, the visible date correctly became October 25, so that
slug was not the immediate rejection cause. The nearby October 24 alumni game
and November 3 reverse fixture were also returned and correctly rejected for
this particular scheduled game.

## Implementation and verification

`tools/free_live_nhl_exclusions.py` checks the exact ID, teams and venue.
`tools/free_live_hardening.run_nhl` applies it before selecting work, including
an entry restored from the old retry cache. No global failure suppression is
introduced; no original PythonAnywhere entry point or schedule is edited.

Eight focused offline tests verify the observed title mismatch, exact-event
scope, restored-backlog cleanup, live-identity precedence, explicit reporting,
and preservation of other unresolved work and pending capture files.

Deployment and a real post-change collection must be verified separately from
these offline tests. An exclusion is an intentional coverage omission, not a
successful capture of this game's prices.
