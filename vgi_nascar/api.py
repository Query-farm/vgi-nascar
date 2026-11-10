"""Small, read-only, caching client for NASCAR's public cf.nascar.com JSON feeds.

These are the first-party (but undocumented) static files that power nascar.com.
They live in S3 behind CloudFront: a missing file answers ``403 AccessDenied``
rather than ``404``, so both statuses mean "this feed does not exist" and map to
an empty result instead of an error.

The origin sends no ``Cache-Control`` header. The connector therefore applies its
own conservative freshness policy (see :data:`TTL`), caches decoded payloads
in-process so repeated SQL queries do not refetch the same file, and forwards the
same lifetime to VGI clients.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from vgi_nascar import __version__

DEFAULT_BASE_URL = "https://cf.nascar.com"
USER_AGENT = f"vgi-nascar/{__version__} (+https://github.com/Query-farm/vgi-nascar)"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
MISSING_STATUSES = frozenset({403, 404})
MAX_ATTEMPTS = 4
MAX_RETRY_DELAY = 30.0
QUERY_TIMEOUT = 120.0
MAX_CONCURRENT_REQUESTS = 4
CACHE_MAX_ENTRIES = 512
FIRST_SEASON = 2015

SERIES_IDS = {"cup": 1, "oreilly": 2, "xfinity": 2, "truck": 3}


@dataclass(frozen=True, slots=True)
class Ttl:
    """Freshness lifetimes, in seconds, applied to each feed family."""

    live: int = 15
    current: int = 300
    standings: int = 600
    drivers: int = 6 * 3_600
    historical: int = 24 * 3_600
    stale_if_error: int = 24 * 3_600


TTL = Ttl()

logger = logging.getLogger(__name__)
_request_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)


class NascarApiError(RuntimeError):
    """A failed response from the NASCAR feeds."""

    def __init__(self, status: int, path: str, body: str) -> None:
        super().__init__(f"NASCAR feed {path} returned HTTP {status}: {body[:300]}")
        self.status = status


@dataclass(frozen=True, slots=True)
class Feed:
    """A decoded feed plus the freshness lifetime the connector applied to it."""

    payload: Any
    ttl: int
    found: bool = True


class _Cache:
    """Thread-safe bounded LRU cache of decoded feeds with per-entry expiry."""

    def __init__(self, max_entries: int = CACHE_MAX_ENTRIES) -> None:
        self._entries: OrderedDict[str, tuple[float, Feed]] = OrderedDict()
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def get(self, key: str) -> Feed | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires, feed = entry
            if expires <= time.monotonic():
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return feed

    def store(self, key: str, feed: Feed) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic() + feed.ttl, feed)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_cache = _Cache()


def clear_cache() -> None:
    """Drop every cached feed (used by tests)."""
    _cache.clear()


def base_url() -> str:
    """Return the CDN root, optionally overridden for hermetic testing."""
    return os.environ.get("NASCAR_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def current_year() -> int:
    """Return the current calendar year in UTC."""
    return datetime.now(UTC).year


def series_id(series: str) -> int:
    """Map a series name to NASCAR's numeric series identifier."""
    try:
        return SERIES_IDS[series.lower()]
    except KeyError:
        raise ValueError(f"unknown series {series!r}; use cup, oreilly (xfinity), or truck") from None


def season_ttl(season: int) -> int:
    """Past seasons are immutable; the current season changes during race weekends."""
    return TTL.historical if season < current_year() else TTL.current


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Return a bounded Retry-After delay or exponential backoff with jitter."""
    if response is not None:
        raw = response.headers.get("retry-after")
        if raw:
            try:
                delay = float(raw)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(raw)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=UTC)
                    delay = (retry_at - datetime.now(UTC)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    delay = -1.0
            if delay >= 0:
                return min(delay, MAX_RETRY_DELAY)
    base = min(0.25 * 2**attempt, MAX_RETRY_DELAY)
    return min(base + random.uniform(0, base * 0.25), MAX_RETRY_DELAY)


_shared_client: httpx.Client | None = None
_shared_client_lock = threading.Lock()


def _client() -> httpx.Client:
    """Return one process-wide connection pool (created lazily)."""
    global _shared_client
    with _shared_client_lock:
        if _shared_client is None:
            _shared_client = httpx.Client(
                timeout=TIMEOUT,
                follow_redirects=True,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            )
        return _shared_client


def _request(path: str, *, client: httpx.Client, deadline: float) -> httpx.Response:
    """Issue the connector's only outbound request, retrying transient failures."""
    url = f"{base_url()}/{path.lstrip('/')}"
    for attempt in range(MAX_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"NASCAR feed query exceeded {QUERY_TIMEOUT:g} seconds")
        started = time.monotonic()
        try:
            if not _request_slots.acquire(timeout=remaining):
                raise TimeoutError(f"NASCAR feed query exceeded {QUERY_TIMEOUT:g} seconds")
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"NASCAR feed query exceeded {QUERY_TIMEOUT:g} seconds")
                response = client.get(
                    url,
                    headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                    timeout=httpx.Timeout(min(30.0, remaining), connect=min(10.0, remaining)),
                )
            finally:
                _request_slots.release()
        except httpx.TransportError:
            logger.warning(
                "nascar_feed_transport_error path=%s attempt=%d duration_ms=%d",
                path,
                attempt + 1,
                round((time.monotonic() - started) * 1_000),
            )
            if attempt + 1 == MAX_ATTEMPTS:
                raise
            time.sleep(min(_retry_delay(None, attempt), max(0.0, deadline - time.monotonic())))
            continue
        logger.debug(
            "nascar_feed_response path=%s status=%d attempt=%d duration_ms=%d",
            path,
            response.status_code,
            attempt + 1,
            round((time.monotonic() - started) * 1_000),
        )
        if response.status_code not in RETRYABLE_STATUSES or attempt + 1 == MAX_ATTEMPTS:
            return response
        delay = min(_retry_delay(response, attempt), max(0.0, deadline - time.monotonic()))
        logger.warning(
            "nascar_feed_retry path=%s status=%d attempt=%d delay_seconds=%.3f",
            path,
            response.status_code,
            attempt + 1,
            delay,
        )
        time.sleep(delay)
    raise AssertionError("retry loop exited without a response")


def fetch_json(path: str, *, ttl: int, client: httpx.Client | None = None) -> Feed:
    """Fetch and decode one feed, serving it from the in-process cache when fresh.

    Missing feeds (S3 ``403``/``404``) are returned with ``found=False`` and a
    ``None`` payload, and are cached too so absent history is not re-probed.
    """
    key = f"{base_url()}/{path.lstrip('/')}"
    cached = _cache.get(key)
    if cached is not None:
        return cached
    deadline = time.monotonic() + QUERY_TIMEOUT
    response = _request(path, client=client or _client(), deadline=deadline)
    if response.status_code in MISSING_STATUSES:
        feed = Feed(payload=None, ttl=ttl, found=False)
    elif not response.is_success:
        raise NascarApiError(response.status_code, path, response.text)
    else:
        try:
            payload = response.json()
        except ValueError as error:
            raise ValueError(f"NASCAR feed {path} did not return JSON") from error
        feed = Feed(payload=payload, ttl=ttl, found=payload is not None)
    _cache.store(key, feed)
    return feed


def fetch_many(paths: list[str], *, ttl: int) -> list[Feed]:
    """Fetch several feeds with bounded concurrency, preserving order."""
    if len(paths) <= 1:
        return [fetch_json(path, ttl=ttl) for path in paths]
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_REQUESTS) as pool:
        return list(pool.map(lambda path: fetch_json(path, ttl=ttl), paths))


# --- feed paths -----------------------------------------------------------------


def race_list(season: int) -> Feed:
    """Season race list for all three national series (2015 onwards)."""
    return fetch_json(f"cacher/{season}/race_list_basic.json", ttl=season_ttl(season))


def race_feed(season: int, series: int, race_id: int, name: str) -> Feed:
    """A per-race cacher feed: weekend-feed, lap-times, live-pit-data, or lap-notes."""
    return fetch_json(f"cacher/{season}/{series}/{race_id}/{name}.json", ttl=season_ttl(season))


def loop_stats(season: int, series: int, race_id: int) -> Feed:
    """Per-driver loop-data statistics for one race."""
    return fetch_json(f"loopstats/prod/{season}/{series}/{race_id}.json", ttl=season_ttl(season))


def points_feed(season: int, series: int) -> Feed:
    """The series points feed stored under a season path."""
    return fetch_json(f"cacher/{season}/{series}/racinginsights-points-feed.json", ttl=TTL.standings)


def drivers() -> Feed:
    """NASCAR's driver master list (about 1.4 MB, so it is cached for hours)."""
    return fetch_json("cacher/drivers.json", ttl=TTL.drivers)


def live_feed() -> Feed:
    """The live timing-and-scoring summary for whichever session is on track."""
    return fetch_json("live/feeds/live-feed.json", ttl=TTL.live)


# --- resolution helpers ---------------------------------------------------------


def season_races(season: int) -> list[dict[str, Any]]:
    """Races of ``season`` only: some race-list paths hold another season's races."""
    return [race for race in races_of(race_list(season)) if race.get("race_season") in (season, None)]


def races_of(feed: Feed) -> list[dict[str, Any]]:
    """Return every race object in a race-list feed, across series."""
    if not feed.found or not isinstance(feed.payload, dict):
        return []
    races: list[dict[str, Any]] = []
    for key in sorted(feed.payload):
        values = feed.payload[key]
        if key.startswith("series_") and isinstance(values, list):
            races.extend(value for value in values if isinstance(value, dict))
    return races


def current_season() -> int:
    """The latest season whose race list has been published (this year, else last year)."""
    year = current_year()
    if races_of(race_list(year)):
        return year
    return year - 1


@dataclass(frozen=True, slots=True)
class RaceRef:
    """Where a race's per-race feeds live."""

    season: int
    series_id: int
    race_id: int
    race: dict[str, Any]


def resolve_race(race_id: int, season: int | None = None) -> RaceRef | None:
    """Find a race's season and series from the race lists.

    With ``season`` given only that season is checked; otherwise seasons are
    searched from the current one back to 2015. Returns ``None`` when the race
    is unknown.
    """
    if race_id <= 0:
        raise ValueError("race_id must be positive")
    seasons: list[int] = (
        [season] if season is not None else list(range(current_season(), FIRST_SEASON - 1, -1))
    )
    for candidate in seasons:
        for race in races_of(race_list(candidate)):
            if race.get("race_id") == race_id:
                # Per-race feeds live under the race's own season, which is not always
                # the race-list path it was found under (the 2017 list holds 2018 races).
                race_season = race.get("race_season")
                return RaceRef(
                    season=race_season if isinstance(race_season, int) else candidate,
                    series_id=int(race.get("series_id") or 0),
                    race_id=race_id,
                    race=race,
                )
    return None
