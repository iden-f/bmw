"""The bot driven at the cadence it actually gets, not the one it asks for.

MEASURED, on this repository, over 10 hours 32 minutes in which nothing but
GitHub's own cron started the bot: five windows were due, two were served,
and the longest gap between checks was 4 hours 42 minutes. Coverage 40%.
GitHub drops whole windows, in runs, rather than individual firings.

Every rule in this bot that counts - a car is gone, a search is broken, the
watcher has gone quiet, a day of the ledger is closed - was written against a
tidy interval. This file drives the same rules at the cadence above, and at
the opposite extreme: a push and a scheduled firing landing three minutes
apart, which is not deduplicated and happens whenever anyone touches the
repository.

The gaps below are the measured ones, rounded to the minute; the date is not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autotrader import clock, events, insight, runner as runner_mod
from autotrader.config import Config
from autotrader.runner import run
from autotrader.state import State

from .helpers import Capture, FakeFetcher, use_channels
from .test_transitions import page, uuid_for

ROOT = Path(__file__).resolve().parent.parent

#: Minutes from the first check to each of the ones that followed, from the
#: measured window. The 282-minute step is the 4h42m gap.
REAL_GAPS = [0, 41, 282, 43, 266]
START = "2026-03-02T03:07:00Z"


class Watcher:
    """One configured bot, driven check by check on a clock you control."""

    def __init__(self, tmp_path, monkeypatch, **settings):
        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.add_search(f"{BASE}?rcp=25", "Example search")
        cfg.set("scraping.delay_ms", 0)
        cfg.set("scraping.enrich_details", False)
        cfg.set("archive.mode", "off")
        cfg.set("dashboard.enabled", False)
        for key, value in settings.items():
            cfg.set(key.replace("__", "."), value)
        cfg.save()
        self.cfg = cfg
        self.path = tmp_path
        self.sink = Capture()
        use_channels(monkeypatch, runner_mod, [self.sink])
        clock.freeze(START)

    def check(self, cars, *, after_minutes=0, scheduled=True):
        """One check, that many minutes after the previous one."""
        from datetime import timedelta
        if after_minutes:
            clock.advance(timedelta(minutes=after_minutes))
        env = ({"AUTOTRADER_SCHEDULED": "1", "RUN_TRIGGER": "schedule"}
               if scheduled else {"RUN_TRIGGER": "push"})
        return run(self.cfg, State.load(self.path / "state.json"),
                   fetcher=FakeFetcher(page(cars)), env=env, force=True)

    def state(self):
        return State.load(self.path / "state.json")

    def entry(self, short):
        return self.state().listings[uuid_for(short)]


BASE = "https://www.autotrader.ca/cars/honda/civic/"


@pytest.fixture
def watcher(tmp_path, monkeypatch):
    def make(**settings):
        return Watcher(tmp_path, monkeypatch, **settings)
    return make


TWO = [{"id": "a", "price": 70000}, {"id": "b", "price": 71000}]
ONE = [{"id": "b", "price": 71000}]


class TestACarThatVanishesInTheGap:
    def test_it_is_not_called_gone_on_the_first_check_that_misses_it(self, watcher):
        """Even after four hours and forty-two minutes of not looking.

        One absence is one page of results, however long ago the last one was.
        The site rotates which cars surface; the clock says nothing about that.
        """
        w = watcher()
        w.check(TWO)
        w.check(ONE, after_minutes=282)
        assert w.entry("a")["status"] == "active"
        assert w.entry("a")["misses"] == 1

    def test_and_is_on_the_next_one(self, watcher):
        w = watcher()
        w.check(TWO)
        w.check(ONE, after_minutes=282)
        report = w.check(ONE, after_minutes=43)
        assert w.entry("a")["status"] == "gone"
        assert report.removed == 1

    def test_two_checks_three_minutes_apart_prove_nothing(self, watcher):
        """A push and a cron firing in the same window. Only the scheduled one
        is deduplicated, so this pair is a supported way to reach two misses -
        in three minutes, which is not evidence that a car sold."""
        w = watcher()
        w.check(TWO)
        w.check(ONE, after_minutes=41)
        report = w.check(ONE, after_minutes=3, scheduled=False)
        assert report.removed == 0
        assert w.entry("a")["status"] == "active"

    def test_but_the_clock_alone_is_not_enough_either(self, watcher):
        """Nine hours of not looking is nine hours of no new evidence."""
        w = watcher()
        w.check(TWO)
        report = w.check(ONE, after_minutes=9 * 60)
        assert report.removed == 0, "one miss is one miss, whenever it happened"

    def test_a_car_that_comes_back_across_the_gap_is_never_called_gone(self, watcher):
        w = watcher()
        w.check(TWO)
        w.check(ONE, after_minutes=282)
        w.check(TWO, after_minutes=43)
        w.check(ONE, after_minutes=266)
        assert w.entry("a")["status"] == "active", \
            "the miss counter resets when the car is seen again"


class TestAPriceThatMovesInTheGap:
    def test_the_drop_is_found_whole_however_long_the_gap(self, watcher):
        """The bot compares against what it stored, not against what it saw
        recently, so a gap costs the timing of the alert and nothing else."""
        w = watcher()
        w.check(TWO)
        report = w.check([{"id": "a", "price": 58000},
                          {"id": "b", "price": 71000}], after_minutes=282)
        assert report.price_drops == 1
        change = next(c for c in w.sink.digests[-1]
                      if c.listing.id == uuid_for("a"))
        assert change.delta == -12000, "the whole move, not the part it saw"

    def test_two_moves_inside_one_gap_are_reported_as_the_net_move(self, watcher):
        """It cannot report what it did not see, and it does not pretend to.

        A car that went 70,000 -> 64,000 -> 66,000 while nobody was looking is
        a $4,000 drop as far as this bot can honestly say, and its price
        history holds the two figures it actually observed rather than three.
        """
        w = watcher()
        w.check(TWO)
        w.check([{"id": "a", "price": 66000}, {"id": "b", "price": 71000}],
                after_minutes=282)
        change = next(c for c in w.sink.digests[-1]
                      if c.listing.id == uuid_for("a"))
        assert change.delta == -4000
        history = [p["price"] for p in w.entry("a")["price_history"]]
        assert history == [70000, 66000]


class TestWhatTheNumbersSayAfterwards:
    def run_the_measured_day(self, w):
        for n, gap in enumerate(REAL_GAPS):
            w.check(TWO, after_minutes=gap)
        return w.state()

    def test_coverage_reports_the_windows_that_were_served(self, watcher):
        """Five checks over 10h32m against a schedule asking for one every two
        hours. Not "five checks, therefore fine"."""
        w = watcher(health__expected_interval_minutes=120)
        state = self.run_the_measured_day(w)
        cover = insight.coverage(state.data["runs"], expected_minutes=120,
                                 window_hours=24,
                                 since_change=state.schedule_changed_at)
        assert cover["partial"] is True, \
            "ten hours of history is not a day, and must not be labelled one"
        assert cover["window_hours"] < 24
        assert cover["checks"] == 5
        assert cover["slots_covered"] <= cover["expected"]
        assert 0 < cover["pct"] <= 100

    def test_the_longest_gap_is_the_one_that_happened(self, watcher):
        w = watcher(health__expected_interval_minutes=120)
        state = self.run_the_measured_day(w)
        cover = insight.coverage(state.data["runs"], expected_minutes=120,
                                 window_hours=24,
                                 since_change=state.schedule_changed_at)
        assert 275 <= cover["longest_gap_minutes"] <= 290, cover

    def test_a_burst_of_checks_does_not_buy_coverage(self, watcher):
        """Four checks inside one window cover one window."""
        w = watcher(health__expected_interval_minutes=120)
        w.check(TWO)
        for _ in range(3):
            w.check(TWO, after_minutes=5, scheduled=False)
        state = w.state()
        cover = insight.coverage(state.data["runs"], expected_minutes=120,
                                 window_hours=24, since_change=None)
        assert cover["checks"] == 4
        assert cover["slots_covered"] <= 1


class TestTheSilenceAlarmKnowsWhichSilenceItIs:
    def test_a_thin_schedule_is_named_as_one(self, watcher):
        """Six hours of quiet on a schedule that serves 40% of its windows is
        a normal-looking day. Sending someone to the Actions tab to find
        nothing wrong is how a channel gets muted."""
        from datetime import timedelta
        w = watcher(health__expected_interval_minutes=120,
                    health__silent_after_hours=6)
        for gap in REAL_GAPS:
            w.check(TWO, after_minutes=gap)
        clock.advance(timedelta(hours=7))
        said = events.silence(w.cfg, w.state(), {})
        assert said and not said["failing"]
        assert "dropping windows" in said["body"], said["body"]
        # Pointed at the fix itself, which is there to be run. (That the
        # README's Schedule section shows how is test_keep_time's to check.)
        assert "scripts/keep-time.sh" in said["body"]
        assert (ROOT / "scripts" / "keep-time.sh").is_file()

    def test_a_schedule_that_was_working_until_now_is_named_as_that(self, watcher):
        from datetime import timedelta
        w = watcher(health__expected_interval_minutes=120,
                    health__silent_after_hours=6)
        for _ in range(6):
            w.check(TWO, after_minutes=120)
        clock.advance(timedelta(hours=7))
        said = events.silence(w.cfg, w.state(), {})
        assert said and not said["failing"]
        assert "something has changed" in said["body"], said["body"]
        assert "Actions tab" in said["body"]

    def test_it_counts_the_windows_that_were_missed(self, watcher):
        from datetime import timedelta
        w = watcher(health__expected_interval_minutes=120,
                    health__silent_after_hours=6)
        w.check(TWO)
        clock.advance(timedelta(hours=8))
        said = events.silence(w.cfg, w.state(), {})
        assert said and "4 missed windows" in said["body"], said["body"]


class TestTheLedgerAcrossAMidnightNobodyWasAwakeFor:
    def test_runs_either_side_of_a_gap_land_on_their_own_days(self, watcher):
        """The 4h42m gap in the measured window crossed 05:00 UTC. A longer
        one crosses midnight, and a ledger that counts per day has to put each
        run on the day it happened rather than on the day it is asked."""
        from datetime import timedelta
        from autotrader import budget
        w = watcher()
        clock.freeze("2026-03-02T22:30:00Z")
        w.check(TWO)
        clock.advance(timedelta(hours=5))          # 03:30 the next day
        w.check(TWO)
        days = w.state().data.get("actions", {}).get("days", {})
        assert set(days) == {"2026-03-02", "2026-03-03"}, days
        ledger = budget.load(w.state(), w.cfg)
        assert ledger.days_elapsed == 2, \
            "two days produced data, whatever the day of the month is"
        assert ledger.used == round(
            sum(sum(d.values()) for d in days.values()), 2)

    def test_it_refuses_to_blame_the_schedule_it_cannot_see(self, watcher):
        """Runs with no recorded trigger are not evidence in either direction.

        A repository being worked on produces pushes and hand runs; records
        written before the bot learned to note its own trigger carry none at
        all. Read as "0% of firings served", that confidently blamed GitHub
        for dropping windows it may never have been asked for - on the one
        channel whose credibility the whole silence alarm exists to protect.
        """
        from datetime import timedelta
        w = watcher(health__expected_interval_minutes=120,
                    health__silent_after_hours=6)
        for _ in range(4):
            w.check(TWO, after_minutes=120, scheduled=False)
        # Strip the triggers, as a state file written by an older build has.
        state = w.state()
        for record in state.data["runs"]:
            record.pop("trigger", None)
        state.save()
        clock.advance(timedelta(hours=7))
        said = events.silence(w.cfg, State.load(w.path / "state.json"), {})
        assert said
        assert "cannot tell you" in said["body"], said["body"]
        assert "dropping windows" not in said["body"], \
            "it does not know that, and must not say it"
