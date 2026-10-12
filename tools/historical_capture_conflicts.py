"""Two archived October 10 originals whose PA slots already hold earlier captures.

This is a terminal conflict proof, never an acknowledgment or permission to
overwrite either observation. New conflicts remain ordinary delivery failures.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re

from Flask_App.collection_cadence import half_hour_capture_slot
from Flask_App.observation_receipt import observation_sha256

PROJECT = Path(__file__).resolve().parents[1]
MANIFEST = 'docs/shared-observations/conflicts/manifest-20261010-2300.json'
MANIFEST_SHA256 = '77df9e590501590c4f1595174a3e27d78c2693d0331873afbcb19136d2347b0f'
IDENTITIES = {('nfl', '6493301'), ('nhl', '7301426')}
SLOT = '2026-10-10T23:00:00+00:00'
RECEIPT_FIELDS = ('status', 'event_type', 'event_id', 'iteration_id', 'sections', 'captured_at',
                  'stored_source_id', 'stored_capture_slot', 'stored_section_count',
                  'stored_observation_version', 'stored_observation_sha256')


class ArchivedCaptureConflict(Exception):
    """The exact original and verified different PA receipt are durably preserved."""


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _read(relative, expected, limit):
    path = PROJECT / relative
    if (not path.resolve().is_relative_to(PROJECT.resolve()) or path.is_symlink()
            or any(parent.is_symlink() for parent in path.parents if parent.is_relative_to(PROJECT))
            or not path.is_file() or path.stat().st_size > limit):
        raise ValueError('Invalid historical conflict archive path')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError('Historical conflict archive integrity mismatch')
    return data


def _entry(record):
    if ((record.get('sport'), record.get('source_id')) not in IDENTITIES
            or record.get('capture_slot') != SLOT):
        return None
    manifest = json.loads(_read(MANIFEST, MANIFEST_SHA256, 16384))
    if manifest.get('version') != 1 or len(manifest.get('observations', [])) != 2:
        raise ValueError('Invalid historical conflict manifest')
    entry = next(row for row in manifest['observations']
                 if row['sport'] == record['sport'] and row['source_id'] == record['source_id'])
    if (record.get('payload_sha256') != entry['sha256']
            or any(record.get(key) != entry[key] for key in
                   ('captured_at', 'capture_slot', 'event_date', 'schedule_id',
                    'section_count', 'observation_sha256'))):
        return None
    expected_path = f"docs/shared-observations/conflicts/snapshot-{entry['sport']}-{entry['source_id']}-20261010-2300.json"
    if entry['file'] != expected_path:
        raise ValueError('Invalid historical conflict payload path')
    data = _read(entry['file'], entry['sha256'], 65536)
    payload = record.get('payload')
    if (not isinstance(payload, dict) or _encoded(payload) != data
            or observation_sha256(entry['sport'], entry['source_id'], entry['captured_at'],
                                  payload['sections']) != entry['observation_sha256']):
        raise ValueError('Historical conflict lost its exact archived original')
    return entry


def classify(record, response):
    """Return a bounded proof only for an archived original and real duplicate receipt."""
    entry = _entry(record)
    if entry is None or not isinstance(response, dict):
        return None
    try:
        if (response.get('status') != 'duplicate' or response.get('event_type') != record['sport']
                or response.get('stored_source_id') != record['source_id']
                or type(response.get('stored_observation_version')) is not int
                or response['stored_observation_version'] != 1
                or type(response.get('stored_section_count')) is not int
                or response['stored_section_count'] != entry['canonical_section_count']
                or type(response.get('sections')) is not int or response['sections'] != record['section_count']
                or any(type(response.get(key)) is not int or not 0 < response[key] < 2**63
                       for key in ('event_id', 'iteration_id'))):
            return None
        digest = response.get('stored_observation_sha256')
        if (type(digest) is not str or not re.fullmatch('[0-9a-f]{64}', digest)
                or digest == record['observation_sha256']):
            return None
        slot, captured = response.get('stored_capture_slot'), response.get('captured_at')
        if type(slot) is not str or slot != SLOT or type(captured) is not str or len(captured) > 40:
            return None
        stored_stamp = datetime.fromisoformat(slot)
        stamp = datetime.fromisoformat(captured.replace('Z', '+00:00'))
        if (stored_stamp.utcoffset() != timedelta(0) or stamp.tzinfo is None
                or half_hour_capture_slot(stamp).astimezone(timezone.utc).isoformat() != SLOT):
            return None
    except (ValueError, TypeError, OverflowError):
        return None
    receipt = {key: response[key] for key in RECEIPT_FIELDS}
    receipt['captured_at'] = stamp.astimezone(timezone.utc).isoformat()
    return dict(version=1, destination='pythonanywhere', archive_manifest_sha256=MANIFEST_SHA256,
                archive_file=entry['file'], archive_payload_sha256=entry['sha256'], receipt=receipt)


def validate(record):
    """Reject forged, expanded, stale or corrupt terminal markers on every read."""
    markers = record.get('archived_conflicts')
    if (not isinstance(markers, dict) or set(markers) != {'pythonanywhere'}
            or record['acknowledged']['pythonanywhere'] is not None):
        raise ValueError('Invalid historical conflict destinations or acknowledgment')
    marker = markers['pythonanywhere']
    expected = classify(record, marker.get('receipt')) if isinstance(marker, dict) else None
    if expected is None or type(marker.get('version')) is not int or expected != marker:
        raise ValueError('Unverified historical conflict marker')


def preserve_pending(path, record):
    """Rename only the exact preserved payload; never replace a different file."""
    validate(record)
    if (path.is_symlink() or not path.is_file() or path.stat().st_size > 65536
            or _encoded(json.loads(path.read_bytes())) != _encoded(record['payload'])):
        raise ValueError('Archived pending file differs from preserved original')
    target = path.with_suffix('.archived-conflict')
    if target != path:
        if target.exists() and (target.is_symlink() or target.read_bytes() != path.read_bytes()):
            raise ValueError('Refuse to replace a different archived pending file')
        path.replace(target)
    return target
