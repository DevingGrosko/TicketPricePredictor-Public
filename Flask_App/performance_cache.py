"""Small process-local TTL cache for expensive read-only page data.

Cache keys include the backing database file's version, so a snapshot written by
another web worker naturally creates a new key. Explicit tag invalidation is
also used after successful ingest requests to free stale entries immediately in
the worker that handled the write.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import os
from pathlib import Path
from threading import Event, RLock
import time
from typing import Any, Callable, Iterable, TypeVar


T = TypeVar("T")


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


PAGE_CACHE_TTL_SECONDS = _positive_int_env("PAGE_CACHE_TTL_SECONDS", 600)
OPTIONS_CACHE_TTL_SECONDS = _positive_int_env("OPTIONS_CACHE_TTL_SECONDS", 600)
PAGE_CACHE_MAX_ENTRIES = _positive_int_env("PAGE_CACHE_MAX_ENTRIES", 256)
CACHE_BUILD_WAIT_SECONDS = _positive_int_env("CACHE_BUILD_WAIT_SECONDS", 90)


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float
    tags: frozenset[str]


class ProcessTTLCache:
    """Thread-safe LRU-ish TTL cache with per-key build coalescing."""

    def __init__(self, max_entries: int = PAGE_CACHE_MAX_ENTRIES) -> None:
        self.max_entries = max(int(max_entries), 1)
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._building: dict[str, Event] = {}
        self._tag_versions: dict[str, int] = {}
        self._lock = RLock()

    def _purge_expired(self, now: float) -> None:
        expired = [
            key for key, entry in self._entries.items() if entry.expires_at <= now
        ]
        for key in expired:
            self._entries.pop(key, None)

    def _tag_snapshot(self, tags: frozenset[str]) -> tuple[tuple[str, int], ...]:
        return tuple(sorted((tag, self._tag_versions.get(tag, 0)) for tag in tags))

    def get_or_create(
        self,
        key: str,
        builder: Callable[[], T],
        *,
        ttl_seconds: int = PAGE_CACHE_TTL_SECONDS,
        tags: Iterable[str] = (),
    ) -> T:
        normalized_tags = frozenset(
            str(tag).casefold() for tag in tags if str(tag).strip()
        )
        ttl = max(int(ttl_seconds), 1)

        while True:
            with self._lock:
                self._purge_expired(time.monotonic())
                entry = self._entries.get(key)
                if entry is not None:
                    self._entries.move_to_end(key)
                    return entry.value

                pending = self._building.get(key)
                if pending is None:
                    pending = Event()
                    self._building[key] = pending
                    tag_snapshot = self._tag_snapshot(normalized_tags)
                    owner = True
                else:
                    owner = False

            if owner:
                break

            # Wait for the request already calculating this key. If it takes too
            # long, calculate an uncached fallback rather than holding a worker
            # indefinitely. The original owner retains control of the cache key.
            if not pending.wait(CACHE_BUILD_WAIT_SECONDS):
                return builder()

        try:
            value = builder()
        except Exception:
            with self._lock:
                current = self._building.pop(key, None)
                if current is not None:
                    current.set()
            raise

        with self._lock:
            if tag_snapshot == self._tag_snapshot(normalized_tags):
                self._entries[key] = _CacheEntry(
                    value=value,
                    expires_at=time.monotonic() + ttl,
                    tags=normalized_tags,
                )
                self._entries.move_to_end(key)
                while len(self._entries) > self.max_entries:
                    self._entries.popitem(last=False)

            current = self._building.pop(key, None)
            if current is not None:
                current.set()
        return value

    def invalidate_tag(self, tag: str) -> int:
        normalized = str(tag).casefold()
        with self._lock:
            self._tag_versions[normalized] = self._tag_versions.get(normalized, 0) + 1
            keys = [
                key for key, entry in self._entries.items() if normalized in entry.tags
            ]
            for key in keys:
                self._entries.pop(key, None)
            return len(keys)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._tag_versions.clear()
            for pending in self._building.values():
                pending.set()
            self._building.clear()

    def size(self) -> int:
        with self._lock:
            self._purge_expired(time.monotonic())
            return len(self._entries)


page_cache = ProcessTTLCache()


def cache_key(*parts: Any) -> str:
    return ":".join(" ".join(str(part or "").split()).casefold() for part in parts)


def file_version(path: str | Path) -> str:
    """Return a cheap key component that changes when a SQLite file changes."""

    resolved = Path(path).expanduser().resolve()
    try:
        stat = resolved.stat()
    except OSError:
        return f"{resolved}:missing"
    return f"{resolved}:{stat.st_mtime_ns:x}-{stat.st_size:x}"


def invalidate_sport_cache(sport_key: str) -> int:
    return page_cache.invalidate_tag(str(sport_key).casefold())
