from datetime import datetime, timedelta, timezone
import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from collector import VividBrowser
from nfl_collector import VividNFLBrowser
from vivid_inventory import CurrentInventoryRecovery, VividCaptureError
from vivid_performer_routes import configure_schedule_navigation
from vivid_webkit import PUBLIC_PAGE_SCRIPT, PublicDOMDriver, WebKitInventorySession, public_inventory, verified_event_date

AT = datetime(2026, 10, 11, 17, tzinfo=timezone.utc)
URL = 'https://www.vividseats.com/old-slug-3-5-2027/production/123'
PERFORMER = 'https://www.vividseats.com/new-orleans-saints-tickets--sports-nfl-football/performer/597'


def payload(pid='123'):
    return {'global': [{'productionId': pid, 'productionName': 'Minnesota Vikings at New Orleans Saints',
        'mapTitle': 'Arena', 'venueId': '100', 'listingCount': '12'}],
        'tickets': [{'l': 'Section'+str(i), 'p': '25.10', 'aip': '29.90', 'q': '2', 'r': 'A'} for i in range(12)]}


def metadata(pid='123'):
    return {'id': pid, 'page_id': pid, 'query_id': pid, 'utc_date': AT.isoformat(),
            'title': 'Minnesota Vikings at New Orleans Saints', 'venue': 'Arena', 'venue_id': '100'}


class Clock:
    value = 0
    def monotonic(self): return self.value
    def time(self): return 1000 + self.value
    def advance(self, milliseconds): self.value += milliseconds/1000


class Response:
    def __init__(self, context, page, status, *, document=False, when=None, query='', method='GET', pid=None):
        pid = pid or context.browser.pid
        self.url = page.url if document else 'https://www.vividseats.com/hermes/api/v1/listings?productionId='+pid+query
        self.status = status
        self.request = SimpleNamespace(url=self.url, method=method,
            resource_type='document' if document else 'xhr',
            timing={'startTime': context.clock.time()*1000+1 if when is None else when}, frame=page.main_frame)
        # SimpleNamespace is unhashable; native Playwright Request objects are hashable.
        self.request = type('Request', (), self.request.__dict__)()
        self.context, self.pid = context, pid
    def body(self):
        if self.status != 200:
            raise AssertionError('Never read an error response body')
        self.context.body_reads.append(self.pid)
        return json.dumps(self.context.browser.body or payload(self.pid)).encode()


class Link:
    def __init__(self, page): self.page = page
    def evaluate(self, _script, **options):
        if self.page.context.browser.fail_phase == 'performer-link-scan':
            self.page.context.clock.advance(options['timeout'])
            raise self.page.context.browser.timeout_error('Private timeout message must not enter diagnostics')
        return self.page.context.popup.url
    def get_attribute(self, _name, **_options): return '_blank'
    def is_visible(self): return True
    def is_enabled(self, **_options): return True
    def click(self, **options):
        self.page.context.clicks += 1
        if self.page.context.browser.fail_phase == 'event-link-click':
            self.page.context.clock.advance(options['timeout'])
            raise self.page.context.browser.timeout_error('Private timeout message must not enter diagnostics')
        self.page.context.emit_event()


class Page:
    def __init__(self, context, url='about:blank', *, event=False):
        self.context, self.url, self.event = context, url, event
        self.main_frame = SimpleNamespace(page=self)
        self.reloads = 0
    def goto(self, url, **options):
        self.url = url
        self.context.navigations.append(url)
        response = Response(self.context, self, self.context.browser.performer_status, document=True)
        self.context.emit(response)
        if self.context.browser.iframe_status:
            iframe=Response(self.context,self,self.context.browser.iframe_status,document=True)
            iframe.request.frame=SimpleNamespace(page=self)
            self.context.emit(iframe)
        # A same-ID prefetch must not satisfy a later clicked event observation.
        self.context.emit(Response(self.context, self, 200, when=self.context.clock.time()*1000-1))
        self.context.browser.navigation_options.append(options)
        self.context.clock.advance(self.context.browser.performer_delay_ms)
        if (self.context.browser.fail_phase == 'performer-navigation'
                or self.context.browser.load_stall and options.get('wait_until') == 'load'):
            self.context.clock.advance(options['timeout']-self.context.browser.performer_delay_ms)
            raise self.context.browser.timeout_error('Private timeout message must not enter diagnostics')
        return response
    def evaluate(self, script, *_args):
        if script == PUBLIC_PAGE_SCRIPT:
            return {'challenge_visible': self.context.browser.challenge,
                    'ready_state': self.context.browser.ready_state,
                    'event': (self.context.browser.metadata or metadata(self.context.browser.pid)) if self.event else None}
        return 'public-eval'
    def locator(self, _selector):
        return SimpleNamespace(count=lambda: 1, nth=lambda _index: Link(self))
    def expect_popup(self, **_options):
        context = self.context
        class Popup:
            def __enter__(self): return SimpleNamespace(value=context.popup)
            def __exit__(self, *_args):
                if context.browser.fail_phase == 'event-popup':
                    context.clock.advance(_options['timeout'])
                    raise context.browser.timeout_error('Private timeout message must not enter diagnostics')
                return False
        return Popup()
    def wait_for_load_state(self, *_args, **options):
        self.context.browser.popup_dom_options.append(options)
        if self.context.browser.fail_phase == 'event-domcontentloaded':
            self.context.clock.advance(options['timeout'])
            raise self.context.browser.timeout_error('Private timeout message must not enter diagnostics')
    def wait_for_timeout(self, milliseconds):
        self.context.clock.advance(milliseconds)
        if self.context.pending_finish and self.context.clock.value >= self.context.browser.delayed_finish_seconds:
            for request in self.context.pending_finish:
                self.context.callbacks['requestfinished'](request)
            self.context.pending_finish=[]
    def reload(self, **_options):
        self.reloads += 1
        self.context.emit_event()
    def content(self): return '<html>public feed</html>'
    def screenshot(self, **_options): pass


class Context:
    def __init__(self, browser, clock):
        self.browser, self.clock = browser, clock
        self.callbacks, self.removed, self.body_reads, self.navigations = {}, [], [], []
        self.clicks = 0
        self.pending_finish=[]
        self.popup = Page(self, 'https://www.vividseats.com/actual-event/production/'+browser.pid, event=True)
    def set_default_timeout(self, _timeout): pass
    def set_default_navigation_timeout(self, _timeout): pass
    def on(self, kind, callback): self.callbacks[kind] = callback
    def remove_listener(self, kind, callback):
        self.removed.append(kind)
        if self.callbacks.get(kind) is callback: self.callbacks.pop(kind)
    def new_page(self): return Page(self)
    def emit(self, response):
        if 'response' in self.callbacks: self.callbacks['response'](response)
        if 'requestfinished' in self.callbacks:
            if response.request.resource_type=='xhr' and response.status==200 and self.browser.delayed_finish_seconds:
                self.pending_finish.append(response.request)
            else:
                self.callbacks['requestfinished'](response.request)
    def emit_event(self):
        status = self.browser.statuses.pop(0) if len(self.browser.statuses)>1 else self.browser.statuses[0]
        self.emit(Response(self, self.popup, 200, document=True))
        if self.browser.fail_phase == 'inventory-wait':
            return
        if self.browser.unrelated_status:
            self.emit(Response(self, self.popup, self.browser.unrelated_status, pid='999'))
        self.emit(Response(self, self.popup, status, query=self.browser.query))
        if self.browser.late200:
            self.emit(Response(self,self.popup,200))


class Browser:
    version = '26.6'
    def __init__(self, clock, pid='123', statuses=(200,), **options):
        self.pid, self.statuses, self.closed = pid, list(statuses), False
        self.performer_status, self.challenge = options.get('performer_status', 200), options.get('challenge', False)
        self.body, self.metadata = options.get('body'), options.get('metadata')
        self.query, self.unrelated_status = options.get('query', ''), options.get('unrelated_status')
        self.delayed_finish_seconds, self.late200=options.get('delayed_finish_seconds',0),options.get('late200',False)
        self.fail_phase = options.get('fail_phase')
        self.load_stall = options.get('load_stall',False)
        self.ready_state = options.get('ready_state','interactive')
        self.performer_delay_ms = options.get('performer_delay_ms',0)
        self.iframe_status=options.get('iframe_status')
        self.navigation_options,self.popup_dom_options = [],[]
        self.context = Context(self, clock)
    def new_context(self): return self.context
    def close(self): self.closed = True


def session(clock, browsers):
    value = WebKitInventorySession.__new__(WebKitInventorySession)
    value.owner = SimpleNamespace(); value.headless=False; value.timeout=8
    value.timeout_error = type('PlaywrightTimeout', (Exception,), {})
    for browser in browsers:
        browser.timeout_error=value.timeout_error
    value.playwright = SimpleNamespace(webkit=SimpleNamespace(launch=Mock(side_effect=browsers)), stop=Mock())
    value.runtime = {'engine':'webkit', 'playwright':'1.63.0', 'headed':True}
    value.routes, value.expected_dates = {}, {}
    value.event_browser=value.event_context=value.event_page=value.event_pid=None
    value.discovery_browser=value._discovery_page=None; value.generation=0
    value.owner.driver = PublicDOMDriver(value)
    value.configure_normal_navigation({'123':PERFORMER}, {'123':AT})
    return value


class WebKitTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock()
        self.addCleanup(patch.stopall)
        patch('vivid_webkit._blocked_category',None).start()
        patch('vivid_webkit.time.monotonic', self.clock.monotonic).start()
        patch('vivid_webkit.time.time', self.clock.time).start()

    def test_native_full_popup_capture_ignores_prefetch_and_redacts_public_fields(self):
        raw=payload();raw['private_token']='private';raw['tickets'][0]['seller_id']='private'
        browser=Browser(self.clock, body=raw);adapter=session(self.clock,[browser])
        clean, stamp=adapter.capture(URL)
        self.assertEqual(stamp,AT);self.assertEqual(len(clean['tickets']),12)
        self.assertNotIn('private',json.dumps(clean));self.assertEqual(browser.context.body_reads,['123'])
        self.assertEqual(browser.context.clicks,1)
        self.assertEqual(set(browser.context.removed),{'response','requestfinished'})
        self.assertEqual(adapter.owner.capture_diagnostics['document_status'],200)
        self.assertTrue(adapter.owner.capture_diagnostics['event_opened_native_popup'])
        self.assertNotIn('_map_geometry',clean)
        self.assertEqual(clean['_map_geometry_diagnostics']['status'],'unavailable')
        adapter.close();self.assertTrue(browser.closed);adapter.playwright.stop.assert_called_once()

    def test_dom_ready_performer_captures_native_inventory_even_when_full_load_would_stall(self):
        browser=Browser(self.clock,load_stall=True,ready_state='interactive',iframe_status=502)
        adapter=session(self.clock,[browser])
        raw,stamp=adapter.capture(URL)
        self.assertEqual(stamp,AT);self.assertEqual(len(raw['tickets']),12)
        self.assertEqual(browser.navigation_options,[dict(wait_until='domcontentloaded',timeout=8000)])
        self.assertEqual(browser.context.clicks,1);self.assertEqual(browser.context.body_reads,['123'])
        diagnostic=adapter.owner.capture_diagnostics
        self.assertEqual(diagnostic['performer_document_status'],200)  # The iframe502 cannot replace it.
        self.assertTrue(diagnostic['performer_domcontentloaded'])
        self.assertEqual(diagnostic['performer_ready_state_at_click'],'interactive')
        self.assertEqual(diagnostic['phase'],'complete')
        self.assertIn('event-identity',diagnostic['phase_times_ms'])
        adapter.close();self.assertTrue(browser.closed)

    def test_timeout_phase_is_retained_without_private_error_text_and_owned_resources_close(self):
        for phase in ('performer-navigation','performer-link-scan','event-link-click',
                      'event-popup','event-domcontentloaded','inventory-wait'):
            with self.subTest(phase=phase):
                clock=Clock()
                with patch('vivid_webkit.time.monotonic',clock.monotonic),patch('vivid_webkit.time.time',clock.time):
                    browser=Browser(clock,fail_phase=phase);adapter=session(clock,[browser])
                    with self.assertRaises(VividCaptureError) as error:adapter.capture(URL)
                    self.assertEqual(error.exception.category,'provider-inventory-timeout')
                    diagnostic=adapter.owner.capture_diagnostics
                    self.assertEqual(diagnostic['timeout_phase'],phase)
                    self.assertEqual(diagnostic['phase'],phase)
                    self.assertLessEqual(diagnostic['capture_elapsed_ms'],8000)
                    self.assertNotIn('Private',json.dumps(diagnostic))
                    self.assertLessEqual(len(diagnostic['phase_times_ms']),10)
                    self.assertTrue(browser.closed);self.assertEqual(browser.context.body_reads,[])
                    self.assertEqual(set(browser.context.removed),{'response','requestfinished'})
                    adapter.close()

    def test_popup_dom_wait_uses_only_remaining_budget_after_a_slow_performer(self):
        browser=Browser(self.clock,performer_delay_ms=7500,fail_phase='event-domcontentloaded')
        adapter=session(self.clock,[browser])
        with self.assertRaises(VividCaptureError):adapter.capture(URL)
        self.assertEqual(browser.popup_dom_options,[dict(timeout=500)])
        self.assertEqual(adapter.owner.capture_diagnostics['capture_elapsed_ms'],8000)
        self.assertEqual(adapter.owner.capture_diagnostics['timeout_phase'],'event-domcontentloaded')
        self.assertTrue(browser.closed);adapter.close()

    def test_response_denial_remains_authoritative_when_performer_navigation_also_times_out(self):
        for status in (401,403,429):
            with self.subTest(status=status),patch('vivid_webkit._blocked_category',None):
                browser=Browser(self.clock,performer_status=status,fail_phase='performer-navigation')
                adapter=session(self.clock,[browser])
                with self.assertRaises(VividCaptureError) as error:adapter.capture(URL)
                expected='provider-rate-limited' if status==429 else 'provider-access-denied'
                self.assertEqual(error.exception.category,expected);self.assertFalse(error.exception.retryable)
                self.assertEqual(adapter.owner.capture_diagnostics['performer_document_status'],status)
                self.assertEqual(browser.context.clicks,0);self.assertEqual(browser.context.body_reads,[])
                self.assertTrue(browser.closed);adapter.close()

    def test_each_normal_capture_uses_fresh_owned_process_even_same_game(self):
        browsers=[Browser(self.clock),Browser(self.clock)];adapter=session(self.clock,browsers)
        adapter.capture(URL);adapter.capture(URL)
        self.assertTrue(browsers[0].closed);self.assertFalse(browsers[1].closed)
        self.assertEqual(adapter.playwright.webkit.launch.call_count,2)
        adapter.close();self.assertTrue(browsers[1].closed)

    def test_current404_recovery_reloads_same_popup_only_once(self):
        browser=Browser(self.clock,statuses=(404,200));adapter=session(self.clock,[browser])
        sleep=Mock()
        recovery=CurrentInventoryRecovery(AT,72,now=lambda:AT-timedelta(hours=1),sleep=sleep)
        # The shared owner receives diagnostics exactly as VividNFLBrowser does.
        adapter.owner.capture=adapter.capture
        raw,stamp=recovery.capture(adapter.owner,URL)
        self.assertEqual(stamp,AT);self.assertEqual(len(raw['tickets']),12)
        self.assertEqual(adapter.playwright.webkit.launch.call_count,1)
        self.assertEqual(browser.context.popup.reloads,1);self.assertEqual(browser.context.clicks,1)
        sleep.assert_called_once_with(15)
        self.assertTrue(adapter.owner.capture_diagnostics['inventory_recovery']['recovered'])
        adapter.close()

    def test_denial_or_visible_challenge_stops_before_event_click_and_closes(self):
        for options in ({'performer_status':403},{'challenge':True},{'unrelated_status':429}):
            with self.subTest(options=options),patch('vivid_webkit._blocked_category',None):
                browser=Browser(self.clock,**options);adapter=session(self.clock,[browser])
                with self.assertRaises(VividCaptureError) as error:adapter.capture(URL)
                self.assertIn(error.exception.category,('provider-access-denied','provider-rate-limited'))
                self.assertEqual(browser.context.body_reads,[]);self.assertTrue(browser.closed)
                if not options.get('unrelated_status'):self.assertEqual(browser.context.clicks,0)
                adapter.close()

    def test_404_does_not_win_over_a_later200_headers_while_native_body_is_finishing(self):
        browser=Browser(self.clock,statuses=(404,),late200=True,delayed_finish_seconds=6)
        adapter=session(self.clock,[browser])
        clean,stamp=adapter.capture(URL)
        self.assertEqual(stamp,AT);self.assertEqual(len(clean['tickets']),12)
        self.assertGreaterEqual(self.clock.value,6)
        self.assertEqual([row['status'] for row in adapter.owner.capture_diagnostics['responses']],[404,200])
        self.assertEqual(browser.context.body_reads,['123']);adapter.close()

    def test_real_denial_latches_across_new_owned_instances_before_runtime_or_navigation(self):
        first=Browser(self.clock,performer_status=403);adapter=session(self.clock,[first])
        with self.assertRaises(VividCaptureError):adapter.capture(URL)
        self.assertTrue(first.closed)
        with self.assertRaises(VividCaptureError) as error:
            WebKitInventorySession(SimpleNamespace(),headless=False,timeout=8)
        self.assertEqual(error.exception.category,'provider-access-denied')
        second=Browser(self.clock);next_adapter=session(self.clock,[second])
        with self.assertRaises(VividCaptureError):next_adapter.capture(URL)
        next_adapter.playwright.webkit.launch.assert_not_called()
        self.assertEqual(second.context.navigations,[])
        adapter.close();next_adapter.close()

    def test_filtered_wrong_pid_count_and_wrong_metadata_never_return_snapshot(self):
        cases=[({'query':'&quantity=2'},'filtered-inventory-only'),
               ({'body':payload('999')},'inventory-identity-mismatch'),
               ({'body':{**payload(),'tickets':payload()['tickets'][:-1]}},'incomplete-inventory'),
               ({'metadata':{**metadata(),'utc_date':'2027-03-05T17:00:00Z'}},'event-metadata-time-mismatch'),
               ({'metadata':{**metadata(),'venue_id':'999'}},'event-metadata-identity-mismatch')]
        for options,category in cases:
            with self.subTest(category=category):
                browser=Browser(self.clock,**options);adapter=session(self.clock,[browser])
                with self.assertRaises(VividCaptureError) as error:adapter.capture(URL)
                self.assertEqual(error.exception.category,category);self.assertTrue(browser.closed)
                adapter.close()

    def test_expected_dates_are_required_and_naive_dates_rejected_before_navigation(self):
        adapter=session(self.clock,[])
        for dates in ({},{'123':AT.replace(tzinfo=None)},{'999':AT}):
            with self.subTest(dates=dates),self.assertRaises(ValueError):
                adapter.configure_normal_navigation({'123':PERFORMER},dates)
        with self.assertRaises(VividCaptureError):adapter.capture(URL.replace('/123','/456'))
        adapter.playwright.webkit.launch.assert_not_called()

    def test_public_dom_discovery_interface_uses_stock_page_without_capture(self):
        browser=Browser(self.clock);adapter=session(self.clock,[browser]);driver=adapter.owner.driver
        driver.get('https://www.vividseats.com/nfl/');self.assertIn('public feed',driver.page_source)
        self.assertEqual(driver.execute_script('window.scrollTo(0, document.body.scrollHeight);'),'public-eval')
        self.assertEqual(browser.context.clicks,0);self.assertEqual(browser.context.body_reads,[])
        adapter.close();self.assertTrue(browser.closed)

    def test_opt_in_factory_delegates_capture_close_without_chrome_constructor(self):
        delegate=Mock();delegate.capture.return_value=(payload(),AT)
        with patch.dict(os.environ,{'TICKETSIGNAL_BROWSER_ENGINE':'webkit'}), \
             patch('vivid_webkit.WebKitInventorySession',return_value=delegate), \
             patch.object(VividBrowser,'__init__',side_effect=AssertionError('No Chrome constructor')):
            browser=VividNFLBrowser(timeout=45)
            self.assertEqual(browser.capture(URL),(payload(),AT));browser.close()
        delegate.close.assert_called_once()

    def test_schedule_configures_observed_home_route_and_official_utc_in_webkit(self):
        delegate=Mock();browser=SimpleNamespace(_webkit_session=delegate)
        game=SimpleNamespace(home_team='New Orleans Saints',event_date=AT)
        with patch.dict(os.environ,{'TICKETSIGNAL_FIREFOX_NAVIGATION':'direct'}):
            configure_schedule_navigation(browser,'nfl',game,URL)
        delegate.configure_normal_navigation.assert_called_once_with({'123':PERFORMER},{'123':AT})

    def test_no_profile_identity_header_interception_or_constructed_request_options(self):
        root=Path(__file__).resolve().parents[1];tree=ast.parse((root/'vivid_webkit.py').read_text())
        forbidden={'headers','all_headers','cookies','storage_state','route','fetch','set_extra_http_headers',
                   'add_init_script','launch_persistent_context'}
        self.assertFalse(any(isinstance(n,ast.Attribute) and n.attr in forbidden for n in ast.walk(tree)))
        launch=next(n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='launch')
        self.assertEqual({kw.arg for kw in launch.keywords},{'headless','timeout'})
        contexts=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='new_context']
        self.assertTrue(all(not n.args and not n.keywords for n in contexts))
        import yaml
        workflow=yaml.load((root/'.github/workflows/collect-ticket-prices.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(workflow['on']['workflow_dispatch']['inputs']['browser_engine']['default'],'webkit')
        for sport in ('nfl','nhl'):
            step=next(s for s in workflow['jobs']['collect-'+sport]['steps'] if s.get('name')=='Install stock WebKit for the shared capture owner')
            self.assertIn("env.TICKETSIGNAL_BROWSER_ENGINE == 'webkit'",step['if'])
            self.assertIn('python -m playwright install --with-deps webkit',step['run'])


if __name__=='__main__':unittest.main()
