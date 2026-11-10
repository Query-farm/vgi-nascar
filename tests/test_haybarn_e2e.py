"""Hermetic end-to-end coverage through Haybarn and the VGI transport."""

from __future__ import annotations

import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from conftest import route


class _NascarHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        status, body, content_type = route(urlparse(self.path).path)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_haybarn_attaches_and_queries_every_current_table() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NascarHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    project = Path(__file__).resolve().parent.parent
    location = f"uv run --project {project} vgi-nascar"
    sql = (
        f"INSTALL vgi FROM community; LOAD vgi; ATTACH 'nascar' (TYPE vgi, LOCATION '{location}'); "
        "SELECT "
        "(SELECT count(*) FROM nascar.main.current_standings) AS standings, "
        "(SELECT count(DISTINCT series_id) FROM nascar.main.current_schedule) AS schedule_series, "
        "(SELECT count(DISTINCT race_id) FROM nascar.main.last_race_results) AS last_races, "
        "(SELECT count(*) FROM nascar.main.live_race) AS live, "
        "(SELECT driver_name FROM nascar.main.race_results(5601, season := 2026) "
        " WHERE finishing_position = 1) AS winner, "
        "(SELECT sum(laps_led) FROM nascar.main.race_leaders(5601, season := 2026)) AS laps_led, "
        "(SELECT count(*) FROM nascar.main.lap_times(5601, season := 2026)) AS laps;"
    )
    env = {**os.environ, "NASCAR_BASE_URL": f"http://127.0.0.1:{server.server_port}"}
    try:
        result = subprocess.run(
            ["uvx", "haybarn-cli", ":memory:", "-csv", "-c", sql],
            cwd=project,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert "standings,schedule_series,last_races,live,winner,laps_led,laps" in result.stdout
    assert "15,3,3,0,Ryan Blaney,267,18" in result.stdout
