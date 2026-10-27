"""One collector pass, driven by a stand-in browser: which pages it opens,
what it keeps, and what it sends.

No real browser and no server: the stand-in answers each address the way
Facebook's pages are shaped in tests/test_marketplace.py, and records every
page opened and every pause taken between them. So these run everywhere,
and they say exactly how much a pass asks of Facebook.
"""
from __future__ import annotations

import secrets
from contextlib import contextmanager
from datetime import timedelta

import pytest

from autotrader import clock
from autotrader import marketplace as M
from autotrader import runner as runner_mod
from autotrader import vault as V
from autotrader.config import Config
from autotrader.state import State
from collector import cycle
from collector import settings as S
from collector.browser import Page
from collector.github import GitHubError

from .helpers import Capture, use_channels
from .test_marketplace import SEARCH_URL, batch, item_page, node, search_page

PHRASE = "a long enough test passphrase"
REST = "REST"


class FakeBrowser:
    """Answers a search with ``cards`` and a listing with its own page.

    ``log`` holds every address opened, and REST for every pause between.
    """

    def __init__(self, cards=None, *, status=None, odometer=51234, session=None,
                 fail=None, signed_in=True):
        self.cards = cards if cards is not None else [
            node("200000001", "2019 Honda civic si coupe 2d", price="24000.00")]
        self.status, self.odometer = status, odometer
        # {"search" or "item": the session a page of that kind lands on}
        self.session = session or {}
        self.fail = fail                    # a query whose page times out
        self._signed_in = signed_in
        self.log: list[str] = []
        self.opened = 0

    @contextmanager
    def open(self):
        self.opened += 1
        yield self

    def signed_in(self) -> bool:
        return self._signed_in

    def rest(self) -> None:
        self.log.append(REST)

    def visit(self, url: str, *, scrolls: int = 0) -> Page:
        self.log.append(url.replace("https://www.facebook.com", ""))
        page = Page(url)
        page.landed = url
        kind = "item" if "/marketplace/item/" in url else "search"
        if self.session.get(kind):
            page.session = self.session[kind]
            page.landed = "https://www.facebook.com/checkpoint/1/"
            return page
        if kind == "item":
            item = url.rstrip("/").rsplit("/", 1)[1]
            page.texts = [item_page(item, "2019 Honda civic si coupe 2d",
                                    odometer=self.odometer, status=self.status)]
        elif self.fail and self.fail in url:
            page.error = "TimeoutError: Timeout 45000ms exceeded."
        else:
            page.texts = [search_page(*self.cards)]
        return page

    def searches(self) -> list[str]:
        return [v for v in self.log if "/search/" in v]

    def items(self) -> list[str]:
        return [v for v in self.log if "/item/" in v]


def watch_config(tmp_path, *, searches=1, models=("Civic",), **rules) -> Config:
    cfg = Config.defaults(tmp_path / "config.json")
    for n in range(searches):
        cfg.add_search(SEARCH_URL.replace("prx=-2", f"prx={100 * n}") if n else SEARCH_URL,
                       f"Search {n + 1}")
    for raw in cfg.data["searches"]:
        raw["filters"] = {"models": list(models), "min_year": 2015,
                          "near": "Ottawa, ON", "max_distance_km": 300, **rules}
    cfg.set("archive.mode", "off")
    cfg.save()
    return cfg


def webp(width: int = 250, height: int = 188, pad: int = 900) -> bytes:
    """A minimal but genuinely parseable VP8X WebP."""
    import struct
    body = (b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00"
            + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
            + b"\x00" * pad)
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body


class FakeCDN:
    """The photo CDN as the collector reaches it: a picture for every link,
    or whatever ``mode`` says. ``calls`` is every address asked for."""

    def __init__(self, mode="ok"):
        self.mode, self.calls = mode, []

    def get(self, url, timeout=None, headers=None):
        self.calls.append(url)
        kind, status, body = "image/webp", 200, webp()
        if self.mode == "403":
            kind, status, body = "text/plain", 403, b"URL signature expired"
        elif self.mode == "html":
            kind, body = "text/html", b"<html>log in</html>"
        elif self.mode == "big":
            body = webp(pad=cycle.PHOTO_MAX_BYTES)
        elif self.mode == "raise":
            raise ConnectionError("connection reset by peer")
        return type("R", (), {"status_code": status, "content": body,
                              "headers": {"Content-Type": kind}})()


def settings(**kw) -> S.Settings:
    kw.setdefault("host", "collector-a")
    return S.Settings(repo="someone/watch", quiet_start="00:00", quiet_end="00:00",
                      channel="", **kw)


# ================================================================ the read

class TestWhatAPageSaidIsKept:
    """A car's own page is opened once. What it said has to reach the bot
    with every batch after, or the next card-only batch judges the car on
    its card alone and lets back in a car the page showed should be hidden."""

    def test_every_later_batch_carries_it(self, tmp_path):
        cfg = watch_config(tmp_path)
        memory: dict = {}
        first = FakeBrowser(status="REBUILT", odometer=151234)
        [part], _ = cycle.read(cfg, M.plan(cfg), first, settings(), memory)
        assert first.items() == ["/marketplace/item/200000001/"]
        later = FakeBrowser(status="REBUILT", odometer=151234)
        [part], _ = cycle.read(cfg, M.plan(cfg), later, settings(), memory)
        assert later.items() == []                   # still opened only once
        [car] = part["listings"]
        assert car["mileage_km"] == 151234 and "mileage_rounded" not in car
        assert car["title_status"] == "Rebuilt" and car["transmission"] == "Manual"
        assert car["trim"] == "Si Coupe 2D"
        # The card's own price and photo stay: the page's are not the card's.
        assert car["price"] == 24000 and car["photo"].endswith("200000001.jpg")

    @pytest.mark.parametrize("rules, status, odometer", [
        ({"exclude_keywords": ["rebuilt"]}, "REBUILT", 61234),
        ({"max_mileage_km": 150000}, None, 150400),
    ])
    def test_a_car_its_page_hid_is_not_let_back_in(self, tmp_path, monkeypatch,
                                                  rules, status, odometer):
        cfg = watch_config(tmp_path, **rules)
        sink = Capture()
        use_channels(monkeypatch, runner_mod, [sink])
        state = State(path=tmp_path / "state.json")
        memory: dict = {}
        cards = [node("200000001", "2019 Honda civic si coupe 2d", price="24000.00",
                      sub="150K km")]
        for _ in range(3):
            browser = FakeBrowser(cards, status=status, odometer=odometer)
            parts, _ = cycle.read(cfg, M.plan(cfg), browser, settings(), memory)
            report = M.ingest(cfg, state, batch(*parts), env={})
            assert state.listings["fb-200000001"]["filtered"]
            assert report.qualified == 0
        assert sink.digests == [] and sink.alerts == []

    def test_a_rounded_figure_from_the_page_is_not_kept_as_exact(self, tmp_path):
        cfg = watch_config(tmp_path)
        memory: dict = {}

        class RoundedPage(FakeBrowser):
            def visit(self, url, *, scrolls=0):
                page = super().visit(url, scrolls=scrolls)
                if "/item/" in url:
                    page.texts = [search_page(node("200000001", "2019 Honda civic si",
                                                   price="24000.00", sub="52K km"))]
                return page

        cycle.read(cfg, M.plan(cfg), RoundedPage(), settings(), memory)
        assert "mileage_km" not in memory["cars"]["200000001"]["facts"]
        [part], _ = cycle.read(cfg, M.plan(cfg), FakeBrowser(), settings(), memory)
        assert part["listings"][0]["mileage_km"] == 45000       # the card's


class TestWhichPagesAPassOpens:

    def test_a_car_two_searches_find_is_opened_once(self, tmp_path):
        cfg = watch_config(tmp_path, searches=2)
        browser = FakeBrowser()
        parts, _ = cycle.read(cfg, M.plan(cfg), browser, settings(), {})
        assert browser.items() == ["/marketplace/item/200000001/"]
        # Both searches' copies carry what the page said.
        assert [p["listings"][0]["mileage_km"] for p in parts] == [51234, 51234]

    def test_it_pauses_between_the_models_of_one_search(self, tmp_path):
        cfg = watch_config(tmp_path, models=("Civic", "Accord"))
        browser = FakeBrowser()
        cycle.read(cfg, M.plan(cfg), browser, settings(), {}, details=False)
        # Each model in both orders, and the pause a person takes between
        # every two pages.
        civic_new, civic_best, accord_new, accord_best = browser.searches()
        assert "query=Honda+Civic" in civic_new and "query=Honda+Civic" in civic_best
        assert "sortBy=" in civic_new and "sortBy=" not in civic_best
        assert "query=Honda+Accord" in accord_new and "query=Honda+Accord" in accord_best
        assert browser.log == [civic_new, REST, civic_best, REST,
                               accord_new, REST, accord_best]

    def test_and_between_searches_and_before_a_listing(self, tmp_path):
        cfg = watch_config(tmp_path, searches=2)
        browser = FakeBrowser()
        cycle.read(cfg, M.plan(cfg), browser, settings(), {})
        assert [v == REST for v in browser.log] == [
            False, True, False, True, False, True, False, True, False]

    def test_a_car_only_one_order_shows_is_read(self, tmp_path):
        # What this is for: a listing Marketplace's newest-first page leaves
        # out, shown by its default order, reaches the batch all the same.
        cfg = watch_config(tmp_path)
        only_in_best = node("200000002", "2019 Honda civic touring", price="26000.00")

        class Browser(FakeBrowser):
            def visit(self, url, *, scrolls=0):
                page = super().visit(url, scrolls=scrolls)
                if "/search/" in url and "sortBy=" not in url:
                    page.texts = [search_page(only_in_best)]
                return page

        [part], _ = cycle.read(cfg, M.plan(cfg), Browser(), settings(), {}, details=False)
        assert sorted(r["id"] for r in part["listings"]) == ["200000001", "200000002"]

    def test_a_car_new_this_pass_goes_first_though_another_search_saw_it_first(
            self, tmp_path):
        # The first search wants neither car; the second wants both, and one
        # of them it has seen before.
        cfg = watch_config(tmp_path, searches=2)
        cfg.data["searches"][0]["filters"]["min_year"] = 2020
        memory = {"cars": {"200000002": {"first": "2026-01-01T00:00:00+00:00",
                                         "seen": clock.stamp()}}}
        cards = [node("200000002", "2018 Honda civic lx", price="21000.00"),
                 node("200000001", "2019 Honda civic si coupe 2d", price="24000.00")]
        browser = FakeBrowser(cards)
        cycle.read(cfg, M.plan(cfg), browser, settings(details_per_cycle=1), memory)
        assert browser.items() == ["/marketplace/item/200000001/"]


class TestASearchReadOnlyInPart:

    def test_says_which_query_failed_beside_what_the_other_read(self, tmp_path):
        # What the bot needs to keep the error and take no car for gone.
        cfg = watch_config(tmp_path, models=("Civic", "Accord"))
        [part], _ = cycle.read(cfg, M.plan(cfg), FakeBrowser(fail="Accord"),
                               settings(), {}, details=False)
        assert part["ok"] and part["listings"]
        assert part["error"].startswith("TimeoutError")


class TestACheckpoint:
    """Facebook asking the account to confirm who it is ends the pass."""

    def test_on_a_search_no_further_page_is_opened(self, tmp_path):
        cfg = watch_config(tmp_path, searches=2, models=("Civic", "Accord"))
        browser = FakeBrowser(session={"search": "checkpoint"})
        parts, session = cycle.read(cfg, M.plan(cfg), browser, settings(), {})
        assert session == "checkpoint" and parts == []
        # One page, and not the next model's, the next search's or a listing.
        assert len(browser.searches()) == 1 and browser.items() == []

    def test_after_a_search_that_read_the_listings_are_left_alone(self, tmp_path):
        cfg = watch_config(tmp_path, searches=2)

        class SecondSearch(FakeBrowser):
            def visit(self, url, *, scrolls=0):
                # The first search's two pages have been read; the second
                # search's first page meets the checkpoint.
                if len(self.searches()) == 2 and "/search/" in url:
                    self.session = {"search": "checkpoint"}
                return super().visit(url, scrolls=scrolls)

        browser = SecondSearch()
        parts, session = cycle.read(cfg, M.plan(cfg), browser, settings(), {})
        assert session == "checkpoint" and len(parts) == 1
        assert browser.items() == []

    def test_on_a_listing_it_is_said(self, tmp_path):
        cfg = watch_config(tmp_path)
        browser = FakeBrowser(session={"item": "checkpoint"})
        parts, session = cycle.read(cfg, M.plan(cfg), browser, settings(), {})
        assert session == "checkpoint" and len(browser.items()) == 1
        assert parts[0]["ok"]                 # what the search read still goes


# ============================================================ fitting a batch

class TestFittingABigFirstRead:

    def key(self):
        return bytes(range(32))

    def parts(self, n_each=150, pictures=False):
        # A first read of three searches: no photo sent yet, no card dated,
        # and photo addresses as random as Facebook's.
        out = [{"search": s, "scope": "x", "ok": True, "error": "", "landed": "",
                "listings": [{"id": str(300000000 + 1000 * n + i),
                              "title": "2018 Honda civic",
                              "photo": "https://cdn.example/" + secrets.token_hex(150)}
                             for i in range(n_each)]}
               for n, s in enumerate(("s1", "s2", "s3"))]
        if pictures:
            for part in out:
                for rec in part["listings"]:
                    rec["photo_data"] = secrets.token_urlsafe(600)
                    rec["photo_type"] = "image/webp"
        return out

    def test_a_read_too_big_for_a_dispatch_goes_whole(self):
        """Five real searches read a thousand cars, and their photo links
        did not fit a dispatch: trimmed photos first, most cars' never
        arrived. A big batch is carried whole now, as a blob."""
        memory: dict = {}
        sent = cycle.build(settings(), self.parts(), polled=True, session="ok")
        sealed = cycle.fit(self.key(), sent, memory)
        assert len(sealed) > M.PAYLOAD_LIMIT
        opened = M.open_batch(self.key(), sealed)["searches"]
        assert [len(p["listings"]) for p in opened] == [150, 150, 150]
        assert all(r.get("photo") for p in opened for r in p["listings"])
        assert all(memory["cars"][r["id"]]["photos"] == 1
                   for p in opened for r in p["listings"])

    def test_past_what_a_blob_carries_pictures_go_then_photos_then_cars(self):
        memory: dict = {}
        sent = cycle.build(settings(), self.parts(pictures=True), polled=True, session="ok")
        whole = len(cycle.fit(self.key(), sent, {}))
        # Room for the cars and their photo links, not for every picture.
        sent = cycle.build(settings(), self.parts(pictures=True), polled=True, session="ok")
        sealed = cycle.fit(self.key(), sent, memory, limit=whole * 2 // 3)
        opened = M.open_batch(self.key(), sealed)["searches"]
        assert [len(p["listings"]) for p in opened] == [150, 150, 150]
        assert all(r.get("photo") for p in opened for r in p["listings"])
        with_picture = [[bool(r.get("photo_data")) for r in p["listings"]] for p in opened]
        # The newest cars of every search keep theirs, not the first search's
        # cars all of theirs.
        assert all(flags[0] and not flags[-1] for flags in with_picture)
        # A picture left out is not counted as carried: a later pass does it.
        last = opened[2]["listings"][-1]["id"]
        assert "picture" not in memory["cars"].get(last, {})
        assert memory["cars"][opened[2]["listings"][0]["id"]]["picture"]
        # Tighter still, and the oldest cars' photo links go, never a car.
        sent = cycle.build(settings(), self.parts(), polled=True, session="ok")
        sealed = cycle.fit(self.key(), sent, {}, limit=M.PAYLOAD_LIMIT)
        opened = M.open_batch(self.key(), sealed)["searches"]
        assert [len(p["listings"]) for p in opened] == [150, 150, 150]
        with_photo = [[bool(r.get("photo")) for r in p["listings"]] for p in opened]
        assert all(flags[0] and not flags[-1] for flags in with_photo)


class TestPictures:
    """Facebook's photo links die within days and its CDN turns the bot's
    runner away once they have, so the collector fetches the card photo
    while the link is fresh and carries the picture itself."""

    def cars(self, tmp_path, *ids_and_titles, plan=None):
        cfg = watch_config(tmp_path) if plan is None else None
        plan = M.plan(cfg) if plan is None else plan
        [item] = plan
        recs = [{"id": i, "title": t, "photo": f"https://cdn.example/{i}.jpg",
                 "created": 1_700_000_000 + n, "price": 24000}
                for n, (i, t) in enumerate(ids_and_titles)]
        parts = [{"search": item["search"], "scope": "x", "ok": True, "error": "",
                  "landed": "", "listings": recs}]
        return cfg, plan, parts

    def test_the_card_photo_of_every_kept_car_is_carried_newest_first(self, tmp_path):
        cfg, plan, parts = self.cars(tmp_path, ("300000001", "2019 Honda civic si"),
                                     ("300000002", "2018 Honda civic lx"),
                                     ("300000003", "2017 Honda accord ex"),
                                     ("300000004", "2012 Honda civic dx"))
        cdn, memory = FakeCDN(), {}
        assert cycle.pictures(cfg, plan, parts, memory, cdn) == 2
        by_id = {r["id"]: r for r in parts[0]["listings"]}
        assert by_id["300000001"]["photo_type"] == "image/webp"
        assert by_id["300000001"]["photo_data"] and by_id["300000002"]["photo_data"]
        # Another model, and a car the rules hide: no picture fetched.
        assert "photo_data" not in by_id["300000003"]
        assert "photo_data" not in by_id["300000004"]
        # Newest first: the one listed last was asked for first.
        assert cdn.calls == ["https://cdn.example/300000002.jpg",
                             "https://cdn.example/300000001.jpg"]
        # And the bot takes the picture in as the car's photo.
        sealed = cycle.fit(bytes(range(32)), cycle.build(settings(), parts, polled=True,
                                                         session="ok"), memory)
        assert memory["cars"]["300000001"]["picture"]
        bot = M.open_batch(bytes(range(32)), sealed)
        state = State(path=tmp_path / "state.json")
        with _in(tmp_path):
            report = M.ingest(cfg, state, bot, env={}, notify=False, fetcher=FakeCDN("403"))
            from autotrader import thumbs
            assert report.pictures == {"carried": 2, "kept": 2, "refused": 0}
            assert thumbs.locals_for("fb-300000001") == ["thumbs/fb-300000001.webp"]
            assert thumbs.locals_for("fb-300000002")
            # The bot asks the CDN for nothing it was handed.
            assert report.photos["fetched"] == 0 and report.photos["kept"] == 2

    def test_once_per_car_and_so_many_per_pass(self, tmp_path):
        cfg, plan, parts = self.cars(tmp_path, ("300000001", "2019 Honda civic si"),
                                     ("300000002", "2018 Honda civic lx"))
        cdn, memory = FakeCDN(), {}
        assert cycle.pictures(cfg, plan, parts, memory, cdn, limit=1) == 1
        cycle.fit(bytes(range(32)), cycle.build(settings(), parts, polled=True,
                                                session="ok"), memory)
        _, _, again = self.cars(tmp_path, ("300000001", "2019 Honda civic si"),
                                ("300000002", "2018 Honda civic lx"), plan=plan)
        assert cycle.pictures(cfg, plan, again, memory, cdn, limit=1) == 1
        cycle.fit(bytes(range(32)), cycle.build(settings(), again, polled=True,
                                                session="ok"), memory)
        _, _, third = self.cars(tmp_path, ("300000001", "2019 Honda civic si"),
                                ("300000002", "2018 Honda civic lx"), plan=plan)
        assert cycle.pictures(cfg, plan, third, memory, cdn, limit=1) == 0
        assert sorted(cdn.calls) == ["https://cdn.example/300000001.jpg",
                                     "https://cdn.example/300000002.jpg"]

    @pytest.mark.parametrize("mode", ["403", "html", "big", "raise"])
    def test_a_photo_that_will_not_come_is_given_up_on(self, tmp_path, mode):
        cfg, plan, parts = self.cars(tmp_path, ("300000001", "2019 Honda civic si"))
        cdn, memory = FakeCDN(mode), {}
        assert cycle.pictures(cfg, plan, parts, memory, cdn) == 0
        assert "photo_data" not in parts[0]["listings"][0]
        assert memory["cars"]["300000001"]["no_picture"]
        assert cycle.pictures(cfg, plan, parts, memory, cdn) == 0
        assert len(cdn.calls) == 1

    def test_a_pass_carries_them_and_says_so(self, home):
        summary = a_pass(home, FakeBrowser())
        assert summary["pictures"] == 1 and home.cdn.calls == ["https://cdn.example/200000001.jpg"]
        [rec] = home.last()["searches"][0]["listings"]
        assert rec["photo_data"] and rec["photo_type"] == "image/webp"
        assert "1 pictures" in cycle._line(summary)
        # The next pass carries no picture for it again.
        summary = a_pass(home, FakeBrowser())
        assert summary["pictures"] == 0
        assert "photo_data" not in home.last()["searches"][0]["listings"][0]


class TestTheSend:
    """A dispatch carries 65,535 characters; a bigger batch goes as a blob."""

    class Session:
        def __init__(self, blobs_ok=True):
            self.calls, self.blobs_ok = [], blobs_ok
            self.headers = {}

        def post(self, url, json=None, timeout=None):
            self.calls.append((url, json))
            if url.endswith("/git/blobs"):
                if not self.blobs_ok:
                    return type("R", (), {"status_code": 403, "text": "no",
                                          "json": lambda s: {}})()
                return type("R", (), {"status_code": 201, "text": "",
                                      "json": lambda s: {"sha": "a" * 40}})()
            return type("R", (), {"status_code": 204, "text": ""})()

    def test_a_small_batch_goes_in_the_dispatch(self):
        from collector.github import GitHub
        http = self.Session()
        gh = GitHub("someone/watch", "t", session=http)
        assert gh.send("x" * 100) == {"blob": None, "bytes": 100}
        [(url, body)] = http.calls
        assert url.endswith("/dispatches") and body["client_payload"]["batch"] == "x" * 100

    def test_a_big_one_goes_up_as_a_blob_the_dispatch_names(self):
        from collector.github import GitHub
        http = self.Session()
        gh = GitHub("someone/watch", "t", session=http)
        big = "x" * (M.PAYLOAD_LIMIT + 1)
        assert gh.send(big) == {"blob": "a" * 40, "bytes": len(big)}
        (up, blob), (dispatch, body) = http.calls
        assert up.endswith("/repos/someone/watch/git/blobs")
        assert blob == {"content": big, "encoding": "utf-8"}
        assert dispatch.endswith("/dispatches")
        assert body["client_payload"] == {"from": "marketplace-collector",
                                          "blob": "a" * 40, "bytes": len(big)}

    def test_a_refused_upload_is_said_and_nothing_is_dispatched(self):
        from collector.github import GitHub
        http = self.Session(blobs_ok=False)
        gh = GitHub("someone/watch", "t", session=http)
        with pytest.raises(GitHubError, match="uploading the batch"):
            gh.send("x" * (M.PAYLOAD_LIMIT + 1))
        assert len(http.calls) == 1


@contextmanager
def _in(path):
    import os
    before = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(before)


# ============================================================ a whole pass

class FakeGitHub:
    """The vault's files, and every batch sent. ``refuse`` fails the send."""

    def __init__(self, files):
        self.files, self.sent, self.refuse = files, [], False

    def file(self, path, ref="vault"):
        return self.files[path]

    def send(self, sealed):
        if self.refuse:
            raise GitHubError("sending the batch: HTTP 503, unavailable")
        self.sent.append(sealed)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A watch in a vault, as the collector fetches it, and its own folder."""
    repo = tmp_path / "repo"
    repo.mkdir()
    watch_config(repo)
    vault = V.Vault.unlock(repo, {V.ENV_KEY: PHRASE}, create=True)
    vault.seal_file(repo / "config.json", "config.enc")
    monkeypatch.setenv("COLLECTOR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(S.ENV_PASSPHRASE, PHRASE)
    monkeypatch.setenv("COLLECTOR_NO_KEYCHAIN", "1")

    def state(**hosts):
        """The bot's state as the vault holds it, with these hosts heard:
        name=(role, minutes ago, session)."""
        doc = State(path=repo / "state.json")
        doc.data["marketplace"] = {"hosts": {
            name: {"role": role, "session": session,
                   "received": (clock.now() - timedelta(minutes=ago)).isoformat(
                       timespec="seconds")}
            for name, (role, ago, session) in hosts.items()}}
        doc.save()
        vault.seal_file(repo / "state.json", "state.enc")
        github.files["state.enc"] = (repo / "vault" / "state.enc").read_bytes()

    github = FakeGitHub({p.name: p.read_bytes() for p in (repo / "vault").iterdir()
                         if p.is_file()})
    cdn = FakeCDN()
    monkeypatch.setattr(cycle, "PHOTO_HTTP", lambda: cdn)
    h = type("Home", (), {})()
    h.github, h.key, h.state, h.cdn = github, vault.key, state, cdn
    h.last = lambda: M.open_batch(vault.key, github.sent[-1])
    return h


def a_pass(home, browser, **kw):
    kw.setdefault("settings", settings())
    return cycle.once(kw.pop("settings"), github=home.github,
                      browser_factory=lambda s: browser, **kw)


class TestExplainSeesWhatAPassFound:
    """explain read with a memory of its own, so what a pass had found on a
    car's own page was left out: a car the bot hides for its rebuilt title
    was listed as on your list."""

    def test_a_car_its_page_hid_is_hidden_there_too(self, tmp_path, monkeypatch):
        from collector.explain import explain, explain_text
        repo = tmp_path / "repo"
        repo.mkdir()
        watch_config(repo, exclude_keywords=["rebuilt"])
        vault = V.Vault.unlock(repo, {V.ENV_KEY: PHRASE}, create=True)
        vault.seal_file(repo / "config.json", "config.enc")
        monkeypatch.setenv("COLLECTOR_HOME", str(tmp_path / "home"))
        monkeypatch.setenv(S.ENV_PASSPHRASE, PHRASE)
        monkeypatch.setenv("COLLECTOR_NO_KEYCHAIN", "1")
        github = FakeGitHub({p.name: p.read_bytes() for p in (repo / "vault").iterdir()
                             if p.is_file()})
        cycle.once(settings(), github=github,
                   browser_factory=lambda s: FakeBrowser(status="REBUILT"))
        [car] = M.open_batch(vault.key, github.sent[-1])["searches"][0]["listings"]
        assert car["title_status"] == "Rebuilt", "the bot hides it"
        kept = cycle.load_memory()
        result = explain(settings(), github=github,
                         browser_factory=lambda s: FakeBrowser(status="REBUILT"))
        [search] = result["searches"]
        assert [l.id for l, _ in search["judged"].hidden] == ["fb-200000001"]
        assert search["judged"].kept == []
        assert "hidden by a rule (1):" in explain_text(result)
        assert cycle.load_memory() == kept, "explaining changes nothing it keeps"
        assert len(github.sent) == 1


class TestHoldingOff:
    """After Facebook signs the collector out or asks the account to confirm
    who it is, every read would land on the same page again."""

    def test_the_passes_after_a_checkpoint_only_check_in(self, home):
        summary = a_pass(home, FakeBrowser(session={"search": "checkpoint"}))
        assert summary["polled"] and summary["session"] == "checkpoint"
        later = FakeBrowser()
        summary = a_pass(home, later)
        assert later.opened == 0 and later.log == []
        sent = home.last()
        # Still said, so the bot keeps its alarm and a standby covers.
        assert sent["polled"] is False and sent["session"] == "checkpoint"
        assert "confirm who it is" in sent["note"] and "collector/run login" in sent["note"]

    def test_and_after_a_sign_out(self, home):
        a_pass(home, FakeBrowser(signed_in=False))
        later = FakeBrowser()
        a_pass(home, later)
        assert later.opened == 0
        sent = home.last()
        assert sent["polled"] is False and sent["session"] == "signed_out"
        assert "signed the collector out" in sent["note"]

    def test_the_bot_is_told_when_the_batch_that_found_it_was_lost(
            self, home, tmp_path, monkeypatch):
        sink = Capture()
        use_channels(monkeypatch, runner_mod, [sink])
        bot = Config.defaults(tmp_path / "bot.json")
        state = State(path=tmp_path / "bot-state.json")
        home.github.refuse = True
        with pytest.raises(GitHubError):
            a_pass(home, FakeBrowser(signed_in=False))
        home.github.refuse = False
        for _ in range(4):
            a_pass(home, FakeBrowser())
            assert home.last()["polled"] is False
            M.ingest(bot, state, home.last(), env={})
        assert len(sink.alerts_matching("sign in again")) == 1

    def test_once_now_reads_and_clears_it(self, home):
        a_pass(home, FakeBrowser(session={"search": "checkpoint"}))
        assert cycle.load_memory()["held"]["session"] == "checkpoint"
        a_pass(home, FakeBrowser(), force=True)
        assert home.last()["polled"] is True and home.last()["session"] == "ok"
        assert "held" not in cycle.load_memory()
        after = FakeBrowser()
        a_pass(home, after)
        assert after.searches()

    def test_it_ends_after_a_while(self, home):
        a_pass(home, FakeBrowser(session={"search": "checkpoint"}))
        clock.freeze(clock.now() + timedelta(hours=cycle.HOLD_HOURS, minutes=1))
        try:
            later = FakeBrowser()
            a_pass(home, later)
        finally:
            clock.freeze(None)
        assert later.searches()

    def test_signing_in_again_ends_it(self, home, monkeypatch):
        from collector import __main__ as cli
        from collector import browser as B
        a_pass(home, FakeBrowser(session={"search": "checkpoint"}))
        assert "held" in cycle.load_memory()
        monkeypatch.setattr(B, "login", lambda cfg: True)
        monkeypatch.setattr(S.Settings, "load", classmethod(lambda cls: settings()))
        assert cli.main(["login"]) == 0
        assert "held" not in cycle.load_memory()


class TestASendThatFails:

    def test_the_pages_it_opened_are_not_opened_again(self, home):
        home.github.refuse = True
        with pytest.raises(GitHubError):
            a_pass(home, FakeBrowser())
        mine = cycle.load_memory()["cars"]["200000001"]
        assert mine["tries"] == 1 and mine["detail"] and mine["facts"]
        # The photo did not go, so it is not counted.
        assert "photos" not in mine
        home.github.refuse = False
        again = FakeBrowser()
        a_pass(home, again)
        assert again.items() == []
        # What the page said goes with the batch that did arrive.
        [car] = home.last()["searches"][0]["listings"]
        assert car["mileage_km"] == 51234

    def test_after_a_few_it_only_checks_in_until_one_is_taken(self, home):
        home.github.refuse = True
        for _ in range(cycle.SEND_TRIES):
            with pytest.raises(GitHubError):
                a_pass(home, FakeBrowser())
        waiting = FakeBrowser()
        with pytest.raises(GitHubError):
            a_pass(home, waiting)
        assert waiting.log == []
        home.github.refuse = False
        summary = a_pass(home, waiting)
        assert not summary["polled"] and summary["note"] == "waiting for GitHub to accept a batch"
        reading = FakeBrowser()
        assert a_pass(home, reading)["polled"] and reading.searches()


class TestAStandby:

    def standby(self):
        return settings(role="standby", host="collector-b")

    def test_stands_by_while_the_primary_reads(self, home):
        home.state(**{"collector-a": ("primary", 20, "ok"),
                      "collector-b": ("standby", 20, "ok")})
        browser = FakeBrowser()
        summary = a_pass(home, browser, settings=self.standby())
        assert not summary["polled"] and summary["note"] == "standing by for collector-a"
        assert browser.log == []

    @pytest.mark.parametrize("session", ["signed_out", "checkpoint"])
    def test_reads_for_a_primary_facebook_will_not_let_in(self, home, session):
        home.state(**{"collector-a": ("primary", 20, session),
                      "collector-b": ("standby", 20, "ok")})
        browser = FakeBrowser()
        summary = a_pass(home, browser, settings=self.standby())
        assert summary["polled"] and browser.searches()
        assert home.last()["role"] == "standby"

    def test_does_not_read_while_the_bot_takes_nothing_in(self, home):
        # The primary is reading; the bot is what stopped, so neither has
        # been heard from. Two Macs reading would double the load for nothing.
        home.state(**{"collector-a": ("primary", 180, "ok"),
                      "collector-b": ("standby", 180, "ok")})
        browser = FakeBrowser()
        summary = a_pass(home, browser, settings=self.standby())
        assert not summary["polled"] and browser.log == []
        assert "has not taken in" in summary["note"]

    def test_a_new_standby_checks_in_before_it_takes_over(self, home):
        home.state(**{"collector-a": ("primary", 180, "ok")})
        browser = FakeBrowser()
        assert not a_pass(home, browser, settings=self.standby())["polled"]
        assert browser.log == []

    def test_takes_over_from_a_quiet_primary(self, home):
        home.state(**{"collector-a": ("primary", 180, "ok"),
                      "collector-b": ("standby", 20, "ok")})
        browser = FakeBrowser()
        assert a_pass(home, browser, settings=self.standby())["polled"]
        assert browser.searches()


class TestAHungPass:
    """The Playwright driver died under a pass on Oct 26 2026 and the process
    sat in the call for four hours with launchd showing it running."""

    def test_a_pass_that_outlives_its_deadline_is_ended(self, monkeypatch):
        import threading
        ended = threading.Event()
        monkeypatch.setattr(cycle, "next_wait", lambda s: 0.0)
        monkeypatch.setattr(cycle.time, "sleep", lambda s: None)
        release = threading.Event()

        def hang(settings, **kw):
            assert ended.wait(5), "the watchdog never fired"
            release.set()
            raise RuntimeError("driver gone")

        monkeypatch.setattr(cycle, "once", hang)
        monkeypatch.setattr(cycle, "load_memory", lambda: {})
        monkeypatch.setattr(cycle, "save_memory", lambda m: None)
        cycle.forever(settings(), deadline=0.05, rounds=1,
                      give_up=lambda started: ended.set())
        assert ended.is_set() and release.is_set()

    def test_a_pass_that_finishes_in_time_is_left_alone(self, monkeypatch):
        fired = []
        monkeypatch.setattr(cycle, "next_wait", lambda s: 0.0)
        monkeypatch.setattr(cycle.time, "sleep", lambda s: None)
        monkeypatch.setattr(cycle, "once", lambda s, **kw: {"polled": False, "note": "x"})
        cycle.forever(settings(), deadline=0.2, rounds=2, give_up=fired.append)
        import time as real
        real.sleep(0.4)
        assert fired == []

    def test_giving_up_ends_the_process_and_says_why(self, monkeypatch):
        import subprocess
        notes, exits, ran = {}, [], []
        monkeypatch.setattr(cycle, "load_memory", lambda: {})
        monkeypatch.setattr(cycle, "save_memory", lambda m: notes.update(m))
        monkeypatch.setattr(cycle.os, "_exit", exits.append)
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: ran.append(a[0]))
        cycle._give_up(cycle.time.monotonic())
        assert exits == [75] and ran and ran[0][0] == "pkill"
        assert "hung" in notes["last_error"]["error"]
