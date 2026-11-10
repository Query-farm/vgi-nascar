"""Shared fixtures: trimmed real cf.nascar.com responses served without the network."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from vgi_nascar import api

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Upstream path -> fixture file. Anything else answers like S3 does for a missing
# object: 403 AccessDenied with an XML body.
ROUTES = {
    "cacher/2026/race_list_basic.json": "race_list_2026.json",
    "cacher/2026/1/5601/weekend-feed.json": "weekend_feed_2026_1_5601.json",
    "cacher/2026/2/5639/weekend-feed.json": "weekend_feed_2026_2_5639.json",
    "cacher/2026/3/5670/weekend-feed.json": "weekend_feed_2026_3_5670.json",
    "cacher/2026/1/5601/lap-times.json": "lap_times_2026_1_5601.json",
    "cacher/2026/1/5601/live-pit-data.json": "live_pit_data_2026_1_5601.json",
    "cacher/2026/1/5601/lap-notes.json": "lap_notes_2026_1_5601.json",
    "loopstats/prod/2026/1/5601.json": "loopstats_2026_1_5601.json",
    "cacher/2026/1/racinginsights-points-feed.json": "points_feed_2026_1.json",
    "cacher/2026/2/racinginsights-points-feed.json": "points_feed_2026_2.json",
    "cacher/2026/3/racinginsights-points-feed.json": "points_feed_2026_3.json",
    "cacher/drivers.json": "drivers.json",
    "live/feeds/live-feed.json": "live_feed_idle.json",
}

ACCESS_DENIED = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b"<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>"
)


def load(name: str) -> Any:
    """Load one fixture file."""
    return json.loads((FIXTURES / name).read_text())


def route(path: str) -> tuple[int, bytes, str]:
    """Return (status, body, content type) for an upstream path."""
    name = ROUTES.get(path.lstrip("/"))
    if name is None:
        return 403, ACCESS_DENIED, "application/xml"
    return 200, (FIXTURES / name).read_bytes(), "application/json"


def fixture_handler(request: httpx.Request) -> httpx.Response:
    """httpx MockTransport handler serving fixtures."""
    status, body, content_type = route(request.url.path)
    return httpx.Response(status, content=body, headers={"Content-Type": content_type})


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    api.clear_cache()
    yield
    api.clear_cache()


@pytest.fixture
def fixture_feeds(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Route every upstream request to the fixtures; returns the requested paths."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path.lstrip("/"))
        return fixture_handler(request)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://cf.nascar.test")
    monkeypatch.setenv("NASCAR_BASE_URL", "https://cf.nascar.test")
    monkeypatch.setattr(api, "_client", lambda: client)
    monkeypatch.setattr(api, "current_year", lambda: 2026)
    return requested
