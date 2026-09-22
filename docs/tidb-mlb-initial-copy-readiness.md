# MLB initial copy readiness after quota review

## Owner's usage screenshot

The screenshot supplied on September 21 shows the `ticketsignal` Starter instance as Active, current spend Free, row-based storage **451.01 MiB**, columnar storage **0 MiB**, and monthly request units **1.2 M**. These are observed dashboard values, not a new direct billing API query or a prediction of production usage.

Official Starter documentation checked during this step lists 5 GiB row storage and 50 million request units per month for an eligible free instance. The screenshot therefore represents about **8.81% of row storage** and **2.4% of monthly request units**, with approximately **4.56 GiB** and **48.8 million RUs** remaining at that displayed usage. The instance allowance is shared by its schemas. Keep Free/$0; do not enable paid scaling. Current headroom does not prove the full import footprint or future ongoing collection will fit.

References:
- https://docs.pingcap.com/tidbcloud/manage-serverless-spend-limit/
- https://docs.pingcap.com/tidbcloud/serverless-faqs/

## Direct MLB staging inventory

Read-only GitHub Actions run **35669990717**, job **106564084200**, commit **84fe3cbcd31d4688a4985ea5c1563bb6371f2c2c**, completed successfully at 2026-09-21T23:59 UTC. It checked the selected database and TiDB server, required the seven expected base tables, and queried their exact counts:

- analytics_dirty_venue: 0
- event: 0
- iterations: 0
- section_bucket_summary: 0
- section_summary_state: 0
- team_report_summary: 0
- tickets: 0

Thus MLB staging is still empty. The 451.01 MiB dashboard figure must not be interpreted as proof that MLB has already been imported. Inventory wrote no rows and did not query PythonAnywhere, NFL, or NHL.

Run: https://github.com/DevingGrosko/TicketPricePredictor-Public/actions/runs/35669990717

## Revised copy path

New file: `tools/tidb_mlb_initial_copy.py`.
Tested commit containing both required helpers: **ffc7d03d41ca940b4fa38baf796ad680b767005a**.

The new entrypoint uses the original helper only after verifying its exact SHA-256. It deliberately does not modify that original helper, whose fingerprint is also pinned by the successful NFL verifier.

This is an EMPTY-TARGET initial copy, not another resume run. It verifies the complete compressed and decoded source hashes and source DDL before connecting. Because the exact source bytes already passed the full prior uploaded-backup audit, it does not recreate the million-row SQLite fingerprint index on PythonAnywhere. It still type-checks values while streaming them, checks exact per-table copied counts, retains parameterized bounded INSERT batches, keeps foreign-key constraints enabled, and rejects server warnings.

All seven MLB destination tables must pass the complete existing schema check and be empty before any INSERT. A nonempty destination causes a stop; the script never deletes, overwrites, truncates, or upserts data. Connections close at table boundaries and reopen between committed batches after a local-processing gap of at least 15 seconds. Every new connection repeats target/schema checks and the required session settings. A failing INSERT or ambiguous COMMIT is never automatically replayed. Cleanup errors do not replace the original failure.

Progress is printed after committed batches, at 100,000-row thresholds or when at least 15 seconds have elapsed. This is not a background heartbeat during a blocked operation and is not a guaranteed runtime estimate.

Default invocation audits only; `--apply` copies only the fixed MLB snapshot to `ticketsignal_staging_mlb`. The script asks for the TiDB password once when not supplied in the process environment and caches it only within that process for connection renewal. No password or raw ticket rows are written to code, reports or public logs.

On successful copying it prints **COPY COMPLETE** and writes a report explicitly containing `copy_completed: true` and **`target_full_comparison_passed: false`**. A separate independent all-field read-only verification remains mandatory. Do not interpret COPY COMPLETE as full migration acceptance. If it stops, preserve the partial staging copy and report the error rather than rerunning or deleting anything.

## Actual tests and hashes

GitHub run **35670408636**, job **106565369108**, at commit **ffc7d03d41ca940b4fa38baf796ad680b767005a**, completed successfully. It passed **17 new orchestration tests** plus **36 unchanged original-helper tests** (including three disposable-MySQL driver tests), **53 tests total**, no skips. The new orchestration tests use synthetic mocks; this run had no staging environment or TiDB secrets. The original SQL batch helper had already passed the earlier bounded live TiDB smoke test. The new full MLB copy has NOT yet run on historical data.

The runner loaded the checksum-pinned original helper, checked the fixed MLB table sequence and total expected 6,062,765 source rows, and printed these SHA-256 values:

```text
30c595edbb0833db50956ffc4cd0303c7514d189fd1f52071ae3ef76158a4139  tidb_mlb_initial_copy.py
58b40b0ca5a23cc1dcdbc10fae79f7744727262fdf45c5bff62a5ee8168d21c6  tidb_mlb_nfl_import.py
```

Run: https://github.com/DevingGrosko/TicketPricePredictor-Public/actions/runs/35670408636

## Boundary and next action

This step added staging-only inventory/copy/test/workflow/document files. It did not modify the original importer, existing production application files, production workflows or settings, source databases, collector or scheduler. No MLB historical rows were written, no NFL or NHL records were altered, no replacement website was deployed, and nothing was merged to main.

The owner can run the two checksum-pinned helpers from a new isolated PythonAnywhere Bash directory, reusing the existing import virtual environment and reading the original `ticketsignal-export.o8YNx0rP/mlb.sql.gz`. Only TiDB MLB staging is the database target. Keep production collection running and all staging writers stopped. After receiving the copy result, perform independent read-only every-field verification on GitHub, inspect the updated quota dashboard, and only then proceed to application deployment and broader migration gates.

No background monitoring or future task was created. PythonAnywhere remains production until the owner approves a proven replacement.
