"""One-time, read-only graph benchmark. No imports, writes or deployment.

Only run main with the explicit isolated preview settings. Legacy series code
is retained solely as a comparison reference, not as a second production path.
Logs contain counts/timings only, never credentials, SQL parameters or prices.
"""
from __future__ import annotations

import json
import signal
import time
from unittest.mock import patch

from flask import template_rendered
from sqlalchemy import event
import graph_builder as graphs
from models import Event, Iteration, Ticket, hours_before_event


def legacy_series(self, section, event_id):
    """The pre-optimization eachEventGraphList logic, for output comparison."""
    SessionLocal = graphs.CreateModel().getSession()
    x, y = [], []
    with SessionLocal() as session:
        tickets = (
            session.query(Ticket).join(Ticket.iteration).join(Iteration.event)
            .filter(Ticket.section == section, Event.id == event_id,
                    Event.URL.like('%--sports-mlb-baseball/%'))
            .order_by(Iteration.captured_at.asc()).all()
        )
        for ticket in tickets:
            x.append(round(hours_before_event(ticket.iteration.event.event_date,
                                              ticket.iteration.captured_at), 3))
            y.append(ticket.price)
    return x, y


def main():
    from Flask_App import staging_site_config as cfg
    cfg.validate_environment(website=True)
    from Flask_App.staging_site import create_app
    app = create_app()
    app.logger.disabled = True
    client = app.test_client()
    result = {'mode': 'staging-readonly', 'deployed': False, 'rows_written': 0}
    reads = [0]
    def count_reads(_a, _b, _sql, _d, _e, _f):
        reads[0] += 1
    def deadline(_a, _b):
        raise TimeoutError('Bounded graph benchmark timed out.')
    old_handler = signal.signal(signal.SIGALRM, deadline)
    signal.alarm(240)
    engine = None
    try:
        contexts = []
        def capture(_sender, template, context, **_kw):
            contexts.append(context)
        with template_rendered.connected_to(capture, app):
            response = client.get('/')
        if response.status_code != 200 or not contexts:
            raise RuntimeError('Could not select a populated MLB preview.')
        venue = next(iter(contexts[-1]['games_dict']))
        response = client.get('/api/baseball/options', query_string={'venue': venue})
        if response.status_code != 200:
            raise RuntimeError('Could not read preview options.')
        options = response.get_json()
        choices = [(int(game), sections[0]) for game, sections in
                   options.get('sections_by_game', {}).items() if sections]
        if not choices:
            raise RuntimeError('No testable graph choice.')
        game, section = choices[0]
        builder = graphs.GraphBuilder()
        engine = cfg.engine_for('mlb')
        event.listen(engine, 'after_cursor_execute', count_reads)
        measured = {}
        outputs = {}
        for label, function in (('legacy_series', legacy_series),
                                ('scalar_series', graphs.GraphBuilder.eachEventGraphList)):
            reads[0] = 0
            started = time.monotonic()
            outputs[label] = function(builder, section, game)
            measured[label] = {'seconds': round(time.monotonic() - started, 4),
                               'sql_reads': reads[0], 'points': len(outputs[label][0])}
            print('SERIES_MEASUREMENT ' + json.dumps({label: measured[label]}), flush=True)
        if not outputs['legacy_series'][0] or outputs['legacy_series'] != outputs['scalar_series']:
            raise RuntimeError('Graph values/order changed.')
        if measured['scalar_series']['sql_reads'] >= measured['legacy_series']['sql_reads']:
            raise RuntimeError('Graph round trips were not reduced.')
        query = {'event': venue, 'game': game, 'section': section,
                 'mode': 'single', 'display': 'money'}
        charts = {}
        for label, function in (('legacy_page', legacy_series),
                                ('optimized_page', graphs.GraphBuilder.eachEventGraphList)):
            contexts.clear()
            reads[0] = 0
            started = time.monotonic()
            with patch.object(graphs.GraphBuilder, 'eachEventGraphList', function):
                with template_rendered.connected_to(capture, app):
                    response = client.get('/graph', query_string=query)
            if response.status_code != 200 or not contexts:
                raise RuntimeError('Graph page failed.')
            context = contexts[-1]
            charts[label] = (context.get('chartX'), context.get('chartY'))
            measured[label] = {'seconds': round(time.monotonic() - started, 4),
                               'sql_reads': reads[0], 'http_status': response.status_code}
            print('PAGE_MEASUREMENT ' + json.dumps({label: measured[label]}), flush=True)
        if not charts['legacy_page'][0] or charts['legacy_page'] != charts['optimized_page']:
            raise RuntimeError('Rendered chart values changed.')
        if cfg.BLOCKED_SQL:
            raise RuntimeError('A blocked SQL operation was attempted.')
        result.update(passed=True, exact_series_match=True, exact_chart_match=True,
                      measurements=measured, blocked_sql_attempts=0)
        print('GRAPH_PERFORMANCE_REPORT ' + json.dumps(result, sort_keys=True), flush=True)
        return 0
    except Exception as error:
        result.update(passed=False, error_type=type(error).__name__)
        print('GRAPH_PERFORMANCE_REPORT ' + json.dumps(result, sort_keys=True), flush=True)
        return 1
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        if engine is not None:
            event.remove(engine, 'after_cursor_execute', count_reads)
        cfg.clear_engines()


if __name__ == '__main__':
    raise SystemExit(main())
