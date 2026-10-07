"""Shared UTC observation slots for independent NFL/NHL pipelines."""
from datetime import datetime, timezone


CAPTURE_TICK_MINUTES = 30


def half_hour_capture_slot(value: datetime) -> datetime:
    """Preserve existing :00 history and allow one distinct :30 observation."""
    value = value.astimezone(timezone.utc)
    return value.replace(minute=0 if value.minute < 30 else 30, second=0, microsecond=0)


def phased_capture_is_due(value: datetime, interval_hours: int | float, phase_hours: int = 0) -> bool:
    """Evaluate half-hour tiers without doubling the existing slower phases."""
    interval_slots = int(interval_hours * 2)
    if interval_slots < 1 or interval_slots != interval_hours * 2:
        raise ValueError("Capture interval must be a positive multiple of 30 minutes")
    slot_number = int(half_hour_capture_slot(value).timestamp() // (CAPTURE_TICK_MINUTES * 60))
    return slot_number % interval_slots == phase_hours * 2
