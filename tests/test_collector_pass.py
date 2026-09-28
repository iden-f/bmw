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
        first, second = browser.searches()
        assert "query=Honda+Civic" in first and "query=Honda+Accord" in second
        assert browser.log == [first, REST, second]

    def test_and_between_searches_and_before_a_listing(self, tmp_path):
        cfg = watch_config(tmp_path, searches=2)
        browser = FakeBrowser()
        cycle.read(cfg, M.plan(cfg), browser, settings(), {})
        assert [v == REST for v in browser.log] == [False, True, False, True, False]

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
                if len(self.searches()) == 1 and "/search/" in url:
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

    def test_photos_go_before_any_car(self):
        # A first read of three searches: no photo sent yet, no card dated,
        # and photo addresses as random as Facebook's.
        parts = [{"search": s, "scope": "x", "ok": True, "error": "", "landed": "",
                  "listings": [{"id": str(300000000 + 1000 * n + i),
                                "title": "2018 Honda civic",
                                "photo": "https://cdn.example/" + secrets.token_hex(150)}
                               for i in range(150)]}
                 for n, s in enumerate(("s1", "s2", "s3"))]
        memory: dict = {}
        sent = cycle.build(settings(), parts, polled=True, session="ok")
        sealed = cycle.fit(self.key(), sent, memory)
        assert len(sealed) <= M.PAYLOAD_LIMIT
        opened = M.open_batch(self.key(), sealed)["searches"]
        assert [len(p["listings"]) for p in opened] == [150, 150, 150]
        with_photo = [[bool(r.get("photo")) for r in p["listings"]] for p in opened]
        # The newest cars of every search keep theirs, not the first search's
        # cars all of theirs.
        assert all(flags[0] and not flags[-1] for flags in with_photo)
        # A photo left out is not counted as sent: a later batch carries it.
        last = opened[2]["listings"][-1]["id"]
        assert "photos" not in memory["cars"].get(last, {})
        assert memory["cars"][opened[2]["listings"][0]["id"]]["photos"] == 1


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
    h = type("Home", (), {})()
    h.github, h.key, h.state = github, vault.key, state
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
