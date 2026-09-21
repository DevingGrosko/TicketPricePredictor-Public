# MLB/NFL export audit and streaming importer readiness

Status: actual uploaded backups audited; new importer unit/driver tests and bounded live TiDB synthetic tests passed. **No MLB or NFL historical rows were imported during this step.** This is not a deployment or cutover.

## Uploaded bytes and source integrity

The owner uploaded `mlb-nfl-transfer.zip` containing exactly `mlb.sql.gz`, `nfl.sql.gz`, `SHA256SUMS.txt`, and `nhl-import-result.json`. ZIP integrity checks succeeded. Both compressed backup hashes match the enclosed manifest and the source hashes previously pasted by the owner.

| Snapshot | Compressed bytes | Decoded SQL bytes | Compressed SHA-256 |
| --- | ---: | ---: | --- |
| NFL | 14,123,278 | 70,483,912 | 98c4a65de897d290b2233c0c07ee4939b0b3c2888ec23b01c8d12fa4c73cd860 |
| MLB | 46,256,829 | 283,171,520 | 06c22d54401a78e078d3aa5472815e070a48f4b135f8523400eec33830a1389f |

Decoded SQL SHA-256:
- NFL: `08e1e4eb3dea0bc0073c75ae290767ef6ac7bb313efe16297ac9d5b7a7a19dec`
- MLB: `f7621c8a692de8ea7736e1f00619917623ed076557f312f1aa76908372445fea`

All CREATE TABLE statements in the actual row dumps match the previously inspected, source-checksum-verified schema archive exactly. The importer additionally pins the ordered DDL fingerprint and dump completion marker. No blind replacement of text inside row payloads is performed.

## Exact exported row counts

These are counted from every literal INSERT row, not estimated MySQL metadata or auto-increment values. A separately implemented SQL string/parenthesis row-boundary scanner independently reproduced every count below.

| NFL table | Rows |
| --- | ---: |
| analytics_dirty_venue | 30 |
| nfl_event | 90 |
| nfl_iterations | 10,367 |
| nfl_tickets | 1,562,356 |
| section_bucket_summary | 56,466 |
| section_summary_state | 90 |
| **NFL total** | **1,629,399** |

| MLB table | Rows |
| --- | ---: |
| analytics_dirty_venue | 11 |
| event | 310 |
| iterations | 39,879 |
| section_bucket_summary | 286,418 |
| section_summary_state | 303 |
| team_report_summary | 10 |
| tickets | 5,735,834 |
| **MLB total** | **6,062,765** |

Every value was checked against its reviewed column type and nullability; all source primary keys were indexed and checked for duplicates, and declared foreign-key relationships were checked for orphan rows. Those checks passed. Canonical every-field fingerprints were computed for all rows. The offline audit created no remote connection.

The uploaded NHL JSON report states `target_full_comparison_passed: true`, `mode: resume`, and 172,874 inserted rows across six tables. Its source hashes, each table count, and each canonical fingerprint were independently recomputed from the earlier uploaded NHL export and matched. This validates the report against the source backup; it is not a new independent live read of NHL staging during this step.

## Importer implementation and resource use

New standalone helper: `tools/tidb_mlb_nfl_import.py`.
Pinned implementation commit: `9e277fc665e0b15a7076d009999c27a577de6a5e`.
Git blob: `f3048bfaea6c6fa06378a3ed464a9512ea8c9f93`.
SHA-256: `58b40b0ca5a23cc1dcdbc10fae79f7744727262fdf45c5bff62a5ee8168d21c6`.

The complete actual NFL and MLB backups passed its offline audit. Unlike the NHL-only helper, this helper streams the gzip and uses a fresh disposable SQLite index of keys/fingerprints to bound memory. Measured scratch files for these backups were 99,012,608 bytes for NFL and 379,166,720 bytes for MLB. Normal exit and handled failure clean up the temporary index. Allow extra account disk headroom for ongoing production files; a filesystem free-space check is not a guarantee of PythonAnywhere account quota availability.

Default mode is offline only. `--apply` requires an empty destination. `--resume` compares every existing destination row with this exact source snapshot before adding only missing rows. `--verify` never inserts. Both source hashes, source DDL, primary-key and foreign-key checks complete before a remote connection is attempted. The destination is restricted to the selected fixed MLB or NFL staging schema, port 4000, and a validated TiDB Cloud hostname with certificate- and hostname-verified TLS. Only `TIDB_STAGING_*` settings are read; production credentials, `.env`, and `.my.cnf` are not used.

Only literal values are decoded from the export; no supplied dump SQL, table creation, session directives, triggers, or other code is executed. The remote write operation is parameterized INSERT in bounded batches of at most 1,000 rows and an additional conservative payload bound. Each batch uses one SQL statement, followed by plain `SHOW WARNINGS` before commit; warnings fail the batch. Constraints remain enabled. Errors roll back the current batch; earlier committed batches may remain. Ambiguous commits are not blindly retried.

The helper never deletes, truncates, overwrites, upserts, or alters remote data. It checks the complete destination schema before writes and performs streamed every-field readback with exact completeness checks afterward. JSON is compared structurally with exact decimal-number normalization; FLOAT values use float32 storage bits. Normalized JSON formatting is not treated as data loss. All staging writers must remain stopped throughout this initial snapshot import.

## Actual tests completed

Workflow: `MLB NFL staging importer validation`.
Run **35553159199**, head commit `9b807cf43a82c6741a0ac9f5f4b08ed5625c6517`.
Run URL: https://github.com/DevingGrosko/TicketPricePredictor-Public/actions/runs/35553159199

- Job **106191578222**, `unit-and-mysql`: completed successfully with **36 tests passed**, no skips. This includes 33 unit tests and three integration tests against a disposable MySQL 8.0.46 service using synthetic data. Real bound multi-value inserts, streamed readback through the disk index, genuine conversion-warning rejection, and non-TiDB source-server rejection were exercised.
- Job **106191687378**, `tidb-rollback-smoke`: completed successfully on the actual TiDB staging instance. For NFL's six and MLB's seven tables, the test validated full schema preflight, inserted two synthetic rows per table inside one externally controlled transaction, compared every field using the actual importer comparison code, forced rollback, and confirmed all thirteen destination tables remained empty. The importer hash printed by this live runner matches the pinned SHA-256 above.

The synthetic test prevents the tested batch helper from committing by wrapping its transaction methods and rolling back the enclosing real transaction in a finally block. It never queries NHL staging. These live tests are bounded synthetic tests, not proof that either full historical import has finished or that production application performance is acceptable.

## Production boundary and next step

Before adding this record, comparison showed main still at `029f1cdb93ed6fd70f56f6ab6e0f32f173a6e22e` and only added staging-specific files relative to main. Existing application files, production workflows, collector endpoints, dispatcher, source credentials, source database, successful NHL importer, and NHL historical data were not changed by this step. Raw backups and credentials were not published in this public repository.

Next: run the pinned helper from a new isolated PythonAnywhere Bash working directory, reusing the already isolated import virtual environment, against the existing local **NFL** backup first. The only remote database connection must be to `ticketsignal_staging_nfl`. Record its full verification result and inspect TiDB's Overview / Usage this month before proceeding with the larger MLB snapshot. Do not enable paid scaling or change the $0/free limit as part of this experiment.

TiDB free capacity has both storage and operation limits: 5 GiB row storage and 50 million request units per month. SQL queries, imports and background work consume request units. A small compressed backup does not establish that the operation allowance is sufficient. Actual usage and imported footprint must be measured. Provider reference: https://docs.pingcap.com/tidbcloud/serverless-faqs/

No replacement website is deployed, no new ongoing collection is enabled, and no cutover or background monitoring is created by this record. Later source observations, full Flask compatibility, concerts/other persistent state, independent scheduling, sustained quotas, and rollback remain separate gates. Keep PythonAnywhere running until explicit owner approval of a proven replacement.
