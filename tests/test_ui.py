"""The local `ui` server: the whole dashboard, decrypted, on this computer.

It answered any website that pointed its own name at 127.0.0.1 (DNS
rebinding): a page on attacker.example could read every car, the notes and
the ntfy topic, and could rewrite config.json through a POST route the page
itself never used. It now answers only requests addressed to this computer,
and writes nothing.
"""
from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from autotrader.config import Config
from autotrader.ui import _handler


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    root = tmp_path_factory.mktemp("ui")
    cfg = Config.defaults(root / "config.json")
    cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=25", "Civic")
    cfg.save()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                _handler(cfg.path, root / "state.json"))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    # The payload reads the photo index and the archive from the working
    # directory, which is not the repository's.
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(root)
        thread.start()
        try:
            yield httpd.server_port, cfg.path
        finally:
            httpd.shutdown()
            httpd.server_close()


def ask(port, path, host, method="GET", body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host)
        if body is not None:
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Origin", f"http://{host}")
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        reply = conn.getresponse()
        return reply.status, reply.read()
    finally:
        conn.close()


@pytest.mark.parametrize("name", ["127.0.0.1", "localhost", "[::1]"])
def test_it_answers_this_computer(server, name):
    port, _ = server
    status, body = ask(port, "/data.json", f"{name}:{port}")
    assert status == 200
    assert json.loads(body)["searches"][0]["name"] == "Civic"


@pytest.mark.parametrize("path", ["/data.json", "/api/data", "/index.html", "/app.js"])
@pytest.mark.parametrize("host", ["attacker.example:{port}", "127.0.0.1.attacker.example:{port}",
                                  "127.0.0.1:1", ""])
def test_it_refuses_a_request_addressed_to_another_name(server, path, host):
    port, _ = server
    status, body = ask(port, path, host.format(port=port))
    assert status == 403
    assert b"Civic" not in body


@pytest.mark.parametrize("host", ["127.0.0.1:{port}", "attacker.example:{port}"])
def test_it_writes_nothing(server, host):
    port, config = server
    before = config.read_text()
    status, _ = ask(port, "/api/config", host.format(port=port), method="POST",
                    body=json.dumps({"searches": []}).encode())
    assert status >= 400
    assert config.read_text() == before
