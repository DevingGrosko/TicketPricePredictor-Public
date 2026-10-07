"""Keep recently published content hashes available across Pages deployments.

Only validated public JSON/JS hashes enter this compressed cache. Raw database caches,
credentials, manifests, and HTML are never copied into the public output.
"""
from collections import deque
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.error import HTTPError, URLError

from tools.build_static_preview import BuildError, encoded


PUBLIC_BASE = 'https://devinggrosko.github.io/TicketPricePredictor-Public/'
HASHED_PATH = re.compile(r'(?:native/(?:data-([a-f0-9]{64})\.json|script-([a-f0-9]{64})\.js)|data/(?:series|game|report|index)-([a-f0-9]{64})\.json)')
RETENTION = timedelta(minutes=30)
CACHE_LIMIT = 1024**3
PUBLIC_LIMIT = 700 * 1024**2
FILE_LIMIT = 3 * 1024**2
BOOTSTRAP_LIMIT = 128 * 1024**2
ASSET_COUNT_LIMIT = 100000
BOOTSTRAP_SECONDS = 120


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def asset_path(value):
    if not isinstance(value, str):
        return None
    path = value.removeprefix('/')
    return path if HASHED_PATH.fullmatch(path) else None


def checked_bytes(path, raw):
    match = HASHED_PATH.fullmatch(path)
    if not match or len(raw) > FILE_LIMIT:
        raise BuildError('Invalid retained public asset')
    expected = next(group for group in match.groups() if group)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise BuildError('Retained public asset hash mismatch')
    if path.endswith('.json'):
        json.loads(raw)
    return raw


def public_read(path):
    if path != 'original-manifest.json' and not asset_path(path):
        raise BuildError('Invalid public retention request')
    request = Request(PUBLIC_BASE + path, headers={'User-Agent': 'TicketSignal-public-asset-retention/1.0'})
    with build_opener(NoRedirects()).open(request, timeout=10) as response:
        if response.geturl() != request.full_url:
            raise BuildError('Unexpected public retention redirect')
        raw = response.read(FILE_LIMIT + 1)
    if len(raw) > FILE_LIMIT:
        raise BuildError('Public retention response exceeds its size limit')
    return raw


def references(value):
    if isinstance(value, str):
        path = asset_path(value)
        if path:
            yield path
    elif isinstance(value, dict):
        for item in value.values():
            yield from references(item)
    elif isinstance(value, list):
        for item in value:
            yield from references(item)


def previous_public_assets(current, read):
    """Bootstrap the live manifest's hashes, reusing unchanged local assets."""
    deadline = time.monotonic() + BOOTSTRAP_SECONDS
    stats = {'finished': True, 'recovered_assets': 0, 'downloaded_bytes': 0,
             'unavailable_assets': 0, 'unavailable_manifest': False,
             'unavailable_statuses': {}, 'unvisited_assets': 0, 'warnings': []}

    def attempt(path):
        try:
            return read(path)
        except HTTPError as error:
            if 300 <= error.code < 400:
                raise BuildError('Unexpected public retention redirect') from error
            reason = str(error.code)
        except (URLError, TimeoutError):
            reason = 'connection'
        stats['unavailable_statuses'][reason] = stats['unavailable_statuses'].get(reason, 0) + 1
        return None

    manifest_raw = attempt('original-manifest.json')
    if manifest_raw is None:
        stats.update(unavailable_manifest=True, warnings=['previous-manifest-unavailable'])
        return {}, stats
    if len(manifest_raw) > FILE_LIMIT:
        raise BuildError('Previous public manifest exceeds its size limit')
    stats['downloaded_bytes'] = len(manifest_raw)
    manifest = json.loads(manifest_raw)
    if manifest.get('presentation') != 'original-templates' or not isinstance(manifest.get('sports'), dict):
        raise BuildError('Unexpected previous public manifest')
    pending = deque()
    for path in manifest['sports'].values():
        normalized = asset_path(path)
        if not normalized or not normalized.endswith('.json'):
            raise BuildError('Invalid previous public catalog reference')
        pending.append(normalized)
    seen, added = set(), {}
    while pending:
        if time.monotonic() >= deadline:
            stats['unvisited_assets'] = len(set(pending) - seen)
            stats['warnings'].append('bootstrap-time-budget')
            break
        path = pending.popleft()
        if path in seen:
            continue
        seen.add(path)
        if len(seen) > ASSET_COUNT_LIMIT:
            raise BuildError('Public asset retention inventory exceeds its budget')
        raw = current.get(path)
        if raw is None:
            # Leave enough budget for one bounded response before requesting it.
            if stats['downloaded_bytes'] + FILE_LIMIT > BOOTSTRAP_LIMIT:
                stats['unvisited_assets'] = 1 + len(set(pending) - seen)
                stats['warnings'].append('bootstrap-download-budget')
                break
            raw = attempt(path)
            if raw is None:
                stats['unavailable_assets'] += 1
                continue
            raw = checked_bytes(path, raw)
            stats['downloaded_bytes'] += len(raw)
            added[path] = raw
        if path.endswith('.json'):
            pending.extend(references(json.loads(raw)))
    if stats['unavailable_assets']:
        stats['warnings'].append('previous-public-assets-unavailable')
    stats['recovered_assets'] = len(added)
    return added, stats


def tree_bytes(root):
    return sum(path.stat().st_size for path in root.rglob('*') if path.is_file())


def cached_bytes(file, path):
    if file.stat().st_size > FILE_LIMIT + 4096:
        raise BuildError('Retained public gzip exceeds its size limit')
    try:
        with gzip.open(file, 'rb') as compressed:
            raw = compressed.read(FILE_LIMIT + 1)
    except (OSError, EOFError) as error:
        raise BuildError('Retained public gzip is invalid') from error
    return checked_bytes(path, raw)


def retain_public_assets(output, cache, *, now=None, read=public_read):
    output, cache = Path(output).resolve(), Path(cache).resolve()
    if output == cache or output.is_relative_to(cache) or cache.is_relative_to(output):
        raise BuildError('Public asset cache and output must be separate')
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise BuildError('Public retention clock must be timezone-aware')
    now = now.astimezone(timezone.utc)
    archive = cache / 'published-assets'
    index = archive / 'index.json'
    if archive.is_symlink() or index.is_symlink():
        raise BuildError('Public asset cache symlink rejected')

    current = {}
    for file in output.rglob('*'):
        path = asset_path(file.relative_to(output).as_posix())
        if path and file.is_file():
            if file.is_symlink():
                raise BuildError('Public asset symlink rejected')
            current[path] = checked_bytes(path, file.read_bytes())
    if len(current) > ASSET_COUNT_LIMIT:
        raise BuildError('Public asset retention inventory exceeds its budget')

    previous = {}
    bootstrap = None
    bootstrapped_now = not index.is_file()
    if index.is_file():
        saved = json.loads(index.read_bytes())
        if saved.get('version') != 2 or saved.get('encoding') != 'gzip' or not isinstance(saved.get('assets'), dict):
            raise BuildError('Unexpected public asset cache identity')
        previous = saved['assets']
        bootstrap = saved.get('bootstrap')
    retained, states = {}, {path: None for path in current}
    for path, retired in previous.items():
        if not asset_path(path):
            raise BuildError('Invalid retained public asset path')
        if path in current:
            continue
        retired_at = now if retired is None else datetime.fromisoformat(retired)
        if retired_at.tzinfo is None:
            raise BuildError('Invalid retained public asset timestamp')
        if now >= retired_at + RETENTION:
            continue
        file = archive / (path + '.gz')
        if file.is_symlink() or not file.resolve().is_relative_to(archive):
            raise BuildError('Public asset cache symlink rejected')
        retained[path] = cached_bytes(file, path)
        states[path] = retired_at.isoformat()
    if not index.is_file():
        recovered, bootstrap = previous_public_assets(current, read)
        for path, raw in recovered.items():
            retained[path] = raw
            states[path] = now.isoformat()

    assets = {**current, **retained}
    if len(assets) > ASSET_COUNT_LIMIT:
        raise BuildError('Public asset retention inventory exceeds its budget')
    compressed = {path: gzip.compress(raw, compresslevel=6, mtime=0) for path, raw in assets.items()}
    index_raw = encoded({'version': 2, 'encoding': 'gzip', 'assets': states, 'bootstrap': bootstrap})
    # Check budgets before publishing files or changing the recoverable cache.
    raw_cache_bytes = sum(file.stat().st_size for file in cache.rglob('*')
                          if file.is_file() and not file.is_relative_to(archive))
    compressed_bytes = sum(map(len, compressed.values()))
    combined_bytes = raw_cache_bytes + compressed_bytes + len(index_raw)
    if combined_bytes > CACHE_LIMIT:
        raise BuildError('Source and public asset cache exceeds its 1 GB budget: '
                         + str(raw_cache_bytes) + ' raw source + ' + str(compressed_bytes)
                         + ' compressed public + ' + str(len(index_raw)) + ' index bytes')
    if tree_bytes(output) + sum(map(len, retained.values())) > PUBLIC_LIMIT:
        raise BuildError('Retained publication exceeds the free Pages safety budget')

    archive.mkdir(parents=True, exist_ok=True)
    for path, raw in compressed.items():
        target = archive / (path + '.gz')
        if target.is_symlink() or not target.resolve().is_relative_to(archive):
            raise BuildError('Public asset cache symlink rejected')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    temporary = index.with_suffix('.tmp')
    temporary.write_bytes(index_raw)
    temporary.replace(index)
    for file in archive.rglob('*'):
        if file.is_file() and file != index and file.relative_to(archive).as_posix().removesuffix('.gz') not in assets:
            file.unlink()
    for path, raw in retained.items():
        target = output / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    return {'retained_public_assets': len(retained),
            'retained_public_bytes': sum(map(len, retained.values())),
            'public_asset_cache_files': len(assets), 'public_asset_retention_minutes': 30,
            'raw_source_cache_bytes': raw_cache_bytes,
            'current_public_asset_bytes': sum(map(len, current.values())),
            'compressed_public_asset_cache_bytes': compressed_bytes,
            'combined_source_cache_bytes': combined_bytes,
            'public_asset_bootstrapped_now': bootstrapped_now, 'public_asset_bootstrap': bootstrap}
