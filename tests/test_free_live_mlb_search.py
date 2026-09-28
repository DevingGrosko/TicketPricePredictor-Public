from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
import unittest


class SearchFallbackTest(unittest.TestCase):
    def test_search_does_not_require_formatted_date_and_failure_does_not_abort_fallback(self):
        import collector
        from tools.free_live_mlb_schedule import capture_game
        queries=[]
        url='https://www.vividseats.com/old-1-1-2000--sports-mlb-baseball/production/123'
        class Browser:
            def __init__(self,**kwargs): self.driver=SimpleNamespace(get=lambda url:None)
            def discover_event_urls(self,search):
                queries.append(parse_qs(urlsplit(search).query)['searchTerm'][0])
                if len(queries)==1: raise TimeoutError('fixture discovery timeout')
                return {url}
            def capture(self,url): return {},'provider-time'
            def close(self): pass
        game={'away_team':'San Diego Padres','home_team':'Colorado Rockies','schedule_id':'123'}
        with patch.object(collector,'VividBrowser',Browser),patch(
                'tools.free_live_mlb_schedule.validate_match',return_value=('snapshot','official-time')):
            self.assertEqual(capture_game(game,True,1), (url,'official-time','snapshot'))
        self.assertEqual(queries,['San Diego Padres at Colorado Rockies','San Diego Padres Colorado Rockies'])
