"""VGI worker exposing NASCAR schedules, results, standings and lap data as a SQL catalog."""

from __future__ import annotations

import json
import os
import sys

from vgi import Worker
from vgi.catalog import Catalog, ConstraintInfo, Schema, Table, constraint_record

from vgi_nascar.functions import (
    FUNCTIONS,
    CurrentScheduleFunction,
    CurrentStandingsFunction,
    LastRaceResultsFunction,
    LiveRaceFunction,
)
from vgi_nascar.meta import column_comments
from vgi_nascar.schemas import LIVE_SCHEMA, RESULTS_SCHEMA, SCHEDULE_SCHEMA, STANDINGS_SCHEMA

SOURCE_URL = "https://github.com/Query-farm/vgi-nascar"

_STANDINGS_TAGS = {
    "provider": "nascar",
    "vgi.category": "standings",
    "vgi.title": "Current NASCAR Driver Standings",
    "vgi.subtitle": "Cup, O'Reilly Auto Parts and Truck Series points, one row per driver.",
    "vgi.keywords": json.dumps(["standings", "points", "championship", "playoffs", "wins", "top 10"]),
    "vgi.doc_llm": (
        "Current-season driver championship standings for all three national series, one row per "
        "driver per series (series_id 1 Cup, 2 O'Reilly Auto Parts/Xfinity, 3 Truck). Start here for "
        "points, position, wins, top 5s, top 10s, laps led, stage wins, DNFs and playoff status. "
        "Always filter series_id. For past seasons aggregate season_results() instead."
    ),
    "vgi.doc_md": (
        "NASCAR's current points feed for each national series as a read-only table. "
        "Historical points feeds are not exposed because NASCAR's season paths for them do not "
        "reliably match their contents; use `season_results()` for past-season aggregates."
    ),
    "vgi.example_queries": json.dumps(
        [
            {
                "description": "Current Cup Series top ten.",
                "sql": (
                    "SELECT position, driver_name, points, points_behind_leader, wins, top_5 "
                    "FROM nascar.main.current_standings WHERE series_id = 1 ORDER BY position LIMIT 10"
                ),
            },
            {
                "description": "Points leader of each series.",
                "sql": (
                    "SELECT series_name, driver_name, points FROM nascar.main.current_standings "
                    "WHERE position = 1 ORDER BY series_id"
                ),
            },
        ]
    ),
}

_SCHEDULE_TAGS = {
    "provider": "nascar",
    "vgi.category": "schedule",
    "vgi.title": "Current NASCAR Schedule",
    "vgi.subtitle": "This season's races for all three national series, with winners once run.",
    "vgi.keywords": json.dumps(["schedule", "calendar", "races", "tracks", "winners", "scores"]),
    "vgi.doc_llm": (
        "Current-season race calendar for Cup, O'Reilly Auto Parts (Xfinity) and Truck: one row per "
        "race with race_id, date (US Eastern), track, laps, stage lengths, pole winner, winner, "
        "lead changes and cautions. is_completed separates results from upcoming races. Use race_id "
        "with race_results() and the other per-race functions; schedule() covers 2015 onwards."
    ),
    "vgi.doc_md": "The current NASCAR season's schedule and race summaries as an ordinary table.",
    "vgi.example_queries": json.dumps(
        [
            {
                "description": "Cup Series winners so far this season.",
                "sql": (
                    "SELECT race_date, race_name, track_name, winner_name FROM nascar.main.current_schedule "
                    "WHERE series_id = 1 AND is_completed ORDER BY race_date"
                ),
            }
        ]
    ),
}

_LAST_RACE_TAGS = {
    "provider": "nascar",
    "vgi.category": "results",
    "vgi.title": "Latest NASCAR Race Results",
    "vgi.subtitle": "Official finishing order of each national series' most recent race.",
    "vgi.keywords": json.dumps(["results", "finishing order", "winner", "last race", "scores"]),
    "vgi.doc_llm": (
        "Official results of the most recently completed race of each series (one race per "
        "series_id), one row per car: finishing and starting position, driver, team, manufacturer, "
        "laps led, status and points earned. Use race_results(race_id) for any other race."
    ),
    "vgi.doc_md": "The latest Cup, O'Reilly Auto Parts and Truck race results as a table.",
    "vgi.example_queries": json.dumps(
        [
            {
                "description": "Top five of the latest Cup Series race.",
                "sql": (
                    "SELECT race_name, finishing_position, driver_name, team_name, laps_led "
                    "FROM nascar.main.last_race_results WHERE series_id = 1 AND finishing_position <= 5 "
                    "ORDER BY finishing_position"
                ),
            }
        ]
    ),
}

_LIVE_TAGS = {
    "provider": "nascar",
    "vgi.category": "live",
    "vgi.title": "Live NASCAR Session",
    "vgi.subtitle": "The practice, qualifying or race session on track right now.",
    "vgi.keywords": json.dumps(["live", "lap", "flag", "laps to go", "now"]),
    "vgi.doc_llm": (
        "Zero or one row describing the session currently on track: series, run name and type, "
        "track, current lap, laps to go, flag, cautions and lead changes so far. Empty when nothing "
        "is running; refreshed at most every 15 seconds."
    ),
    "vgi.doc_md": "NASCAR's live timing-and-scoring session summary as a table.",
    "vgi.example_queries": json.dumps(
        [
            {
                "description": "Whether a session is on track, with its lap and flag.",
                "sql": (
                    "SELECT count(*) AS sessions_on_track, max(run_name) AS session, "
                    "max(lap_number) AS lap, max(flag) AS flag FROM nascar.main.live_race"
                ),
            },
        ]
    ),
}

_EXECUTABLE_EXAMPLES = json.dumps(
    [
        {
            "name": "current_schedule_is_populated",
            "description": "The current season's schedule covers all three national series.",
            "sql": "SELECT count(DISTINCT series_id) = 3 AS populated FROM nascar.main.current_schedule",
            "expected_result": [{"populated": True}],
        },
        {
            "name": "current_standings_have_drivers",
            "description": "Every current standings row has a driver and a non-negative points total.",
            "sql": (
                "SELECT count(*) > 0 AND bool_and(points >= 0) AND count(driver_id) = count(*) AS valid "
                "FROM nascar.main.current_standings"
            ),
            "expected_result": [{"valid": True}],
        },
        {
            "name": "championship_race_2026_winner",
            "description": "Ryan Blaney won the 2026 Cup Series Championship Race (race 5601).",
            "sql": (
                "SELECT driver_name FROM nascar.main.race_results(5601, season := 2026) "
                "WHERE finishing_position = 1"
            ),
            "expected_result": [{"driver_name": "Ryan Blaney"}],
        },
        {
            "name": "lead_stints_match_laps_led",
            "description": "Lead stints in race 5601 add up to the 267 laps run.",
            "sql": (
                "SELECT sum(laps_led)::INTEGER AS laps FROM nascar.main.race_leaders(5601, season := 2026)"
            ),
            "expected_result": [{"laps": 267}],
        },
    ]
)

AGENT_TASKS: list[dict[str, str]] = [
    {
        "name": "series_points_leaders",
        "prompt": "Who currently leads the points standings in each of NASCAR's three national series?",
    },
    {
        "name": "latest_cup_winner",
        "prompt": "Who won the latest NASCAR Cup race, and how many Cup races are on this year's schedule?",
    },
    {
        "name": "live_session",
        "prompt": "Is any NASCAR session on track right now? If so, what lap and flag is it on?",
    },
    {
        "name": "championship_race_top_five",
        "prompt": "Show the top five of the 2026 Cup Series Championship Race (race 5601) with birth dates.",
    },
    {
        "name": "championship_race_laps_led",
        "prompt": "Using the lead stints, who led the most laps in 2026 NASCAR race 5601, and how many?",
    },
    {
        "name": "season_2025_cup_wins",
        "prompt": "List each driver's number of 2025 NASCAR Cup Series points-race wins, most first.",
    },
    {
        "name": "fastest_pit_stop",
        "prompt": "Who had the fastest four-tire stop (time stationary in the box) in 2026 race 5601?",
    },
    {
        "name": "cautions_and_commentary",
        "prompt": "For each caution in 2026 race 5601, give its reason and how many lap notes it has.",
    },
    {
        "name": "fastest_lap_and_rating",
        "prompt": "In 2026 NASCAR race 5601, who ran the fastest lap and who earned the top driver rating?",
    },
    {
        "name": "pole_and_stage_winners",
        "prompt": "Who won the pole and who won each stage of the 2026 Cup Series Championship Race (5601)?",
    },
]


def _key_constraints(table: str, key: tuple[str, ...]) -> tuple[ConstraintInfo, ...]:
    """Primary-key and NOT NULL records for a read-only table keyed by ``key``."""
    records = [
        constraint_record(
            ["main"],
            table,
            "pk",
            "PRIMARY_KEY",
            [[column] for column in key],
            enforcement="ENFORCED",
            validation="ALL_ROWS",
        )
    ]
    records += [
        constraint_record(
            ["main"],
            table,
            f"{column}_not_null",
            "NOT_NULL",
            [[column]],
            enforcement="ENFORCED",
            validation="ALL_ROWS",
        )
        for column in key
    ]
    return tuple(records)


NASCAR_CATALOG = Catalog(
    name="nascar",
    default_schema="main",
    source_url=SOURCE_URL,
    comment="Read-only NASCAR schedules, results, standings, lap times and pit stops from NASCAR's feeds",
    tags={
        "provider": "nascar",
        "domain": "sports",
        "vgi.title": "NASCAR Racing Data",
        "vgi.subtitle": "Cup, O'Reilly Auto Parts and Truck Series schedules, results, standings and laps.",
        "vgi.source_url": SOURCE_URL,
        "vgi.author": "Query Farm LLC <hello@query.farm>",
        "vgi.copyright": (
            "Worker (c) 2026 Query Farm LLC. NASCAR data (c) National Association for Stock Car Auto "
            "Racing, LLC; use is subject to NASCAR's terms. Not affiliated with or endorsed by NASCAR."
        ),
        "vgi.license": "MIT",
        "vgi.support_contact": "https://github.com/Query-farm/vgi-nascar/issues",
        "vgi.support_policy_url": "https://github.com/Query-farm/vgi-nascar/blob/main/README.md",
        "vgi.keywords": json.dumps(
            ["NASCAR", "motorsport", "stock car racing", "Cup Series", "Xfinity", "Truck Series"]
        ),
        "vgi.executable_examples": _EXECUTABLE_EXAMPLES,
        "vgi.agent_test_tasks": json.dumps(AGENT_TASKS),
        "vgi.doc_llm": (
            "Read-only NASCAR data for the Cup, O'Reilly Auto Parts (formerly Xfinity) and Craftsman "
            "Truck Series from NASCAR's own cf.nascar.com feeds. Tables: current_standings (driver "
            "points), current_schedule (this season's races and winners), last_race_results and "
            "live_race. Functions: schedule(season, series) from 2015, season_results(season, series) "
            "for season aggregates, and per-race functions keyed by race_id (race_results, "
            "session_results, stage_results, race_cautions, race_leaders, lap_times, pit_stops, "
            "lap_notes, loop_stats) plus drivers(). series_id is 1 Cup, 2 O'Reilly, 3 Truck."
        ),
        "vgi.doc_md": (
            "# NASCAR schedules, results and lap data\n\n"
            "Query NASCAR's three national series directly from SQL: schedules and race summaries "
            "since 2015, official results, stage and session results, cautions, lead changes, "
            "lap-by-lap times (2020 onwards), pit stops, commentary, loop-data driver ratings and "
            "current points standings.\n\n"
            "## Data model\n\n"
            "Races are identified by `race_id`; drivers by `driver_id`. Find a race in "
            "`current_schedule` or `schedule()`, then pass its `race_id` (and `season` to skip the "
            "lookup) to the per-race functions. Missing upstream files yield empty results, not "
            "errors.\n\n"
            "Data is fetched on demand and cached in-process; nothing is rehosted. Not affiliated "
            "with NASCAR."
        ),
    },
    schemas=[
        Schema(
            path=["main"],
            comment="NASCAR national-series schedules, results, standings and race detail",
            tags={
                "provider": "nascar",
                "domain": "sports",
                "vgi.title": "NASCAR National Series",
                "vgi.keywords": json.dumps(
                    ["NASCAR", "standings", "schedule", "results", "lap times", "pit stops", "loop data"]
                ),
                "vgi.categories": json.dumps(
                    [
                        {
                            "name": "standings",
                            "title": "Standings",
                            "description": "Driver championship points and season statistics.",
                            "keywords": ["standings", "points", "championship", "playoffs"],
                        },
                        {
                            "name": "schedule",
                            "title": "Schedule",
                            "description": "Season race calendars with race summaries and winners.",
                            "keywords": ["schedule", "calendar", "races", "tracks"],
                        },
                        {
                            "name": "results",
                            "title": "Results",
                            "description": "Official race, season, practice and qualifying results.",
                            "keywords": ["results", "finishing order", "qualifying", "practice"],
                        },
                        {
                            "name": "race-detail",
                            "title": "Race Detail",
                            "description": "Stages, cautions, leaders, lap times, pit stops and loop data.",
                            "keywords": ["laps", "cautions", "pit stops", "driver rating", "stages"],
                        },
                        {
                            "name": "drivers",
                            "title": "Drivers",
                            "description": "Driver profiles and identifiers.",
                            "keywords": ["drivers", "profiles", "hometown"],
                        },
                        {
                            "name": "live",
                            "title": "Live",
                            "description": "The session currently on track.",
                            "keywords": ["live", "now", "flag"],
                        },
                    ]
                ),
                "vgi.doc_llm": (
                    "Use current_standings for points, current_schedule for this season's races, "
                    "last_race_results for the latest finishing orders and live_race for what is on "
                    "track. schedule() and season_results() cover past seasons; per-race functions take "
                    "a race_id and optional season."
                ),
                "vgi.doc_md": (
                    "## NASCAR analytical data\n\n"
                    "Rows are race-, car-in-race-, stage-, caution-, lap- or pit-stop-grain, keyed by "
                    "`race_id` and `driver_id`. Times and speeds are exact decimals.\n\n"
                    "Start with the current tables. For history, find a `race_id` with `schedule()` and "
                    "pass it to the per-race functions; aggregate `season_results()` for season, team "
                    "and manufacturer statistics."
                ),
                "vgi.example_queries": _STANDINGS_TAGS["vgi.example_queries"],
            },
            functions=FUNCTIONS,
            tables=[
                Table(
                    name="current_standings",
                    function=CurrentStandingsFunction,
                    comment="Current-season driver points standings, one row per driver per series",
                    tags=_STANDINGS_TAGS,
                    column_comments=column_comments(STANDINGS_SCHEMA),
                    constraints=_key_constraints("current_standings", ("series_id", "driver_id")),
                    cardinality_estimate=200,
                ),
                Table(
                    name="current_schedule",
                    function=CurrentScheduleFunction,
                    comment="Current-season race schedule and summaries for all three national series",
                    tags=_SCHEDULE_TAGS,
                    column_comments=column_comments(SCHEDULE_SCHEMA),
                    constraints=_key_constraints("current_schedule", ("race_id",)),
                    cardinality_estimate=120,
                ),
                Table(
                    name="last_race_results",
                    function=LastRaceResultsFunction,
                    comment="Official results of each series' most recently completed race",
                    tags=_LAST_RACE_TAGS,
                    column_comments=column_comments(RESULTS_SCHEMA),
                    constraints=_key_constraints("last_race_results", ("race_id", "driver_id")),
                    cardinality_estimate=115,
                ),
                Table(
                    name="live_race",
                    function=LiveRaceFunction,
                    comment="The NASCAR session currently on track; empty when nothing is running",
                    tags=_LIVE_TAGS,
                    column_comments=column_comments(LIVE_SCHEMA),
                    constraints=_key_constraints("live_race", ("race_id",)),
                    cardinality_estimate=1,
                ),
            ],
        )
    ],
)


class NascarWorker(Worker):
    """Worker process hosting the read-only NASCAR catalog."""

    catalog = NASCAR_CATALOG


def main() -> None:
    """Run over stdio by default, or HTTP when VGI's ``--http`` flag is supplied."""
    NascarWorker.main()


def main_http() -> None:
    """Run HTTP with production-safe configuration requirements."""
    if not os.environ.get("VGI_SIGNING_KEY"):
        raise SystemExit(
            "VGI_SIGNING_KEY is required for HTTP mode so state remains valid across restarts and replicas"
        )
    auth_configured = bool(os.environ.get("VGI_BEARER_TOKENS") or os.environ.get("VGI_JWT_ISSUER"))
    auth_configured = auth_configured or os.environ.get("VGI_PROXY_PROOF_MODE", "").lower() == "require"
    if not auth_configured and os.environ.get("VGI_ALLOW_ANONYMOUS") != "1":
        raise SystemExit(
            "HTTP authentication is required; configure bearer/JWT/proxy proof, or explicitly set "
            "VGI_ALLOW_ANONYMOUS=1 for an intentionally public deployment"
        )
    argv = sys.argv[1:]
    if "--http" not in argv:
        argv = ["--http", *argv]
    if "--describe" not in argv and "--no-describe" not in argv:
        argv = ["--no-describe", *argv]
    if not any(value == "--cors-origins" or value.startswith("--cors-origins=") for value in argv):
        argv = ["--cors-origins", "", *argv]
    sys.argv = [sys.argv[0], *argv]
    NascarWorker.main()
