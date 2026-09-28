# Independent collection and publication activation

Activation: 2026-09-28 00:45-00:46 UTC (September 27 evening, America/New_York). Implementation pinned to 0d087c75be5c552d63ec610446e078d1854aef9e. This is the beginning of the reliability trial, not production cutover or historical backfill.

## Installed changes

Main commit 32c6e2d6f8e786cbf33c2e4970fe8274b4f27b16 adds .github/workflows/free-ticket-collect.yml. Main commit 6d3a29bb859a3bea449fad6dfdbc125d42a35d13 changes .github/workflows/free-ticket-site.yml. Comparing main with ecba05c971f429a558527308d44e6d8169198241 confirms these are the only two changed main-branch paths. PythonAnywhere application code, its original workflows, Render configuration and database schemas were not changed. No historical backfill, deletion or overwrite was performed.

Collection is scheduled at minutes 7 and 37. Publishing is separately scheduled at minutes 17 and 47. A completed independent collection workflow additionally triggers publication, whether its conclusion is success or failure. The publisher does not depend on the collector matrix and reads valid committed snapshots while collection may still be running. Per-sport database reads retain the existing transaction-consistent cache reader. Captures committed after a sport's read are eligible for the following publication. Invalid payloads and incomplete transactions are still rejected; failure of the website's own build or validation retains the previously deployed site.

NFL now uses two independent browser workers with unordered completion handling: a fast result can commit before a slow or failed game finishes. Only the coordinator delivers writes, avoiding shared database sessions across browser workers. Each valid snapshot is queued before delivery, committed atomically, independently read back, and removed from its pending queue only after acknowledgment. Failed NFL delivery does not disable other game deliveries. Same-hour completion receipts avoid unnecessarily recapturing successful NFL games when retrying the remainder. Retry counts and run durations remain bounded.

Section rows for all three staging writers are inserted as a single batch per snapshot instead of ORM inserts requiring per-section generated identifiers. Atomic event/iteration/ticket commit and duplicate-slot behavior remain intact. NFL/NHL retain their existing hourly or slower adaptive game tiers; half-hour publishing is not a claim of a new capture of every future game every half-hour.

## Offline verification

Run 36363288684, job 108744595216, implementation 0d087c75be5c552d63ec610446e078d1854aef9e passed: 37 tests and 23 subtests in 4.84 seconds. The checks include existing atomicity/duplicate/cache tests and new failure injection. One test holds a slow game until another game is successfully committed, then fails the slow game and confirms subsequent delivery continues. Another fails one database write and confirms two other games commit while the failed payload remains queued. Tests also verify a single batch insert for each sport, duplicate acknowledgments, bounded worker count, delayed-cron evaluation, publisher independence and cleanup scope. These simulated failures are not a claim that every live provider capture succeeds.

## Actual publication while collection was incomplete

Independent collector run 36363390711 started at approximately 00:45:36 UTC. MLB job 108744877120 finished with a real failure: its two candidate pages yielded no listings response within the 35-second timeout, so no new MLB snapshot was stored. This remains a reported failure, not silently relabeled as successful. NHL job 108744877428 subsequently stored all 16 games due in that slot, with zero failed captures or pending uploads. Its collection step ran approximately 00:46:00-00:52:29 UTC. The initial parallel NFL job 108744877460 was still in progress at the last status check recorded for this note; no completed NFL runtime or speedup is claimed here.

Publisher run 36363421900 ran independently during that same collection run. Build job 108745037585 restored the preceding source cache and included the first seven newly committed NHL captures (423 ticket rows), without waiting for NHL or NFL to finish and without being blocked by the failed MLB job. The original-interface Chrome checks passed for MLB, NFL and NHL, including chart-value comparisons, report navigation, maps and the MLB buying-window flow, with zero severe browser-console errors. These Chrome tests used the built mounted site before deployment.

Deploy job 108745797967 successfully published to https://devinggrosko.github.io/TicketPricePredictor-Public/ and verified the exact public generated_at value 2026-09-28T00:46:57.940541+00:00 at 00:51:01 UTC. The public manifest reported live_updates_enabled=true. At that publication's read points, source maxima were MLB 2026-09-27T16:30:00+00:00, NFL 2026-09-27T16:00:00+00:00 and NHL 2026-09-28T00:00:00+00:00. These are capture-slot labels and per-sport maxima, not evidence of gap-free history or precise wall-clock scrape timestamps. Later successful captures belong in the following publication.

The public upload was 33,048,269 bytes and its temporary artifact was removed after deployment. The pre-upload inventory was 228,725,322 existing repository artifact bytes, below the configured conservative allowance.

## Free-tier safeguards and outstanding trial

Both workflows use ordinary ubuntu-latest GitHub-hosted runners for this public repository. No paid plan, larger runner, payment method or spending-limit increase was added. Cache and artifact checks stop rather than request paid expansion. Recovery state is bounded to 20 MiB per sport; the source cache is bounded to 1 GiB, with two recovery copies retained per namespace and an 8 GiB repository cache safety threshold. Cleanup targets only this free pipeline's completed-run namespaces, not production collector caches. Account-wide billing settings were not inspected or changed and are not certified by these repository-level guards.

The 30-minute schedules are now installed, not merely proposed. GitHub's scheduling is best effort. Sustained run coverage, actual publication intervals, late commits, retries and provider failures still require the initial 48-hour trial. A read-only review was scheduled in ChatGPT for 48 hours after activation work. Keep PythonAnywhere and its existing collectors running. Missing historical days and concert migration remain deferred until the replacement and a safe overlap-aware backfill are verified.
