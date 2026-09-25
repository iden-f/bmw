"""Failure-mode sweep: the things that actually go wrong on real infrastructure."""
import errno
import json
import os
import time
from datetime import datetime

from pathlib import Path

import pytest

from autotrader import notifiers, runner as runner_mod
from autotrader.archive import archive_listing
from autotrader.config import Config
from autotrader.lock import AlreadyRunning, run_lock
from autotrader.listing import Listing
from autotrader.notifiers import Notifier, Result, in_quiet_hours
from autotrader.state import Change, State

from .helpers import a_check_later

SEARCH = "https://www.autotrader.ca/cars/honda/civic/?rcp=15&srt=35&prx=-2&loc=K1P"


# ---------------------------------------------------------------- state files

class TestCorruptState:
    @pytest.mark.parametrize("content", [
        "", "   ", "{", "{ this is not json", "null", "[]", '{"listings": ',
        '{"version": 2, "listings": {"1": ', "\x00\x01binary garbage",
    ])
    def test_unreadable_state_never_raises(self, tmp_path, monkeypatch, content):
        monkeypatch.chdir(tmp_path)
        path = tmp_path / "state.json"
        path.write_text(content, errors="ignore")
        state = State.load(path)
        assert isinstance(state.listings, dict)

    def test_a_truncated_file_is_kept_for_inspection(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path = tmp_path / "state.json"
        good = State(path=path)
        good.record(Listing(id="1", url="u", title="t", price=1000,
                            price_source="detail", search_id="s"))
        good.save()
        whole = path.read_text()
        path.write_text(whole[: len(whole) // 2])       # truncated mid-write
        State.load(path)
        assert (tmp_path / "state.corrupt.json").exists()

    def test_state_with_a_wrong_shape_is_rejected_not_trusted(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"listings": {"1": {}}}))   # no version marker
        assert State.load(path).listings == {}

    def test_a_listing_entry_missing_fields_does_not_crash(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"version": 2, "listings": {"1": {"id": "1"}}}))
        state = State.load(path)
        assert state.get_listing("1").id == "1"
        assert state.stats()["total"] == 1
        state.prune()

    def test_unknown_future_fields_are_ignored(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"version": 2, "listings": {
            "1": {"id": "1", "url": "u", "invented_by_a_later_version": True}}}))
        assert State.load(path).get_listing("1").id == "1"


class TestDiskProblems:
    def test_a_full_disk_during_archiving_does_not_kill_the_run(self, tmp_path, monkeypatch):
        def no_space(*args, **kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr("pathlib.Path.write_text", no_space)
        listing = Listing(id="1", url="u", title="t")
        assert archive_listing(listing, {"mode": "metadata"}, root=tmp_path) is None

    @pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                        reason="root ignores directory permissions")
    def test_a_read_only_archive_directory_is_survivable(self, tmp_path):
        target = tmp_path / "ro"
        target.mkdir()
        target.chmod(0o500)
        try:
            assert archive_listing(Listing(id="1", url="u"), {"mode": "metadata"},
                                   root=target) is None
        finally:
            target.chmod(0o700)

    def test_a_failed_state_save_is_reported_not_swallowed(self, tmp_path, monkeypatch):
        state = State(path=tmp_path / "nope" / "deep" / "state.json")
        with pytest.raises(OSError):
            state.save()

    def test_the_lock_is_skipped_rather_than_fatal_when_undiskable(self, tmp_path):
        with run_lock(tmp_path / "missing-dir" / "x.lock") as held:
            assert held is False


# ---------------------------------------------------------------- notifiers

class TestNotifierFailures:
    def _cfg(self):
        return Config.defaults()

    def test_a_429_is_reported_not_raised(self):
        class RateLimited(Notifier):
            name = "rl"
            def _send(self, changes, run):
                raise RuntimeError("HTTP 429: Too Many Requests")

        results = notifiers.dispatch(self._cfg(), [_change()],
                                     notifiers=[RateLimited({}, {}, {})])
        assert results[0].ok is False and "429" in results[0].detail

    def test_a_rate_limited_alert_is_retried_on_the_next_run(self, bench, monkeypatch):
        class RateLimited(Notifier):
            name = "rl"
            def _send(self, changes, run):
                raise RuntimeError("HTTP 429: Too Many Requests")

        real = notifiers.dispatch
        monkeypatch.setattr(runner_mod.notifiers, "dispatch",
                            lambda c, ch, r=None, e=None, n=None:
                            real(c, ch, r, e, [RateLimited({}, {}, {})]))
        assert bench.run().new == 3

        monkeypatch.setattr(runner_mod.notifiers, "dispatch",
                            lambda c, ch, r=None, e=None, n=None: real(c, ch, r, e, [bench.sink]))
        bench.run()
        assert len(bench.sink.digests[0]) == 3

    def test_one_channel_rate_limited_does_not_hold_up_the_others(self):
        class RateLimited(Notifier):
            name = "rl"
            def _send(self, changes, run): raise RuntimeError("HTTP 429")

        class Works(Notifier):
            name = "ok"
            def _send(self, changes, run): return Result("ok", True)

        results = notifiers.dispatch(self._cfg(), [_change()],
                                     notifiers=[RateLimited({}, {}, {}), Works({}, {}, {})])
        assert [r.ok for r in results] == [False, True]

    def test_a_partial_success_still_counts_as_delivered(self, bench, monkeypatch):
        """One working channel means the alert is not re-sent to everyone later."""
        class Broken(Notifier):
            name = "broken"
            def _send(self, changes, run): raise RuntimeError("down")

        real = notifiers.dispatch
        monkeypatch.setattr(runner_mod.notifiers, "dispatch",
                            lambda c, ch, r=None, e=None, n=None:
                            real(c, ch, r, e, [Broken({}, {}, {}), bench.sink]))
        bench.run()
        bench.sink.digests.clear()
        bench.run()
        assert bench.sink.digests == []

    def test_verify_never_raises(self):
        class Exploding(Notifier):
            name = "boom"
            def _verify(self): raise RuntimeError("kaboom")
        assert Exploding({}, {}, {}).verify().ok is False


# ---------------------------------------------------------------- interruption

class TestInterruptedRuns:
    def test_an_interrupt_mid_archive_still_saves_state(self, bench, monkeypatch):
        calls = {"n": 0}

        def die_on_second(listing, config, fetcher=None, html=None, root=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt()
            return None

        monkeypatch.setattr(runner_mod.archive_mod, "archive_listing", die_on_second)
        bench.cfg.set("archive.mode", "metadata")
        with pytest.raises(KeyboardInterrupt):
            bench.run()
        # The finally block must have run.
        saved = json.loads((bench.path / "state.json").read_text())
        assert saved["listings"], "state must survive an interrupt"

    def test_cars_found_before_an_interrupt_are_delivered_exactly_once(self, bench, monkeypatch):
        """The interrupt lands after two cars were detected but before anything
        was sent. Those two are owed to the user, and must arrive once."""
        calls = {"n": 0}

        def die_on_second(listing, config, fetcher=None, html=None, root=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt()
            return None

        monkeypatch.setattr(runner_mod.archive_mod, "archive_listing", die_on_second)
        bench.cfg.set("archive.mode", "metadata")
        with pytest.raises(KeyboardInterrupt):
            bench.run()
        assert bench.sink.digests == [], "nothing was sent before the interrupt"

        bench.run()
        delivered = {c.listing.id for batch in bench.sink.digests for c in batch}
        assert len(delivered) == 3, "all three cars must arrive, none lost"

        bench.sink.digests.clear()
        assert bench.run().new == 0
        assert bench.sink.digests == [], "and never a second time"

    def test_a_lock_stops_a_second_run_from_interleaving(self, tmp_path):
        path = tmp_path / "run.lock"
        with run_lock(path):
            with pytest.raises(AlreadyRunning):
                with run_lock(path):
                    pass

    def test_a_lock_from_a_dead_process_is_reclaimed(self, tmp_path):
        path = tmp_path / "run.lock"
        path.write_text(json.dumps({"pid": 2 ** 22, "started_at": time.time()}))
        with run_lock(path) as held:
            assert held

    def test_a_lock_left_by_a_hung_run_is_eventually_reclaimed(self, tmp_path):
        path = tmp_path / "run.lock"
        path.write_text(json.dumps({"pid": os.getpid(), "started_at": time.time() - 99999}))
        with run_lock(path, stale_after=60) as held:
            assert held


# ---------------------------------------------------------------- clock

class TestClockSkew:
    WINDOW = {"quiet_hours": {"enabled": True, "start": "23:00", "end": "07:00"},
              "timezone": "America/Toronto"}

    @pytest.mark.parametrize("hour,quiet", [(22, False), (23, True), (0, True),
                                            (3, True), (6, True), (7, False), (8, False)])
    def test_a_window_crossing_midnight(self, hour, quiet):
        assert in_quiet_hours(self.WINDOW, datetime(2026, 3, 8, hour, 0)) is quiet

    def test_a_window_inside_one_day(self):
        window = {"quiet_hours": {"enabled": True, "start": "09:00", "end": "17:00"}}
        assert in_quiet_hours(window, datetime(2026, 3, 8, 12, 0))
        assert not in_quiet_hours(window, datetime(2026, 3, 8, 20, 0))

    def test_the_daylight_saving_jump_does_not_wedge_the_window(self):
        """2 a.m. does not exist on this date in Toronto."""
        for hour in range(0, 5):
            in_quiet_hours(self.WINDOW, datetime(2026, 3, 8, hour, 30))

    @pytest.mark.parametrize("bad", [
        {"start": "25:00", "end": "07:00"}, {"start": "nonsense", "end": "07:00"},
        {"start": "", "end": ""}, {"start": None, "end": None},
        {"start": "23:00"}, {},
    ])
    def test_a_malformed_window_never_silences_notifications(self, bad):
        settings = {"quiet_hours": {"enabled": True, **bad}}
        assert in_quiet_hours(settings, datetime(2026, 3, 8, 2, 0)) is False

    def test_an_unknown_timezone_falls_back_instead_of_crashing(self):
        settings = {"quiet_hours": {"enabled": True, "start": "23:00", "end": "07:00"},
                    "timezone": "Mars/Olympus_Mons"}
        assert isinstance(in_quiet_hours(settings), bool)

    def test_a_zero_length_window_is_never_quiet(self):
        settings = {"quiet_hours": {"enabled": True, "start": "09:00", "end": "09:00"}}
        assert not in_quiet_hours(settings, datetime(2026, 3, 8, 9, 0))


# ---------------------------------------------------------------- listings

class TestListingOddities:
    def test_the_same_id_twice_in_one_page_is_recorded_once(self, tmp_path):
        state = State(path=tmp_path / "s.json")
        first = state.record(Listing(id="1", url="u", title="a", price=1000,
                                     price_source="search", search_id="s"))
        second = state.record(Listing(id="1", url="u", title="a", price=1000,
                                      price_source="search", search_id="s"))
        assert first.kind == Change.NEW and second is None
        assert len(state.listings) == 1

    def test_a_car_that_disappears_and_returns_is_not_announced_twice(self, tmp_path):
        state = State(path=tmp_path / "s.json")
        car = Listing(id="1", url="u", title="a", price=1000,
                      price_source="detail", search_id="s")
        assert state.record(car).kind == Change.NEW
        state.mark_notified(["1"])

        state.mark_missing("s", set())
        a_check_later()
        state.mark_missing("s", set())
        assert state.listings["1"]["status"] == "gone"

        # It comes back - relisted, or it simply fell off page 1 for a while.
        # Worth recording as its own kind of event, because a watcher that
        # keeps resurrecting cars is telling you its removal detection is
        # wrong. What it must never be is a second discovery.
        change = state.record(car)
        assert change is not None and change.kind == Change.RELISTED
        assert change.kind != Change.NEW
        assert state.listings["1"]["status"] == "active"
        assert state.listings["1"]["notified"] is True
        assert state.listings["1"]["relisted_at"]

    def test_a_relisting_is_off_by_default_so_it_cannot_become_noise(self):
        from autotrader.config import Config
        assert Config.defaults(Path("/tmp/unused-config.json")).get(
            "notifications.notify_on.relisted") is False

    def test_a_car_that_returns_cheaper_still_reports_the_drop(self, tmp_path):
        """The drop is never lost. It is now told as the better story.

        This used to assert PRICE_DROP, on the reasoning that the price move
        is the useful fact and the relisting is context. That reading loses
        something real: a seller who pulls a car and puts it back $8,000
        cheaper has done more than edit a live listing, and reporting the two
        identically throws the difference away. So the kind is now RELISTED -
        carrying the same delta, counted in the same price_drops total, and
        gated by the same price-drop switch.
        """
        state = State(path=tmp_path / "s.json")
        state.record(Listing(id="1", url="u", price=100000, price_source="detail", search_id="s"))
        state.mark_missing("s", set())
        a_check_later()
        state.mark_missing("s", set())
        change = state.record(Listing(id="1", url="u", price=92000,
                                      price_source="detail", search_id="s"))
        assert change.kind == Change.RELISTED
        assert change.delta == -8000
        assert "$8,000 cheaper" in change.describe()

    def test_the_miss_counter_resets_when_a_car_reappears(self, tmp_path):
        state = State(path=tmp_path / "s.json")
        car = Listing(id="1", url="u", price=1000, price_source="detail", search_id="s")
        state.record(car)
        state.mark_missing("s", set())
        state.record(car)                      # seen again
        assert state.mark_missing("s", set()) == []
        assert state.listings["1"]["status"] == "active"

    def test_a_listing_with_no_id_is_never_stored(self, fixture_html):
        from autotrader.parser import parse_search_page
        result = parse_search_page(
            '<a href="/a/honda/civic/x/on/">no id here</a>'
            '<a href="/a/honda/civic/x/on/5_123_z/">too short</a>',
            "https://www.autotrader.ca/")
        assert len(result) == 0


def _change():
    return Change(Change.NEW, Listing(id="1", url="https://www.autotrader.ca/a/x/19_1_/",
                                      title="2021 Honda Civic", price=30000))
