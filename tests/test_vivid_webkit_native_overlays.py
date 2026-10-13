"""Opt-in real WebKit check using only locally fulfilled synthetic pages.

Run with RUN_WEBKIT_OVERLAY_FIXTURE=1 after installing the pinned WebKit runtime.
Every browser request is fulfilled or aborted locally; no provider is contacted.
"""
from datetime import datetime, timezone
from html import escape
import json
import os
from types import SimpleNamespace
import unittest

import vivid_webkit


PID = '123'
EVENT = 'https://www.vividseats.com/fixture-event/production/123'
PERFORMER = 'https://www.vividseats.com/new-orleans-saints-tickets--sports-nfl-football/performer/597'
INVENTORY = 'https://www.vividseats.com/hermes/api/v1/listings?productionId=123'
AT = datetime(2026, 11, 1, 18, tzinfo=timezone.utc)


def performer_fixture(*, overlays, iframe_kind=None, explicit_dismissal=False):
    promotion = '''
      <div class="ab-iam-root"><div class="ab-in-app-message" role="dialog">
        <p>Sign up for offers</p>
        <button class="ab-close-button" aria-label="Close Message">X</button>
      </div></div>
    '''
    if iframe_kind:
        known = iframe_kind == 'braze'
        root_class = 'ab-iam-root' if known else 'fixture-unknown-root'
        frame_class = 'ab-in-app-message ab-html-message ab-modal-interactions' if known else 'fixture-unknown-message'
        frame_html = '''<!doctype html><html><body style="margin:0;background:#ddd;height:100vh">
          <p>Fixture HTML promotion</p><script>
          window.addEventListener('keydown', e => {
            if (e.key === 'Escape') parent.fixtureEscape(e);
          });
          </script></body></html>'''
        promotion = (f'<div class="{root_class}"><iframe title="Modal Message" '
                     f'class="{frame_class}" srcdoc="{escape(frame_html, quote=True)}"></iframe></div>')
    controls = promotion + '''
      <div class="trustarc-banner-overlay"></div>
      <div id="consent_blackbar"><div id="truste-consent-track">
        <button id="reject">Reject Non-Essential Cookies</button>
      </div></div>
    ''' if overlays else ''
    return '''<!doctype html><html><head><style>
      body { margin: 0; font-family: sans-serif; }
      #ordinary-event { position: absolute; top: 200px; left: 100px; padding: 20px; }
      .trustarc-banner-overlay { position: fixed; inset: 0; background: #0006; z-index: 3000; }
      #consent_blackbar { position: fixed; bottom: 0; left: 0; right: 0; padding: 30px;
                          background: white; z-index: 3001; }
      .ab-iam-root, .fixture-unknown-root { position: fixed; inset: 0; background: #0003; z-index: 2000; }
      .ab-in-app-message { position: absolute; left: 30%; top: 15%; background: white; padding: 50px; }
      iframe { box-sizing: border-box; position: fixed !important; inset: 0 !important;
               padding: 0 !important; border: 0; width: 100%; height: 100%; }
      button { padding: 15px; }
      .ab-pause-scrolling { overflow: hidden; }
    </style></head><body>''' + controls + f'''
      <a id="ordinary-event" target="_blank" href="{EVENT}">Open fixture game</a>
      <script>
        window.fixtureClicks = [];
        function record(action, event) {{ fixtureClicks.push({{action, trusted: event.isTrusted}}); }}
        window.fixtureEscape = e => {{
          record('escape', e);
          if ({json.dumps(iframe_kind == 'braze' and not explicit_dismissal)}) {{
            document.querySelector('.ab-iam-root').remove();
            document.body.classList.remove('ab-pause-scrolling');
          }}
        }};
        window.addEventListener('keydown', e => {{
          if (e.key === 'Escape' && document.querySelector('iframe')) fixtureEscape(e);
        }});
        document.querySelector('#ordinary-event').addEventListener('click', e => record('game', e));
        const reject = document.querySelector('#reject');
        if (reject) {{
          document.body.classList.add('ab-pause-scrolling');
          reject.addEventListener('click', e => {{
            record('reject', e);
            document.querySelector('.trustarc-banner-overlay').remove();
            document.querySelector('#consent_blackbar').remove();
          }});
          const close = document.querySelector('.ab-close-button');
          if (close) close.addEventListener('click', e => {{
              record('close', e);
              document.querySelector('.ab-iam-root').remove();
              document.body.classList.remove('ab-pause-scrolling');
            }});
        }}
      </script></body></html>'''


def event_fixture():
    data = {'props': {'pageProps': {'id': PID, 'initialProductionDetailsData': {'data': {
        'id': PID, 'utcDate': AT.isoformat(), 'name': 'Minnesota Vikings at New Orleans Saints',
        'venue': {'name': 'Arena', 'id': '100'}}}}}, 'query': {'id': PID}}
    return ('<!doctype html><html><body><h1>Fixture game</h1>'
            '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + '</script>'
            '<script>fetch(' + json.dumps(INVENTORY) + ').then(r => r.json());</script></body></html>')


class FixtureSession(vivid_webkit.WebKitInventorySession):
    def __init__(self, *, overlays, iframe_kind=None, explicit_dismissal=False, capture_budget=30):
        self.overlays = overlays
        self.iframe_kind = iframe_kind
        self.explicit_dismissal = explicit_dismissal
        self.capture_budget = capture_budget
        self.last_performer_state = None
        self.requests = []
        self.unexpected_requests = []
        super().__init__(SimpleNamespace(), headless=True, timeout=30)
        self.configure_normal_navigation({PID: PERFORMER}, {PID: AT})

    def new_browser(self):
        browser, context = super().new_browser()

        def local_route(route):
            url = route.request.url
            self.requests.append(url)
            if url == PERFORMER:
                route.fulfill(status=200, content_type='text/html', body=performer_fixture(
                    overlays=self.overlays, iframe_kind=self.iframe_kind, explicit_dismissal=self.explicit_dismissal))
            elif url == EVENT:
                route.fulfill(status=200, content_type='text/html', body=event_fixture())
            elif url == INVENTORY:
                payload = {'global': [{'productionId': PID,
                    'productionName': 'Minnesota Vikings at New Orleans Saints',
                    'mapTitle': 'Arena', 'venueId': '100', 'listingCount': '12'}],
                    'tickets': [{'l': 'Section' + str(i), 'p': '25.10', 'aip': '29.90',
                                 'q': '2', 'r': 'A'} for i in range(12)]}
                route.fulfill(status=200, content_type='application/json', body=json.dumps(payload))
            else:
                self.unexpected_requests.append(url)
                route.abort()

        context.route('**/*', local_route)
        # Retain the normal cold browser launch allowance, then bound only the
        # expected negative capture cases so they cannot prolong the test suite.
        self.timeout = self.capture_budget
        return browser, context

    def close_event(self):
        if self.event_context is not None:
            for page in self.event_context.pages:
                if page.url == PERFORMER:
                    self.last_performer_state = page.evaluate('''() => ({
                      clicks: window.fixtureClicks,
                      framePresent: !!document.querySelector('iframe'),
                      scrollLocked: document.body.classList.contains('ab-pause-scrolling')
                    })''')
        super().close_event()


@unittest.skipUnless(os.environ.get('RUN_WEBKIT_OVERLAY_FIXTURE') == '1', 'opt-in stock WebKit fixture')
class NativeOverlayFixtureTests(unittest.TestCase):
    def setUp(self):
        vivid_webkit._blocked_category = None

    def check_capture(self, *, overlays, iframe_kind=None):
        adapter = FixtureSession(overlays=overlays, iframe_kind=iframe_kind)
        self.addCleanup(adapter.close)
        payload, stamp = adapter.capture(EVENT)
        self.assertEqual(stamp, AT)
        self.assertEqual(len(payload['tickets']), 12)
        self.assertEqual(adapter.requests, [PERFORMER, EVENT, INVENTORY])
        self.assertEqual(adapter.unexpected_requests, [])
        performer = adapter.event_context.pages[0]
        expected = ['reject', 'escape' if iframe_kind else 'close', 'game'] if overlays else ['game']
        self.assertEqual(performer.evaluate('window.fixtureClicks'),
                         [{'action': action, 'trusted': True} for action in expected])
        self.assertFalse(performer.locator('.trustarc-banner-overlay').count())
        self.assertFalse(performer.locator('.ab-iam-root').count())
        self.assertFalse(performer.evaluate("document.body.classList.contains('ab-pause-scrolling')"))
        diagnostics = adapter.owner.capture_diagnostics
        self.assertTrue(diagnostics['visible_event_link_clicked'])
        self.assertTrue(diagnostics['event_opened_native_popup'])
        self.assertEqual(diagnostics['event_link_trial_attempts'], 1)
        self.assertEqual(diagnostics.get('performer_overlay_actions', []),
            [{'action': 'reject-optional-cookies', 'outcome': 'clicked'},
             {'action': 'dismiss-html-promotion', 'outcome': 'escape-sent'} if iframe_kind else
             {'action': 'close-promotion', 'outcome': 'clicked'}] if overlays else [])

    def test_native_reject_then_promo_close_unblocks_full_capture(self):
        self.check_capture(overlays=True)

    def test_no_overlay_keeps_the_normal_full_capture_path(self):
        self.check_capture(overlays=False)

    def test_native_escape_in_html_promotion_frame_unblocks_full_capture(self):
        self.check_capture(overlays=True, iframe_kind='braze')

    def check_closed_failure(self, *, iframe_kind, explicit_dismissal):
        adapter = FixtureSession(overlays=True, iframe_kind=iframe_kind,
                                 explicit_dismissal=explicit_dismissal, capture_budget=4)
        self.addCleanup(adapter.close)
        with self.assertRaises(vivid_webkit.VividCaptureError) as error:
            adapter.capture(EVENT)
        self.assertEqual(error.exception.category, 'provider-inventory-timeout')
        self.assertEqual(adapter.requests, [PERFORMER])
        self.assertEqual(adapter.unexpected_requests, [])
        expected = ['reject', 'escape'] if iframe_kind == 'braze' else ['reject']
        self.assertEqual(adapter.last_performer_state, {
            'clicks': [{'action': action, 'trusted': True} for action in expected],
            'framePresent': True, 'scrollLocked': True})
        diagnostics = adapter.owner.capture_diagnostics
        self.assertNotIn('visible_event_link_clicked', diagnostics)
        self.assertNotIn('event_opened_native_popup', diagnostics)
        self.assertEqual(diagnostics.get('performer_overlay_actions', []),
            [{'action': 'reject-optional-cookies', 'outcome': 'clicked'}] +
            ([{'action': 'dismiss-html-promotion', 'outcome': 'escape-sent'}]
             if iframe_kind == 'braze' else []))

    def test_explicit_dismissal_setting_keeps_html_frame_and_fails_closed(self):
        self.check_closed_failure(iframe_kind='braze', explicit_dismissal=True)

    def test_unknown_frame_receives_no_escape_and_fails_closed(self):
        self.check_closed_failure(iframe_kind='unknown', explicit_dismissal=False)


if __name__ == '__main__':
    unittest.main()
