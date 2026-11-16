"""Typed Arrow schemas and flattening for NASCAR's cf.nascar.com feeds."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import pyarrow as pa

from vgi_nascar.meta import field

TIMESTAMP_ET = pa.timestamp("us")
TIMESTAMP_UTC = pa.timestamp("us", tz="UTC")
SECONDS = pa.decimal128(9, 3)
SPEED = pa.decimal128(8, 3)

FLAG_NAMES = {1: "green", 2: "yellow", 3: "red", 4: "checkered", 8: "pre-race", 9: "none"}
RUN_TYPES = {1: "practice", 2: "qualifying", 3: "race"}
MANUFACTURERS = {"Chv": "Chevrolet", "Frd": "Ford", "Tyt": "Toyota", "Ram": "Ram", "Dge": "Dodge"}
SERIES_NAMES = {
    1: "NASCAR Cup Series",
    2: "NASCAR O'Reilly Auto Parts Series",
    3: "NASCAR Craftsman Truck Series",
}

# --- shared columns -------------------------------------------------------------


def _race_columns() -> list[pa.Field]:
    return [
        field("season", pa.int32(), "Season year.", nullable=False),
        field("series_id", pa.int32(), "NASCAR series: 1 Cup, 2 O'Reilly Auto Parts (Xfinity), 3 Truck."),
        field("race_id", pa.int32(), "Stable NASCAR race identifier.", nullable=False),
    ]


_DRIVER_ID = "Stable NASCAR driver identifier (joins every table and drivers())."
_FLAG_STATE = "NASCAR flag code: 1 green, 2 yellow, 3 red, 4 checkered, 8 pre-race, 9 none."

SCHEDULE_SCHEMA = pa.schema(
    [
        field("season", pa.int32(), "Season year.", nullable=False),
        field("series_id", pa.int32(), "NASCAR series: 1 Cup, 2 O'Reilly Auto Parts (Xfinity), 3 Truck."),
        field("series_name", pa.string(), "Full series name."),
        field("race_id", pa.int32(), "Stable NASCAR race identifier.", nullable=False),
        field("race_name", pa.string(), "Official race name, including the sponsor."),
        field("race_type_id", pa.int32(), "NASCAR race type: 1 points race, 2 non-points exhibition."),
        field("is_points_race", pa.bool_(), "Whether the race awards championship points."),
        field("playoff_round", pa.int32(), "Playoff round number; 0 for regular-season races."),
        field("track_id", pa.int32(), "NASCAR track identifier."),
        field("track_name", pa.string(), "Name of the speedway, road course or street circuit."),
        field(
            "race_date",
            TIMESTAMP_ET,
            "Race start as NASCAR publishes it, wall clock without offset: US Eastern (America/New_York) "
            "for most races, track-local time for some 2019-2023 races at Pacific and Mountain tracks.",
        ),
        field(
            "qualifying_date",
            TIMESTAMP_ET,
            "Qualifying time as published, wall clock without offset (same zone caveat as race_date).",
        ),
        field("scheduled_laps", pa.int32(), "Scheduled race distance in laps."),
        field("actual_laps", pa.int32(), "Laps actually run, including overtime; 0 before the race."),
        field("scheduled_distance", pa.float64(), "Scheduled race distance in miles."),
        field("actual_distance", pa.float64(), "Distance actually run in miles."),
        field("stage_1_laps", pa.int32(), "Length of stage 1 in laps; 0 when the race has no stages."),
        field("stage_2_laps", pa.int32(), "Length of stage 2 in laps."),
        field("stage_3_laps", pa.int32(), "Length of the final stage in laps."),
        field("cars_in_field", pa.int32(), "Number of starters."),
        field("pole_winner_driver_id", pa.int32(), "Driver identifier of the pole winner."),
        field("pole_winner_speed", pa.float64(), "Pole-winning qualifying speed in mph."),
        field(
            "winner_driver_id",
            pa.int32(),
            "Driver identifier of the race winner; NULL before the race and for 2015-2016 races.",
        ),
        field("winner_name", pa.string(), "Race winner's name, from the driver list or official results."),
        field("lead_changes", pa.int32(), "Number of lead changes."),
        field("leaders", pa.int32(), "Number of different leaders."),
        field("cautions", pa.int32(), "Number of caution periods."),
        field("caution_laps", pa.int32(), "Laps run under caution."),
        field("average_speed", pa.float64(), "Winner's average speed in mph."),
        field("total_race_time", pa.string(), "Race duration as published, H:MM:SS."),
        field("margin_of_victory", pa.string(), "Margin of victory as published, in seconds."),
        field("restrictor_plate", pa.bool_(), "Whether the race used superspeedway (drafting) rules."),
        field(
            "television_broadcaster",
            pa.string(),
            "Network carrying the race on US television, for example FOX or NBC.",
        ),
        field("radio_broadcaster", pa.string(), "Radio network carrying the race, for example MRN or PRN."),
        field("is_completed", pa.bool_(), "Whether the race has been run."),
    ]
)

RESULTS_SCHEMA = pa.schema(
    [
        *_race_columns(),
        field("race_name", pa.string(), "Official race name."),
        field("track_name", pa.string(), "Name of the speedway, road course or street circuit."),
        field(
            "race_date",
            TIMESTAMP_ET,
            "Race start as NASCAR publishes it, wall clock without offset: US Eastern (America/New_York) "
            "for most races, track-local time for some 2019-2023 races at Pacific and Mountain tracks.",
        ),
        field("finishing_position", pa.int32(), "Official finishing position (1 = winner).", nullable=False),
        field("starting_position", pa.int32(), "Grid position at the start (1 = pole)."),
        field("car_number", pa.string(), "Car number as displayed, for example 12 or 00."),
        field("driver_id", pa.int32(), _DRIVER_ID, nullable=False),
        field("driver_name", pa.string(), "Driver's full name."),
        field("team_id", pa.int32(), "NASCAR team identifier."),
        field("team_name", pa.string(), "Race team (organization) fielding the car."),
        field("owner_id", pa.int32(), "Car owner identifier."),
        field("owner_name", pa.string(), "Car owner name."),
        field("crew_chief_id", pa.int32(), "Crew chief identifier."),
        field("crew_chief_name", pa.string(), "Full name of the crew chief for this race."),
        field("manufacturer", pa.string(), "Car manufacturer, for example Chevrolet, Ford or Toyota."),
        field("car_model", pa.string(), "Car model, for example Camaro, Mustang or Camry."),
        field("sponsor", pa.string(), "Primary sponsor as published."),
        field("qualifying_position", pa.int32(), "Position in qualifying (1 = fastest)."),
        field("qualifying_speed", pa.float64(), "Qualifying speed in mph."),
        field("laps_completed", pa.int32(), "Number of laps completed before finishing or retiring."),
        field("laps_led", pa.int32(), "Number of laps completed in the lead."),
        field("times_led", pa.int32(), "Number of separate times the driver led."),
        field("laps_behind", pa.int32(), "Laps behind the winner at the finish."),
        field("time_behind_seconds", SECONDS, "Seconds behind the winner for cars on the lead lap."),
        field("finishing_status", pa.string(), "Status at the finish, for example Running or Accident."),
        field("points_earned", pa.int32(), "Championship points earned in this race, including stages."),
        field("playoff_points_earned", pa.int32(), "Playoff bonus points earned in this race."),
        field("points_position", pa.int32(), "Driver's championship position after this race."),
        field("disqualified", pa.bool_(), "Whether the entry was disqualified."),
        field("hometown_city", pa.string(), "Driver's hometown city."),
        field("hometown_state", pa.string(), "Driver's hometown state or province."),
        field("hometown_country", pa.string(), "Driver's hometown country."),
    ]
)

SESSION_SCHEMA = pa.schema(
    [
        *_race_columns(),
        field("run_id", pa.int32(), "NASCAR session identifier.", nullable=False),
        field("run_name", pa.string(), "Session name, for example Practice or Busch Pole Qualifying."),
        field("run_type", pa.string(), "Session kind: practice, qualifying or race."),
        field("run_date_utc", TIMESTAMP_UTC, "Session start time in UTC."),
        field("position", pa.int32(), "Classified position in the session.", nullable=False),
        field("car_number", pa.string(), "Car number as displayed."),
        field("driver_id", pa.int32(), _DRIVER_ID),
        field("driver_name", pa.string(), "Driver's full name."),
        field("manufacturer", pa.string(), "Car manufacturer."),
        field("best_lap_time_seconds", SECONDS, "Best lap time in seconds."),
        field("best_lap_speed", SPEED, "Best lap speed in mph."),
        field("best_lap_number", pa.int32(), "Lap on which the best time was set."),
        field("laps_completed", pa.int32(), "Laps completed in the session."),
        field("delta_to_leader_seconds", SECONDS, "Gap to the fastest car in seconds (negative = slower)."),
        field("disqualified", pa.bool_(), "Whether the time was disallowed."),
    ]
)

STAGE_SCHEMA = pa.schema(
    [
        *_race_columns(),
        field(
            "stage_number",
            pa.int32(),
            "Stage number, 1 or 2 (the final stage is the race result).",
            nullable=False,
        ),
        field("position", pa.int32(), "Position at the end of the stage.", nullable=False),
        field("driver_id", pa.int32(), _DRIVER_ID),
        field("driver_name", pa.string(), "Driver's full name."),
        field("car_number", pa.string(), "Car number as displayed."),
        field("stage_points", pa.int32(), "Championship points awarded for the stage finish."),
    ]
)

CAUTION_SCHEMA = pa.schema(
    [
        *_race_columns(),
        field("caution_number", pa.int32(), "Caution sequence number within the race.", nullable=False),
        field("start_lap", pa.int32(), "First lap run under this caution."),
        field("end_lap", pa.int32(), "Last lap run under this caution."),
        field("laps", pa.int32(), "Laps under this caution (inclusive of both ends)."),
        field("reason", pa.string(), "Caution category, for example Competition, Accident or Debris."),
        field("comment", pa.string(), "NASCAR's description of the caution."),
        field("free_pass_car_number", pa.string(), "Car number awarded the free pass (lucky dog), if any."),
        field("flag_state", pa.int32(), _FLAG_STATE),
    ]
)

LEADER_SCHEMA = pa.schema(
    [
        *_race_columns(),
        field("stint_number", pa.int32(), "Lead stint sequence number within the race.", nullable=False),
        field("start_lap", pa.int32(), "First lap led in this stint."),
        field("end_lap", pa.int32(), "Last lap led in this stint."),
        field("laps_led", pa.int32(), "Laps led in this stint (inclusive of both ends)."),
        field("car_number", pa.string(), "Car number of the leader."),
        field("driver_id", pa.int32(), "Leader's driver identifier, matched from the race results."),
        field("driver_name", pa.string(), "Leader's name, matched from the race results."),
    ]
)

LAP_TIMES_SCHEMA = pa.schema(
    [
        *_race_columns(),
        field("driver_id", pa.int32(), _DRIVER_ID, nullable=False),
        field("driver_name", pa.string(), "Driver's name with NASCAR's scoring markers removed."),
        field("car_number", pa.string(), "Car number as displayed."),
        field("manufacturer", pa.string(), "Car manufacturer."),
        field("lap", pa.int32(), "Lap number; lap 0 is the starting grid.", nullable=False),
        field("lap_time_seconds", SECONDS, "Lap time in seconds; NULL on lap 0."),
        field("lap_speed", SPEED, "Lap speed in mph; NULL on lap 0."),
        field("running_position", pa.int32(), "Running position at the end of the lap."),
        field("flag_state", pa.int32(), "Flag code at the end of the lap: " + _FLAG_STATE.split(": ", 1)[1]),
    ]
)

PIT_STOP_SCHEMA = pa.schema(
    [
        *_race_columns(),
        field("stop_number", pa.int32(), "Pit-road visit sequence number for this car.", nullable=False),
        field("car_number", pa.string(), "Car number as displayed."),
        field("driver_id", pa.int32(), "Driver identifier, matched from the race results by car number."),
        field("driver_name", pa.string(), "Driver's name."),
        field("manufacturer", pa.string(), "Car manufacturer."),
        field("lap", pa.int32(), "The car's lap count when it entered pit road."),
        field("leader_lap", pa.int32(), "The leader's lap when the car entered pit road."),
        field(
            "pit_in_flag_state", pa.int32(), "Flag code at pit entry: 1 green, 2 yellow, 3 red, 8 pre-race."
        ),
        field("pit_out_flag_state", pa.int32(), "Flag code at pit exit."),
        field("pit_in_race_time", pa.float64(), "Elapsed race time at pit entry, in seconds."),
        field("pit_out_race_time", pa.float64(), "Elapsed race time at pit exit, in seconds."),
        field("pit_road_seconds", SECONDS, "Time on pit road, entry to exit, in seconds; NULL when untimed."),
        field("stop_seconds", SECONDS, "Time stationary in the pit box, in seconds; NULL when untimed."),
        field("in_travel_seconds", SECONDS, "Pit entry to the pit box, in seconds; NULL when untimed."),
        field("out_travel_seconds", SECONDS, "Pit box to pit exit, in seconds; NULL when untimed."),
        field("stop_type", pa.string(), "Service type, for example FOUR_WHEEL_CHANGE or OTHER."),
        field("tires_changed", pa.int32(), "Number of tires changed, 0 to 4."),
        field("left_front_changed", pa.bool_(), "Whether the left-front tire was changed."),
        field("left_rear_changed", pa.bool_(), "Whether the left-rear tire was changed."),
        field("right_front_changed", pa.bool_(), "Whether the right-front tire was changed."),
        field("right_rear_changed", pa.bool_(), "Whether the right-rear tire was changed."),
        field("position_in", pa.int32(), "Running position at pit entry."),
        field("position_out", pa.int32(), "Running position at pit exit."),
        field("positions_gained", pa.int32(), "Positions gained (positive) or lost (negative) on pit road."),
    ]
)

LAP_NOTES_SCHEMA = pa.schema(
    [
        *_race_columns(),
        field(
            "lap", pa.int32(), "Lap the note refers to; lap 0 covers pre-race and qualifying.", nullable=False
        ),
        field("note_id", pa.int64(), "NASCAR note identifier.", nullable=False),
        field("flag_state", pa.int32(), _FLAG_STATE),
        field("note", pa.string(), "NASCAR's lap-by-lap race commentary."),
        field("driver_ids", pa.list_(pa.int32()), "Distinct driver identifiers mentioned in the note."),
    ]
)

LOOP_STATS_SCHEMA = pa.schema(
    [
        *_race_columns(),
        field("driver_id", pa.int32(), _DRIVER_ID, nullable=False),
        field("driver_name", pa.string(), "Driver's name, matched from the race results."),
        field("car_number", pa.string(), "Car number, matched from the race results."),
        field("start_position", pa.int32(), "Grid position at the start (1 = pole)."),
        field("mid_race_position", pa.int32(), "Position at the race's halfway point."),
        field("finish_position", pa.int32(), "Finishing position."),
        field("closing_position", pa.int32(), "Position at the start of the final 10% of the race."),
        field("closing_laps_diff", pa.int32(), "Positions gained over the closing laps."),
        field("best_position", pa.int32(), "Best running position at any lap."),
        field("worst_position", pa.int32(), "Worst running position at any lap."),
        field("average_position", pa.float64(), "Average running position across all laps."),
        field("green_flag_passes", pa.int32(), "Green-flag passes made."),
        field("green_flag_times_passed", pa.int32(), "Times passed by other cars under green."),
        field("passing_differential", pa.int32(), "Green-flag passes made minus times passed."),
        field("quality_passes", pa.int32(), "Green-flag passes of cars running in the top 15."),
        field("fastest_laps", pa.int32(), "Laps on which the driver was fastest in the field."),
        field("top_15_laps", pa.int32(), "Laps run inside the top 15."),
        field("laps_led", pa.int32(), "Number of laps completed in the lead."),
        field("laps", pa.int32(), "Number of laps completed before finishing or retiring."),
        field("driver_rating", pa.float64(), "NASCAR driver rating, a 0-150 loop-data composite."),
    ]
)

STANDINGS_SCHEMA = pa.schema(
    [
        field("season", pa.int32(), "Season the standings belong to.", nullable=False),
        field(
            "series_id",
            pa.int32(),
            "NASCAR series: 1 Cup, 2 O'Reilly Auto Parts (Xfinity), 3 Truck.",
            nullable=False,
        ),
        field("series_name", pa.string(), "Full series name."),
        field("position", pa.int32(), "Championship position (1 = points leader)."),
        field("driver_id", pa.int32(), _DRIVER_ID, nullable=False),
        field("driver_name", pa.string(), "Driver's full name."),
        field("first_name", pa.string(), "Driver's first name."),
        field("last_name", pa.string(), "Driver's last name."),
        field("car_number", pa.string(), "Car number as displayed."),
        field("manufacturer", pa.string(), "Car manufacturer."),
        field("points", pa.int32(), "Championship points total (points)."),
        field("points_behind_leader", pa.int32(), "Points behind the leader (0 for the leader)."),
        field("points_earned_last_race", pa.int32(), "Points earned in the most recent race."),
        field("previous_position", pa.int32(), "Championship position before the most recent race."),
        field("positions_gained", pa.int32(), "Positions gained (positive) or lost since the previous race."),
        field("playoff_eligible", pa.bool_(), "Whether NASCAR flags the driver as playoff-eligible."),
        field("playoff_rank", pa.int32(), "Playoff seeding rank; 0 when not applicable."),
        field("playoff_points", pa.int32(), "Playoff bonus points."),
        field("playoff_race_wins", pa.int32(), "Race wins counted toward the playoffs."),
        field("starts", pa.int32(), "Number of races started."),
        field("wins", pa.int32(), "Number of race wins."),
        field("poles", pa.int32(), "Number of pole positions."),
        field("top_5", pa.int32(), "Number of top-five finishes."),
        field("top_10", pa.int32(), "Number of top-ten finishes."),
        field("laps_led", pa.int32(), "Number of laps completed in the lead."),
        field("dnf", pa.int32(), "Number of races not finished (DNF)."),
        field("stage_points", pa.int32(), "Points earned from stage finishes."),
        field("stage_1_wins", pa.int32(), "Number of stage 1 wins."),
        field("stage_2_wins", pa.int32(), "Number of stage 2 wins."),
        field("stage_3_wins", pa.int32(), "Stage 3 wins (races with three stages before the finish)."),
        field("playoff_stage_wins", pa.int32(), "Stage wins counted toward the playoffs."),
    ]
)

DRIVERS_SCHEMA = pa.schema(
    [
        field("driver_id", pa.int32(), _DRIVER_ID, nullable=False),
        field("series", pa.string(), "NASCAR's series slug for the profile, for example nascar-cup-series."),
        field("full_name", pa.string(), "Driver's full name."),
        field("first_name", pa.string(), "Driver's first name."),
        field("last_name", pa.string(), "Driver's last name."),
        field("birth_date", pa.date32(), "Date of birth, when NASCAR publishes it."),
        field("hometown_city", pa.string(), "Hometown city."),
        field("hometown_state", pa.string(), "Hometown state or province."),
        field("hometown_country", pa.string(), "Hometown country."),
        field("team", pa.string(), "Current team as published on the driver profile."),
        field("crew_chief", pa.string(), "Current crew chief as published on the driver profile."),
        field("car_badge", pa.string(), "Car-number badge on the profile (some placeholders repeat the ID)."),
        field(
            "rookie_year",
            pa.int32(),
            "Rookie year as published on the profile (NASCAR repeats one value in all three series fields).",
        ),
        field("part_time", pa.bool_(), "Whether NASCAR marks the driver as part-time."),
        field("profile_published", pa.bool_(), "Whether the nascar.com driver profile is published."),
        field("twitter_handle", pa.string(), "Social handle as published."),
        field("profile_url", pa.string(), "Link to the driver's nascar.com page."),
        field("image_url", pa.string(), "Link to NASCAR's driver image (not copied or rehosted)."),
    ]
)

LIVE_SCHEMA = pa.schema(
    [
        field("race_id", pa.int32(), "Race identifier of the session on track.", nullable=False),
        field("series_id", pa.int32(), "NASCAR series of the session."),
        field("series_name", pa.string(), "Full series name."),
        field("run_id", pa.int32(), "Session identifier."),
        field("run_name", pa.string(), "Session name."),
        field("run_type", pa.string(), "Session kind: practice, qualifying or race."),
        field("track_id", pa.int32(), "Track identifier."),
        field("track_name", pa.string(), "Name of the speedway, road course or street circuit."),
        field("track_length", pa.float64(), "Track length in miles."),
        field("lap_number", pa.int32(), "Current lap."),
        field("laps_in_race", pa.int32(), "Scheduled laps in the session."),
        field("laps_to_go", pa.int32(), "Laps remaining."),
        field("flag_state", pa.int32(), _FLAG_STATE),
        field("flag", pa.string(), "Flag name for flag_state."),
        field("elapsed_seconds", pa.int32(), "Elapsed session time in seconds."),
        field("cautions", pa.int32(), "Caution periods so far."),
        field("caution_laps", pa.int32(), "Laps under caution so far."),
        field("lead_changes", pa.int32(), "Lead changes so far."),
        field("leaders", pa.int32(), "Different leaders so far."),
        field("stage_number", pa.int32(), "Current stage."),
        field("stage_end_lap", pa.int32(), "Lap on which the current stage ends."),
        field("cars_on_track", pa.int32(), "Number of cars in the live timing feed."),
        field("updated_at", TIMESTAMP_UTC, "Feed timestamp, converted to UTC."),
    ]
)

# --- coercion helpers -----------------------------------------------------------


def mapping(value: Any) -> Mapping[str, Any]:
    """Return ``value`` when it is a mapping, else an empty mapping."""
    return value if isinstance(value, Mapping) else {}


def to_int(value: Any) -> int | None:
    """Parse an integer-like value without treating booleans as integers."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        return int(value)
    except TypeError, ValueError, OverflowError:
        try:
            return int(float(value))
        except TypeError, ValueError, OverflowError:
            return None


def to_positive_int(value: Any) -> int | None:
    """Parse an identifier or year where 0 and negatives are NASCAR's 'none' markers."""
    result = to_int(value)
    return result if result is not None and result > 0 else None


def to_float(value: Any) -> float | None:
    """Parse a finite float."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        result = float(value)
    except TypeError, ValueError:
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


def to_decimal(value: Any, places: int = 3) -> Decimal | None:
    """Parse a finite decimal quantized to ``places`` digits."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation, ValueError:
        return None
    if not result.is_finite():
        return None
    return result.quantize(Decimal(1).scaleb(-places))


def to_duration(value: Any) -> Decimal | None:
    """Parse a non-negative duration in seconds; NASCAR's pit feed uses ``-1`` for "not timed"."""
    result = to_decimal(value)
    return result if result is not None and result >= 0 else None


def to_str(value: Any) -> str | None:
    """Return a stripped string, mapping blanks to NULL."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def to_bool(value: Any) -> bool | None:
    """Parse NASCAR's booleans, which arrive as bools, 0/1, or 'true'/'false'."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0"}:
        return value.strip().lower() in {"true", "1"}
    return None


def to_local_timestamp(value: Any) -> datetime | None:
    """Parse a zone-less NASCAR timestamp, keeping the published wall clock."""
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        return None
    if result.year <= 1:
        return None
    return result.replace(tzinfo=None) if result.tzinfo is None else result


def to_utc_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp; zone-less values are taken to be UTC already."""
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    # .NET emits seven fractional digits; Python accepts at most six.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        result = datetime.fromisoformat(text)
    except ValueError:
        return None
    if result.year <= 1:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def to_date(value: Any) -> date | None:
    """Parse a date or datetime string, treating .NET's year-1 sentinel as NULL."""
    stamp = to_local_timestamp(value)
    return stamp.date() if stamp is not None else None


def manufacturer(value: Any) -> str | None:
    """Expand NASCAR's three-letter manufacturer codes."""
    text = to_str(value)
    return MANUFACTURERS.get(text, text) if text else None


_MARKERS = re.compile(r"^\*\s*|\s*\([^)]*\)\s*$")


def clean_driver_name(value: Any) -> str | None:
    """Strip scoring markers such as a leading '*' or trailing '(i)'/'(C)' from a name."""
    text = to_str(value)
    if text is None:
        return None
    previous = None
    while previous != text:
        previous = text
        text = _MARKERS.sub("", text).strip()
    return text or None


def batch(rows: Sequence[Mapping[str, Any]], schema: pa.Schema) -> pa.RecordBatch:
    """Build a record batch in the declared (possibly projected) column order."""
    return pa.RecordBatch.from_arrays(
        [pa.array([row.get(column.name) for row in rows], type=column.type) for column in schema],
        schema=schema,
    )


def _dicts(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


# --- flatteners -----------------------------------------------------------------


def schedule_rows(
    races: Iterable[Mapping[str, Any]],
    names: Mapping[int, str],
    winners: Mapping[int, tuple[int, str | None]] | None = None,
) -> list[dict[str, Any]]:
    """One row per race from a season race list.

    ``winners`` maps race_id to (driver_id, name) taken from official results, for
    completed races whose race-list entry omits ``winner_driver_id``.
    """
    rows: list[dict[str, Any]] = []
    for race in races:
        series = to_int(race.get("series_id"))
        race_type = to_int(race.get("race_type_id"))
        winner = to_positive_int(race.get("winner_driver_id"))
        winner_name = names.get(winner) if winner else None
        fallback = (winners or {}).get(to_int(race.get("race_id")) or 0)
        if winner is None and fallback is not None:
            winner, winner_name = fallback
        actual_laps = to_int(race.get("actual_laps"))
        rows.append(
            {
                "season": to_int(race.get("race_season")),
                "series_id": series,
                "series_name": SERIES_NAMES.get(series or 0),
                "race_id": to_int(race.get("race_id")),
                "race_name": to_str(race.get("race_name")),
                "race_type_id": race_type,
                "is_points_race": None if race_type is None else race_type == 1,
                "playoff_round": to_int(race.get("playoff_round")),
                "track_id": to_int(race.get("track_id")),
                "track_name": to_str(race.get("track_name")),
                "race_date": to_local_timestamp(race.get("race_date")),
                "qualifying_date": to_local_timestamp(race.get("qualifying_date")),
                "scheduled_laps": to_int(race.get("scheduled_laps")),
                "actual_laps": actual_laps,
                "scheduled_distance": to_float(race.get("scheduled_distance")),
                "actual_distance": to_float(race.get("actual_distance")),
                "stage_1_laps": to_int(race.get("stage_1_laps")),
                "stage_2_laps": to_int(race.get("stage_2_laps")),
                "stage_3_laps": to_int(race.get("stage_3_laps")),
                "cars_in_field": to_int(race.get("number_of_cars_in_field")),
                "pole_winner_driver_id": to_positive_int(race.get("pole_winner_driver_id")),
                "pole_winner_speed": to_float(race.get("pole_winner_speed")),
                "winner_driver_id": winner,
                "winner_name": winner_name,
                "lead_changes": to_int(race.get("number_of_lead_changes")),
                "leaders": to_int(race.get("number_of_leaders")),
                "cautions": to_int(race.get("number_of_cautions")),
                "caution_laps": to_int(race.get("number_of_caution_laps")),
                "average_speed": to_float(race.get("average_speed")),
                "total_race_time": to_str(race.get("total_race_time")),
                "margin_of_victory": to_str(race.get("margin_of_victory")),
                "restrictor_plate": to_bool(race.get("restrictor_plate")),
                "television_broadcaster": to_str(race.get("television_broadcaster")),
                "radio_broadcaster": to_str(race.get("radio_broadcaster")),
                "is_completed": bool(winner) or bool(actual_laps),
            }
        )
    rows.sort(key=lambda row: (row["series_id"] or 0, row["race_date"] or datetime.max, row["race_id"] or 0))
    return rows


def weekend_race(payload: Any) -> Mapping[str, Any]:
    """Return the race object inside a weekend feed."""
    races = _dicts(mapping(payload).get("weekend_race"))
    return races[0] if races else {}


def _context(race: Mapping[str, Any], season: int, series: int, race_id: int) -> dict[str, Any]:
    return {"season": season, "series_id": series, "race_id": race_id}


def result_rows(payload: Any, season: int, series: int, race_id: int) -> list[dict[str, Any]]:
    """One row per entry from a weekend feed's official results."""
    race = weekend_race(payload)
    base = {
        **_context(race, season, series, race_id),
        "race_name": to_str(race.get("race_name")),
        "track_name": to_str(race.get("track_name")),
        "race_date": to_local_timestamp(race.get("race_date")),
    }
    rows = []
    for result in _dicts(race.get("results")):
        diff_time = to_int(result.get("diff_time"))
        rows.append(
            {
                **base,
                "finishing_position": to_int(result.get("finishing_position")),
                "starting_position": to_int(result.get("starting_position")),
                "car_number": to_str(result.get("car_number")),
                "driver_id": to_int(result.get("driver_id")),
                "driver_name": to_str(result.get("driver_fullname")),
                "team_id": to_positive_int(result.get("team_id")),
                "team_name": to_str(result.get("team_name")),
                "owner_id": to_positive_int(result.get("owner_id")),
                "owner_name": to_str(result.get("owner_fullname")),
                "crew_chief_id": to_positive_int(result.get("crew_chief_id")),
                "crew_chief_name": to_str(result.get("crew_chief_fullname")),
                "manufacturer": manufacturer(result.get("car_make")),
                "car_model": to_str(result.get("car_model")),
                "sponsor": to_str(result.get("sponsor")),
                "qualifying_position": to_positive_int(result.get("qualifying_position")),
                "qualifying_speed": to_float(result.get("qualifying_speed")),
                "laps_completed": to_int(result.get("laps_completed")),
                "laps_led": to_int(result.get("laps_led")),
                "times_led": to_int(result.get("times_led")),
                "laps_behind": to_int(result.get("diff_laps")),
                "time_behind_seconds": (
                    to_decimal(Decimal(diff_time) / 1000) if diff_time is not None else None
                ),
                "finishing_status": to_str(result.get("finishing_status")),
                "points_earned": to_int(result.get("points_earned")),
                "playoff_points_earned": to_int(result.get("playoff_points_earned")),
                "points_position": to_positive_int(result.get("points_position")),
                "disqualified": to_bool(result.get("disqualified")),
                "hometown_city": to_str(result.get("hometown_city")),
                "hometown_state": to_str(result.get("hometown_state")),
                "hometown_country": to_str(result.get("hometown_country")),
            }
        )
    rows = [row for row in rows if row["driver_id"] is not None and row["finishing_position"] is not None]
    rows.sort(key=lambda row: row["finishing_position"])
    return rows


def car_index(payload: Any) -> dict[str, tuple[int | None, str | None]]:
    """Map car number to (driver_id, driver_name) from a weekend feed's results."""
    return {
        str(result.get("car_number")): (
            to_int(result.get("driver_id")),
            to_str(result.get("driver_fullname")),
        )
        for result in _dicts(weekend_race(payload).get("results"))
    }


def driver_index(payload: Any) -> dict[int, tuple[str | None, str | None]]:
    """Map driver id to (driver_name, car_number) from a weekend feed's results."""
    index: dict[int, tuple[str | None, str | None]] = {}
    for result in _dicts(weekend_race(payload).get("results")):
        driver_id = to_int(result.get("driver_id"))
        if driver_id is not None:
            index[driver_id] = (to_str(result.get("driver_fullname")), to_str(result.get("car_number")))
    return index


def session_rows(payload: Any, season: int, series: int, race_id: int) -> list[dict[str, Any]]:
    """One row per car per practice or qualifying session in a weekend feed."""
    rows = []
    for run in _dicts(mapping(payload).get("weekend_runs")):
        run_type = to_int(run.get("run_type"))
        base = {
            "season": season,
            "series_id": series,
            "race_id": race_id,
            "run_id": to_int(run.get("weekend_run_id")),
            "run_name": to_str(run.get("run_name")),
            "run_type": RUN_TYPES.get(run_type or 0),
            "run_date_utc": to_utc_timestamp(run.get("run_date_utc")),
        }
        for result in _dicts(run.get("results")):
            rows.append(
                {
                    **base,
                    "position": to_int(result.get("finishing_position")),
                    "car_number": to_str(result.get("car_number")),
                    "driver_id": to_positive_int(result.get("driver_id")),
                    "driver_name": to_str(result.get("driver_name")),
                    "manufacturer": manufacturer(result.get("manufacturer")),
                    "best_lap_time_seconds": to_decimal(result.get("best_lap_time")),
                    "best_lap_speed": to_decimal(result.get("best_lap_speed")),
                    "best_lap_number": to_int(result.get("best_lap_number")),
                    "laps_completed": to_int(result.get("laps_completed")),
                    "delta_to_leader_seconds": to_decimal(result.get("delta_leader")),
                    "disqualified": to_bool(result.get("disqualified")),
                }
            )
    rows = [row for row in rows if row["run_id"] is not None and row["position"] is not None]
    rows.sort(key=lambda row: (row["run_date_utc"] or datetime.max.replace(tzinfo=UTC), row["position"]))
    return rows


def stage_rows(payload: Any, season: int, series: int, race_id: int) -> list[dict[str, Any]]:
    """One row per classified car per completed stage."""
    rows = []
    for stage in _dicts(weekend_race(payload).get("stage_results")):
        number = to_int(stage.get("stage_number"))
        for result in _dicts(stage.get("results")):
            rows.append(
                {
                    "season": season,
                    "series_id": series,
                    "race_id": race_id,
                    "stage_number": number,
                    "position": to_int(result.get("finishing_position")),
                    "driver_id": to_positive_int(result.get("driver_id")),
                    "driver_name": to_str(result.get("driver_fullname")),
                    "car_number": to_str(result.get("car_number")),
                    "stage_points": to_int(result.get("stage_points")),
                }
            )
    rows = [row for row in rows if row["stage_number"] is not None and row["position"] is not None]
    rows.sort(key=lambda row: (row["stage_number"], row["position"]))
    return rows


def caution_rows(payload: Any, season: int, series: int, race_id: int) -> list[dict[str, Any]]:
    """One row per caution period."""
    segments = sorted(
        _dicts(weekend_race(payload).get("caution_segments")),
        key=lambda value: to_int(value.get("start_lap")) or 0,
    )
    rows = []
    for number, segment in enumerate(segments, start=1):
        start, end = to_int(segment.get("start_lap")), to_int(segment.get("end_lap"))
        rows.append(
            {
                "season": season,
                "series_id": series,
                "race_id": race_id,
                "caution_number": number,
                "start_lap": start,
                "end_lap": end,
                "laps": end - start + 1 if start is not None and end is not None else None,
                "reason": to_str(segment.get("reason")),
                "comment": to_str(segment.get("comment")),
                "free_pass_car_number": to_str(segment.get("beneficiary_car_number")),
                "flag_state": to_int(segment.get("flag_state")),
            }
        )
    return rows


def leader_rows(payload: Any, season: int, series: int, race_id: int) -> list[dict[str, Any]]:
    """One row per lead stint, resolved to a driver through the race results."""
    cars = car_index(payload)
    stints = sorted(
        _dicts(weekend_race(payload).get("race_leaders")),
        key=lambda value: to_int(value.get("start_lap")) or 0,
    )
    rows = []
    for number, stint in enumerate(stints, start=1):
        start, end = to_int(stint.get("start_lap")), to_int(stint.get("end_lap"))
        car = to_str(stint.get("car_number"))
        driver_id, driver_name = cars.get(car or "", (None, None))
        rows.append(
            {
                "season": season,
                "series_id": series,
                "race_id": race_id,
                "stint_number": number,
                "start_lap": start,
                "end_lap": end,
                "laps_led": end - start + 1 if start is not None and end is not None else None,
                "car_number": car,
                "driver_id": driver_id,
                "driver_name": driver_name,
            }
        )
    return rows


def lap_time_rows(payload: Any, season: int, series: int, race_id: int) -> list[dict[str, Any]]:
    """One row per driver per lap from the lap-times feed."""
    body = mapping(payload)
    flags = {
        to_int(flag.get("LapsCompleted")): to_int(flag.get("FlagState")) for flag in _dicts(body.get("flags"))
    }
    rows = []
    for car in _dicts(body.get("laps")):
        driver_id = to_positive_int(car.get("NASCARDriverID"))
        if driver_id is None:
            continue
        base = {
            "season": season,
            "series_id": series,
            "race_id": race_id,
            "driver_id": driver_id,
            "driver_name": clean_driver_name(car.get("FullName")),
            "car_number": to_str(car.get("Number")),
            "manufacturer": manufacturer(car.get("Manufacturer")),
        }
        for lap in _dicts(car.get("Laps")):
            number = to_int(lap.get("Lap"))
            if number is None:
                continue
            rows.append(
                {
                    **base,
                    "lap": number,
                    "lap_time_seconds": to_decimal(lap.get("LapTime")),
                    "lap_speed": to_decimal(lap.get("LapSpeed")),
                    "running_position": to_int(lap.get("RunningPos")),
                    "flag_state": flags.get(number),
                }
            )
    rows.sort(key=lambda row: (row["lap"], row["running_position"] or 999))
    return rows


def pit_stop_rows(
    payload: Any, cars: Mapping[str, tuple[int | None, str | None]], season: int, series: int, race_id: int
) -> list[dict[str, Any]]:
    """One row per pit-road visit from the live pit data feed."""
    stops = sorted(
        _dicts(payload),
        key=lambda value: (to_float(value.get("pit_in_race_time")) or 0.0, str(value.get("vehicle_number"))),
    )
    counters: dict[str, int] = {}
    rows = []
    for stop in stops:
        car = to_str(stop.get("vehicle_number")) or ""
        counters[car] = counters.get(car, 0) + 1
        driver_id, matched_name = cars.get(car, (None, None))
        tires = [
            to_bool(stop.get(f"{corner}_tire_changed"))
            for corner in ("left_front", "left_rear", "right_front", "right_rear")
        ]
        rows.append(
            {
                "season": season,
                "series_id": series,
                "race_id": race_id,
                "stop_number": counters[car],
                "car_number": car or None,
                "driver_id": driver_id,
                "driver_name": matched_name or clean_driver_name(stop.get("driver_name")),
                "manufacturer": manufacturer(stop.get("vehicle_manufacturer")),
                "lap": to_int(stop.get("lap_count")),
                "leader_lap": to_int(stop.get("leader_lap")),
                "pit_in_flag_state": to_int(stop.get("pit_in_flag_status")),
                "pit_out_flag_state": to_int(stop.get("pit_out_flag_status")),
                "pit_in_race_time": to_float(stop.get("pit_in_race_time")),
                "pit_out_race_time": to_float(stop.get("pit_out_race_time")),
                "pit_road_seconds": to_duration(stop.get("total_duration")),
                "stop_seconds": to_duration(stop.get("pit_stop_duration")),
                "in_travel_seconds": to_duration(stop.get("in_travel_duration")),
                "out_travel_seconds": to_duration(stop.get("out_travel_duration")),
                "stop_type": to_str(stop.get("pit_stop_type")),
                "tires_changed": sum(1 for value in tires if value),
                "left_front_changed": tires[0],
                "left_rear_changed": tires[1],
                "right_front_changed": tires[2],
                "right_rear_changed": tires[3],
                "position_in": to_positive_int(stop.get("pit_in_rank")),
                "position_out": to_positive_int(stop.get("pit_out_rank")),
                "positions_gained": to_int(stop.get("positions_gained_lost")),
            }
        )
    return rows


def lap_note_rows(payload: Any, season: int, series: int, race_id: int) -> list[dict[str, Any]]:
    """One row per lap-by-lap commentary note."""
    laps = mapping(mapping(payload).get("laps"))
    rows = []
    for lap_key, notes in laps.items():
        lap = to_int(lap_key)
        if lap is None:
            continue
        for note in _dicts(notes):
            note_id = to_int(note.get("NoteID"))
            if note_id is None:
                continue
            ids: list[int] = []
            for value in note.get("DriverIDs") or []:
                driver_id = to_positive_int(value)
                if driver_id is not None and driver_id not in ids:
                    ids.append(driver_id)
            rows.append(
                {
                    "season": season,
                    "series_id": series,
                    "race_id": race_id,
                    "lap": lap,
                    "note_id": note_id,
                    "flag_state": to_int(note.get("FlagState")),
                    "note": to_str(note.get("Note")),
                    "driver_ids": ids,
                }
            )
    rows.sort(key=lambda row: (row["lap"], row["note_id"]))
    return rows


def loop_stat_rows(
    payload: Any, drivers: Mapping[int, tuple[str | None, str | None]], season: int, series: int, race_id: int
) -> list[dict[str, Any]]:
    """One row per driver from the loop-data statistics feed."""
    blocks = [block for block in _dicts(payload) if to_int(block.get("race_id")) in (race_id, None)]
    rows = []
    for block in blocks:
        for stat in _dicts(block.get("drivers")):
            driver_id = to_positive_int(stat.get("driver_id"))
            if driver_id is None:
                continue
            name, car = drivers.get(driver_id, (None, None))
            rows.append(
                {
                    "season": season,
                    "series_id": series,
                    "race_id": race_id,
                    "driver_id": driver_id,
                    "driver_name": name,
                    "car_number": car,
                    "start_position": to_int(stat.get("start_ps")),
                    "mid_race_position": to_int(stat.get("mid_ps")),
                    "finish_position": to_int(stat.get("ps")),
                    "closing_position": to_int(stat.get("closing_ps")),
                    "closing_laps_diff": to_int(stat.get("closing_laps_diff")),
                    "best_position": to_int(stat.get("best_ps")),
                    "worst_position": to_int(stat.get("worst_ps")),
                    "average_position": to_float(stat.get("avg_ps")),
                    "green_flag_passes": to_int(stat.get("passes_gf")),
                    "green_flag_times_passed": to_int(stat.get("passed_gf")),
                    "passing_differential": to_int(stat.get("passing_diff")),
                    "quality_passes": to_int(stat.get("quality_passes")),
                    "fastest_laps": to_int(stat.get("fast_laps")),
                    "top_15_laps": to_int(stat.get("top15_laps")),
                    "laps_led": to_int(stat.get("lead_laps")),
                    "laps": to_int(stat.get("laps")),
                    "driver_rating": to_float(stat.get("rating")),
                }
            )
    rows.sort(key=lambda row: (row["finish_position"] or 999, row["driver_id"]))
    return rows


def standings_rows(payload: Any, season: int, series: int) -> list[dict[str, Any]]:
    """One row per driver from a series points feed."""
    rows = []
    seen: set[int] = set()
    for entry in _dicts(payload):
        driver_id = to_positive_int(entry.get("driver_id"))
        if driver_id is None or driver_id in seen:
            continue
        seen.add(driver_id)
        delta = to_int(entry.get("delta_leader"))
        rows.append(
            {
                "season": season,
                "series_id": series,
                "series_name": SERIES_NAMES.get(series),
                "position": to_positive_int(entry.get("position")),
                "driver_id": driver_id,
                "driver_name": to_str(entry.get("driver_name")),
                "first_name": to_str(entry.get("driver_first_name")),
                "last_name": to_str(entry.get("driver_last_name")),
                "car_number": to_str(entry.get("car_no")),
                "manufacturer": manufacturer(entry.get("manufacturer")),
                "points": to_int(entry.get("points")),
                "points_behind_leader": abs(delta) if delta is not None else None,
                "points_earned_last_race": to_int(entry.get("points_earned")),
                "previous_position": to_positive_int(entry.get("pre_week_points_pos")),
                "positions_gained": to_int(entry.get("pos_gl")),
                "playoff_eligible": to_bool(entry.get("playoff_eligible")),
                "playoff_rank": to_int(entry.get("playoff_rank")),
                "playoff_points": to_int(entry.get("playoff_points")),
                "playoff_race_wins": to_int(entry.get("playoff_race_wins")),
                "starts": to_int(entry.get("starts")),
                "wins": to_int(entry.get("wins")),
                "poles": to_int(entry.get("poles")),
                "top_5": to_int(entry.get("top_5")),
                "top_10": to_int(entry.get("top_10")),
                "laps_led": to_int(entry.get("laps_led")),
                "dnf": to_int(entry.get("dnf")),
                "stage_points": to_int(entry.get("stage_points")),
                "stage_1_wins": to_int(entry.get("stage_1_wins")),
                "stage_2_wins": to_int(entry.get("stage_2_wins")),
                "stage_3_wins": to_int(entry.get("stage_3_wins")),
                "playoff_stage_wins": to_int(entry.get("playoff_stage_wins")),
            }
        )
    rows.sort(key=lambda row: (row["position"] or 9_999, row["driver_id"]))
    return rows


SERIES_SLUGS = {
    "cup": "nascar-cup-series",
    "oreilly": "nascar-oreilly-auto-parts-series",
    "xfinity": "nascar-oreilly-auto-parts-series",
    "truck": "nascar-craftsman-truck-series",
}


def driver_rows(payload: Any, series: str | None = None) -> list[dict[str, Any]]:
    """One row per (driver, series profile) from the driver master list."""
    wanted = SERIES_SLUGS[series] if series else None
    rows = []
    seen: set[tuple[int, str | None]] = set()
    for entry in _dicts(mapping(payload).get("response")):
        driver_id = to_positive_int(entry.get("Nascar_Driver_ID"))
        slug = to_str(entry.get("Driver_Series"))
        if driver_id is None or (driver_id, slug) in seen or (wanted and slug != wanted):
            continue
        seen.add((driver_id, slug))
        rows.append(
            {
                "driver_id": driver_id,
                "series": slug,
                "full_name": to_str(entry.get("Full_Name")),
                "first_name": to_str(entry.get("First_Name")),
                "last_name": to_str(entry.get("Last_Name")),
                "birth_date": to_date(entry.get("DOB")),
                "hometown_city": to_str(entry.get("Hometown_City")),
                "hometown_state": to_str(entry.get("Hometown_State")),
                "hometown_country": to_str(entry.get("Hometown_Country")),
                "team": to_str(entry.get("Team")),
                "crew_chief": to_str(entry.get("Crew_Chief")),
                "car_badge": to_str(entry.get("Badge")),
                # The three per-series rookie-year fields always hold the same value
                # upstream, so they are exposed as one column rather than three copies.
                "rookie_year": next(
                    (
                        year
                        for key in ("Rookie_Year_Series_1", "Rookie_Year_Series_2", "Rookie_Year_Series_3")
                        if (year := to_positive_int(entry.get(key))) is not None
                    ),
                    None,
                ),
                "part_time": to_bool(entry.get("Driver_Part_Time")),
                "profile_published": to_str(entry.get("Driver_Post_Status")) == "publish",
                "twitter_handle": to_str(entry.get("Twitter_Handle")),
                "profile_url": to_str(entry.get("Driver_Page")),
                "image_url": to_str(entry.get("Image")),
            }
        )
    rows.sort(key=lambda row: (row["driver_id"], row["series"] or ""))
    return rows


def driver_names(payload: Any) -> dict[int, str]:
    """Map driver id to full name from the driver master list."""
    names: dict[int, str] = {}
    for row in driver_rows(payload):
        if row["full_name"] and row["driver_id"] not in names:
            names[row["driver_id"]] = row["full_name"]
    return names


def live_rows(payload: Any) -> list[dict[str, Any]]:
    """Zero or one row describing the session currently on track."""
    body = mapping(payload)
    race_id = to_positive_int(body.get("race_id"))
    if race_id is None:
        return []
    series = to_positive_int(body.get("series_id"))
    stage = mapping(body.get("stage"))
    flag_state = to_int(body.get("flag_state"))
    vehicles = body.get("vehicles")
    return [
        {
            "race_id": race_id,
            "series_id": series,
            "series_name": SERIES_NAMES.get(series or 0),
            "run_id": to_positive_int(body.get("run_id")),
            "run_name": to_str(body.get("run_name")),
            "run_type": RUN_TYPES.get(to_int(body.get("run_type")) or 0),
            "track_id": to_positive_int(body.get("track_id")),
            "track_name": to_str(body.get("track_name")),
            "track_length": to_float(body.get("track_length")),
            "lap_number": to_int(body.get("lap_number")),
            "laps_in_race": to_int(body.get("laps_in_race")),
            "laps_to_go": to_int(body.get("laps_to_go")),
            "flag_state": flag_state,
            "flag": FLAG_NAMES.get(flag_state or 0),
            "elapsed_seconds": to_int(body.get("elapsed_time")),
            "cautions": to_int(body.get("number_of_caution_segments")),
            "caution_laps": to_int(body.get("number_of_caution_laps")),
            "lead_changes": to_int(body.get("number_of_lead_changes")),
            "leaders": to_int(body.get("number_of_leaders")),
            "stage_number": to_int(stage.get("stage_num")),
            "stage_end_lap": to_int(stage.get("finish_at_lap")),
            "cars_on_track": len(vehicles) if isinstance(vehicles, list) else None,
            "updated_at": to_utc_timestamp(body.get("time_of_day_os")),
        }
    ]
