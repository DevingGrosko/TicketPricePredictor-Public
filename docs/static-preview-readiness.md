# Static sports preview: build and browser validation

Status: **the first static snapshot preview is implemented and has passed a full real-data build plus representative Chrome interaction tests. It has NOT been deployed to Render and is NOT a production replacement.** PythonAnywhere and the existing dynamic Render preview were not changed. Live collection, incremental publication and full legacy feature parity remain separate gates.

Branch: `staging/tidb-free-hosting-2026-09-20`.

## What was built

- `tools/build_static_preview.py`: isolated, read-only export from the three TiDB staging sports schemas. Required source columns are read once per sport in a repeatable-read transaction, with the large ticket result streamed into a temporary local SQLite work file. The remote connection closes before report generation. The temporary database is never published.
- `static_preview/index.html`, `app.js`, `styles.css`: independent static frontend with MLB/NFL/NHL directories, team/venue search, precomputed section reports and rankings, game/section selectors, dollar/relative-price views, and SVG charts with pointer and keyboard inspection. No application API or database connection is used by the browser.
- Hashed JSON files are loaded on demand and checked with SHA-256 in the browser. The directory is not a download of the entire historical database. The initial manifest is 880 bytes; source sport indexes are about 42–112 KB uncompressed. Chart shards are capped at 384 KiB. The largest report in this build is about 1.07 MiB uncompressed; reports are not all fetched at once. Browser cache retains at most 24 data objects.
- The builder reuses existing pure section identity, bucket aggregation and ranking functions rather than issuing requests to slow Flask views. Teams, actual venues, currencies and latest seasons remain separate; preseason, excluded products and known incomplete MLB dates are excluded using existing policies. Raw individual-game charts preserve provider labels and do not claim all legacy case-insensitive selection behavior is identical.
- Source-capture timestamps are shown separately from the publication-build timestamp. The UI and manifest explicitly label the site as a historical snapshot with live updates disabled.
- File bounds, checksums, references and chart dimensions are checked. A new output directory is exposed only after a successful build; an existing output is not overwritten. No SQL dump, working database, credentials, Python source or `.env` is included in the publish directory.
- `render-static-staging.yaml`: a NEW static Render service named `ticketsignal-static-preview`, not the existing `ticketsignal-staging-preview`. It has no running Python service, extra database, disk, worker or cron; code auto-deployment is disabled. Only `static-preview-dist` is published. TiDB settings are build-time secret inputs, never frontend settings.

## Completed full build and unit tests

Run **35807163830**, commit **583cf8cf2f98e1a03ab93ee66833921165178c8c**:
https://github.com/DevingGrosko/TicketPricePredictor-Public/actions/runs/35807163830

Offline job **107010466138** passed syntax checks and all **16 targeted static-preview tests**. Full test discovery ran **455 tests: 448 passed, 7 skipped, no failures**. The 16 targeted tests are included in that total, not additional. Skipped tests require the separately configured disposable MySQL service. The same 16 unit tests also ran successfully locally using synthetic/fake source clients; that was not a local live database test.

Snapshot job **107010627868** completed successfully. Its build took **231.54 seconds**, excluding dependency installation and artifact upload. It read these actual staging source counts:

| Sport | Source games | Source captures | Source ticket records | Eligible static game entries | Team/venue reports |
| --- | ---: | ---: | ---: | ---: | ---: |
| MLB | 310 | 39,879 | 5,735,834 | 303 | 10 |
| NFL | 90 | 10,367 | 1,562,356 | 88 | 32 |
| NHL | 203 | 1,936 | 143,181 | 139 | 32 |
| Total | 603 | 52,182 | 7,441,371 | 530 | 74 |

The different eligible totals reflect public-history filters, not source deletions. The builder wrote no source rows or schemas. Some eligible games can have no usable chart observations; a game entry is not a guarantee of a populated chart in every section.

Output: **1,148 JSON data files, 119,651,060 JSON bytes**, maximum file **1,117,660 bytes**. Every file and reference passed the independent post-build check. It resolved **73,710 game–section series and 7,326,718 chart points**. These are publication counts, not a claim that every raw ticket must be public or that every historic UI calculation has been independently compared.

Latest source capture shown by the build was **2026-09-20 23:30 UTC for MLB** and **2026-09-20 23:00 UTC for NFL/NHL**. The September 23 build time does not make the source prices current.

Initial data artifact: `ticketsignal-static-snapshot`, artifact **10728756258**, ZIP SHA-256 `9955cc1e3c8f7ab1b03c13beab18e7f8f94548518f1275a63bb7aa41a35e7b4c`, compressed size **12,148,441 bytes**. The downloaded artifact's checksum was also verified locally.

## Browser failure found, fixed, then retested

The first run's browser job **107011550710** completed the MLB flow but timed out opening the next sport via a same-document URL fragment. The initial UI handled deep links only during first page startup. This was corrected with a hash-change navigation handler in **dd582eccdadc5b447e28c49f35856c709efe7509**. Chart labels for one-point series were also made readable and pluralization corrected.

To avoid an unnecessary repeat of the multi-million-row database read for a frontend-only correction, the corrected UI was tested against the already validated data artifact in a separate job. The UI commit deliberately skipped the duplicate full-build push job; it did NOT bypass the subsequent syntax, unit, integrity or actual browser tests.

Run **35807900255**, tested commit **64f1d82af88b0d9e8290863873e20dbda166eb0b**, browser job **107012692627**, completed successfully:
https://github.com/DevingGrosko/TicketPricePredictor-Public/actions/runs/35807900255

The runner had no TiDB credentials. It served the artifact with a plain Python static HTTP server, copied in the corrected UI, and exercised actual Chrome/Selenium interactions. No Flask server ran and no external HTTP requests were observed from the page. All 16 targeted unit tests and all generated-data integrity checks passed again.

Representative MLB/NFL/NHL report/game flows used games with 145, 253 and 16 captures respectively. Tests exercised cross-sport deep-link navigation, game and section changes, populated SVG chart output, relative-price toggle, arrow-key point inspection, directory back navigation and search filtering. All three sport flows passed. The mobile-width check found no horizontal document overflow at a 390-pixel viewport. Six desktop/mobile screenshots were generated; representative screenshots were visually inspected.

Measured local-browser flows were **1.017 s MLB, 0.838 s NFL, 0.811 s NHL**. These combine local navigation and interactions against an in-runner static server. **They are NOT public Render/CDN response times, not single-page-load measurements, and must not be compared directly with the old remote Flask measurements as a speedup ratio.** Real public hosting performance is still unmeasured.

Accepted package: `ticketsignal-static-tested-preview`, artifact **10727969419**, ZIP SHA-256 `f5a2eeb06a53ad884c32bd486f8c13f560853888bf6b1cbf0e03a76d7a533ec2`, size **12,148,523 bytes**. Screenshots artifact **10727954578**, SHA-256 `28b4d8eae5f399e3ebcfb25592b4f7fef97ecfee5cb070261eb905f2d66368a7`.

The browser-only workflow pins the source artifact from this completed build. That test fixture expires after seven days; update its run reference after another full build rather than treating the pinned artifact as permanent hosting or a live data source. Render's proposed build reads TiDB directly and does not depend on this Actions artifact.

## Deployment: owner action still required

A Render integration search returned no available connected deployment tool. No service has been created, modified or deployed by these changes. Create a **NEW Blueprint**, leaving the old one alone:

- Blueprint name: `ticketsignal-static-staging`.
- Repository: `DevingGrosko/TicketPricePredictor-Public`.
- Branch: `staging/tidb-free-hosting-2026-09-20`.
- Blueprint Path: `render-static-staging.yaml`.
- Review: one new **Static Site**, `ticketsignal-static-preview`; no additional paid resources.
- Enter the existing three `TIDB_STAGING_*` values directly into Render's build-secret fields. Do not enter production PythonAnywhere credentials or upload `.env`.
- After deployment, set this Blueprint's Auto Sync to No as well; infrastructure sync is distinct from disabled service code auto-deploy.
- Share the newly assigned URL and build errors, if any. Do not guess the URL from the desired service name.

The build configuration matches the current documented static Blueprint fields, but it has not yet been exercised on Render's build infrastructure. Static hosting is free within included workspace bandwidth and pipeline-minute allowances, not unlimited. No paid resource or plan change is authorized here.

## Remaining work before replacement

This is a first static implementation, not the unchanged old frontend. **Seating maps, the legacy multi-game comparison/prediction flows, concerts, all old route/deep-link compatibility, and exhaustive numerical parity are not included yet.** Existing section report timelines are present, but they must not be mislabeled as the old quarter-hour multi-game tool. Broader browser/device tests and visual acceptance remain necessary.

**No live ingestion, post-export catch-up, independent collection scheduler, daily automatic publishing or half-hour incremental publishing was activated.** The initial builder recomputes the complete snapshot in roughly four minutes on the measured runner. It must not be scheduled 48 times per day without first measuring request units/build quotas and implementing appropriate incremental updates. At the measured duration, 48 complete builds would be roughly 185 build minutes per day before installation/hosting overhead, not a sensible assumed-free plan.

The next cadence implementation should preserve frequent raw collection, regenerate only changed game payloads, refresh broader historical reports on a lower cadence, publish a validated manifest last, retain the prior complete publication when a refresh fails, and display per-dataset freshness. Storing outputs under content hashes helps caching but does not by itself implement incrementality, cross-deploy old-file retention, an independent scheduler, or complete catch-up logic.

## Production boundary

Comparison from **34073481566a6fedc0044c4f2213cbf2e3c68917** through **64f1d82af88b0d9e8290863873e20dbda166eb0b** showed only nine new static-specific files across ten staging commits. No existing production source, workflow, collector, endpoint, database schema or Render dynamic Blueprint was modified in this step. No merge, production request, new price collection, TiDB write, paid service, domain switch or production cutover occurred. PythonAnywhere remains authoritative until the owner approves a separately tested replacement.

Official provider references checked:
- https://render.com/docs/static-sites
- https://render.com/docs/free
- https://render.com/docs/blueprint-spec
- https://docs.github.com/en/actions/how-tos/manage-workflow-runs/skip-workflow-runs
