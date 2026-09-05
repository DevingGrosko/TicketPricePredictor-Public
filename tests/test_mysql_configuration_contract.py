from pathlib import Path
import unittest


class MySQLConfigurationContractTests(unittest.TestCase):
    def test_active_sports_use_independent_mysql_databases(self):
        root = Path(__file__).resolve().parents[1]

        config = (root / "Flask_App/database_config.py").read_text(
            encoding="utf-8"
        )
        for variable in (
            "MYSQL_MLB_DATABASE",
            "MYSQL_NFL_DATABASE",
            "MYSQL_NHL_DATABASE",
        ):
            with self.subTest(variable=variable):
                self.assertIn(variable, config)
        self.assertIn("def create_ticket_engine", config)
        self.assertIn("def create_mysql_engine", config)

        integrations = {
            "models.py": 'create_ticket_engine(\n            "mlb"',
            "Flask_App/nfl_blueprint.py": 'create_ticket_engine(\n            "nfl"',
            "Flask_App/nhl_blueprint.py": 'create_ticket_engine(\n            "nhl"',
        }
        for relative, expected in integrations.items():
            with self.subTest(relative=relative):
                text = (root / relative).read_text(encoding="utf-8")
                self.assertIn(expected, text)

    def test_migration_preserves_source_sqlite_files_and_verifies_copy(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "Flask_App/mysql_cutover.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("source_path.unlink(", text)
        self.assertNotIn("os.remove(source_path", text)
        self.assertNotIn("source_path.replace(", text)
        self.assertIn("def _source_fingerprint", text)
        self.assertIn("SQLite source changed during migration", text)
        self.assertIn("def verify_mysql_against_manifest", text)
        self.assertIn("foreign-key orphan verification failed", text)


if __name__ == "__main__":
    unittest.main()
