# Original-interface static restoration: accepted for preview deployment

As of September 26, 2026, the optimized full snapshot build and corrected real-data Chrome acceptance tests have completed successfully. The existing static Render Blueprint has been updated in GitHub to build this presentation. A Render sync/deployment has NOT been triggered directly or independently verified by this work; do not describe the restored interface as confirmed live. PythonAnywhere remains production.

## Original presentation and restored sports flows

The builder renders the existing production Flask templates at build time and copies the original CSS/JavaScript assets, rather than using the earlier redesigned static frontend. The actual-data build generated 10,385 HTML pages and copied 21 original asset files. Asset checks verify byte equality. The original MLB/NFL/NHL homepages, team reports, section drill-down/evidence pages, original interactive chart presentation, dollar/percentage views, NFL/NHL seating maps, MLB multi-game market history, and historical buying-window page are included.

These are the actual original templates, with static data loading and a visible saved-snapshot notice added. This is not a claim of exhaustive pixel/numerical parity for every selection. Counts and recency differ from current production because the source remains the saved export. The JavaScript-disabled PNG fallback is not regenerated; published interactive charts require JavaScript. Concert data is still not migrated; the original concert layout displays an explicit unavailable-in-staging notice. Live ingestion, post-export catch-up and automatic daily/half-hour publishing remain disabled.

## Root cause and bounded profile

Run 36259131224, job 108451455098, profiled 20 section pages for Indianapolis Colts (three games). It took 24.0634 seconds after source extraction; repeated map sanitization/usability checks dominated the profile, while page rendering itself was much smaller. Run 36259471967, job 108452402949, repeated the same bounded profile with build-local caches in 2.4484 seconds. These are separate CI-runner profiling measurements, not public page-load timings.

`tools/build_original_fast.py` retains the original validators for each distinct immutable map/label-set/threshold, caches the repeated results with bounded strong-reference identity caches, memoizes repeated label/section normalization, and restores all patched functions when the build ends or raises. It does not edit production analysis functions or weaken map sanitization. Seven cache tests passed, including full byte-for-byte baseline/cached synthetic output with provider geometry, malformed-map parity, cache bounds, distinct-key separation and restoration after errors.

Baseline profile artifact 10911493423: SHA-256 64e1b5e6c6917909971856783781c54d6eb2798e63f750187978c077d6fd5107.
Optimized profile artifact 10911692771: SHA-256 ecd64c62ace8b80732eea8936a272935463b0c010255224ebab867effee6f1c6.
Both were downloaded and checked against their recorded digests.

## Full build and regression results

Full build run: https://github.com/DevingGrosko/TicketPricePredictor-Public/actions/runs/36259599269
Tested build commit: 0778e403117677b1f21fc0387d9275c0ec3b445f.
Offline job 108452806472 completed successfully: 472 tests run, 465 passed, seven skipped, no failures. Skipped tests require disposable CI MySQL. The seven cache tests are included in this total. Synthetic Chrome flows also passed.

Snapshot job 108453000849 completed successfully. The complete build took 207.05 seconds, excluding dependency installation, validation and artifact upload. The source read 310 MLB games/39,879 captures/5,735,834 tickets; 90 NFL games/10,367 captures/1,562,356 tickets; and 203 NHL games/1,936 captures/143,181 tickets. The original-template rendering stages took 30.81 seconds MLB, 32.45 seconds NFL and 14.24 seconds NHL.

The independent output validation passed: 530 eligible game entries, 74 reports, 73,710 game-section series, 7,326,718 chart points, 1,148 JSON data files, 10,385 HTML pages, 21 original assets, and 11,487 checked local link/data/asset targets. Base data JSON totaled 119,661,273 bytes; additional native output totaled 197,060,223 bytes. This is not the amount a visitor downloads on opening one page.

Build artifact 10911962629, `ticketsignal-original-static`: 62,353,291 ZIP bytes, SHA-256 aaa89c53c39828b62934131116d14e4da8900cea1243834e00ac6ce4f75466d2. The build reported zero database writes and live updates disabled. It does not deploy.

## Final browser issue corrected, then all flows passed

The first real-data browser job in that build failed at its final buying-window label assertion. It exposed a real presentation mismatch: JavaScript toFixed displayed a quarter-hour value of 15.25 as 15.3, whereas the original Python template displays 15.2. Commit c7bf5f6b71df9a37049c0baf74590a7d401b4fe1 corrected the static adapter to preserve the original half-even decimal label. A dedicated JavaScript-vs-Python test checks every quarter-hour value from 0 through 48 hours (193 inputs). It passed.

Successful final browser run: https://github.com/DevingGrosko/TicketPricePredictor-Public/actions/runs/36260249180
Job 108454571124, commit 83ccb19892db91a40dd39db0ff21df38a2de41c6: all steps completed successfully. This run reused the completed full snapshot artifact and replaced ONLY the corrected `native/bridge.js`. It had no TiDB credentials and performed no further database read or write.

All three real-data sport flows passed: original homepages, team-to-section navigation, game/section forms, native charts, exact published chart X/Y equality and percentage normalization. NFL/NHL map rendering and search passed. MLB multi-game chart and the historical buying-window value passed, including the corrected 15.2 label. Homepages passed the 390-pixel no-horizontal-overflow check. The test recorded zero application API requests, zero write requests and zero severe console errors. Original external Google Fonts were allowed; PythonAnywhere, TiDB and API URLs were blocked. Tests used a plain local static HTTP server on the CI runner; these are NOT public Render performance measurements.

Accepted screenshot/report artifact 10912066139, `original-interface-browser-accepted`: 8,670,828 bytes, SHA-256 22e49ab7c4cc48825c13b512f863ccf8edbe0ef1f752ace447723861aceb93c3. Downloaded checksum matched. Actual-data original-layout MLB homepage, NFL map and corrected buying-window screenshots were visually inspected, with the homepage compared to the saved original reference. The final browser report states passed=true, all sport flows=true, legacy_market_and_buying_window=true, console_errors=0.

## Existing Render preview configuration

Commit 8117f5be2f0fcd9eb006ad355bf33b7b70afcbe6 updates only the existing `render-static-staging.yaml` service configuration:

- Same static service `ticketsignal-static-preview`, staging branch, static publish directory, environment inputs and automatic code deploys off.
- Build: install requirements-staging.txt; run `python -u -m tools.build_original_fast --output static-preview-dist`; validate with `python -m tools.check_original_static static-preview-dist`.
- CSP matches the browser-tested original UI policy: original Google Fonts and inline styling allowed, inline executable scripts still prohibited. Hashed data/scripts use immutable caching; entry manifests/adapter/pages revalidate.
- No new database, paid worker/disk, recurring job, service type change, main-branch merge or production cutover.

The owner should open the EXISTING Blueprint managing this static service (previously named `ticketsignal-static-staging-v2`) and choose Manual Sync with Auto Sync disabled. Review/apply changes to the existing `ticketsignal-static-preview` only. Do not create a new Blueprint or merely redeploy with the old build command: the Blueprint sync applies both build-command and header changes. Keep existing environment values. Render documentation: https://render.com/docs/infrastructure-as-code .

If Auto Sync was not disabled, the pushed YAML could initiate a preview deployment automatically; this has not been inspected. Confirm Render status and then test the actual public restored pages before describing it as deployed/accepted on Render.

## Production and remaining work

Comparison from 583c0bc580d4db2bc12f40c094cfe6fe9232b0b2 to 8117f5be2f0fcd9eb006ad355bf33b7b70afcbe6 shows eight changed/added files: static-specific profile/build/browser workflows, cache helper/tests, one static adapter formatting correction, and the existing static-preview YAML. No production Flask template/CSS/JS, main collector workflow or database schema was edited. This work performed read-only staging builds, not production requests, collection, import or database mutations.

The latest saved source observations remain September 20, 2026: 23:30 UTC MLB, 23:00 UTC NFL/NHL. Automatic data updates and concerts remain separate unfinished tasks. Public restored-interface deployment verification, broader devices/selection coverage, all legacy deep-link edge cases and sustained free-tier usage still need acceptance. Frequent publishing must not simply run this full build 48 times daily without quota evaluation and incremental work.
