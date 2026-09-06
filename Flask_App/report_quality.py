"""Read-only sampling audit. Uses existing compact summaries, never raw tickets."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from Flask_App.materialized_analytics import SUMMARY_SCHEMA_VERSION
from Flask_App.nfl_stadium_blueprint import (
    build_mlb_stadium_context, build_nfl_stadium_context, build_nhl_arena_context,
)


def quality_report(sport: str, team: str = "", venue: str = "") -> dict[str, Any]:
    builders = {
        "mlb": build_mlb_stadium_context,
        "nfl": build_nfl_stadium_context,
        "nhl": build_nhl_arena_context,
    }
    if sport not in builders:
        raise ValueError("sport must be one of: mlb, nfl, nhl")
    context = builders[sport](venue, team)
    result = {
        "status": "ok",
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "sport": sport,
        "policy_version": 1,
        "summary_schema_version": SUMMARY_SCHEMA_VERSION,
        "error": context.get("error"),
    }
    if not team and not venue:
        result["teams"] = context["stadiums"]
        return result
    sections = context.get("all_sections", [])
    keys = (
        "name", "section_key", "game_count", "observation_count",
        "ranking_price", "ranking_price_games", "ranking_drop_percent",
        "ranking_drop_games", "ranking_total_games", "ranking_required_games",
        "ranking_price_eligible", "ranking_drop_eligible", "ranking_ambiguous_label",
    )
    result.update({
        "team": context.get("selected_team"),
        "venue": context.get("selected_venue"),
        "season": context.get("report_season"),
        "games": context.get("game_count", 0),
        "completed_games": context.get("completed_game_count", 0),
        "sections": [{key: row.get(key) for key in keys} for row in sections],
        "section_count": len(sections),
        "one_game_sections": sum(row["game_count"] == 1 for row in sections),
        "price_eligible_sections": sum(row["ranking_price_eligible"] for row in sections),
        "drop_eligible_sections": sum(row["ranking_drop_eligible"] for row in sections),
        "ambiguous_bare_labels": sum(row["ranking_ambiguous_label"] for row in sections),
        "price_game_coverage": dict(Counter(row["ranking_price_games"] for row in sections)),
        "drop_game_coverage": dict(Counter(row["ranking_drop_games"] for row in sections)),
        "cheapest": [row["name"] for row in context.get("cheapest_sections", [])],
        "drops": [row["name"] for row in context.get("biggest_drops", [])],
    })
    return result
