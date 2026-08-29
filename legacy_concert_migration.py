"""Compatibility exports for the legacy concert cleanup."""

from models import is_legacy_concert_url, migrate_legacy_concert_rows

__all__ = ["is_legacy_concert_url", "migrate_legacy_concert_rows"]
