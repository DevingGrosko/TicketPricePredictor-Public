"""One manual delivery of two unchanged Dallas public observations from run37737650041."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
from tools.shared_capture import identity, run_legacy

FILES = (
    ('7300563', '0c51ba6a85315e4efd9481f6e0414379857fb0ff65786eb569694ed2ffe12c4c',
     '2026-10-08T06:28:03.564576+00:00', '2026-10-30T00:00:00+00:00', 85),
    ('7300529', '864361479d3963e41761184be97d9c557991ff24bf0a7709b258d0ea658d0844',
     '2026-10-08T06:28:14.516309+00:00', '2026-10-21T00:00:00+00:00', 86),
)

def load_fixed(project=None):
    project = Path(project) if project is not None else Path(__file__).resolve().parents[1]
    saved = []
    for pid, digest, observed, event, sections in FILES:
        path = project/'docs/shared-observations'/f'snapshot-nhl-{pid}-dallas-oct8.json'
        if (path.is_symlink() or not path.is_file() or path.stat().st_size > 4*1024**2
                or not path.resolve().is_relative_to(project.resolve())):
            raise ValueError('Invalid bounded fixed public observation')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Fixed public observation SHA256 differs from the real canary')
        value = json.loads(data)
        source, slot, _key = identity('nhl', value)
        if (source != pid or slot != '2026-10-08T06:00:00+00:00' or value['captured_at'] != observed
                or value['event_date'] != event or value['section_count'] != sections):
            raise ValueError('Fixed public observation identity or original time changed')
        saved.append(value)
    return saved

def deliver(directory, pending_dir, health_output):
    saved = load_fixed()  # Validate both before the first real POST.
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
