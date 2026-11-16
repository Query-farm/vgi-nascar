"""Serve the recorded cf.nascar.com fixtures over HTTP for the SQL tests.

``ci/run-integration.sh`` starts this server, points ``NASCAR_BASE_URL`` at
it, and runs ``test/sql`` through DuckDB, so the suite is hermetic. Unknown
paths answer 403 AccessDenied like S3 does. Prints ``PORT:<n>`` once listening.
"""

from __future__ import annotations

import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))

from conftest import route  # noqa: E402


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


def main() -> None:
    """Serve until killed."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NascarHandler)
    print(f"PORT:{server.server_port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
