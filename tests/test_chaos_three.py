"""The four the previous rounds did not break.

Round one broke the parse and the state file. Round two broke the CDN, the
platform, a channel, the clock and the commit. These are the ones left on the
list: the allowance running out, a token being revoked, the site rate-limiting
the bot, and the network dying in the middle of a run.

Each one is held to the same three things, which is the whole standard this
project has for a fault:

  * it degrades VISIBLY - the page and the run report say what happened;
  * the message carries the REAL error, not a category;
  * it heals WITHOUT A HUMAN once the fault clears.

Anything that needs somebody to go and do something is a bug, not a
scenario.
"""
from __future__ import annotations

import json

import pytest

from autotrader import budget, clock, runner as runner_mod
from autotrader.config import Config
from autotrader.http import BlockedError, FetchError, Response
from autotrader.runner import run
from autotrader.state import State

from .helpers import Capture, FakeFetcher, next_check, use_channels

BASE = "https://www.autotrader.ca/cars/honda/civic"


@pytest.fixture
def bench(tmp_path, monkeypatch, fixture_html):
    monkeypatch.chdir(tmp_path)
    cfg = Config.defaults(tmp_path / "config.json")
    cfg.add_search(f"{BASE}?rcp=25", "Example search")
    cfg.set("scraping.delay_ms", 0)
    cfg.set("scraping.retries", 0)
    cfg.set("scraping.enrich_details", False)
    cfg.set("archive.mode", "off")
    cfg.set("dashboard.enabled", False)
    cfg.save()

    sink = Capture()
    use_channels(monkeypatch, runner_mod, [sink])
    html = fixture_html("search_next_data")

    def go(page=None, *, fetcher=None, **kw):
        next_check(kw.pop("minutes_later", None))
        return run(cfg, State.load(tmp_path / "state.json"),
                   fetcher=fetcher or FakeFetcher(page if page is not None else html),
                   env=kw.pop("env", {}), **kw)

    return type("Bench", (), {
        "cfg": cfg, "sink": sink, "run": staticmethod(go), "path": tmp_path,
        "html": html,
        "state": staticmethod(lambda: State.load(tmp_path / "state.json")),
        "raw": staticmethod(lambda: json.loads(
            (tmp_path / "state.json").read_text(encoding="utf-8"))),
    })


# ------------------------------------------------ 9. the allowance runs out
class TestTheAllowanceRunsOut:
    """The bot stops itself rather than spending someone's money quietly.

    It stops by writing a FILE, not by calling an API: a file is visible in
    the repository, survives a token with no actions: write, and is removed
    by hand by whoever decides the spending is fine. The workflow reads it
    before it installs anything, so a stopped bot costs nothing at all.
    """

    def spend_the_month(self, bench, share=1.0):
        """Write a month of history at that share of the allowance."""
        state = bench.state()
        ledger = budget.load(state, bench.cfg)
        now = clock.now()
        month = f"{now.year:04d}-{now.month:02d}"
        per_day = ledger.allowance * share / 3
        state.data["actions"] = {
            "month": month,
            "days": {f"{month}-{d:02d}": {"drawing": per_day} for d in (1, 2, 3)},
            "runs": 400,
        }
        state.save()

    def test_it_writes_the_stop_file_rather_than_spending_past_the_ceiling(self, bench):
        bench.run()
        self.spend_the_month(bench)
        bench.run()
        stop = bench.path / budget.STOP_FILE
        assert stop.exists(), "it spent past the ceiling without stopping"
        said = stop.read_text(encoding="utf-8")
        assert "3,000" in said or "3000" in said, said
        assert budget.STOP_FILE in said or "delete" in said.lower(), \
            "the file has to say how to undo it"

    def test_the_workflow_reads_it_before_it_installs_anything(self):
        """A stopped bot that still spends a minute checking is not stopped."""
        text = (
            __import__("pathlib").Path(".github/workflows/watch.yml")
            .read_text(encoding="utf-8"))
        guard = text.index(budget.STOP_FILE)
        for step in ("actions/setup-python", "pip install"):
            assert text.index(step) > guard, \
                f"{step} runs before the budget guard"

    def test_it_says_so_on_every_channel(self, bench):
        bench.run()
        self.spend_the_month(bench)
        bench.sink.alerts.clear()
        bench.run()
        said = " ".join(b for _, b in bench.sink.alerts)
        assert "stop" in said.lower() or "budget" in said.lower(), bench.sink.alerts

    def test_deleting_the_file_is_all_it_takes_to_resume(self, bench):
        bench.run()
        self.spend_the_month(bench)
        bench.run()
        (bench.path / budget.STOP_FILE).unlink()
        # A new month, which is what actually happens: the ledger is keyed by
        # month, so the day it turns over there is nothing spent.
        state = bench.state()
        state.data["actions"] = {"month": "1999-01", "days": {}, "runs": 0}
        state.save()
        report = bench.run()
        assert report.ok
        assert not (bench.path / budget.STOP_FILE).exists(), \
            "it stopped itself again on a month it has not spent"

    def test_an_exhausted_allowance_does_not_stop_an_exempt_repository(self, bench):
        """An account's 3,000 minutes can hit 100% while this repository's
        scheduled runs keep succeeding, because a public repository on a
        standard runner is not billed."""
        state = bench.state()
        now = clock.now()
        month = f"{now.year:04d}-{now.month:02d}"
        state.data["actions"] = {
            "month": month,
            "days": {f"{month}-{d:02d}": {"exempt": 2000.0} for d in (1, 2, 3)},
            "runs": 400}
        state.save()
        report = bench.run()
        assert report.ok
        assert not (bench.path / budget.STOP_FILE).exists(), \
            "6,000 exempt minutes are not 6,000 minutes of someone's allowance"


# ------------------------------------------------------- 10. a revoked token
class TestATokenIsRevoked:
    """Every token this bot uses can be revoked, and none of them stop it.

    The check itself needs no credential at all - it reads a public website -
    so a revoked token costs the bot the things around the check (publishing
    the page, saving the vault) and never the check.
    """

    def test_the_check_needs_no_token(self, bench, monkeypatch):
        for name in ("GITHUB_TOKEN", "GH_TOKEN", "NTFY_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        report = bench.run(env={})
        assert report.ok and report.searches_run == 1

    def test_a_rejected_channel_credential_is_named_and_retired(self, bench):
        """Not "a channel failed": which channel, and what it said."""
        from autotrader.notifiers import Notifier, Result

        class Revoked(Notifier):
            name = "telegram"

            def __init__(self):
                super().__init__({}, {}, {})
                self.tries = 0

            def _send(self, changes, run):
                self.tries += 1
                return Result("telegram", False,
                              "401 Unauthorized: bot token is invalid")

            def _send_text(self, subject, body):
                return self._send(None, None)

        dead = Revoked()
        sink = Capture()
        use_channels(pytest.MonkeyPatch(), runner_mod, [dead, sink])
        report = bench.run()
        blamed = " ".join(str(r) for r in report.channel_results) + \
            " ".join(report.warnings)
        assert "401" in blamed or "invalid" in blamed, blamed

    def test_a_failed_publish_cannot_lose_the_check(self):
        """The page is a copy of the state file, and it is written after it.

        The publish step needs a token with write access; the check needs
        none. Ordering the workflow so the results are committed before the
        page is built means a revoked or downgraded token costs the copy and
        never the original - and the next run with a working token republishes
        it from state.json with nothing lost.
        """
        from pathlib import Path
        text = Path(".github/workflows/watch.yml").read_text(encoding="utf-8")
        checked = text.index("- name: Check\n")
        saved = text.index("- name: Save\n")
        published = text.index("- name: Publish the dashboard")
        assert checked < saved < published, (
            "the check and the commit of its results must both come before "
            "the step that needs a write token")

    def test_the_published_page_is_rebuilt_from_state_every_run(self):
        """So a run that could not publish is not a gap in the record - the
        next successful one republishes everything, not just its own changes."""
        from autotrader import dashboard
        assert "state" in dashboard.write.__code__.co_varnames, \
            "the page is built from the state file, not accumulated in place"


# ---------------------------------------------- 11. the site rate-limits it
class TestTheSiteRateLimitsIt:
    """429 is not a parse failure and must never be read as an empty market."""

    class Limited:
        """A site that answers 429 until `until`, then serves the page."""

        def __init__(self, html, fail_for=2):
            self.html, self.left = html, fail_for
            self.stats = {"requests": 0, "spent": 0, "budget": 0}
            self.spent = 0

        budget_left = 1_000_000

        def get(self, url, referer=None, allow_block=False):
            self.spent += 1
            self.stats["requests"] = self.spent
            if self.left > 0:
                self.left -= 1
                raise BlockedError(f"HTTP 429 from {url} - too many requests")
            return Response(url=url, status=200, elapsed_ms=1, text=self.html)

        def get_bytes(self, *a, **k):
            return None

        def close(self):
            pass

    def test_it_is_reported_as_a_block_not_as_no_cars(self, bench):
        site = self.Limited(bench.html)
        report = bench.run(fetcher=site)
        assert not report.ok
        assert "429" in " ".join(report.errors), report.errors
        assert report.listings_seen == 0

    def test_nothing_is_removed_on_a_page_it_could_not_read(self, bench):
        bench.run()
        before = {k: v["status"] for k, v in bench.raw()["listings"].items()}
        for _ in range(3):
            bench.run(fetcher=self.Limited(bench.html, fail_for=99))
        after = {k: v["status"] for k, v in bench.raw()["listings"].items()}
        assert after == before, "absence from a page it never read is not absence"

    def test_it_heals_on_its_own_when_the_site_relents(self, bench):
        site = self.Limited(bench.html, fail_for=1)
        first = bench.run(fetcher=site)
        assert not first.ok
        second = bench.run(fetcher=self.Limited(bench.html, fail_for=0))
        assert second.ok and second.listings_seen > 0
        assert bench.state().search_health(
            bench.cfg.searches[0].id)["consecutive_failures"] == 0

    def test_the_advice_is_about_the_thing_that_happened(self, bench):
        for _ in range(3):
            bench.run(fetcher=self.Limited(bench.html, fail_for=99))
        said = " ".join(b for _, b in bench.sink.alerts)
        assert "anti-bot" in said or "delay_ms" in said or "less often" in said, said


# ----------------------------------------- 14. the network dies mid-run
class TestTheNetworkDiesHalfwayThrough:
    """Not before the run and not after it: during."""

    class Partitioned:
        """Answers `ok_for` requests, then the network is gone."""

        def __init__(self, html, ok_for=1):
            self.html, self.ok_for = html, ok_for
            self.stats = {"requests": 0, "spent": 0, "budget": 0}
            self.spent = 0

        budget_left = 1_000_000

        def get(self, url, referer=None, allow_block=False):
            self.spent += 1
            self.stats["requests"] = self.spent
            if self.spent > self.ok_for:
                raise FetchError(f"connection reset by peer: {url}")
            return Response(url=url, status=200, elapsed_ms=1, text=self.html)

        def get_bytes(self, *a, **k):
            raise FetchError("connection reset by peer")

        def close(self):
            pass

    def test_what_it_read_before_the_partition_is_kept(self, bench):
        report = bench.run(fetcher=self.Partitioned(bench.html, ok_for=1))
        assert report.listings_seen > 0, "the page it did read is not thrown away"
        assert bench.raw()["listings"], "and is written down"

    def test_the_error_is_the_real_one(self, bench):
        report = bench.run(fetcher=self.Partitioned(bench.html, ok_for=0))
        assert "connection reset" in " ".join(report.errors), report.errors

    def test_a_partition_during_the_photos_does_not_fail_the_check(self, bench):
        bench.cfg.set("dashboard.enabled", True)
        bench.cfg.save()
        report = bench.run(fetcher=self.Partitioned(bench.html, ok_for=99))
        assert report.ok, report.errors

    def test_the_next_run_picks_up_where_it_stopped(self, bench):
        bench.run(fetcher=self.Partitioned(bench.html, ok_for=1))
        report = bench.run()
        assert report.ok
        assert bench.state().search_health(
            bench.cfg.searches[0].id)["consecutive_failures"] == 0

    def test_state_is_never_left_half_written(self, bench):
        """The runner saves in a finally block, and the save is atomic."""
        bench.run(fetcher=self.Partitioned(bench.html, ok_for=1))
        raw = (bench.path / "state.json").read_text(encoding="utf-8")
        json.loads(raw)                      # parses, or this raises
        assert not list(bench.path.glob("state.json.tmp*")), \
            "a temporary file left behind is a write that did not finish"
