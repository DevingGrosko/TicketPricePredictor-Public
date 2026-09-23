"""Synthetic-only preview tests; no remote database or production credentials."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask
from sqlalchemy import create_engine, event
from Flask_App import database_config as db
from Flask_App import staging_site_config as cfg
from Flask_App import staging_site as site

ENV = {
    'TICKETSIGNAL_STAGING_SITE': '1',
    'TIDB_STAGING_HOST': 'gateway.example.tidbcloud.com',
    'TIDB_STAGING_USERNAME': 'synthetic.preview',
    'TIDB_STAGING_PASSWORD': 'synthetic:p@ss/ word',
    'FLASK_SECRET_KEY': 'synthetic-preview-key-not-for-production',
}


class RoutingTests(unittest.TestCase):
    def tearDown(self):
        cfg.clear_engines()
        cfg.BLOCKED_SQL.clear()
        db._MYSQL_ENGINES.clear()

    def test_default_sqlite_unchanged(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(db, 'load_dotenv'):
            self.assertEqual(db.configured_backend(), 'sqlite')

    def test_original_mysql_url_unchanged(self):
        env = {'TICKETSIGNAL_DATABASE_BACKEND': 'mysql', 'MYSQL_HOST': 'source.example',
               'MYSQL_USERNAME': 'old-user', 'MYSQL_PASSWORD': 'synthetic', 'MYSQL_MLB_DATABASE': 'old_mlb'}
        with patch.dict(os.environ, env, clear=True), patch.object(db, 'load_dotenv'):
            url = db.mysql_url('mlb')
            self.assertEqual((url.host, url.database, url.port), ('source.example', 'old_mlb', None))
            self.assertEqual(db.configured_backend(), 'mysql')

    def test_original_explicit_sqlite_override_unchanged(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=True):
            engine = db.create_ticket_engine('mlb', sqlite_path=Path(root)/'t.db', force_sqlite=True)
            self.assertEqual(engine.dialect.name, 'sqlite'); engine.dispose()

    def test_invalid_opt_in_does_not_silently_use_production(self):
        with patch.dict(os.environ, {'TICKETSIGNAL_STAGING_SITE': 'tru'}, clear=True):
            with self.assertRaises(RuntimeError): db.configured_backend()
            with self.assertRaises(RuntimeError): cfg.enabled()

    def test_staging_never_loads_dotenv(self):
        with patch.dict(os.environ, ENV, clear=True), patch.object(db, 'load_dotenv') as load:
            self.assertEqual(db.configured_backend(), 'mysql')
            for sport in ('mlb', 'nfl', 'nhl'):
                url = db.mysql_url(sport)
                self.assertEqual(url.database, 'ticketsignal_staging_' + sport)
                self.assertEqual(url.port, 4000)
            load.assert_not_called()

    def test_staging_routes_to_isolated_engine_only(self):
        with patch.dict(os.environ, ENV, clear=True), patch.object(cfg, 'engine_for') as make, patch.object(db, 'create_engine') as legacy:
            self.assertIs(db.create_ticket_engine('nfl', sqlite_path='unused.db'), make.return_value)
            make.assert_called_once_with('nfl'); legacy.assert_not_called()

    def test_staging_refuses_sqlite_override_and_setting_rewrite(self):
        with patch.dict(os.environ, ENV, clear=True):
            with self.assertRaises(RuntimeError): db.create_ticket_engine('mlb', sqlite_path='unused.db', force_sqlite=True)
            with self.assertRaises(RuntimeError): db.update_backend_setting('mysql')

    def test_missing_credentials_stop(self):
        env = dict(ENV); env.pop('TIDB_STAGING_PASSWORD')
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError): db.configured_backend()

    def test_production_settings_are_rejected(self):
        for key in ('MYSQL_HOST', 'MYSQL_PASSWORD', 'COLLECTOR_INGEST_TOKEN', 'DATABASE_PATH', 'PYTHONANYWHERE_SSH_KEY'):
            with self.subTest(key=key), patch.dict(os.environ, {**ENV, key:'synthetic'}, clear=True):
                with self.assertRaises(RuntimeError): cfg.validate_environment()

    def test_sqlite_backend_not_accepted_for_preview(self):
        with patch.dict(os.environ, {**ENV, 'TICKETSIGNAL_DATABASE_BACKEND':'sqlite'}, clear=True):
            with self.assertRaises(RuntimeError): cfg.validate_environment()

    def test_clean_checkout_required(self):
        with patch.dict(os.environ, ENV, clear=True), patch.object(Path, 'exists', return_value=True):
            with self.assertRaises(RuntimeError): cfg.validate_environment()

    def test_separate_web_secret_required_before_app_import(self):
        with patch.dict(os.environ, {**ENV, 'FLASK_SECRET_KEY':'short'}, clear=True), patch.object(site.importlib, 'import_module') as imp:
            with self.assertRaises(RuntimeError): site.create_app()
            imp.assert_not_called()

    def test_unknown_sport_and_host_rejected(self):
        with patch.dict(os.environ, ENV, clear=True):
            with self.assertRaises(ValueError): cfg.engine_for('concerts')
        with patch.dict(os.environ, {**ENV, 'TIDB_STAGING_HOST':'production.example'}, clear=True):
            with self.assertRaises(ValueError): cfg.engine_for('mlb')

    def test_lazy_pooled_engine_has_tls_and_fixed_schema(self):
        with patch.dict(os.environ, ENV, clear=True):
            engine = cfg.engine_for('mlb')
            self.assertIs(engine, cfg.engine_for('mlb'))
            self.assertEqual(engine.url.port, 4000)
            self.assertEqual(engine.url.database, 'ticketsignal_staging_mlb')
            self.assertTrue(engine.hide_parameters)
            self.assertNotIn(ENV['TIDB_STAGING_PASSWORD'], str(engine.url))

    def test_wrong_server_or_schema_fails_connection_check(self):
        for row in [('production', 'TiDB', 1), ('ticketsignal_staging_mlb','MySQL',1), ('ticketsignal_staging_mlb','TiDB',0)]:
            c=MagicMock(); c.cursor.return_value.__enter__.return_value.fetchone.return_value=row
            with self.subTest(row=row), self.assertRaises(RuntimeError): cfg.check_connection(c,'ticketsignal_staging_mlb')


class GuardTests(unittest.TestCase):
    def tearDown(self): cfg.BLOCKED_SQL.clear()

    def test_read_queries_accepted(self):
        for q in ('SELECT 1', 'SHOW CREATE TABLE `event`', 'DESCRIBE `event`', 'SELECT event.id FROM event WHERE event.Place=%s'):
            cfg.require_read_sql(q)

    def test_nonreads_and_side_effect_reads_rejected(self):
        for q in ('INSERT INTO x VALUES (1)', 'DELETE FROM x', 'UPDATE x SET x=1',
                  'CREATE TABLE x(x INT)', 'DROP TABLE x', 'ALTER TABLE x ADD y INT',
                  'TRUNCATE x', 'SET FOREIGN_KEY_CHECKS=0', 'SELECT 1; DROP TABLE x',
                  'SELECT * FROM x FOR UPDATE', "SELECT GET_LOCK('x',1)",
                  "SELECT 1 INTO OUTFILE '/tmp/x'", 'SELECT /* comment */ 1'):
            with self.subTest(q=q), self.assertRaises(cfg.StagingReadOnlyError): cfg.require_read_sql(q)

    def test_real_sqlalchemy_hook_blocks_before_execution(self):
        engine=create_engine('sqlite://')
        @event.listens_for(engine, 'before_cursor_execute')
        def guard(_a,_b,s,_d,_e,_f): cfg.require_read_sql(s)
        with engine.connect() as c:
            self.assertEqual(c.exec_driver_sql('SELECT 1').scalar(),1)
            with self.assertRaises(cfg.StagingReadOnlyError): c.exec_driver_sql('CREATE TABLE forbidden(x INT)')
        engine.dispose()


class PreviewHttpTests(unittest.TestCase):
    def setUp(self):
        self.app=Flask(__name__)
        self.app.add_url_rule('/', 'home', lambda:'<html><body>Original page</body></html>')
        self.app.add_url_rule('/concerts', 'concerts_home', lambda:'must not run')
        self.app.add_url_rule('/write', 'write', lambda:'must not run', methods=['POST'])
        site.install_preview(self.app); self.client=self.app.test_client()

    def test_html_preserves_page_and_adds_banner_headers(self):
        r=self.client.get('/')
        self.assertEqual(r.status_code,200)
        self.assertIn(b'Original page',r.data); self.assertIn(b'ticketsignal-staging-banner',r.data)
        self.assertEqual(r.headers['X-TicketSignal-Environment'],'staging-readonly')
        self.assertIn('noindex',r.headers['X-Robots-Tag'])

    def test_writes_and_concerts_blocked(self):
        self.assertEqual(self.client.post('/write').status_code,409)
        self.assertEqual(self.client.post('/api/collector/snapshot').status_code,409)
        self.assertEqual(self.client.get('/concerts').status_code,503)

    def test_health_is_independent_of_database(self):
        with patch.object(cfg,'engine_for') as engine:
            self.assertEqual(self.client.get('/healthz').status_code,200)
            engine.assert_not_called()

    def test_ready_checks_each_database_and_masks_errors(self):
        with patch.object(cfg,'engine_for') as engine:
            self.assertEqual(self.client.get('/readyz').status_code,200)
            self.assertEqual(engine.call_count,3)
        with patch.object(cfg,'engine_for',side_effect=RuntimeError('secret-details')):
            r=self.client.get('/readyz')
            self.assertEqual(r.status_code,503); self.assertNotIn(b'secret-details',r.data)

    def test_guard_installation_is_idempotent(self):
        site.install_preview(self.app)
        self.assertEqual(self.client.get('/').data.count(b'ticketsignal-staging-banner'),1)


if __name__ == '__main__': unittest.main()
