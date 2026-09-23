# Update cadence and initial graph performance investigation

Status: one candidate query optimization is on the staging branch. It is **not deployed to Render**, and overall live performance validation has **not passed**. Do not treat fewer queries or passing offline tests as full performance acceptance. PythonAnywhere remains production.

## Cadence is separate from page-serving work

The inspected `main` version of `github_dispatcher.py` has a 30-minute interval and an eight-minute offset, intended to dispatch at :08/:38. Its target is the existing `main` collection workflow. The inspected collection workflow executes Selenium on GitHub-hosted runners and posts snapshots to PythonAnywhere. The PythonAnywhere dispatcher supplies the clock; the native GitHub schedule is a six-hour baseball recovery trigger, not the primary half-hour scheduler. NFL's workflow evaluation is hourly with its existing adaptive collection logic. This establishes configured behavior, not a fresh audit of successful captures or delivery guarantees.

No collection interval, endpoint, schedule or production workflow was changed in this investigation. The Render preview still displays the saved sports snapshots and has ingestion disabled. Thirty-minute publishing is technically possible, but an independent scheduler and sustained workload within free limits are not established yet. Daily publishing could still preserve half-hour source observations if those observations continue to be collected; daily scraping instead would not preserve that detail.

## Code findings

The original `GraphBuilder.eachEventGraphList` selected Ticket ORM objects and then dereferenced `ticket.iteration.captured_at` and `ticket.iteration.event.event_date`. The joins used to filter the query did not eager-load these relationships. The candidate selects price, capture time and event time together with the same joins, filters, capture-time ordering, and timezone conversion. It does not remove observations or change refresh frequency. Other graph calculations, plotting and concert code were left in place.

The team-report path already has persistent JSON summaries. Its `render_materialized_mlb_team_report` can synchronously try a refresh and then rebuild a report when the stored payload is missing or considered stale. That is a code path to investigate, not proof that it caused the previously measured 11.9-second report. In this intentionally read-only preview, write attempts are blocked. No team-summary refresh was executed as part of this investigation.

Existing page caches are process-local and expire; they are not a durable precomputed publishing layer. Render's Free web-service startup delay is also separate from SQL/query time.

## Changes and tests

Candidate source commit: `c9e8a7d716aaf590e1d21a18342675ecdf78ace1`.
Latest tested commit: `92486603136c8af6ba2ecf6e0badce3e0399c36c`.

New files are a synthetic regression test, a bounded read-only benchmark, and its staging-only push-triggered workflow. No recurring schedule was added. The only existing source file changed during this step is `graph_builder.py`.

Run `35804289387` passed the seven targeted graph tests but failed full discovery because another collector test's lightweight graph-module stub was imported by the new test. The new tests were corrected to load a private copy of the real module without changing the other tests' imports.

Run **35804438220**, offline job **107001953885**, then passed all seven targeted tests. Full discovery ran **439 tests: 432 passed and 7 skipped**, with no failures. The skipped cases require a disposable MySQL test service that is not configured in this workflow. Coverage checks output equality, constant query count, duplicate observations, exact sections, missing/other-sport filters, zero prices, dollar/percentage views, multi-game aggregation and time windows using synthetic SQLite data.

## Actual live result: incomplete

The same run's live job **107002100932** failed overall. It used only the guarded TiDB staging engines from a GitHub runner. It did not exercise the deployed Render URL and did not send any writes.

Its actual series measurements were:

| History reader | Points | SQL reads | Seconds |
| --- | ---: | ---: | ---: |
| Original lazy relationship reader | 122 | 124 | 50.3977 |
| Candidate scalar reader | 122 | 1 | 23.8075 |

The benchmark next checks exact series equality and a reduced query count before comparing full pages. It subsequently failed with a RuntimeError before producing a full-page measurement or a successful final report. The recorded error does not expose the precise HTTP/database failure, so its underlying cause remains undiagnosed. No successful optimized-page timing was obtained.

The read reduction is observed, but **23.8 seconds remains too slow**. The earlier 9.6-second public Render graph timing came from a different test and cannot be combined with these numbers into a before/after public-site improvement claim. Query plans, server work, connection behavior and full-page profiling still need investigation. The candidate is not approved for production or presented as a completed fix.

Run: https://github.com/DevingGrosko/TicketPricePredictor-Public/actions/runs/35804438220

## Proposed next design, not implemented here

Keep raw price collection independent of the public report refresh rate. Calculate and store ready-to-display chart/report results after data batches rather than forcing visitors to wait for expensive recomputation. Refresh changed game histories on an approximately half-hour cadence where useful, with daily broader historical summaries. Display the last successful data timestamp and retain the last validated output when a refresh fails.

A prebuilt static frontend with small per-game/per-report JSON files is a candidate to remove visitor dependence on a sleeping Flask service. TiDB would remain the history store; the browser would not receive database credentials or download the whole database. Existing chart/dropdown interactions would need explicit adaptation and tests. This is not achieved merely by changing a Render service type. Static-host build and bandwidth limits and scheduler reliability still require measurement; do not promise free, exact half-hour publication without that work.

Official references checked during the investigation:
- https://render.com/docs/free
- https://render.com/docs/static-sites
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows

No Render deploy, paid resource, merge to main, production connection, data import, database/schema modification, collector change or cutover was performed. The failed benchmark left no background process or recurring task scheduled by this work.
