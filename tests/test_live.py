"""Opt-in smoke tests against NASCAR's public cf.nascar.com feeds."""

from __future__ import annotations

import pytest

from vgi_nascar import api
from vgi_nascar import functions as f
from vgi_nascar import schemas as s

pytestmark = pytest.mark.live


def test_current_schedule_and_standings() -> None:
    schedule = f.schedule_rows(api.current_season(), None)
    assert {row["series_id"] for row in schedule} == {1, 2, 3}
    standings = f.current_standings_rows()
    assert {row["series_id"] for row in standings} == {1, 2, 3}
    for series in (1, 2, 3):
        rows = [row for row in standings if row["series_id"] == series]
        assert rows[0]["position"] == 1 and rows[0]["points_behind_leader"] == 0
        assert len({row["driver_id"] for row in rows}) == len(rows)
    s.batch(standings, s.STANDINGS_SCHEMA)


def test_pinned_2026_championship_race() -> None:
    ref = api.resolve_race(5601, season=2026)
    assert ref is not None and ref.series_id == 1
    results = f.RaceResultsFunction.rows(ref)
    assert len(results) == 40
    assert results[0]["driver_name"] == "Ryan Blaney"
    leaders = f.RaceLeadersFunction.rows(ref)
    assert sum(row["laps_led"] for row in leaders) == 267
    led = {row["driver_id"]: row["laps_led"] for row in results}
    by_driver: dict[int, int] = {}
    for row in leaders:
        by_driver[row["driver_id"]] = by_driver.get(row["driver_id"], 0) + row["laps_led"]
    assert all(led[driver] == laps for driver, laps in by_driver.items())
    cautions = f.RaceCautionsFunction.rows(ref)
    assert sum(row["laps"] for row in cautions) == ref.race["number_of_caution_laps"]
    laps = f.LapTimesFunction.rows(ref)
    assert max(row["lap"] for row in laps) == 267
    assert f.PitStopsFunction.rows(ref)
    assert len(f.LoopStatsFunction.rows(ref)) == 40
    assert f.LapNotesFunction.rows(ref)


def test_2025_cup_win_totals() -> None:
    rows = f.season_result_rows(2025, "cup")
    schedule = {row["race_id"]: row for row in f.schedule_rows(2025, "cup")}
    wins: dict[str, int] = {}
    for row in rows:
        if row["finishing_position"] == 1 and schedule[row["race_id"]]["is_points_race"]:
            wins[row["driver_name"]] = wins.get(row["driver_name"], 0) + 1
    assert sum(wins.values()) == 36
    assert wins["Denny Hamlin"] == 6


def test_missing_history_is_empty_not_an_error() -> None:
    assert f.schedule_rows(2017, None) == []
    ref = api.resolve_race(4390, season=2015)
    assert ref is not None
    assert f.RaceResultsFunction.rows(ref) == []


def test_drivers_and_live_feed() -> None:
    rows = s.driver_rows(api.drivers().payload)
    assert len(rows) > 500
    assert any(row["full_name"] == "Ryan Blaney" for row in rows)
    assert len(s.live_rows(api.live_feed().payload)) <= 1
