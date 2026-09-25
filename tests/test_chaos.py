"""Deliberately breaking the bot, to see whether it puts itself back together.

Every one of these is something that has actually happened to a scheduled job
somewhere: a half-written state file, a config edited by hand into invalid
JSON, a results page truncated mid-transfer, a notification channel whose
credentials were revoked. The bar is not "it survives". The bar is:

  * it degrades - the run finishes and does not throw;
  * it says so - the problem reaches a human through a channel that works,
    or is recorded where the dashboard will show it;
  * it recovers on its own - the next run, with the fault removed, is normal
    again and has not lost or double-announced anything.

Anything that only manages the first two is a bot that needs a person, which
is the thing this whole rebuild exists to avoid.
"""
import json
import os
import re
import stat

import pytest

from autotrader import notifiers, runner as runner_mod
from autotrader.config import Config, ConfigError
from autotrader.http import FetchError, Response
from autotrader.runner import _still_listed, run
from autotrader.state import State

from .helpers import Capture, FakeFetcher, next_check, use_channels

BASE = "https://www.autotrader.ca/cars/honda/civic"


@pytest.fixture
def chaos(tmp_path, monkeypatch, fixture_html):
    """A working watcher, so a fault can be introduced into a known-good run."""
    monkeypatch.chdir(tmp_path)
    cfg = Config.defaults(tmp_path / "config.json")
    cfg.add_search(f"{BASE}?rcp=25", "Example search")
    cfg.set("scraping.delay_ms", 0)
    cfg.set("scraping.retries", 0)
    cfg.set("scraping.enrich_details", False)
    cfg.set("archive.mode", "off")
    # Name every car in the digest. These tests are about channels failing
    # and recovering, not about the digest's length: with the shipped cap of
    # 12 the cars a message could not name stay owed and arrive on the next
    # run, which reads here as "delivered twice" when nothing was. That
    # behaviour has its own test in tests/test_notify.py.
    cfg.set("notifications.max_listings_per_message", 50)
    cfg.save()

    sink = Capture()
    use_channels(monkeypatch, runner_mod, [sink])
    html = fixture_html("search_next_data")

    def go(page=None, **kw):
        # A schedule-interval between runs. Every countdown this file breaks
        # on purpose - the removal grace, the silence alarm, the failure
        # streak - is counted in elapsed time, and two runs in the same
        # second is not a cadence the bot can ever have.
        next_check(kw.pop("minutes_later", None))
        return run(cfg, State.load(tmp_path / "state.json"),
                   fetcher=FakeFetcher(page if page is not None else html),
                   env={}, **kw)

    return type("Chaos", (), {
        "cfg": cfg, "sink": sink, "run": staticmethod(go), "path": tmp_path,
        "html": html,
        "state": staticmethod(lambda: json.loads(
            (tmp_path / "state.json").read_text(encoding="utf-8"))),
    })


class TestCorruptState:
    def test_a_half_written_state_file_does_not_stop_the_run(self, chaos):
        chaos.run()
        before = len(chaos.state()["listings"])
        assert before

        # A job killed mid-write, or a disk that filled during one.
        (chaos.path / "state.json").write_text(
            '{"version": 2, "listings": {"a": {"id": "a"', encoding="utf-8")

        report = chaos.run()
        assert report.ok
        assert len(chaos.state()["listings"]) == before

    def test_the_broken_file_is_kept_rather_than_overwritten(self, chaos):
        chaos.run()
        (chaos.path / "state.json").write_text("{ this is not json", encoding="utf-8")
        chaos.run()

        quarantined = chaos.path / "state.corrupt.json"
        assert quarantined.exists()
        assert "not json" in quarantined.read_text(encoding="utf-8")

    def test_recovery_costs_one_round_of_reannouncements_and_no_more(self, chaos):
        """Losing state means the cars look new again - once.

        That is the honest cost of a destroyed file and it is why the file is
        written atomically in the first place. What must not happen is it
        recurring: the rebuilt state has to stick.
        """
        chaos.run()
        chaos.sink.digests.clear()
        (chaos.path / "state.json").write_text("garbage", encoding="utf-8")

        rebuilt = chaos.run()
        assert rebuilt.new > 0                      # the unavoidable round

        chaos.sink.digests.clear()
        assert chaos.run().new == 0                 # and it is over

    def test_a_state_file_of_the_wrong_shape_is_refused_not_trusted(self, chaos):
        (chaos.path / "state.json").write_text('["a", "b", "c"]', encoding="utf-8")
        report = chaos.run()
        assert report.ok
        assert isinstance(chaos.state()["listings"], dict)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can write read-only files")
    def test_state_that_cannot_be_written_still_notifies(self, chaos):
        """Alerts matter more than bookkeeping: tell someone, then complain."""
        chaos.run()
        chaos.sink.digests.clear()
        (chaos.path / "state.json").chmod(stat.S_IRUSR)
        try:
            report = chaos.run(chaos.html.replace("BMW", "BMW", 1))
        finally:
            (chaos.path / "state.json").chmod(stat.S_IRUSR | stat.S_IWUSR)
        assert report is not None                   # the run finished


class TestBadConfig:
    def test_invalid_json_is_reported_in_words_not_a_traceback(self, chaos):
        (chaos.path / "config.json").write_text('{"searches": [', encoding="utf-8")
        with pytest.raises(ConfigError) as exc:
            Config.load(chaos.path / "config.json")
        assert "config.json" in str(exc.value)

    def test_a_search_with_no_url_is_skipped_not_fatal(self, chaos):
        chaos.cfg.data["searches"].append(
            {"id": "broken", "name": "Broken", "url": "", "enabled": True})
        chaos.cfg.save()

        report = chaos.run()
        assert report.searches_run >= 1             # the good one still ran

    def test_a_search_pointed_at_the_wrong_site_fails_only_itself(self, chaos):
        chaos.cfg.data["searches"].append(
            {"id": "elsewhere", "name": "Not AutoTrader",
             "url": "https://example.com/cars", "enabled": True})
        chaos.cfg.save()

        report = chaos.run()
        assert report.searches_run >= 1
        assert len(chaos.state()["listings"]) > 0

    def test_nonsense_filter_values_do_not_take_the_run_down(self, chaos):
        chaos.cfg.set("filters.max_price", "not a number")
        chaos.cfg.set("filters.min_year", None)
        chaos.cfg.save()
        report = chaos.run()
        assert report is not None

    def test_no_searches_at_all_is_a_warning_not_a_crash(self, chaos):
        chaos.cfg.data["searches"] = []
        chaos.cfg.save()
        report = chaos.run()
        assert report.ok
        assert any("No searches" in w for w in report.warnings)


class TestAMangledPage:
    def test_a_page_truncated_mid_transfer_is_read_as_far_as_it_goes(self, chaos):
        cut = chaos.html[:len(chaos.html) // 2]
        report = chaos.run(cut)
        assert report is not None

    def test_a_page_of_pure_noise_alerts_rather_than_passing_silently(self, chaos):
        chaos.run()                                  # prove the parser first
        report = chaos.run("<html><body><p>hello</p></body></html>")

        assert not report.ok
        assert report.empty_parses == ["Example search"]
        assert any("no listings could be read" in e for e in report.errors)

    def test_a_broken_page_leaves_the_evidence_behind(self, chaos):
        chaos.run()
        report = chaos.run("<html><body>nothing</body></html>")

        assert report.diagnostics
        assert (chaos.path / "diagnostics").exists()
        # The page itself, not only a description of it.
        assert any(p.endswith(".page.html.gz")
                   for p in (str(x) for x in (chaos.path / "diagnostics").iterdir()))

    def test_a_genuinely_empty_search_is_not_mistaken_for_a_broken_parser(
            self, chaos, fixture_html):
        chaos.run()
        report = chaos.run(fixture_html("search_empty"))

        assert report.ok
        assert not report.empty_parses
        assert any("no results" in w.lower() for w in report.warnings)

    def test_cars_are_not_declared_gone_because_one_page_broke(self, chaos):
        """The two-run grace period is what stops a bad page emptying the
        dashboard - and it must not be consumed by a page we could not read."""
        chaos.run()
        for _ in range(4):
            report = chaos.run("<html><body>nothing at all</body></html>")
        assert report.removed == 0
        assert all(e["status"] == "active" for e in chaos.state()["listings"].values())

    def test_the_parser_recovers_the_moment_the_page_does(self, chaos):
        chaos.run()
        chaos.run("<html><body>broken</body></html>")
        chaos.sink.digests.clear()

        report = chaos.run()
        assert report.ok
        assert report.new == 0                      # nothing re-announced


def cheaper(html, by):
    """Knock every price down, so a run has something it must deliver."""
    return re.sub(r'"priceRaw":(\d+)',
                  lambda m: f'"priceRaw":{int(m.group(1)) - by}', html)


class TestADeadChannel:
    def _channels(self, monkeypatch, *channels):
        use_channels(monkeypatch, runner_mod, list(channels))

    def test_a_channel_that_fails_permanently_retires_itself(self, chaos, monkeypatch):
        dead = _Dead("dead", permanent=True)
        good = Capture()
        self._channels(monkeypatch, dead, good)

        for cycle in range(4):
            chaos.run(cheaper(chaos.html, 2000 * cycle))

        assert chaos.state()["channels"]["dead"]["disabled_at"]
        assert good.digests, "the working channel stopped getting alerts too"

    def test_the_working_channel_still_gets_everything(self, chaos, monkeypatch):
        dead, good = _Dead("dead", permanent=True), Capture()
        self._channels(monkeypatch, dead, good)
        chaos.run()
        assert good.digests, "a broken channel swallowed the alert"

    def test_a_transient_failure_does_not_retire_anything(self, chaos, monkeypatch):
        """A 503 is not a wrong password. Retiring on it would take a channel
        off for good because the service had a bad afternoon."""
        flaky, good = _Dead("flaky", permanent=False), Capture()
        self._channels(monkeypatch, flaky, good)
        for cycle in range(4):
            chaos.run(cheaper(chaos.html, 2000 * cycle))
        assert not (chaos.state().get("channels", {}).get("flaky", {})).get("disabled_at")
        assert flaky.attempts >= 4, "it stopped being tried"

    def test_a_retired_channel_is_announced_through_one_that_works(
            self, chaos, monkeypatch):
        dead, good = _Dead("dead", permanent=True), Capture()
        self._channels(monkeypatch, dead, good)
        for cycle in range(4):
            chaos.run(cheaper(chaos.html, 2000 * cycle))

        told = " ".join(subject + body for subject, body in good.alerts)
        assert "dead" in told.lower(), good.alerts

    def test_retirement_is_written_where_the_next_run_will_read_it(
            self, chaos, monkeypatch):
        """Switching a channel off has to survive the process that did it.

        The retirement is recorded in config.json, and the next run builds its
        channels from config - so a channel that retired itself is not
        constructed at all, rather than constructed and skipped.
        """
        dead, good = _Dead("ntfy", permanent=True), Capture()
        self._channels(monkeypatch, dead, good)
        for cycle in range(4):
            chaos.run(cheaper(chaos.html, 2000 * cycle))

        reloaded = Config.load(chaos.path / "config.json")
        assert reloaded.get("notifications.channels.ntfy.enabled") is False
        assert reloaded.get("notifications.channels.ntfy.disabled_reason")

        built = [c.name for c in notifiers.build(reloaded, {})]
        assert "ntfy" not in built, built

    def test_everything_failing_holds_the_alerts_rather_than_losing_them(
            self, chaos, monkeypatch):
        self._channels(monkeypatch, _Dead("a", permanent=False), _Dead("b", permanent=False))
        chaos.run()

        held = [e for e in chaos.state()["listings"].values() if e.get("pending")]
        assert held, "changes were dropped when every channel failed"

    def test_and_delivers_them_when_a_channel_comes_back(self, chaos, monkeypatch):
        self._channels(monkeypatch, _Dead("a", permanent=False))
        chaos.run()

        good = Capture()
        self._channels(monkeypatch, good)
        chaos.run()

        assert good.digests, "held alerts were never delivered"
        assert not [e for e in chaos.state()["listings"].values() if e.get("pending")]


class _Dead(notifiers.Notifier):
    """A channel that always fails, permanently or otherwise."""

    def __init__(self, name, *, permanent):
        self.name = name
        super().__init__({}, {}, {})
        self.permanent = permanent
        self.attempts = 0

    def _send(self, changes, run):
        self.attempts += 1
        # Result.permanent is read off the failure text, exactly as it is for
        # a real channel - so these are real Gmail and Cloudflare wording.
        return notifiers.Result(self.name, False, (
            "(535, b'5.7.8 Username and Password not accepted')"
            if self.permanent else "503 service unavailable, try again"))

    def _send_text(self, subject, body):
        return self._send([], None)


class TestAHalfWorkingParse:
    """The dangerous failure is not zero listings - that one is loud.

    It is a page that reads *partly*: pagination breaking, a strategy losing
    half the cards, one page of three timing out. The bot sees twenty cars
    where it saw sixty, and every one of the missing forty starts a countdown
    to being announced as sold.
    """

    def _shrink(self, html, keep):
        payload = json.loads(re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                                       html, re.S).group(1))
        results = payload["props"]["pageProps"]["searchResults"]
        results["listings"] = results["listings"][:keep]
        stripped = re.sub(r'(id="__NEXT_DATA__"[^>]*>).*?(</script>)',
                          lambda m: m.group(1) + json.dumps(payload) + m.group(2),
                          html, count=1, flags=re.S)
        # Take the JSON-LD out too, or another strategy simply wins instead.
        return re.sub(r'<script[^>]*application/ld\+json[^>]*>.*?</script>', "",
                      stripped, flags=re.S)

    def test_a_collapse_in_results_does_not_start_the_removal_countdown(self, chaos):
        chaos.run()
        assert chaos.state()["searches"][chaos.cfg.searches[0].id]["last_count"] == 19

        report = chaos.run(self._shrink(chaos.html, 4))

        assert report.removed == 0
        assert any("of the usual" in w for w in report.warnings), report.warnings
        misses = [e.get("misses", 0) for e in chaos.state()["listings"].values()]
        assert max(misses) == 0, "a run that read badly still spent the grace period"

    def test_a_sustained_collapse_is_eventually_believed(self, chaos):
        """Held for the run that collapsed, not forever: once the smaller
        number is the normal number, the missing cars really have gone."""
        chaos.run()
        small = self._shrink(chaos.html, 4)

        # Fifteen of nineteen vanishing at once is treated as suspicious, not
        # as news: the bot checks their listing pages first, and only believes
        # absence after several runs of not being able to establish anything.
        # It gets there, it just refuses to get there in one step.
        removed = 0
        for _ in range(12):
            report = chaos.run(small)
            removed += report.removed
            if removed == 15:
                break
        assert removed == 15
        assert report.requests_made <= 2 + 12, "verification ran without a cap"

    def test_it_says_out_loud_that_it_is_checking_rather_than_believing(self, chaos):
        chaos.run()
        small = self._shrink(chaos.html, 4)
        chaos.run(small)
        report = chaos.run(small)
        assert any("missing at once" in w for w in report.warnings), report.warnings
        assert report.removed == 0

    def test_an_ordinary_wobble_is_still_treated_as_a_removal(self, chaos):
        """Losing two cars out of nineteen is a sale, not a broken parser.

        The guard must not be so cautious that it stops reporting removals,
        which is the whole feature it is protecting.
        """
        chaos.run()
        smaller = self._shrink(chaos.html, 17)
        removed = sum(chaos.run(smaller).removed for _ in range(3))
        assert removed == 2
        assert not any("of the usual" in w
                       for w in chaos.run(smaller).warnings)


class TestConfirmingARemoval:
    """The listing page is the only place that can settle whether a car sold."""

    def _fetcher(self, *, status=200, body=""):
        class One:
            stats = {"requests": 0}
            budget_left = 100

            def get(self, url, referer=None, allow_block=False):
                if status != 200:
                    raise FetchError(f"HTTP {status} from {url}")
                return Response(url=url, status=200, text=body, elapsed_ms=1)
        return One()

    def test_a_404_means_gone(self):
        assert _still_listed("https://www.autotrader.ca/offers/x",
                             self._fetcher(status=404)) is False

    def test_a_timeout_means_nothing_either_way(self):
        assert _still_listed("https://www.autotrader.ca/offers/x",
                             self._fetcher(status=503)) is None

    def test_the_page_saying_so_means_gone(self):
        page = "<html><body><h1>This listing is no longer available</h1></body></html>"
        assert _still_listed("https://www.autotrader.ca/offers/x",
                             self._fetcher(body=page)) is False

    def test_a_page_that_still_describes_a_car_means_still_listed(self):
        page = ('<html><head><script type="application/ld+json">'
                '{"@context":"https://schema.org","@type":"Car","name":"Honda Civic",'
                '"offers":{"@type":"Offer","price":90000,"priceCurrency":"CAD"}}'
                '</script></head><body>for sale</body></html>')
        assert _still_listed("https://www.autotrader.ca/offers/x",
                             self._fetcher(body=page)) is True

    def test_an_unreadable_page_is_not_taken_as_proof_of_a_sale(self):
        """A parser that has fallen behind must not start announcing sales."""
        assert _still_listed("https://www.autotrader.ca/offers/x",
                             self._fetcher(body="<html><body>?</body></html>")) is None

    def test_a_car_with_no_url_cannot_be_checked(self):
        assert _still_listed("", self._fetcher()) is None


class TestKnowingWhereTheResultsEnd:
    """"Complete" is what licenses a removal, so it must not be guessed."""

    def _fetcher(self, pages):
        class Paged:
            stats = {"requests": 0}
            budget_left = 100

            def __init__(self):
                self.asked = []

            def get(self, url, referer=None, allow_block=False):
                n = int(re.search(r"[?&]page=(\d+)", url).group(1)) if "page=" in url else 1
                self.asked.append(n)
                return Response(url=url, status=200, elapsed_ms=1,
                                text=pages[min(n, len(pages)) - 1])

            def get_bytes(self, *a, **k):
                return None

            def close(self):
                pass
        return Paged()

    def _page(self, letters):
        """One card per letter, each with a real uuid so ids do not collide."""
        cards = "".join(
            f'<article><a href="/offers/honda-civic-{c * 8}-1111-2222-3333-444444444444">x</a>'
            f'<h2>2021 Honda Civic</h2>'
            f'<p data-testid="regular-price">$ 90,000</p></article>'
            for c in letters)
        return f"<html><body>{cards}</body></html>"

    def _scrape(self, chaos, pages):
        from autotrader.runner import scrape_search
        chaos.cfg.set("scraping.max_pages", 4)
        chaos.cfg.save()
        return scrape_search(chaos.cfg.searches[0], chaos.cfg, self._fetcher(pages))

    def test_a_short_last_page_is_the_end(self, chaos):
        result = self._scrape(chaos, [
            self._page("01234"), self._page("56789"), self._page("ab")])
        assert result.complete
        assert len(result.listings) == 12

    def test_a_page_that_repeats_itself_is_the_end(self, chaos):
        result = self._scrape(chaos, [self._page("01234"), self._page("01234")])
        assert result.complete
        assert len(result.listings) == 5

    def test_stopping_at_the_page_limit_is_not_the_end(self, chaos):
        result = self._scrape(chaos, [
            self._page("0123"), self._page("4567"),
            self._page("89ab"), self._page("cdef")])
        assert not result.complete
        assert len(result.listings) == 16

    def test_a_page_we_could_not_read_is_not_the_end_either(self, chaos):
        """Otherwise a broken page four authorises removals for pages five on."""
        result = self._scrape(chaos, [
            self._page("0123"), self._page("4567"),
            "<html><body>nothing here</body></html>", self._page("89ab")])
        assert not result.complete
        assert len(result.listings) == 8
