# Flask snapshot preview: implementation and measured validation

Status: the isolated Flask preview has passed bounded application checks against the actual TiDB sports snapshots. **No Render service has been created or deployed by this step. This is not a production cutover or full feature/performance acceptance.** PythonAnywhere remains the production website, storage and collection path.

## Implemented on the staging branch only

Branch: `staging/tidb-free-hosting-2026-09-20`.

- `Flask_App/database_config.py` adds explicit opt-in routing when `TICKETSIGNAL_STAGING_SITE=1`. Normal MySQL and explicit SQLite test routing remain separate. The staging path ignores production MySQL settings rather than using them as a fallback, rejects mixed settings and SQLite overrides, and cannot rewrite the backend setting.
- `Flask_App/staging_site_config.py` uses the fixed MLB/NFL/NHL staging schemas, port 4000, certificate- and hostname-verified TLS, and bounded per-sport connection pools. It validates the actual selected database, TiDB server and foreign-key setting on connection creation. SQLAlchemy execution hooks permit only the preview's SELECT/SHOW/DESCRIBE reads and reject writes and recognized side-effect expressions before execution.
- `Flask_App/staging_site.py` reuses the original Flask application under an isolated factory. It requires a separate web secret and a clean checkout without a discoverable `.env`. It blocks write HTTP methods, blocks concert routes, labels HTML as a saved-data staging preview, adds noindex headers, and provides `/healthz` (no DB query) and `/readyz` (three connection checks). These are application-level safeguards, not a claim that the supplied database account has server-enforced SELECT-only privileges; a dedicated read-only DB user remains preferable for a public preview.
- `Flask_App/staging_wsgi.py` is the separate web-server entry point. The production entry point is not replaced.
- `requirements-staging.txt`, `.python-version` and `render-staging.yaml` prepare one independent free Render web service in Virginia, using Python 3.13, one Gunicorn worker, no additional database/disk/worker/cron, and the staging branch. Code auto-deploy is off. Render Blueprint Auto Sync is a separate dashboard setting and should also be set to No for controlled changes.

No credentials, raw snapshots or database exports were added to the repository. The checksum-pinned original import helpers and verification manifests were not changed.

## Actual completed tests

Workflow: **TiDB read-only Flask preview validation**.

### Regression and boundary coverage

Run **35800765974**, commit `c3c130c2771bc2b141fd1b4bbda306a26e89f346`, offline job **106990450877**, completed successfully. Its targeted phase passed **23 preview tests**. Its full test discovery ran **432 tests**, with **425 passing and 7 skipped** (the separate disposable-MySQL driver tests did not have their test service enabled in this workflow). The 23 preview tests are part of that full discovery; do not add them again to claim a larger unique test count.

Coverage includes unchanged normal MySQL/SQLite selection, staging-only settings and schema names, rejected production/local settings, no `.env` fallback, TLS configuration, lazy bounded engines, target checks, actual SQLAlchemy write interception on a disposable SQLite connection, blocked preview write methods/concert routes, visible staging labels, health checks, and error masking.

The later run **35801250716**, commit `c86c9ca3e07608c18f7a86e4c61307bfaa442fad`, also completed both offline phases successfully. That commit adds request deadlines and safe diagnostic types/locations to the smoke test; it does not change application behavior.

### Live application coverage

Run **35801250716**, live job **106992019204**, completed successfully at 2026-09-23T00:17 UTC. It exercised the real Flask test client against the actual TiDB staging databases, with database reads guarded and no production credentials.

| Request | Result | Measured seconds in this run |
| --- | --- | ---: |
| `/healthz` | 200, process health | 0.002 |
| `/readyz` | 200, all three database connections | 4.408 |
| `/` | 200, imported MLB games present | 0.913 |
| `/baseball` | 200, cached MLB page | 0.001 |
| `/nfl` | 200 | 0.856 |
| `/nhl` | 200 | 0.800 |
| `/api/baseball/options` | 200, selectable game sections | 0.429 |
| `/graph`, selected single game/section | 200, nonempty chart X and Y data | 16.025 |
| `/concerts` | 503, deliberately not migrated | 0.000 |

POSTs to the three sport snapshot ingestion endpoints and analytics backfill were blocked with 409 before their handlers. The report recorded zero blocked SQL attempts by the permitted pages, 139 completed SQL reads after readiness, and peak process RSS of 123,788 KiB in that GitHub runner. No SQL writes, schema migrations, Selenium collection, price ingestion or summary refresh were run by these live checks. This was an in-process Flask test, not a Render deployment or browser-rendered JavaScript test.

Run URL: https://github.com/DevingGrosko/TicketPricePredictor-Public/actions/runs/35801250716

## Issues found and limitations that remain

An initial banner syntax error was caught before any live application test and corrected. A later graph request failed after the 20-second pool wait using the single-connection import-style pool. The existing GraphBuilder opens nested independent ORM sessions, so the website preview now allows three connections per sport while retaining one web worker. The subsequent graph tests returned populated results; the normal production pool was not changed.

**Performance is not accepted yet.** The latest representative graph took about 16 seconds, and an earlier successful run (35800765974, live job 106990668990) took approximately 92 seconds on the initial MLB and NHL landing requests and 18 seconds for the graph. The later run's landing pages were below one second, but that does not establish that the earlier delays are fixed. Their exact cause was not proved. These GitHub timings are not Render benchmarks or a promise of comparable performance to PythonAnywhere. The smoke test now has a 120-second per-request budget and safe failure diagnostics. Query round trips, connection behavior, cold starts and actual host resource limits require further testing before cutover.

Only representative landing pages and one populated MLB graph were exercised against live TiDB. NFL/NHL graph/map/arena/stadium views, multi-game comparisons, prediction paths, materialized reports and stale-summary behavior, browser interactions, concurrent traffic and restart recovery are not all validated by this run. A read path that needs to refresh a materialized summary will be blocked in this deliberately read-only preview and may need separate handling.

Concerts use separate local persistent state and are not included in these sports snapshots. They remain explicitly disabled in this preview, not silently replaced with an empty local database. Staging ingestion, independent scheduling, collection completeness, post-export catch-up, ongoing request-unit usage, backups and rollback remain separate gates.

## Owner-side deployment setup

The available connected tools do not provide Render account deployment access. The owner can create a new preview through Render's dashboard:

1. Sign in, choose **New > Blueprint**, and connect only the required repository where the provider permits repository-level selection.
2. Select `DevingGrosko/TicketPricePredictor-Public`, branch `staging/tidb-free-hosting-2026-09-20`, and Blueprint Path `render-staging.yaml`.
3. Review that the plan contains **one new Free web service**, named `ticketsignal-staging-preview`, in Virginia. It must not update an existing production service or add a paid resource.
4. Supply `TIDB_STAGING_HOST`, `TIDB_STAGING_USERNAME`, and `TIDB_STAGING_PASSWORD` directly in Render's secret prompts. GitHub environment secrets do not transfer automatically. Use staging credentials only, not PythonAnywhere/MySQL credentials; do not upload `.env`. The Blueprint generates a separate `FLASK_SECRET_KEY`.
5. After reviewing the configuration, deploy the preview and share its separate `onrender.com` URL and any relevant build/runtime error. Set Blueprint Settings > Auto Sync to No so infrastructure changes remain manual as well. Do not change production domains, endpoints, secrets, collector schedules or PythonAnywhere settings.

The YAML follows the current official Blueprint specification but has not yet been validated by the Render account's create/deploy flow. A first deployment may uncover provider-specific configuration or network-access issues.

Render Free sleeps after 15 minutes without incoming traffic and has an ephemeral filesystem and constrained compute. Treat this as a candidate preview host, not a proven production replacement. Do not enable paid scaling or add paid infrastructure without separate owner approval.

Provider references checked:
- https://render.com/docs/infrastructure-as-code
- https://render.com/docs/blueprint-spec
- https://render.com/docs/free
- https://render.com/docs/python-version
- https://docs.pingcap.com/developer/dev-guide-sample-application-python-sqlalchemy/

## Production boundary

After the successful latest run, a comparison still showed main at `029f1cdb93ed6fd70f56f6ab6e0f32f173a6e22e`. The sole modified pre-existing source file on the work branch is `Flask_App/database_config.py`; the other additions are migration/preview-specific. Existing production workflow files, application routes, source databases, dispatcher and collector endpoints were not changed on main or deployed to PythonAnywhere. No merge or cutover occurred. Keep PythonAnywhere running until the independent replacement satisfies the remaining gates and the owner explicitly approves the switch.

No scheduled background monitoring or future automation was created by this step.
