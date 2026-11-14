# NASCAR pit-timing notebook

[01 · Time in the pit box is only part of time on pit road](01_pit_box_time_is_not_pit_road_time.ipynb) preserves untimed visits, checks a duration-component identity, and compares complete four-wheel changes by entry flag state. It studies the 2026 Cup Series Championship Race at Homestead-Miami Speedway, race ID 5601. It is not a crew ranking or causal time-savings estimate.

## Run from this directory

Requires Python 3.13–3.14, [uv](https://docs.astral.sh/uv/), Git, and internet for initial packages, worker checkout and VGI extension. Replay reads committed response bodies through a loopback server; no NASCAR requests are made in replay mode.

```sh
cd notebooks
uv sync --locked
uv run --locked python execute.py
uv run --locked python -m ipykernel install --user --name vgi-nascar-notebooks --display-name 'Python (vgi-nascar notebooks)'
```

Select the kernel, then Restart Kernel → Run All. The runner uses a fresh temporary kernel, sets cwd to this directory and needs no global kernel installation. Copying the entire notebooks directory is sufficient; the worker checkout is not discovered or used.

The visible literal `uvx --from git+https://github.com/Query-farm/vgi-nascar@11c952ed289db479f5ee81cdd6c5ebc14d0fc3c5 ... vgi-nascar` pins an immutable remote source revision. This revision contains the existing negative-duration-to-NULL fix absent from tag v0.2.0; no worker code is modified by this notebook. Explicit runtime pins are SDK 0.51.0, RPC 0.53.1, httpx 0.28.1, httpx2 2.16.0 and Arrow 25.0.1. Notebook Haybarn is locked to 2.0.0rc11. The worker is not published on PyPI. Revalidate default, experiment and live modes before changing any pin.

## Experiment and live check

```sh
uv run --locked python execute.py --parameters '{"ENTRY_FLAG_STATE":2}' --output-dir validation/yellow-entry
uv run --locked python execute.py --parameters '{"DATA_MODE":"live"}' --output-dir validation/live
```

Defaults: `DATA_MODE="replay"`, `ENTRY_FLAG_STATE=1` (green). The experiment changes only the entry state to yellow. Both modes request the same season and race, not current live timing. Live has no recorded-data fallback and reports a retrieval interval; HTTP retries may occur and are not individually instrumented. Inherited `NASCAR_BASE_URL` is overridden in both modes and restored at cleanup.

Overrides require a separate output destination. Unknown names are rejected. The previous successful default is preserved on execution failure; successful outputs are saved atomically. Experiment copies include fixtures, helper, branding, documentation, runner and environment. Validation copies, charts, runtime audits and environments are ignored; the main notebook retains embedded outputs.

## Data provenance and bounded retrieval

`fixtures/manifest.json` records exact public URLs, status, byte counts, SHA-256 and per-response UTC capture times. The three files are complete, unmodified responses fetched from NASCAR's first-party `https://cf.nascar.com` feeds on 2026-11-14:

- `race-list.json`: `/cacher/2026/race_list_basic.json`, 269,994 bytes.
- `weekend.json`: `/cacher/2026/1/5601/weekend-feed.json`, 66,219 bytes.
- `pit-stops.json`: `/cacher/2026/1/5601/live-pit-data.json`, 182,513 bytes, 285 visits.

The replay helper verifies all hashes and serves only exact path/query matches. Unknown paths return an error, never a live fetch or synthetic replacement. The real worker resolves driver IDs and maps raw duration sentinels; the notebook checks raw visit and sentinel counts. One fixed season list resolves this race, and only this race's weekend/pit files are fetched. No season-results fanout or SQL LIMIT is used. Later filtering is local; charts never refetch. Responses were captured separately and are not an atomic snapshot. Capture time is distinct from execution time and from the race's wall-clock schedule; no UTC race time is inferred.

NASCAR's feeds are undocumented and can change or disappear. The root README describes their provenance and licensing limitations; worker code licensing does not grant rights in the underlying data. These public response fixtures are a small reproducibility example, not a data mirror. This project is not affiliated with or endorsed by NASCAR.

## Analytical limits

The cohort requires the service label FOUR_WHEEL_CHANGE, four reported tires changed, green/yellow entry, and all four durations. It preserves zero durations if present and reports excluded missing values. Raw negative durations mean untimed and become SQL NULL in the pinned source. Required empty results fail clearly because upstream 403/404 can mean unavailable data.

The component identity permits 0.003 seconds of combined millisecond rounding. Non-box duration is calculated per visit; separate medians need not add. Flag state is measured at entry and can change before exit. Dots represent visits, so drivers can recur. Timing includes travel and context but does not measure the counterfactual time lost by leaving the racing surface, isolate crew performance, or establish a finishing-position effect. Scatter axes are shared across both flag cohorts. Long visits remain included and are shown explicitly in a table. Both charts use symmetric logarithmic scales with a linear 0–1 second segment, so zero remains representable and long visits do not hide the main distribution.

[Notebook standard](STANDARD.md) · [Query.Farm](https://query.farm/) · [VGI](https://query.farm/vgi/)
