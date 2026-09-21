# NFL snapshot independently verified after failed resume cleanup

## Result

GitHub Actions run **35669373393**, job **106562163182**, commit **1615338c3804253c2078a25afe6183e56da39b26**, completed successfully. All **13 credential-free verifier tests passed**. The live read-only verification ran from 2026-09-21T23:50:39.698884+00:00 to 2026-09-21T23:51:19.529337+00:00 (about 40 seconds), after the test phase.

Run: https://github.com/DevingGrosko/TicketPricePredictor-Public/actions/runs/35669373393

The live runner fetched all columns of all rows from the six NFL staging tables, canonicalized every value with the checksum-pinned original helper, rejected duplicate primary keys, sorted by encoded primary-key bytes, and compared each complete table fingerprint and exact count with independently recomputed source fingerprints.

| Table | Rows verified | Result |
| --- | ---: | --- |
| analytics_dirty_venue | 30 | Complete all-field fingerprint matched |
| nfl_event | 90 | Complete all-field fingerprint matched |
| nfl_iterations | 10,367 | Complete all-field fingerprint matched |
| nfl_tickets | 1,562,356 | Complete all-field fingerprint matched |
| section_bucket_summary | 56,466 | Complete all-field fingerprint matched |
| section_summary_state | 90 | Complete all-field fingerprint matched |
| Total | 1,629,399 | All six tables passed |

The actual run report states `mode: read-only`, `rows_written: 0`, and `target_full_comparison_passed: true`. Schema preflight also passed. This is a new, independent live data read, not merely acceptance of the owner's earlier console output and not just a COUNT(*) check.

## Source provenance

The expected manifest was regenerated locally from `nfl.sql.gz` inside the owner's previously uploaded `mlb-nfl-transfer.zip`. The gzip matched the embedded source manifest and the source checksum previously supplied by the owner. Both compressed and decompressed lengths and hashes, the exact DDL hash, source column types, primary-key uniqueness, exact table counts, and the dump completion marker were checked. No original export file was altered.

- Compressed NFL SHA-256: `98c4a65de897d290b2233c0c07ee4939b0b3c2888ec23b01c8d12fa4c73cd860`.
- Decoded NFL SQL SHA-256: `08e1e4eb3dea0bc0073c75ae290767ef6ac7bb313efe16297ac9d5b7a7a19dec`.
- Expected-manifest SHA-256: `05201ae0f497585e526fc2288e0d4bdff5d0f72f2a9ba5f53141786c6b7678af`.
- Original canonicalization-helper SHA-256: `58b40b0ca5a23cc1dcdbc10fae79f7744727262fdf45c5bff62a5ee8168d21c6`.

The small public manifest contains only schema definitions, counts and fingerprints, not ticket rows or credentials. Source and target use the original importer's exact canonical rules: text and integers remain exact, datetime microseconds are included, FLOAT uses float32 storage bits, and JSON uses structure plus exact decimal normalization rather than literal whitespace/key order.

## Failure interpretation and recovery

The owner reported all six `Existing matching` counts equaling source counts, followed by `0 new rows committed` for every table and `InterfaceError (database code 0)` during the old resume path. The pinned old importer unnecessarily rereads the entire source after that initial full comparison and keeps its database connection open while skipping existing rows. It then attempts a second target comparison. Its final rollback is not protected against a closed connection, so a cleanup exception can replace the original failure.

PyMySQL raises InterfaceError(0, '') when it tries to use a closed connection. An idle timeout during the local rescan is a plausible explanation, but the supplied message does not establish the exact disconnect cause or timeout. The original underlying exception was not available in the transcript. No unsupported claim of a specific timeout duration is made.

Provider source reference: https://github.com/PyMySQL/PyMySQL/blob/main/pymysql/connections.py

Recovery used the new `tools/tidb_nfl_readonly_verify.py`, not another resume/import. It opens a fresh connection for each bounded table read, closes the connection before CPU-heavy fingerprint computation, prints hashing progress, and prevents cleanup errors from masking an earlier read failure. A proxy permits only the SELECT/SHOW statements used by the pinned metadata and readback code. It never calls the import or write helpers. This verification is intended for a GitHub runner, where buffering the reviewed NFL table sizes is practical, not an arbitrary larger database or memory-constrained web service.

## Production boundary and remaining gates

Only new staging-specific verifier, manifest, tests, workflow and this documentation were added. The old importer and all existing application/workflow files were left unchanged. No production database, PythonAnywhere service, collector endpoint, scheduler, NHL database or MLB database was accessed by this verification. No database rows were inserted, deleted, updated, or overwritten. No migration change was merged to main and no replacement website was deployed.

This establishes that the saved NFL export is present and matches the readback window. It does not synchronize observations collected on production after the original export, prove the entire Flask application, or establish free-tier headroom. Staging writers must remain stopped while validating these initial snapshots.

Do not rerun the old NFL resume command or any empty-table rehearsal on the populated database. The connection/skip inefficiency in the old MLB/NFL importer is NOT changed by this recovery; review that path before providing another large import command. Check actual TiDB storage and request-unit usage before proceeding to MLB. Keep the free/$0 setting and keep PythonAnywhere in service until an independent replacement is proven and the owner approves cutover.
