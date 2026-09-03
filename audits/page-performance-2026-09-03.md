# Live page-performance audit — 2026-09-03

Production host tested: `https://bunnyjeff.pythonanywhere.com`

The benchmark used `curl -L` from a GitHub-hosted runner, recorded time to first byte, total transfer time, and response bytes, and applied a 45-second per-request timeout.

| Page | HTTP | Time to first byte | Total time | Response size |
|---|---:|---:|---:|---:|
| MLB home `/` | 200 | 4.701 s | 5.093 s | 1,451,907 bytes |
| NFL home `/nfl` | 200 | 1.023 s | 1.220 s | 391,664 bytes |
| NHL home `/nhl` | 200 | 1.400 s | 1.597 s | 366,965 bytes |
| Fenway report | timeout | no bytes in 45 s | >45 s | 0 bytes |
| Citi Field report | timeout | no bytes in 45 s | >45 s | 0 bytes |
| Fenway section detail | timeout | no bytes in 45 s | >45 s | 0 bytes |
| SoFi section detail | timeout | no bytes in 45 s | >45 s | 0 bytes |

## Interpretation

The home-page measurements prove that the delay is substantially server-side and that the MLB landing page is also unusually large. The Fenway report was the first expensive team-report request in the sequence and did not return within 45 seconds. Later requests may have been delayed both by their own work and by worker queueing behind that still-running request, so this run should not be used to claim that every listed report independently takes more than 45 seconds.

## Code-level causes found

1. Home routes load all tracked event objects and embed game/section dictionaries for every event into the returned HTML. The MLB response is about 1.45 MB.
2. A team report reads every ticket snapshot row for every game at the selected venue, then performs grouping, time bucketing, price aggregation, and drawdown calculations in Python on every request.
3. The `Areas analyzed` addition performs another full pass over the same snapshot rows.
4. A section-detail request first constructs the complete team report, then opens the database again, retrieves the venue data again, and builds the selected section timeline and map.
5. NFL and NHL home/report queries load full ORM event rows, including stored map geometry that is not needed for ordinary cards or rankings.
6. The baseball schema does not declare indexes on `iterations.event_id`, `tickets.iteration_id`, or `tickets.section`, although those fields are central to report queries.

## Recommended order

1. Add short-lived server-side caching for home, team-report, and section-detail contexts; invalidate it when a snapshot is stored.
2. Remove duplicate passes and duplicate database reads. A section-detail request should query only its selected section and reuse the team summary.
3. Add the missing baseball indexes and select only the columns needed by each page.
4. Stop embedding the full game/section inventory in the landing-page HTML; fetch detailed dropdown data only when a user selects a team or game.
5. Re-benchmark. Move production data from SQLite to MySQL or PostgreSQL if request times remain high after the query and caching fixes.
