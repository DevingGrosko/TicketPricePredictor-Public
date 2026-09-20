"""Offline staging-connection tests: no credentials or database are required."""
from __future__ import annotations

import os
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy.engine import make_url
from Flask_App import tidb_staging


class StagingTiDBTests(unittest.TestCase):
    def settings(self, **changes):
        # Synthetic test values only. No real account identifiers or secrets.
        result = {
            "TIDB_STAGING_HOST": "gateway01.example.prod.aws.tidbcloud.com",
            "TIDB_STAGING_USERNAME": "test-prefix.staging",
            "TIDB_STAGING_PASSWORD": "synthetic:p@ss/ word?#",
        }
        result.update(changes)
        return result

    def config(self, **changes):
        return tidb_staging.StagingTiDBConfig.from_environment(self.settings(**changes))

    def test_uses_required_tidb_port_and_mysql_driver(self):
        url = self.config().url("mlb")
        self.assertEqual(url.drivername, "mysql+pymysql")
        self.assertEqual(url.port, 4000)
        self.assertEqual(url.query["charset"], "utf8mb4")

    def test_schemas_are_distinct_and_staging_only(self):
        databases = {self.config().url(sport).database for sport in ("mlb", "nfl", "nhl")}
        self.assertEqual(databases, {
            "ticketsignal_staging_mlb", "ticketsignal_staging_nfl", "ticketsignal_staging_nhl"
        })

    def test_rejects_arbitrary_or_system_schema_requests(self):
        for value in ("sys", "mysql", "information_schema", "concerts", "", "ticket$mlb", "mlb; DROP TABLE t"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.config().url(value)

    def test_rejects_production_hosts_and_suffix_lookalikes(self):
        for host in (
            "example.mysql.pythonanywhere-services.com", "localhost", "127.0.0.1",
            "tidbcloud.com.attacker.example", "evil-tidbcloud.com", "tidbcloud.com",
            "https://gateway.tidbcloud.com", "user@gateway.tidbcloud.com", "-bad.tidbcloud.com",
        ):
            with self.subTest(host=host), self.assertRaises(ValueError):
                self.config(TIDB_STAGING_HOST=host)

    def test_normalizes_hostname_and_username_but_preserves_password(self):
        config = self.config(
            TIDB_STAGING_HOST=" GATEWAY.example.tidbcloud.com ",
            TIDB_STAGING_USERNAME=" prefix.user ",
            TIDB_STAGING_PASSWORD=" password with spaces ",
        )
        self.assertEqual(config.host, "gateway.example.tidbcloud.com")
        self.assertEqual(config.username, "prefix.user")
        self.assertEqual(config.password, " password with spaces ")

    def test_missing_values_do_not_fall_back_to_production(self):
        production = {
            "MYSQL_HOST": "example.mysql.pythonanywhere-services.com",
            "MYSQL_USERNAME": "production-user", "MYSQL_PASSWORD": "production-secret",
            "TICKETSIGNAL_DATABASE_BACKEND": "mysql",
        }
        with patch.dict(os.environ, production, clear=True), self.assertRaises(ValueError):
            tidb_staging.StagingTiDBConfig.from_environment()

    def test_each_required_value_is_checked_without_leaking_others(self):
        for name in self.settings():
            values = self.settings(**{name: " "})
            with self.subTest(name=name), self.assertRaises(ValueError) as caught:
                tidb_staging.StagingTiDBConfig.from_environment(values)
            self.assertIn(name, str(caught.exception))
            self.assertNotIn(self.settings()["TIDB_STAGING_PASSWORD"], str(caught.exception))

    def test_direct_constructor_also_validates(self):
        with self.assertRaises(ValueError):
            tidb_staging.StagingTiDBConfig("localhost", "user", "pass")
        for username, password in (("", "pass"), ("user", ""), ("user", " ")):
            with self.subTest(username=username, password=password), self.assertRaises(ValueError):
                tidb_staging.StagingTiDBConfig("gateway.example.tidbcloud.com", username, password)

    def test_password_is_hidden_from_repr_and_default_url_rendering(self):
        config = self.config()
        self.assertNotIn(config.password, repr(config))
        self.assertNotIn(config.password, str(config.url("nfl")))
        self.assertNotIn(config.password, repr(config.url("nfl")))

    def test_special_password_characters_round_trip(self):
        original = self.config().url("nhl")
        restored = make_url(original.render_as_string(hide_password=False))
        self.assertEqual(restored.password, original.password)
        self.assertEqual(restored.username, original.username)

    def test_tls_requires_hostname_certificate_and_modern_version(self):
        context = self.config().tls_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_nonexistent_ca_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as root, self.assertRaises(ValueError):
            self.config(TIDB_STAGING_CA_FILE=str(Path(root) / "missing.pem"))

    def test_invalid_ca_file_does_not_disable_verification(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "invalid.pem"
            path.write_text("not a certificate")
            with self.assertRaises(ssl.SSLError):
                self.config(TIDB_STAGING_CA_FILE=str(path)).tls_context()

    def test_engine_is_lazy_bounded_and_has_verified_tls(self):
        with patch.object(tidb_staging, "create_engine") as create:
            returned = tidb_staging.create_staging_engine("mlb", config=self.config())
        self.assertIs(returned, create.return_value)
        create.assert_called_once()
        url = create.call_args.args[0]
        options = create.call_args.kwargs
        self.assertEqual(url.database, "ticketsignal_staging_mlb")
        self.assertTrue(options["hide_parameters"])
        self.assertFalse(options["echo"])
        self.assertTrue(options["pool_pre_ping"])
        self.assertEqual(options["pool_size"], 1)
        self.assertEqual(options["max_overflow"], 0)
        args = options["connect_args"]
        self.assertEqual((args["connect_timeout"], args["read_timeout"], args["write_timeout"]), (10, 90, 90))
        self.assertTrue(args["ssl"].check_hostname)
        self.assertEqual(args["ssl"].verify_mode, ssl.CERT_REQUIRED)

    def test_invalid_target_is_rejected_before_engine_creation(self):
        with patch.object(tidb_staging, "create_engine") as create, self.assertRaises(ValueError):
            tidb_staging.create_staging_engine("sys", config=self.config())
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
