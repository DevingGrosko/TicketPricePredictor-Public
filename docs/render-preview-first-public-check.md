# First public Render preview check

The owner supplied https://ticketsignal-staging-preview.onrender.com after deploying the isolated Blueprint. This step tested that actual public HTTPS URL; it was not another in-process Flask test.

The chat web tool could not fetch the new URL and the local execution environment could not resolve its hostname. Those local failures were not treated as proof of a site outage. Testing instead ran on the connected GitHub runner.

## Completed run and scope

GitHub Actions run **35803275452**, job **106998256842**, test commit **10d26ef1bed52287cc1d44d165d13cbddf3c7e9d**, completed successfully. The offline URL-boundary, HTML-parser and chart-array assertions passed first. Actual public requests ran from **2026-09-23T00:43:22.258218+00:00** through **2026-09-23T00:43:46.374473+00:00**, about 24 seconds in total.

Run: https://github.com/DevingGrosko/TicketPricePredictor-Public/actions/runs/35803275452

The check made **18 sequential GET requests**, all to the exact owner-supplied preview host. Redirects were disabled. It used ordinary certificate-verified HTTPS, a fixed read-route allowlist, request time and body-size bounds, no database driver or credentials, and no deployment or write endpoints. There was no recurring schedule or ongoing keepalive; the one permitted startup retry was not needed.

## Observed results

| Check | HTTP result | Seconds |
| --- | ---: | ---: |
| `/healthz` | 200; status ok, staging-readonly | 0.117 |
| `/readyz` | 200; status ok, all 3 database connections reported ready | 0.506 |
| MLB landing `/` | 200; labeled preview HTML and selectable venues | 0.127 |
| NFL landing `/nfl` | 200; labeled preview HTML | 0.073 |
| NHL landing `/nhl` | 200; labeled preview HTML | 0.331 |
| Repeated MLB landing `/` | 200 | 0.094 |
| MLB game/section options, one venue | 200; populated JSON | 0.217 |
| MLB single-game price graph, selection from options | 200; **122 numeric X/Y data points**, all finite and arrays the same length | 9.631 |
| Five linked CSS resources | All 200 with CSS bodies | 0.069–0.115 each |
| Linked graph JavaScript | 200 with JavaScript body | 0.165 |
| One linked MLB team report `/baseball/stadium` | 200; preview HTML | 11.861 |
| One linked NFL team report `/nfl/stadium` | 200; preview HTML | 0.063 |
| One linked NHL arena report `/nhl/arena` | 200; preview HTML | 0.373 |
| `/concerts` | Expected 503 with explicit `not_migrated` JSON | 0.110 |

Application responses included `X-TicketSignal-Environment: staging-readonly` and `X-Robots-Tag: noindex, nofollow`. Tested HTML responses contained the visible saved-September-21-snapshot banner. The script did not accept a Render platform loading/error page as a successful app response. The graph check validated embedded chart values, not merely HTTP 200. Report-page checks verified successful labeled HTML, not every report value or every possible venue.

## Interpretation and remaining work

The separate Render website is serving the TiDB-backed sports preview. These are one-run HTTP response timings from one GitHub runner, not browser rendering timings, a cold-start benchmark, or a long-term availability measurement. In particular the selected MLB graph (~9.6 s) and selected MLB report (~11.9 s) remain slow despite passing functional response checks. Do not infer that previous intermittent delays have been solved.

This is still a **read-only saved-snapshot preview**, not a production replacement. Price ingestion and concerts remain deliberately disabled. The checks did not run browser JavaScript, validate all interactive flows, compare live production output, check all game/section/report combinations, exercise new-price ingestion, test independent scheduling, refresh analytics, synchronize observations received after the original export, verify restart/cold-start recovery, or establish ongoing free-tier quota headroom. The owner has not approved production cutover.

Next work is browser/feature validation and slow MLB view investigation, followed by separately isolated new-price ingestion and scheduler testing. Do not turn on the existing production workflows or run the old initial-import/empty-schema rehearsals on the populated databases.

## Change boundary

Only new staging-branch files were added in this step: `tools/render_preview_http_check.py`, `.github/workflows/render-preview-http-check.yml`, and this result record. No existing app code, deployment Blueprint, production workflow, secret, ingestion endpoint, scheduler or database was changed by this step. The test sent no requests to PythonAnywhere and performed no direct database access. It did not initiate a Render deployment, restart, account/billing change, merge to main, or production cutover. The tool does not report or inspect the Render dashboard's Auto Sync setting.
