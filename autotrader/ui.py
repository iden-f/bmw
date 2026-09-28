"""A local, read-only viewer of the dashboard.

It serves the page in ``docs/`` with a ``data.json`` built on each request
from config.json and state.json, so the dashboard can be read on this
computer without publishing anything. It writes nothing: changes go through
the published, locked dashboard (see control.py).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, urlsplit

from . import dashboard
from .config import Config, ConfigError
from .state import State

log = logging.getLogger(__name__)

DOCS = Path(__file__).resolve().parent.parent / "docs"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".ico": "image/x-icon", ".webp": "image/webp",
}


def _handler(config_path: Path, state_path: Path, host: str = "127.0.0.1"):
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
            # What it serves is every car and the ntfy topic, so it refuses
            # to be embedded by a page in another tab.
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _addressed_here(self) -> bool:
            """Whether the request names this computer, not some website.

            A page on another site can point its own name at 127.0.0.1 (DNS
            rebinding) and then read this server as its own origin; the Host
            header still carries that name. An IP address cannot be pointed
            anywhere else, so any IP is accepted, and localhost, and the name
            `ui --host` was given, which is the address it opens. Any port:
            a forwarded one (ssh -L, a container's mapping) is named as the
            browser opened it, and a rebinding page's name is refused anyway.
            """
            try:
                name = urlsplit("//" + self.headers.get("Host", "")).hostname or ""
            except ValueError:
                return False
            if name and name in ("localhost", host.lower()):
                return True
            try:
                ipaddress.ip_address(name)
            except ValueError:
                return False
            return True

        # ---------------- routes ----------------

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802
            if not self._addressed_here():
                return self._json(403, {"error": "open this at http://127.0.0.1"})
            route = urlparse(self.path).path
            if route in ("/", "/index.html"):
                return self._file(DOCS / "index.html")
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
        httpd = ThreadingHTTPServer((host, port), _handler(config_path, state_path, host))
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
