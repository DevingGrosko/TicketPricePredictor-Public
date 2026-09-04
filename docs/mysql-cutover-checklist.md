# MySQL production cutover checklist

1. Create independent PythonAnywhere MySQL databases for MLB, NFL, and NHL.
2. Add destination-only `BASEBALL_MYSQL_URL`, `NFL_MYSQL_URL`, and `NHL_MYSQL_URL` values to the server-owned `.env`; do not commit credentials.
3. Pause the PythonAnywhere GitHub dispatcher so the SQLite source files stop changing.
4. Run `python tools/migrate_sqlite_to_mysql.py --sport all` from the project virtual environment.
5. Require successful source/destination row-count verification for every raw and materialized table.
6. Add the same URLs as `BASEBALL_DATABASE_URL`, `NFL_DATABASE_URL`, and `NHL_DATABASE_URL`.
7. Reload the web app and verify MLB, NFL, NHL, team-report, section-detail, and authenticated ingest routes.
8. Resume the dispatcher and confirm a new snapshot is written to MySQL and reflected in a report.
9. Keep the SQLite files unchanged as the rollback source for at least several days.
10. Roll back by removing the three active `*_DATABASE_URL` values and reloading the web app.
