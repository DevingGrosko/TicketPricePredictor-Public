# TiDB schema rehearsal: verified results

This supersedes the not-yet-applied status in the earlier schema review. No historical data import or website deployment has occurred.

## Integrity of the received schema archive

The owner's uncompressed source-archive SHA-256 matches the 40,960-byte tar received in chat exactly:
`02a1616ba73f6a4b67100c28049f519273cac469a83119bc1c3c7cb4360a8560`.

All 12 regular members were inspected. The 19 source CREATE TABLE statements in `tools/tidb_staging_schema.sql` were independently compared with the uploaded archive and matched exactly. Fixture SHA-256: `989f549b31b4f626a2d2bd893267150f77e4253837d28bf2e0a8fd8cd8a6e605`.

The following compressed-file checksums were supplied by the owner from PythonAnywhere's export manifest. They are EXPECTED checksums for future transfer verification, not independent validation of row dumps that have not been received:

```text
06c22d54401a78e078d3aa5472815e070a48f4b135f8523400eec33830a1389f  mlb.sql.gz
98c4a65de897d290b2233c0c07ee4939b0b3c2888ec23b01c8d12fa4c73cd860  nfl.sql.gz
3e4a611b3021b32a06dc3c32c5ff87e799aec1eb290aac7bf63851b8fc1e2413  nhl.sql.gz
67007a309e97d9649286545a412335e963cd6217574fc3b8aff933ff9830dad4  schema-review.tar.gz
```

## Completed live test

GitHub Actions run **35547875358**, job **106176989791**, commit **c27b4dbe13040936e1348e490c3716667b2a832e**, completed successfully. The logs show **47 isolated offline tests passed**, followed by successful live checks for MLB (7 tables), NFL (6), and NHL (6). Run URL: https://github.com/DevingGrosko/TicketPricePredictor-Public/actions/runs/35547875358

The live checks created missing tables in the three fixed TiDB staging schemas, compared column names/types/nullability/auto-increment flags, index names/columns/ordering/uniqueness, table and VARCHAR collations, six foreign-key definitions, and declared auto-increment bases against the reviewed definitions. The only target DDL transformation was to make `DEFAULT CHARSET=utf8 COLLATE=utf8_general_ci` explicit instead of the source's implicit `utf8mb3` default.

Synthetic probes exercised microsecond datetime and JSON round trips, Unicode ticket-section text and integer prices, a representative floating-point summary value, all six parent-child foreign-key rejections, NFL/NHL duplicate capture slots and source identifiers, and case/accent/trailing-space uniqueness. Every probe transaction was rolled back, and every table was checked to be row-empty afterward. **All 19 empty staging tables now exist; zero historical ticket rows were imported.**

## Problems corrected during testing

Earlier runs stopped rather than relaxing checks or modifying existing tables. TiDB reported the index NON_UNIQUE flag as text, so the checker now normalizes that numeric representation while retaining exact uniqueness comparisons. The checker also initially treated information_schema.TABLES.AUTO_INCREMENT like MySQL's next-ID metadata. It now verifies the retained base from SHOW CREATE TABLE's table-options line; the event table already retained AUTO_INCREMENT=313. Four targeted tests were added for parsing that declaration. These were validation-tool differences, not observed loss of source indexes or imported IDs.

This verifies the declared auto-increment base, not the full application's generated-ID behavior under concurrency. The synthetic probes use explicit negative IDs. Actual generated IDs and application transaction behavior still require integration tests.

## Production boundary and remaining work

Only the staging branch was updated. A comparison after the successful run showed main still at `029f1cdb93ed6fd70f56f6ab6e0f32f173a6e22e`, with only added staging files relative to main. Existing PythonAnywhere application code, production workflow files, credentials, databases, dispatcher, and collector endpoints were not changed or accessed by the rehearsal. Source export files were read locally from the uploaded archive, not changed on PythonAnywhere.

This is a successful schema and bounded synthetic-data rehearsal, not a completed migration. No actual exported row set has been imported or reconciled. The full Flask application, concerts/local persistent state, sustained collection, independent scheduler, free-tier request-unit consumption, and replacement hosting remain to be validated. The original empty-SCHEMA connection check predates this step and intentionally does not accept schemas containing these tables; do not mistake that older check for the next import-validation gate.

Next: obtain a checksum-verifiable copy of the small NHL export and validate a bounded import path on that dataset before loading the larger NFL and MLB exports. Keep originals intact. Do not publish raw database dumps in this public Git repository. Do not retire PythonAnywhere or merge migration changes to main without the owner's explicit cutover approval.

Provider references used for metadata/constraint interpretation:
- https://docs.pingcap.com/tidbcloud/information-schema-tables/
- https://docs.pingcap.com/tidbcloud/sql-statement-show-table-next-rowid/
- https://docs.pingcap.com/tidbcloud/foreign-key/
