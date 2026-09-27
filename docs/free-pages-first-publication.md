# First public GitHub Pages deployment

Verified September 27, 2026. Public address: https://devinggrosko.github.io/TicketPricePredictor-Public/ . This is a successfully published preview, not an activated half-hour update service or a production cutover.

## Deployment and isolation

The user selected GitHub Actions as the Pages source. The scoped workflow preflight confirmed build_type=workflow and the public project URL. Repository artifact inventory was 226,621,727 bytes before this deployment; its conservative temporary-upload checks passed.

Only .github/workflows/free-ticket-site.yml was added to main, commit 4b08b70d16212bd1476ee41b43e096a861cc55ef. A comparison against 029f1cdb93ed6fd70f56f6ab6e0f32f173a6e22e confirms no other main-branch file changed. The workflow checks out reviewed implementation ad54b65c28bb0a54a3912542c658177042ce96a5 rather than merging the migration branch into production.

Deployment run 36330739705: ready job 108652036981, build job 108652093983, and deploy job 108652850249 all completed successfully. The original UI build and mounted Chrome tests passed. The bounded source cache was saved, the public-files-only artifact was uploaded, GitHub Pages deployment succeeded, and this run's temporary artifact was deleted afterward. No new capture or database-writing step ran. PythonAnywhere, its existing collection configuration, Render configuration, and billing settings were not changed.

## Actual public browser acceptance

Run 36331181597, job 108653279748, test commit 6ddd4d57bfe1349a89781855dc54b833f5353fb5 completed successfully. PUBLIC_PAGES_RESULT reported passed=true at 2026-09-27T15:53:17.149664+00:00. The test used the actual public GitHub Pages origin and project path, not a local server.

All three sports passed original homepage, hidden engineering-banner, mobile-width, report/section navigation, game/section form, chart value, and percentage-toggle checks. NFL/NHL map display/search/history links and MLB multi-game/buying-window flows passed. Eleven original CSS assets matched repository bytes. Requested published assets and content-addressed data passed SHA-256 checks.

Measured homepage/chart action timings were MLB 0.717/0.233 seconds, NFL 0.277/0.300 seconds, NHL 0.184/0.270 seconds. Sample charts had 6, 118, and 16 points respectively. These are single-run Selenium readiness measurements with browser HTTP caching disabled; CDN caches may have been warm. They are not Web Vitals, load tests, or universal guarantees.

The test audited 394 browser requests and reported zero application API requests, zero writes, and zero severe console errors. The bounded metadata reader fetched 38 files totaling 1,199,067 bytes. Allowed origins were the Pages project and the original Google Fonts hosts; PythonAnywhere and TiDB browser calls were blocked.

The first public-test attempt, run 36330927999, failed in the test HTTP reader because generated catalogs include both root-relative /native/... and relative data/... references. The checker was corrected to normalize only the optional leading slash while retaining the same fixed host, allowed-path checks, hash checks, and UI assertions. No site rebuild or application change was needed for that test correction.

## Data freshness and remaining activation

The public output includes the previously accepted post-export MLB capture: event 30313, 109 section series, captured through 2026-09-26T23:30:00+00:00. This does not fill the intervening historical gap or refresh every MLB game. NFL/NHL source maxima remain 2026-09-20T23:00:00+00:00. live_updates_enabled remains false.

The installed main-branch workflow has workflow_dispatch and a narrow workflow-file push trigger ONLY. No schedule was installed. The prepared 17,47-minute recurring capture/build/deploy template remains inactive under docs/free-ticket-site.workflow.yml on the feature branch.

Still required before unattended activation: verify the owner's TiDB Free/$0 spending setting and GitHub paid-usage blocking, test a complete independent all-sports capture/publication cycle, and verify cache restoration between separate executions and cleanup behavior. This turn confirms first public deployment and local-to-public presentation/data delivery, not those remaining gates. No paid plan or resource was created and no spending limit was increased; account-wide $0 billing protection was not verified.
