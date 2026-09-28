"""Validate/replay a local archive of saved captures without inventing timestamps.

Default is validation-only. --apply requires the existing staging-write opt-in.
Inputs must be the user's trusted collector snapshots, not untrusted submissions.
No SQL dump import, schema change, deletion, or replacement of existing captures.
"""
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
import argparse
import hashlib
import json


def recover(sport, directory, apply=False):
    import collector
    from tools import free_refresh_capture as storage
    from tools.free_live_hardening import preserved_parser
    root = Path(directory).resolve()
    files = sorted(root.glob('*.json'))
    if not root.is_dir() or not files:
        raise ValueError('Choose a directory containing trusted saved capture JSON files')
    report = {'sport':sport, 'applied':apply, 'files':[], 'errors':[]}
    engine = None
    with ExitStack() as stack:
        stack.enter_context(patch.object(collector,'MIN_USABLE_SECTIONS',1))
        stack.enter_context(patch.object(storage,'parse_payload',preserved_parser(storage.parse_payload)))
        try:
            if apply: engine = storage.open_writer(sport)
            for path in files:
                try:
                    if path.is_symlink() or path.stat().st_size > 4*1024**2:
                        raise ValueError('Invalid capture file')
                    raw = path.read_bytes()
                    payload = json.loads(raw)
                    storage.parse_payload(sport,payload)
                    result = storage.store_payload(engine,sport,payload) if apply else {'status':'valid'}
                    report['files'].append({'file':path.name,'sha256':hashlib.sha256(raw).hexdigest(),
                        'original_captured_at':payload['captured_at'], 'result':result})
                except Exception as exc:
                    report['errors'].append({'file':path.name,'type':type(exc).__name__})
        finally:
            if engine is not None: engine.dispose()
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sport',choices=('mlb','nfl','nhl'),required=True)
    parser.add_argument('--directory',required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--report',required=True)
    args=parser.parse_args()
    if Path(args.report).resolve().is_relative_to(Path(args.directory).resolve()):
        raise ValueError('Put the recovery report outside the capture input directory')
    report=recover(args.sport,args.directory,args.apply)
    Path(args.report).write_text(json.dumps(report,indent=2)+'\n')
    print('FREE_RECOVERY '+json.dumps({'validated':len(report['files']),'errors':len(report['errors']),
                                     'applied':args.apply}),flush=True)
    raise SystemExit(bool(report['errors']))
