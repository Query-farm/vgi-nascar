<p align="center">
  <a href="https://query.farm/vgi/">
    <img src="https://raw.githubusercontent.com/Query-farm/vgi-kalshi/main/docs/vgi-logo.png" alt="Vector Gateway Interface logo" width="320">
  </a>
</p>

<h1 align="center">vgi-nascar</h1>

<p align="center">
  NASCAR schedules, results, standings, lap times and pit stops as ordinary DuckDB tables.<br>
  A <strong>read-only</strong> <a href="https://query.farm/vgi/">VGI</a> worker, built by <a href="https://query.farm">🚜 Query.Farm</a>
</p>

<p align="center">
  <a href="https://github.com/Query-farm/vgi-nascar/actions/workflows/ci.yml"><img src="https://github.com/Query-farm/vgi-nascar/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/python-3.13%2B-3776AB.svg" alt="Python 3.13+">
  <a href="https://query.farm/vgi/"><img src="https://img.shields.io/badge/VGI-read--only-6f42c1.svg" alt="Read-only VGI worker"></a>
</p>

---

`vgi-nascar` fetches data on demand from the JSON feeds behind nascar.com
(`cf.nascar.com`) and returns it as typed Arrow data for Haybarn SQL. It covers
the three national series: the **NASCAR Cup Series**, the **NASCAR O'Reilly
Auto Parts Series** (formerly Xfinity) and the **NASCAR Craftsman Truck
Series**. It needs no credentials, and it does not rehost or persist a NASCAR
dataset.

## Quick start

The worker is not published to PyPI. `uvx` runs the tagged release straight from
GitHub and caches it after the first use:

```bash
uvx --from git+https://github.com/Query-farm/vgi-nascar@v0.2.3 vgi-nascar --help
```

Launch Haybarn with the catalog pre-attached:

```bash
uvx haybarn-cli \
  -cmd "INSTALL vgi FROM community; LOAD vgi; ATTACH 'nascar' (TYPE vgi, LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-nascar@v0.2.3 vgi-nascar');" \
  :memory:
```

Or attach it in an existing Haybarn session:

```sql
INSTALL vgi FROM community;
LOAD vgi;
ATTACH 'nascar' (
  TYPE vgi,
  LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-nascar@v0.2.3 vgi-nascar'
);
```

For source development, run `uv lock --check --no-sources`, then
`uv sync --no-sources` and `uv run --no-sources vgi-nascar`. The lockfile
resolves every dependency from package registries and does not require a
sibling checkout.

## Catalog surface

The catalog has four tables and twelve parameterized table functions:

| Kind | Objects |
| --- | --- |
| Current season | `current_standings`, `current_schedule`, `last_race_results`, `live_race` |
| Seasons | `schedule()`, `season_results()` |
| One race | `race_results()`, `session_results()`, `stage_results()`, `race_cautions()`, `race_leaders()` |
| Lap and pit data | `lap_times()`, `pit_stops()`, `lap_notes()`, `loop_stats()` |
| Drivers | `drivers()` |

`series_id` is `1` for Cup, `2` for O'Reilly Auto Parts (Xfinity) and `3` for
Truck. Functions that take a `series` argument accept `cup`, `oreilly` (alias
`xfinity`) or `truck`. Races are identified by `race_id` and drivers by
`driver_id`. Both IDs are stable and join across every object.

| Data | Available from |
| --- | --- |
| Schedules and race summaries | 2015 (NASCAR's 2017 race list holds 2018 races, so 2017 returns no rows) |
| Official results, stages, cautions, lead stints, sessions | 2018 |
| Loop-data statistics | 2019 |
| Lap times, pit stops, lap notes | 2020 |
| Points standings | current season only (see below) |

When an upstream file does not exist, NASCAR's S3 bucket answers
`403 AccessDenied`. The worker treats that as "no data" and returns an empty
result instead of an error.

## Usage

### Standings and schedule

```sql
-- Current Cup Series top ten
SELECT position, driver_name, points, points_behind_leader, wins, top_5
FROM nascar.main.current_standings
WHERE series_id = 1
ORDER BY position
LIMIT 10;

-- This season's Cup winners
SELECT race_date, race_name, track_name, winner_name
FROM nascar.main.current_schedule
WHERE series_id = 1 AND is_completed
ORDER BY race_date;

-- A past season's points races
SELECT race_date, race_name, track_name, winner_name
FROM nascar.main.schedule(season := 2025, series := 'cup')
WHERE is_points_race
ORDER BY race_date;
```

`race_date` is the start time NASCAR publishes, as a wall-clock time with no
offset stored. It is US Eastern (America/New_York) for most races, but NASCAR
published track-local time for some 2019-2023 races at Pacific and Mountain
tracks (for example, the 2019 Las Vegas spring race reads 12:30 rather than
15:30). NASCAR's race lists also omit the winner for 2015-2019 and some 2022
races. From 2018 the worker fills it in from the official results, so
`schedule()` for those seasons fetches each race's results file once. For
2015-2016 the winner stays NULL.

### Results

```sql
-- Latest finishing order in each series
SELECT series_id, race_name, finishing_position, driver_name, team_name, laps_led
FROM nascar.main.last_race_results
WHERE finishing_position <= 5
ORDER BY series_id, finishing_position;

-- Any race by ID (passing season skips the lookup)
SELECT finishing_position, car_number, driver_name, manufacturer,
       laps_led, time_behind_seconds, finishing_status, points_earned
FROM nascar.main.race_results(5601, season := 2026)
ORDER BY finishing_position;

-- Qualifying, and the stage winners
SELECT position, driver_name, best_lap_time_seconds, best_lap_speed
FROM nascar.main.session_results(5601, season := 2026)
WHERE run_type = 'qualifying'
ORDER BY position;

SELECT stage_number, driver_name, stage_points
FROM nascar.main.stage_results(5601, season := 2026)
WHERE position = 1;
```

### Season and team statistics

`season_results()` returns every completed race of one series and season, one
row per car. Aggregate it for driver, team, owner or manufacturer statistics:

```sql
SELECT r.driver_name,
       count(*) FILTER (WHERE r.finishing_position = 1)  AS wins,
       count(*) FILTER (WHERE r.finishing_position <= 5) AS top_5,
       sum(r.laps_led)                                   AS laps_led,
       round(avg(r.finishing_position), 1)               AS avg_finish
FROM nascar.main.season_results(season := 2025, series := 'cup') r
JOIN nascar.main.schedule(season := 2025, series := 'cup') USING (race_id)
WHERE is_points_race
GROUP BY r.driver_name
ORDER BY wins DESC, top_5 DESC;
```

The first query for a season fetches about 38 race files, four at a time.
Later queries read them from the in-process cache.

### Inside one race

```sql
-- Lead stints add up to the race distance
SELECT driver_name, sum(laps_led) AS laps_led, count(*) AS stints
FROM nascar.main.race_leaders(5601, season := 2026)
GROUP BY driver_name
ORDER BY laps_led DESC;

-- Cautions, with the number of commentary notes posted during each one
SELECT c.caution_number, c.start_lap, c.end_lap, c.reason, count(n.note_id) AS notes
FROM nascar.main.race_cautions(5601, season := 2026) c
LEFT JOIN nascar.main.lap_notes(5601, season := 2026) n
  ON n.lap BETWEEN c.start_lap AND c.end_lap
GROUP BY ALL
ORDER BY c.caution_number;

-- Green-flag pace: median lap time per driver
SELECT driver_name, median(lap_time_seconds) AS median_green_lap
FROM nascar.main.lap_times(5601, season := 2026)
WHERE flag_state = 1 AND lap > 0
GROUP BY driver_name
ORDER BY median_green_lap
LIMIT 10;

-- Top ten with the green- and yellow-flag pit stops each made and their time on pit road
SELECT r.finishing_position, r.driver_name, count(*) AS stops,
       sum(p.pit_road_seconds) AS pit_road_seconds
FROM nascar.main.race_results(5601, season := 2026) r
JOIN nascar.main.pit_stops(5601, season := 2026) p USING (driver_id)
WHERE r.finishing_position <= 10 AND p.pit_in_flag_state IN (1, 2)
GROUP BY r.finishing_position, r.driver_name
ORDER BY r.finishing_position;

-- Fastest four-tire stops
SELECT driver_name, lap, stop_seconds, pit_road_seconds, positions_gained
FROM nascar.main.pit_stops(5601, season := 2026)
WHERE stop_type = 'FOUR_WHEEL_CHANGE'
ORDER BY stop_seconds
LIMIT 10;

-- Loop data: driver rating and passing
SELECT driver_name, driver_rating, green_flag_passes, quality_passes, average_position
FROM nascar.main.loop_stats(5601, season := 2026)
ORDER BY driver_rating DESC;
```

Lap times, speeds and pit durations are exact decimals. Flag codes are
`1` green, `2` yellow, `3` red, `4` checkered, `8` pre-race and `9` none.

### Drivers and live timing

```sql
SELECT full_name, team, hometown_city, hometown_state, birth_date
FROM nascar.main.drivers(series := 'cup')
WHERE profile_published
ORDER BY last_name;

SELECT series_name, run_name, track_name, lap_number, laps_to_go, flag
FROM nascar.main.live_race;   -- no rows when nothing is on track
```

## Data source, licence and affiliation

**This project is not affiliated with, endorsed by, or sponsored by NASCAR**
(the National Association for Stock Car Auto Racing, LLC) or any team, track
or sponsor. "NASCAR" and the series names are trademarks of their owners and
are used here only to describe the data.

- **Source.** The data comes from NASCAR's own first-party JSON feeds at
  `cf.nascar.com`, the files that power nascar.com. They are public and need no
  key, but they are **undocumented**, carry **no published licence**, and can
  change or disappear without notice.
- **Terms of use.** NASCAR's website terms page refuses non-browser clients
  (HTTP 403), so this project could not review it. Before using this data
  commercially or redistributing query results, read nascar.com's terms of use
  yourself and get whatever permission they require. The data remains the property of NASCAR
  and its licensors.
- **No rehosting.** The worker only fetches feeds when you run a query and keeps
  them in a short-lived in-process cache. It does not mirror, persist or
  redistribute NASCAR data. Image and profile columns are links to nascar.com,
  not copies.
- **Be a polite client.** Requests send an honest
  `User-Agent: vgi-nascar/<version> (+https://github.com/Query-farm/vgi-nascar)`.
  At most four requests run at once, and responses are cached (see
  *Reliability*).
- **Known upstream quirks.** The season segment of the points-feed path does
  not reliably match the contents. For example, the `2024` file holds the 2025
  season, and the `2025` file duplicates 2026. For that reason the worker
  exposes only *current* standings; for past seasons, aggregate
  `season_results()`. The `2017` race list contains 2018 races, and
  the worker filters those out.

The worker's source code is MIT-licensed. That licence covers this code only,
not the data.

## Production HTTP deployment

Stdio is the recommended transport when Haybarn can launch the worker locally.
Use HTTP when the worker must be shared across hosts. The production HTTP entry
point refuses to start unless it has a stable signing key and either
authentication or an explicit decision to allow anonymous access:

```bash
export VGI_SIGNING_KEY='a-long-random-secret-shared-by-every-replica'
export VGI_BEARER_TOKENS='replace-with-a-random-token=haybarn'

uvx --from git+https://github.com/Query-farm/vgi-nascar@v0.2.3 vgi-nascar-http \
  --host 0.0.0.0 \
  --port 8000 \
  --cors-origins 'https://sql.example.com' \
  --log-format json
```

`vgi-nascar-http` disables the description pages and cross-origin access
unless you configure them explicitly. Terminate TLS at a trusted proxy or load
balancer. Every replica must receive the same `VGI_SIGNING_KEY`. Outstanding
state tokens are signed with it, so rotate it in a coordinated deployment.

Supported authentication options are:

- `VGI_BEARER_TOKENS`: comma-separated `token=principal` pairs.
- `VGI_JWT_ISSUER` and `VGI_JWT_AUDIENCE`: JWT/JWKS authentication.
- `VGI_PROXY_PROOF_MODE=require` with the corresponding VGI proxy-proof
  settings when a trusted proxy authenticates users.

`VGI_ALLOW_ANONYMOUS=1` permits an intentionally public service. Because every
query reaches NASCAR's CDN, protect such a service with gateway rate limits.
The health probe is `GET /health`.

### Container

```bash
docker build -t vgi-nascar .
docker run --rm -p 8000:8000 \
  -e VGI_SIGNING_KEY='replace-with-a-stable-random-secret' \
  -e VGI_BEARER_TOKENS='replace-with-a-random-token=haybarn' \
  vgi-nascar
```

The image runs as an unprivileged user, emits JSON logs, disables interactive
description pages, and includes a health check.

## Reliability and operations

The origin sends no `Cache-Control` header, so the worker applies its own
freshness policy. It caches decoded feeds in-process (bounded LRU) and gives
VGI clients the same lifetime, with a 24-hour `stale-if-error` window:

| Feed | Lifetime |
| --- | --- |
| Live timing | 15 seconds |
| Current-season race lists and race files | 5 minutes |
| Points standings | 10 minutes |
| Driver list (about 1.4 MB) | 6 hours |
| Past seasons | 24 hours |

Each upstream request has a 30-second timeout, and each query has a 120-second
total deadline. A request gets at most four attempts, and the process sends at
most four requests at once. HTTP 429 and transient 5xx responses are retried
with exponential backoff and jitter. `Retry-After` is honored, capped at 30
seconds per retry. Redirects are followed. Missing files (`403`/`404`) are
cached as empty, so absent history is not probed again. Successful requests
are logged at debug level; retries and transport failures are logged as
warnings.

`NASCAR_BASE_URL` (default `https://cf.nascar.com`) can point the worker at an
alternate root for hermetic tests. Do not set it from untrusted input.

## Development and release checks

```bash
uv lock --check --no-sources
uv sync --no-sources
uv run --no-sources ruff check .
uv run --no-sources ruff format --check .
uv run --no-sources pytest --cov=vgi_nascar --cov-fail-under=80
uv build
uvx twine check dist/*
uvx --no-sources --from vgi-lint-check==2.2.0 vgi-lint lint \
  'uv run --no-sources nascar_worker.py' \
  --no-execute --no-check-links --audit-waivers --fail-on warning
```

The offline suite uses trimmed real responses in `tests/fixtures/`. It includes
a hermetic Haybarn end-to-end test that serves those fixtures from a local HTTP
server and queries every table. Live upstream smoke tests are opt-in with
`uv run pytest -m live`. They are pinned to the 2026 Cup Series Championship
Race (race 5601) and the 2025 Cup season. A daily serialized workflow runs them
together with the executing `vgi-lint` pass.

Publishing is driven by releases. Create a GitHub release named `vX.Y.Z` that
matches the version in `pyproject.toml`. CI then builds, validates and attests
the wheel and sdist, attaches them to the release, and publishes a
multi-platform container image to `ghcr.io/query-farm/vgi-nascar`. The package is
not published to PyPI; install it from the release tag as shown in the quick
start.

---

Copyright 2026 Query Farm LLC - https://query.farm

## Executed notebook

[Separate pit-box service time from total pit-road time](notebooks/README.md).
