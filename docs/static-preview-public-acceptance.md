# Deployed static preview: first public browser acceptance

The owner supplied `https://ticketsignal-static-preview.onrender.com/#sport=mlb` after deploying the static Blueprint. This check tested that actual public site, not the older dynamic Render preview or a local static server.

## Completed test

- GitHub workflow: **Public static preview acceptance**.
- Run **36080369028**, job **107900772059**, commit **01e32fb2d9c5d19616251f80d4fadea09748d119**.
- All job steps completed successfully, including offline URL/series assertions and the public Chrome test.
- Public checks started **2026-09-25T01:04:11.513793Z** and finished **2026-09-25T01:04:19.692934Z** (September 24 evening in US Eastern time).
- Run: https://github.com/DevingGrosko/TicketPricePredictor-Public/actions/runs/36080369028

The chat web fetch and local DNS could not access this hostname; those environment failures were not treated as a site outage. Actual public testing used the connected GitHub runner, reported by its setup as Azure westus. The test used no TiDB credentials or SQL clients, did not query production, and only requested the fixed static host. A new branch-only test script and push-triggered workflow were added; no recurring schedule or deploy was started.

## Public browser measurements

Real Chrome loaded the deployed site with its HTTP cache disabled. Each sport began from a blank document, resetting the JavaScript application cache. CDN caches were not cleared and may already have been warm, including from the HTTP checks immediately beforehand. These are single-run functional timings including Selenium action/polling overhead, not Web Vitals, a load test, or universal latency guarantees.

| Sport | Directory usable after navigation | Click team report to ready | Open game view, select game/section, show chart | Sample chart points |
| --- | ---: | ---: | ---: | ---: |
| MLB | 0.302 s | 0.126 s | 0.481 s | 145 |
| NFL | 0.133 s | 0.103 s | 0.529 s | 253 |
| NHL | 0.544 s | 0.105 s | 0.585 s | 16 |

All sampled directories, report selections and chart interactions completed in under 0.6 seconds in this run. This does not establish exhaustive performance coverage or directly comparable speedup ratios against earlier Flask measurements, which used different selections, environments and timing boundaries.

For all three sport flows, the test checked game and section selection, a populated SVG chart, exact X/Y array equality with the downloaded published series, the dollar-to-relative-price toggle, keyboard point inspection, directory back navigation, filtering out a nonexistent team search, and same-document report deep-link navigation. The relative view began at 100.0%. There was no horizontal document overflow at a 390-pixel viewport. Six desktop/mobile screenshots were captured; the MLB desktop chart and mobile screenshot were inspected visually. This was not comprehensive device, accessibility or visual-parity testing.

The browser recorded **30 HTTP GET requests**, all to the same static origin, **zero application API requests**, **zero write requests**, and **zero severe browser-console errors**. Brotli response compression was observed. PythonAnywhere, TiDB and `/api/` URLs were explicitly blocked in the browser as an additional boundary.

## File checks and freshness

An independent bounded HTTP reader made **16 successful GET requests**: HTML, JavaScript, CSS, manifest, and one directory/report/game/series set per sport. The deployed HTML, JavaScript and CSS matched the reviewed repository files byte for byte. All 12 content-addressed data responses matched the SHA-256 embedded in their filenames. Preview responses contained noindex headers. Entry files used `no-cache`; hashed data used `public, max-age=31536000, immutable`.

The initial HTML was **4,550 bytes** and took **0.0927 seconds** for that HTTP request. The manifest was **880 bytes**. These sizes exclude subsequently fetched data and assets and must not be described as the whole page download. Sample raw HTTP data responses took **0.0691–0.3537 seconds** with compression explicitly disabled in that reader; those are separate from the browser timings.

The deployed directory lists **303 MLB, 88 NFL and 139 NHL eligible games**, and **10 MLB, 32 NFL and 32 NHL team/venue reports**. Those are **530 eligible game entries and 74 reports**, not all underlying database records.

The manifest states **live_updates_enabled: false**. Its publication build time is **2026-09-24T22:08:30.276015Z**, but the latest source observations remain:

- MLB: **2026-09-20T23:30:00Z**.
- NFL: **2026-09-20T23:00:00Z**.
- NHL: **2026-09-20T23:00:00Z**.

The successful website deployment does not make those observations current. Browser-to-file equality is not a new database-to-export reconciliation or proof of numerical parity with every legacy calculation.

Results and screenshots artifact: **10841657855**, `public-static-preview-results`, ZIP size **333,104 bytes**, SHA-256 `db5633f8cc1b5a1b9f00ed0807d3c5ed25c5a44a5de7db301f652bbafb9d312b`. Its checksum was also verified after download to the working container.

## Boundaries and next gates

This is a successful first public static-host acceptance test. It is still a saved-snapshot preview. No collection, catch-up, daily publishing, half-hour incremental publishing, summary refresh, database write, service creation/deploy, billing change, merge, domain switch or production cutover occurred in this step. PythonAnywhere and existing collectors were not changed. The existing dynamic preview was not contacted.

Live-data catch-up, isolated ongoing ingestion, an independent scheduler, validated incremental publication and sustained usage within the free allowances remain unfinished. The earlier static-preview limitations also remain: seating maps, legacy multi-game comparison/prediction flows, concerts, old-route compatibility and exhaustive numerical parity have not been implemented or accepted by this test. Keep production running until those replacement requirements are resolved and cutover is explicitly approved.

Next work should preserve source collection frequency, publish only changed game outputs, retain the previous complete publication on failure, and refresh broader historical reports at a lower cadence. This test has not enabled or promised a 30-minute refresh schedule.
