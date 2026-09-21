"""Exercise the real MLB/NFL importer on synthetic TiDB rows, then roll back.

Only empty MLB/NFL staging tables are allowed. NHL and production are not queried.
No source exports are required, and no commits of application rows are permitted.
"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import tempfile
from tools import tidb_mlb_nfl_import as imp

FIXTURE_SHA = '989f549b31b4f626a2d2bd893267150f77e4253837d28bf2e0a8fd8cd8a6e605'


class RollbackOnly:
    def __init__(self, c): self.c = c
    def cursor(self, *a, **kw): return self.c.cursor(*a, **kw)
    def begin(self): pass
    def commit(self): pass
    def rollback(self): pass


def empty(c, tables):
    for t in tables:
        imp.require(not imp.query(c, f'SELECT 1 FROM `{t.name}` LIMIT 1'), 'Target has data; synthetic smoke test refused.')
    c.rollback()


def synthetic_row(t, i, sport):
    values = []
    for name, kind, null, _, _ in t.columns:
        if null == 'YES': value = None
        elif kind in ('int', 'tinyint(1)'): value = 1
        elif kind == 'float': value = 137.5
        elif kind.startswith('datetime'): value = datetime(2026, 9, 20, 12, 34, 56, 0 if kind == 'datetime' else 123456)
        elif kind == 'json': value = json.dumps({'probe':["é'\\", True, None, 1.25]})
        else: value = 'staging_probe_' + str(i)
        if name in ('id','event_id','iteration_id'): value = -900000-i
        if name == 'sport': value = sport
        if name in ('source_url','URL'): value = 'https://example.invalid/staging/' + str(i)
        values.append(value)
    return imp.typed_row(t, tuple(values))


def main():
    raw = Path('tools/tidb_staging_schema.sql').read_bytes()
    imp.require(hashlib.sha256(raw).hexdigest() == FIXTURE_SHA, 'Schema fixture checksum mismatch.')
    sections = re.split(r'^-- sport: (mlb|nfl|nhl)\n',raw.decode(),flags=re.M)
    source = dict(zip(sections[1::2], sections[2::2]))
    for sport in ('nfl','mlb'):
        tables = [imp.parse_table(m[0]) for m in imp.CREATE.finditer(source[sport])]
        ddl = '\n\n'.join(t.original for t in tables) + '\n'
        imp.require(hashlib.sha256(ddl.encode()).hexdigest() == imp.SPECS[sport]['ddl_sha'], 'Sport DDL hash mismatch.')
        c = imp.connect_staging(sport)
        try:
            imp.check_target(c,sport,tables);empty(c,tables)
            with c.cursor() as cur:
                cur.execute("SET SESSION sql_mode='STRICT_TRANS_TABLES,NO_AUTO_VALUE_ON_ZERO,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION'")
                cur.execute("SET SESSION time_zone='+00:00'")
                cur.execute("SET SESSION tidb_txn_mode='pessimistic'")
            c.commit()  # Session settings only; no rows yet.
            with tempfile.TemporaryDirectory() as root:
                index=imp.DiskIndex(Path(root)/'synthetic.sqlite')
                try:
                    c.begin();proxy=RollbackOnly(c)
                    for t in tables:
                        rows=[synthetic_row(t,i,sport) for i in (1,2)]
                        t.count=len(rows);index.add_table(t);index.add_rows(t,rows)
                        imp.insert_batch(proxy,t,rows)
                    imp.compare_target(proxy,tables,index,complete=True)
                finally:
                    c.rollback();index.close()
            empty(c,tables)
            print(f'PASS {sport}: complete schema preflight, two synthetic rows per table, bound batches, plain SHOW WARNINGS, streamed all-field comparison and rollback. All target tables remain empty.',flush=True)
        finally:
            try:c.rollback()
            finally:c.close()
    print('Importer SHA256: '+hashlib.sha256(Path('tools/tidb_mlb_nfl_import.py').read_bytes()).hexdigest())
    print('PASS: no historical rows imported; no NHL or PythonAnywhere access.',flush=True)


if __name__=='__main__':
    try:main()
    except Exception as e:
        code=e.args[0] if e.args and isinstance(e.args[0],int) else None
        detail=str(e) if isinstance(e,imp.Stop) else type(e).__name__
        print(f'STOP: {detail}; database_code={code}')
        raise SystemExit(1)
