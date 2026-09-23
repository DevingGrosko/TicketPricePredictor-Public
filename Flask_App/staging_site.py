"""Reuse the existing Flask views in an explicitly isolated snapshot preview."""
from __future__ import annotations

import importlib
import os
import re

from flask import jsonify, request
from sqlalchemy import text
from Flask_App import staging_site_config as settings

BANNER = (
    '<div id="ticketsignal-staging-banner" role="status" '
    'style="padding:12px;text-align:center;background:#fff3cd;color:#332701;font:14px sans-serif">'
    'STAGING PREVIEW — saved September 21 snapshots; not live prices. '
    'Read-only: price ingestion and concerts are not enabled here.'
    '</div>'
)


def install_preview(app):
    if app.config.get('TICKETSIGNAL_PREVIEW_INSTALLED'):
        return app
    app.config.update(TICKETSIGNAL_PREVIEW_INSTALLED=True, DEBUG=False,
                      PROPAGATE_EXCEPTIONS=False, SESSION_COOKIE_SECURE=True,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')

    def protect_snapshot():
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            return jsonify(status='staging_readonly', message='Writes are disabled on this preview.'), 409
        if 'concert' in (request.endpoint or '').lower() or '/concert' in request.path.lower():
            return jsonify(status='not_migrated', message='Concert history is not available in this sports-only preview.'), 503
        return None

    app.before_request_funcs.setdefault(None, []).insert(0, protect_snapshot)

    @app.after_request
    def label_preview(response):
        response.headers['X-TicketSignal-Environment'] = 'staging-readonly'
        response.headers['X-Robots-Tag'] = 'noindex, nofollow'
        if (response.mimetype == 'text/html' and not response.direct_passthrough
                and response.status_code == 200):
            body = response.get_data(as_text=True)
            body = re.sub(r'(<body\b[^>]*>)', lambda m: m[0] + BANNER, body, count=1, flags=re.I)
            response.set_data(body)
        return response

    @app.get('/healthz')
    def staging_health():
        return jsonify(status='ok', environment='staging-readonly')

    @app.get('/readyz')
    def staging_ready():
        try:
            for sport in settings.SCHEMAS:
                with settings.engine_for(sport).connect() as c:
                    c.execute(text('SELECT 1')).scalar_one()
        except Exception:
            return jsonify(status='unavailable', environment='staging-readonly'), 503
        return jsonify(status='ok', environment='staging-readonly', databases=3)

    @app.errorhandler(settings.StagingReadOnlyError)
    def blocked_database_write(_error):
        return jsonify(status='staging_readonly', message='This request requires a database write, which is disabled during preview validation.'), 503

    return app


def create_app():
    settings.validate_environment(website=True)
    os.environ.setdefault('MPLBACKEND', 'Agg')
    app = importlib.import_module('Flask_App.flask_app').app
    return install_preview(app)
