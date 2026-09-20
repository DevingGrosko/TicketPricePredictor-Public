# TicketSignal: isolated TiDB staging migration

Status: planning and repository safety review only. No replacement service has been deployed or tested against TiDB. No application code, existing workflow, production setting, production data, or collector schedule is changed by this document.

## Non-negotiable boundary

Keep the working PythonAnywhere website, MySQL databases, dispatcher, and collection pipeline running until an independent replacement is proven and the owner explicitly approves cutover. Do not merge migration work to main, run production deployment or migration workflows, redirect ingestion, change production secrets, pause the dispatcher, delete backups, or cancel PythonAnywhere as part of staging setup.

Work branch: `staging/tidb-free-hosting-2026-09-20`.

The existing `.github/workflows/deploy-pythonanywhere.yml` deploys selected main-branch pushes. It also permits manual dispatch, and its deployment job only excludes pull requests. Therefore **do not manually dispatch the existing production workflow from any branch**, including this staging branch. New staging tooling must have its own explicit target checks and must not consume production deployment credentials.

## Target under evaluation

- Database: one TiDB Cloud Starter Free instance, with separate staging schemas for MLB, NFL, and NHL. Preserve historical records, identifiers, timestamps, constraints, and analytics semantics. TiDB is MySQL-compatible, but application compatibility and performance remain unproven.
- Website: keep Flask and the existing frontend initially, rather than combining the database migration with a static-site rewrite. Render Free is a candidate, not a promise of equivalent performance or reliability. The owner must accept its cold-start tradeoff before choosing it as the eventual public host.
- Collection: retain the working production path initially. Test ingestion on the staging database only, using copied, non-sensitive fixtures or a separately approved read-only data feed. Do not double the production Selenium workload by default.
- Scheduling: design and test the replacement scheduler separately before PythonAnywhere can be retired. A staging website fed by PythonAnywhere is not yet an independent replacement.

## Account-side prerequisites

1. The owner creates or selects a TiDB Cloud Starter **Free** instance. Do not select a paid/scalable instance or add a paid budget for this experiment. Record the non-secret region and connection endpoint. Use separate staging credentials and certificate-verified TLS; do not reuse production credentials.
2. Once the web-host tradeoff is accepted, the owner creates a free web service bound only to this staging branch and a separate temporary URL. Never point it at the production database. Restrict the hosting provider's repository authorization to the required repository where supported.
3. Store staging credentials only in the hosting dashboard and an isolated GitHub staging environment where needed. Never paste passwords, private keys, connection strings containing credentials, `.env`, or database exports into public issues, repository files, Actions logs, or chat.
4. Obtain a consistent, read-only export of the application databases through an owner-controlled PythonAnywhere console or an explicitly authorized read-only connection. The repository deployment workflow describes a restricted SSH key, not general-purpose database-export access. Do not repurpose that key or assume general shell access.

Only non-secret setup confirmations and the staging URL need to be shared in chat.

## Implementation sequence

### 1. Isolate configuration

Review all repository workflows before adding executable staging automation. Guard staging tools against production hosts and schema names; never silently fall back to production when a staging setting is missing. Add an explicit port and certificate-verified TLS configuration for the MySQL driver, with regression tests preserving existing PythonAnywhere behavior. Check SQL dialect differences and test transactions, auto-increment IDs, foreign keys, indexes, timestamps, collations, and analytics queries.

The current database configuration uses SQLAlchemy/PyMySQL with host and schema settings but no configurable port or explicit TLS arguments. This requires review rather than simply replacing the hostname.

### 2. Account for all persistent state

The owner's size query reports approximately 449.72 MiB for the three MySQL schemas together; this is an estimate from MySQL table/index metadata, not a measured TiDB footprint. Measure actual imported storage and growth on TiDB.

Audit concert storage, audit files, backup directories, generated analytics, lock files, and caches. Do not assume the three sports schemas are the whole application. Durable data must not depend on an ephemeral web-service filesystem. Disposable caches must be safe to rebuild. Keep credentials and exports out of Git.

### 3. Copy, never move, history

Verify table engines and an appropriate consistent-snapshot export method before execution. Export only application data, not MySQL user accounts or system schemas. Avoid destructive commands and table-locking exports; account for read load on production. Import into fresh staging schemas only. Record a per-source snapshot cutoff for comparisons while the live service continues to receive new observations.

Validate schema, row counts at the recorded cutoff, primary/foreign-key relationships, captured timestamps, representative values, and aggregates. Byte-for-byte storage size is not expected to match across database engines. Inspect any existing SQLite-only data separately.

### 4. Deploy an isolated website

Deploy the current Flask frontend against staging storage, with separate authentication secrets, clear staging identification, and no production URLs for writes. Add startup and database health checks. Review local-file behavior before deploying to Render Free or another ephemeral host. Test every supported page and API, not just the homepage.

### 5. Prove ingestion and resilience

Start with bounded staging-only ingestion tests. Check duplicate replay handling, partial failures, database reconnects, authentication, request timeouts, cold starts, and restarts. Any later read-only synchronization must have a bounded production load and staging failures must not affect production collection. Reconcile updates received after the initial export; a single old snapshot is insufficient for final cutover.

Measure database request-unit consumption during imports, scheduled analytics, collection, and realistic browsing. The free storage allowance alone does not establish that the workload is free. Test actual import footprint rather than treating the source's approximately 450 MiB as an exact quota prediction.

### 6. Acceptance and cutover gate

Proposed observation period: at least seven consecutive days covering representative game-day load, including independent scheduled collection once its separate test is approved. Record completeness of expected captures, retries and missing intervals, page/API equivalence on the same data cutoff, warm and cold response times, restart recovery, storage growth, and daily request-unit use. Forecast normal monthly usage rather than extrapolating only from a quiet test day.

Before requesting cutover approval, demonstrate that the replacement does not depend on PythonAnywhere for scheduling, ingestion, storage, serving, or backups. Define final catch-up and verification steps, a recoverable backup, and a rollback method that accounts for observations collected after cutover. Do not treat an old PythonAnywhere copy as automatically current after cutover.

The owner must approve the final switch. Keep PythonAnywhere available during the agreed rollback window. Canceling or deleting it is a separate owner decision.

## Provider references checked on 2026-09-20

- TiDB Cloud Starter pricing details: https://www.pingcap.com/tidb-cloud-starter-pricing-details/
  - An eligible Free instance includes 5 GiB row storage and 50 million request units per month; exhausting quota throttles a Free instance. This is not unlimited database compute. The optional Scalable mode has different budget requirements and is outside the initial free-only plan.
- Render Free limitations: https://render.com/docs/free
  - Free web services sleep after 15 minutes without inbound traffic; waking takes about one minute. Local filesystem changes are lost on restart/redeploy/spin-down. Free services have resource and usage limits, and Render recommends against their use for production applications. These tradeoffs must be evaluated, not hidden.

No background monitoring or scheduled migration work is created by this plan.
