# Banner change and 30-minute refresh audit

Verified September 26, 2026. Requested website publishing cadence: every 30 minutes, not daily. This cadence has NOT been enabled for the Render static site by this work.

## Display change completed

Commit 6d483f566a861b49e621eaed1621c0b981035223 changes only static_original/preview.css. The engineering/snapshot banner is no longer displayed on sports pages. This is a display change; it does not change capture timestamps, invent freshness, enable ingestion or remove the concert page's explicit missing-data notice.

The Render API confirmed deploy dep-das3pigjo6nc73a3dvng of service srv-daqpucgu01pc73bi97a0 live at 2026-09-26T21:49:45.746874Z. The build generated the same 530 eligible games and 74 reports, reported zero database writes and live_updates_enabled=false, and took 173.46 seconds for generation alone. The original source snapshot counts were unchanged.

Public Chrome verification: run https://github.com/DevingGrosko/TicketPricePredictor-Public/actions/runs/36274099144 , job 108493386939. BANNER_DISPLAY_RESULT passed=true. MLB, NFL and NHL homepages showed no banner at desktop and 390-pixel mobile widths; their original headlines remained visible and there was no horizontal overflow. The concert missing-data notice remained visible. No severe browser-console errors were reported. The public stylesheet matched the reviewed file, SHA-256 06cac1201732ad0248a9517b2979be873b738b0ce4bc449cd2da9e6387bb8199. The report artifact is 10917005949.

The same public test confirmed live_updates_enabled=false and latest stored observations of 2026-09-20T23:30:00+00:00 for MLB, and 2026-09-20T23:00:00+00:00 for NFL/NHL. Removing the banner did NOT make these data current.

## Current production capture evidence

Inspected the actual health-report JSON artifacts, not just green workflow status. Each downloaded ZIP was checked against the GitHub-reported SHA-256 digest.

MLB: run 36273566959, artifact 10915934734, capture slot 2026-09-26T21:30:00+00:00. Five games due, four captured, four uploads with result=stored, one skipped, zero current capture failures, zero discovery failures and zero pending snapshots. Report status healthy. ZIP SHA-256 49b2e7fa3565ac21a6c15033929e6883c1d574711e43a360da6e62502685177f.

The MLB report also lists 139 rejected older replay records: 120 rejected for the seven-day replay limit and 19 for the baseball 72-hour capture window. These are separate from the four successful current captures. This audit does not establish that historical collection was gap-free or resolve those old records.

NFL: run 36271877951, artifact 10915384277, capture slot 2026-09-26T17:00:00-04:00. All 28 due games captured and uploaded with result=stored; zero failed, unresolved or pending. Report status healthy. ZIP SHA-256 3a4e7fcc7eebed94e54a3751e5e7d13fcb2468a3c3189f871d436caef16eee1e.

NHL: same run 36271877951, artifact 10916306293, capture slot 2026-09-26T17:00:00-04:00. All nine due games captured and uploaded with result=stored; zero failed, unresolved or pending. Report status healthy. ZIP SHA-256 f428e6f3b4523e1698867cede07db7efd899e09537a1730173f856ad565036e6.

These are sampled recent runs, not an all-games or long-term uptime guarantee.

## Destination and cadence distinction

The inspected main-branch .github/workflows/collect-ticket-prices.yml (commit 029f1cdb93ed6fd70f56f6ab6e0f32f173a6e22e) sends baseball snapshots to https://bunnyjeff.pythonanywhere.com/api/collector/snapshot, NFL to /api/nfl/snapshot and NHL to /api/nhl/snapshot on that same production host. It does not send these captures to TiDB or publish them to the Render static site.

PythonAnywhere requests the main workflow at :08 and :38. Baseball uses half-hour capture slots. NFL and NHL are evaluated hourly, skipping the second-half-hour dispatch; their collectors apply adaptive per-game schedules. The inspected NFL report states 1-hour / 3-hour / 6-hour tiers. The NHL report states 1-hour / 6-hour / 12-hour / 24-hour tiers. A 30-minute website publication does not imply a new capture of every future game every 30 minutes.

## Publishing gap and free-tier constraint

The Render service still runs a full TiDB snapshot build and has automatic code deployment disabled. Fresh production captures need a tested delivery path into the new data pipeline, post-export catch-up, and a publisher that processes changed data. The existing full-build path is not yet an incremental publisher. A cron trigger alone would only republish the old snapshot.

Official Render build documentation retrieved September 26, 2026: https://render.com/docs/build-pipeline . Hobby includes 500 pipeline minutes per month. At the newly measured 173.46-second generation time, 48 full builds per day for 30 days would use approximately 4,163 generation minutes, before dependency installation, validation and other counted tasks. This is not a safe assumed-free half-hour schedule. Do not enable repeated full Render rebuilds or raise spending limits as a shortcut.

Official GitHub documentation: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows supports scheduled workflows at intervals including 30 minutes, but warns that runs can be delayed or dropped and scheduled workflows must be on the default branch. https://docs.github.com/en/actions/concepts/billing-and-usage states that standard hosted runners in public repositories are free. These facts support investigating GitHub-based preprocessing and separate lightweight data publication; they do not establish that an end-to-end free half-hour implementation is already working.

No existing production collector, PythonAnywhere settings, main branch, TiDB schema/data or billing configuration was changed. This work changed the static banner display, deployed that one change, added a bounded public display test, and audited recent capture results. Ongoing ingestion and 30-minute publishing remain unimplemented, not silently enabled or replaced with a daily schedule.
