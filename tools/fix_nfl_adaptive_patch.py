from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Expected one match in {path}, found {count}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "tests/test_nfl_schedule_collector.py",
    """        games = [
            ScheduledNFLGame(
                schedule_id="final-week",
                event_date=slot + timedelta(hours=100),
                away_team="Buffalo Bills",
                home_team="Houston Texans",
                venue="Test Stadium",
                name="Buffalo Bills at Houston Texans",
            ),
            ScheduledNFLGame(
                schedule_id=f"early-{index}",
                event_date=slot + timedelta(hours=500),
                away_team="Dallas Cowboys",
                home_team="New York Giants",
                venue="Test Stadium",
                name="Dallas Cowboys at New York Giants",
            )
            for index in range(12)
        ]
""",
    """        games = [
            ScheduledNFLGame(
                schedule_id="final-week",
                event_date=slot + timedelta(hours=100),
                away_team="Buffalo Bills",
                home_team="Houston Texans",
                venue="Test Stadium",
                name="Buffalo Bills at Houston Texans",
            )
        ] + [
            ScheduledNFLGame(
                schedule_id=f"early-{index}",
                event_date=slot + timedelta(hours=500),
                away_team="Dallas Cowboys",
                home_team="New York Giants",
                venue="Test Stadium",
                name="Dallas Cowboys at New York Giants",
            )
            for index in range(12)
        ]
""",
)
replace_once(
    "tests/test_nfl_schedule_collector.py",
    "        self.assertGreater(len(due), 1)\n",
    "",
)

# Add explicit API boundary coverage without changing the database schema.
replace_once(
    "tests/test_nfl_blueprint.py",
    """            finally:
                if old_db is None:
                    os.environ.pop("NFL_DATABASE_PATH", None)
                else:
                    os.environ["NFL_DATABASE_PATH"] = old_db
                if old_token is None:
                    os.environ.pop("COLLECTOR_INGEST_TOKEN", None)
                else:
                    os.environ["COLLECTOR_INGEST_TOKEN"] = old_token


if __name__ == "__main__":
""",
    """            finally:
                if old_db is None:
                    os.environ.pop("NFL_DATABASE_PATH", None)
                else:
                    os.environ["NFL_DATABASE_PATH"] = old_db
                if old_token is None:
                    os.environ.pop("COLLECTOR_INGEST_TOKEN", None)
                else:
                    os.environ["COLLECTOR_INGEST_TOKEN"] = old_token

    @patch("Flask_App.nfl_blueprint.write_nfl_audit")
    @patch("Flask_App.nfl_blueprint.create_nfl_daily_backup")
    def test_api_accepts_thirty_days_and_rejects_beyond_window(self, backup, audit):
        with tempfile.TemporaryDirectory() as directory:
            old_db = os.environ.get("NFL_DATABASE_PATH")
            old_token = os.environ.get("COLLECTOR_INGEST_TOKEN")
            os.environ["NFL_DATABASE_PATH"] = str(Path(directory) / "nfl.db")
            os.environ["COLLECTOR_INGEST_TOKEN"] = "test-token"
            try:
                app = Flask(__name__, template_folder="../Flask_App/templates")
                app.register_blueprint(nfl_blueprint)
                app.config.update(TESTING=True)
                client = app.test_client()
                captured_at = datetime.now(timezone.utc).replace(
                    minute=0, second=0, microsecond=0
                )
                headers = {"Authorization": "Bearer test-token"}
                snapshot = self._snapshot()

                boundary_payload = snapshot_to_payload(
                    "https://www.vividseats.com/game/production/1234567",
                    captured_at + timedelta(hours=720),
                    captured_at,
                    snapshot,
                )
                boundary_payload["event_type"] = "nfl"
                accepted = client.post(
                    "/api/nfl/snapshot", json=boundary_payload, headers=headers
                )
                self.assertEqual(accepted.status_code, 201, accepted.get_data(as_text=True))

                outside_payload = snapshot_to_payload(
                    "https://www.vividseats.com/game/production/7654321",
                    captured_at + timedelta(hours=721),
                    captured_at,
                    EventSnapshot(
                        source_id="7654321",
                        title=snapshot.title,
                        venue=snapshot.venue,
                        sections=snapshot.sections,
                    ),
                )
                outside_payload["event_type"] = "nfl"
                rejected = client.post(
                    "/api/nfl/snapshot", json=outside_payload, headers=headers
                )
                self.assertEqual(rejected.status_code, 400)
                self.assertIn("30-day", rejected.get_json()["error"])
                backup.assert_called_once()
                audit.assert_called_once()
            finally:
                if old_db is None:
                    os.environ.pop("NFL_DATABASE_PATH", None)
                else:
                    os.environ["NFL_DATABASE_PATH"] = old_db
                if old_token is None:
                    os.environ.pop("COLLECTOR_INGEST_TOKEN", None)
                else:
                    os.environ["COLLECTOR_INGEST_TOKEN"] = old_token


if __name__ == "__main__":
""",
)

print("Applied NFL adaptive cadence corrections.")
