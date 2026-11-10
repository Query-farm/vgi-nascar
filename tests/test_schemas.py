"""Flattening of real (trimmed) NASCAR feed responses into typed Arrow batches."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pyarrow as pa
import pytest
from conftest import load

from vgi_nascar import api
from vgi_nascar import schemas as s

ALL_SCHEMAS = [
    s.SCHEDULE_SCHEMA,
    s.RESULTS_SCHEMA,
    s.SESSION_SCHEMA,
    s.STAGE_SCHEMA,
    s.CAUTION_SCHEMA,
    s.LEADER_SCHEMA,
    s.LAP_TIMES_SCHEMA,
    s.PIT_STOP_SCHEMA,
    s.LAP_NOTES_SCHEMA,
    s.LOOP_STATS_SCHEMA,
    s.STANDINGS_SCHEMA,
    s.DRIVERS_SCHEMA,
    s.LIVE_SCHEMA,
]


@pytest.mark.parametrize("schema", ALL_SCHEMAS)
def test_every_column_has_a_comment(schema: pa.Schema) -> None:
    for column in schema:
        assert column.metadata and column.metadata[b"comment"], column.name


@pytest.mark.parametrize("schema", ALL_SCHEMAS)
def test_empty_batches_keep_their_schema(schema: pa.Schema) -> None:
    assert s.batch([], schema).schema == schema


def test_schedule_rows() -> None:
    races = api.races_of(api.Feed(payload=load("race_list_2026.json"), ttl=1))
    rows = s.schedule_rows(races, {4023: "Ryan Blaney"})
    batch = s.batch(rows, s.SCHEDULE_SCHEMA)
    assert batch.schema == s.SCHEDULE_SCHEMA
    final = next(row for row in rows if row["race_id"] == 5601)
    assert final["series_name"] == "NASCAR Cup Series"
    assert final["track_name"] == "Homestead-Miami Speedway"
    assert final["race_date"] == datetime(2026, 11, 8, 15, 0)
    assert final["winner_driver_id"] == 4023 and final["winner_name"] == "Ryan Blaney"
    assert final["is_points_race"] and final["is_completed"]
    clash = next(row for row in rows if row["race_id"] == 5593)
    assert clash["is_points_race"] is False
    assert [row["series_id"] for row in rows] == sorted(row["series_id"] for row in rows)


def test_result_rows() -> None:
    rows = s.result_rows(load("weekend_feed_2026_1_5601.json"), 2026, 1, 5601)
    assert s.batch(rows, s.RESULTS_SCHEMA).num_rows == len(rows)
    winner = rows[0]
    assert winner["finishing_position"] == 1
    assert winner["driver_name"] == "Ryan Blaney"
    assert winner["manufacturer"] == "Ford"
    assert winner["time_behind_seconds"] == Decimal("0.000")
    assert rows[1]["time_behind_seconds"] == Decimal("0.314")
    assert winner["race_name"] == "NASCAR Cup Series Championship Race"
    assert any(row["finishing_status"] != "Running" for row in rows)


def test_session_rows() -> None:
    rows = s.session_rows(load("weekend_feed_2026_1_5601.json"), 2026, 1, 5601)
    s.batch(rows, s.SESSION_SCHEMA)
    assert {row["run_type"] for row in rows} == {"practice", "qualifying"}
    practice = rows[0]
    assert practice["run_date_utc"] == datetime(2026, 11, 6, 22, 30, tzinfo=UTC)
    assert practice["best_lap_time_seconds"] == Decimal("32.680")
    assert practice["driver_name"] == "Chase Briscoe"


def test_stage_caution_and_leader_rows() -> None:
    payload = load("weekend_feed_2026_1_5601.json")
    stages = s.stage_rows(payload, 2026, 1, 5601)
    assert [(row["stage_number"], row["position"]) for row in stages][:2] == [(1, 1), (1, 2)]
    assert stages[0]["driver_name"] == "Tyler Reddick" and stages[0]["stage_points"] == 10

    cautions = s.caution_rows(payload, 2026, 1, 5601)
    assert [row["laps"] for row in cautions] == [10, 7, 6]
    assert sum(row["laps"] for row in cautions) == s.weekend_race(payload)["number_of_caution_laps"]

    leaders = s.leader_rows(payload, 2026, 1, 5601)
    assert sum(row["laps_led"] for row in leaders) == 267
    assert leaders[0]["driver_name"] == "Chase Briscoe" and leaders[0]["car_number"] == "19"
    s.batch(leaders, s.LEADER_SCHEMA)


def test_lap_time_rows() -> None:
    rows = s.lap_time_rows(load("lap_times_2026_1_5601.json"), 2026, 1, 5601)
    s.batch(rows, s.LAP_TIMES_SCHEMA)
    grid = [row for row in rows if row["lap"] == 0]
    assert all(row["lap_time_seconds"] is None for row in grid)
    assert grid[0]["flag_state"] == 8
    blaney = next(row for row in rows if row["driver_id"] == 4023 and row["lap"] == 1)
    assert blaney["driver_name"] == "Ryan Blaney"
    assert blaney["lap_time_seconds"] == Decimal("34.714")
    assert blaney["lap_speed"] == Decimal("155.557")
    assert blaney["manufacturer"] == "Ford"


def test_pit_stop_rows() -> None:
    cars = s.car_index(load("weekend_feed_2026_1_5601.json"))
    rows = s.pit_stop_rows(load("live_pit_data_2026_1_5601.json"), cars, 2026, 1, 5601)
    s.batch(rows, s.PIT_STOP_SCHEMA)
    four = next(row for row in rows if row["stop_type"] == "FOUR_WHEEL_CHANGE")
    assert four["tires_changed"] == 4
    assert four["stop_seconds"] is not None
    assert all(row["stop_number"] >= 1 for row in rows)


def test_lap_note_rows() -> None:
    rows = s.lap_note_rows(load("lap_notes_2026_1_5601.json"), 2026, 1, 5601)
    s.batch(rows, s.LAP_NOTES_SCHEMA)
    assert rows[0]["lap"] == 0
    for row in rows:
        assert len(row["driver_ids"]) == len(set(row["driver_ids"]))


def test_loop_stat_rows() -> None:
    drivers = s.driver_index(load("weekend_feed_2026_1_5601.json"))
    rows = s.loop_stat_rows(load("loopstats_2026_1_5601.json"), drivers, 2026, 1, 5601)
    s.batch(rows, s.LOOP_STATS_SCHEMA)
    blaney = next(row for row in rows if row["driver_id"] == 4023)
    assert blaney["driver_name"] == "Ryan Blaney"
    assert blaney["driver_rating"] == pytest.approx(118.81)
    assert blaney["finish_position"] == 1


def test_standings_rows() -> None:
    rows = s.standings_rows(load("points_feed_2026_1.json"), 2026, 1)
    s.batch(rows, s.STANDINGS_SCHEMA)
    leader = rows[0]
    assert leader["position"] == 1 and leader["points_behind_leader"] == 0
    assert leader["driver_name"] == "Kyle Larson"
    assert rows[1]["points_behind_leader"] > 0
    assert leader["playoff_eligible"] is True


def test_driver_rows_deduplicate_and_filter() -> None:
    payload = load("drivers.json")
    rows = s.driver_rows(payload)
    keys = [(row["driver_id"], row["series"]) for row in rows]
    assert len(keys) == len(set(keys))
    assert all(row["driver_id"] > 0 for row in rows)
    blaney = next(row for row in rows if row["driver_id"] == 4023)
    assert blaney["birth_date"] == date(1993, 12, 31)
    assert blaney["rookie_year_cup"] == 2017
    assert blaney["profile_published"] is True
    assert {row["series"] for row in s.driver_rows(payload, "cup")} == {"nascar-cup-series"}
    assert s.driver_names(payload)[4030] == "Kyle Larson"
    s.batch(rows, s.DRIVERS_SCHEMA)


def test_live_rows() -> None:
    assert s.live_rows(load("live_feed_idle.json")) == []
    active = {
        **load("live_feed_idle.json"),
        "race_id": 5601,
        "series_id": 1,
        "run_type": 3,
        "flag_state": 2,
        "vehicles": [{}, {}],
    }
    rows = s.live_rows(active)
    assert rows[0]["flag"] == "yellow" and rows[0]["run_type"] == "race"
    assert rows[0]["cars_on_track"] == 2
    assert rows[0]["updated_at"] == datetime(2026, 11, 10, 17, 29, 40, 273782, tzinfo=UTC)
    s.batch(rows, s.LIVE_SCHEMA)


def test_coercions() -> None:
    assert s.to_int(True) is None and s.to_int("7") == 7 and s.to_int("7.0") == 7 and s.to_int("x") is None
    assert s.to_positive_int(0) is None and s.to_positive_int(-1) is None
    assert s.to_float("nan") is None and s.to_float("1.5") == 1.5 and s.to_float("x") is None
    assert s.to_decimal("1.23456") == Decimal("1.235") and s.to_decimal("inf") is None
    assert s.to_decimal("x") is None
    assert s.to_str("  ") is None and s.to_str(" a ") == "a"
    assert s.to_bool("false") is False and s.to_bool(1) is True and s.to_bool("maybe") is None
    assert s.to_date("0001-01-01T00:00:00") is None and s.to_date("") is None
    assert s.to_utc_timestamp("garbage") is None and s.to_local_timestamp("garbage") is None
    assert s.to_utc_timestamp("2026-01-01T00:00:00") == datetime(2026, 1, 1, tzinfo=UTC)
    assert s.clean_driver_name("* Chad Finchum(i)") == "Chad Finchum"
    assert s.clean_driver_name("Ryan Blaney (C)") == "Ryan Blaney"
    assert s.manufacturer("Tyt") == "Toyota" and s.manufacturer("Ford") == "Ford"
