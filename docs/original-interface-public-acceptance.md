# Restored original interface: public Render acceptance

Verified September 26, 2026. The restored presentation is now live at https://ticketsignal-static-preview.onrender.com/ . This is a successful preview deployment, not a production cutover or live-data migration.

## Deployment evidence

The connected Render service `srv-daqpucgu01pc73bi97a0` in the confirmed Devin's workspace now uses `tools.build_original_fast`, followed by `tools.check_original_static`, and still publishes `./static-preview-dist` as a static site. Automatic code deployment is off.

The user's Blueprint sync created deploy `dep-das1f3vavr4c738kcbl0` for commit `2061e35268ffa71f8cf1f2a1fc12a325c62109a1`. The Render API returned `status: live`, with `finishedAt: 2026-09-26T19:11:09.60529Z` (3:11 PM EDT). Build logs independently ended with `Your site is live`.

Render generated 10,385 original-template HTML pages and 21 original assets. Its build report recorded 184.07 seconds for generation (excluding dependency installation, validation and upload), zero database writes, and live updates disabled. Output validation checked 530 eligible games, 74 reports, 73,710 game-section series, 7,326,718 chart points and 11,487 local link/data/asset targets. These are build-wide counts, not browser download sizes.

## Actual public-site test

Workflow: https://github.com/DevingGrosko/TicketPricePredictor-Public/actions/runs/36265088107
Successful attempt: 2; job: 108468487819; test commit: badd1d9a2c9c97d5020805a41db9cfb15d289819.

The first attempt ran while Render was still uploading and correctly rejected the older simplified homepage. After the Render API confirmed the restored deploy was live, the same test was rerun without changing the application or its assertions. All job steps completed successfully. Public checks ran 2026-09-26T19:12:18.269280Z to 19:12:59.407284Z.

The test requested the actual public address. It verified all 21 original CSS/JavaScript assets byte-for-byte against the reviewed repository, as well as the restored static adapter and preview stylesheet. It checked the deployed Content-Security-Policy, including support for the original Google Fonts. Requested content-addressed JSON files passed their SHA-256 checks.

Real Chrome exercised the original MLB/NFL/NHL homepages, team-report selection, section drill-down and evidence, game/section forms, original chart rendering, exact sampled chart X/Y agreement with published data, dollar/percentage switching, and NFL/NHL map rendering/search/history links. MLB multi-game market history and its historical buying-window value passed. The concert page explicitly states that its data has not been migrated. The tested homepages had no horizontal overflow at a 390-pixel viewport.

| Sport | Homepage ready | Team report ready after click | Section detail ready after click | Game submit to chart ready | Map navigation to ready |
| --- | ---: | ---: | ---: | ---: | ---: |
| MLB | 1.589 s | 1.025 s | 1.054 s | 0.916 s | not sampled separately |
| NFL | 1.017 s | 0.717 s | 0.732 s | 1.005 s | 1.575 s |
| NHL | 1.029 s | 0.653 s | 0.825 s | 0.917 s | 1.467 s |

These are single-run Selenium action/readiness timings from an Azure eastus GitHub runner, with browser HTTP caching disabled. CDN caches may have been warm, including from preceding file checks. They are not Web Vitals, universal speed guarantees, exhaustive selection coverage, or directly comparable ratios against older tests with different selections. Sample individual charts contained 6 MLB, 118 NFL and 16 NHL points.

The test reported 401 browser requests, zero application API requests, zero write requests and zero severe browser-console errors. Only the static origin and the original Google Fonts hosts were allowed by the request audit; PythonAnywhere, TiDB and /api/ requests were blocked. A separate bounded HTTP reader checked publication and asset bytes.

Screenshot/report artifact: 10913646079, `public-original-ui-acceptance`, 8,671,919 bytes, SHA-256 `0a1e8e54c3a6612f51c05cb795af7f8025642432489d33ae3717c08b24461f40`. The downloaded ZIP checksum matched. The live MLB homepage was visually compared with the saved PythonAnywhere reference: the original branding, typography, colours and layout are retained; the preview adds its snapshot notice and shows older game counts. The public NFL map screenshot was also inspected. This is not an exhaustive pixel-parity claim.

## Remaining scope and changes in this verification

The public manifest identifies an original-template publication generated 2026-09-26T19:06:29.993963Z, but source observations remain September 20: 23:30 UTC for MLB and 23:00 UTC for NFL/NHL. `live_updates_enabled` is false. A successful deployment does not make the source observations current.

PythonAnywhere, its existing collectors, the production branch, TiDB schemas and data, billing settings and the older dynamic Render previews were not modified by this verification. Two static-only acceptance files and this note were added to the staging branch; no scheduled task, application change or second deployment was triggered. The Render deployment itself was initiated by the user's Blueprint sync.

The restored interface is ready for the owner's visual review at the existing static-preview address. Post-export data catch-up, independent ongoing ingestion/publishing, concert data, and broader equivalence checks remain unfinished. Keep PythonAnywhere production running until those requirements are resolved and cutover is explicitly approved.
