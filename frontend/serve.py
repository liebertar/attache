"""Static server for the map and the manual approval page, with caching turned off.

python3 -m http.server sends no Cache-Control, so browsers kept an old map.html for days.
Instead of asking for a hard reload after every change, tell them not to store anything.

The root URL opens the map: "/" and "/index.html" answer 302 to "/map.html", query string kept.
"""
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

MAP_PAGE = "/map.html"
ROOT_PATHS = {"/", "/index.html"}


def map_location(path: str) -> str | None:
    """Where a request for the root goes, or None when the path is not the root."""
    parts = urlsplit(path)
    if parts.path not in ROOT_PATHS:
        return None
    return f"{MAP_PAGE}?{parts.query}" if parts.query else MAP_PAGE


class NoCacheHandler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        if not self._redirect_root():
            super().do_GET()

    def do_HEAD(self) -> None:
        if not self._redirect_root():
            super().do_HEAD()

    def _redirect_root(self) -> bool:
        location = map_location(self.path)
        if location is None:
            return False
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — parent signature
        pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3100
    directory = sys.argv[2] if len(sys.argv) > 2 else "."
    handler = partial(NoCacheHandler, directory=directory)
    ThreadingHTTPServer(("", port), handler).serve_forever()


if __name__ == "__main__":
    main()
