from types import SimpleNamespace
from unittest.mock import Mock
from tools.free_live_mlb_schedule import provider_order


def test_result_order_does_not_follow_misleading_url_slugs():
    browser=SimpleNamespace(driver=SimpleNamespace(execute_script=Mock(return_value=['z-current','a-spring','z-current'])))
    assert provider_order(browser, {'a-spring','z-current','c-not-rendered'}) == ['z-current','a-spring','c-not-rendered']


def test_unavailable_dom_keeps_every_candidate():
    browser=SimpleNamespace(driver=SimpleNamespace(execute_script=Mock(side_effect=RuntimeError('navigation'))))
    assert provider_order(browser, {'b','a'}) == ['a','b']
