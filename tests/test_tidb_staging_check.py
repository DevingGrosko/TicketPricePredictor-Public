"""Offline checks for read-only SQL, target verification and secret-safe logs."""
from contextlib import redirect_stdout
import io
import os
import ssl
import tempfile
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy.exc import OperationalError
from Flask_App import tidb_staging_check as check
from Flask_App.tidb_staging import SCHEMAS, StagingTiDBConfig


class ConnectionCheckTests(unittest.TestCase):
    def setUp(self):
        self.config = StagingTiDBConfig("gateway.example.tidbcloud.com", "synthetic.user", "synthetic-secret")
        self.engine = MagicMock()
        self.connection = self.engine.connect.return_value.__enter__.return_value
        self.connection.execute.return_value.one.return_value = (1, SCHEMAS["mlb"], "8.0-TiDB-test")
        self.connection.execute.return_value.scalar_one.return_value = 0

    def run_check(self, sport="mlb"):
        with patch.object(check, "create_staging_engine", return_value=self.engine) as factory:
            check.check_schema(sport, self.config)
        return factory

    def test_success_uses_only_select_and_binds_staging_schema(self):
        self.run_check().assert_called_once_with("mlb", config=self.config)
        calls = self.connection.execute.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(str(call.args[0]).startswith("SELECT ") for call in calls))
        self.assertEqual(calls[1].args[1], {"schema": SCHEMAS["mlb"]})
        self.engine.dispose.assert_called_once()
        self.connection.commit.assert_not_called()

    def test_rejects_wrong_schema(self):
        self.connection.execute.return_value.one.return_value = (1, "sys", "TiDB")
        with self.assertRaisesRegex(check.CheckFailed, "wrong_database"):
            self.run_check()
        self.engine.dispose.assert_called_once()

    def test_rejects_non_tidb_server(self):
        self.connection.execute.return_value.one.return_value = (1, SCHEMAS["mlb"], "MySQL")
        with self.assertRaisesRegex(check.CheckFailed, "wrong_server"):
            self.run_check()

    def test_rejects_nonempty_schema_without_changing_it(self):
        self.connection.execute.return_value.scalar_one.return_value = 3
        with self.assertRaisesRegex(check.CheckFailed, "not_empty"):
            self.run_check()
        self.engine.dispose.assert_called_once()
        self.connection.commit.assert_not_called()

    def test_rejects_invalid_target_before_engine_creation(self):
        with patch.object(check, "create_staging_engine") as factory, self.assertRaises(check.CheckFailed):
            check.check_schema("sys", self.config)
        factory.assert_not_called()

    def test_connection_failure_disposes_engine(self):
        self.engine.connect.side_effect = RuntimeError("synthetic-secret")
        with self.assertRaises(RuntimeError):
            self.run_check()
        self.engine.dispose.assert_called_once()

    def test_missing_secrets_fail_before_connecting(self):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch.object(check, "create_staging_engine") as factory, redirect_stdout(output):
            self.assertEqual(check.main(), 1)
        factory.assert_not_called()
        self.assertIn("Missing or invalid", output.getvalue())

    def test_raw_errors_are_never_printed(self):
        for error in (RuntimeError("synthetic-secret"), ValueError("synthetic-secret"),
                      ssl.SSLError("synthetic-secret"), check.CheckFailed("synthetic-secret"),
                      OperationalError("private-sql", {}, Exception(1045, "synthetic-secret")),
                      OperationalError("private-sql", {}, Exception(2003, "synthetic-secret"))):
            with self.subTest(error_type=type(error)):
                output = check.safe_diagnostic(error)
                self.assertNotIn("synthetic-secret", output)
                self.assertNotIn("private-sql", output)

    def test_main_reports_each_schema_and_summary_without_credentials(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as root:
            summary = Path(root) / "summary.md"
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}, clear=True), \
                 patch.object(check.StagingTiDBConfig, "from_environment", return_value=self.config), \
                 patch.object(check, "check_schema") as runner, redirect_stdout(output):
                self.assertEqual(check.main(), 0)
            self.assertEqual(runner.call_count, 3)
            self.assertEqual(output.getvalue(), summary.read_text())
        for sport in SCHEMAS:
            self.assertIn("PASS " + sport, output.getvalue())
        self.assertNotIn(self.config.password, output.getvalue())
        self.assertNotIn(self.config.username, output.getvalue())

    def test_partial_failure_never_reports_overall_success(self):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(check.StagingTiDBConfig, "from_environment", return_value=self.config), \
             patch.object(check, "check_schema", side_effect=[None, RuntimeError("secret"), None]), \
             redirect_stdout(output):
            self.assertEqual(check.main(), 1)
        self.assertIn("FAIL nfl", output.getvalue())
        self.assertNotIn("PASS: all three", output.getvalue())


if __name__ == "__main__":
    unittest.main()
