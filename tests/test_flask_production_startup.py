import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


class FlaskProductionStartupTests(unittest.TestCase):
    def test_fresh_app_import_with_pythonanywhere_deploy_allowlist(self):
        """Prove the live app needs only files synchronized by the deploy key."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = os.environ.copy()
            env.update(
                {
                    "DATABASE_PATH": str(root / "baseball.db"),
                    "CONCERT_DATABASE_PATH": str(root / "concerts.db"),
                    "COLLECTOR_INGEST_TOKEN": "integration-test-token",
                    "FLASK_SECRET_KEY": "integration-test-secret",
                }
            )
            script = textwrap.dedent(
                """
                from datetime import datetime, timedelta, timezone
                import importlib.abc
                import sys

                # PythonAnywhere's restricted deploy key synchronizes
                # Flask_App, models.py, and graph_builder.py. These modular
                # compatibility files are intentionally absent from the live
                # import path and must never be required by the WSGI app.
                class BlockUndeployedConcertModules(importlib.abc.MetaPathFinder):
                    blocked = {
                        "concert_models",
                        "concert_graph_builder",
                        "concert_collector",
                        "legacy_concert_migration",
                    }

                    def find_spec(self, fullname, path=None, target=None):
                        if fullname in self.blocked:
                            raise ImportError(
                                f"{fullname} is unavailable in the restricted deployment"
                            )
                        return None

                sys.meta_path.insert(0, BlockUndeployedConcertModules())

                from Flask_App.flask_app import app

                now = datetime.now(timezone.utc).replace(
                    minute=0, second=0, microsecond=0
                )
                event_date = now + timedelta(days=2)
                url = (
                    "https://www.vividseats.com/test-artist-tickets-"
                    f"{event_date.month}-{event_date.day}-{event_date.year}"
                    "--concerts-pop/production/1234567"
                )
                sections = [
                    {
                        "section": f"Section {index}",
                        "price": 50 + index,
                        "listing_count": 1,
                        "row": "",
                        "quantity": "2",
                        "displayed_price": str(50 + index),
                        "alternate_price": "",
                        "price_source": "p",
                    }
                    for index in range(10)
                ]
                payload = {
                    "schema_version": 1,
                    "event_type": "concert",
                    "captured_at": now.isoformat(),
                    "event_date": event_date.isoformat(),
                    "source_url": url,
                    "source_id": "1234567",
                    "title": "Test Artist: World Tour",
                    "venue": "Test Arena",
                    "section_count": len(sections),
                    "sections": sections,
                }
                headers = {
                    "Authorization": "Bearer integration-test-token",
                    "Content-Type": "application/json",
                }

                client = app.test_client()
                first = client.post(
                    "/api/concerts/snapshot", json=payload, headers=headers
                )
                assert first.status_code == 201, first.get_data(as_text=True)
                assert first.get_json()["status"] == "stored"

                duplicate = client.post(
                    "/api/concerts/snapshot", json=payload, headers=headers
                )
                assert duplicate.status_code == 200, duplicate.get_data(as_text=True)
                assert duplicate.get_json()["status"] == "duplicate"

                wrong_endpoint = client.post(
                    "/api/collector/snapshot", json=payload, headers=headers
                )
                assert wrong_endpoint.status_code == 400

                concerts_page = client.get("/concerts")
                assert concerts_page.status_code == 200
                assert b"Test Artist" in concerts_page.data
                """
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=60,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
