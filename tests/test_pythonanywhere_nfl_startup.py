from datetime import datetime, timezone
from pathlib import Path
import unittest

from Flask_App.flask_app import app
from Flask_App import nfl_blueprint as nfl_runtime


class PythonAnywhereNFLStartupTests(unittest.TestCase):
    def test_blueprint_has_no_new_root_module_runtime_dependency(self):
        source = Path(nfl_runtime.__file__).read_text(encoding="utf-8")
        self.assertNotIn("from nfl_metadata import", source)
        self.assertEqual(
            nfl_runtime.canonical_venue_name("US Bank Stadium"),
            "U.S. Bank Stadium",
        )
        summer = datetime(2026, 8, 30, 5, tzinfo=timezone.utc)
        self.assertTrue(nfl_runtime.eastern_iso(summer).endswith("-04:00"))

    def test_nfl_routes_are_registered_at_application_startup(self):
        routes = {rule.rule for rule in app.url_map.iter_rules()}
        self.assertIn("/nfl", routes)
        self.assertIn("/nfl/archive", routes)
        self.assertIn("/nfl/map", routes)
        self.assertIn("/api/nfl/snapshot", routes)
        self.assertIn("/nhl", routes)
        self.assertIn("/nhl/map", routes)
        self.assertIn("/nhl/graph", routes)
        self.assertIn("/api/nhl/snapshot", routes)


if __name__ == "__main__":
    unittest.main()
