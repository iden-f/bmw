"""Politeness: this scrapes somebody else's website, on a schedule, forever."""
import time

import pytest
import requests

from autotrader.config import Config
from autotrader.http import BASE_HEADERS, DEFAULT_USER_AGENT, BudgetExhausted, Fetcher
from autotrader.runner import run
from autotrader.state import State

from .helpers import FakeFetcher


class OfflineSession:
    """A session that answers instantly, so pacing can be measured."""

    def __init__(self, body="<html></html>", status=200):
        self.headers = {}
        self.body = body
        self.status = status
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        response = requests.Response()
        response.status_code = self.status
        response._content = self.body.encode()
        response.url = url
        return response

    def close(self):
        pass


def fetcher(**kw):
    kw.setdefault("delay_ms", 0)
    kw.setdefault("retries", 0)
    f = Fetcher(**kw)
    f.session = OfflineSession()
    return f


class TestRequestBudget:
    def test_the_budget_is_a_hard_stop(self):
        f = fetcher(budget=5)
        for _ in range(5):
            f.get("https://www.autotrader.ca/cars/")
        with pytest.raises(BudgetExhausted):
            f.get("https://www.autotrader.ca/cars/")
        assert f.session.calls == 5

    def test_retries_count_against_the_budget(self):
        """Three attempts at one URL are three requests to the site."""
        f = fetcher(budget=3, retries=5)
        f.session = OfflineSession(status=503)
        with pytest.raises(BudgetExhausted):
            f.get("https://www.autotrader.ca/cars/")
        assert f.session.calls == 3

    def test_photo_downloads_are_billed_too(self):
        f = fetcher(budget=2)
        f.get_bytes("https://cdn.example/a.jpg")
        f.get_bytes("https://cdn.example/b.jpg")
        assert f.get_bytes("https://cdn.example/c.jpg") is None
        assert f.session.calls == 2

    def test_a_photo_over_budget_returns_none_rather_than_raising(self):
        f = fetcher(budget=1)
        f.get_bytes("https://cdn.example/a.jpg")
        assert f.get_bytes("https://cdn.example/b.jpg") is None

    def test_zero_means_unlimited(self):
        f = fetcher(budget=0)
        for _ in range(30):
            f.get("https://www.autotrader.ca/cars/")
        assert f.session.calls == 30

    def test_the_remaining_allowance_is_visible(self):
        f = fetcher(budget=10)
        f.get("https://www.autotrader.ca/cars/")
        assert f.budget_left == 9
        assert f.stats["spent"] == 1


class TestRunnerRespectsTheBudget:
    def test_a_run_stops_scraping_once_the_budget_is_spent(self, tmp_path, monkeypatch,
                                                           fixture_html):
        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "c.json")
        for i in range(6):
            cfg.add_search(f"https://www.autotrader.ca/cars/honda/model-{i}/?rcp=15",
                           f"Search {i}")
        cfg.set("scraping.delay_ms", 0)
        cfg.set("scraping.max_pages", 5)
        cfg.set("scraping.enrich_details", False)

        spy = FakeFetcher(fixture_html("search_cards"), budget=4)
        original = spy.get

        def metered(url, referer=None, allow_block=False):
            if spy.spent >= spy.budget:
                raise BudgetExhausted("allowance spent")
            return original(url, referer, allow_block)

        spy.get = metered
        report = run(cfg, State.load(tmp_path / "s.json"), fetcher=spy, notify=False)

        assert spy.spent <= 4
        assert report.budget_exhausted
        assert report.searches_run < 6, "it must stop early, not scrape everything"

    def test_running_out_of_budget_is_not_treated_as_a_broken_search(self, tmp_path,
                                                                    monkeypatch, fixture_html):
        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "c.json")
        cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=15", "Civic")
        cfg.add_search("https://www.autotrader.ca/cars/toyota/corolla/?rcp=15", "Corolla")
        cfg.set("scraping.delay_ms", 0)

        spy = FakeFetcher(fixture_html("search_cards"), budget=1)
        original = spy.get

        def metered(url, referer=None, allow_block=False):
            if spy.spent >= spy.budget:
                raise BudgetExhausted("allowance spent")
            return original(url, referer, allow_block)

        spy.get = metered
        state = State.load(tmp_path / "s.json")
        report = run(cfg, state, fetcher=spy, notify=False)

        assert report.searches_failed == 0, "we chose to stop; nothing is broken"
        for search in cfg.active_searches:
            assert state.search_health(search.id)["consecutive_failures"] == 0

    def test_detail_lookups_are_capped_independently(self, tmp_path, monkeypatch,
                                                     fixture_html, archive_html):
        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "c.json")
        cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=15", "Civic")
        cfg.set("scraping.delay_ms", 0)
        cfg.set("scraping.enrich_limit", 1)

        details = {i: archive_html(i) for i in ("13166607", "68819631", "13221555")}
        spy = FakeFetcher(fixture_html("search_cards"), details)
        run(cfg, State.load(tmp_path / "s.json"), fetcher=spy, notify=False)

        detail_calls = [u for u in spy.urls if "/a/" in u]
        assert len(detail_calls) == 1, "enrich_limit must cap detail fetches"

    def test_the_request_count_is_reported(self, bench):
        report = bench.run()
        assert report.requests_made > 0
        assert report.to_dict()["requests_made"] == report.requests_made


class TestPoliteness:
    def test_requests_are_spaced_out(self):
        f = fetcher(delay_ms=120)
        started = time.monotonic()
        for _ in range(3):
            f.get("https://www.autotrader.ca/cars/")
        elapsed = (time.monotonic() - started) * 1000
        # Two gaps of at least 120 ms between three requests.
        assert elapsed >= 200, f"requests were not paced ({elapsed:.0f} ms)"

    def test_the_delay_is_jittered_not_metronomic(self):
        """Identical gaps are a fingerprint; vary them."""
        f = fetcher(delay_ms=60)
        gaps = []
        for _ in range(6):
            before = time.monotonic()
            f.get("https://www.autotrader.ca/cars/")
            gaps.append(round(time.monotonic() - before, 4))
        assert len(set(gaps)) > 1, "every gap was identical"

    def test_the_bot_does_not_announce_itself_as_a_bot(self):
        assert "bot" not in DEFAULT_USER_AGENT.lower()
        assert "scrap" not in DEFAULT_USER_AGENT.lower()

    def test_it_sends_the_headers_a_browser_would(self):
        f = Fetcher()          # a real session, not the offline stand-in
        try:
            assert f.session.headers["User-Agent"] == DEFAULT_USER_AGENT
            assert f.session.headers["Accept-Language"].startswith("en-CA")
            for header in ("Accept", "Accept-Language", "Sec-Fetch-Mode"):
                assert header in BASE_HEADERS
        finally:
            f.close()

    def test_backoff_grows_between_retries(self, monkeypatch):
        slept = []
        monkeypatch.setattr("autotrader.http.time.sleep", lambda s: slept.append(s))
        f = Fetcher(retries=3, delay_ms=0)
        f.session = OfflineSession(status=503)
        with pytest.raises(Exception):
            f.get("https://www.autotrader.ca/cars/")
        real = [s for s in slept if s > 1]
        assert real == sorted(real), f"backoff did not increase: {real}"
        assert max(real) <= 32, "backoff must stay bounded"

    def test_the_default_config_ships_a_budget(self):
        assert Config.defaults().get("scraping.request_budget", 0) > 0
        assert Config.defaults().get("scraping.delay_ms", 0) > 0
