from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Expected one match in {path}, found {count}: {old[:100]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def replace_all(path: str, old: str, new: str, expected: int | None = None) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if expected is not None and count != expected:
        raise RuntimeError(f"Expected {expected} matches in {path}, found {count}: {old!r}")
    if count == 0:
        raise RuntimeError(f"No matches in {path}: {old!r}")
    target.write_text(text.replace(old, new), encoding="utf-8")


# ---------------------------------------------------------------------------
# Core NFL collector: 30-day window, deterministic per-game staggering, and
# adaptive 6h / 3h / 1h cadence.
# ---------------------------------------------------------------------------
replace_once(
    "nfl_collector.py",
    """NFL games are discovered from Vivid's league feed, tracked during the final
seven days before kickoff, sampled once per UTC hour, and uploaded to the
NFL-only API and database.
""",
    """NFL games are discovered from Vivid's league feed and tracked during the
final 30 days before kickoff. Each game is sampled every six hours from 30 to
14 days out, every three hours from 14 to 7 days out, and once per hour during
the final week before being uploaded to the NFL-only API and database.
""",
)
replace_once(
    "nfl_collector.py",
    "import html\nimport json\n",
    "import hashlib\nimport html\nimport json\n",
)
replace_once(
    "nfl_collector.py",
    """NFL_CAPTURE_WINDOW_HOURS = 7 * 24
DISCOVERY_HORIZON_DAYS = 7
SMOKE_HORIZON_DAYS = 45
""",
    """NFL_CAPTURE_WINDOW_HOURS = 30 * 24
NFL_THREE_HOUR_WINDOW_HOURS = 14 * 24
NFL_HOURLY_WINDOW_HOURS = 7 * 24
NFL_EARLY_CADENCE_HOURS = 6
NFL_MIDDLE_CADENCE_HOURS = 3
NFL_FINAL_CADENCE_HOURS = 1
DISCOVERY_HORIZON_DAYS = 30
SMOKE_HORIZON_DAYS = 45
""",
)
replace_once(
    "nfl_collector.py",
    """def hourly_capture_slot(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def nfl_is_within_capture_window(event_date: datetime, now: datetime) -> bool:
    hours_until = (as_utc(event_date) - now.astimezone(timezone.utc)).total_seconds() / 3600
    return 0 < hours_until <= NFL_CAPTURE_WINDOW_HOURS
""",
    """def hourly_capture_slot(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def nfl_capture_interval_hours(event_date: datetime, now: datetime) -> int | None:
    \"\"\"Return the collection interval for a game at the supplied moment.\"\"\"
    hours_until = (
        as_utc(event_date) - now.astimezone(timezone.utc)
    ).total_seconds() / 3600
    if not 0 < hours_until <= NFL_CAPTURE_WINDOW_HOURS:
        return None
    if hours_until <= NFL_HOURLY_WINDOW_HOURS:
        return NFL_FINAL_CADENCE_HOURS
    if hours_until <= NFL_THREE_HOUR_WINDOW_HOURS:
        return NFL_MIDDLE_CADENCE_HOURS
    return NFL_EARLY_CADENCE_HOURS


def nfl_capture_tier(event_date: datetime, now: datetime) -> str | None:
    interval = nfl_capture_interval_hours(event_date, now)
    return {
        NFL_FINAL_CADENCE_HOURS: \"final_7_days_hourly\",
        NFL_MIDDLE_CADENCE_HOURS: \"days_8_to_14_every_3_hours\",
        NFL_EARLY_CADENCE_HOURS: \"days_15_to_30_every_6_hours\",
    }.get(interval)


def nfl_capture_phase(cadence_key: str, interval_hours: int) -> int:
    \"\"\"Assign a stable hourly phase so longer-window games are staggered.\"\"\"
    if interval_hours <= 1:
        return 0
    digest = hashlib.sha256(str(cadence_key).encode(\"utf-8\")).digest()
    return int.from_bytes(digest[:4], \"big\") % interval_hours


def nfl_capture_is_due(
    event_date: datetime,
    capture_slot: datetime,
    cadence_key: str = \"\",
) -> bool:
    interval = nfl_capture_interval_hours(event_date, capture_slot)
    if interval is None:
        return False
    if interval == NFL_FINAL_CADENCE_HOURS:
        return True
    phase_key = cadence_key or as_utc(event_date).isoformat()
    phase = nfl_capture_phase(phase_key, interval)
    utc_hour = int(hourly_capture_slot(capture_slot).timestamp() // 3600)
    return utc_hour % interval == phase


def approximate_nfl_event_time(game: DiscoveredNFLGame) -> datetime | None:
    \"\"\"Use a stable afternoon kickoff only for degraded feed-only pacing.\"\"\"
    if game.date_hint is None:
        return None
    return datetime(
        game.date_hint.year,
        game.date_hint.month,
        game.date_hint.day,
        13,
        tzinfo=NEW_YORK,
    )


def adaptive_due_nfl_games(
    games: list[DiscoveredNFLGame],
    now: datetime,
    limit: int | None = None,
) -> list[DiscoveredNFLGame]:
    \"\"\"Filter feed-only fallback games using the same staggered cadence.\"\"\"
    capture_slot = hourly_capture_slot(now)
    eligible = upcoming_nfl_games(
        games,
        now,
        horizon_days=DISCOVERY_HORIZON_DAYS,
    )
    due = []
    for game in eligible:
        approximate_date = approximate_nfl_event_time(game)
        if approximate_date is None:
            continue
        if nfl_capture_is_due(approximate_date, capture_slot, game.url):
            due.append(game)
    return due if limit is None else due[:limit]


def nfl_is_within_capture_window(event_date: datetime, now: datetime) -> bool:
    hours_until = (as_utc(event_date) - now.astimezone(timezone.utc)).total_seconds() / 3600
    return 0 < hours_until <= NFL_CAPTURE_WINDOW_HOURS
""",
)
replace_once(
    "nfl_collector.py",
    """    discovered, discovery_errors = discover_nfl_games(headless, timeout)
    due_games = upcoming_nfl_games(discovered, started_at)
    undated = sum(game.date_hint is None for game in discovered)
    print(
        f\"{len(due_games)} NFL games fall within the seven-day date window \"
        f\"({undated} discovered links had no date hint).\",
        flush=True,
    )

    capture_slot = hourly_capture_slot(started_at)
""",
    """    discovered, discovery_errors = discover_nfl_games(headless, timeout)
    capture_slot = hourly_capture_slot(started_at)
    due_games = adaptive_due_nfl_games(discovered, capture_slot)
    in_window = upcoming_nfl_games(
        discovered,
        started_at,
        horizon_days=DISCOVERY_HORIZON_DAYS,
    )
    undated = sum(game.date_hint is None for game in discovered)
    print(
        f\"{len(due_games)} of {len(in_window)} NFL games inside the 30-day \"
        f\"window are due in this staggered cadence slot \"
        f\"({undated} discovered links had no date hint).\",
        flush=True,
    )

""",
)
replace_once(
    "nfl_collector.py",
    """            if not nfl_is_within_capture_window(event_date, datetime.now(timezone.utc)):
                skipped += 1
                print(
                    f\"SKIP: NFL game is outside the exact seven-day window: {game.url}\",
                    flush=True,
                )
                continue

            snapshot = NFLSnapshotParser.parse(raw_payload)
""",
    """            if not nfl_is_within_capture_window(event_date, datetime.now(timezone.utc)):
                skipped += 1
                print(
                    f\"SKIP: NFL game is outside the exact 30-day window: {game.url}\",
                    flush=True,
                )
                continue
            if not nfl_capture_is_due(event_date, capture_slot, game.url):
                skipped += 1
                print(
                    f\"SKIP: NFL game is not due in this adaptive cadence slot: {game.url}\",
                    flush=True,
                )
                continue

            snapshot = NFLSnapshotParser.parse(raw_payload)
""",
)
replace_once(
    "nfl_collector.py",
    '        "cadence": "hourly",\n',
    '        "cadence": "adaptive: 6h from days 15-30, 3h from days 8-14, hourly in final 7 days",\n',
)
replace_once(
    "nfl_collector.py",
    '        "due": len(due_games),\n',
    '        "in_window": len(in_window),\n        "due": len(due_games),\n        "skipped_by_cadence": len(in_window) - len(due_games),\n',
)


# ---------------------------------------------------------------------------
# Schedule-backed collector: resolve only games due during the current hour.
# ---------------------------------------------------------------------------
replace_once(
    "nfl_schedule_collector.py",
    """    hourly_capture_slot,
    nfl_is_within_capture_window,
    nfl_snapshot_to_payload,
""",
    """    hourly_capture_slot,
    nfl_capture_interval_hours,
    nfl_capture_is_due,
    nfl_capture_tier,
    nfl_is_within_capture_window,
    nfl_snapshot_to_payload,
""",
)
replace_once(
    "nfl_schedule_collector.py",
    """def matchup_key_from_title(title: str) -> tuple[str, str] | None:
""",
    """def schedule_games_due(
    schedule: list[ScheduledNFLGame],
    capture_slot: datetime,
) -> list[ScheduledNFLGame]:
    return [
        game
        for game in schedule
        if nfl_capture_is_due(game.event_date, capture_slot, game.schedule_id)
    ]


def schedule_cadence_summary(
    schedule: list[ScheduledNFLGame],
    capture_slot: datetime,
) -> dict[str, dict[str, int]]:
    in_window = {\"1h\": 0, \"3h\": 0, \"6h\": 0}
    due = {\"1h\": 0, \"3h\": 0, \"6h\": 0}
    for game in schedule:
        interval = nfl_capture_interval_hours(game.event_date, capture_slot)
        if interval is None:
            continue
        label = f\"{interval}h\"
        in_window[label] += 1
        if nfl_capture_is_due(game.event_date, capture_slot, game.schedule_id):
            due[label] += 1
    return {\"in_window\": in_window, \"due_now\": due}


def matchup_key_from_title(title: str) -> tuple[str, str] | None:
""",
)
replace_once(
    "nfl_schedule_collector.py",
    """    try:
        schedule, schedule_source = fetch_schedule_games(started_at)
    except Exception as exc:
""",
    """    capture_slot = hourly_capture_slot(started_at)
    try:
        schedule, schedule_source = fetch_schedule_games(started_at)
    except Exception as exc:
""",
)
replace_once(
    "nfl_schedule_collector.py",
    """    feed_rows, feed_errors = discover_nfl_games(headless, timeout)
    resolutions, search_errors = resolve_schedule_games(
        schedule,
        feed_rows,
        headless=headless,
        timeout=timeout,
    )

    capture_slot = hourly_capture_slot(started_at)
""",
    """    due_schedule = schedule_games_due(schedule, capture_slot)
    cadence_summary = schedule_cadence_summary(schedule, capture_slot)
    if due_schedule:
        feed_rows, feed_errors = discover_nfl_games(headless, timeout)
        resolutions, search_errors = resolve_schedule_games(
            due_schedule,
            feed_rows,
            headless=headless,
            timeout=timeout,
        )
    else:
        feed_rows, feed_errors = [], []
        resolutions, search_errors = [], []
        print(
            f\"No NFL games are due in this adaptive cadence slot; \"
            f\"{len(schedule)} remain inside the 30-day window.\",
            flush=True,
        )

""",
)
replace_once(
    "nfl_schedule_collector.py",
    '                raise ValueError("Resolved Vivid event is outside the exact 168-hour window.")\n',
    '                raise ValueError("Resolved Vivid event is outside the exact 720-hour window.")\n',
)
replace_once(
    "nfl_schedule_collector.py",
    """                    uploads.append(
                        {
                            \"schedule_id\": game.schedule_id,
                            \"url\": url,
""",
    """                    uploads.append(
                        {
                            \"schedule_id\": game.schedule_id,
                            \"cadence_hours\": nfl_capture_interval_hours(
                                game.event_date, capture_slot
                            ),
                            \"cadence_tier\": nfl_capture_tier(
                                game.event_date, capture_slot
                            ),
                            \"url\": url,
""",
)
replace_once(
    "nfl_schedule_collector.py",
    """    expected = len(schedule)
    coverage = 100.0 if expected == 0 else round(captured / expected * 100, 2)
""",
    """    expected = len(due_schedule)
    coverage = 100.0 if expected == 0 else round(captured / expected * 100, 2)
""",
)
replace_once(
    "nfl_schedule_collector.py",
    """        \"capture_window_hours\": NFL_CAPTURE_WINDOW_HOURS,
        \"scheduled_due\": expected,
""",
    """        \"capture_window_hours\": NFL_CAPTURE_WINDOW_HOURS,
        \"cadence_policy\": {
            \"days_15_to_30_hours\": 6,
            \"days_8_to_14_hours\": 3,
            \"final_7_days_hours\": 1,
            \"staggering\": \"deterministic per schedule ID\",
        },
        \"scheduled_in_window\": len(schedule),
        \"scheduled_due\": expected,
        \"skipped_by_cadence\": len(schedule) - expected,
        \"cadence_tiers\": cadence_summary,
""",
)
replace_once(
    "nfl_schedule_collector.py",
    """        \"NFL schedule-backed collection finished: \"
        f\"{captured}/{expected} captured ({coverage:.2f}% coverage), \"
""",
    """        \"NFL schedule-backed collection finished: \"
        f\"{captured}/{expected} due games captured ({coverage:.2f}% coverage); \"
        f\"{len(schedule)} games remain inside the 30-day window, \"
""",
)


# ---------------------------------------------------------------------------
# API/database window and public UI copy. No schema migration is required.
# ---------------------------------------------------------------------------
replace_once(
    "Flask_App/nfl_blueprint.py",
    """NFL history is intentionally isolated from both the existing baseball database
and the archived concert database. Each game receives at most one observation
per UTC hour during the final seven days before kickoff.
""",
    """NFL history is intentionally isolated from both the existing baseball database
and the archived concert database. Games are accepted during the final 30 days
before kickoff, with the collector choosing a 6-hour, 3-hour, or hourly cadence.
""",
)
replace_once(
    "Flask_App/nfl_blueprint.py",
    "NFL_CAPTURE_WINDOW_HOURS = 7 * 24\n",
    "NFL_CAPTURE_WINDOW_HOURS = 30 * 24\n",
)
replace_once(
    "Flask_App/nfl_blueprint.py",
    '            raise ValueError("The NFL game is outside the seven-day capture window.")\n',
    '            raise ValueError("The NFL game is outside the 30-day capture window.")\n',
)

replace_all(
    "Flask_App/templates/NFLHomeScreen.html",
    "final seven days",
    "final 30 days",
    expected=1,
)
replace_once(
    "Flask_App/templates/NFLHomeScreen.html",
    "<strong>168:00</strong>",
    "<strong>720:00</strong>",
)
replace_once(
    "Flask_App/templates/NFLHomeScreen.html",
    ">7 DAYS<",
    ">30 DAYS<",
)
replace_once(
    "Flask_App/templates/NFLHomeScreen.html",
    "<div><span>Cadence</span><strong>Every 60 minutes</strong></div>",
    "<div><span>Cadence</span><strong>6h → 3h → 1h</strong></div>",
)
replace_once(
    "Flask_App/templates/NFLHomeScreen.html",
    """    <div><span>01</span><strong>Seven-day window</strong><small>Collection begins 168 hours before kickoff.</small></div>
    <div><span>02</span><strong>Section-level view</strong><small>Follow the lowest observed listing in a selected section.</small></div>
    <div><span>03</span><strong>Independent history</strong><small>NFL observations stay separate from baseball data.</small></div>
""",
    """    <div><span>01</span><strong>Thirty-day window</strong><small>Collection begins up to 720 hours before kickoff.</small></div>
    <div><span>02</span><strong>Adaptive cadence</strong><small>Every 6 hours, then 3 hours, then hourly in the final week.</small></div>
    <div><span>03</span><strong>Section-level history</strong><small>NFL observations stay separate from baseball data.</small></div>
""",
)
replace_once(
    "Flask_App/templates/NFLHomeScreen.html",
    "The chart then shows how that section’s observed market price changed as kickoff approached.",
    "The chart then shows how that section’s observed market price changed across the month leading into kickoff.",
)
replace_once(
    "Flask_App/templates/NFLHomeScreen.html",
    "<h3>A cleaner read on the final week.</h3>",
    "<h3>A longer read on the road to kickoff.</h3>",
)
replace_once(
    "Flask_App/templates/NFLHomeScreen.html",
    """          <div><span>168h</span><p><strong>Full final-week window</strong><small>From seven days out to kickoff.</small></p></div>
          <div><span>1h</span><p><strong>Consistent snapshots</strong><small>One market observation each hour.</small></p></div>
""",
    """          <div><span>720h</span><p><strong>Full 30-day window</strong><small>Early movement through the final whistle countdown.</small></p></div>
          <div><span>6→3→1h</span><p><strong>Adaptive snapshots</strong><small>More frequent observations as kickoff gets closer.</small></p></div>
""",
)

replace_once(
    "Flask_App/templates/nfl_graph.html",
    """        <strong>7D</strong>
        <small>Hourly snapshots</small>
""",
    """        <strong>30D</strong>
        <small>Adaptive snapshots</small>
""",
)
replace_once(
    "Flask_App/templates/nfl_graph.html",
    "Hover over the line to inspect each hourly market observation.",
    "Hover over the line to inspect each captured market observation.",
)
replace_once(
    "Flask_App/templates/nfl_graph.html",
    "Each point is the lowest observed listing price for this section during that hourly snapshot.",
    "Each point is the lowest observed listing price for this section at that scheduled capture time.",
)
replace_once(
    "Flask_App/templates/nfl_graph.html",
    """          <div><dt>168h</dt><dd>Maximum lookback</dd></div>
          <div><dt>1h</dt><dd>Snapshot interval</dd></div>
""",
    """          <div><dt>720h</dt><dd>Maximum lookback</dd></div>
          <div><dt>6/3/1h</dt><dd>Adaptive interval</dd></div>
""",
)
replace_once(
    "Flask_App/templates/nfl_graph.html",
    "<div><i class=\"nfl-chart-guide__dot\"></i><span><strong>Hourly point</strong><small>A captured market observation.</small></span></div>",
    "<div><i class=\"nfl-chart-guide__dot\"></i><span><strong>Captured point</strong><small>A scheduled market observation.</small></span></div>",
)


# ---------------------------------------------------------------------------
# Workflows, tests, and documentation.
# ---------------------------------------------------------------------------
replace_once(
    ".github/workflows/collect-ticket-prices.yml",
    "    timeout-minutes: 45\n    steps:\n      - name: Select the hourly NFL slot",
    "    timeout-minutes: 60\n    steps:\n      - name: Select the hourly NFL slot",
)
replace_once(
    ".github/workflows/collect-ticket-prices.yml",
    "      - name: Collect the complete scheduled NFL slate inside seven days\n",
    "      - name: Collect due NFL games across the adaptive 30-day window\n",
)
replace_once(
    ".github/workflows/nfl-smoke-test.yml",
    "      - name: Validate the complete seven-day NFL schedule source\n",
    "      - name: Validate the complete 30-day NFL schedule source\n",
)
replace_once(
    ".github/workflows/nfl-smoke-test.yml",
    "            --horizon-hours 168 \\\n",
    "            --horizon-hours 720 \\\n",
)

replace_once(
    "tests/test_nfl_collector.py",
    """    hourly_capture_slot,
    is_nfl_game_title,
    nfl_is_within_capture_window,
""",
    """    adaptive_due_nfl_games,
    hourly_capture_slot,
    is_nfl_game_title,
    nfl_capture_interval_hours,
    nfl_capture_is_due,
    nfl_capture_phase,
    nfl_is_within_capture_window,
""",
)
replace_once(
    "tests/test_nfl_collector.py",
    """class NFLCadenceTests(unittest.TestCase):
    def test_window_is_exactly_seven_days(self):
        now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        self.assertTrue(nfl_is_within_capture_window(now + timedelta(hours=168), now))
        self.assertFalse(
            nfl_is_within_capture_window(
                now + timedelta(hours=168, seconds=1),
                now,
            )
        )
        self.assertFalse(nfl_is_within_capture_window(now, now))

    def test_every_run_in_an_hour_maps_to_one_capture_slot(self):
""",
    """class NFLCadenceTests(unittest.TestCase):
    def test_window_is_exactly_thirty_days(self):
        now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        self.assertTrue(nfl_is_within_capture_window(now + timedelta(hours=720), now))
        self.assertFalse(
            nfl_is_within_capture_window(
                now + timedelta(hours=720, seconds=1),
                now,
            )
        )
        self.assertFalse(nfl_is_within_capture_window(now, now))

    def test_interval_tiers_use_six_three_and_one_hours(self):
        now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        self.assertEqual(nfl_capture_interval_hours(now + timedelta(hours=720), now), 6)
        self.assertEqual(nfl_capture_interval_hours(now + timedelta(hours=336), now), 3)
        self.assertEqual(nfl_capture_interval_hours(now + timedelta(hours=168), now), 1)
        self.assertIsNone(
            nfl_capture_interval_hours(now + timedelta(hours=720, seconds=1), now)
        )

    def test_longer_interval_games_are_staggered_by_game_key(self):
        midnight = datetime(2026, 9, 1, 0, tzinfo=timezone.utc)
        six_hour_game = midnight + timedelta(hours=500)
        phase = nfl_capture_phase("schedule-123", 6)
        due_slot = midnight + timedelta(hours=phase)
        next_slot = due_slot + timedelta(hours=1)
        self.assertTrue(
            nfl_capture_is_due(six_hour_game, due_slot, "schedule-123")
        )
        self.assertFalse(
            nfl_capture_is_due(six_hour_game, next_slot, "schedule-123")
        )

    def test_final_week_game_is_due_every_hour(self):
        game_date = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
        for hour in range(24):
            slot = datetime(2026, 9, 1, hour, tzinfo=timezone.utc)
            self.assertTrue(nfl_capture_is_due(game_date, slot, "any-game"))

    def test_feed_fallback_uses_adaptive_due_filter(self):
        now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        page = \"\"\"
        <a href=\"/a/production/1000001\">Dallas Cowboys at New York Giants Sep 5, 2026</a>
        <a href=\"/b/production/1000002\">Baltimore Ravens at Pittsburgh Steelers Sep 20, 2026</a>
        <a href=\"/c/production/1000003\">Buffalo Bills at Houston Texans Sep 27, 2026</a>
        \"\"\"
        rows = extract_nfl_game_rows(page, now=now)
        due = adaptive_due_nfl_games(rows, now)
        self.assertTrue(any("1000001" in game.url for game in due))
        self.assertLessEqual(len(due), len(rows))

    def test_every_run_in_an_hour_maps_to_one_capture_slot(self):
""",
)

replace_once(
    "tests/test_nfl_schedule_collector.py",
    """    parse_schedule_payload,
    schedule_url,
    validate_captured_match,
""",
    """    parse_schedule_payload,
    schedule_cadence_summary,
    schedule_games_due,
    schedule_url,
    validate_captured_match,
""",
)
replace_once(
    "tests/test_nfl_schedule_collector.py",
    """                self._event(
                    \"3\",
                    now + timedelta(hours=169),
                    \"Dallas Cowboys\",
                    \"New York Giants\",
                ),
""",
    """                self._event(
                    \"3\",
                    now + timedelta(hours=400),
                    \"Dallas Cowboys\",
                    \"New York Giants\",
                ),
                self._event(
                    \"6\",
                    now + timedelta(hours=719),
                    \"Baltimore Ravens\",
                    \"Pittsburgh Steelers\",
                ),
                self._event(
                    \"7\",
                    now + timedelta(hours=721),
                    \"Miami Dolphins\",
                    \"New York Jets\",
                ),
""",
)
replace_once(
    "tests/test_nfl_schedule_collector.py",
    '        self.assertEqual([game.schedule_id for game in games], ["1", "2"])\n',
    '        self.assertEqual([game.schedule_id for game in games], ["1", "2", "3", "6"])\n',
)
replace_once(
    "tests/test_nfl_schedule_collector.py",
    """    def test_exact_168_hour_boundary_is_included(self):
        now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        payload = {
            \"events\": [
                self._event(
                    \"boundary\",
                    now + timedelta(hours=168),
                    \"Baltimore Ravens\",
                    \"Pittsburgh Steelers\",
                )
            ]
        }
        games = parse_schedule_payload(payload, now)
        self.assertEqual(len(games), 1)

    def test_schedule_url_requests_a_date_range_and_large_limit(self):
        now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        url = schedule_url(now, 168)
        self.assertIn(\"dates=20260901-20260909\", url)
        self.assertIn(\"limit=1000\", url)
""",
    """    def test_exact_720_hour_boundary_is_included(self):
        now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        payload = {
            \"events\": [
                self._event(
                    \"boundary\",
                    now + timedelta(hours=720),
                    \"Baltimore Ravens\",
                    \"Pittsburgh Steelers\",
                )
            ]
        }
        games = parse_schedule_payload(payload, now)
        self.assertEqual(len(games), 1)

    def test_schedule_url_requests_a_month_range_and_large_limit(self):
        now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        url = schedule_url(now, 720)
        self.assertIn(\"dates=20260901-20261002\", url)
        self.assertIn(\"limit=1000\", url)

    def test_schedule_due_filter_staggers_longer_window_games(self):
        slot = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        games = [
            ScheduledNFLGame(
                schedule_id=\"final-week\",
                event_date=slot + timedelta(hours=100),
                away_team=\"Buffalo Bills\",
                home_team=\"Houston Texans\",
                venue=\"Test Stadium\",
                name=\"Buffalo Bills at Houston Texans\",
            ),
            ScheduledNFLGame(
                schedule_id=f\"early-{index}\",
                event_date=slot + timedelta(hours=500),
                away_team=\"Dallas Cowboys\",
                home_team=\"New York Giants\",
                venue=\"Test Stadium\",
                name=\"Dallas Cowboys at New York Giants\",
            )
            for index in range(12)
        ]
        due = schedule_games_due(games, slot)
        self.assertIn(\"final-week\", {game.schedule_id for game in due})
        self.assertGreater(len(due), 1)
        self.assertLess(len(due), len(games))
        summary = schedule_cadence_summary(games, slot)
        self.assertEqual(summary[\"in_window\"][\"1h\"], 1)
        self.assertEqual(summary[\"in_window\"][\"6h\"], 12)
        self.assertEqual(sum(summary[\"due_now\"].values()), len(due))
""",
)

replace_once(
    "README.md",
    "- NFL games: once per hour during the final 168 hours.\n",
    "- NFL games: every 6 hours from days 30–15, every 3 hours from days 14–8, and hourly during the final 7 days.\n",
)
replace_once(
    "README.md",
    "- `NFL-collection.db`: NFL games, hourly iterations, and section observations.\n",
    "- `NFL-collection.db`: NFL games, adaptive-cadence iterations, and section observations.\n",
)
replace_once(
    "README.md",
    "- `/api/nfl/snapshot`: authenticated NFL ingestion with a 168-hour window.\n",
    "- `/api/nfl/snapshot`: authenticated NFL ingestion with a 720-hour window.\n",
)
replace_once(
    "README.md",
    "- `nfl_schedule_collector.py`: complete-slate schedule seeding, Vivid resolution, coverage checks, and hourly collection.\n",
    "- `nfl_schedule_collector.py`: 30-day schedule seeding, Vivid resolution, staggered adaptive cadence, and coverage checks.\n",
)
replace_once(
    "README.md",
    """- NFL runs on the first dispatch of each UTC hour.
- The NFL collector first loads the complete scheduled slate inside the exact seven-day window.
""",
    """- NFL evaluates its schedule on the first dispatch of each UTC hour.
- The NFL collector first loads the complete scheduled slate inside the exact 30-day window.
- Each game receives a deterministic phase so 6-hour and 3-hour captures are spread across hourly runs instead of arriving in one large batch.
- Games 15–30 days out run every 6 hours, games 8–14 days out run every 3 hours, and games in the final 7 days run every hour.
""",
)
replace_once(
    "README.md",
    "- The actual kickoff parsed from each event page remains authoritative for enforcing the final 168-hour storage window.\n",
    "- The actual kickoff parsed from each event page remains authoritative for enforcing the final 720-hour storage window.\n",
)

print("Applied NFL 30-day adaptive cadence patch.")
