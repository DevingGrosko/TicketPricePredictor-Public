from pathlib import Path


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise SystemExit(f"Expected one {label}, found {count}")
    return source.replace(old, new, 1)


collector_path = Path("nfl_schedule_collector.py")
collector = collector_path.read_text(encoding="utf-8")
collector = replace_once(
    collector,
    '''"""Schedule-backed NFL ticket collection with explicit coverage accounting.

The existing Vivid league feed is useful, but a lazy-loaded marketplace page is
not a reliable statement of the complete NFL slate. This collector first loads
a structured NFL schedule for the requested time window, then resolves every
scheduled matchup to a Vivid event. Feed matches are used first; missing games
are recovered through Vivid search. Every due game is either captured or listed
as unresolved in the health report.
"""''',
    '''"""Schedule-backed U.S. NFL ticket collection with coverage accounting.

The collector loads the structured NFL schedule, removes games played outside
the United States, and then resolves each due domestic matchup to a Vivid event.
Feed matches are used first and missing domestic games are recovered through
Vivid search. Every in-scope game is either captured or reported as unresolved.
"""''',
    "collector module docstring",
)

helper_start = collector.find("_US_COUNTRY_MARKERS = frozenset(")
helper_end = collector.find("\n\ndef _parse_datetime", helper_start)
if helper_start < 0 or helper_end < 0:
    raise SystemExit("Could not locate the existing country/provider-gap helpers")
collector = (
    collector[:helper_start]
    + '''_US_COUNTRY_MARKERS = frozenset(
    {
        "us",
        "usa",
        "united states",
        "united states of america",
    }
)


def is_us_venue_country(value: Any) -> bool:
    """Keep unknown legacy locations, but exclude every explicit non-U.S. venue."""
    country = " ".join(str(value or "").split()).casefold().replace(".", "")
    return not country or country in _US_COUNTRY_MARKERS
'''
    + collector[helper_end:]
)

neutral_site_line = '        neutral_site = bool(competition.get("neutralSite") is True)\n'
collector = replace_once(
    collector,
    neutral_site_line,
    '        if not is_us_venue_country(country):\n'
    '            continue\n\n'
    + neutral_site_line,
    "schedule country-scope insertion point",
)

result_start_marker = '    pending_count = len(list(pending_dir.glob("*.json")))\n'
result_end_marker = '\n\n\ndef run_schedule_smoke('
result_start = collector.find(result_start_marker)
result_end = collector.find(result_end_marker, result_start)
if result_start < 0 or result_end < 0:
    raise SystemExit("Could not locate the NFL collection result block")
result_block = '''    pending_count = len(list(pending_dir.glob("*.json")))
    expected = len(due_schedule)
    coverage = 100.0 if expected == 0 else round(captured / expected * 100, 2)
    all_errors = feed_errors + search_errors + capture_errors + queue_errors
    if unresolved or capture_errors or feed_errors or search_errors:
        status = "degraded"
    elif pending_count:
        status = "queued"
    else:
        status = "healthy"

    report = {
        "status": status,
        "event_type": "nfl",
        "coverage_mode": "schedule-backed",
        "collection_scope": "us-venues-only",
        "schedule_status": "available",
        "schedule_source": schedule_source,
        "timezone": "America/New_York",
        "started_at": eastern_iso(started_at),
        "capture_slot": eastern_iso(capture_slot),
        "capture_window_hours": NFL_CAPTURE_WINDOW_HOURS,
        "cadence_policy": {
            "days_15_to_30_hours": 6,
            "days_8_to_14_hours": 3,
            "final_7_days_hours": 1,
            "staggering": "deterministic per schedule ID",
        },
        "scheduled_in_window": len(schedule),
        "scheduled_due": expected,
        "skipped_by_cadence": len(schedule) - expected,
        "cadence_tiers": cadence_summary,
        "feed_discovered": len(feed_rows),
        "matched_from_feed": matched_from_feed,
        "recovered_from_search": recovered_from_search,
        "unresolved_count": len(unresolved),
        "unresolved": unresolved,
        "coverage_percent": coverage,
        "captured": captured,
        "uploaded": uploaded,
        "queued": queued,
        "replayed": replayed,
        "pending": pending_count,
        "failed": len(capture_errors),
        "uploads": uploads,
        "errors": all_errors,
    }
    _write_health(health_output, report)
    print(
        "NFL U.S.-venue collection finished: "
        f"{captured}/{expected} due games captured ({coverage:.2f}% coverage); "
        f"{len(schedule)} domestic games remain inside the 30-day window, "
        f"{recovered_from_search} recovered through search, "
        f"{len(unresolved)} unresolved, {queued} queued, {replayed} replayed.",
        flush=True,
    )

    if expected > 0 and captured < expected:
        return 1
    if feed_errors or search_errors:
        return 1
    return 0
'''
collector = collector[:result_start] + result_block + collector[result_end:]
collector_path.write_text(collector, encoding="utf-8")


blueprint_path = Path("Flask_App/nfl_blueprint.py")
blueprint = blueprint_path.read_text(encoding="utf-8")
blueprint = replace_once(
    blueprint,
    '''def _clean_metadata_text(value: Any, maximum: int = 250) -> str:
    return " ".join(str(value or "").split())[:maximum]


def normalize_nfl_schedule_metadata(
''',
    '''def _clean_metadata_text(value: Any, maximum: int = 250) -> str:
    return " ".join(str(value or "").split())[:maximum]


_US_COUNTRY_MARKERS = frozenset(
    {"us", "usa", "united states", "united states of america"}
)


def _country_is_explicitly_non_us(value: Any) -> bool:
    country = _clean_metadata_text(value).casefold().replace(".", "")
    return bool(country) and country not in _US_COUNTRY_MARKERS


def normalize_nfl_schedule_metadata(
''',
    "web metadata helper insertion point",
)
blueprint = replace_once(
    blueprint,
    '''    schedule_id = _clean_metadata_text(raw.get("schedule_id"), maximum=160)
    canonical_venue = canonical_venue_name(
        raw.get("canonical_venue") or provider_venue
    )
    return {
        "schedule_id": schedule_id or None,
        "away_team": away_team,
        "home_team": home_team,
        "canonical_venue": canonical_venue,
        "city": _clean_metadata_text(raw.get("city")),
        "country": _clean_metadata_text(raw.get("country")),
''',
    '''    schedule_id = _clean_metadata_text(raw.get("schedule_id"), maximum=160)
    canonical_venue = canonical_venue_name(
        raw.get("canonical_venue") or provider_venue
    )
    city = _clean_metadata_text(raw.get("city"))
    country = _clean_metadata_text(raw.get("country"))
    if _country_is_explicitly_non_us(country):
        raise ValueError(
            "International NFL games are outside the U.S.-venue collection scope."
        )
    return {
        "schedule_id": schedule_id or None,
        "away_team": away_team,
        "home_team": home_team,
        "canonical_venue": canonical_venue,
        "city": city,
        "country": country,
''',
    "web metadata country validation block",
)

schema_start = blueprint.find("def _ensure_nfl_schema(")
schema_insert = blueprint.find("            rows = connection.execute(\n", schema_start)
if schema_start < 0 or schema_insert < 0:
    raise SystemExit("Could not locate the NFL schema cleanup insertion point")
cleanup_block = '''            international_predicate = (
                "country IS NOT NULL AND TRIM(country) <> '' AND "
                "LOWER(REPLACE(TRIM(country), '.', '')) NOT IN "
                "('us', 'usa', 'united states', 'united states of america')"
            )
            connection.exec_driver_sql(
                "DELETE FROM nfl_tickets WHERE iteration_id IN ("
                "SELECT id FROM nfl_iterations WHERE event_id IN ("
                f"SELECT id FROM nfl_event WHERE {international_predicate}))"
            )
            connection.exec_driver_sql(
                "DELETE FROM nfl_iterations WHERE event_id IN ("
                f"SELECT id FROM nfl_event WHERE {international_predicate})"
            )
            connection.exec_driver_sql(
                f"DELETE FROM nfl_event WHERE {international_predicate}"
            )

'''
blueprint = blueprint[:schema_insert] + cleanup_block + blueprint[schema_insert:]
blueprint = replace_once(
    blueprint,
    '''            all_games = (
                session.query(NFLEvent)
                .order_by(NFLEvent.event_date)
                .all()
            )
''',
    '''            all_games = [
                game
                for game in (
                    session.query(NFLEvent)
                    .order_by(NFLEvent.event_date)
                    .all()
                )
                if not _country_is_explicitly_non_us(game.country)
            ]
''',
    "NFL history query",
)
blueprint_path.write_text(blueprint, encoding="utf-8")


schedule_tests_path = Path("tests/test_nfl_schedule_collector.py")
schedule_tests = schedule_tests_path.read_text(encoding="utf-8")
schedule_tests = replace_once(
    schedule_tests,
    '''    EVENT_TIME_TOLERANCE_HOURS,
    _schedule_collection_exit_code,
    is_expected_vivid_provider_gap,
    ScheduledNFLGame,
''',
    '''    EVENT_TIME_TOLERANCE_HOURS,
    ScheduledNFLGame,
    is_us_venue_country,
''',
    "schedule test imports",
)
schedule_tests = replace_once(
    schedule_tests,
    '''        *,
        completed: bool = False,
    ):
''',
    '''        *,
        completed: bool = False,
        country: str = "USA",
    ):
''',
    "schedule event helper signature",
)
schedule_tests = replace_once(
    schedule_tests,
    '                    "venue": {"fullName": "Test Stadium"},\n',
    '''                    "venue": {
                        "fullName": "Test Stadium",
                        "address": {"country": country},
                    },
''',
    "schedule test venue",
)
international_fixture_marker = '''                self._event(
                    "7",
                    now + timedelta(hours=721),
                    "Miami Dolphins",
                    "New York Jets",
                ),
'''
schedule_tests = replace_once(
    schedule_tests,
    international_fixture_marker,
    international_fixture_marker
    + '''                self._event(
                    "8",
                    now + timedelta(hours=100),
                    "San Francisco 49ers",
                    "Los Angeles Rams",
                    country="Australia",
                ),
''',
    "schedule international fixture insertion point",
)
provider_test_start = schedule_tests.find(
    "    def test_non_us_games_are_expected_vivid_provider_gaps"
)
provider_test_end = schedule_tests.find(
    "\n\nclass NFLVividResolutionTests", provider_test_start
)
if provider_test_start < 0 or provider_test_end < 0:
    raise SystemExit("Could not locate the old provider-gap tests")
schedule_tests = (
    schedule_tests[:provider_test_start]
    + '''    def test_explicit_non_us_venues_are_out_of_scope(self):
        self.assertTrue(is_us_venue_country("USA"))
        self.assertTrue(is_us_venue_country("United States"))
        self.assertTrue(is_us_venue_country(""))
        self.assertFalse(is_us_venue_country("Australia"))
        self.assertFalse(is_us_venue_country("Brazil"))
'''
    + schedule_tests[provider_test_end:]
)
schedule_tests_path.write_text(schedule_tests, encoding="utf-8")


metadata_tests_path = Path("tests/test_nfl_metadata_enhancements.py")
metadata_tests = metadata_tests_path.read_text(encoding="utf-8")
metadata_tests = replace_once(
    metadata_tests,
    '''    CreateNFLModel,
    NFLEvent,
    format_nfl_capture_label,
''',
    '''    CreateNFLModel,
    NFLEvent,
    NFLIteration,
    NFLTicket,
    _SCHEMA_READY,
    format_nfl_capture_label,
''',
    "metadata test model imports",
)
metadata_tests = replace_once(
    metadata_tests,
    '''    nfl_map,
    store_nfl_snapshot,
''',
    '''    nfl_map,
    normalize_nfl_schedule_metadata,
    store_nfl_snapshot,
''',
    "metadata normalization import",
)
new_metadata_tests = '''    def test_international_schedule_metadata_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside the U.S.-venue"):
            normalize_nfl_schedule_metadata(
                {
                    "away_team": "San Francisco 49ers",
                    "home_team": "Los Angeles Rams",
                    "canonical_venue": "Melbourne Cricket Ground",
                    "city": "Melbourne",
                    "country": "Australia",
                    "neutral_site": True,
                },
                title="San Francisco 49ers at Los Angeles Rams",
                provider_venue="Melbourne Cricket Ground",
            )

    def test_existing_international_rows_are_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "nfl.db"
            model = CreateNFLModel(db_path)
            try:
                with model.getSession()() as session:
                    domestic = NFLEvent(
                        source_id="domestic",
                        title="Dallas Cowboys at New York Giants",
                        event_date=datetime(2026, 9, 10, 20),
                        sections=["Section 1"],
                        source_url="https://www.vividseats.com/game/production/9100001",
                        venue="MetLife Stadium",
                        country="USA",
                    )
                    international = NFLEvent(
                        source_id="international",
                        title="San Francisco 49ers at Los Angeles Rams",
                        event_date=datetime(2026, 9, 10, 20),
                        sections=["Section 1"],
                        source_url="https://www.vividseats.com/game/production/9100002",
                        venue="Melbourne Cricket Ground",
                        country="Australia",
                    )
                    iteration = NFLIteration(
                        event=international,
                        captured_at=datetime(2026, 9, 1, 12),
                    )
                    iteration.tickets.append(
                        NFLTicket(
                            section="Section 1",
                            price=100,
                            listing_count=1,
                        )
                    )
                    session.add_all([domestic, international, iteration])
                    session.commit()
            finally:
                model.engine.dispose()

            _SCHEMA_READY.discard(str(db_path.resolve()))
            model = CreateNFLModel(db_path)
            try:
                with model.getSession()() as session:
                    self.assertEqual(
                        [event.source_id for event in session.query(NFLEvent).all()],
                        ["domestic"],
                    )
                    self.assertEqual(session.query(NFLIteration).count(), 0)
                    self.assertEqual(session.query(NFLTicket).count(), 0)
            finally:
                model.engine.dispose()

'''
metadata_tests = replace_once(
    metadata_tests,
    "    def test_existing_database_is_migrated_and_backfilled(self):\n",
    new_metadata_tests
    + "    def test_existing_database_is_migrated_and_backfilled(self):\n",
    "metadata migration test insertion point",
)
metadata_tests_path.write_text(metadata_tests, encoding="utf-8")
