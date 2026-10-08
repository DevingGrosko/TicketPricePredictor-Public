"""One manual delivery of three unchanged public observations from run37734103002."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
from tools.shared_capture import identity, run_legacy

FILES = (
    ('7299775', '8ca736c8bfcaa20cb28b5bcbe72b2d85a43e2fc1abad673966c8a7430de6c6ce',
     '2026-10-08T05:48:12.337452+00:00', '2026-11-07T01:00:00+00:00', 66),
    ('7302223', 'f6e4952bcdd03bebd188a049c1e6a0aeac7a9a793eed7cd42b6cefeab24e7ac6',
     '2026-10-08T05:48:23.919028+00:00', '2026-11-06T00:00:00+00:00', 79),
    ('7299771', '80716c8c5fa8e72ac3cf5965bd2b5ccb089575f41f10f053ba76adc12386c003',
     '2026-10-08T05:48:32.414163+00:00', '2026-11-06T01:00:00+00:00', 69),
)

def load_fixed(project=None):
    project = Path(project) if project is not None else Path(__file__).resolve().parents[1]
    saved = []
    for pid, digest, observed, event, sections in FILES:
        path = project/'docs/shared-observations'/f'snapshot-nhl-{pid}-identity-oct8.json'
        if (path.is_symlink() or not path.is_file() or path.stat().st_size > 4*1024**2
                or not path.resolve().is_relative_to(project.resolve())):
            raise ValueError('Invalid bounded fixed public observation')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Fixed public observation SHA256 differs from the real canary')
        value = json.loads(data)
        source, slot, _key = identity('nhl', value)
        if (source != pid or slot != '2026-10-08T05:30:00+00:00' or value['captured_at'] != observed
                or value['event_date'] != event or value['section_count'] != sections):
            raise ValueError('Fixed public observation identity or original time changed')
        saved.append(value)
    return saved

def deliver(directory, pending_dir, health_output):
    saved = load_fixed()  # Validate all three before the first real POST.
    return run_legacy('nhl', directory, pending_dir, health_output, saved=saved)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--pending-dir', required=True)
    parser.add_argument('--health-output', required=True)
    args = parser.parse_args()
    try:
        return deliver(args.directory, args.pending_dir, args.health_output)
    except Exception as exc:
        print('NHL_SAVED_DELIVERY_FAILED '+type(exc).__name__, flush=True)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
