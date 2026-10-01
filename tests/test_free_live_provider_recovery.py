import base64
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools import free_live_provider_recovery as r

URL = 'https://www.vividseats.com/example/production/123'
API = 'https://www.vividseats.com/hermes/api/v1/listings?productionId=123'
AT = datetime(2026, 10, 4, 17, tzinfo=timezone.utc)


def message(method, **params):
    return {'message': json.dumps({'message': {'method': method, 'params': params}})}


def received(status=200, url=API, identity='listing'):
    return message('Network.responseReceived', requestId=identity, type='XHR',
                   response={'url': url, 'status': status, 'mimeType': 'application/json'})


def payload(identity='123', tickets=None):
    return {'global': [{'productionId': identity, 'productionName': 'A at B', 'mapTitle': 'Venue'}],
            'tickets': [{'l': 'Section 1', 'p': 25}] if tickets is None else tickets}


class Clock:
    def __init__(self):
        self.value = 0

    def monotonic(self):
        self.value += 0.01
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Driver:
    title = 'Normal event page'

    def __init__(self, batches, bodies):
        self.batches = iter([[], *batches])
        self.bodies = iter(bodies)
        self.reads = 0
        self.commands = []
        self.last = None

    def get_log(self, kind):
        return next(self.batches, [])

    def get(self, url):
        self.visited = url

    def execute_cdp_cmd(self, command, arguments):
        self.commands.append((command, arguments))
        if command == 'Network.enable':
            return {}
        assert command == 'Network.getResponseBody'
        self.reads += 1
        value = next(self.bodies, self.last)
        self.last = value
        if isinstance(value, Exception):
            raise value
        return {'body': json.dumps(value), 'base64Encoded': False}


@pytest.fixture
def environment(monkeypatch):
    import nfl_collector as n
    clock = Clock()
    monkeypatch.setattr(r.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(r.time, 'sleep', clock.sleep)
    monkeypatch.setattr(n, 'extract_map_geometry_from_json', lambda *a, **k: None)
    monkeypatch.setattr(n, 'choose_best_geometry', lambda *a: None)
    monkeypatch.setattr(n, 'geometry_is_usable', lambda *a: False)
    monkeypatch.setattr(n, 'geometry_section_count', lambda *a: 0)
    monkeypatch.setattr(n, 'MAP_GEOMETRY_SETTLE_SECONDS', 0)
    return n


def browser(batches=None, bodies=None):
    driver = Driver(batches or [[received()]], bodies or [payload()])
    return SimpleNamespace(driver=driver, timeout=3,
        _looks_like_map_response=lambda u, m: False,
        _response_text=lambda i: None,
        _event_datetime=lambda u: AT,
        _geometry_from_response=lambda *a: None,
        _dom_map_geometry=lambda *a: None,
        _open_map_view=lambda: False)


def test_reads_response_when_completion_event_is_not_in_same_poll(environment):
    b = browser()
    result, stamp = r.capture(b, URL)
    assert result['tickets'] and stamp == AT


def test_retries_local_body_read_without_reloading_page(environment):
    b = browser(bodies=[RuntimeError('No resource with given identifier found'), payload()])
    result, stamp = r.capture(b, URL)
    assert b.driver.reads == 2
    assert b.capture_diagnostics['body_read_retries'] == 1
    assert result['global'][0]['productionId'] == '123'


def test_loading_finished_first_does_not_lose_later_response(environment):
    b = browser(batches=[[message('Network.loadingFinished', requestId='listing')], [received()]])
    assert r.capture(b, URL)[1] == AT


@pytest.mark.parametrize('status,category', [(401, 'access-denied'), (403, 'access-denied'),
                                           (429, 'rate-limited'), (503, 'provider-server-error')])
def test_http_failures_are_explicit(environment, status, category):
    b = browser(batches=[[received(status)]])
    with pytest.raises(r.ProviderCaptureError) as exc:
        r.capture(b, URL)
    assert exc.value.category == category
    assert r.retryable(exc.value) == (status == 503)
    assert b.driver.reads == 0


def test_empty_inventory_is_not_a_success_or_a_timeout(environment):
    b = browser(bodies=[payload(tickets=[])])
    with pytest.raises(r.ProviderCaptureError) as exc:
        r.capture(b, URL)
    assert exc.value.category == 'empty-inventory'
    assert not r.retryable(exc.value)


def test_wrong_production_id_is_never_accepted(environment):
    b = browser(bodies=[payload('999')])
    with pytest.raises(r.ProviderCaptureError) as exc:
        r.capture(b, URL)
    assert exc.value.diagnostics['rejected_production_id'] == '999'


def test_badging_endpoint_is_not_confused_with_inventory(environment):
    b = browser(batches=[[received(403, 'https://www.vividseats.com/hermes/api/v1/badging/productions/123/sold/listings', 'badge'), received()]])
    result, _ = r.capture(b, URL)
    assert result['tickets']
    assert len(b.capture_diagnostics['responses']) == 1


def test_real_listings_are_preserved_when_map_is_unavailable(environment):
    result, _ = r.capture(browser(), URL)
    assert result['_map_geometry_diagnostics']['status'] == 'unavailable'
    assert result['tickets']


def test_usable_geometry_is_preserved(environment, monkeypatch):
    geometry = {'source': 'test-map', 'coverage_ratio': 1}
    monkeypatch.setattr(environment, 'choose_best_geometry', lambda *a: geometry)
    monkeypatch.setattr(environment, 'geometry_is_usable', lambda *a: True)
    monkeypatch.setattr(environment, 'geometry_section_count', lambda *a: 10)
    result, _ = r.capture(browser(), URL)
    assert result['_map_geometry'] is geometry
    assert result['_map_geometry_diagnostics']['mapped_sections'] == 10


def test_metadata_timeout_identified_separately(environment):
    b = browser()
    def missing(url):
        raise ValueError('Could not determine the event date and time from the Vivid page.')
    b._event_datetime = missing
    with pytest.raises(r.ProviderCaptureError) as exc:
        r.capture(b, URL)
    assert exc.value.category == 'metadata-timeout'


def test_body_unavailable_is_reported_without_query_strings(environment):
    b = browser(bodies=[RuntimeError('https://example.test/path?secret=do-not-log')])
    with pytest.raises(r.ProviderCaptureError) as exc:
        r.capture(b, URL)
    assert 'do-not-log' not in str(exc.value)
    assert exc.value.diagnostics['body_read_retries'] > 1
    assert exc.value.diagnostics['pending_requests'][0]['status'] == 200


def test_base64_body():
    d = Mock()
    d.execute_cdp_cmd.return_value = {'body': base64.b64encode(json.dumps(payload()).encode()).decode(), 'base64Encoded': True}
    assert r.response_payload(d, 'a') == payload()


@pytest.mark.parametrize('sport', ['nfl', 'nhl'])
def test_both_sports_retry_timeouts_and_keep_official_time(monkeypatch, sport):
    import nfl_schedule_collector as nfl
    import nhl_schedule_collector as nhl
    module = nfl if sport == 'nfl' else nhl
    parser = module.NFLSnapshotParser if sport == 'nfl' else module.NHLSnapshotParser
    first, second = Mock(), Mock()
    first.capture.side_effect = TimeoutError('provider stalled')
    second.capture.return_value = ({}, AT)
    second.capture_diagnostics = {}
    factory = Mock(side_effect=[first, second])
    monkeypatch.setattr(module, 'VividNFLBrowser', factory)
    snapshot = SimpleNamespace(title='Correct teams')
    monkeypatch.setattr(parser, 'parse', Mock(return_value=snapshot))
    validate = Mock()
    monkeypatch.setattr(module, 'validate_captured_match', validate)
    sleep = Mock()
    monkeypatch.setattr(r.time, 'sleep', sleep)
    resolution = SimpleNamespace(game=SimpleNamespace(schedule_id='game', event_date=AT),
                                 candidates=[SimpleNamespace(url=URL)])
    result = r.capture_resolution(sport, resolution, headless=True, timeout=35)
    assert result == (URL, AT, snapshot)
    assert factory.call_count == 2
    first.close.assert_called_once()
    second.close.assert_called_once()
    sleep.assert_called_once_with(2)
    validate.assert_called_once_with(resolution.game, AT, snapshot.title)


@pytest.mark.parametrize('sport', ['nfl', 'nhl'])
def test_validation_mismatch_not_retried(monkeypatch, sport):
    import nfl_schedule_collector as nfl
    import nhl_schedule_collector as nhl
    module = nfl if sport == 'nfl' else nhl
    parser = module.NFLSnapshotParser if sport == 'nfl' else module.NHLSnapshotParser
    b = Mock()
    b.capture.return_value = ({}, AT)
    factory = Mock(return_value=b)
    monkeypatch.setattr(module, 'VividNFLBrowser', factory)
    monkeypatch.setattr(parser, 'parse', Mock(return_value=SimpleNamespace(title='Wrong teams')))
    monkeypatch.setattr(module, 'validate_captured_match', Mock(side_effect=ValueError('Wrong teams')))
    sleep = Mock()
    monkeypatch.setattr(r.time, 'sleep', sleep)
    resolution = SimpleNamespace(game=SimpleNamespace(schedule_id='game', event_date=AT), candidates=[SimpleNamespace(url=URL)])
    with pytest.raises(r.ProviderCaptureError) as exc:
        r.capture_resolution(sport, resolution, headless=True, timeout=35)
    assert factory.call_count == 1
    assert exc.value.diagnostics['attempts'][0]['will_retry'] is False
    b.close.assert_called_once()
    sleep.assert_not_called()


def test_exhausted_transport_retries_stay_failed(monkeypatch):
    import nfl_schedule_collector as nfl
    b = Mock()
    b.capture.side_effect = TimeoutError('stalled')
    factory = Mock(return_value=b)
    monkeypatch.setattr(nfl, 'VividNFLBrowser', factory)
    sleep = Mock()
    monkeypatch.setattr(r.time, 'sleep', sleep)
    resolution = SimpleNamespace(game=SimpleNamespace(schedule_id='game', event_date=AT), candidates=[SimpleNamespace(url=URL)])
    with pytest.raises(r.ProviderCaptureError) as exc:
        r.capture_resolution('nfl', resolution, headless=True, timeout=35)
    assert factory.call_count == 3 and b.close.call_count == 3
    assert [c.args[0] for c in sleep.call_args_list] == [2, 5]
    assert len(exc.value.diagnostics['attempts']) == 3


def test_context_restores_legacy_code_and_does_not_change_cadence():
    import nfl_collector as b
    import nfl_schedule_collector as nfl
    import nhl_schedule_collector as nhl
    originals = (b.VividNFLBrowser.capture, nfl._capture_resolution, nhl._capture_resolution)
    cadence = (nfl.schedule_games_due, nhl.schedule_games_due)
    with r.provider_recovery():
        assert b.VividNFLBrowser.capture is r.capture
        assert (nfl.schedule_games_due, nhl.schedule_games_due) == cadence
    assert (b.VividNFLBrowser.capture, nfl._capture_resolution, nhl._capture_resolution) == originals


def test_entrypoint_rejects_paused_mlb(tmp_path):
    with pytest.raises(ValueError, match='MLB is paused'):
        r.run('mlb', tmp_path)
    assert not list(tmp_path.iterdir())


def test_health_enrichment_does_not_turn_failures_green(tmp_path, monkeypatch):
    from tools import free_live_hardening as h
    from tools.free_live_collect import write_json, read_json
    def failed(sport, directory):
        write_json(Path(directory) / 'health.json', {'status': 'degraded', 'failed': 1, 'committed': 2})
        return 1
    from pathlib import Path
    monkeypatch.setattr(h, 'run', failed)
    assert r.run('nhl', tmp_path) == 1
    health = read_json(tmp_path / 'health.json')
    assert health['status'] == 'degraded' and health['failed'] == 1 and health['committed'] == 2
    assert 'provider_recovery' in health
