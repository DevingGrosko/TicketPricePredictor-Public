from pathlib import Path
import tempfile
import unittest

from Flask_App.performance_cache import ProcessTTLCache, file_version


class ProcessTTLCacheTests(unittest.TestCase):
    def test_reuses_value_until_tag_is_invalidated(self):
        cache = ProcessTTLCache(max_entries=4)
        calls = []

        def build():
            calls.append(len(calls) + 1)
            return {"build": calls[-1]}

        first = cache.get_or_create("report", build, tags=("mlb",), ttl_seconds=60)
        second = cache.get_or_create("report", build, tags=("mlb",), ttl_seconds=60)
        self.assertIs(first, second)
        self.assertEqual(calls, [1])

        self.assertEqual(cache.invalidate_tag("MLB"), 1)
        third = cache.get_or_create("report", build, tags=("mlb",), ttl_seconds=60)
        self.assertEqual(third, {"build": 2})
        self.assertEqual(calls, [1, 2])

    def test_builder_failure_is_not_cached(self):
        cache = ProcessTTLCache(max_entries=4)
        calls = 0

        def build():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("planned failure")
            return "ok"

        with self.assertRaisesRegex(RuntimeError, "planned failure"):
            cache.get_or_create("key", build, ttl_seconds=60)
        self.assertEqual(cache.get_or_create("key", build, ttl_seconds=60), "ok")
        self.assertEqual(calls, 2)

    def test_file_version_is_path_scoped_and_changes_with_file(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.db"
            second = Path(directory) / "second.db"
            first.write_bytes(b"one")
            second.write_bytes(b"one")

            first_version = file_version(first)
            second_version = file_version(second)
            self.assertNotEqual(first_version, second_version)

            first.write_bytes(b"a longer database value")
            self.assertNotEqual(first_version, file_version(first))


if __name__ == "__main__":
    unittest.main()
