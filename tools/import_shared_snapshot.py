"""Manual import of one public NHL observation; no browser or scheduler work.

Prepare one legacy-compatible snapshot from a hash-verified complete inventory,
then run each destination in a separate credential environment. Retrying a
delivery reuses the same observation and its original capture timestamp.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

from Flask_App.collection_cadence import half_hour_capture_slot

LIMIT = 4 * 1024**2
PAYLOAD_FIELDS = {'schema_version', 'captured_at', 'event_date', 'source_url',
    'source_id', 'title', 'venue', 'section_count', 'sections', 'event_type',
    'currency', 'schedule', 'map_geometry'}
PA_ENDPOINT = 'https://bunnyjeff.pythonanywhere.com/api/nhl/snapshot'


def _time(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Observation and event timestamps require a timezone')
    return stamp.astimezone(timezone.utc)


def _read(path, expected_sha256):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > LIMIT:
        raise ValueError('Input is not a bounded regular snapshot')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError('Input SHA-256 mismatch')
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError('Input must be a JSON object')
    return value


def prepare(inventory_path, inventory_sha256, source_url, production_id,
            captured_at, event_at, output):
    from collector import validated_vivid_url
    from nhl_collector import NHLSnapshotParser, nhl_snapshot_to_payload
    from vivid_inventory import validate_inventory
    raw = _read(inventory_path, inventory_sha256)
    if set(raw) != {'global', 'tickets'}:
        raise ValueError('Only public inventory metadata and listings may be imported')
    validate_inventory(raw, production_id)
    metadata = raw['global'][0]
    if int(metadata['listingCount']) != len(raw['tickets']):
        raise ValueError('Inventory is incomplete or filtered')
    source_url = validated_vivid_url(source_url)
    if source_url.rstrip('/').split('/')[-1] != production_id:
        raise ValueError('Inventory and event URL identify different productions')
    captured, event = _time(captured_at), _time(event_at)
    if not 0 < (event - captured).total_seconds() <= 30 * 24 * 3600:
        raise ValueError('Event is outside the capture window at observation time')
    # The existing parser only needs these public fields. Headers, cookies,
    # request bodies, seller IDs and other transport/session data are excluded.
    global_fields = ('productionId', 'productionName', 'mapTitle', 'currencyCode', 'currency')
    listing_fields = ('l', 'p', 'aip', 'r', 'q', 'tags')
    public = {'global': [{key: metadata[key] for key in global_fields if key in metadata}],
              'tickets': [{key: row[key] for key in listing_fields if key in row}
                          for row in raw['tickets']]}
    snapshot = NHLSnapshotParser.parse(public)
    schedule = {'country': metadata.get('venueCountry'),
                'venue_timezone': metadata.get('venueTimeZone'),
                'canonical_venue': snapshot.venue}
    result = nhl_snapshot_to_payload(source_url, event, captured, snapshot, schedule=schedule)
    from tools.free_refresh_capture import parse_payload
    parse_payload('nhl', result, now=captured)
    data = (json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and output.read_bytes() != data:
        raise ValueError('Refusing to replace a different prepared observation')
    output.write_bytes(data)
    return dict(status='prepared', source_id=snapshot.source_id,
        inventory_count=len(raw['tickets']), section_count=len(snapshot.sections),
        captured_at=result['captured_at'], event_date=result['event_date'],
        capture_slot=half_hour_capture_slot(captured).isoformat(),
        inventory_sha256=inventory_sha256, payload_sha256=hashlib.sha256(data).hexdigest())


def load_payload(path, sha256, *, now=None):
    value = _read(path, sha256)
    if not set(value) <= PAYLOAD_FIELDS or value.get('event_type') != 'nhl':
        raise ValueError('Only the prepared NHL snapshot format is accepted')
    from tools.free_refresh_capture import parse_payload
    parse_payload('nhl', value, now=now or datetime.now(timezone.utc))
    return value


def verify_receipt(payload, response, destination):
    if not isinstance(response, dict) or response.get('status') not in ('stored', 'duplicate'):
        raise ValueError('Destination did not acknowledge the observation')
    if any(type(response.get(key)) is not int or response[key] <= 0
           for key in ('event_id', 'iteration_id', 'sections')):
        raise ValueError('Destination receipt is missing stored row identifiers')
    if response['sections'] != payload['section_count']:
        raise ValueError('Destination section count differs from captured observation')
    stamp = datetime.fromisoformat(response['captured_at'].replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        if destination != 'tidb':
            raise ValueError('PythonAnywhere receipt is missing its timezone')
        stamp = stamp.replace(tzinfo=timezone.utc)
    slot = half_hour_capture_slot(_time(payload['captured_at']))
    if half_hour_capture_slot(stamp) != slot:
        raise ValueError('Destination stored a different capture slot')
    return dict(destination=destination, status=response['status'],
        source_id=payload['source_id'], event_id=response['event_id'],
        iteration_id=response['iteration_id'], sections=response['sections'],
        observed_at=payload['captured_at'], captured_at=slot.isoformat(),
        event_date=payload['event_date'])


def deliver_pythonanywhere(payload, *, send=None):
    if any(os.environ.get(key) for key in
           ('TIDB_STAGING_HOST', 'TIDB_STAGING_USERNAME', 'TIDB_STAGING_PASSWORD')):
        raise RuntimeError('PythonAnywhere delivery requires a separate credential environment')
    token = os.environ.get('COLLECTOR_INGEST_TOKEN', '')
    if not token:
        raise RuntimeError('COLLECTOR_INGEST_TOKEN is required')
    if send is None:
        from collector import post_snapshot_with_retry
        send = post_snapshot_with_retry
    response = send(PA_ENDPOINT, token, payload, timeout=20)
    return verify_receipt(payload, response, 'pythonanywhere')


def deliver_tidb(payload, *, writer=None):
    from tools.free_refresh_capture import open_writer, store_payload, models_for
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    engine = (writer or open_writer)('nhl')
    try:
        response = store_payload(engine, 'nhl', payload)
        receipt = verify_receipt(payload, response, 'tidb')
        _, _, Ticket = models_for('nhl')
        with Session(engine) as session:
            actual = sorted((row.section, row.price, row.listing_count) for row in
                session.scalars(select(Ticket).where(Ticket.iteration_id == response['iteration_id'])))
        expected = sorted((row['section'], row['price'], row['listing_count']) for row in payload['sections'])
        if actual != expected:
            raise ValueError('Committed TiDB prices or inventory counts differ from original snapshot')
        receipt['price_readback_verified'] = True
        return receipt
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    preparation = commands.add_parser('prepare')
    preparation.add_argument('--inventory', required=True)
    preparation.add_argument('--inventory-sha256', required=True)
    preparation.add_argument('--source-url', required=True)
    preparation.add_argument('--production-id', required=True)
    preparation.add_argument('--captured-at', required=True)
    preparation.add_argument('--event-at', required=True)
    preparation.add_argument('--output', required=True)
    for command in ('verify', 'pythonanywhere', 'tidb'):
        delivery = commands.add_parser(command)
        delivery.add_argument('--payload', required=True)
        delivery.add_argument('--sha256', required=True)
        delivery.add_argument('--receipt', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            report = prepare(args.inventory, args.inventory_sha256, args.source_url,
                args.production_id, args.captured_at, args.event_at, args.output)
        else:
            payload = load_payload(args.payload, args.sha256)
            if args.command == 'verify':
                report = dict(status='verified', source_id=payload['source_id'],
                    sections=payload['section_count'], observed_at=payload['captured_at'])
            else:
                report = {'pythonanywhere': deliver_pythonanywhere, 'tidb': deliver_tidb}[args.command](payload)
            report['payload_sha256'] = args.sha256
            Path(args.receipt).write_text(json.dumps(report, indent=2) + '\n')
        print('SHARED_SNAPSHOT_IMPORT ' + json.dumps(report, sort_keys=True))
        return 0
    except Exception as exc:
        # No raw SQL/HTTP exception strings: a driver can include credentials.
        print('SHARED_SNAPSHOT_IMPORT_FAILED ' + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
