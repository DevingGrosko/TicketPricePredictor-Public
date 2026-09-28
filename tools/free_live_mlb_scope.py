"""Original MLB home-venue scope, shared by collection and publication.

The existing collector.VENUE_FEEDS remains the source of truth. Matching an
away team is not enough. This module never deletes database observations.
"""
from pathlib import Path
import hashlib
import json

from Flask_App.report_policy import normalized, report_venue


def venue_key(value):
    return normalized(report_venue(value))


def tracked_venues():
    from collector import VENUE_FEEDS
    return tuple(VENUE_FEEDS)


def venue_in_scope(value):
    return venue_key(value) in {venue_key(venue) for venue in tracked_venues()}


def retain_scoped_events(sport, result):
    """Filter public inputs, not the stored cache or underlying source counts."""
    if sport != 'mlb':
        return result
    events, latest, captures, counts = result
    selected = {eid: event for eid, event in events.items()
                if venue_in_scope(getattr(event, 'Place', None) or getattr(event, 'venue', None))}
    return (selected, {eid: latest.get(eid) for eid in selected},
            {eid: captures.get(eid, 0) for eid in selected}, counts)


def quarantine_out_of_scope(pending_dir):
    """Retain old out-of-scope payload bytes, but do not replay them as work.

    Invalid files remain in the active queue for the existing validation path.
    The archive is inside the collector's saved state, not a public artifact.
    """
    pending = Path(pending_dir)
    archive = pending.parent / 'out-of-scope-payloads'
    moved = []
    for path in sorted({*pending.glob('*.json'), *pending.glob('*.rejected')}):
        if path.is_symlink() or path.stat().st_size > 4 * 1024**2:
            continue
        raw = path.read_bytes()
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(payload, dict) or not payload.get('venue'):
            continue
        if venue_in_scope(payload['venue']):
            continue
        archive.mkdir(parents=True, exist_ok=True)
        if archive.is_symlink():
            raise ValueError('Unsafe scope archive')
        target = archive / (hashlib.sha256(raw).hexdigest() + '.json')
        if target.is_symlink():
            raise ValueError('Unsafe archived payload')
        if target.exists():
            if target.read_bytes() != raw:
                raise ValueError('Conflicting archived payload')
            path.unlink()  # An identical preserved copy already exists.
        else:
            path.replace(target)
        moved.append(path.name)
    return moved
