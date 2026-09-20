# TiDB staging setup: connection foundation

Status as of 2026-09-20: the TiDB instance has been created by the owner. An isolated connection helper and 15 offline tests have been added on `staging/tidb-free-hosting-2026-09-20`. All 15 tests passed locally. No live TiDB connection, import, application integration, or replacement deployment has been validated.

The existing PythonAnywhere database configuration, website, collector, scheduler, production branch, and workflow files have not been modified by this step. `Flask_App/tidb_staging.py` is not yet called by the Flask application. It is a connection foundation, not a finished migration.

## Owner step: create empty staging schemas

In the **TiDB Cloud console**, open the new instance's **SQL Editor**. Run this on TiDB, **not** in a PythonAnywhere console:

```sql
CREATE DATABASE IF NOT EXISTS ticketsignal_staging_mlb CHARACTER SET utf8mb4;
CREATE DATABASE IF NOT EXISTS ticketsignal_staging_nfl CHARACTER SET utf8mb4;
CREATE DATABASE IF NOT EXISTS ticketsignal_staging_nhl CHARACTER SET utf8mb4;

SELECT schema_name
FROM information_schema.schemata
WHERE schema_name IN (
    'ticketsignal_staging_mlb',
    'ticketsignal_staging_nfl',
    'ticketsignal_staging_nhl'
)
ORDER BY schema_name;
```

These commands only create empty schemas in the selected TiDB instance. They do not import history, create tables, delete data, or touch PythonAnywhere. Do not use `sys` as the application's database. The import step must explicitly preserve and validate source table character sets and collations rather than relying on schema defaults.

## Isolated environment settings

The helper reads only these settings from its process environment:

| Setting | Value |
| --- | --- |
| `TIDB_STAGING_HOST` | The host from the staging instance's Connect dialog, without scheme or port. |
| `TIDB_STAGING_USERNAME` | The complete prefixed database username from that dialog. |
| `TIDB_STAGING_PASSWORD` | The staging password, stored in deployment secrets. |
| `TIDB_STAGING_CA_FILE` | Optional path to a trusted CA bundle on the actual deployment OS. Leave unset to use that system's trusted roots. |

Port 4000 and the three staging schema names are fixed by the helper. TLS requires trusted-certificate and hostname verification, with TLS 1.2 or higher. Missing staging settings never fall back to production `MYSQL_*` values or a local `.env`. The helper does not create tables, run SQL, or connect merely on import.

No real credentials, account-specific usernames, or database exports are committed in these files. Do not put credentials in commands that print to shared logs. Replace any temporary shared credential before production use, and configure the application's appropriate database privileges separately from administrative access.

## Tests actually run

```sh
python -m unittest discover -s tests -p 'test_tidb_staging.py' -v
```

Result: **15 tests passed**. Coverage includes staging-only schema selection, production-host rejection, missing-setting rejection, TLS verification, invalid CA failures, credential-safe representations, URL encoding, and bounded lazy engine configuration. Engine creation is mocked; these are not live driver or TiDB integration tests. The entire pre-existing Flask test suite has not been run in this environment.

## Current execution limitation

A TCP reachability check from the assistant's runtime failed during DNS resolution before authentication or SQL execution. This is not evidence of an incorrect password, a broken TiDB instance, or an IP access-list problem. The available connector search returned no TiDB management connector. The next live connection test must run in an authorized network-enabled environment. Do not broaden TiDB network access merely to work around this runtime limitation.

## Remaining implementation gates

- Wire the isolated engine into staging-only application startup, retaining production behavior.
- Audit all persistent state, including concert data and local backup/audit/cache behavior, before using an ephemeral web host.
- Validate actual PyMySQL/TiDB connectivity, schema compatibility, and table engines/collations.
- Copy source history with a consistent read-only export, then validate it at a recorded source cutoff.
- Deploy only to a staging URL, using separate secrets; do not change production collector endpoints.
- Prove new ingestion, independent scheduling, restart recovery, page/API equivalence, quota usage, and cutover/rollback procedures as described in `tidb-staging-migration-plan.md`.

Do not merge to main, manually dispatch the existing production workflows, or retire PythonAnywhere during this setup.

## References

- TiDB SQL Editor: https://docs.pingcap.com/tidbcloud/explore-data-with-chat2query/
- TiDB CREATE DATABASE: https://docs.pingcap.com/tidbcloud/sql-statement-create-database/
- TiDB system schemas: https://docs.pingcap.com/tidbcloud/database-schema-concepts/
