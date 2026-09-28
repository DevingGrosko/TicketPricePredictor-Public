from types import SimpleNamespace
from unittest.mock import patch
import unittest


class MetadataIsolationTest(unittest.TestCase):
    def test_each_candidate_starts_blank_and_uses_page_metadata(self):
        import collector
        from tools.free_live_mlb_schedule import capture_game
        seen = []
        class Browser:
            def __init__(self, **kwargs):
                self.driver = SimpleNamespace(get=lambda url: seen.append(('get', url)))
            def _event_datetime(self, url):
                seen.append(('metadata', url))
                return 'verified-date'
            def capture(self, url):
                if seen != [('get', 'about:blank')]:
                    raise AssertionError('Previous page was not cleared')
                return {}, self._event_datetime(url)
            def close(self):
                seen.append(('close', None))
        url = 'https://www.vividseats.com/old-1-1-2000--sports-mlb-baseball/production/123'
        with patch.object(collector, 'VividBrowser', Browser), patch(
                'tools.free_live_mlb_schedule.validate_match', return_value=('snapshot', 'official-date')):
            self.assertEqual(capture_game({}, True, 1, url), (url, 'official-date', 'snapshot'))
        self.assertEqual(seen, [('get', 'about:blank'), ('metadata', ''), ('close', None)])
