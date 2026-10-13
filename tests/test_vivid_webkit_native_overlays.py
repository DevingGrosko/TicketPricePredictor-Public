"""Opt-in real WebKit check using only locally fulfilled synthetic pages.

Run with RUN_WEBKIT_OVERLAY_FIXTURE=1 after installing the pinned WebKit runtime.
Every browser request is fulfilled or aborted locally; no provider is contacted.
"""
from datetime import datetime, timezone
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


def performer_fixture(*, overlays):
    controls = '''
      <div class="ab-iam-root"><div class="ab-in-app-message" role="dialog">
        <p>Sign up for offers</p>
        <button class="ab-close-button" aria-label="Close Message">X</button>
      </div></div>
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
      .ab-iam-root { position: fixed; inset: 0; background: #0003; z-index: 2000; }
      .ab-in-app-message { position: absolute; left: 30%; top: 15%; background: white; padding: 50px; }
      button { padding: 15px; }
      .ab-pause-scrolling { overflow: hidden; }
    </style></head><body>''' + controls + f'''
      <a id="ordinary-event" target="_blank" href="{EVENT}">Open fixture game</a>
      <script>
        window.fixtureClicks = [];
        function record(action, event) {{ fixtureClicks.push({{action, trusted: event.isTrusted}}); }}
        document.querySelector('#ordinary-event').addEventListener('click', e => record('game', e));
        const reject = document.querySelector('#reject');
        if (reject) {{
          document.body.classList.add('ab-pause-scrolling');
          reject.addEventListener('click', e => {{
            record('reject', e);
            document.querySelector('.trustarc-banner-overlay').remove();
            document.querySelector('#consent_blackbar').remove();
          }});
          document.querySelector('.ab-close-button').addEventListener('click', e => {{
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
    def __init__(self, *, overlays):
        self.overlays = overlays
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
                route.fulfill(status=200, content_type='text/html', body=performer_fixture(overlays=self.overlays))
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
        return browser, context


@unittest.skipUnless(os.environ.get('RUN_WEBKIT_OVERLAY_FIXTURE') == '1', 'opt-in stock WebKit fixture')
class NativeOverlayFixtureTests(unittest.TestCase):
    def setUp(self):
        vivid_webkit._blocked_category = None

    def check_capture(self, *, overlays):
        adapter = FixtureSession(overlays=overlays)
        self.addCleanup(adapter.close)
        payload, stamp = adapter.capture(EVENT)
        self.assertEqual(stamp, AT)
        self.assertEqual(len(payload['tickets']), 12)
        self.assertEqual(adapter.requests, [PERFORMER, EVENT, INVENTORY])
        self.assertEqual(adapter.unexpected_requests, [])
        performer = adapter.event_context.pages[0]
        expected = ['reject', 'close', 'game'] if overlays else ['game']
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
             {'action': 'close-promotion', 'outcome': 'clicked'}] if overlays else [])

    def test_native_reject_then_promo_close_unblocks_full_capture(self):
        self.check_capture(overlays=True)

    def test_no_overlay_keeps_the_normal_full_capture_path(self):
        self.check_capture(overlays=False)


if __name__ == '__main__':
    unittest.main()
