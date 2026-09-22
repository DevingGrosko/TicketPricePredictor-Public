# MLB historical snapshot independently verified

## Completed live verification

The owner reported `COPY COMPLETE` from the separate MLB initial-copy tool. A new read-only verification subsequently completed on the actual TiDB staging database, rather than treating that copy message as acceptance.

GitHub Actions run **35675400758**, job **106580766738**, commit **fd1453ae515edba2e47216790a10e28ffc1bc62b**, completed successfully. Its 15 credential-free verifier tests all passed. The live comparison ran from **2026-09-22T01:20:48.174719+00:00** to **2026-09-22T01:23:03.078940+00:00**, approximately 135 seconds.

Run: https://github.com/DevingGrosko/TicketPricePredictor-Public/actions/runs/35675400758

| MLB table | Exact rows | Result |
| --- | ---: | --- |
| analytics_dirty_venue | 11 | All-field fingerprint matched |
| event | 310 | All-field fingerprint matched |
| iterations | 39,879 | All-field fingerprint matched |
| section_bucket_summary | 286,418 | All-field fingerprint matched |
| section_summary_state | 303 | All-field fingerprint matched |
| team_report_summary | 10 | All-field fingerprint matched |
| tickets | 5,735,834 | All-field fingerprint matched |
| **Total** | **6,062,765** | **All seven tables passed** |

The actual live report states `mode: read-only`, `rows_written: 0`, and `target_full_comparison_passed: true`. It also passed the full existing schema preflight. The verifier reads every column of every row, rejects missing/extra counts and duplicate primary keys, and compares each complete canonical table fingerprint. This is not merely a row-count or sample check.

## Source provenance and verification method

The expected manifest was recomputed locally from `mlb.sql.gz` in the owner's previously uploaded `mlb-nfl-transfer.zip`. The ZIP member CRC read succeeded. The source gzip matched both its pinned checksum and the enclosed source manifest. The complete decoded stream matched its exact byte count, checksum and dump completion marker. The ordered table definitions matched their previously reviewed DDL fingerprint. Every source row was decoded as literals, type-checked and included in an all-field fingerprint, with duplicate source primary keys rejected. The recomputation used a separate local implementation of the original canonical format; live target canonicalization used the original checksum-pinned importer helper.

- Compressed source: 46,256,829 bytes; SHA-256 `06c22d54401a78e078d3aa5472815e070a48f4b135f8523400eec33830a1389f`.
- Decoded SQL: 283,171,520 bytes; SHA-256 `f7621c8a692de8ea7736e1f00619917623ed076557f312f1aa76908372445fea`.
- Ordered DDL SHA-256: `fffc0abaa69caecffcfc32c68644b5da4c2e339c12e05dba96a4a06c35abaad3`.
- Expected manifest SHA-256: `bded56fcf0509feb5cd2a292c3a2908ed77d248916f33f3df2c7a4a92e49ef27`.
- Original canonicalization helper SHA-256: `58b40b0ca5a23cc1dcdbc10fae79f7744727262fdf45c5bff62a5ee8168d21c6`.

Canonical rules preserve exact integer/text values and datetime microseconds, compare FLOAT by float32 storage bits, and compare JSON structure with exact decimal-number normalization. JSON whitespace/key order is not treated as data loss. Fingerprints are combined in encoded-primary-key byte order, consistent with the original source-index method.

The new verifier buffers only one reviewed table at a time on a standard public GitHub runner, with a limit of the expected row count plus one to detect growth. Each database connection closes before CPU-heavy hashing. No SQL from the dump is executed. Its connection proxy permits only the static SELECT/SHOW operations used by metadata/readback checks and rejects writes, multiple statements, output-file and locking expressions. Cleanup cannot hide the original read error. This is a one-time bounded verification, not a general large-database streaming service.

## Three sports snapshots: verification status

| Sport | Tables | Snapshot rows | Evidence |
| --- | ---: | ---: | --- |
| NHL | 6 | 172,874 | Earlier successful owner-run import/readback; received JSON report reconciled against the uploaded source; see `tidb-nhl-import-completed.md` and `tidb-mlb-nfl-import-readiness.md` |
| NFL | 6 | 1,629,399 | Earlier independent live read-only run 35669373393; see `tidb-nfl-import-verified.md` |
| MLB | 7 | 6,062,765 | Independent live read-only run above |
| **Total** | **19** | **7,865,038** | Saved sports snapshots verified in their respective readback windows |

NHL and NFL were not re-read during this MLB-only operation. These results cover the exported snapshots, not later observations collected by production. They do not establish a single atomic snapshot across all sports or ongoing synchronization. Keep staging writers stopped while these initial snapshots remain the comparison baseline.

## Production boundary and next gates

Only new MLB-specific manifest, read-only verifier, tests, workflow and this record were added to `staging/tidb-free-hosting-2026-09-20`. After the successful run, repository comparison still showed main at `029f1cdb93ed6fd70f56f6ab6e0f32f173a6e22e` and only added staging files relative to main. No original importer, production app/configuration, existing workflow, source credentials or backup was altered. This operation did not connect to PythonAnywhere, NFL or NHL. It performed no database inserts, updates, deletes, truncation, imports or synthetic writes. Nothing was merged to main and no website was deployed.

No further initial import is needed for these three saved snapshots. Do not rerun any empty-target copy or schema rehearsal against these now-populated schemas.

Next: obtain the updated TiDB storage/request-unit usage after MLB, retaining Free/$0; then integrate and test the Flask application against isolated staging storage and a separate temporary website URL. The application database configuration currently still lacks the integrated TiDB staging port/TLS selection, and concert/local persistent state needs its own audit. Generated IDs and transactions under new ingestion, all pages/APIs, independent collection/scheduling, later-data catch-up, sustained quotas and rollback remain unproven. PythonAnywhere remains the production target until the owner explicitly approves a tested replacement.

No recurring monitoring or scheduled future task was created by this step.
