#!/usr/bin/env bash
# Run only in the owner's PythonAnywhere Bash console. Export and package only.
# No imports, TiDB access, deployments, schema changes, or source row writes.
(
  set -euo pipefail
  umask 077
  command -v python3 >/dev/null
  work=$(mktemp -d "$HOME/ticketsignal-backfill.XXXXXXXX")

  curl --fail --show-error --silent --location \
    'https://raw.githubusercontent.com/DevingGrosko/TicketPricePredictor-Public/0da75a9979cd4f3a9d72dd9d71a2f1d868944e99/tools/export_mysql_staging_source.sh' \
    -o "$work/export.sh"

  printf '%s  %s\n' \
    '9fb26c1b89d1f039245fffa136764c88c2c31c798e7ef55fc604d5a135339ef3' \
    "$work/export.sh" | sha256sum --check

  bash "$work/export.sh" | tee "$work/export.log"

  python3 - "$work/export.log" <<'PY'
from pathlib import Path
from zipfile import ZipFile, ZIP_STORED
import shutil
import sys

lines = Path(sys.argv[1]).read_text().splitlines()
folders = [line[len('Export directory: '):]
           for line in lines if line.startswith('Export directory: ')]
if len(folders) != 1 or 'EXPORT COMPLETE. No import or deployment was performed.' not in lines:
    raise SystemExit('STOP: export completion was not verified.')
folder = Path(folders[0])
names = ('mlb.sql.gz', 'nfl.sql.gz', 'nhl.sql.gz',
         'schema-review.tar.gz', 'SHA256SUMS.txt')
for name in names:
    if not (folder / name).is_file():
        raise SystemExit(f'STOP: missing {name}')
needed = sum((folder / name).stat().st_size for name in names)
if shutil.disk_usage(folder).free < needed + 64 * 1024**2:
    raise SystemExit(f'Not enough spare disk to create a ZIP. Exports are intact in {folder}.')
output = folder / 'ticketsignal-history-backfill.zip'
partial = folder / 'ticketsignal-history-backfill.zip.partial'
if output.exists():
    raise SystemExit('STOP: output already exists; it was not overwritten.')
with ZipFile(partial, 'x', compression=ZIP_STORED) as archive:
    for name in names:
        archive.write(folder / name, name)
partial.rename(output)
print(f'\nUPLOAD THIS FILE: {output}')
print(f'Size: {output.stat().st_size / 1024**2:.1f} MiB')
print('Export only. Neither database was changed; no import was performed.')
PY
)
