"""A tiny local server so the dashboard can save settings directly.

The same ``docs/index.html`` is used everywhere. Served from here it finds
``api/health`` and switches into read-write mode; opened from GitHub Pages or
from disk it stays read-only and offers copy/download instead.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import dashboard
from .config import Config, ConfigError
from .state import State

log = logging.getLogger(__name__)

DOCS = Path(__file__).resolve().parent.parent / "docs"
MAX_BODY = 2_000_000

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".ico": "image/x-icon", ".webp": "image/webp",
}


def _handler(config_path: Path, state_path: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AutoTraderUI"

        def log_message(self, fmt: str, *args) -> None:
            log.debug("%s - %s", self.address_string(), fmt % args)

        # ---------------- helpers ----------------

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # This server edits local files, so it refuses to be embedded or
            # read cross-origin by a page in another tab.
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _same_origin(self) -> bool:
            """Reject writes initiated by another site (basic CSRF guard)."""
            origin = self.headers.get("Origin")
            if not origin:
                return True  # curl and friends send no Origin
            host = urlparse(origin).netloc
            return host in {self.headers.get("Host", ""), f"127.0.0.1:{self.server.server_port}",
                            f"localhost:{self.server.server_port}"}

        # ---------------- routes ----------------

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802
            route = urlparse(self.path).path
            if route in ("/", "/index.html"):
                return self._file(DOCS / "index.html")
            if route == "/api/health":
                return self._json(200, {"ok": True, "live": True,
                                        "config": str(config_path),
                                        "state": str(state_path)})
            if route in ("/data.json", "/api/data"):
                try:
                    cfg = Config.load(config_path)
                    state = State.load(state_path)
                    return self._json(200, dashboard.build_payload(cfg, state))
                except ConfigError as exc:
                    return self._json(500, {"error": str(exc)})
            # Anything else is a static file from docs/, and only from docs/.
            candidate = (DOCS / route.lstrip("/")).resolve()
            try:
                candidate.relative_to(DOCS.resolve())
            except ValueError:
                return self._json(403, {"error": "outside the docs directory"})
            return self._file(candidate)

        def do_POST(self) -> None:  # noqa: N802
            route = urlparse(self.path).path
            if route != "/api/config":
                return self._json(404, {"error": "no such endpoint"})
            if not self._same_origin():
                return self._json(403, {"error": "cross-origin write refused"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._json(400, {"error": "bad Content-Length"})
            if length <= 0 or length > MAX_BODY:
                return self._json(413, {"error": "body missing or too large"})
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return self._json(400, {"error": f"not valid JSON: {exc}"})
            if not isinstance(payload, dict):
                return self._json(400, {"error": "expected a JSON object"})
            try:
                cfg = Config(payload, config_path)
                cfg.normalise()
                cfg.save(config_path)
            except (ConfigError, OSError) as exc:
                return self._json(400, {"error": str(exc)})
            log.info("saved %s (%d search(es))", config_path, len(cfg.data.get("searches", [])))
            return self._json(200, {"ok": True, "searches": len(cfg.data.get("searches", []))})

        def _file(self, path: Path) -> None:
            if not path.is_file():
                return self._json(404, {"error": f"{path.name} not found"})
            try:
                body = path.read_bytes()
            except OSError as exc:
                return self._json(500, {"error": str(exc)})
            self._send(200, body, CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"))

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765,
          config_path: Path = Path("config.json"),
          state_path: Path = Path("state.json"),
          open_browser: bool = True) -> int:
    if not (DOCS / "index.html").is_file():
        print(f"Cannot find {DOCS / 'index.html'} - is the repository complete?")
        return 1

    # Make sure there is something to show before the browser opens.
    try:
        cfg = Config.load(config_path)
        dashboard.write(cfg, State.load(state_path))
    except ConfigError as exc:
        print(f"config problem: {exc}")
        return 2

    try:
        httpd = ThreadingHTTPServer((host, port), _handler(config_path, state_path))
    except OSError as exc:
        print(f"Could not listen on {host}:{port} - {exc}")
        print("Try a different port: python -m autotrader ui --port 8899")
        return 1

    url = f"http://{host}:{port}/"
    print(f"AutoTrader Watch is running at {url}")
    print(f"  settings file: {config_path.resolve()}")
    print("  press Ctrl+C to stop")
    if open_browser and not os.getenv("AUTOTRADER_NO_BROWSER"):
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0
