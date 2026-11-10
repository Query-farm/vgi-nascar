"""Tests for the cf.nascar.com HTTP boundary: retries, caching and missing files."""

from __future__ import annotations

import httpx
import pytest

from vgi_nascar import __version__, api


def _install(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(recording))
    monkeypatch.setenv("NASCAR_BASE_URL", "https://cdn.example.test/")
    monkeypatch.setattr(api, "_client", lambda: client)
    monkeypatch.setattr(api.time, "sleep", lambda seconds: None)
    return seen


def test_sends_honest_user_agent_and_builds_url(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install(monkeypatch, lambda request: httpx.Response(200, json={"ok": True}))
    feed = api.fetch_json("cacher/drivers.json", ttl=60)
    assert feed.payload == {"ok": True}
    assert feed.found and feed.ttl == 60
    assert str(seen[0].url) == "https://cdn.example.test/cacher/drivers.json"
    assert (
        seen[0].headers["User-Agent"]
        == f"vgi-nascar/{__version__} (+https://github.com/Query-farm/vgi-nascar)"
    )


def test_s3_access_denied_means_missing_and_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install(monkeypatch, lambda request: httpx.Response(403, text="<Error>AccessDenied</Error>"))
    first = api.fetch_json("cacher/2014/race_list_basic.json", ttl=60)
    second = api.fetch_json("cacher/2014/race_list_basic.json", ttl=60)
    assert not first.found and first.payload is None
    assert second is first
    assert len(seen) == 1


def test_json_null_is_treated_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, lambda request: httpx.Response(200, content=b"null"))
    assert not api.loop_stats(2015, 1, 4390).found


def test_successful_responses_are_cached_until_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install(monkeypatch, lambda request: httpx.Response(200, json=[1]))
    clock = [1_000.0]
    monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])
    api.fetch_json("a.json", ttl=10)
    api.fetch_json("a.json", ttl=10)
    assert len(seen) == 1
    clock[0] += 11
    api.fetch_json("a.json", ttl=10)
    assert len(seen) == 2


def test_cache_is_bounded() -> None:
    cache = api._Cache(max_entries=2)
    for key in ("a", "b", "c"):
        cache.store(key, api.Feed(payload=key, ttl=60))
    assert cache.get("a") is None
    assert cache.get("c") is not None


def test_retryable_response_honors_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        if attempts == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    _install(monkeypatch, handler)
    sleeps: list[float] = []
    monkeypatch.setattr(api.time, "sleep", sleeps.append)
    assert api.fetch_json("x.json", ttl=1).payload == {"ok": True}
    assert attempts == 3
    assert sleeps[0] == 2.0
    assert 0 < sleeps[1] <= api.MAX_RETRY_DELAY


def test_retry_after_http_date_and_garbage() -> None:
    dated = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
    assert api._retry_delay(dated, 0) == 0.0 or api._retry_delay(dated, 0) <= api.MAX_RETRY_DELAY
    garbage = httpx.Response(429, headers={"Retry-After": "soon"})
    assert 0 < api._retry_delay(garbage, 1) <= api.MAX_RETRY_DELAY
    assert api._retry_delay(httpx.Response(429, headers={"Retry-After": "999"}), 0) == api.MAX_RETRY_DELAY


def test_persistent_server_error_is_descriptive(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(api.NascarApiError, match="HTTP 500.*boom"):
        api.fetch_json("x.json", ttl=1)


def test_client_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install(monkeypatch, lambda request: httpx.Response(400, text="bad"))
    with pytest.raises(api.NascarApiError, match="HTTP 400"):
        api.fetch_json("x.json", ttl=1)
    assert len(seen) == 1


def test_non_json_success_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, lambda request: httpx.Response(200, text="<html>"))
    with pytest.raises(ValueError, match="did not return JSON"):
        api.fetch_json("x.json", ttl=1)


def test_transport_errors_are_retried_then_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("down", request=request)

    _install(monkeypatch, handler)
    with pytest.raises(httpx.ConnectError):
        api.fetch_json("x.json", ttl=1)
    assert attempts == api.MAX_ATTEMPTS


def test_shared_client_follows_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "_shared_client", None)
    client = api._client()
    try:
        assert client.follow_redirects is True
        assert api._client() is client
    finally:
        client.close()
        monkeypatch.setattr(api, "_shared_client", None)


def test_fetch_many_preserves_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, lambda request: httpx.Response(200, json=request.url.path))
    feeds = api.fetch_many([f"{index}.json" for index in range(6)], ttl=1)
    assert [feed.payload for feed in feeds] == [f"/{index}.json" for index in range(6)]


def test_series_names_and_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    assert [api.series_id(name) for name in ("cup", "XFINITY", "oreilly", "truck")] == [1, 2, 2, 3]
    with pytest.raises(ValueError, match="unknown series"):
        api.series_id("arca")
    monkeypatch.setattr(api, "current_year", lambda: 2026)
    assert api.season_ttl(2020) == api.TTL.historical
    assert api.season_ttl(2026) == api.TTL.current


def test_current_season_falls_back_to_last_year(
    fixture_feeds: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "current_year", lambda: 2027)
    assert api.current_season() == 2026
    assert "cacher/2027/race_list_basic.json" in fixture_feeds


def test_resolve_race_searches_seasons(fixture_feeds: list[str]) -> None:
    ref = api.resolve_race(5601)
    assert ref is not None
    assert (ref.season, ref.series_id, ref.race_id) == (2026, 1, 5601)
    assert api.resolve_race(424242, season=2026) is None
    with pytest.raises(ValueError, match="positive"):
        api.resolve_race(0)


def test_race_list_paths_holding_another_season_are_filtered(monkeypatch: pytest.MonkeyPatch) -> None:
    # NASCAR's 2017 race list actually contains the 2018 season.
    payload = {"series_1": [{"race_id": 4681, "series_id": 1, "race_season": 2018}]}
    _install(monkeypatch, lambda request: httpx.Response(200, json=payload))
    assert api.season_races(2017) == []
    assert [race["race_id"] for race in api.season_races(2018)] == [4681]
    ref = api.resolve_race(4681, season=2017)
    assert ref is not None and ref.season == 2018
