# TicketSignal report-quality audit — 6 September 2026

## Scope

Read-only snapshot of all public MLB, NFL and NHL team-card directories and their options endpoints, captured at approximately 18:39 UTC. The section statistics below describe **event section inventories**, not the number of priced observations in each time window. Inventories can include labels seen at any point; they do not establish that a section has a usable price at every capture. The deployed ranking audit separately checks priced materialized summaries.

## Findings

NHL rendered 35 venue cards for 32 teams. Washington Capitals appeared at Capital One Arena and GIANT Center; Los Angeles Kings at Crypto.com Arena and Toyota Arena; Carolina Hurricanes at Lenovo Center and First Horizon Coliseum. These are separate venue labels for different buildings, not aliases to collapse into a single seating map. NFL rendered 30 venue cards for 32 teams, combining Giants/Jets and Rams/Chargers. MLB had 11 cards and no currently duplicated team cards.

NHL options included 64 preseason games among 106 listed games. Preseason must not enter the normal comparison cohort. Existing rows remain stored; exclusions apply to public selection and statistics.

Across the 11 MLB inventories, 2,112 raw venue-scoped labels became 2,088 groups under the previous conservative normalization, removing 24 spelling/format variants. Of those groups, 205 occurred in only one game (9.8%). **That 9.8% is a sparsity rate, not a duplicate probability.**

At Citi Field, all 120 one-game groups were attached to a single Mariners at Mets event dated August 16, 2025 (stored event ID 6), alongside 22 games from 2026. Many unusual label families coexist in that legacy event. Neither identical section numbers nor non-overlapping collection dates prove that two labels describe the same ticket product. The fix isolates the latest season for team recommendations; it does not rewrite or delete the 2025 event.

## MLB inventory coverage

| Venue | Games, all years | Raw labels | Previous normalized groups | One-game groups | Latest-season games | Latest-season one-game groups |
|---|---:|---:|---:|---:|---:|---:|
| Busch Stadium | 14 | 203 | 203 | 9 | 14 | 9 |
| Citi Field | 23 | 287 | 287 | 120 | 22 | 1 |
| Citizens Bank Park | 27 | 149 | 149 | 5 | 24 | 2 |
| Fenway Park | 27 | 395 | 378 | 42 | 23 | 35 |
| George M. Steinbrenner Field | 2 | 37 | 37 | 1 | 2 | 1 |
| Nationals Park | 27 | 132 | 129 | 2 | 22 | 2 |
| Oriole Park at Camden Yards | 24 | 175 | 175 | 9 | 21 | 10 |
| Truist Park | 28 | 165 | 165 | 3 | 24 | 5 |
| Uniqlo Field at Dodger Stadium | 23 | 272 | 271 | 8 | 23 | 8 |
| Wrigley Field | 19 | 160 | 160 | 2 | 19 | 2 |
| Yankee Stadium | 25 | 137 | 134 | 4 | 22 | 4 |

## What can safely be combined

The existing capitalization, punctuation, audited Fenway Pavilion naming, and typo rules remain. This audit adds the obvious `FIRLD BOX 17` / `Field Box 17` spelling correction. Confirmed aliases of the same physical venue share a canonical venue scope. Different physical venues do not.

The accompanying JSON records 282 same-number label clusters needing review. These are **candidate-review groups, not confirmed duplicates**. Field Box, Dugout Box, Grandstand, Club, Suite and a bare number must not be combined just because their numbers match. Some coexist within a single event. Ambiguous bare numeric labels are not eligible to win a ranking, but remain inspectable.

A defensible expansion of merging would require provider section identifiers or seating-map geometry and ticket-product metadata, ideally checked against simultaneous price observations. Similar prices or labels alone are insufficient.

## Implemented comparison policy

One card per normalized home team. Reports, section detail links and cache keys retain the home team as well as the physical venue. Giants and Jets, and Rams and Chargers, therefore have separate price histories without pretending they use different buildings. Different venues for the same team are selected separately. Team recommendations use the latest tracked season; prior game records remain in the single-game history unless preseason.

Cheapest sections use the final 24 hours only: each included game must have observed prices in 12–24h, 6–12h and 0–6h windows. Each window uses its median; the three windows receive duration weights 12:6:6. Per-game results then receive equal weight. A section needs at least three completed games and coverage of at least 60% of all completed games in the team/venue/season cohort. Upcoming games do not enter this historical ranking. Missing windows are not interpolated.

Largest typical drops use a fixed final-48h window for MLB and final-seven-day window for NFL/NHL. All five constituent windows must have at least three observations. The calculation finds each game's largest earlier-high to later-low percentage decline from bucket medians, then takes the median across qualifying games. It uses the same three-game / 60% eligibility rule. Incomplete games and one-snapshot spikes cannot supply a complete comparison path. This is not a guarantee against every data error.

If fewer than five sections qualify, the report shows fewer than five. If none qualify, it explicitly says there is not enough comparable history. The wider exploratory chart and first-to-last metric remain on the section detail page, alongside the fixed-window comparison metrics and sample counts.

## Limitations and collection priorities

Three games and 60% coverage are explicit product safeguards, not a validated confidence interval or proof of representativeness. Sections can still be observed for different subsets of games; opponent, weekday, demand, resale inventory and missingness can affect comparisons. No correction can reconstruct prices never captured. These are observed listing-price summaries, not realized sale prices or guaranteed savings.

Priority collection improvements are reliable home-team and season-type metadata, stable provider section/product IDs, and captures throughout the declared comparison horizon. MLB should consistently cover the entire final 48 hours, especially every final-day bucket. Longer-range NFL/NHL declines need coverage over the full final week. A missing bucket should be surfaced rather than filled with a guessed price.

Raw ticket tables and successful MySQL migration state are untouched. Only derived summaries are versioned and rebuilt to apply conservative identities consistently; stale versions are excluded until rebuilt. Public reports continue to read compact summaries rather than scanning raw ticket history.
