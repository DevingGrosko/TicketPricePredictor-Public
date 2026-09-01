import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


class FlaskProductionStartupTests(unittest.TestCase):
    def test_fresh_app_import_with_pythonanywhere_deploy_allowlist(self):
        """Exercise the exact files synchronized by the restricted deploy key."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = os.environ.copy()
            env.update(
                {
                    "DATABASE_PATH": str(root / "baseball.db"),
                    "CONCERT_DATABASE_PATH": str(root / "concerts.db"),
                    "NFL_DATABASE_PATH": str(root / "nfl.db"),
                    "NHL_DATABASE_PATH": str(root / "nhl.db"),
                    "COLLECTOR_INGEST_TOKEN": "integration-test-token",
                    "FLASK_SECRET_KEY": "integration-test-secret",
                }
            )
            script = textwrap.dedent(
                """
                from datetime import datetime, timedelta, timezone
                import importlib.abc
                import sys

                # The live deploy synchronizes Flask_App, models.py, and
                # graph_builder.py. Root collector programs must not be
                # required for WSGI startup. League blueprints live inside
                # Flask_App and are part of the deployed application.
                class BlockUndeployedRootModules(importlib.abc.MetaPathFinder):
                    blocked = {
                        "concert_models",
                        "concert_graph_builder",
                        "concert_collector",
                        "legacy_concert_migration",
                        "nhl_collector",
                        "nhl_schedule_collector",
                    }

                    def find_spec(self, fullname, path=None, target=None):
                        if fullname in self.blocked:
                            raise ImportError(
                                f"{fullname} is unavailable in the restricted deployment"
                            )
                        return None

                sys.meta_path.insert(0, BlockUndeployedRootModules())

                from models import Base, database_path
                from Flask_App.flask_app import app

                Base.metadata.create_all(
                    __import__("sqlalchemy").create_engine(
                        f"sqlite:///{database_path()}"
                    )
                )

                now = datetime.now(timezone.utc).replace(
                    minute=0, second=0, microsecond=0
                )
                event_date = now + timedelta(days=2)
                headers = {
                    "Authorization": "Bearer integration-test-token",
                    "Content-Type": "application/json",
                }
                client = app.test_client()

                nfl_sections = [
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
                nfl_payload = {
                    "schema_version": 1,
                    "event_type": "nfl",
                    "captured_at": now.isoformat(),
                    "event_date": event_date.isoformat(),
                    "source_url": (
                        "https://www.vividseats.com/nfl-game/production/1234567"
                    ),
                    "source_id": "1234567",
                    "title": "Dallas Cowboys at New York Giants",
                    "venue": "MetLife Stadium",
                    "section_count": len(nfl_sections),
                    "sections": nfl_sections,
                }

                first = client.post(
                    "/api/nfl/snapshot", json=nfl_payload, headers=headers
                )
                assert first.status_code == 201, first.get_data(as_text=True)
                assert first.get_json()["status"] == "stored"
                nfl_event_id = first.get_json()["event_id"]

                duplicate = client.post(
                    "/api/nfl/snapshot", json=nfl_payload, headers=headers
                )
                assert duplicate.status_code == 200, duplicate.get_data(as_text=True)
                assert duplicate.get_json()["status"] == "duplicate"

                nhl_sections = [
                    {
                        "section": f"Section {100 + index}",
                        "price": 90 + index,
                        "listing_count": 2,
                        "row": "A",
                        "quantity": "2",
                        "displayed_price": str(90 + index),
                        "alternate_price": "",
                        "price_source": "p",
                    }
                    for index in range(10)
                ]
                nhl_payload = {
                    "schema_version": 1,
                    "event_type": "nhl",
                    "captured_at": now.isoformat(),
                    "event_date": event_date.isoformat(),
                    "source_url": (
                        "https://www.vividseats.com/nhl-game/production/2234567"
                    ),
                    "source_id": "2234567",
                    "title": "Boston Bruins at Toronto Maple Leafs",
                    "venue": "Scotiabank Arena",
                    "currency": "USD",
                    "schedule": {
                        "schedule_id": "2026020001",
                        "away_team": "Boston Bruins",
                        "home_team": "Toronto Maple Leafs",
                        "canonical_venue": "Scotiabank Arena",
                        "venue_timezone": "America/Toronto",
                        "country": "Canada",
                        "neutral_site": False,
                        "game_type": 2,
                        "season": 20262027,
                    },
                    "section_count": len(nhl_sections),
                    "sections": nhl_sections,
                }

                first_nhl = client.post(
                    "/api/nhl/snapshot", json=nhl_payload, headers=headers
                )
                assert first_nhl.status_code == 201, first_nhl.get_data(as_text=True)
                assert first_nhl.get_json()["status"] == "stored"
                assert first_nhl.get_json()["currency"] == "USD"
                nhl_event_id = first_nhl.get_json()["event_id"]

                duplicate_nhl = client.post(
                    "/api/nhl/snapshot", json=nhl_payload, headers=headers
                )
                assert duplicate_nhl.status_code == 200, duplicate_nhl.get_data(as_text=True)
                assert duplicate_nhl.get_json()["status"] == "duplicate"

                wrong_baseball_endpoint = client.post(
                    "/api/collector/snapshot", json=nfl_payload, headers=headers
                )
                assert wrong_baseball_endpoint.status_code == 400

                wrong_concert_endpoint = client.post(
                    "/api/concerts/snapshot", json=nfl_payload, headers=headers
                )
                assert wrong_concert_endpoint.status_code == 400

                homepage = client.get("/")
                assert homepage.status_code == 200
                assert b"Ticket price intelligence" in homepage.data
                assert b"NFL market tracker" not in homepage.data
                assert b"NHL market tracker" not in homepage.data

                baseball_alias = client.get("/baseball")
                assert baseball_alias.status_code == 200
                assert b"Ticket price intelligence" in baseball_alias.data

                nfl_page = client.get("/nfl")
                assert nfl_page.status_code == 200
                assert b"NFL market tracker" in nfl_page.data
                assert b"Dallas Cowboys" in nfl_page.data

                nfl_map = client.get(
                    f"/nfl/map?team=New%20York%20Giants&game={nfl_event_id}"
                )
                assert nfl_map.status_code == 200
                assert b"Interactive section explorer" in nfl_map.data
                assert b"MetLife Stadium" in nfl_map.data

                nhl_page = client.get("/nhl")
                assert nhl_page.status_code == 200
                assert b"NHL market tracker" in nhl_page.data
                assert b"Toronto Maple Leafs" in nhl_page.data

                nhl_map = client.get(
                    f"/nhl/map?team=Toronto%20Maple%20Leafs&game={nhl_event_id}"
                )
                assert nhl_map.status_code == 200
                assert b"Interactive arena explorer" in nhl_map.data
                assert b"Scotiabank Arena" in nhl_map.data
                """
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=90,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
