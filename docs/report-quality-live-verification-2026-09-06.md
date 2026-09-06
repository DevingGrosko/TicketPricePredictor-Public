# Live report-quality verification — 6 September 2026

Application changes: PR #77, commit `c6232e97f41c250a9d33b62efebba484d037d9b2`.

## Validation and deployment

All six PR workflows passed, including 208 Python tests and populated MySQL migration/report fixtures. Local browser fixtures passed twelve full-row hit/click checks across MLB/NFL/NHL and desktop/mobile widths, plus six keyboard activation checks.

The first production summary backfill timed out. Recovery used bounded, retryable one-event batches without another worker reload. Rebuild run `34055159955`, job `101545655147`, completed successfully, including all three version-2 summary rebuilds and the final live audit. Artifact `9995889371` (`rebuilt-live-report-quality`) contains the complete evidence snapshot, completed at 19:42 UTC.

MySQL remained selected and migration writes were unpaused when maintenance started. The recovery workflow did not invoke cutover commands or alter raw ticket rows. Only derived summaries were rebuilt.

## Live checks

All 75 displayed team cards were checked: 11 MLB, 32 NFL, 32 NHL. No duplicate team cards remained. Team identifiers were preserved in report and section URLs. Every rendered ranking row was a native full-row link, and every ranked candidate satisfied the configured minimum game-count and coverage checks. The first ranked section detail page for each team with eligible candidates loaded with its comparable ranking evidence.

The earlier duplicate-card causes and inventory analysis are documented in `report-quality-audit-2026-09-06.md`. Different physical buildings are not merged merely because one team plays at both; shared buildings do not combine different home teams.

## Rebuilt MLB results

These are **priced section groups** in the latest tracked team/venue season, inside the supported analysis horizon—not physical stadium-section counts. They differ from the earlier all-label game-inventory counts.

| Home team | Completed games | Priced section groups | Cheapest-eligible groups | Drop-eligible groups |
|---|---:|---:|---:|---:|
| Atlanta Braves | 23 | 163 | 110 | 99 |
| Baltimore Orioles | 19 | 166 | 123 | 113 |
| Boston Red Sox | 21 | 366 | 99 | 42 |
| Chicago Cubs | 19 | 160 | 127 | 117 |
| Los Angeles Dodgers | 20 | 271 | 214 | 206 |
| New York Mets | 22 | 166 | 152 | 139 |
| New York Yankees | 21 | 133 | 85 | 76 |
| Philadelphia Phillies | 22 | 146 | 112 | 81 |
| St. Louis Cardinals | 14 | 203 | 131 | 122 |
| Tampa Bay Rays | 2 | 37 | 0 | 0 |
| Washington Nationals | 22 | 126 | 103 | 83 |

Totals: 1,937 priced section groups; 1,256 eligible for cheapest comparisons and 1,078 eligible for typical-drop comparisons. Ten of eleven tracked MLB teams have eligible candidates for both lists. Tampa Bay has only two completed games in its latest stored season and is correctly withheld from historical recommendations.

The Mets' lowest ranked section was Promenade Outfield 535, supported by 18 of 22 completed games. Boston's lowest ranked section was Bleachers 39, supported by 14 of 21 completed games. These are evidence-snapshot examples, not promises of available prices or permanent ranking positions.

NFL and NHL had no completed, non-preseason games in the selected report cohorts. Their historical ranking lists therefore remain empty rather than being populated with preseason results. Available individual-game and section histories remain inspectable.

## Interpretation

Three games and 60% coverage are explicit product safeguards, not a validated confidence interval. A section can still be observed for a different subset of games from another section; opponent, weekday, demand, inventory and missingness can influence comparisons. Prices never captured cannot be reconstructed. These are observed listing-price summaries, not completed-sale prices or guaranteed savings.

The initial inventory audit found no repeated exact full game labels within a team/venue bucket. That is only a limited label-based sanity check, not proof that provider/schedule identities never duplicate an official game.
