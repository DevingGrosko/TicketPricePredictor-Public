"""Opt-in legacy capture owner with durable, credential-separated delivery.

Capture keeps the real PythonAnywhere queue and acknowledgments. The same
immutable public observation is delivered to TiDB in a separate job. Nothing
imports this draft from recurring production entry points.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from unittest.mock import patch
from urllib.parse import urlsplit

from Flask_App.collection_cadence import half_hour_capture_slot

SPORTS = ('nfl', 'nhl')
DESTINATIONS = ('pythonanywhere', 'tidb')
LIMIT = 20 * 1024**2
FILE_LIMIT = 4 * 1024**2 + 8192
EXPORT_MANIFEST = 'observations.manifest'
FIELDS = {'schema_version', 'captured_at', 'event_date', 'source_url', 'source_id',
          'title', 'venue', 'section_count', 'sections', 'event_type', 'currency', 'schedule', 'map_geometry'}
PRIVATE = re.compile(r'password|secret|token|authorization|cookie|headers|api.?key', re.I)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def public_only(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if PRIVATE.search(str(key)):
                raise ValueError('Private transport fields cannot enter shared capture state')
            public_only(child)
    elif isinstance(value, list):
        for child in value:
            public_only(child)


def identity(sport, payload):
    from collector import as_utc, snapshot_from_payload
    from nfl_collector import is_nfl_game_title
    from nhl_collector import is_nhl_game_title
    if sport not in SPORTS or not isinstance(payload, dict) or payload.get('event_type') != sport:
        raise ValueError('Shared capture accepts the matching NFL/NHL sport only')
    if not set(payload) <= FIELDS or len(encoded(payload)) > 4 * 1024**2:
        raise ValueError('Invalid bounded public snapshot')
    public_only(payload)
    parsed = urlsplit(payload.get('source_url', ''))
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Shared source URL cannot contain credentials or transport queries')
    for key in ('captured_at', 'event_date'):
        if datetime.fromisoformat(payload[key].replace('Z', '+00:00')).tzinfo is None:
            raise ValueError('Original observation requires explicit timestamps')
    _url, event, captured, snapshot = snapshot_from_payload(payload)
    if captured.tzinfo is None or event.tzinfo is None:
        raise ValueError('Original observation requires explicit timestamps')
    if not (is_nfl_game_title if sport == 'nfl' else is_nhl_game_title)(snapshot.title):
        raise ValueError('Snapshot matchup belongs to another sport')
    if not 0 < (as_utc(event) - as_utc(captured)).total_seconds() <= 30 * 24 * 3600:
        raise ValueError('Snapshot event is outside its original capture window')
    slot = half_hour_capture_slot(as_utc(captured)).isoformat()
    key = hashlib.sha256(f'{sport}\0{snapshot.source_id}\0{slot}'.encode()).hexdigest()
    return snapshot.source_id, slot, key


class MirrorQueue:
    def __init__(self, directory, sport, *, byte_limit=LIMIT):
        if sport not in SPORTS:
            raise ValueError('MLB remains paused')
        self.root, self.sport, self.byte_limit = Path(directory), sport, byte_limit
        if self.root.is_symlink():
            raise ValueError('Shared state cannot be a symlink')
        self.root.mkdir(parents=True, exist_ok=True)

    def _save(self, path, value):
        data = encoded(value)
        if len(data) > FILE_LIMIT:
            raise ValueError('Shared record exceeds its budget')
        def reserved(record, size):
            return size + 1024 * sum(ack is None for ack in record['acknowledged'].values())
        used = sum(reserved(self.read(p), p.stat().st_size) for p in self.root.glob('*.json') if p != path)
        used += sum(p.stat().st_size for p in self.root.rglob('*') if p.is_file() and p.parent != self.root)
        if used + reserved(value, len(data)) > self.byte_limit:
            raise ValueError('Shared queue budget exhausted; pending observations remain intact')
        with tempfile.NamedTemporaryFile(dir=self.root, prefix='.shared-', delete=False) as out:
            temporary = Path(out.name)
            out.write(data); out.flush(); os.fsync(out.fileno())
        try:
            temporary.replace(path)
            fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)

    def read(self, path):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > FILE_LIMIT:
            raise ValueError('Invalid shared state file')
        value = json.loads(path.read_bytes())
        if (value.get('version') != 1 or value.get('sport') != self.sport
                or set(value.get('acknowledged', {})) != set(DESTINATIONS)):
            raise ValueError('Wrong shared state format')
        stamp = datetime.fromisoformat(value['captured_at'].replace('Z', '+00:00'))
        if (stamp.tzinfo is None or half_hour_capture_slot(stamp).isoformat() != value['capture_slot']
                or type(value['section_count']) is not int or value['section_count'] <= 0):
            raise ValueError('Invalid original shared observation metadata')
        public_only(value)
        expected = hashlib.sha256(f"{self.sport}\0{value['source_id']}\0{value['capture_slot']}".encode()).hexdigest()
        if path.name != expected + '.json' or not re.fullmatch('[0-9a-f]{64}', value['payload_sha256']):
            raise ValueError('Shared snapshot identity mismatch')
        if 'payload' in value:
            payload = value['payload']
            if (identity(self.sport, payload) != (value['source_id'], value['capture_slot'], expected)
                    or hashlib.sha256(encoded(payload)).hexdigest() != value['payload_sha256']
                    or payload['captured_at'] != value['captured_at']
                    or payload['section_count'] != value['section_count']):
                raise ValueError('Immutable shared snapshot changed')
            from Flask_App.observation_receipt import observation_sha256
            actual_digest = observation_sha256(self.sport, value['source_id'], value['captured_at'], payload['sections'])
            if value.get('observation_sha256', actual_digest) != actual_digest:
                raise ValueError('Immutable shared observation digest changed')
            value['observation_sha256'] = actual_digest
        elif not all(value['acknowledged'].values()):
            raise ValueError('Unacknowledged observation lost its payload')
        for dest, ack in value['acknowledged'].items():
            if ack is not None and self.acknowledgment(value, dest, ack) != ack:
                raise ValueError('Invalid saved destination acknowledgment')
        return value

    def records(self):
        return [(path, self.read(path)) for path in sorted(self.root.glob('*.json'))]

    def enqueue(self, payload):
        from Flask_App.observation_receipt import observation_sha256
        source, slot, key = identity(self.sport, payload)
        digest = hashlib.sha256(encoded(payload)).hexdigest()
        path = self.root / (key + '.json')
        if path.exists():
            if self.read(path)['payload_sha256'] != digest:
                raise ValueError('Different observation already exists for this game and slot')
            return path
        value = dict(version=1, sport=self.sport, source_id=source, capture_slot=slot,
            captured_at=payload['captured_at'], event_date=payload['event_date'],
            schedule_id=str((payload.get('schedule') or {}).get('schedule_id') or ''),
            section_count=payload['section_count'], payload_sha256=digest, payload=payload,
            observation_sha256=observation_sha256(self.sport, source, payload['captured_at'], payload['sections']),
            acknowledged={dest: None for dest in DESTINATIONS})
        self._save(path, value)
        return path

    @staticmethod
    def acknowledgment(record, destination, response):
        if (destination not in DESTINATIONS or not isinstance(response, dict)
                or response.get('status') not in ('stored', 'duplicate')
                or response.get('event_type') != record['sport']):
            raise ValueError('Destination did not acknowledge the matching sport')
        if destination == 'pythonanywhere':
            from Flask_App.observation_receipt import verify_stored_receipt
            verify_stored_receipt(response, sport=record['sport'], source_id=record['source_id'],
                capture_slot=record['capture_slot'], section_count=record['section_count'],
                expected_sha256=record.get('observation_sha256'))
        if any(type(response.get(k)) is not int or response[k] <= 0 for k in ('event_id', 'iteration_id', 'sections')):
            raise ValueError('Missing stored row identifiers')
        stamp = datetime.fromisoformat(response['captured_at'].replace('Z', '+00:00'))
        if stamp.tzinfo is None or half_hour_capture_slot(stamp).isoformat() != record['capture_slot']:
            raise ValueError('Destination acknowledged a different observation slot')
        if response['sections'] != record['section_count']:
            raise ValueError('Destination acknowledged a different section count')
        clean = {k: response[k] for k in ('status', 'event_type', 'event_id', 'iteration_id', 'sections', 'captured_at')}
        if destination == 'pythonanywhere' and response.get('stored_observation_sha256') is not None:
            clean.update({k: response[k] for k in ('stored_source_id', 'stored_capture_slot',
                'stored_section_count', 'stored_observation_version', 'stored_observation_sha256')})
        return clean

    def acknowledge(self, payload, destination, response):
        path = self.enqueue(payload)
        value = self.read(path)
        value['acknowledged'][destination] = self.acknowledgment(value, destination, response)
        if all(value['acknowledged'].values()):
            value.pop('payload', None)
        self._save(path, value)

    def merge(self, directory):
        incoming = MirrorQueue(directory, self.sport)
        for path, value in incoming.records():
            target = self.root / path.name
            if target.exists():
                current = self.read(target)
                if current['payload_sha256'] != value['payload_sha256']:
                    raise ValueError('Incoming observation collides with the stored slot')
                for dest in DESTINATIONS:
                    current['acknowledged'][dest] = current['acknowledged'][dest] or value['acknowledged'][dest]
                if all(current['acknowledged'].values()):
                    current.pop('payload', None)
                self._save(target, current)
            else:
                self._save(target, value)

    def pending(self, destination):
        return [(path, value) for path, value in self.records() if value['acknowledged'][destination] is None]

    def covered(self, game, slot):
        return any(value['schedule_id'] == str(game.schedule_id) and value['capture_slot'] == slot.isoformat()
                   and datetime.fromisoformat(value['event_date']) == game.event_date
                   for _path, value in self.records())

    def prune_receipts(self, now):
        for path, value in self.records():
            if 'payload' not in value and datetime.fromisoformat(value['captured_at']) < now - timedelta(days=7):
                path.unlink()


def _export_manifest(sport, count, digest):
    return (f'shared-observations-v1\nsport={sport}\nrecords={count}\nsha256={digest}\n').encode()


def _export_digest(digest, name, data):
    digest.update(f'{name}\0{hashlib.sha256(data).hexdigest()}\n'.encode())


def export_observations(sport, directory, output):
    """Export validated root records, including an explicit empty checkpoint."""
    mirror = MirrorQueue(directory, sport)
    output = Path(output)
    if (output.is_symlink() or output.resolve() == mirror.root.resolve()
            or output.resolve().is_relative_to(mirror.root.resolve())):
        raise ValueError('Shared export must be separate from durable capture state')
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError('Shared export requires a fresh empty directory')
    rows, total, digest = [], 0, hashlib.sha256()
    for path in sorted(mirror.root.glob('*.json')):
        data = encoded(mirror.read(path))
        total += len(data)
        if total + 256 > LIMIT:
            raise ValueError('Shared export exceeds its public byte budget')
        rows.append((path.name, data))
        _export_digest(digest, path.name, data)
    manifest = _export_manifest(sport, len(rows), digest.hexdigest())
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix='.shared-export-') as temp:
        staging = Path(temp) / 'records'; staging.mkdir()
        for name, data in rows:
            (staging / name).write_bytes(data)
        (staging / EXPORT_MANIFEST).write_bytes(manifest)
        if output.exists():
            output.rmdir()  # Only the verified empty output directory is removed.
        staging.replace(output)
    return dict(sport=sport, records=len(rows), bytes=total + len(manifest))


def validate_export(sport, directory):
    """A missing artifact is different from a verified zero-record export."""
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Missing shared observation export')
    manifest = root / EXPORT_MANIFEST
    if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 256:
        raise ValueError('Missing or invalid shared observation manifest')
    data = manifest.read_bytes()
    match = re.fullmatch(rb'shared-observations-v1\nsport=(nfl|nhl)\nrecords=([0-9]+)\nsha256=([0-9a-f]{64})\n', data)
    if not match or match[1].decode() != sport:
        raise ValueError('Wrong shared observation export format or sport')
    mirror = MirrorQueue(root, sport)
    total, count, digest = len(data), 0, hashlib.sha256()
    for path in sorted(root.iterdir()):
        if path.name == EXPORT_MANIFEST:
            continue
        if path.suffix != '.json' or path.is_symlink() or not path.is_file():
            raise ValueError('Shared export contains unexpected state')
        total += path.stat().st_size
        if total > LIMIT:
            raise ValueError('Shared export exceeds its public byte budget')
        mirror.read(path)
        _export_digest(digest, path.name, path.read_bytes())
        count += 1
    if count != int(match[2]) or digest.hexdigest() != match[3].decode():
        raise ValueError('Shared observation export is incomplete or changed')
    return dict(sport=sport, records=count, bytes=total)


def saved_observations(manifest_path, expected_sha256, sport):
    project = Path(__file__).resolve().parents[1]
    def read(relative, digest):
        if (not re.fullmatch(r'docs/shared-observations/[a-z0-9-]+\.json', relative)
                or not re.fullmatch('[0-9a-f]{64}', digest)):
            raise ValueError('Saved observations require explicit public paths and hashes')
        path = project / relative
        if (path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024**2
                or not path.resolve().is_relative_to(project / 'docs/shared-observations')):
            raise ValueError('Invalid bounded saved observation')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Saved observation integrity mismatch')
        return json.loads(data)
    manifest = read(manifest_path, expected_sha256)
    rows = manifest.get('observations')
    if manifest.get('version') != 1 or not isinstance(rows, list) or not 2 <= len(rows) <= 4:
        raise ValueError('Invalid bounded saved observation manifest')
    validated = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'sport', 'file', 'sha256'} or row['sport'] not in SPORTS:
            raise ValueError('Invalid saved observation manifest entry')
        value = read(row['file'], row['sha256'])
        identity(row['sport'], value)
        validated.append(value)
    if {value['event_type'] for value in validated} != set(SPORTS):
        raise ValueError('Saved pilot requires both explicit sports')
    return [value for value in validated if value['event_type'] == sport]


def run_legacy(sport, directory, pending_dir, health_output, *, timeout=45, runner=None,
               acknowledgments=None, saved=None, legacy_free_state=None):
    import collector
    import nfl_schedule_collector as nfl
    import nhl_schedule_collector as nhl
    if (sport not in SPORTS or os.environ.get('TICKETSIGNAL_STAGING_SITE') == '1'
            or any(os.environ.get(k) for k in ('TIDB_STAGING_HOST', 'TIDB_STAGING_USERNAME', 'TIDB_STAGING_PASSWORD'))):
        raise RuntimeError('Legacy capture requires a separate PythonAnywhere credential environment')
    token = os.environ.get('COLLECTOR_INGEST_TOKEN')
    if not token:
        raise RuntimeError('COLLECTOR_INGEST_TOKEN is required')
    mirror = MirrorQueue(directory, sport)
    if acknowledgments is not None:
        mirror.merge(acknowledgments)
    if saved is not None:
        for observation in saved:
            mirror.enqueue(observation)
    mirror.prune_receipts(datetime.now(timezone.utc))
    pending_dir = Path(pending_dir); pending_dir.mkdir(parents=True, exist_ok=True)
    queue_original, post_original = collector.queue_snapshot, collector.post_snapshot_with_retry
    # Mirror ALL existing payloads before replay can unlink an acknowledged PA file.
    existing_payloads = set()
    for path in sorted({*pending_dir.glob('*.json'), *pending_dir.glob('*.rejected')}):
        if path.is_symlink() or path.stat().st_size > 4 * 1024**2:
            raise ValueError('Invalid existing pending snapshot')
        observation = json.loads(path.read_bytes())
        mirror.enqueue(observation)
        existing_payloads.add(hashlib.sha256(encoded(observation)).hexdigest())
    # A prior runner may have cached the mirror but missed saving the PA queue.
    for _path, value in mirror.pending('pythonanywhere'):
        if value['payload_sha256'] not in existing_payloads:
            queue_original(value['payload'], pending_dir)
    endpoint = f'https://bunnyjeff.pythonanywhere.com/api/{sport}/snapshot'
    def queue(payload, pending):
        path = queue_original(payload, pending)
        mirror.enqueue(payload)  # Always durable before any real PA upload.
        return path
    def post(destination, ingest_token, payload, **kwargs):
        if destination != endpoint:
            raise ValueError('Shared capture uses the fixed matching PythonAnywhere endpoint')
        response = post_original(destination, ingest_token, payload, **kwargs)
        record = mirror.read(mirror.enqueue(payload))
        try:
            mirror.acknowledgment(record, 'pythonanywhere', response)
        except ValueError as exc:
            # A real upload can report an existing, different immutable slot.
            # Retain it as rejected and continue independent pending games.
            raise collector.SnapshotUploadError(
                f'PythonAnywhere observation conflict or unverified receipt: {exc}',
                retryable=False, status_code=409,
            ) from exc
        mirror.acknowledge(payload, 'pythonanywhere', response)  # No synthetic acknowledgment.
        return response
    module = nfl if sport == 'nfl' else nhl
    if (os.environ.get('TICKETSIGNAL_FIREFOX_NAVIGATION', 'direct') == 'performer'
            and os.environ.get('TICKETSIGNAL_BROWSER_ENGINE', 'chrome') not in {'firefox', 'webkit'}):
        raise ValueError('Performer navigation requires Firefox or WebKit')
    due_original = module.schedule_games_due
    reused = []
    covered = {(value['schedule_id'], value['capture_slot'], datetime.fromisoformat(value['event_date']))
               for _path, value in mirror.records()}
    def due(schedule, slot):
        selected = []
        for game in due_original(schedule, slot):
            if (str(game.schedule_id), slot.isoformat(), game.event_date) in covered:
                reused.append(str(game.schedule_id))
            else:
                selected.append(game)
        return selected
    with ExitStack() as stack:
        for target in (collector, nfl, nhl):
            stack.enter_context(patch.object(target, 'queue_snapshot', queue))
            stack.enter_context(patch.object(target, 'post_snapshot_with_retry', post))
        stack.enter_context(patch.object(module, 'schedule_games_due', due))
        if saved is not None:
            count, available, errors = collector.replay_pending_snapshots(endpoint, token, pending_dir)
            pending = len(list(pending_dir.glob('*.json'))) + len(list(pending_dir.glob('*.rejected')))
            report = dict(status='healthy' if available and not errors and not pending else 'queued',
                mode='delivery-only', event_type=sport, captured=0, scheduled_due=None, coverage_percent=None,
                replayed=count, pending=pending, errors=errors)
            Path(health_output).write_text(json.dumps(report, indent=2) + '\n')
            code = int(report['status'] != 'healthy')
        else:
            if runner is not None:
                code = runner(endpoint, token, False, timeout, Path(health_output), pending_dir)
            else:
                from tools.shared_capture_policy import run_owner
                code = run_owner(sport, module, mirror, endpoint, token, timeout,
                                 Path(health_output), pending_dir, legacy=legacy_free_state)
    health = json.loads(Path(health_output).read_text()) if Path(health_output).exists() else {'status': 'report-unavailable'}
    report = dict(sport=sport, reused_current_observations=sorted(set(reused)),
        pending_pythonanywhere=len(mirror.pending('pythonanywhere')), pending_tidb=len(mirror.pending('tidb')),
        legacy_status=health['status'], legacy_exit_code=code, mode=health.get('mode', 'capture'))
    print('SHARED_CAPTURE_REPORT ' + json.dumps(report), flush=True)
    return code or int(health['status'] != 'healthy' or report['pending_pythonanywhere'] > 0)


def deliver_tidb(sport, directory, incoming, *, sender=None, legacy_free_state=None):
    mirror = MirrorQueue(directory, sport)
    errors = []
    try:
        validate_export(sport, incoming)
    except Exception as exc:
        # A missing/bad current artifact is a real failure, while previously
        # validated cached observations may still finish independent delivery.
        errors.append('incoming-export-' + type(exc).__name__)
    else:
        mirror.merge(incoming)
    if sender is None:
        from tools.import_shared_snapshot import deliver_tidb as sender
    for _path, value in mirror.pending('tidb'):
        try:
            response = sender(value['payload'])
            if (response.get('source_id') != value['source_id']
                    or response.get('observed_at') != value['captured_at']
                    or response.get('price_readback_verified') is not True
                    or response.get('identity_readback_verified') is not True):
                raise ValueError('TiDB readback did not verify the original observation')
            mirror.acknowledge(value['payload'], 'tidb', response)
        except Exception as exc:
            errors.append(type(exc).__name__)
    legacy = {}
    if legacy_free_state is not None:
        from tools.shared_capture_policy import replay_free_pending
        legacy = replay_free_pending(sport, legacy_free_state, sender=sender)
        errors.extend(legacy['errors'])
    report = dict(sport=sport, pending_tidb=len(mirror.pending('tidb')), error_types=errors,
                  legacy_replay=legacy)
    print('SHARED_TIDB_REPORT ' + json.dumps(report), flush=True)
    return int(bool(errors or report['pending_tidb'] or legacy.get('pending')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('collect', 'deliver', 'export'))
    parser.add_argument('--sport', choices=SPORTS, required=True)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--pending-dir')
    parser.add_argument('--health-output')
    parser.add_argument('--incoming')
    parser.add_argument('--output')
    parser.add_argument('--timeout', type=int, default=45)
    parser.add_argument('--acknowledgments')
    parser.add_argument('--legacy-free-state')
    parser.add_argument('--saved-manifest')
    parser.add_argument('--manifest-sha256')
    args = parser.parse_args()
    try:
        if args.command == 'export':
            if not args.output:
                parser.error('export requires output')
            report = export_observations(args.sport, args.directory, args.output)
            print('SHARED_EXPORT_REPORT ' + json.dumps(report), flush=True)
            return 0
        if args.command == 'collect':
            if not args.pending_dir or not args.health_output:
                parser.error('collect requires pending-dir and health-output')
            if bool(args.saved_manifest) != bool(args.manifest_sha256):
                parser.error('Saved replay requires both public manifest and its SHA-256')
            saved = saved_observations(args.saved_manifest, args.manifest_sha256, args.sport) if args.saved_manifest else None
            return run_legacy(args.sport, args.directory, args.pending_dir, args.health_output, timeout=args.timeout,
                              acknowledgments=args.acknowledgments, saved=saved, legacy_free_state=args.legacy_free_state)
        if not args.incoming:
            parser.error('deliver requires incoming')
        return deliver_tidb(args.sport, args.directory, args.incoming, legacy_free_state=args.legacy_free_state)
    except Exception as exc:
        print('SHARED_CAPTURE_FAILED ' + type(exc).__name__, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
