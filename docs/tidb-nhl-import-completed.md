# NHL snapshot import: owner-reported completion

## Evidence and provenance

The owner pasted the full successful console output from the corrected importer, downloaded from commit `8bfdbfc83f48fc552daea051b3dd644b7729e6d6` and checksum-verified before execution. The working directory was `/home/bunnyjeff/ticketsignal-nhl-import.S6LrvnKj/retry.IOLN4Gzc`. The command used `--resume` against the existing source export and requested `nhl-import-result.json` in that working directory.

The output reports that both pinned source checksums passed, all six tables initially had zero matching rows, all expected rows were inserted, and every field of every target row matched the exported snapshot on final read-back. Final line: `PASS: NHL snapshot fully verified in TiDB staging. PythonAnywhere was not accessed.`

| Table | Rows reported inserted and verified |
| --- | ---: |
| analytics_dirty_venue | 35 |
| nhl_event | 203 |
| nhl_iterations | 1,936 |
| nhl_tickets | 143,181 |
| section_bucket_summary | 27,316 |
| section_summary_state | 203 |
| Total | 172,874 |

This completion record is grounded in the owner's console transcript. It is not a claim that the assistant independently reran a live full-data comparison in this step. The corresponding JSON report has not yet been received. The earlier locally audited source counts agree with this transcript.

## Scope

The completed operation is a copy of the pinned NHL export into `ticketsignal_staging_nhl`. The script ran in an isolated PythonAnywhere console environment, read an existing backup file, and connected only to TiDB; it did not read or modify the production MySQL databases. Its success does not establish that staging contains later observations collected after the original export.

Before adding this record, the repository comparison showed main still at `029f1cdb93ed6fd70f56f6ab6e0f32f173a6e22e`, and the staging branch only added migration-specific files relative to main. This step adds documentation only, not application code or executable workflow changes.

Do not rerun the empty-schema bootstrap, empty-table rehearsal, or empty-table warning probe against the now-populated NHL database. Do not truncate or delete the verified history. The importer has a read-only `--verify` mode if a new complete comparison is needed.

## Next gates

Receive checksum-verifiable NFL and MLB exports, preferably packaged together with the source `SHA256SUMS.txt` and the NHL verification report. Inspect the actual exports before adapting the importer; do not merely rename the NHL-only script's input because its schema and snapshot checks are intentionally specific.

NFL and MLB historical imports, the full Flask application, concerts and other persistent state, generated IDs under new ingestion, independent collection and scheduling, hosting behavior, quota usage, later-data catch-up, and rollback remain separate tasks. PythonAnywhere remains production; no cutover, production redeployment, cancellation, or background monitoring is authorized by this completion record.
