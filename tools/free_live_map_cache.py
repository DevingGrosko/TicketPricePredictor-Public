"""Memoize repeated pure section-name comparisons during one capture run.

The existing matching rules and geometry validation remain authoritative. The
bounded functools caches are thread-safe and hold no browser or network state.
"""
from contextlib import contextmanager
from functools import lru_cache
from unittest.mock import patch


@contextmanager
def cached_map_matching():
    import nfl_metadata as maps
    original = maps.match_section_name
    normalize = maps.normalize_section_name
    number = maps.section_number

    @lru_cache(maxsize=16384)
    def match(candidate, labels):
        return original(candidate, labels)

    @lru_cache(maxsize=8192)
    def normalized(value):
        return normalize(value)

    @lru_cache(maxsize=8192)
    def numbered(value):
        return number(value)

    def safe_match(candidate, labels):
        labels = tuple(labels)
        if type(candidate) in (str, int, float, bool, type(None)) and all(type(s) is str for s in labels):
            return match(candidate, labels)
        return original(candidate, labels)

    def safe_normalize(value):
        return normalized(value) if type(value) is str else normalize(value)

    def safe_number(value):
        return numbered(value) if type(value) is str else number(value)

    try:
        with patch.object(maps, 'normalize_section_name', safe_normalize), \
             patch.object(maps, 'section_number', safe_number), \
             patch.object(maps, 'match_section_name', safe_match):
            yield match
    finally:
        match.cache_clear()
        normalized.cache_clear()
        numbered.cache_clear()
