"""Build the original static UI with bounded, build-local memoization.

The cached inputs belong to an immutable staging snapshot. The original
validators still run on every distinct map/label-set/threshold combination.
No production code, database rows, collector, schedule, or deployment is changed.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
from contextlib import ExitStack, contextmanager
from functools import lru_cache, wraps
import time
from unittest.mock import patch


class IdentityMemo:
    """Bounded cache retaining input references to prevent object-id reuse."""

    def __init__(self, function, limit=128):
        if limit < 1:
            raise ValueError('Cache limit must be positive.')
        self.function, self.limit = function, limit
        self.entries = OrderedDict()
        self.hits = self.misses = 0

    def __call__(self, geometry, names, **kwargs):
        labels = tuple(names)
        key = (id(geometry), labels, tuple(sorted(kwargs.items())))
        stored = self.entries.get(key)
        if stored is not None and stored[0] is geometry:
            self.entries.move_to_end(key)
            self.hits += 1
            return stored[1]
        self.misses += 1
        result = self.function(geometry, labels, **kwargs)
        self.entries[key] = (geometry, result)
        self.entries.move_to_end(key)
        while len(self.entries) > self.limit:
            self.entries.popitem(last=False)
        return result

    def clear(self):
        self.entries.clear()


@contextmanager
def cached_original_analysis():
    """Scope all memoization to this build and restore originals on every exit."""
    from Flask_App import nfl_stadium_blueprint as api
    from Flask_App import nfl_blueprint as nfl
    from Flask_App import nhl_blueprint as nhl

    sanitizer = IdentityMemo(nfl.sanitize_map_geometry)
    usable = IdentityMemo(nfl.geometry_is_usable)
    canonical = lru_cache(maxsize=16384)(api.section_identity)
    normalize_cached = lru_cache(maxsize=8192)(nfl.normalize_section_name)
    number_cached = lru_cache(maxsize=8192)(nfl.section_number)
    original_normalize, original_number = nfl.normalize_section_name, nfl.section_number

    @wraps(original_normalize)
    def normalized(value):
        return normalize_cached(value) if isinstance(value, str) else original_normalize(value)

    @wraps(original_number)
    def numbered(value):
        return number_cached(value) if isinstance(value, str) else original_number(value)

    original_public = api._public_sections

    @lru_cache(maxsize=512)
    def public_cached(labels):
        return tuple(original_public(labels))

    @wraps(original_public)
    def public_sections(labels):
        # Return a fresh list so callers cannot modify cached output.
        values = tuple(labels or ())
        if not all(isinstance(label, str) for label in values):
            return original_public(values)
        return list(public_cached(values))

    try:
        with ExitStack() as stack:
            for module in (api, nfl, nhl):
                for name, replacement in (('sanitize_map_geometry', sanitizer), ('geometry_is_usable', usable)):
                    if hasattr(module, name):
                        stack.enter_context(patch.object(module, name, replacement))
            stack.enter_context(patch.object(api, 'section_identity', canonical))
            stack.enter_context(patch.object(api, '_public_sections', public_sections))
            stack.enter_context(patch.object(nfl, 'normalize_section_name', normalized))
            stack.enter_context(patch.object(nfl, 'section_number', numbered))
            yield {'sanitizer': sanitizer, 'usable': usable, 'canonical': canonical}
    finally:
        sanitizer.clear(); usable.clear()
        canonical.cache_clear(); normalize_cached.cache_clear(); number_cached.cache_clear(); public_cached.cache_clear()


def build(output):
    from tools import build_original_static as original
    original_sport = original.OriginalPages._sport

    @wraps(original_sport)
    def progress(self, sport, *args, **kwargs):
        start = time.monotonic()
        print(f'ORIGINAL_RENDER_START {sport}', flush=True)
        result = original_sport(self, sport, *args, **kwargs)
        print(f'ORIGINAL_RENDER_DONE {sport}: {time.monotonic()-start:.2f}s', flush=True)
        return result

    with cached_original_analysis() as caches, patch.object(original.OriginalPages, '_sport', progress):
        report = original.build(output)
        print('MAP_CACHE', {name: {'hits': obj.hits, 'misses': obj.misses} for name, obj in caches.items() if isinstance(obj, IdentityMemo)}, flush=True)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='static-preview-dist')
    args = parser.parse_args()
    try:
        build(args.output)
    except Exception as exc:
        # Do not include provider exceptions, connection details, or credentials.
        import traceback
        frames = traceback.extract_tb(exc.__traceback__)
        print('ORIGINAL_FAST_BUILD_FAILED', type(exc).__name__, [(f.name, f.lineno) for f in frames[-5:]], flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
