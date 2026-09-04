from pathlib import Path
import unittest


class MySQLConfigurationContractTests(unittest.TestCase):
    def test_active_sports_have_independent_database_urls(self):
        root = Path(__file__).resolve().parents[1]
        expected = {
            "models.py": "BASEBALL_DATABASE_URL",
            "Flask_App/nfl_blueprint.py": "NFL_DATABASE_URL",
            "Flask_App/nhl_blueprint.py": "NHL_DATABASE_URL",
        }
        for relative, variable in expected.items():
            with self.subTest(relative=relative):
                text = (root / relative).read_text(encoding="utf-8")
                self.assertIn(variable, text)
                self.assertIn("create_runtime_engine", text)

    def test_migration_never_replaces_source_sqlite_files(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "tools/migrate_sqlite_to_mysql.py").read_text(encoding="utf-8")
        self.assertNotIn("unlink(", text)
        self.assertNotIn("os.remove", text)
        self.assertIn("verify_counts", text)


if __name__ == "__main__":
    unittest.main()
