# Free half-hour publication: implementation and rehearsal status

Verified September 26, 2026. The requested publication interval is 30 minutes, not daily. Work is isolated on feature/free-half-hour-publication, based on staging/tidb-free-hosting-2026-09-20. No recurring publication schedule or GitHub Pages deployment has been enabled. PythonAnywhere and the existing Render preview remain unchanged.

## Implemented path

Existing collector/parser functions -> isolated TiDB staging raw tables -> recoverable source cache -> original-template static build on a standard GitHub Actions runner -> prepared GitHub Pages publication workflow.

The same original templates and styles are used. Published copies of the navigation scripts, links and form targets are adapted to /TicketPricePredictor-Public, the intended GitHub Pages project path. No existing production template, stylesheet, collector workflow or database schema was edited. A branch comparison against 7df2947de980fc9f4f80d49098db03391233837b showed only 13 newly added helper, test, rehearsal-workflow and template files before this note.

The implementation avoids half-hour Render rebuilds. TiDB remains the durable database; GitHub's recoverable cache is not a replacement database. Source reads are incremental after the initial cache seed. The HTML/report build still regenerates the publication on the Actions runner; this is not yet a changed-pages-only renderer.

## One real new capture accepted into staging

Workflow run 36279602452; capture job 108508870396. The bounded smoke discovered a real upcoming MLB game, New York Mets at Washington Nationals at Nationals Park, using the existing Selenium collector. A candidate with only one usable section was rejected before the accepted game was captured.

The accepted snapshot contained 109 sections and was committed to ticketsignal_staging_mlb with event_id 30313, iteration_id 70496 and capture slot 2026-09-26T23:30:00. A second delivery returned duplicate with the same identifiers and section count. The smoke reported passed=true and production_requests=0. It did not POST to PythonAnywhere.

Artifact 10918846520, free-refresh-one-capture-report: 451 bytes; SHA-256 bc59fb6bab6c7b4e2998475c007ceb3706041dcf1dab01507ab0e01c6d175ff7.

This was an actual staging write: one event, one capture and 109 ticket-price rows. It was not a full all-sports collection cycle and did not fill the post-export history gap. No stored historical capture was deleted or overwritten.

## Full cached build and browser rehearsal passed

Successful run: https://github.com/DevingGrosko/TicketPricePredictor-Public/actions/runs/36280285372
Tested build/adapter commit: 82e972a3a14dc55b78c5f8892fa8294f99c809d4.
Offline job 108510685696 and full snapshot/browser job 108510803173 completed successfully.

The build read the historical staging snapshot plus the accepted new MLB capture. Source counts:
- MLB: 311 events, 39,880 captures, 5,735,943 ticket rows.
- NFL: 90 events, 10,367 captures, 1,562,356 ticket rows.
- NHL: 203 events, 1,936 captures, 143,181 ticket rows.

It produced 531 eligible game entries, 74 reports, 10,385 original-template HTML pages and 24,641 total public files. The mounted publication totaled 327,821,230 bytes and passed 21,460 local link/data/asset target checks. This is the full published site size, not one visitor's download.

Original generation took 210.14 seconds; subsequent project-path adaptation and validation took additional time. This is build preparation, not a public-page latency measurement.

The independent fresh-capture output assertion found event 30313 in the generated catalog with captured_through 2026-09-26T23:30:00+00:00, 109 section series and one observation per section. It reported deployed=false.

A second source read against all three actual TiDB databases reused the cache and fetched zero raw ticket rows, with zero unseen captures. It still read the small metadata catalogs. This verifies avoidance of repeated full raw-ticket scans; it is not a claim of zero database requests. Persistent GitHub cache restore/save across different runner executions has not yet been accepted end to end.

Chrome then exercised the original interface under the actual intended project-path structure on a local static HTTP server. MLB, NFL and NHL home/report/section/game-chart flows passed; sampled chart X/Y values and percentage normalization matched. NFL/NHL map navigation and search passed. MLB multi-game history and buying-window output passed. Mobile homepages had no horizontal overflow; the engineering banner remained hidden. The browser report recorded zero severe console errors and zero application API requests.

FREE_PAGES_BROWSER passed=true. Sample charts contained 6 MLB, 118 NFL and 16 NHL points. This was not a public GitHub Pages test and does not establish that a deployed URL exists yet.

Report artifact 10918009582, free-pages-rehearsal-result: 438 bytes; SHA-256 d40ac49ed885cbf8781e85ae637a454d3d822f9cc3f4442681a9dae83f97b1bb.

Two earlier mounted-site rehearsals failed and were corrected: section-picker option values initially lacked the project prefix, then the local test server incorrectly dropped that prefix in directory-slash redirects. The third rehearsal passed without weakening the navigation or data assertions.

## Safety and orchestration tests

Run 36280568893 at commit b80c413b4a23410cc566bae664e86445bc4e617b; job 108511466755 completed successfully. It ran the free-refresh test files and validated the inactive workflow template.

Tests cover raw write restrictions, transactional rollback, all-three-sport duplicate handling, capture-slot rules, stale-replay metadata preservation, lower-ID late commits, failed-cache preservation, rescheduled-event lead times, source-cache sport identity, unchanged CSS/data under project mounting, adaptive sport cadence, repository storage caps, cleanup scope and directory redirects.

The prepared workflow uses a 17,47 minute cron, standard ubuntu-latest runners, serialized writers, bounded pending queues, source caching and failure-before-publication gates. MLB is evaluated each half-hour; NFL/NHL retain hourly evaluation of their existing adaptive per-game tiers. Half-hour publication does not mean a fresh scrape of every future game every half-hour.

The storage helper only selects its own namespaced completed-run caches and temporary publication artifacts. It does not delete existing production collector caches/artifacts. Upload budgets stop rather than request paid storage. These helpers are unit-tested; their real GitHub API cleanup/deployment interaction is not yet accepted. No artifact/cache cleanup was executed in this rehearsal.

## Required owner setting and activation gates

A read-only GitHub Pages settings request with pages:read returned HTTP 404. This means a configured Pages site was not accessible to that workflow token; no Pages configuration was created or changed. The owner should open repository Settings -> Pages -> Build and deployment -> Source, choose GitHub Actions, and skip the suggested Jekyll/static workflow templates. The prepared project-specific workflow is already in this branch.

The connected GitHub actions currently available here do not expose a Pages configuration write. No administration token or new account is required from the owner for the dashboard selection.

The production scheduler template is intentionally in docs/free-ticket-site.workflow.yml, not .github/workflows. Its source commit placeholder must be replaced with a final reviewed 40-character commit when installed. Activating later should add ONLY .github/workflows/free-ticket-site.yml on main, not merge this feature branch wholesale. Existing PythonAnywhere deployment path filters were inspected and do not match that new standalone workflow path.

Before unattended activation: confirm owner-controlled paid overages are blocked, make a first actual Pages deployment, verify new captures on its public URL, and test the complete capture/build/deploy/cleanup sequence. GitHub standard runners for public repositories and Pages are free, but storage and service allowances remain bounded. Keep TiDB spending at Free/$0. No billing settings, spending limits, paid resources or payment methods were changed by this work.

Official references checked September 26, 2026:
- https://docs.github.com/en/billing/concepts/product-billing/github-actions
- https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site
- https://docs.github.com/en/billing/how-tos/set-up-budgets

## Still unfinished

Public Pages deployment, the activated 30-minute schedule, a complete independent all-sports live cycle, post-September-20 historical catch-up, concert migration and sustained free-tier usage acceptance remain unfinished. The successful one-game capture does not make all historical data current. Catch-up must reconcile event/provider identities and capture slots; new independent IDs must not be blindly overwritten with production IDs.

Keep PythonAnywhere production and the working Render preview available until the replacement is verified and cutover is explicitly approved. This work added no paid service and did not cancel the existing PythonAnywhere subscription.
