"""Public stored-observation proof shared by NFL/NHL servers and collectors."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re

from Flask_App.collection_cadence import half_hour_capture_slot


def observation_sha256(sport, source_id, captured_at, sections):
    if sport not in ('nfl', 'nhl') or not str(source_id).isdigit():
        raise ValueError('Invalid observation sport or source identity')
    stamp = captured_at if isinstance(captured_at, datetime) else datetime.fromisoformat(
        captured_at.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Observation digest requires an aware capture slot')
    slot = half_hour_capture_slot(stamp).astimezone(timezone.utc).isoformat()
    rows = []
    for row in sections:
        get = row.get if isinstance(row, dict) else lambda key: getattr(row, key)
        raw_section = get('section')
        if not isinstance(raw_section, str):
            raise ValueError('Invalid committed observation section')
        section = ' '.join(raw_section.split())
        price, count = get('price'), get('listing_count')
        if (not section or isinstance(price, bool) or isinstance(count, bool)
                or int(price) != price or int(count) != count or price < 0 or count <= 0):
            raise ValueError('Invalid committed observation row')
        rows.append([section, int(price), int(count)])
    if not rows or len({row[0].casefold() for row in rows}) != len(rows):
        raise ValueError('Stored observation has missing or duplicate sections')
    value = dict(sport=sport, source_id=str(source_id), capture_slot=slot, sections=sorted(rows))
    data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(data).hexdigest()


def stored_observation_receipt(session, sport, Event, Iteration, Ticket, event_id, iteration_id):
    """Read actual committed rows; incoming payload facts are never substituted."""
    from sqlalchemy import select
    from models import captured_datetime_utc
    event = session.get(Event, event_id)
    iteration = session.get(Iteration, iteration_id)
    if event is None or iteration is None or iteration.event_id != event.id:
        raise ValueError('Stored receipt does not identify one committed observation')
    rows = session.scalars(select(Ticket).where(Ticket.iteration_id == iteration.id)).all()
    stamp = half_hour_capture_slot(captured_datetime_utc(iteration.captured_at)).isoformat()
    return dict(stored_source_id=event.source_id, stored_capture_slot=stamp,
                stored_section_count=len(rows), stored_observation_version=1,
                stored_observation_sha256=observation_sha256(sport, event.source_id, stamp, rows))


def verify_stored_receipt(response, *, sport, source_id, capture_slot, section_count, expected_sha256):
    """Require duplicate proof; compare every supplied proof during rollout."""
    if sport not in ('nfl', 'nhl') or response.get('event_type') != sport:
        raise ValueError('Stored observation belongs to a different sport')
    digest = response.get('stored_observation_sha256')
    if digest is None:
        if response.get('status') == 'duplicate':
            raise ValueError('Duplicate observation requires actual stored readback')
        return False  # Old stored responses remain compatible until server rollout.
    stamp = datetime.fromisoformat(str(response.get('stored_capture_slot', '')).replace('Z', '+00:00'))
    if (type(response.get('stored_observation_version')) is not int
            or response['stored_observation_version'] != 1 or not isinstance(digest, str)
            or not re.fullmatch('[0-9a-f]{64}', digest) or digest != expected_sha256
            or str(response.get('stored_source_id')) != str(source_id)
            or type(response.get('stored_section_count')) is not int
            or response['stored_section_count'] != section_count
            or stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0
            or stamp.isoformat() != capture_slot):
        raise ValueError('Stored observation differs from the original snapshot')
    return True
