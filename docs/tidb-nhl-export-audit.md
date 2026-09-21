# NHL backup audit and importer readiness

## Actual received bytes

The owner's `nhl-transfer.zip` was inspected locally. It contains exactly `nhl.sql.gz` (2,815,051 bytes) and `SHA256SUMS.txt` (318 bytes). ZIP integrity and gzip decompression succeeded. The embedded compressed-file SHA-256 matches both the enclosed manifest and the previously supplied source checksum:

`3e4a611b3021b32a06dc3c32c5ff87e799aec1eb290aac7bf63851b8fc1e2413`

The decoded SQL is 14,800,108 bytes; SHA-256:
`11a40edb7194328f187e4216c9cb8fd74065450353b0b7b3743f54b121a8225b`.

## Exact counts from exported INSERT values

| Table | Exported rows |
| --- | ---: |
| analytics_dirty_venue | 35 |
| nhl_event | 203 |
| nhl_iterations | 1,936 |
| nhl_tickets | 143,181 |
| section_bucket_summary | 27,316 |
| section_summary_state | 203 |
| Total | 172,874 |

These are parsed row counts, not information_schema estimates or guesses from AUTO_INCREMENT. A separate character-state row-boundary counter independently returned the same six counts. All 52 columns were type-checked. Primary-key duplicates and orphaned declared foreign keys were checked; none were found. Per-table canonical fingerprints were computed locally. No historical rows or raw exports were committed to GitHub.

## Importer and tests

`tools/tidb_nhl_import.py` is standalone and accepts only this exact compressed and decompressed snapshot. By default it performs only an offline audit. Its explicit `--apply` mode requires six existing, compatible, empty NHL staging tables; it does not execute source DDL or dump session directives. Data is decoded as literals and sent as parameterized INSERT batches of at most 250 rows with additional payload-size bounds. Foreign-key checks stay enabled. No .env, .my.cnf, source MySQL connection, application deployment, or other sports schema is used.

The target is fixed to `ticketsignal_staging_nhl`, port 4000, a validated TiDB Cloud hostname, and certificate/hostname-verified TLS. Host and username use staging-only environment settings. A missing password is requested privately from the console. It must be the TiDB staging password, not the source MySQL password.

Every column of every imported row is read back and checked. Integer/text values, datetime microseconds, FLOAT storage bits, and JSON structure/numeric values are compared. JSON formatting/key order is not treated as data loss. SQL/row values and credentials are not printed in errors.

A failed run can retain earlier committed staging batches. It does not truncate, delete, overwrite or automatically retry ambiguous commits. A separate `--resume` mode first proves every existing target row matches this exact export, then inserts only missing rows. `--verify` does not insert. Keep staging writers stopped until the initial snapshot is verified.

Actual verification performed:
- Offline audit of the entire uploaded NHL snapshot succeeded.
- 33 new unit tests passed locally.
- GitHub run 35549181927, job 106180631725, at commit e6d15a65d743f65ebb77d22831d0dd96509a0067 completed successfully with **37 tests passed**: those 33 unit tests plus 4 integration tests against a disposable MySQL 8.0.46 instance using synthetic data. Real PyMySQL escaping/binding, streamed read-back, JSON/FLOAT/DATETIME behavior and source-server rejection were exercised. No TiDB secrets or production resources were used by this CI workflow.
- Run: https://github.com/DevingGrosko/TicketPricePredictor-Public/actions/runs/35549181927

Tested importer SHA-256: `fde0a0d01545b486f4c14188b984d0573cf25afbc9b1f2c166f10df74135864c`.
Git blob SHA: `7b46530d705b90961806c690da27ad61c972ec56`.

## Current limit and next action

**No NHL historical rows have been imported into TiDB by this step.** The assistant runtime cannot resolve the TiDB hostname. GitHub Actions can reach TiDB using environment secrets, as established earlier, but the connector has no binary-upload-to-Actions capability for this chat attachment. Do not publish raw exports in the public repository to bridge that gap.

The owner can run the pinned importer from a new, isolated PythonAnywhere Bash working directory and virtual environment, reading the already saved `ticketsignal-export.o8YNx0rP/nhl.sql.gz`. This reads the local export file, not the production database; its only database connection is to TiDB staging. It consumes some console CPU and disk but does not change the production code, virtual environment, database, dispatcher, collector endpoints or deployment. The source backup remains unchanged.

The full Flask app, generated-ID behavior under new ingestion, other sports, concert state, independent scheduling, hosting and sustained free-tier capacity remain unvalidated. Do not switch production or remove PythonAnywhere based on these importer tests.
