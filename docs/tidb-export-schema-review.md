# Review of the uploaded TicketSignal source schemas

Status: offline review of the owner's `schema-review.tar.gz` attachment is complete. No schema SQL, row import, deployment, or production change was executed during this review. Live application compatibility remains unproven.

## Source inspected

The archive contains README.txt, source-version.txt, export-times.tsv, and schema/table/column metadata files for MLB, NFL, and NHL. All twelve regular-file members were inspected. MySQL source version is 8.0.46; the dump client identifies itself as 8.0.40.

| Sport | Tables | Columns | Declared foreign keys | Source metadata data + index size (MiB) |
| --- | ---: | ---: | ---: | ---: |
| MLB | 7 | 41 | 2 | 390.89 |
| NFL | 6 | 46 | 2 | 46.11 |
| NHL | 6 | 52 | 2 | 12.72 |

The total is 19 tables, 139 columns, and 6 foreign keys. Every table has a primary key. Column types present are int, tinyint, varchar, datetime (including microseconds), float, and json. The source metadata size is approximately 449.72 MiB; it is not a prediction of TiDB physical storage. TABLE_ROWS values are estimates and must not be used as exact import-validation counts.

The source metadata reports InnoDB and utf8mb3_general_ci for every table. All text columns in the column metadata also use utf8mb3_general_ci. NFL/NHL capture-slot unique keys, event URL/source-ID unique keys, other indexes, all foreign keys, timestamps, JSON columns, and explicit AUTO_INCREMENT options need to be retained.

## Main adjustment required in the target definitions

The exported CREATE TABLE statements specify `DEFAULT CHARSET=utf8mb3` but omit an explicit COLLATE clause. Accepting a different destination default could change case/accent comparisons, grouping, and uniqueness behavior.

Prepared target definitions therefore use:

```sql
DEFAULT CHARSET=utf8 COLLATE=utf8_general_ci
```

This retains the three-byte charset using its alias and explicitly selects the corresponding general case-insensitive collation; it is not a conversion to utf8mb4 or a change to source data. PingCAP documents utf8 and utf8mb3 as aliases and documents utf8_general_ci support. The actual destination behavior, including case, accent, trailing-space, and uniqueness checks, must still be exercised on TiDB before accepting the migration.

Reference: https://docs.pingcap.com/tidbcloud/character-set-and-collation/

Nineteen target CREATE TABLE statements were prepared locally from the inspected schema files. For each statement, reversing the table-options replacement exactly reproduced the original CREATE TABLE statement. All six foreign keys were retained. This is an offline transformation check, not a successful TiDB schema-creation test. No data INSERT statements were supplied or processed.

Do not apply a blind global text replacement to full data dumps: a replacement in a string/JSON payload would alter data. Only the table-options clause of a validated CREATE TABLE statement should be adapted. Keep the original exports unchanged.

## Integrity status

The uploaded attachment is a readable, uncompressed tar stream of 40,960 bytes even though its filename ends in `.tar.gz`. Its SHA-256 is:

```text
02a1616ba73f6a4b67100c28049f519273cac469a83119bc1c3c7cb4360a8560
```

The source console reported a roughly 4 KiB compressed archive. Some download/upload step may have decompressed it; the cause has not been established. The source SHA256SUMS.txt file was not included in this attachment. Consequently, no comparison with the source checksum manifest has yet been performed. The uncompressed stream hash above can be compared with `gzip -dc schema-review.tar.gz | sha256sum` on the source; it must not be compared directly with a checksum for the compressed gzip bytes.

SHA-256 fingerprints of the inspected schema-only files:

- mlb.schema.sql: a688cbb4b924d8d5837f601f9db1c16f58807d15850e168b41b5618c7541bebb
- nfl.schema.sql: 853ce8916b41329e020e70a580885104e9223bc128cf4026f8d3ba823b96ecec
- nhl.schema.sql: 480b797ae81cd7348de9068610161fd0fe073328eee9930c526091ea261907e3

## Remaining gates

Obtain the source checksum manifest and compare the uncompressed schema-archive hash before relying on the transferred bytes for import. Validate a bounded target-only schema rehearsal and application/constraint behavior, then import only into the approved staging schemas from checksum-verified copies. Preserve row identifiers and source semantics; independently check exact imported counts and integrity against the exported rows. Do not use estimated TABLE_ROWS or AUTO_INCREMENT values as row counts.

The export log brackets the three independent snapshots between 2026-09-21T00:03:25Z and 2026-09-21T00:03:51Z. Those are execution brackets, not a single common snapshot timestamp. Production collection continued, so later observations need a catch-up and reconciliation plan before cutover.

This archive covers the three sports MySQL databases only. Concert SQLite data and other persistent application state are still separate migration tasks. Do not merge to main, change production endpoints, stop the dispatcher, or cancel PythonAnywhere as part of this review.
