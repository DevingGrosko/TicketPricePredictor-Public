# TiDB staging validation — 2026-09-20

This record supersedes the earlier connection-not-yet-tested status in `tidb-staging-setup.md`. It records actual GitHub Actions results, not a production-readiness claim.

## Observed results

1. The first connection test ran all 25 isolated staging tests successfully, but TiDB returned unknown-database errors for each expected schema. The subsequent metadata-only diagnostic authenticated with certificate-verified TLS and found no schema names beginning with `ticketsignal` on the connected instance. No credentials were printed.
2. The staging bootstrap workflow created only the three missing empty schemas: `ticketsignal_staging_mlb`, `ticketsignal_staging_nfl`, and `ticketsignal_staging_nhl`. It first checked the TiDB server identity and refused targets containing tables or views. It used only fixed staging names and `CREATE DATABASE IF NOT EXISTS`, with no drops, imports or account changes.
3. Bootstrap run **35545394582**, job **106170258064**, completed successfully. Its logs show **25 isolated tests passed** and **all three staging schemas reachable and empty** using the staging connection helper.
4. The read-only connection-check workflow was then rerun independently. Run **35545295896**, latest job **106170341212**, completed successfully: offline tests and live connectivity/empty-schema check both passed. The failure-only diagnostic step was skipped.

Run records:
- https://github.com/DevingGrosko/TicketPricePredictor-Public/actions/runs/35545394582
- https://github.com/DevingGrosko/TicketPricePredictor-Public/actions/runs/35545295896

## Scope and limits

All repository additions remain on `staging/tidb-free-hosting-2026-09-20`. Before adding this record, comparison against main showed only eight added staging code, test, workflow and documentation files, with no changes to existing files; main remained at `029f1cdb93ed6fd70f56f6ab6e0f32f173a6e22e`.

The existing PythonAnywhere website, production MySQL databases, collector endpoints, dispatcher, application configuration and production workflow files were not changed by these operations. The only database writes were creation of the three empty TiDB staging schemas. No historical data has been copied, no application tables have been created, and no replacement website has been deployed.

These results prove the tested GitHub runners can authenticate to the TiDB instance and access the intended empty schemas. They do not establish application SQL compatibility, ingestion reliability, full application test success, production performance, free-tier capacity, migration completeness, or independent scheduling.

## Next prerequisite

Inspect source MySQL version, storage engines, and collations using read-only metadata queries in PythonAnywhere before selecting a consistent export method. Do not reset source credentials, stop collection, switch production branches, or run old migration/deploy workflows for this step.

The next source-side query is:

```sql
SELECT VERSION() AS mysql_version;

SELECT table_schema AS database_name,
       engine,
       table_collation,
       COUNT(*) AS table_count
FROM information_schema.tables
WHERE table_schema IN (
    'bunnyjeff$ticketsignal_mlb',
    'bunnyjeff$ticketsignal_nfl',
    'bunnyjeff$ticketsignal_nhl'
)
  AND table_type = 'BASE TABLE'
GROUP BY table_schema, engine, table_collation
ORDER BY table_schema, engine, table_collation;
```
