"""Readers carrying old catalogs survive a bounded deployment transition."""
from datetime import datetime, timedelta, timezone
import hashlib
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from tools import free_public_assets as assets
from tools.build_static_preview import BuildError, encoded


NOW = datetime(2026, 10, 7, 2, tzinfo=timezone.utc)


def blob(value, kind='native/data', suffix='json'):
    raw = encoded(value) if suffix == 'json' else value.encode()
    path = kind + '-' + hashlib.sha256(raw).hexdigest() + '.' + suffix
    return path, raw


def live_catalog(catalog_path, files):
    manifest = encoded({'presentation': 'original-templates', 'sports': {'nfl': '/' + catalog_path}})
    calls = []

    def read(path):
        calls.append(path)
        return manifest if path == 'original-manifest.json' else files[path]

    return read, calls


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.cache = self.root / 'cache'
        self.cache.mkdir()

    def site(self, name, files):
        root = self.root / name
        root.mkdir()
        for path, raw in files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        return root

    def test_first_build_keeps_previous_catalog_chain_and_reuses_current_hashes(self):
        shard, shard_raw = blob({'sections': {'key': {'x': [2], 'y': [50]}}}, 'data/series')
        game, game_raw = blob({'sections': [{'file': shard, 'key': 'key'}]}, 'data/game')
        old, old_raw = blob({'games': {'1': {'file': game}}, 'untrusted': '../../.env'})
        new, new_raw = blob({'games': {'1': {'file': game}}, 'captured_through': NOW.isoformat()})
        live = {old: old_raw, game: game_raw, shard: shard_raw}
        read, calls = live_catalog(old, live)
        site = self.site('first', {new: new_raw, game: game_raw, shard: shard_raw})
        (self.cache / 'nfl.sqlite').write_bytes(b'private raw history')
        (self.cache / '.env').write_bytes(b'private credentials')

        result = assets.retain_public_assets(site, self.cache, now=NOW, read=read)

        self.assertEqual(calls, ['original-manifest.json', old])
        self.assertEqual((site / old).read_bytes(), old_raw)
        self.assertEqual(result['retained_public_assets'], 1)
        self.assertFalse((site / '.env').exists())
        self.assertFalse((site / 'nfl.sqlite').exists())
        self.assertFalse((site / 'published-assets/index.json').exists())

    def test_retention_starts_when_asset_retires_and_expires_after_thirty_minutes(self):
        old, old_raw = blob({'games': {}})
        new, new_raw = blob({'games': {}, 'captured_through': NOW.isoformat()})
        script, script_raw = blob('window.test = true;', 'native/script', 'js')
        read, _ = live_catalog(old, {old: old_raw})
        first = self.site('first', {old: old_raw, script: script_raw})
        assets.retain_public_assets(first, self.cache, now=NOW, read=read)
        never_read = lambda path: self.fail('Existing retention cache must not fetch public files')

        # A delayed next build still grants old readers a full transition window.
        second = self.site('second', {new: new_raw})
        retired = NOW + timedelta(hours=1)
        assets.retain_public_assets(second, self.cache, now=retired, read=never_read)
        self.assertEqual((second / old).read_bytes(), old_raw)
        self.assertEqual((second / script).read_bytes(), script_raw)

        third = self.site('third', {new: new_raw})
        result = assets.retain_public_assets(third, self.cache, now=retired + timedelta(minutes=29), read=never_read)
        self.assertEqual(result['retained_public_assets'], 2)
        fourth = self.site('fourth', {new: new_raw})
        result = assets.retain_public_assets(fourth, self.cache, now=retired + timedelta(minutes=30), read=never_read)
        self.assertEqual(result['retained_public_assets'], 0)
        self.assertFalse((fourth / old).exists())
        self.assertFalse((self.cache / 'published-assets' / (old + '.gz')).exists())

    def test_reused_hash_is_deduplicated_and_becomes_current_again(self):
        old, old_raw = blob({'games': {}})
        new, new_raw = blob({'games': {'2': {}}})
        read, _ = live_catalog(old, {old: old_raw})
        assets.retain_public_assets(self.site('first', {old: old_raw}), self.cache, now=NOW, read=read)
        compressed_path = self.cache / 'published-assets' / (old + '.gz')
        compressed_before = compressed_path.read_bytes()
        self.assertEqual(gzip.decompress(compressed_before), old_raw)
        assets.retain_public_assets(self.site('second', {new: new_raw}), self.cache, now=NOW + timedelta(minutes=1))
        third = self.site('third', {old: old_raw, new: new_raw})
        result = assets.retain_public_assets(third, self.cache, now=NOW + timedelta(minutes=2))
        self.assertEqual(result['public_asset_cache_files'], 2)
        self.assertEqual(result['retained_public_assets'], 0)
        state = json.loads((self.cache / 'published-assets/index.json').read_bytes())
        self.assertEqual(state['assets'], {old: None, new: None})
        self.assertEqual(compressed_path.read_bytes(), compressed_before)

    def test_budgets_fail_before_output_or_existing_cache_changes(self):
        old, old_raw = blob({'games': {}})
        new, new_raw = blob({'games': {'2': {}}})
        read, _ = live_catalog(old, {old: old_raw})
        assets.retain_public_assets(self.site('first', {old: old_raw}), self.cache, now=NOW, read=read)
        index = self.cache / 'published-assets/index.json'
        saved = index.read_bytes()
        private = self.cache / 'nfl.sqlite'
        private.write_bytes(b'unchanged private source cache')
        for name, limit, value in [('cache', 'CACHE_LIMIT', len(private.read_bytes())),
                                   ('public', 'PUBLIC_LIMIT', len(new_raw) + len(old_raw) - 1)]:
            site = self.site('budget-' + name, {new: new_raw})
            with patch.object(assets, limit, value), self.assertRaises(BuildError):
                assets.retain_public_assets(site, self.cache, now=NOW + timedelta(minutes=1))
            self.assertEqual(index.read_bytes(), saved)
            self.assertFalse((site / old).exists())
            self.assertEqual(private.read_bytes(), b'unchanged private source cache')

    def test_initial_seed_rejects_path_traversal_and_bad_hashes(self):
        current, raw = blob({'games': {}})
        for name, bad in [('traversal', '../../.env'), ('hash', 'native/data-' + '0' * 64 + '.json')]:
            site = self.site(name, {current: raw})
            read, calls = live_catalog(bad, {bad: raw})
            with self.assertRaises(BuildError):
                assets.retain_public_assets(site, self.cache, now=NOW, read=read)
            self.assertFalse((self.cache / 'published-assets').exists())
            if name == 'traversal':
                self.assertEqual(calls, ['original-manifest.json'])

    def test_bootstrap_download_budget_is_bounded(self):
        old, old_raw = blob({'games': {}})
        new, new_raw = blob({'games': {'new': {}}})
        read, _ = live_catalog(old, {old: old_raw})
        site = self.site('first', {new: new_raw})
        with patch.object(assets, 'BOOTSTRAP_LIMIT', len(old_raw) - 1):
            result = assets.retain_public_assets(site, self.cache, now=NOW, read=read)
        self.assertEqual(result['retained_public_assets'], 0)
        self.assertIn('bootstrap-download-budget', result['public_asset_bootstrap']['warnings'])
        self.assertTrue((self.cache / 'published-assets/index.json').exists())

    def test_cached_payload_hash_is_verified_before_public_copy(self):
        old, old_raw = blob({'games': {}})
        new, new_raw = blob({'games': {'new': {}}})
        read, _ = live_catalog(old, {old: old_raw})
        assets.retain_public_assets(self.site('first', {old: old_raw}), self.cache, now=NOW, read=read)
        (self.cache / 'published-assets' / (old + '.gz')).write_bytes(gzip.compress(b'corrupted payload'))
        site = self.site('second', {new: new_raw})
        with self.assertRaises(BuildError):
            assets.retain_public_assets(site, self.cache, now=NOW + timedelta(minutes=1))
        self.assertFalse((site / old).exists())

    def test_missing_previous_hash_does_not_block_recovered_assets_or_retry_bootstrap(self):
        missing, missing_raw = blob({'missing': True})
        available, available_raw = blob({'sections': {'101': {'x': [1], 'y': [50]}}}, 'data/series')
        old, old_raw = blob({'references': [missing, available]})
        new, new_raw = blob({'games': {}})
        read, _ = live_catalog(old, {old: old_raw, available: available_raw})

        def partial(path):
            if path == missing:
                raise HTTPError(assets.PUBLIC_BASE + path, 404, 'missing', {}, None)
            return read(path)

        first = self.site('first', {new: new_raw})
        result = assets.retain_public_assets(first, self.cache, now=NOW, read=partial)
        self.assertEqual((first / old).read_bytes(), old_raw)
        self.assertEqual((first / available).read_bytes(), available_raw)
        self.assertFalse((first / missing).exists())
        self.assertEqual(result['public_asset_bootstrap']['unavailable_assets'], 1)
        self.assertEqual(result['public_asset_bootstrap']['unavailable_statuses'], {'404': 1})
        self.assertTrue(result['public_asset_bootstrap']['finished'])

        second = self.site('second', {new: new_raw})
        result = assets.retain_public_assets(second, self.cache, now=NOW + timedelta(minutes=1),
                    read=lambda path: self.fail('Partial bootstrap must not be repeated'))
        self.assertFalse(result['public_asset_bootstrapped_now'])
        self.assertEqual((second / available).read_bytes(), available_raw)

    def test_partial_time_budget_keeps_verified_recovery_and_finishes_bootstrap(self):
        first, first_raw = blob({'value': 1}, 'data/game')
        second, second_raw = blob({'value': 2}, 'data/game')
        old, old_raw = blob({'references': [first, second]})
        new, new_raw = blob({'games': {}})
        read, calls = live_catalog(old, {old: old_raw, first: first_raw, second: second_raw})
        site = self.site('first', {new: new_raw})
        with patch.object(assets.time, 'monotonic', side_effect=[0, 0, 0, assets.BOOTSTRAP_SECONDS]):
            result = assets.retain_public_assets(site, self.cache, now=NOW, read=read)
        self.assertEqual((site / old).read_bytes(), old_raw)
        self.assertEqual((site / first).read_bytes(), first_raw)
        self.assertFalse((site / second).exists())
        self.assertNotIn(second, calls)
        self.assertEqual(result['public_asset_bootstrap']['unvisited_assets'], 1)
        self.assertIn('bootstrap-time-budget', result['public_asset_bootstrap']['warnings'])
        self.assertTrue(result['public_asset_bootstrap']['finished'])

    def test_missing_manifest_finishes_bootstrap_but_redirect_remains_hard_failure(self):
        current, raw = blob({'games': {}})
        for code in (404, 301):
            cache = self.root / ('cache-' + str(code))
            site = self.site('site-' + str(code), {current: raw})
            def unavailable(path):
                raise HTTPError(assets.PUBLIC_BASE + path, code, 'unavailable', {}, None)
            if code == 301:
                with self.assertRaises(BuildError):
                    assets.retain_public_assets(site, cache, now=NOW, read=unavailable)
                self.assertFalse(cache.exists())
            else:
                result = assets.retain_public_assets(site, cache, now=NOW, read=unavailable)
                self.assertTrue(result['public_asset_bootstrap']['unavailable_manifest'])
                self.assertTrue((cache / 'published-assets/index.json').is_file())

    def test_invalid_gzip_and_oversized_decompression_are_hard_failures(self):
        old, old_raw = blob({'games': {}})
        new, new_raw = blob({'games': {'new': {}}})
        read, _ = live_catalog(old, {old: old_raw})
        assets.retain_public_assets(self.site('first', {old: old_raw}), self.cache, now=NOW, read=read)
        cached = self.cache / 'published-assets' / (old + '.gz')
        for name, raw in [('corrupt', b'invalid gzip'),
                          ('oversized', gzip.compress(b'x' * (assets.FILE_LIMIT + 1)))]:
            cached.write_bytes(raw)
            site = self.site(name, {new: new_raw})
            with self.assertRaises(BuildError):
                assets.retain_public_assets(site, self.cache, now=NOW + timedelta(minutes=1))
            self.assertFalse((site / old).exists())

    def test_cache_budget_counts_compressed_asset_bytes(self):
        current, raw = blob({'long': 'x' * 100000})
        read, _ = live_catalog(current, {current: raw})
        site = self.site('compressed', {current: raw})
        (self.cache / 'nfl.sqlite').write_bytes(b'private cache')
        with patch.object(assets, 'CACHE_LIMIT', 5000):
            result = assets.retain_public_assets(site, self.cache, now=NOW, read=read)
        self.assertGreater(result['current_public_asset_bytes'], 100000)
        self.assertLess(result['compressed_public_asset_cache_bytes'], 1000)
        self.assertEqual(result['raw_source_cache_bytes'], len(b'private cache'))
        self.assertLessEqual(result['combined_source_cache_bytes'], 5000)
        self.assertEqual(result['combined_source_cache_bytes'], assets.tree_bytes(self.cache))

    def test_publisher_build_keeps_retention_outside_validated_public_output(self):
        from tools import build_original_fast, free_refresh_publish as publisher
        from tests.test_original_static import fixture
        site = self.root / 'mounted'
        retain = assets.retain_public_assets

        def retained(output, cache):
            return retain(output, cache, now=NOW,
                          read=lambda path: (Path(output) / path).read_bytes())

        with patch.object(build_original_fast, 'build', side_effect=fixture), \
             patch.object(assets, 'retain_public_assets', side_effect=retained):
            report = publisher.build(site, self.cache, scheduled=True)

        self.assertGreater(report['public_asset_cache_files'], 10)
        self.assertEqual(report['retained_public_assets'], 0)
        self.assertTrue((self.cache / 'published-assets/index.json').is_file())
        self.assertFalse((site / 'published-assets').exists())
        self.assertTrue(json.loads((site / 'original-manifest.json').read_bytes())['live_updates_enabled'])


if __name__ == '__main__':
    unittest.main()
