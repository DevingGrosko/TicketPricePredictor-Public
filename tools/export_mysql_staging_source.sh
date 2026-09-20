#!/usr/bin/env bash
# Copy-only export for TicketSignal, run in the owner's PythonAnywhere Bash console.
# No connection to TiDB, repository edits, deployments, imports, or source writes.
# MySQL reference: https://dev.mysql.com/doc/refman/8.0/en/mysqldump.html
# Account credential file: https://help.pythonanywhere.com/pages/MySQLBackupRestore
set -Eeuo pipefail
umask 077

SOURCE_HOST='bunnyjeff.mysql.pythonanywhere-services.com'
SOURCE_USER='bunnyjeff'
CREDENTIAL_FILE="${HOME:?HOME must be set}/.my.cnf"

for tool in mysql mysqldump gzip sha256sum mktemp tar; do
    command -v "$tool" >/dev/null || { printf 'STOP: %s is not installed.\n' "$tool" >&2; exit 1; }
done
if [[ ! -r "$CREDENTIAL_FILE" ]]; then
    printf 'STOP: PythonAnywhere saved MySQL credentials (~/.my.cnf) are unavailable.\n' >&2
    printf 'Do not reset the production password or paste it into this script.\n' >&2
    exit 1
fi

# --defaults-file is first, so only the specified regular option file is read.
# Host, user, protocol, and database targets are explicit. No password is printed.
MYSQL=(mysql "--defaults-file=$CREDENTIAL_FILE" "--host=$SOURCE_HOST"
       "--user=$SOURCE_USER" --protocol=TCP --connect-timeout=15
       --batch --raw --skip-column-names)
DUMP=(mysqldump "--defaults-file=$CREDENTIAL_FILE" "--host=$SOURCE_HOST"
      "--user=$SOURCE_USER" --protocol=TCP
      --single-transaction --quick --no-tablespaces --set-gtid-purged=OFF
      --column-statistics=0 --hex-blob --default-character-set=utf8mb4
      --no-create-db --skip-add-drop-table --skip-add-locks --skip-disable-keys
      --skip-triggers --skip-routines --skip-events --skip-lock-tables)

OUT=$(mktemp -d "$HOME/ticketsignal-export.XXXXXXXX")
trap 'printf "Export stopped. Partial files, if any, are retained in %s; do not import them.\n" "$OUT" >&2' ERR
printf 'Export directory: %s\n' "$OUT"
printf 'Source reads only; do not run table/schema changes or schema-changing deployments during this export.\n'
"${MYSQL[@]}" --execute='SELECT VERSION();' > "$OUT/source-version.txt"
printf 'sport\tphase\tutc_time\n' > "$OUT/export-times.tsv"

# Check ALL source schemas before dumping any history. Do not silently omit
# database-side programs or non-transactional tables from the migration.
for sport in mlb nfl nhl; do
    db="bunnyjeff\$ticketsignal_${sport}"
    issues=$("${MYSQL[@]}" --execute="
        SELECT CONCAT('Non-InnoDB table or view: ', TABLE_NAME)
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = '$db'
          AND (TABLE_TYPE <> 'BASE TABLE' OR COALESCE(ENGINE, '') <> 'InnoDB')
        UNION ALL
        SELECT CONCAT('Trigger requires review: ', TRIGGER_NAME)
        FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = '$db'
        UNION ALL
        SELECT CONCAT('Routine requires review: ', ROUTINE_NAME)
        FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = '$db'
        UNION ALL
        SELECT CONCAT('Scheduled database event requires review: ', EVENT_NAME)
        FROM information_schema.EVENTS WHERE EVENT_SCHEMA = '$db';")
    if [[ -n "$issues" ]]; then
        printf 'STOP: %s needs migration review:\n%s\n' "$db" "$issues" >&2
        exit 1
    fi
    count=$("${MYSQL[@]}" --execute="SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = '$db' AND TABLE_TYPE = 'BASE TABLE';")
    if [[ ! "$count" =~ ^[0-9]+$ ]] || [[ "$count" -eq 0 ]]; then
        printf 'STOP: could not confirm nonempty source schema %s.\n' "$db" >&2
        exit 1
    fi
    printf '%s: %s InnoDB tables confirmed.\n' "$sport" "$count"
    "${MYSQL[@]}" --execute="SELECT TABLE_NAME, TABLE_TYPE, ENGINE, TABLE_COLLATION, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH FROM information_schema.TABLES WHERE TABLE_SCHEMA = '$db' ORDER BY TABLE_NAME;" > "$OUT/$sport.tables.tsv"
    "${MYSQL[@]}" --execute="SELECT TABLE_NAME, ORDINAL_POSITION, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, CHARACTER_SET_NAME, COLLATION_NAME, COLUMN_KEY, EXTRA FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = '$db' ORDER BY TABLE_NAME, ORDINAL_POSITION;" > "$OUT/$sport.columns.tsv"
done

for sport in mlb nfl nhl; do
    db="bunnyjeff\$ticketsignal_${sport}"
    printf 'Exporting %s ...\n' "$sport"
    # Small, separate schema copy for review; do not rewrite the full dump.
    "${DUMP[@]}" --no-data "$db" > "$OUT/$sport.schema.sql.partial"
    mv "$OUT/$sport.schema.sql.partial" "$OUT/$sport.schema.sql"
    printf '%s\tstart\t%s\n' "$sport" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$OUT/export-times.tsv"
    # Each full dump is one consistent snapshot for that sport, not one shared
    # snapshot across all three sports. pipefail prevents a failed dump from
    # being presented as a successful gzip. Compress as we go to conserve quota.
    "${DUMP[@]}" "$db" | gzip -6 -n > "$OUT/$sport.sql.gz.partial"
    gzip -t "$OUT/$sport.sql.gz.partial"
    mv "$OUT/$sport.sql.gz.partial" "$OUT/$sport.sql.gz"
    printf '%s\tfinish\t%s\n' "$sport" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$OUT/export-times.tsv"
    printf 'Finished %s.\n' "$sport"
done

cat > "$OUT/README.txt" <<'NOTES'
COPY-ONLY SOURCE EXPORT; NOT IMPORTED AND NOT A FINISHED MIGRATION.
mlb.sql.gz, nfl.sql.gz, nhl.sql.gz: unmodified mysqldump SQL, schema + all rows.
Each sport was dumped with a separate InnoDB consistent snapshot. Times bracket
execution; they are not exact snapshot timestamps or a shared global cutoff.
Do not change table definitions during the dump. Ongoing normal row writes
may continue; later writes will need a separate catch-up plan before cutover.
*.schema.sql: separate schema-only dumps for compatibility review.
*.tables.tsv: table name, type, engine, collation, ESTIMATED rows, data/index bytes.
*.columns.tsv: table, position, column, type, nullability, charset, collation, key, extra.
Preserve charset/collation in the originals. Review target-only adaptations.
This export covers only the three named MySQL schemas, not concert SQLite data,
audit files, application code, or all other persistent state in PythonAnywhere.
No credentials, .env, or .my.cnf are copied into this export directory.
Do not commit dumps to Git or import them into production. A source dump is not
proof of TiDB compatibility or final data equivalence. Inspect before importing.
NOTES
(
    cd "$OUT"
    # Only metadata and schema definitions; no ticket-price rows in this archive.
    tar -czf schema-review.tar.gz README.txt source-version.txt export-times.tsv \
        mlb.schema.sql nfl.schema.sql nhl.schema.sql \
        mlb.tables.tsv nfl.tables.tsv nhl.tables.tsv \
        mlb.columns.tsv nfl.columns.tsv nhl.columns.tsv
    sha256sum mlb.sql.gz nfl.sql.gz nhl.sql.gz schema-review.tar.gz > SHA256SUMS.txt
    sha256sum --check SHA256SUMS.txt
)
printf '\nEXPORT COMPLETE. No import or deployment was performed.\n'
printf 'Files: %s\n' "$OUT"
ls -lh "$OUT"/*.gz "$OUT/SHA256SUMS.txt"
