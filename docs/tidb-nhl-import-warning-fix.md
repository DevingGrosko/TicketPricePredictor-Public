# NHL importer: TiDB warning-query fix

## Cause and correction

The owner ran the pinned NHL importer and reported `ProgrammingError (database code 1064)` immediately after authentication. The importer used `SHOW WARNINGS LIMIT 1` after its first INSERT batch and before committing. This syntax worked in the disposable MySQL tests but is rejected by the connected TiDB instance.

Commit `8bfdbfc83f48fc552daea051b3dd644b7729e6d6` changes that statement to `SHOW WARNINGS`. The existing `fetchone()` check still rejects any server warning; warnings are not suppressed and the rollback-on-failure behavior is unchanged. The only other file difference is the end-of-file newline.

Provider syntax reference: https://docs.pingcap.com/tidbcloud/sql-statement-show-warnings/

## Actual live verification

GitHub run `35549753226`, job `106182176576`, commit `ce72c0084cf9d6aef35a138e0cc0a15a2d54a50f`, completed successfully at 2026-09-21T01:05 UTC.
Run: https://github.com/DevingGrosko/TicketPricePredictor-Public/actions/runs/35549753226

Observed on the actual TiDB NHL staging database:
- Authentication, all six schema checks, and all three importer session-setting statements succeeded.
- `SHOW WARNINGS LIMIT 1` independently reproduced database error 1064.
- The corrected importer `insert_batch()` inserted a synthetic row, ran plain `SHOW WARNINGS`, and reached its commit boundary. A test wrapper compared the inserted values and forced rollback instead of permitting that commit.
- A deliberately overflowing synthetic integer under connection-local non-strict mode generated a real server warning. The importer rejected it and rolled back the batch, confirming that the fix did not remove conversion protection.
- All six NHL staging tables contained exactly zero committed rows both BEFORE and AFTER the test: analytics_dirty_venue, nhl_event, nhl_iterations, nhl_tickets, section_bucket_summary, and section_summary_state.
- The smoke workflow's offline test phase passed 36 tests and skipped 4 disposable-MySQL-only tests. It was followed by the live TiDB probes above; these are separate from a complete historical import.

Corrected importer SHA-256 printed by the successful runner:
`89c5fb1e758ce85920eaa94c76461d6c5044900bd1c477cdec09a19928c83949`
Git blob: `eafe889937e6247fb9dd16967c65f605678c8784`.

## Scope and next action

No historical rows were imported by this diagnostic; there is no partial NHL row import visible in staging to undo. No synthetic data was committed. No production source credentials, PythonAnywhere services, export files, deployments, or collector endpoints were accessed or changed. All commits remain on `staging/tidb-free-hosting-2026-09-20`; comparison after the fix showed main still at `029f1cdb93ed6fd70f56f6ab6e0f32f173a6e22e`, with only added staging files relative to main.

The owner still has the older checksum-pinned script in `/home/bunnyjeff/ticketsignal-nhl-import.S6LrvnKj/`. Download the fixed commit to a NEW helper filename there, verify the corrected checksum above, reuse that isolated virtual environment, and run `--resume` against the existing checksum-pinned NHL export with a new report filename. Resume compares every existing target row before inserting only missing rows and never deletes or overwrites. The live diagnostic found empty tables, but resume retains protection against any state change between checking and running.

Only an eventual successful full source-to-target row comparison establishes that this historical snapshot is imported. Website deployment, production cutover, other sports, and sustained independent collection remain separate tasks.
