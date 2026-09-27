"""The Marketplace collector, end to end, against a stand-in Facebook and GitHub.

A real browser loads a local page built the way a Marketplace search page is
- its first results in the page's own JSON, more arriving from an API call
when it is scrolled - and the collector's batch goes to a local stand-in for
GitHub's API. The batch is then opened and taken in by the bot's own code,
so this is the proof that the two halves agree.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from autotrader import marketplace as M
from autotrader import vault as V
from autotrader.config import Config
from autotrader.state import State

from .test_marketplace import SEARCH_URL, api_reply, item_page, node, search_page

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

PHRASE = "a long enough test passphrase"


def _browser_path() -> str | None:
    found = sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome"))
    return str(found[0]) if found else None


SCROLL_JS = """
<script>
let asked = false;
addEventListener('scroll', () => {
  if (asked) return; asked = true;
  fetch('/api/graphql/', {method: 'POST', body: 'q=1'});
});
</script>
<div style="height:20000px">results</div>
"""


class FakeFacebook(BaseHTTPRequestHandler):
    """Serves search pages, one scroll's worth of API reply, and item pages."""
    mode = "ok"
    first = [node("200000001", "2019 Honda civic si coupe 2d", price="24000.00")]
    more = [node("200000002", "2018 Honda civic lx sedan 4d", price="21000.00"),
            node("200000003", "2017 Honda accord ex", price="19000.00")]
    visited: list[str] = []

    def log_message(self, *a):
        pass

    def _send(self, body: str, status: int = 200, headers=None):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        type(self).visited.append(self.path)
        if path == "/sign-in":
            return self._send("signed in", headers={
                "Set-Cookie": "c_user=100000; Path=/; Max-Age=86400"})
        if type(self).mode == "signed_out" and path.startswith("/marketplace"):
            return self._send("", 302, {"Location": "/login/?next=x"})
        if path.startswith("/login"):
            return self._send("<html>log in</html>")
        if "/search/" in path:
            page = search_page(*type(self).first)
            return self._send(page.replace("</body>", SCROLL_JS + "</body>"))
        if path.startswith("/marketplace/item/"):
            item = path.split("/")[3]
            return self._send(item_page(item, "2019 Honda civic si coupe 2d",
                                        odometer=51234))
        return self._send("not here", 404)

    def do_POST(self):
        if urlsplit(self.path).path.startswith("/api/graphql"):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = api_reply(*type(self).more).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send("", 404)


class FakeGitHub(BaseHTTPRequestHandler):
    """The two calls the collector makes, and the one its check makes."""
    files: dict[str, bytes] = {}
    sent: list[dict] = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path.startswith("/repos/someone/watch/contents/"):
            name = parts.path.rsplit("/", 1)[-1]
            blob = type(self).files.get(name)
            if blob is None or "ref=vault" not in parts.query:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
            return
        if parts.path == "/repos/someone/watch":
            body = json.dumps({"permissions": {"push": True}}).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(body); return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == "/repos/someone/watch/dispatches":
            type(self).sent.append(json.loads(
                self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204); self.end_headers(); return
        self.send_response(404); self.end_headers()


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def world(tmp_path, monkeypatch):
    chrome = _browser_path()
    if not chrome:
        pytest.skip("no bundled chromium to drive")
    from collector import settings as S
    from collector.github import GitHub

    # The watch: a vault holding a config with one search.
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = Config.defaults(repo / "config.json")
    cfg.add_search(SEARCH_URL, "Example search")
    cfg.data["searches"][0]["filters"] = {"models": ["Civic"], "min_year": 2015,
                                          "near": "Ottawa, ON", "max_distance_km": 300}
    cfg.save()
    vault = V.Vault.unlock(repo, {V.ENV_KEY: PHRASE}, create=True)
    vault.seal_file(repo / "config.json", "config.enc")
    State(path=repo / "state.json").save()
    vault.seal_file(repo / "state.json", "state.enc")
    FakeGitHub.files = {p.name: p.read_bytes() for p in (repo / "vault").iterdir()
                        if p.is_file()}
    FakeGitHub.sent = []
    FakeFacebook.mode, FakeFacebook.visited = "ok", []

    fb, gh = _serve(FakeFacebook), _serve(FakeGitHub)
    monkeypatch.setenv("COLLECTOR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(S.ENV_TOKEN, "test-token")
    monkeypatch.setenv(S.ENV_PASSPHRASE, PHRASE)
    monkeypatch.setenv("COLLECTOR_NO_KEYCHAIN", "1")
    settings = S.Settings(repo="someone/watch", host="collector-a", channel="",
                          facebook=f"http://127.0.0.1:{fb.server_port}",
                          quiet_start="00:00", quiet_end="00:00", scrolls=1,
                          extra={"executable_path": chrome, "pace": 0})
    github = GitHub("someone/watch", "test-token", api=f"http://127.0.0.1:{gh.server_port}")

    w = type("World", (), {})()
    w.settings, w.github, w.repo, w.cfg, w.key = settings, github, repo, cfg, vault.key
    w.fb = f"http://127.0.0.1:{fb.server_port}"
    try:
        yield w
    finally:
        github.http.close()
        for server in (fb, gh):
            server.shutdown()
            server.server_close()


def sign_in(world):
    from collector.browser import Browser
    browser = Browser(world.settings)
    with browser.open():
        page = browser.context.new_page()
        page.goto(world.fb + "/sign-in")
        page.close()
        assert browser.signed_in()


def last_batch(world):
    [body] = FakeGitHub.sent[-1:]
    assert body["event_type"] == "marketplace"
    assert body["client_payload"]["from"] == "marketplace-collector"
    return M.open_batch(world.key, body["client_payload"]["batch"])


class TestAPass:

    def test_reads_the_page_and_its_scroll_and_sends_it_sealed(self, world):
        from collector.cycle import once
        sign_in(world)
        summary = once(world.settings, github=world.github)
        assert summary["polled"] and summary["session"] == "ok"
        batch = last_batch(world)
        [part] = batch["searches"]
        assert part["ok"] and part["search"] == world.cfg.searches[0].id
        ids = {r["id"] for r in part["listings"]}
        # The first result came with the page, the rest with the scroll.
        assert ids == {"200000001", "200000002", "200000003"}
        # The search it asked for is the one the bot planned.
        [search] = [v for v in FakeFacebook.visited if "/search/" in v]
        assert "query=Honda+Civic" in search and "sortBy=creation_time_descend" in search

    def test_opens_the_page_of_a_car_worth_hearing_about(self, world):
        from collector.cycle import once
        sign_in(world)
        once(world.settings, github=world.github)
        items = [v for v in FakeFacebook.visited if "/marketplace/item/" in v]
        # Both Civics pass the rules; the Accord is another model and is not opened.
        assert sorted(items) == ["/marketplace/item/200000001/",
                                 "/marketplace/item/200000002/"]
        cars = {r["id"]: r for r in last_batch(world)["searches"][0]["listings"]}
        assert cars["200000001"]["mileage_km"] == 51234
        assert cars["200000001"]["transmission"] == "Manual"
        # Once is enough: the next pass does not open them again.
        FakeFacebook.visited = []
        once(world.settings, github=world.github)
        assert not [v for v in FakeFacebook.visited if "/marketplace/item/" in v]

    def test_the_bot_takes_in_what_the_collector_sent(self, world):
        from collector.cycle import once
        sign_in(world)
        once(world.settings, github=world.github)
        state = State(path=world.repo / "state.json")
        body = FakeGitHub.sent[-1]["client_payload"]["batch"]
        report = M.ingest(world.cfg, state, M.open_batch(world.key, body, state),
                          env={}, notify=False)
        assert report.searches_run == 1
        assert {"fb-200000001", "fb-200000002"} <= set(state.listings)
        assert "fb-200000003" not in state.listings       # another model

    def test_signed_out_is_sent_as_such(self, world):
        from collector.cycle import once
        summary = once(world.settings, github=world.github)
        assert summary["session"] == "signed_out"
        batch = last_batch(world)
        assert batch["session"] == "signed_out" and batch["searches"] == []

    def test_a_session_facebook_ends_mid_pass_is_caught(self, world):
        from collector.cycle import once
        sign_in(world)
        FakeFacebook.mode = "signed_out"
        summary = once(world.settings, github=world.github)
        assert summary["session"] == "signed_out"

    def test_overnight_it_only_checks_in(self, world):
        from collector.cycle import once
        world.settings.quiet_start, world.settings.quiet_end = "00:00", "23:59"
        summary = once(world.settings, github=world.github,
                       now=datetime(2026, 1, 1, 12, 0))
        assert not summary["polled"] and summary["note"] == "overnight pause"
        assert not FakeFacebook.visited
        assert last_batch(world)["polled"] is False


class TestStandby:

    def _state_with(self, world, host, role, minutes_ago):
        from autotrader import clock
        from datetime import timedelta
        heard = (clock.now() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
        state = State(path=world.repo / "state.json")
        state.data["marketplace"] = {"hosts": {host: {"received": heard, "role": role}}}
        state.save()
        vault = V.Vault.unlock(world.repo, {V.ENV_KEY: PHRASE})
        vault.seal_file(world.repo / "state.json", "state.enc")
        FakeGitHub.files["state.enc"] = (world.repo / "vault" / "state.enc").read_bytes()

    def test_stands_by_while_the_primary_is_heard(self, world):
        from collector.cycle import once
        world.settings.role, world.settings.host = "standby", "collector-b"
        self._state_with(world, "collector-a", "primary", 20)
        summary = once(world.settings, github=world.github)
        assert not summary["polled"] and "standing by for collector-a" in summary["note"]
        assert not FakeFacebook.visited

    def test_takes_over_when_it_is_not(self, world):
        from collector.cycle import once
        sign_in(world)
        world.settings.role, world.settings.host = "standby", "collector-b"
        self._state_with(world, "collector-a", "primary", 200)
        summary = once(world.settings, github=world.github)
        assert summary["polled"] and summary["session"] == "ok"
        assert last_batch(world)["role"] == "standby"


class TestFitting:

    def test_a_huge_read_is_trimmed_to_fit_newest_first(self, world):
        from collector.cycle import build, fit
        import secrets
        # Random text, as a real photo address is: it does not compress.
        records = [{"id": str(300000000 + i), "title": "2018 Honda civic",
                    "created": 1_700_000_000 + i,
                    "photo": "https://cdn.example/" + secrets.token_hex(150)}
                   for i in range(900)]
        batch = build(world.settings, [{"search": "s", "scope": "x", "ok": True,
                                        "error": "", "landed": "", "listings": records}],
                      polled=True, session="ok")
        sealed = fit(world.key, batch, {})
        assert len(sealed) <= M.PAYLOAD_LIMIT
        kept = M.open_batch(world.key, sealed)["searches"][0]["listings"]
        assert kept and kept[0]["created"] == max(r["created"] for r in records)

    def test_photos_stop_once_sent_enough(self, world):
        from collector.cycle import PHOTO_SENDS, build, fit
        memory: dict = {}
        for n in range(PHOTO_SENDS + 1):
            batch = build(world.settings, [{"search": "s", "scope": "x", "ok": True,
                                            "error": "", "landed": "", "listings": [
                                                {"id": "300000001", "photo": "https://cdn.example/a"}]}],
                          polled=True, session="ok")
            sent = M.open_batch(world.key, fit(world.key, batch, memory))
            has = "photo" in sent["searches"][0]["listings"][0]
            assert has == (n < PHOTO_SENDS)


class TestTheClock:

    def test_overnight_wraps_midnight(self):
        from collector.cycle import resting
        from collector.settings import Settings
        s = Settings(quiet_start="23:30", quiet_end="06:00")
        assert resting(s, datetime(2026, 1, 1, 23, 45))
        assert resting(s, datetime(2026, 1, 1, 3, 0))
        assert not resting(s, datetime(2026, 1, 1, 12, 0))

    def test_waits_twenty_to_thirty_minutes(self):
        from collector.cycle import next_wait
        from collector.settings import Settings
        waits = [next_wait(Settings()) / 60 for _ in range(200)]
        assert 20 <= min(waits) and max(waits) <= 30


class TestTheService:

    def test_keeps_the_mac_awake_and_restarts(self):
        from collector import launchd
        agent = launchd.agent()
        assert agent["ProgramArguments"][:2] == ["/usr/bin/caffeinate", "-i"]
        assert agent["ProgramArguments"][-1] == "daemon"
        assert agent["KeepAlive"] is True and agent["RunAtLoad"] is True

    def test_the_wrapper_runs_the_package(self):
        run = Path(__file__).resolve().parent.parent / "collector" / "run"
        assert run.stat().st_mode & 0o111, "collector/run must be executable"
        assert "-m collector" in run.read_text()


class TestSecrets:

    def test_off_a_mac_they_come_from_the_environment(self, monkeypatch):
        from collector import settings as S
        monkeypatch.setenv("COLLECTOR_NO_KEYCHAIN", "1")
        monkeypatch.setenv(S.ENV_TOKEN, "t")
        monkeypatch.delenv(S.ENV_PASSPHRASE, raising=False)
        assert S.secret(S.TOKEN) == "t" and S.secret(S.PASSPHRASE) == ""

    def test_nothing_the_collector_keeps_is_in_the_clone(self, monkeypatch):
        from collector import settings as S
        monkeypatch.delenv("COLLECTOR_HOME", raising=False)
        assert S.repo_root() not in S.home().parents


class TestAPageItCannotRead:

    def test_is_a_failed_read_not_an_empty_market(self, world):
        from collector.cycle import once
        sign_in(world)
        FakeFacebook.first, FakeFacebook.more = [], []
        try:
            once(world.settings, github=world.github)
        finally:
            FakeFacebook.first = [node("200000001", "2019 Honda civic si coupe 2d",
                                       price="24000.00")]
            FakeFacebook.more = [node("200000002", "2018 Honda civic lx sedan 4d",
                                      price="21000.00"),
                                 node("200000003", "2017 Honda accord ex", price="19000.00")]
        [part] = last_batch(world)["searches"]
        assert part["ok"] is False
        assert part["error"] == "the page loaded but no listings could be read from it"


class TestWhichPagesItOpens:

    def test_a_new_car_before_the_backlog(self, world):
        from collector.cycle import once
        sign_in(world)
        world.settings.details_per_cycle = 1
        once(world.settings, github=world.github)
        assert [v for v in FakeFacebook.visited if "/item/" in v] == \
            ["/marketplace/item/200000001/"]
        before = list(FakeFacebook.first)
        FakeFacebook.first = [node("200000009", "2020 Honda civic ex", price="26000.00"),
                              *before]
        FakeFacebook.visited = []
        try:
            once(world.settings, github=world.github)
        finally:
            FakeFacebook.first = before
        # 200000002 has waited a pass, but the car that just appeared goes first.
        assert [v for v in FakeFacebook.visited if "/item/" in v] == \
            ["/marketplace/item/200000009/"]
