from datetime import datetime, timedelta, timezone
import ast
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from collector import VividBrowser
from nfl_collector import VividNFLBrowser
from vivid_inventory import CurrentInventoryRecovery, VividCaptureError
from vivid_performer_routes import configure_schedule_navigation
from vivid_webkit import BLOCKER_ELEMENT_SCRIPT, CONSENT_CONTAINER, CONSENT_REJECT_LABEL, PROMO_CLOSE_SELECTOR, PROMO_HTML_IFRAME_SELECTOR, LINK_ACTIONABILITY_SCRIPT, PUBLIC_PAGE_SCRIPT, BlockerScreenshotBudget, PublicDOMDriver, WebKitInventorySession, click_timeout_reason, public_inventory, safe_blocker_element, verified_event_date

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
        if _script == BLOCKER_ELEMENT_SCRIPT:
            self.page.context.browser.blocker_reads += 1
            return {'hit':{'tag':'IFRAME','id':'ad-frame','classes':['overlay','https://private/?token=secret'],
                           'position':'fixed','src':'https://private/?token=secret'},
                    'hit_is_iframe':True,'hit_contains_iframe':False,'hit_inside_iframe':False,
                    'hit_in_header':False,'known_consent_present':True,'hit_in_known_consent':False,
                    'hit_box':{'x':0,'y':0,'width':1280,'height':720},
                    'viewport':{'width':1280,'height':720},'text':'private'}
        if _script == LINK_ACTIONABILITY_SCRIPT:
            return {'attached': True, 'has_box': True, 'in_viewport': True, 'receives_pointer': False,
                    'obstructed_by_dialog': True, 'ready_state': self.page.context.browser.ready_state,
                    'private_token': 'private'}
        if self.page.context.browser.fail_phase == 'performer-link-scan':
            self.page.context.clock.advance(options['timeout'])
            raise self.page.context.browser.timeout_error('Private timeout message must not enter diagnostics')
        return self.page.context.popup.url
    def get_attribute(self, _name, **_options): return '_blank'
    def is_visible(self): return True
    def is_enabled(self, **_options): return True
    def click(self, **options):
        if options.get('trial'):
            browser = self.page.context.browser
            self.page.context.trials += 1
            self.page.run_handlers(options['timeout'])
            browser.actions.append('event-trial')
            if browser.trial_failures or browser.overlays:
                browser.trial_failures = max(0, browser.trial_failures - 1)
                browser.overlays.update(browser.late_overlays)
                browser.late_overlays.clear()
                self.page.context.clock.advance(options['timeout'])
                if browser.denial_during_trial:
                    self.page.context.emit(Response(self.page.context, self.page, 403, document=True))
                raise browser.timeout_error('private-token overlay intercepts pointer events')
            return
        self.page.context.browser.overlays.update(self.page.context.browser.before_click_overlays)
        self.page.context.browser.before_click_overlays.clear()
        self.page.run_handlers(options['timeout'])
        self.page.context.clicks += 1
        self.page.context.browser.actions.append('event-click')
        if self.page.context.browser.fail_phase == 'event-link-click':
            self.page.context.clock.advance(options['timeout'])
            raise self.page.context.browser.timeout_error('Private timeout message must not enter diagnostics')
        self.page.context.emit_event()


class OverlayControls:
    def __init__(self, page, action): self.page, self.action = page, action
    def get_by_text(self, text, *, exact):
        assert self.action == 'reject-optional-cookies' and text == CONSENT_REJECT_LABEL and exact is True
        return self
    def count(self):
        browser = self.page.context.browser
        return browser.control_counts.get(self.action, int(self.action in browser.overlays))
    def nth(self, index):
        assert index == 0
        return self
    @property
    def first(self): return self
    def is_visible(self): return self.action not in self.page.context.browser.hidden_controls
    def locator(self, selector):
        assert self.action == 'dismiss-html-promotion' and selector == 'body'
        return self
    def press(self, key, **options):
        assert self.action == 'dismiss-html-promotion' and key == 'Escape'
        self.click(**options)
        if self.page.context.browser.html_dismissal_disabled:
            self.page.context.browser.overlays.add(self.action)
    def click(self, **options):
        browser = self.page.context.browser
        assert set(options) == {'timeout'}  # No force, JS click, or navigation bypass.
        browser.actions.append(self.action)
        browser.control_clicks.append((self.action, options))
        if browser.control_timeout == self.action:
            self.page.context.clock.advance(options['timeout'])
            raise browser.timeout_error('private control timeout')
        browser.overlays.discard(self.action)
        if browser.denial_after_control:
            self.page.context.emit(Response(self.page.context, self.page, 403, document=True))


class Page:
    def __init__(self, context, url='about:blank', *, event=False):
        self.context, self.url, self.event = context, url, event
        self.main_frame = SimpleNamespace(page=self)
        self.reloads = 0
        self.handlers = []
        self.handler_running = False
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
        if _selector == CONSENT_CONTAINER: return OverlayControls(self, 'reject-optional-cookies')
        if _selector == PROMO_CLOSE_SELECTOR: return OverlayControls(self, 'close-promotion')
        if _selector == PROMO_HTML_IFRAME_SELECTOR: return OverlayControls(self, 'dismiss-html-promotion')
        assert _selector.startswith("a[href*='/production/")
        return SimpleNamespace(count=lambda: 1, nth=lambda _index: Link(self))
    def frame_locator(self, selector):
        assert selector == PROMO_HTML_IFRAME_SELECTOR
        return OverlayControls(self, 'dismiss-html-promotion')
    def add_locator_handler(self, locator, handler, **options):
        assert options == {'times': 1}  # Keep the default wait for the overlay to disappear.
        self.handlers.append([locator, handler, options['times']])
        self.context.browser.handler_registrations.append(locator.action)
    def run_handlers(self, timeout):
        if self.handler_running: return
        started = self.context.clock.monotonic()
        for row in self.handlers:
            locator, handler, remaining = row
            if remaining and locator.count() and locator.is_visible():
                row[2] -= 1
                self.handler_running = True
                try: handler(locator)
                finally: self.handler_running = False
                if locator.count() and locator.is_visible():
                    elapsed = (self.context.clock.monotonic() - started) * 1000
                    self.context.clock.advance(max(0, timeout - elapsed))
                    raise self.context.browser.timeout_error('overlay intercepts pointer events')
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
        if not self.event:
            self.context.browser.readiness_waits.append(options)
            if self.context.browser.load_stall:
                self.context.clock.advance(options['timeout'])
                raise self.context.browser.timeout_error('private load stall')
            self.context.browser.ready_state = 'complete'
            return
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
    def screenshot(self, **options):
        browser=self.context.browser
        browser.screenshots.append((options,browser.closed))
        Path(options['path']).write_bytes(browser.screenshot_bytes)
        if browser.screenshot_timeout:
            self.context.clock.advance(options['timeout'])
            raise browser.timeout_error('private screenshot failure')


class Context:
    def __init__(self, browser, clock):
        self.browser, self.clock = browser, clock
        self.callbacks, self.removed, self.body_reads, self.navigations = {}, [], [], []
        self.clicks = 0
        self.trials = 0
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
        self.trial_failures = options.get('trial_failures', 0)
        self.denial_during_trial = options.get('denial_during_trial', False)
        self.readiness_waits = []
        self.blocker_reads,self.screenshots = 0,[]
        self.screenshot_timeout=options.get('screenshot_timeout',False)
        self.screenshot_bytes=options.get('screenshot_bytes',b'public viewport')
        self.overlays = set(options.get('overlays', []))
        self.late_overlays = set(options.get('late_overlays', []))
        self.before_click_overlays = set(options.get('before_click_overlays', []))
        self.handler_registrations = []
        self.control_counts = options.get('control_counts', {})
        self.hidden_controls = options.get('hidden_controls', set())
        self.control_timeout = options.get('control_timeout')
        self.denial_after_control = options.get('denial_after_control', False)
        self.html_dismissal_disabled = options.get('html_dismissal_disabled', False)
        self.actions, self.control_clicks = [], []
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

    def test_observed_cookie_and_signup_overlays_use_native_reject_then_close_before_event(self):
        browser = Browser(self.clock, overlays=['reject-optional-cookies', 'close-promotion'])
        adapter = session(self.clock, [browser])
        clean, stamp = adapter.capture(URL)
        self.assertEqual(stamp, AT); self.assertEqual(len(clean['tickets']), 12)
        self.assertEqual(browser.actions, ['reject-optional-cookies', 'close-promotion', 'event-trial', 'event-click'])
        self.assertEqual(browser.control_clicks, [('reject-optional-cookies', {'timeout': 3000}),
                                                 ('close-promotion', {'timeout': 3000})])
        self.assertEqual(adapter.owner.capture_diagnostics['performer_overlay_actions'], [
            {'action': 'reject-optional-cookies', 'outcome': 'clicked'},
            {'action': 'close-promotion', 'outcome': 'clicked'}])
        self.assertEqual(browser.context.body_reads, ['123']); adapter.close()

    def test_observed_html_promotion_uses_one_native_escape_after_cookie_rejection(self):
        browser = Browser(self.clock, overlays=['reject-optional-cookies', 'dismiss-html-promotion'])
        adapter = session(self.clock, [browser]); adapter.capture(URL)
        self.assertEqual(browser.actions, ['reject-optional-cookies', 'dismiss-html-promotion', 'event-trial', 'event-click'])
        self.assertEqual(adapter.owner.capture_diagnostics['performer_overlay_actions'], [
            {'action': 'reject-optional-cookies', 'outcome': 'clicked'},
            {'action': 'dismiss-html-promotion', 'outcome': 'escape-sent'}])
        adapter.close()

    def test_explicit_html_dismissal_disabled_still_fails_without_second_escape(self):
        browser = Browser(self.clock, overlays=['dismiss-html-promotion'], html_dismissal_disabled=True)
        adapter = session(self.clock, [browser]); adapter.timeout = 45
        with self.assertRaises(VividCaptureError): adapter.capture(URL)
        self.assertEqual(browser.actions, ['dismiss-html-promotion', 'event-trial'])
        self.assertEqual(browser.context.clicks, 0); self.assertEqual(browser.context.body_reads, [])
        self.assertTrue(browser.closed); adapter.close()

    def test_hidden_or_ambiguous_html_frames_never_receive_escape(self):
        for options in ({'hidden_controls': {'dismiss-html-promotion'}},
                        {'control_counts': {'dismiss-html-promotion': 2}}):
            with self.subTest(options=options):
                browser = Browser(self.clock, overlays=['dismiss-html-promotion'], **options)
                adapter = session(self.clock, [browser]); adapter.timeout = 45
                with self.assertRaises(VividCaptureError): adapter.capture(URL)
                self.assertEqual(browser.control_clicks, []); self.assertEqual(browser.context.clicks, 0)
                adapter.close()

    def test_html_escape_timeout_is_clipped_to_capture_deadline_and_not_repeated(self):
        browser = Browser(self.clock, overlays=['dismiss-html-promotion'],
                          control_timeout='dismiss-html-promotion', performer_delay_ms=7500)
        adapter = session(self.clock, [browser])
        with self.assertRaises(VividCaptureError): adapter.capture(URL)
        self.assertEqual(browser.control_clicks, [('dismiss-html-promotion', {'timeout': 500})])
        self.assertEqual(adapter.owner.capture_diagnostics['performer_overlay_actions'], [
            {'action': 'dismiss-html-promotion', 'outcome': 'timeout'}])
        self.assertEqual(browser.context.trials, 1); self.assertEqual(browser.context.clicks, 0)
        self.assertEqual(adapter.owner.capture_diagnostics['capture_elapsed_ms'], 8000)
        self.assertTrue(browser.closed); adapter.close()

    def test_handler_dismisses_cookie_and_html_overlay_arriving_during_only_actual_click(self):
        browser = Browser(self.clock, before_click_overlays=[
            'reject-optional-cookies', 'dismiss-html-promotion'])
        adapter = session(self.clock, [browser]); adapter.timeout = 45
        clean, stamp = adapter.capture(URL)
        self.assertEqual(stamp, AT); self.assertEqual(len(clean['tickets']), 12)
        self.assertEqual(browser.context.trials, 1); self.assertEqual(browser.context.clicks, 1)
        self.assertEqual(browser.actions, ['event-trial', 'reject-optional-cookies',
                                         'dismiss-html-promotion', 'event-click'])
        self.assertEqual(browser.handler_registrations, ['reject-optional-cookies',
                         'close-promotion', 'dismiss-html-promotion'])
        diagnostic = adapter.owner.capture_diagnostics
        self.assertEqual(diagnostic['phase'], 'complete')
        self.assertNotIn('event_link_trial_timeout_reason', diagnostic)
        self.assertEqual(diagnostic['performer_overlay_actions'], [
            {'action': 'reject-optional-cookies', 'outcome': 'clicked'},
            {'action': 'dismiss-html-promotion', 'outcome': 'escape-sent'}])
        self.assertEqual(browser.context.body_reads, ['123']); adapter.close()

    def test_handler_cannot_extend_click_budget_or_repeat_ambiguous_real_click(self):
        browser = Browser(self.clock, before_click_overlays=['reject-optional-cookies'],
                          control_timeout='reject-optional-cookies', performer_delay_ms=7500)
        adapter = session(self.clock, [browser])
        with self.assertRaises(VividCaptureError): adapter.capture(URL)
        self.assertEqual(browser.context.trials, 1); self.assertEqual(browser.context.clicks, 0)
        self.assertEqual(browser.control_clicks, [('reject-optional-cookies', {'timeout': 500})])
        diagnostic = adapter.owner.capture_diagnostics
        self.assertEqual(diagnostic['capture_elapsed_ms'], 8000)
        self.assertEqual(diagnostic['timeout_phase'], 'event-link-click')
        self.assertEqual(diagnostic['event_link_click_timeout_reason'], 'pointer-intercepted')
        self.assertEqual(browser.context.body_reads, []); self.assertTrue(browser.closed)
        adapter.close()

    def test_cookie_banner_appearing_after_first_trial_is_rejected_before_second(self):
        browser = Browser(self.clock, trial_failures=1, late_overlays=['reject-optional-cookies'])
        adapter = session(self.clock, [browser]); adapter.timeout = 45
        adapter.capture(URL)
        self.assertEqual(browser.actions, ['event-trial', 'reject-optional-cookies', 'event-trial', 'event-click'])
        self.assertEqual(len(browser.control_clicks), 1); adapter.close()

    def test_unknown_ambiguous_or_hidden_controls_are_not_clicked(self):
        for options in ({'overlays': ['unknown']},
                        {'overlays': ['reject-optional-cookies'], 'control_counts': {'reject-optional-cookies': 0}},
                        {'overlays': ['close-promotion'], 'control_counts': {'close-promotion': 2}},
                        {'overlays': ['close-promotion'], 'hidden_controls': {'close-promotion'}}):
            with self.subTest(options=options):
                browser = Browser(self.clock, **options); adapter = session(self.clock, [browser]); adapter.timeout = 45
                with self.assertRaises(VividCaptureError): adapter.capture(URL)
                self.assertEqual(browser.control_clicks, []); self.assertEqual(browser.context.clicks, 0)
                self.assertEqual(browser.context.trials, 2); self.assertTrue(browser.closed); adapter.close()

    def test_timed_out_control_is_never_reclicked_or_replaced_by_another_consent_choice(self):
        browser = Browser(self.clock, overlays=['reject-optional-cookies'], control_timeout='reject-optional-cookies')
        adapter = session(self.clock, [browser]); adapter.timeout = 45
        with self.assertRaises(VividCaptureError): adapter.capture(URL)
        self.assertEqual(browser.actions, ['reject-optional-cookies', 'event-trial'])
        self.assertEqual(adapter.owner.capture_diagnostics['performer_overlay_actions'], [
            {'action': 'reject-optional-cookies', 'outcome': 'timeout'}])
        self.assertEqual(adapter.owner.capture_diagnostics['capture_elapsed_ms'], 20000)
        self.assertNotIn('private', json.dumps(adapter.owner.capture_diagnostics)); adapter.close()

    def test_native_control_timeout_cannot_extend_remaining_capture_budget(self):
        browser = Browser(self.clock, overlays=['reject-optional-cookies'],
                          control_timeout='reject-optional-cookies', performer_delay_ms=7500)
        adapter = session(self.clock, [browser])
        with self.assertRaises(VividCaptureError): adapter.capture(URL)
        self.assertEqual(browser.control_clicks, [('reject-optional-cookies', {'timeout': 500})])
        self.assertEqual(browser.context.trials, 1); self.assertEqual(browser.context.clicks, 0)
        self.assertEqual(adapter.owner.capture_diagnostics['capture_elapsed_ms'], 8000)
        self.assertTrue(browser.closed); adapter.close()

    def test_visible_challenge_or_denial_stops_before_overlay_controls(self):
        for options in ({'challenge': True}, {'performer_status': 403}):
            with self.subTest(options=options), patch('vivid_webkit._blocked_category', None):
                browser = Browser(self.clock, overlays=['reject-optional-cookies'], **options)
                adapter = session(self.clock, [browser])
                with self.assertRaises(VividCaptureError) as error: adapter.capture(URL)
                self.assertEqual(error.exception.category, 'provider-access-denied')
                self.assertEqual(browser.control_clicks, []); self.assertEqual(browser.context.clicks, 0)
                self.assertTrue(browser.closed); adapter.close()

    def test_denial_after_cookie_rejection_stops_before_promo_or_event_click(self):
        browser = Browser(self.clock, overlays=['reject-optional-cookies', 'close-promotion'], denial_after_control=True)
        adapter = session(self.clock, [browser])
        with self.assertRaises(VividCaptureError) as error: adapter.capture(URL)
        self.assertEqual(error.exception.category, 'provider-access-denied')
        self.assertEqual(browser.actions, ['reject-optional-cookies']); self.assertEqual(browser.context.clicks, 0)
        self.assertTrue(browser.closed); adapter.close()

    def test_native_full_popup_capture_ignores_prefetch_and_redacts_public_fields(self):
        raw=payload();raw['private_token']='private';raw['tickets'][0]['seller_id']='private'
        browser=Browser(self.clock, body=raw);adapter=session(self.clock,[browser])
        clean, stamp=adapter.capture(URL)
        self.assertEqual(stamp,AT);self.assertEqual(len(clean['tickets']),12)
        self.assertNotIn('private',json.dumps(clean));self.assertEqual(browser.context.body_reads,['123'])
        self.assertEqual(browser.context.clicks,1)
        self.assertEqual(browser.context.trials,1)
        self.assertEqual(browser.readiness_waits,[])
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

    def test_transient_layout_wait_retries_actionability_but_clicks_only_once(self):
        browser=Browser(self.clock,trial_failures=1)
        adapter=session(self.clock,[browser]);adapter.timeout=45
        raw,stamp=adapter.capture(URL)
        self.assertEqual(stamp,AT);self.assertEqual(len(raw['tickets']),12)
        self.assertEqual(browser.context.trials,2);self.assertEqual(browser.context.clicks,1)
        self.assertEqual(browser.context.body_reads,['123'])
        self.assertEqual(browser.readiness_waits,[dict(timeout=5000)])
        diagnostic=adapter.owner.capture_diagnostics
        self.assertTrue(diagnostic['event_link_readiness_wait_completed'])
        self.assertEqual(diagnostic['event_link_trial_timeout_reason'],'pointer-intercepted')
        self.assertEqual(diagnostic['event_link_actionability'],{
            'attached':True,'has_box':True,'in_viewport':True,'receives_pointer':False,
            'obstructed_by_dialog':True,'ready_state':'interactive'})
        self.assertEqual(diagnostic['performer_ready_state_at_click'],'complete')
        self.assertNotIn('private',json.dumps(diagnostic));adapter.close()

    def test_persistent_obstruction_stops_after_two_trials_without_click_or_inventory(self):
        browser=Browser(self.clock,trial_failures=2,load_stall=True)
        adapter=session(self.clock,[browser]);adapter.timeout=45
        with self.assertRaises(VividCaptureError) as error:adapter.capture(URL)
        self.assertEqual(error.exception.category,'provider-inventory-timeout')
        self.assertEqual(browser.context.trials,2);self.assertEqual(browser.context.clicks,0)
        self.assertEqual(browser.context.body_reads,[]);self.assertTrue(browser.closed)
        diagnostic=adapter.owner.capture_diagnostics
        self.assertEqual(diagnostic['timeout_phase'],'event-link-ready')
        self.assertEqual(diagnostic['capture_elapsed_ms'],25000)
        self.assertFalse(diagnostic['event_link_readiness_wait_completed'])
        self.assertNotIn('private',json.dumps(diagnostic));adapter.close()

    def test_trial_exhausting_remaining_budget_has_no_load_wait_or_second_attempt(self):
        browser=Browser(self.clock,trial_failures=2,performer_delay_ms=7500)
        adapter=session(self.clock,[browser])
        with self.assertRaises(VividCaptureError):adapter.capture(URL)
        self.assertEqual(browser.context.trials,1);self.assertEqual(browser.context.clicks,0)
        self.assertEqual(browser.readiness_waits,[])
        self.assertEqual(adapter.owner.capture_diagnostics['capture_elapsed_ms'],8000)
        self.assertEqual(adapter.owner.capture_diagnostics['timeout_phase'],'event-link-ready')
        self.assertTrue(browser.closed);adapter.close()

    def test_provider_denial_during_trial_stops_without_readiness_retry(self):
        browser=Browser(self.clock,trial_failures=2,denial_during_trial=True)
        adapter=session(self.clock,[browser]);adapter.timeout=45
        with self.assertRaises(VividCaptureError) as error:adapter.capture(URL)
        self.assertEqual(error.exception.category,'provider-access-denied')
        self.assertFalse(error.exception.retryable)
        self.assertEqual(browser.context.trials,1);self.assertEqual(browser.context.clicks,0)
        self.assertEqual(browser.readiness_waits,[]);self.assertTrue(browser.closed)
        self.assertEqual(browser.context.body_reads,[]);adapter.close()

    def test_actual_click_timeout_is_never_repeated(self):
        browser=Browser(self.clock,fail_phase='event-link-click')
        adapter=session(self.clock,[browser]);adapter.timeout=45
        with self.assertRaises(VividCaptureError):adapter.capture(URL)
        self.assertEqual(browser.context.trials,1);self.assertEqual(browser.context.clicks,1)
        self.assertEqual(browser.readiness_waits,[])
        diagnostic=adapter.owner.capture_diagnostics
        self.assertEqual(diagnostic['timeout_phase'],'event-link-click')
        self.assertEqual(diagnostic['event_link_click_timeout_reason'],'unclassified')
        self.assertNotIn('Private',json.dumps(diagnostic));adapter.close()

    def test_opt_in_blocker_diagnostics_save_one_bounded_screenshot_before_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            browser=Browser(self.clock,trial_failures=2)
            adapter=session(self.clock,[browser]);adapter.timeout=45
            adapter.configure_blocker_diagnostics(directory)
            with self.assertRaises(VividCaptureError):adapter.capture(URL)
            diagnostic=adapter.owner.capture_diagnostics
            self.assertEqual(browser.context.clicks,0);self.assertEqual(browser.context.trials,2)
            self.assertEqual(browser.blocker_reads,1);self.assertEqual(len(browser.screenshots),1)
            options,closed_at_screenshot=browser.screenshots[0]
            self.assertFalse(closed_at_screenshot);self.assertTrue(browser.closed)
            self.assertEqual(options,dict(path=str(Path(directory)/'blocker-123.png'),type='png',
                full_page=False,animations='allow',caret='initial',timeout=1500))
            self.assertEqual((Path(directory)/'blocker-123.png').read_bytes(),browser.screenshot_bytes)
            self.assertEqual(diagnostic['blocker_diagnostic_status'],'captured')
            self.assertEqual(diagnostic['blocker_screenshot_sha256'],hashlib.sha256(browser.screenshot_bytes).hexdigest())
            self.assertTrue(diagnostic['blocker_element']['hit_is_iframe'])
            self.assertTrue(diagnostic['blocker_element']['known_consent_present'])
            self.assertEqual(diagnostic['blocker_element']['hit']['classes'],['overlay'])
            self.assertNotIn('private',json.dumps(diagnostic));self.assertNotIn('secret',json.dumps(diagnostic))
            adapter.close()

    def test_default_and_successful_capture_do_not_take_blocker_screenshots(self):
        with tempfile.TemporaryDirectory() as directory:
            for enabled,failures in ((False,2),(True,0)):
                browser=Browser(self.clock,trial_failures=failures)
                adapter=session(self.clock,[browser]);adapter.timeout=45
                if enabled:adapter.configure_blocker_diagnostics(directory)
                if failures:
                    with self.assertRaises(VividCaptureError):adapter.capture(URL)
                else:adapter.capture(URL)
                self.assertEqual(browser.blocker_reads,0);self.assertEqual(browser.screenshots,[])
                self.assertEqual(list(Path(directory).iterdir()),[]);adapter.close()

    def test_job_screenshot_allowance_spans_sessions_and_failed_attempts(self):
        for first_times_out in (False, True):
            with self.subTest(first_times_out=first_times_out), tempfile.TemporaryDirectory() as directory:
                budget=BlockerScreenshotBudget(); browsers=[]; diagnostics=[]
                for index in range(3):
                    browser=Browser(self.clock,trial_failures=2,
                        screenshot_timeout=first_times_out and index==0)
                    adapter=session(self.clock,[browser]);adapter.timeout=45
                    adapter.configure_blocker_diagnostics(directory,screenshot_budget=budget)
                    with self.assertRaises(VividCaptureError):adapter.capture(URL)
                    browsers.append(browser);diagnostics.append(adapter.owner.capture_diagnostics)
                    adapter.close()
                self.assertEqual([len(browser.screenshots) for browser in browsers],[1,0,0])
                self.assertEqual([browser.blocker_reads for browser in browsers],[1,1,1])
                self.assertEqual([row['blocker_diagnostic_status'] for row in diagnostics],
                    ['timeout' if first_times_out else 'captured','metadata-only','metadata-only'])
                self.assertEqual(len(list(Path(directory).glob('*.png'))),0 if first_times_out else 1)

    def test_blocker_screenshot_timeout_uses_remaining_budget_and_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            browser=Browser(self.clock,trial_failures=2,screenshot_timeout=True)
            adapter=session(self.clock,[browser]);adapter.timeout=21
            adapter.configure_blocker_diagnostics(directory)
            with self.assertRaises(VividCaptureError):adapter.capture(URL)
            self.assertEqual(browser.screenshots[0][0]['timeout'],1000)
            self.assertEqual(adapter.owner.capture_diagnostics['capture_elapsed_ms'],21000)
            self.assertEqual(adapter.owner.capture_diagnostics['blocker_diagnostic_status'],'timeout')
            self.assertEqual(list(Path(directory).iterdir()),[])
            self.assertTrue(browser.closed);adapter.close()

    def test_access_denial_still_stops_before_any_blocker_diagnostic_read_or_screenshot(self):
        with tempfile.TemporaryDirectory() as directory:
            browser=Browser(self.clock,trial_failures=2,denial_during_trial=True)
            adapter=session(self.clock,[browser]);adapter.timeout=45
            adapter.configure_blocker_diagnostics(directory)
            with self.assertRaises(VividCaptureError) as error:adapter.capture(URL)
            self.assertEqual(error.exception.category,'provider-access-denied')
            self.assertEqual(browser.blocker_reads,0);self.assertEqual(browser.screenshots,[])
            self.assertEqual(browser.context.clicks,0);adapter.close()

    def test_actual_click_timeout_saves_blocker_and_never_reclicks(self):
        with tempfile.TemporaryDirectory() as directory:
            browser=Browser(self.clock,fail_phase='event-link-click')
            adapter=session(self.clock,[browser]);adapter.timeout=45
            adapter.configure_blocker_diagnostics(directory)
            with self.assertRaises(VividCaptureError):adapter.capture(URL)
            self.assertEqual(browser.context.clicks,1);self.assertEqual(browser.context.trials,1)
            self.assertEqual(browser.blocker_reads,1);self.assertEqual(len(browser.screenshots),1)
            self.assertEqual(adapter.owner.capture_diagnostics['timeout_phase'],'event-link-click')
            self.assertEqual(adapter.owner.capture_diagnostics['blocker_diagnostic_status'],'captured')
            self.assertTrue(browser.closed);adapter.close()

    def test_blocker_screenshot_budget_rejects_large_images_without_retaining_them(self):
        with tempfile.TemporaryDirectory() as directory:
            browser=Browser(self.clock,trial_failures=2,screenshot_bytes=b'x'*(2*1024**2+1))
            adapter=session(self.clock,[browser]);adapter.timeout=45
            adapter.configure_blocker_diagnostics(directory)
            with self.assertRaises(VividCaptureError):adapter.capture(URL)
            diagnostic=adapter.owner.capture_diagnostics
            self.assertEqual(diagnostic['blocker_diagnostic_status'],'unavailable')
            self.assertNotIn('blocker_screenshot_file',diagnostic)
            self.assertEqual(list(Path(directory).iterdir()),[]);adapter.close()

    def test_blocker_layout_whitelist_bounds_tokens_ancestors_and_coordinates(self):
        value=safe_blocker_element({'hit':{'tag':'CUSTOM-PRIVATE','role':'private','id':'overlay',
            'classes':['safe']*10+['private'],'position':'fixed','text':'private'},
            'ancestors':[{'tag':'DIV','id':'x'*81,'classes':['url?token=private']}] * 20,
            'hit_box':{'x':-12.567,'y':True,'width':float('nan'),'height':100001,'secret':1},
            'viewport':{'width':1280,'height':720},'hit_is_iframe':1,'cookies':'private'})
        self.assertEqual(value['hit'],{'tag':'OTHER','id':'overlay','classes':['safe']*6,'position':'fixed'})
        self.assertEqual(len(value['ancestors']),4)
        self.assertEqual(value['hit_box'],{'x':-12.57})
        self.assertNotIn('hit_is_iframe',value)
        self.assertNotIn('private',json.dumps(value,allow_nan=False))

    def test_click_failure_categories_never_include_exception_text(self):
        for phrase, expected in (
            ('intercepts pointer events','pointer-intercepted'),
            ('element is not stable','element-not-stable'),
            ('element is outside of the viewport','outside-viewport'),
            ('element is not visible','element-not-visible'),
            ('element is not enabled','element-not-enabled'),
            ('element was detached','element-detached'),
            ('waiting for scheduled navigations','navigation-wait'),
            ('private error text','unclassified')):
            with self.subTest(phrase=phrase):
                self.assertEqual(click_timeout_reason(Exception('private-token: '+phrase)),expected)

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
            self.assertEqual(step['run'], 'bash tools/install_capture_webkit.sh')
            self.assertIn('python -m playwright install --with-deps webkit',
                          (root/'tools/install_capture_webkit.sh').read_text())


if __name__=='__main__':unittest.main()
