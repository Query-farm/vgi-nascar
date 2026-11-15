# Changelog

All notable changes to this project are documented here.

## [0.2.1]

- The `live_race` / `all_live_race` example descriptions say the query returns
  0 with NULL lap and flag when no session is on track, the usual answer
  between sessions (vgi-lint VGI913).
- `pit_stops()` durations (`pit_road_seconds`, `stop_seconds`, `in_travel_seconds`,
  `out_travel_seconds`) are NULL when NASCAR did not time the visit. The feed's
  `-1` placeholder (post-checkered and some pre-race visits) previously came
  through as `-1.000` and skewed sums and averages.
- Composed examples that combine the catalog's objects (latest race with the
  current standings; results with pit stops, stage results and lap-by-lap
  commentary) and two composed agent tasks (`stage_fastest_laps`,
  `caution_pit_stops`).

## [0.2.0]

- Port to VGI protocol 3.3 (vgi-python 0.51). Requires the current VGI DuckDB
  extension; 3.2 extensions are incompatible. No SQL surface changes.

## [0.1.0]

- Initial release on VGI protocol 3.2 (vgi-python 0.50) with vgi-lint 2.2.0.
- Tables: `current_standings`, `current_schedule`, `last_race_results`, `live_race`.
- Functions: `schedule()`, `season_results()`, `race_results()`, `session_results()`,
  `stage_results()`, `race_cautions()`, `race_leaders()`, `lap_times()`, `pit_stops()`,
  `lap_notes()`, `loop_stats()` and `drivers()` for the Cup, O'Reilly Auto Parts
  (Xfinity) and Craftsman Truck Series.
- Caching, rate-limited HTTP client with retries, `Retry-After` support, and S3
  `403 AccessDenied` mapped to empty results.
- Hermetic Haybarn end-to-end test, live smoke tests, container image and
  release workflows.
