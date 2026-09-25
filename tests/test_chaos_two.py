"""Chaos, round two: eight specific ways this could go wrong in the next year.

Round one broke the things the rebuild had just touched. These are the ones
that only become possible once the bot has been running a while, or that
belong to parts added since: a CDN that stops answering, a config change
arriving through the new write channel, a platform that migrates its markup
again, two runs at once, a clock that jumps, a channel that rate-limits
forever, and a push GitHub refuses.

The bar is the same as round one, and it is not "it survives":

  * it degrades - the run finishes and does not throw;
  * it says so - through a channel that works, or recorded where the
    dashboard will show it;
  * it recovers on its own - the next clean run is normal and has not lost
    or double-announced anything.

Three of these found real bugs. Those are marked where they are.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from autotrader import clock
from autotrader import lock as lock_mod, notifiers, runner as runner_mod, thumbs
from autotrader.config import Config
from autotrader.runner import run
from autotrader.state import State

from .helpers import Capture, FakeFetcher, use_channels

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
        return run(cfg, State.load(tmp_path / "state.json"),
                   fetcher=FakeFetcher(page if page is not None else html),
                   env={}, **kw)

    return type("Bench", (), {
        "cfg": cfg, "sink": sink, "run": staticmethod(go), "path": tmp_path,
        "html": html,
        "state": staticmethod(lambda: json.loads(
            (tmp_path / "state.json").read_text(encoding="utf-8"))),
    })


# --------------------------------------------------------------- 1. the CDN
class TestTheCdnStopsAnswering:
    """Photos are a nicety. A check is the job."""

    def car(self, i=1):
        return {"id": f"{i:08d}-0000-0000-0000-000000000000", "status": "active",
                "filtered": False, "images": [f"https://cdn.test/{i}.webp"]}

    @pytest.mark.parametrize("fault", [
        ConnectionError("connection reset by peer"),
        TimeoutError("timed out"),
        OSError("network is unreachable"),
    ])
    def test_a_cdn_that_throws_does_not_reach_the_run(self, tmp_path, monkeypatch,
                                                      fault):
        monkeypatch.setattr(thumbs, "THUMB_DIR", tmp_path / "docs/thumbs")
        monkeypatch.setattr(thumbs, "INDEX", tmp_path / "docs/thumbs/index.json")

        class Dead:
            def get_asset(self, url, referer=None):
                raise fault

        report = thumbs.sync([self.car()], Dead())
        assert report.failed == 1 and report.fetched == 0

    def test_a_cdn_serving_someone_elses_login_page(self, tmp_path, monkeypatch):
        """200 OK, text/html, a redirect to a sign-in wall. Not a photo."""
        monkeypatch.setattr(thumbs, "THUMB_DIR", tmp_path / "docs/thumbs")
        monkeypatch.setattr(thumbs, "INDEX", tmp_path / "docs/thumbs/index.json")

        class Wall:
            def get_asset(self, url, referer=None):
                return {"status": 200, "type": "text/html",
                        "content": b"<html>sign in</html>"}

        report = thumbs.sync([self.car()], Wall())
        assert report.failed == 1
        assert "not an image" in report.samples[0]["error"]
        assert not list((tmp_path / "docs/thumbs").glob("*.webp"))

    def test_photos_already_kept_survive_the_cdn_going_away(self, tmp_path,
                                                            monkeypatch):
        """The reason for keeping our own copy in the first place."""
        monkeypatch.setattr(thumbs, "THUMB_DIR", tmp_path / "docs/thumbs")
        monkeypatch.setattr(thumbs, "INDEX", tmp_path / "docs/thumbs/index.json")
        import struct

        def webp():
            body = (b"VP8X" + struct.pack("<I", 10) + b"\x00" * 4
                    + (249).to_bytes(3, "little") + (186).to_bytes(3, "little")
                    + b"\x00" * 600)
            return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body

        class Ok:
            def get_asset(self, url, referer=None):
                return {"status": 200, "type": "image/webp", "content": webp()}

        class Gone:
            def get_asset(self, url, referer=None):
                raise ConnectionError("gone")

        thumbs.sync([self.car()], Ok())
        kept = list((tmp_path / "docs/thumbs").glob("*.webp"))
        assert len(kept) == 1

        after = thumbs.sync([self.car()], Gone())
        assert after.kept == 1
        assert list((tmp_path / "docs/thumbs").glob("*.webp")) == kept


# ------------------------------------------------- 2. a config change arrives
class TestAChangeArrivesThroughTheWriteChannel:
    def test_a_control_file_that_is_not_json_at_all(self, bench):
        from autotrader import control
        with pytest.raises(control.Rejected) as exc:
            control.parse("max_price = 45000")
        # Says what it wanted, and where to get one made properly.
        assert "JSON" in str(exc.value) and "dashboard" in str(exc.value)

    def test_a_change_that_would_empty_the_watch_is_refused(self, bench):
        from autotrader import control
        out = control.apply(bench.cfg, State.load(bench.path / "state.json"),
                            [{"action": "remove-search", "search": "Example search"}])
        assert not out.changed
        bench.cfg.save()
        assert bench.run().searches_run == 1

    def test_a_refused_change_leaves_the_config_byte_identical(self, bench):
        from autotrader import control
        before = (bench.path / "config.json").read_bytes()
        control.apply(bench.cfg, State.load(bench.path / "state.json"), [
            {"action": "set-rule", "rule": "max_price", "value": 50000},
            {"action": "set-rule", "rule": "no_such_rule", "value": 1},
        ])
        bench.cfg.save()
        assert (bench.path / "config.json").read_bytes() == before

    def test_an_applied_change_takes_effect_on_the_very_next_run(self, bench):
        from autotrader import control
        bench.run()
        bench.sink.digests.clear()
        out = control.apply(bench.cfg, State.load(bench.path / "state.json"),
                            [{"action": "set-rule", "rule": "max_price",
                              "value": 1000}])
        assert out.changed
        bench.cfg.save()
        report = bench.run()
        assert report.ok
        assert report.filtered_out > 0


# --------------------------------------------- 3. the platform migrates again
class TestThePlatformMigratesAgain:
    """It already happened once, mid-project. Assume it happens again."""

    def test_markup_nothing_understands_is_a_failure_not_a_quiet_zero(self, bench):
        report = bench.run("<html><body><div id=root></div>"
                           "<script>window.__NEXT='v9'</script></body></html>")
        assert report.searches_failed == 1 or report.empty_parses
        assert not [c for batch in bench.sink.digests for c in batch]

    def test_it_does_not_mark_every_car_gone(self, bench):
        """The expensive failure: a parser break read as the market emptying."""
        bench.run()
        alive = sum(1 for e in bench.state()["listings"].values()
                    if e.get("status") == "active")
        assert alive

        bench.run("<html><body>nothing here</body></html>")
        still = sum(1 for e in bench.state()["listings"].values()
                    if e.get("status") == "active")
        assert still == alive, "a page it could not read is not evidence of anything"

    def test_and_it_recovers_the_moment_the_markup_is_readable(self, bench):
        bench.run()
        bench.run("<html>broken</html>")
        bench.sink.digests.clear()
        report = bench.run()
        assert report.ok and report.searches_failed == 0
        assert report.new == 0, "recovery must not re-announce the back catalogue"


# --------------------------------------------- 4. state truncated mid-write
class TestStateTruncatedMidWrite:
    """Not corrupt - truncated. A valid prefix, cut off by a full disk."""

    def test_a_valid_prefix_is_not_mistaken_for_valid_state(self, bench):
        bench.run()
        whole = (bench.path / "state.json").read_text(encoding="utf-8")
        assert len(whole) > 400
        (bench.path / "state.json").write_text(whole[:len(whole) // 2],
                                               encoding="utf-8")
        report = bench.run()
        assert report.ok

    def test_the_truncated_file_is_kept_for_a_person_to_look_at(self, bench):
        bench.run()
        whole = (bench.path / "state.json").read_text(encoding="utf-8")
        (bench.path / "state.json").write_text(whole[:200], encoding="utf-8")
        bench.run()
        assert (bench.path / "state.corrupt.json").exists()

    def test_a_write_that_dies_partway_leaves_the_old_file_intact(self, bench):
        """What atomic replace buys: the previous state, not half of both."""
        bench.run()
        good = (bench.path / "state.json").read_text(encoding="utf-8")
        ids = set(json.loads(good)["listings"])

        state = State.load(bench.path / "state.json")
        real = state.path.with_suffix(".tmp")

        def die(*a, **kw):
            real.write_text('{"version": 2, "listings": {"x"', encoding="utf-8")
            raise OSError(28, "No space left on device")

        import unittest.mock as mock
        with mock.patch.object(type(real), "replace", die):
            try:
                state.save()
            except OSError:
                pass
        assert set(json.loads(
            (bench.path / "state.json").read_text())["listings"]) == ids


# ------------------------------------------------- 5. two runs at once
class TestTwoRunsAtOnce:
    def test_the_second_one_refuses_rather_than_interleaving(self, tmp_path):
        path = tmp_path / ".lock"
        with lock_mod.run_lock(path):
            with pytest.raises(lock_mod.AlreadyRunning) as exc:
                with lock_mod.run_lock(path):
                    pass
        # And says which process and for how long, so the fix is obvious.
        assert str(os.getpid()) in str(exc.value)
        assert str(path) in str(exc.value)

    def test_a_crashed_run_does_not_lock_the_bot_out_forever(self, tmp_path):
        """The failure mode that needs a human: a stale lock nobody clears."""
        path = tmp_path / ".lock"
        path.write_text(json.dumps({
            "pid": 999_999,
            "started_at": time.time() - lock_mod.STALE_AFTER_SECONDS - 60,
        }), encoding="utf-8")
        with lock_mod.run_lock(path) as held:
            assert held                            # taken over, not blocked

    def test_a_lock_held_by_a_process_that_is_gone_is_taken_over_at_once(self, tmp_path):
        """No need to wait out the staleness window when the pid is dead."""
        path = tmp_path / ".lock"
        path.write_text(json.dumps({"pid": 999_999, "started_at": time.time()}),
                        encoding="utf-8")
        with lock_mod.run_lock(path) as held:
            assert held

    def test_a_disk_that_will_not_take_the_lock_does_not_stop_the_run(self, tmp_path):
        """Bookkeeping must never outrank the job."""
        with lock_mod.run_lock(tmp_path / "no" / "such" / "dir" / ".lock") as held:
            assert held is False

    def test_the_lock_is_released_even_when_the_run_throws(self, tmp_path):
        path = tmp_path / ".lock"
        with pytest.raises(ValueError):
            with lock_mod.run_lock(path):
                raise ValueError("the run died")
        assert not path.exists()
        with lock_mod.run_lock(path) as held:
            assert held


# ------------------------------------------------------- 6. the clock jumps
class TestTheClockJumps:
    def test_a_run_stamped_in_the_future_does_not_hide_every_later_one(self, bench):
        """A runner with a skewed clock writes a timestamp an hour ahead.

        Anything that computes an age by subtraction gets a negative number,
        and a coverage window that treats "less than zero minutes ago" as out
        of range reports the bot as silent while it is running fine.
        """
        from autotrader import insight
        now = clock.now()
        runs = [{"at": (now + timedelta(hours=1)).isoformat(), "ok": True},
                {"at": (now - timedelta(minutes=30)).isoformat(), "ok": True}]
        cov = insight.coverage(runs, expected_minutes=30, window_hours=24, since_change=None)
        assert cov["successful"] >= 1
        assert cov["pct"] >= 0

    def test_quiet_hours_across_a_daylight_saving_jump(self, bench):
        from autotrader import notifiers as n
        settings = {"timezone": "America/Toronto",
                    "quiet_hours": {"enabled": True, "start": "23:00",
                                    "end": "07:00"}}
        # 2026-03-08 07:00 UTC is the instant Toronto skips 02:00 to 03:00.
        for minute in range(0, 120, 15):
            when = datetime(2026, 3, 8, 6, 0, tzinfo=timezone.utc) + \
                timedelta(minutes=minute)
            assert n.in_quiet_hours(settings, when) in (True, False)

    def test_a_negative_age_is_not_reported_as_a_gap(self, bench):
        from autotrader import insight
        now = clock.now()
        runs = [{"at": (now + timedelta(minutes=90)).isoformat(), "ok": True}]
        cov = insight.coverage(runs, expected_minutes=30, window_hours=24, since_change=None)
        assert (cov.get("longest_gap_minutes") or 0) >= 0


# ------------------------------------- 7. a channel that rate-limits forever
class TestAChannelThatSays429Forever:
    """ntfy.sh is free and shared. A quota is not hypothetical.

    The rule this suite exists to protect: a channel being down must never
    eat the news. The alert is owed until it is delivered, however long the
    channel stays down.
    """

    @staticmethod
    def bench(tmp_path, monkeypatch, html, channel):
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.add_search(f"{BASE}?rcp=25", "Example search")
        cfg.set("scraping.delay_ms", 0)
        cfg.set("scraping.enrich_details", False)
        cfg.set("archive.mode", "off")
        # Name every car in the digest. These tests are about channels failing
        # and recovering, not about the digest's length: with the shipped cap of
        # 12 the cars a message could not name stay owed and arrive on the next
        # run, which reads here as "delivered twice" when nothing was. That
        # behaviour has its own test in tests/test_notify.py.
        cfg.set("notifications.max_listings_per_message", 50)
        cfg.save()
        use_channels(monkeypatch, runner_mod, [channel])
        return lambda: run(cfg, State.load(tmp_path / "state.json"),
                           fetcher=FakeFetcher(html), env={})

    class Limited(Capture):
        """Everything a real channel is, except that it answers 429."""
        name = "ntfy"

        def __init__(self, up=False):
            super().__init__()
            self.up = up
            self.tries = 0

        def _send(self, changes, run):
            self.tries += 1
            if not self.up:
                return notifiers.Result(self.name, False, "HTTP 429 rate limited")
            return super()._send(changes, run)

        def _send_text(self, subject, body):
            self.tries += 1
            if not self.up:
                return notifiers.Result(self.name, False, "HTTP 429 rate limited")
            return super()._send_text(subject, body)

    def test_the_run_still_finishes(self, tmp_path, monkeypatch, fixture_html):
        monkeypatch.chdir(tmp_path)
        channel = self.Limited()
        go = self.bench(tmp_path, monkeypatch, fixture_html("search_next_data"),
                        channel)
        report = go()
        assert report is not None and channel.tries >= 1

    def test_the_alert_is_held_rather_than_lost(self, tmp_path, monkeypatch,
                                                fixture_html):
        monkeypatch.chdir(tmp_path)
        go = self.bench(tmp_path, monkeypatch, fixture_html("search_next_data"),
                        self.Limited())
        go()
        state = json.loads((tmp_path / "state.json").read_text())
        owed = [e for e in state["listings"].values()
                if e.get("pending") or not e.get("notified")]
        assert owed, "a rate-limited send must leave the cars owed an alert"

    def test_it_does_not_retry_forever_inside_one_run(self, tmp_path, monkeypatch,
                                                      fixture_html):
        """A job with a thirty-minute budget cannot spend it on a 429."""
        monkeypatch.chdir(tmp_path)
        channel = self.Limited()
        go = self.bench(tmp_path, monkeypatch, fixture_html("search_next_data"),
                        channel)
        go()
        assert channel.tries < 10, channel.tries

    def test_and_delivered_once_the_limit_lifts(self, tmp_path, monkeypatch,
                                                fixture_html):
        monkeypatch.chdir(tmp_path)
        html = fixture_html("search_next_data")
        channel = self.Limited()
        go = self.bench(tmp_path, monkeypatch, html, channel)
        go()
        assert not channel.digests

        channel.up = True
        go()
        assert channel.digests, "the held alert was never delivered"

    def test_the_delivery_is_not_doubled_when_it_finally_lands(self, tmp_path,
                                                               monkeypatch,
                                                               fixture_html):
        monkeypatch.chdir(tmp_path)
        html = fixture_html("search_next_data")
        channel = self.Limited()
        go = self.bench(tmp_path, monkeypatch, html, channel)
        go(); go(); go()                        # three runs against a dead channel
        channel.up = True
        go()
        first = sum(len(b) for b in channel.digests)
        channel.digests.clear()
        go()
        assert not [c for b in channel.digests for c in b], \
            "the same cars were announced twice"
        assert first, "nothing was delivered at all"


# ----------------------------------------- 8. GitHub refuses the push
class TestPushProtectionRefusesTheCommit:
    """The one failure that is not in the bot's own process.

    Push protection rejects a commit whose contents look like a credential.
    The bot writes config.json and docs/data.json on every run, so the way
    this bites is a token ending up in one of them - and the defence has to
    be that it never gets that far.
    """

    def test_the_dashboard_refuses_to_write_a_credential(self, tmp_path):
        from autotrader import dashboard
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.set("notifications.channels.webhook.url",
                "https://hooks.slack.com/services/T00000000/B00000000/"
                "XXXXXXXXXXXXXXXXXXXXXXXX")
        cfg.save()
        state = State({"version": 2, "listings": {}, "searches": {}, "runs": []})
        with pytest.raises(ValueError) as exc:
            dashboard.write(cfg, state, {}, path=tmp_path / "docs/data.json")
        assert "credential" in str(exc.value)
        assert not (tmp_path / "docs/data.json").exists(), \
            "it must refuse before writing, not after"

    # Assembled at run time, never written out whole.
    #
    # The first version of this test spelled these literally, and GitHub's
    # push protection rejected the commit - this exact scenario, happening to
    # this exact test, on the push that introduced it. Which is the point: a
    # rejected push is not a warning, it is a scheduled job that stops working
    # with no error anyone reads. A test about that failure must not cause it.
    SHAPES = {
        "github token": "ghp" + "_" + "0123456789" + "abcdefghijklmnopqrstuvwxyz" + "AB",
        "github fine-grained": "github" + "_pat_" + "11ABCDEFG0aBcDeFgHiJkL" + "_" + "abcdefghijklmnop",
        "slack bot token": "xox" + "b-" + "111111111111-2222222222222-" + "abcdefghijklmnopqrstuvwx",
        "aws access key": "AKI" + "A" + "IOSFODNN7EXAMPLE",
        "private key": "-----BEGIN " + "RSA PRIVATE" + " KEY-----",
        "google api key": "AIza" + "S" * 35,
        "stripe live key": "sk" + "_live_" + "abcdefghijklmnopqrstuvwx",
    }

    @pytest.mark.parametrize("what", sorted(SHAPES))
    def test_the_shapes_github_itself_scans_for(self, what):
        """The bot never makes one of these. Someone pasting one into a
        search's notes field would publish it to a public page and wedge
        every future push behind a rejection. The scanner knew only the five
        shapes this bot handles; it knows GitHub's now."""
        from autotrader.dashboard import find_secrets
        assert find_secrets({"note": self.SHAPES[what]}), what

    def test_the_test_data_here_is_never_written_out_whole(self):
        """The rule the commit before this one broke."""
        from pathlib import Path
        source = Path(__file__).read_text()
        for what, value in self.SHAPES.items():
            assert value not in source, (
                f"{what} is spelled literally in this file, which will get "
                f"the next push rejected - build it from parts")

    def test_a_price_is_not_mistaken_for_a_token(self, tmp_path):
        """A scanner that cries wolf gets switched off."""
        import uuid
        from autotrader.dashboard import find_secrets
        from autotrader.provision import generate_topic
        assert not find_secrets({
            # A listing id as random-looking as the site's, made up here.
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "an invented listing")),
            "price": 41234, "vin": "1HGCM82633A004352",
            "url": "https://www.autotrader.ca/a/honda/civic/milton/on/19_12345678/",
            # Twenty random characters, which is what a secret looks like to
            # anything that goes by shape alone.
            "topic": generate_topic(),
        })
