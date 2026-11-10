"""VGI table functions over NASCAR's schedule, results, standings, and lap data."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg
from vgi.cache_control import CacheControl
from vgi.metadata import FunctionExample
from vgi.table_function import (
    ProcessParams,
    TableCardinality,
    TableFunctionGenerator,
    bind_fixed_schema,
    init_single_worker,
)
from vgi_rpc.rpc import OutputCollector

from vgi_nascar import api
from vgi_nascar import schemas as s
from vgi_nascar.meta import result_columns_schema

SERIES_CHOICES = ("cup", "oreilly", "xfinity", "truck")
_SERIES_DOC = (
    "Series: cup (NASCAR Cup Series), oreilly or xfinity (O'Reilly Auto Parts Series, formerly "
    "Xfinity), or truck (Craftsman Truck Series)."
)


# --- shared plumbing --------------------------------------------------------------


def _cache_control(ttl: int) -> CacheControl:
    return CacheControl(ttl=ttl, stale_if_error=api.TTL.stale_if_error)


def _emit(rows: Sequence[Mapping[str, Any]], schema: pa.Schema, out: OutputCollector, ttl: int) -> None:
    out.emit(s.batch(rows, schema), cache_control=_cache_control(ttl))
    out.finish()


def _example_tags(examples: Sequence[tuple[str, str]]) -> str:
    return json.dumps([{"description": description, "sql": sql} for description, sql in examples])


def _function_examples(examples: Sequence[tuple[str, str]]) -> list[FunctionExample]:
    return [FunctionExample(sql=sql, description=description.rstrip(".")) for description, sql in examples]


def _tags(
    category: str, schema: pa.Schema, doc_llm: str, doc_md: str, examples: Sequence[tuple[str, str]]
) -> dict:
    return {
        "provider": "nascar",
        "vgi.category": category,
        "vgi.result_columns_schema": result_columns_schema(schema),
        "vgi.doc_llm": doc_llm,
        "vgi.doc_md": doc_md,
        "vgi.example_queries": _example_tags(examples),
    }


def _names() -> dict[int, str]:
    return s.driver_names(api.drivers().payload)


def _completed(race: Mapping[str, Any]) -> bool:
    return bool(s.to_positive_int(race.get("winner_driver_id")) or s.to_int(race.get("actual_laps")))


def latest_completed_races(races: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The most recently run race of each series."""
    latest: dict[int, Mapping[str, Any]] = {}
    for race in races:
        series = s.to_int(race.get("series_id"))
        if series is None or not _completed(race):
            continue
        stamp = s.to_local_timestamp(race.get("race_date")) or datetime.min
        best = latest.get(series)
        if best is None or stamp >= (s.to_local_timestamp(best.get("race_date")) or datetime.min):
            latest[series] = race
    return [latest[key] for key in sorted(latest)]


# --- arguments --------------------------------------------------------------------

_SEASON = Arg(
    "season",
    default=None,
    arrow_type=pa.int32(),
    ge=api.FIRST_SEASON,
    doc="Season year (2015 onwards). NULL selects the current season.",
)


@dataclass(kw_only=True)
class ScheduleArgs:
    """Arguments accepted by ``schedule``."""

    season: Annotated[int | None, _SEASON]
    series: Annotated[
        str | None,
        Arg(
            "series",
            default=None,
            arrow_type=pa.string(),
            choices=SERIES_CHOICES,
            doc=_SERIES_DOC + " NULL returns all three national series.",
        ),
    ]


@dataclass(kw_only=True)
class SeasonResultsArgs:
    """Arguments accepted by ``season_results``."""

    season: Annotated[int | None, _SEASON]
    series: Annotated[
        str,
        Arg("series", default="cup", arrow_type=pa.string(), choices=SERIES_CHOICES, doc=_SERIES_DOC),
    ]


@dataclass(kw_only=True)
class DriversArgs:
    """Arguments accepted by ``drivers``."""

    series: Annotated[
        str | None,
        Arg(
            "series",
            default=None,
            arrow_type=pa.string(),
            choices=SERIES_CHOICES,
            doc="Restrict to driver profiles for one series; NULL returns every profile.",
        ),
    ]


@dataclass(kw_only=True)
class RaceArgs:
    """Arguments shared by every per-race function."""

    race_id: Annotated[
        int, Arg(0, arrow_type=pa.int32(), ge=1, doc="NASCAR race identifier (see schedule()).")
    ]
    season: Annotated[
        int | None,
        Arg(
            "season",
            default=None,
            arrow_type=pa.int32(),
            ge=api.FIRST_SEASON,
            doc="Season of the race. Optional: NULL searches the seasons for race_id (slower).",
        ),
    ]


# --- schedule ---------------------------------------------------------------------


def schedule_rows(season: int, series: str | None) -> list[dict[str, Any]]:
    """Flattened schedule rows for one season, optionally one series."""
    races = api.season_races(season)
    if series is not None:
        wanted = api.series_id(series)
        races = [race for race in races if s.to_int(race.get("series_id")) == wanted]
    return s.schedule_rows(races, _names() if races else {})


_SCHEDULE_EXAMPLES = [
    (
        "The 2025 Cup Series points races and their winners.",
        "SELECT race_date, race_name, track_name, winner_name "
        "FROM nascar.main.schedule(season := 2025, series := 'cup') "
        "WHERE is_points_race ORDER BY race_date",
    ),
    (
        "Tracks visited by every national series in 2024.",
        "SELECT track_name, count(DISTINCT series_id) AS series "
        "FROM nascar.main.schedule(season := 2024) GROUP BY track_name HAVING series = 3",
    ),
]


@init_single_worker
@bind_fixed_schema
class ScheduleFunction(TableFunctionGenerator[ScheduleArgs]):
    """NASCAR race schedule and race summary for a season."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = s.SCHEDULE_SCHEMA

    class Meta:
        """Function discovery metadata."""

        name = "schedule"
        description = "NASCAR race schedule and race summaries by season and series"
        categories = ["motorsport", "schedule"]
        projection_pushdown = True
        tags = _tags(
            "schedule",
            s.SCHEDULE_SCHEMA,
            (
                "One row per race (points races and exhibitions) with date, track, laps, distance, "
                "stage lengths, pole and race winner, lead changes, cautions and broadcaster. Seasons "
                "2015 onwards (NASCAR publishes no 2017 list, so 2017 is empty); series cup, "
                "oreilly (xfinity) or truck, NULL for all. Use race_id from "
                "here with race_results(), lap_times(), pit_stops() and the other per-race functions."
            ),
            (
                "Season race lists from NASCAR's own feeds. `race_date` is the published US Eastern "
                "wall-clock start time. Before a race runs, `winner_driver_id` is NULL and "
                "`is_completed` is false. Winner names come from NASCAR's driver list."
            ),
            _SCHEDULE_EXAMPLES,
        )
        examples = _function_examples(_SCHEDULE_EXAMPLES[:1])

    @classmethod
    def cardinality(cls, params: object) -> TableCardinality:
        """About 40 races per series per season."""
        return TableCardinality(estimate=120, max=200)

    @classmethod
    def process(cls, params: ProcessParams[ScheduleArgs], state: None, out: OutputCollector) -> None:
        """Fetch and emit one season's schedule."""
        season = params.args.season or api.current_season()
        _emit(schedule_rows(season, params.args.series), params.output_schema, out, api.season_ttl(season))


_CURRENT_SCHEDULE_EXAMPLES = [
    (
        "Races scheduled and already run in each series this season.",
        "SELECT series_name, count(*) AS races, count(*) FILTER (WHERE is_completed) AS completed "
        "FROM nascar.main.all_current_schedule() GROUP BY series_name ORDER BY series_name",
    )
]


@init_single_worker
@bind_fixed_schema
class CurrentScheduleFunction(TableFunctionGenerator[None]):
    """Current-season schedule backing the ``current_schedule`` table."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = s.SCHEDULE_SCHEMA

    class Meta:
        """Function discovery metadata."""

        name = "all_current_schedule"
        description = "Current NASCAR season schedule (scan behind current_schedule)"
        categories = ["motorsport", "schedule"]
        projection_pushdown = True
        tags = _tags(
            "schedule",
            s.SCHEDULE_SCHEMA,
            (
                "The scan behind the current_schedule table. Prefer the table; use schedule() for "
                "another season or a single series."
            ),
            "Returns the current season's races for all three national series.",
            _CURRENT_SCHEDULE_EXAMPLES,
        )
        examples = _function_examples(_CURRENT_SCHEDULE_EXAMPLES)

    @classmethod
    def cardinality(cls, params: object) -> TableCardinality:
        """About 40 races per series per season."""
        return TableCardinality(estimate=120, max=200)

    @classmethod
    def process(cls, params: ProcessParams[None], state: None, out: OutputCollector) -> None:
        """Fetch and emit the current season's schedule."""
        season = api.current_season()
        _emit(schedule_rows(season, None), params.output_schema, out, api.TTL.current)


# --- standings --------------------------------------------------------------------


def current_standings_rows() -> list[dict[str, Any]]:
    """Points standings for all three series from the current season's feeds."""
    season = api.current_season()
    rows: list[dict[str, Any]] = []
    for series in (1, 2, 3):
        for candidate in (season, season - 1):
            feed = api.points_feed(candidate, series)
            if feed.found:
                rows.extend(s.standings_rows(feed.payload, candidate, series))
                break
    return rows


_CURRENT_STANDINGS_EXAMPLES = [
    (
        "Read the table's backing scan directly: the Cup Series top ten.",
        "SELECT position, driver_name, points, wins FROM nascar.main.all_current_standings() "
        "WHERE series_id = 1 ORDER BY position LIMIT 10",
    )
]


@init_single_worker
@bind_fixed_schema
class CurrentStandingsFunction(TableFunctionGenerator[None]):
    """Current driver points standings backing the ``current_standings`` table."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = s.STANDINGS_SCHEMA

    class Meta:
        """Function discovery metadata."""

        name = "all_current_standings"
        description = "Current NASCAR driver points standings (scan behind current_standings)"
        categories = ["motorsport", "standings"]
        projection_pushdown = True
        tags = _tags(
            "standings",
            s.STANDINGS_SCHEMA,
            (
                "The scan behind the current_standings table: one row per driver per series with "
                "points, position, wins, top 5s, top 10s, laps led, stage wins and playoff fields. "
                "Prefer the table."
            ),
            "Current-season driver points for Cup, O'Reilly Auto Parts (Xfinity) and Truck.",
            _CURRENT_STANDINGS_EXAMPLES,
        )
        examples = _function_examples(_CURRENT_STANDINGS_EXAMPLES)

    @classmethod
    def cardinality(cls, params: object) -> TableCardinality:
        """Roughly 40-100 point-scoring drivers per series."""
        return TableCardinality(estimate=200, max=600)

    @classmethod
    def process(cls, params: ProcessParams[None], state: None, out: OutputCollector) -> None:
        """Fetch and emit current standings."""
        _emit(current_standings_rows(), params.output_schema, out, api.TTL.standings)


# --- results ----------------------------------------------------------------------


def last_race_rows() -> list[dict[str, Any]]:
    """Official results of the most recently completed race in each series."""
    season = api.current_season()
    rows: list[dict[str, Any]] = []
    for race in latest_completed_races(api.season_races(season)):
        series = s.to_int(race.get("series_id")) or 0
        race_id = s.to_int(race.get("race_id")) or 0
        feed = api.race_feed(season, series, race_id, "weekend-feed")
        rows.extend(s.result_rows(feed.payload, season, series, race_id))
    return rows


_LAST_RACE_EXAMPLES = [
    (
        "Read the table's backing scan directly: each series' latest winner.",
        "SELECT series_id, race_name, driver_name FROM nascar.main.all_last_race_results() "
        "WHERE finishing_position = 1 ORDER BY series_id",
    )
]


@init_single_worker
@bind_fixed_schema
class LastRaceResultsFunction(TableFunctionGenerator[None]):
    """Latest race results backing the ``last_race_results`` table."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = s.RESULTS_SCHEMA

    class Meta:
        """Function discovery metadata."""

        name = "all_last_race_results"
        description = "Results of each series' most recent race (scan behind last_race_results)"
        categories = ["motorsport", "results"]
        projection_pushdown = True
        tags = _tags(
            "results",
            s.RESULTS_SCHEMA,
            "The scan behind the last_race_results table. Prefer the table; use race_results() for any race.",
            "Official finishing order of the latest completed Cup, O'Reilly and Truck races.",
            _LAST_RACE_EXAMPLES,
        )
        examples = _function_examples(_LAST_RACE_EXAMPLES)

    @classmethod
    def cardinality(cls, params: object) -> TableCardinality:
        """Three fields of roughly 36-40 cars."""
        return TableCardinality(estimate=115, max=150)

    @classmethod
    def process(cls, params: ProcessParams[None], state: None, out: OutputCollector) -> None:
        """Fetch and emit the latest race of each series."""
        _emit(last_race_rows(), params.output_schema, out, api.TTL.current)


def season_result_rows(season: int, series: str) -> list[dict[str, Any]]:
    """Every completed race's official results for one series and season."""
    wanted = api.series_id(series)
    races = [
        race
        for race in api.season_races(season)
        if s.to_int(race.get("series_id")) == wanted and _completed(race)
    ]
    ids = [s.to_int(race.get("race_id")) or 0 for race in races]
    feeds = api.fetch_many(
        [f"cacher/{season}/{wanted}/{race_id}/weekend-feed.json" for race_id in ids],
        ttl=api.season_ttl(season),
    )
    rows: list[dict[str, Any]] = []
    for race_id, feed in zip(ids, feeds, strict=True):
        rows.extend(s.result_rows(feed.payload, season, wanted, race_id))
    rows.sort(key=lambda row: (row["race_date"] or datetime.max, row["race_id"], row["finishing_position"]))
    return rows


_SEASON_RESULTS_EXAMPLES = [
    (
        "2025 Cup Series wins and top-fives by driver, points races only.",
        "SELECT r.driver_name, count(*) FILTER (WHERE r.finishing_position = 1) AS wins, "
        "count(*) FILTER (WHERE r.finishing_position <= 5) AS top_5 "
        "FROM nascar.main.season_results(season := 2025) r "
        "JOIN nascar.main.schedule(season := 2025, series := 'cup') USING (race_id) "
        "WHERE is_points_race GROUP BY r.driver_name ORDER BY wins DESC, top_5 DESC LIMIT 10",
    ),
    (
        "Laps led by team in the 2025 Truck Series.",
        "SELECT team_name, sum(laps_led) AS laps_led "
        "FROM nascar.main.season_results(season := 2025, series := 'truck') "
        "GROUP BY team_name ORDER BY laps_led DESC",
    ),
]


@init_single_worker
@bind_fixed_schema
class SeasonResultsFunction(TableFunctionGenerator[SeasonResultsArgs]):
    """Every race result of a series season."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = s.RESULTS_SCHEMA

    class Meta:
        """Function discovery metadata."""

        name = "season_results"
        description = "Official results of every completed race in one NASCAR series season"
        categories = ["motorsport", "results", "driver-stats", "team-stats"]
        projection_pushdown = True
        tags = _tags(
            "results",
            s.RESULTS_SCHEMA,
            (
                "One row per car per completed race of a season (points races and exhibitions; join "
                "schedule() on race_id and filter is_points_race when that matters). Aggregate it for "
                "season driver, team, owner or manufacturer statistics: wins, top 5s, laps led, average "
                "finish, DNFs (finishing_status <> 'Running'). Defaults to the current Cup season."
            ),
            (
                "The per-race official results for a whole season, fetched with bounded concurrency "
                "and cached. This is the source for season and career aggregates in SQL; NASCAR's "
                "points feeds are only reliable for the current season."
            ),
            _SEASON_RESULTS_EXAMPLES,
        )
        examples = _function_examples(_SEASON_RESULTS_EXAMPLES[:1])

    @classmethod
    def cardinality(cls, params: object) -> TableCardinality:
        """About 38 races of roughly 38 cars."""
        return TableCardinality(estimate=1_450, max=3_000)

    @classmethod
    def process(cls, params: ProcessParams[SeasonResultsArgs], state: None, out: OutputCollector) -> None:
        """Fetch and emit a whole season of results."""
        season = params.args.season or api.current_season()
        _emit(
            season_result_rows(season, params.args.series), params.output_schema, out, api.season_ttl(season)
        )


# --- per-race functions -----------------------------------------------------------


class _RaceFunction:
    """Shared ``process``/``cardinality`` for functions keyed by one race."""

    ESTIMATE: ClassVar[int] = 40
    MAXIMUM: ClassVar[int] = 100

    @classmethod
    def rows(cls, ref: api.RaceRef) -> list[dict[str, Any]]:  # pragma: no cover - overridden
        """Return the flattened rows for one resolved race."""
        raise NotImplementedError

    @classmethod
    def cardinality(cls, params: object) -> TableCardinality:
        """Return the per-race planner estimate."""
        return TableCardinality(estimate=cls.ESTIMATE, max=cls.MAXIMUM)

    @classmethod
    def process(cls, params: ProcessParams[RaceArgs], state: None, out: OutputCollector) -> None:
        """Resolve the race and emit its rows; an unknown race yields no rows."""
        ref = api.resolve_race(params.args.race_id, params.args.season)
        rows = cls.rows(ref) if ref is not None else []
        ttl = api.season_ttl(ref.season) if ref is not None else api.TTL.current
        _emit(rows, params.output_schema, out, ttl)


def _weekend(ref: api.RaceRef) -> Any:
    return api.race_feed(ref.season, ref.series_id, ref.race_id, "weekend-feed").payload


_RACE_RESULTS_EXAMPLES = [
    (
        "Finishing order of the 2026 Cup Series Championship Race.",
        "SELECT finishing_position, car_number, driver_name, team_name, laps_led, finishing_status "
        "FROM nascar.main.race_results(5601, season := 2026) ORDER BY finishing_position",
    )
]


@init_single_worker
@bind_fixed_schema
class RaceResultsFunction(_RaceFunction, TableFunctionGenerator[RaceArgs]):
    """Official results of one race."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = s.RESULTS_SCHEMA

    class Meta:
        """Function discovery metadata."""

        name = "race_results"
        description = "Official finishing order of one NASCAR race"
        categories = ["motorsport", "results"]
        projection_pushdown = True
        tags = _tags(
            "results",
            s.RESULTS_SCHEMA,
            (
                "One row per car: finishing and starting position, driver, team, owner, crew chief, "
                "manufacturer, laps completed and led, gap to the winner, finishing status and points "
                "earned. Pass race_id from schedule(); passing season too avoids a search. Published "
                "for races from 2018; earlier races return no rows."
            ),
            (
                "Official results from the race's weekend feed. `time_behind_seconds` is exact; cars "
                "a lap or more down report `laps_behind` instead."
            ),
            _RACE_RESULTS_EXAMPLES,
        )
        examples = _function_examples(_RACE_RESULTS_EXAMPLES)

    @classmethod
    def rows(cls, ref: api.RaceRef) -> list[dict[str, Any]]:
        """Flatten the official results."""
        return s.result_rows(_weekend(ref), ref.season, ref.series_id, ref.race_id)


_SESSION_EXAMPLES = [
    (
        "Qualifying order for the 2026 Cup Series Championship Race.",
        "SELECT position, driver_name, best_lap_time_seconds, best_lap_speed "
        "FROM nascar.main.session_results(5601, season := 2026) "
        "WHERE run_type = 'qualifying' ORDER BY position",
    )
]


@init_single_worker
@bind_fixed_schema
class SessionResultsFunction(_RaceFunction, TableFunctionGenerator[RaceArgs]):
    """Practice and qualifying results of one race weekend."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = s.SESSION_SCHEMA
    ESTIMATE = 80
    MAXIMUM = 400

    class Meta:
        """Function discovery metadata."""

        name = "session_results"
        description = "Practice and qualifying results for one NASCAR race weekend"
        categories = ["motorsport", "results", "qualifying"]
        projection_pushdown = True
        tags = _tags(
            "results",
            s.SESSION_SCHEMA,
            (
                "One row per car per practice or qualifying session of a race weekend with best lap "
                "time, best speed, laps run and gap to the fastest car. Filter run_type = 'qualifying' "
                "for qualifying."
            ),
            "Weekend practice and qualifying classifications from the race's weekend feed.",
            _SESSION_EXAMPLES,
        )
        examples = _function_examples(_SESSION_EXAMPLES)

    @classmethod
    def rows(cls, ref: api.RaceRef) -> list[dict[str, Any]]:
        """Flatten the weekend sessions."""
        return s.session_rows(_weekend(ref), ref.season, ref.series_id, ref.race_id)


_STAGE_EXAMPLES = [
    (
        "Stage winners of the 2026 Cup Series Championship Race.",
        "SELECT stage_number, driver_name, stage_points FROM nascar.main.stage_results(5601, season := 2026) "
        "WHERE position = 1 ORDER BY stage_number",
    )
]


@init_single_worker
@bind_fixed_schema
class StageResultsFunction(_RaceFunction, TableFunctionGenerator[RaceArgs]):
    """Stage finishing order of one race."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = s.STAGE_SCHEMA
    ESTIMATE = 20

    class Meta:
        """Function discovery metadata."""

        name = "stage_results"
        description = "Top-ten stage finishes and stage points for one NASCAR race"
        categories = ["motorsport", "results", "stages"]
        projection_pushdown = True
        tags = _tags(
            "race-detail",
            s.STAGE_SCHEMA,
            (
                "One row per car scoring stage points at the end of each stage (usually the top ten "
                "of stages 1 and 2). The final stage is the race result itself."
            ),
            "Stage classifications from the race's weekend feed.",
            _STAGE_EXAMPLES,
        )
        examples = _function_examples(_STAGE_EXAMPLES)

    @classmethod
    def rows(cls, ref: api.RaceRef) -> list[dict[str, Any]]:
        """Flatten the stage results."""
        return s.stage_rows(_weekend(ref), ref.season, ref.series_id, ref.race_id)


_CAUTION_EXAMPLES = [
    (
        "Cautions in the 2026 Cup Series Championship Race.",
        "SELECT caution_number, start_lap, end_lap, laps, reason, comment "
        "FROM nascar.main.race_cautions(5601, season := 2026) ORDER BY caution_number",
    )
]


@init_single_worker
@bind_fixed_schema
class RaceCautionsFunction(_RaceFunction, TableFunctionGenerator[RaceArgs]):
    """Caution periods of one race."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = s.CAUTION_SCHEMA
    ESTIMATE = 8

    class Meta:
        """Function discovery metadata."""

        name = "race_cautions"
        description = "Caution periods (yellow flags) of one NASCAR race"
        categories = ["motorsport", "race-detail", "cautions"]
        projection_pushdown = True
        tags = _tags(
            "race-detail",
            s.CAUTION_SCHEMA,
            (
                "One row per caution period with start and end lap, laps under yellow, reason "
                "(Competition, Accident, Debris, Stage...), NASCAR's comment and the free-pass car."
            ),
            "Caution segments from the race's weekend feed; `laps` counts both end laps.",
            _CAUTION_EXAMPLES,
        )
        examples = _function_examples(_CAUTION_EXAMPLES)

    @classmethod
    def rows(cls, ref: api.RaceRef) -> list[dict[str, Any]]:
        """Flatten the caution segments."""
        return s.caution_rows(_weekend(ref), ref.season, ref.series_id, ref.race_id)


_LEADER_EXAMPLES = [
    (
        "Laps led by driver in the 2026 Cup Series Championship Race.",
        "SELECT driver_name, sum(laps_led) AS laps_led, count(*) AS stints "
        "FROM nascar.main.race_leaders(5601, season := 2026) GROUP BY driver_name ORDER BY laps_led DESC",
    )
]


@init_single_worker
@bind_fixed_schema
class RaceLeadersFunction(_RaceFunction, TableFunctionGenerator[RaceArgs]):
    """Lead changes of one race."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = s.LEADER_SCHEMA
    ESTIMATE = 20

    class Meta:
        """Function discovery metadata."""

        name = "race_leaders"
        description = "Lap-by-lap lead stints of one NASCAR race"
        categories = ["motorsport", "race-detail", "lead changes"]
        projection_pushdown = True
        tags = _tags(
            "race-detail",
            s.LEADER_SCHEMA,
            (
                "One row per lead stint (start lap, end lap, laps led) with the leading car and driver. "
                "Summing laps_led per driver reproduces race_results laps_led."
            ),
            "Lead stints from the race's weekend feed, with drivers matched by car number.",
            _LEADER_EXAMPLES,
        )
        examples = _function_examples(_LEADER_EXAMPLES)

    @classmethod
    def rows(cls, ref: api.RaceRef) -> list[dict[str, Any]]:
        """Flatten the lead stints."""
        return s.leader_rows(_weekend(ref), ref.season, ref.series_id, ref.race_id)


_LAP_TIME_EXAMPLES = [
    (
        "Each driver's fastest lap in the 2026 Cup Series Championship Race.",
        "SELECT driver_name, min(lap_time_seconds) AS best_lap, max(lap_speed) AS best_speed "
        "FROM nascar.main.lap_times(5601, season := 2026) GROUP BY driver_name ORDER BY best_lap LIMIT 10",
    )
]


@init_single_worker
@bind_fixed_schema
class LapTimesFunction(_RaceFunction, TableFunctionGenerator[RaceArgs]):
    """Every lap of every car in one race."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = s.LAP_TIMES_SCHEMA
    ESTIMATE = 10_000
    MAXIMUM = 40_000

    class Meta:
        """Function discovery metadata."""

        name = "lap_times"
        description = "Lap-by-lap times, speeds and running positions for one NASCAR race (2020 onwards)"
        categories = ["motorsport", "race-detail", "lap times"]
        projection_pushdown = True
        tags = _tags(
            "race-detail",
            s.LAP_TIMES_SCHEMA,
            (
                "One row per car per lap: lap time in seconds, speed in mph, running position and the "
                "flag at the end of the lap (filter flag_state = 1 for green-flag pace). Lap 0 is the "
                "starting grid. Available from 2020; earlier races return no rows."
            ),
            (
                "Lap times from NASCAR's lap-times feed. Times and speeds are exact decimals; scoring "
                "markers such as `*` and `(i)` are removed from driver names."
            ),
            _LAP_TIME_EXAMPLES,
        )
        examples = _function_examples(_LAP_TIME_EXAMPLES)

    @classmethod
    def rows(cls, ref: api.RaceRef) -> list[dict[str, Any]]:
        """Flatten the lap times."""
        feed = api.race_feed(ref.season, ref.series_id, ref.race_id, "lap-times")
        return s.lap_time_rows(feed.payload, ref.season, ref.series_id, ref.race_id)


_PIT_EXAMPLES = [
    (
        "Fastest four-tire green-flag stops in the 2026 Cup Series Championship Race.",
        "SELECT driver_name, lap, stop_seconds, pit_road_seconds "
        "FROM nascar.main.pit_stops(5601, season := 2026) "
        "WHERE stop_type = 'FOUR_WHEEL_CHANGE' AND pit_in_flag_state = 1 ORDER BY stop_seconds LIMIT 10",
    )
]


@init_single_worker
@bind_fixed_schema
class PitStopsFunction(_RaceFunction, TableFunctionGenerator[RaceArgs]):
    """Pit-road visits in one race."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = s.PIT_STOP_SCHEMA
    ESTIMATE = 300
    MAXIMUM = 1_500

    class Meta:
        """Function discovery metadata."""

        name = "pit_stops"
        description = "Timed pit-road visits with tire service for one NASCAR race"
        categories = ["motorsport", "race-detail", "pit stops"]
        projection_pushdown = True
        tags = _tags(
            "race-detail",
            s.PIT_STOP_SCHEMA,
            (
                "One row per pit-road visit: lap, flag at entry, total pit-road time, stationary stop "
                "time, travel in and out, tires changed and positions gained or lost. stop_type "
                "FOUR_WHEEL_CHANGE / TWO_WHEEL_CHANGE_* / OTHER. Available from 2020."
            ),
            "Pit timing from NASCAR's live pit data feed; durations are exact seconds.",
            _PIT_EXAMPLES,
        )
        examples = _function_examples(_PIT_EXAMPLES)

    @classmethod
    def rows(cls, ref: api.RaceRef) -> list[dict[str, Any]]:
        """Flatten the pit data, resolving drivers by car number."""
        feed = api.race_feed(ref.season, ref.series_id, ref.race_id, "live-pit-data")
        if not feed.found:
            return []
        cars = s.car_index(_weekend(ref))
        return s.pit_stop_rows(feed.payload, cars, ref.season, ref.series_id, ref.race_id)


_NOTE_EXAMPLES = [
    (
        "Commentary for the final ten laps of the 2026 Cup Series Championship Race.",
        "SELECT lap, note FROM nascar.main.lap_notes(5601, season := 2026) "
        "WHERE lap >= 258 ORDER BY lap, note_id",
    )
]


@init_single_worker
@bind_fixed_schema
class LapNotesFunction(_RaceFunction, TableFunctionGenerator[RaceArgs]):
    """Lap-by-lap commentary of one race."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = s.LAP_NOTES_SCHEMA
    ESTIMATE = 100
    MAXIMUM = 1_000

    class Meta:
        """Function discovery metadata."""

        name = "lap_notes"
        description = "NASCAR's lap-by-lap race commentary notes for one race"
        categories = ["motorsport", "race-detail", "commentary"]
        projection_pushdown = True
        tags = _tags(
            "race-detail",
            s.LAP_NOTES_SCHEMA,
            (
                "One row per commentary note with the lap, flag and the driver IDs mentioned. Use "
                "list_contains(driver_ids, <id>) to follow one driver. Lap 0 covers pre-race events. "
                "Available from 2020."
            ),
            "Lap notes from NASCAR's lap-notes feed.",
            _NOTE_EXAMPLES,
        )
        examples = _function_examples(_NOTE_EXAMPLES)

    @classmethod
    def rows(cls, ref: api.RaceRef) -> list[dict[str, Any]]:
        """Flatten the lap notes."""
        feed = api.race_feed(ref.season, ref.series_id, ref.race_id, "lap-notes")
        return s.lap_note_rows(feed.payload, ref.season, ref.series_id, ref.race_id)


_LOOP_EXAMPLES = [
    (
        "Highest driver ratings in the 2026 Cup Series Championship Race.",
        "SELECT driver_name, driver_rating, green_flag_passes, quality_passes, average_position "
        "FROM nascar.main.loop_stats(5601, season := 2026) ORDER BY driver_rating DESC LIMIT 10",
    )
]


@init_single_worker
@bind_fixed_schema
class LoopStatsFunction(_RaceFunction, TableFunctionGenerator[RaceArgs]):
    """Loop-data driver statistics of one race."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = s.LOOP_STATS_SCHEMA

    class Meta:
        """Function discovery metadata."""

        name = "loop_stats"
        description = "NASCAR loop-data statistics (driver rating, passes, average position) for one race"
        categories = ["motorsport", "race-detail", "driver-stats"]
        projection_pushdown = True
        tags = _tags(
            "race-detail",
            s.LOOP_STATS_SCHEMA,
            (
                "One row per driver: driver rating (0-150), green-flag passes, times passed, quality "
                "passes, fastest laps, laps in the top 15, average/best/worst running position and "
                "closing position. Available from 2019."
            ),
            "Loop statistics from NASCAR's loopstats feed, with names matched from the race results.",
            _LOOP_EXAMPLES,
        )
        examples = _function_examples(_LOOP_EXAMPLES)

    @classmethod
    def rows(cls, ref: api.RaceRef) -> list[dict[str, Any]]:
        """Flatten the loop statistics."""
        feed = api.loop_stats(ref.season, ref.series_id, ref.race_id)
        if not feed.found:
            return []
        drivers = s.driver_index(_weekend(ref))
        return s.loop_stat_rows(feed.payload, drivers, ref.season, ref.series_id, ref.race_id)


# --- drivers and live -------------------------------------------------------------

_DRIVER_EXAMPLES = [
    (
        "Published Cup Series driver profiles with hometowns.",
        "SELECT full_name, team, hometown_city, hometown_state, birth_date "
        "FROM nascar.main.drivers(series := 'cup') WHERE profile_published ORDER BY last_name",
    )
]


@init_single_worker
@bind_fixed_schema
class DriversFunction(TableFunctionGenerator[DriversArgs]):
    """NASCAR's driver master list."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = s.DRIVERS_SCHEMA

    class Meta:
        """Function discovery metadata."""

        name = "drivers"
        description = "NASCAR driver profiles: names, birth dates, hometowns, teams and rookie years"
        categories = ["motorsport", "drivers"]
        projection_pushdown = True
        tags = _tags(
            "drivers",
            s.DRIVERS_SCHEMA,
            (
                "One row per driver profile (a driver can have one per series). Use to resolve "
                "driver_id to names, birth dates, hometowns, current team, crew chief and rookie "
                "years. profile_published is true for drivers with a live nascar.com page."
            ),
            (
                "NASCAR's driver master list. Image and profile columns are links to nascar.com; "
                "no media is copied. Many historical profiles are sparse drafts."
            ),
            _DRIVER_EXAMPLES,
        )
        examples = _function_examples(_DRIVER_EXAMPLES)

    @classmethod
    def cardinality(cls, params: object) -> TableCardinality:
        """About 800 driver profiles."""
        return TableCardinality(estimate=800, max=3_000)

    @classmethod
    def process(cls, params: ProcessParams[DriversArgs], state: None, out: OutputCollector) -> None:
        """Fetch and emit driver profiles."""
        series = params.args.series
        _emit(s.driver_rows(api.drivers().payload, series), params.output_schema, out, api.TTL.drivers)


_LIVE_EXAMPLES = [
    (
        "Read the table's backing scan directly: is anything on track right now?",
        "SELECT count(*) AS sessions_on_track, max(lap_number) AS lap, max(flag) AS flag "
        "FROM nascar.main.all_live_race()",
    )
]


@init_single_worker
@bind_fixed_schema
class LiveRaceFunction(TableFunctionGenerator[None]):
    """Live session status backing the ``live_race`` table."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = s.LIVE_SCHEMA

    class Meta:
        """Function discovery metadata."""

        name = "all_live_race"
        description = "Status of the NASCAR session currently on track (scan behind live_race)"
        categories = ["motorsport", "live"]
        projection_pushdown = True
        tags = _tags(
            "live",
            s.LIVE_SCHEMA,
            "The scan behind the live_race table: zero rows when nothing is on track. Prefer the table.",
            "Live timing-and-scoring summary, refreshed at most every 15 seconds.",
            _LIVE_EXAMPLES,
        )
        examples = _function_examples(_LIVE_EXAMPLES)

    @classmethod
    def cardinality(cls, params: object) -> TableCardinality:
        """At most one session is live."""
        return TableCardinality(estimate=1, max=1)

    @classmethod
    def process(cls, params: ProcessParams[None], state: None, out: OutputCollector) -> None:
        """Fetch and emit the live session summary."""
        _emit(s.live_rows(api.live_feed().payload), params.output_schema, out, api.TTL.live)


FUNCTIONS = [
    ScheduleFunction,
    CurrentScheduleFunction,
    CurrentStandingsFunction,
    LastRaceResultsFunction,
    SeasonResultsFunction,
    RaceResultsFunction,
    SessionResultsFunction,
    StageResultsFunction,
    RaceCautionsFunction,
    RaceLeadersFunction,
    LapTimesFunction,
    PitStopsFunction,
    LapNotesFunction,
    LoopStatsFunction,
    DriversFunction,
    LiveRaceFunction,
]
