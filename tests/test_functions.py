"""Table-function behaviour against fixture-backed feeds."""

from __future__ import annotations

from types import SimpleNamespace

import pyarrow as pa
import pytest

from vgi_nascar import functions as f
from vgi_nascar import schemas as s


class _Out:
    def __init__(self) -> None:
        self.batches: list[pa.RecordBatch] = []
        self.cache_controls: list[object] = []
        self.finished = False

    def emit(self, batch: pa.RecordBatch, cache_control: object = None) -> None:
        self.batches.append(batch)
        self.cache_controls.append(cache_control)

    def finish(self) -> None:
        self.finished = True


def _run(function, args=None) -> pa.RecordBatch:
    out = _Out()
    function.process(SimpleNamespace(args=args, output_schema=function.FIXED_SCHEMA), None, out)
    assert out.finished and len(out.batches) == 1
    assert out.cache_controls[0].ttl > 0
    return out.batches[0]


def _race(race_id: int = 5601, season: int | None = 2026) -> f.RaceArgs:
    return f.RaceArgs(race_id=race_id, season=season)


def test_current_tables(fixture_feeds: list[str]) -> None:
    schedule = _run(f.CurrentScheduleFunction)
    assert set(schedule.column("series_id").to_pylist()) == {1, 2, 3}
    standings = _run(f.CurrentStandingsFunction)
    assert set(standings.column("series_id").to_pylist()) == {1, 2, 3}
    last = _run(f.LastRaceResultsFunction)
    assert sorted(set(last.column("race_id").to_pylist())) == [5601, 5639, 5670]
    winners = [row for row in last.to_pylist() if row["finishing_position"] == 1]
    assert {row["driver_name"] for row in winners} == {"Ryan Blaney", "Brandon Jones", "Kaden Honeycutt"}
    assert _run(f.LiveRaceFunction).num_rows == 0


def test_schedule_filters_series(fixture_feeds: list[str]) -> None:
    batch = _run(f.ScheduleFunction, f.ScheduleArgs(season=2026, series="truck"))
    assert set(batch.column("series_id").to_pylist()) == {3}
    assert _run(f.ScheduleFunction, f.ScheduleArgs(season=None, series=None)).num_rows > batch.num_rows
    assert _run(f.ScheduleFunction, f.ScheduleArgs(season=2014, series=None)).num_rows == 0


def test_season_results(fixture_feeds: list[str]) -> None:
    batch = _run(f.SeasonResultsFunction, f.SeasonResultsArgs(season=2026, series="cup"))
    # Only race 5601 has a fixture weekend feed; the others are absent (403) and contribute nothing.
    assert set(batch.column("race_id").to_pylist()) == {5601}
    assert "cacher/2026/1/5596/weekend-feed.json" in fixture_feeds


@pytest.mark.parametrize(
    ("function", "minimum"),
    [
        (f.RaceResultsFunction, 8),
        (f.SessionResultsFunction, 6),
        (f.StageResultsFunction, 6),
        (f.RaceCautionsFunction, 3),
        (f.RaceLeadersFunction, 19),
        (f.LapTimesFunction, 18),
        (f.PitStopsFunction, 6),
        (f.LapNotesFunction, 3),
        (f.LoopStatsFunction, 3),
    ],
)
def test_race_functions(fixture_feeds: list[str], function, minimum: int) -> None:
    batch = _run(function, _race())
    assert batch.num_rows >= minimum
    assert set(batch.column("race_id").to_pylist()) == {5601}
    assert batch.schema == function.FIXED_SCHEMA


def test_race_function_resolves_season(fixture_feeds: list[str]) -> None:
    assert _run(f.RaceResultsFunction, _race(season=None)).num_rows >= 8


@pytest.mark.parametrize("function", [f.RaceResultsFunction, f.PitStopsFunction, f.LoopStatsFunction])
def test_unknown_or_missing_race_yields_no_rows(fixture_feeds: list[str], function) -> None:
    assert _run(function, _race(race_id=999_999)).num_rows == 0
    # Race 5596 is listed but none of its per-race feeds exist in the fixtures (S3 403).
    assert _run(function, _race(race_id=5596)).num_rows == 0


def test_drivers(fixture_feeds: list[str]) -> None:
    assert _run(f.DriversFunction, f.DriversArgs(series=None)).num_rows == 4
    cup = _run(f.DriversFunction, f.DriversArgs(series="cup"))
    assert set(cup.column("series").to_pylist()) == {"nascar-cup-series"}


def test_projected_output_schema(fixture_feeds: list[str]) -> None:
    projected = pa.schema([s.RESULTS_SCHEMA.field("driver_name"), s.RESULTS_SCHEMA.field("laps_led")])
    out = _Out()
    f.RaceResultsFunction.process(SimpleNamespace(args=_race(), output_schema=projected), None, out)
    assert out.batches[0].schema == projected


def test_latest_completed_races_picks_one_per_series() -> None:
    races = [
        {"series_id": 1, "race_id": 1, "race_date": "2026-01-01T00:00:00", "winner_driver_id": 5},
        {"series_id": 1, "race_id": 2, "race_date": "2026-02-01T00:00:00", "winner_driver_id": 6},
        {"series_id": 1, "race_id": 3, "race_date": "2026-03-01T00:00:00", "winner_driver_id": 0},
        {"series_id": None, "race_id": 4},
    ]
    assert [race["race_id"] for race in f.latest_completed_races(races)] == [2]


def test_cardinalities_are_declared() -> None:
    for function in f.FUNCTIONS:
        estimate = function.cardinality(None)
        assert 0 < estimate.estimate <= estimate.max


def test_missing_winners_are_read_from_official_results(fixture_feeds: list[str]) -> None:
    # NASCAR's 2018-2019 race lists carry no winner_driver_id; the worker reads the
    # winner from each completed race's weekend feed instead.
    races = [
        {"race_id": 5601, "series_id": 1, "actual_laps": 267, "race_season": 2026},
        {"race_id": 5639, "series_id": 2, "actual_laps": 200, "winner_driver_id": 4062},
        {"race_id": 9999, "series_id": 1, "actual_laps": 0},
    ]
    assert f.missing_winners(2026, races) == {5601: (4023, "Ryan Blaney")}
    assert fixture_feeds == ["cacher/2026/1/5601/weekend-feed.json"]
    rows = s.schedule_rows(races, {}, f.missing_winners(2026, races))
    final = next(row for row in rows if row["race_id"] == 5601)
    assert final["winner_driver_id"] == 4023 and final["winner_name"] == "Ryan Blaney"


def test_missing_winners_skip_seasons_without_results(fixture_feeds: list[str]) -> None:
    races = [{"race_id": 4390, "series_id": 1, "actual_laps": 200}]
    assert f.missing_winners(2016, races) == {}
    assert fixture_feeds == []
