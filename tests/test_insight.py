from autotrader import clock


class TestCoverageMeasuresTheWatchNotTheExitCode:
    """The number claims to answer "could a car have come and gone unseen".

    It was counting runs whose exit code was zero, which is a different
    question. For a day and a half one bookkeeping invariant tripped on every
    run - the run fetched both searches, read two hundred listings and wrote
    them all down, then exited 1 - and this figure read 18.8% while the site
    was in fact being read every time a check happened. It said the market
    was unwatched. It meant the ledger was inconsistent. Those need separate
    numbers, because they need different fixes.
    """

    @staticmethod
    def run_at(minutes_ago, **over):
        from datetime import timedelta
        row = {"at": (clock.now()
                      - timedelta(minutes=minutes_ago)).isoformat(),
               "ok": True, "searches_run": 2, "searches_failed": 0}
        row.update(over)
        return row

    def test_a_run_that_read_the_site_covers_its_slot(self):
        from autotrader import insight
        runs = [self.run_at(m, ok=False,
                            errors=["the bot's own bookkeeping is inconsistent"])
                for m in range(0, 240, 30)]
        cov = insight.coverage(runs, expected_minutes=30, window_hours=24, since_change=None)
        assert cov["successful"] == len(runs)
        assert cov["clean"] == 0
        assert cov["complained"] == len(runs)

    def test_a_run_whose_searches_all_failed_does_not(self):
        from autotrader import insight
        runs = [self.run_at(m, ok=False, searches_run=2, searches_failed=2)
                for m in range(0, 240, 30)]
        assert insight.coverage(runs, expected_minutes=30, since_change=None)["successful"] == 0

    def test_a_partial_failure_still_counts(self):
        """One search down is a narrower watch, not a blind one."""
        from autotrader import insight
        runs = [self.run_at(0, ok=False, searches_run=2, searches_failed=1)]
        assert insight.coverage(runs, expected_minutes=30, since_change=None)["successful"] == 1

    def test_a_skipped_run_never_looked(self):
        from autotrader import insight
        runs = [self.run_at(m, skipped=True) for m in range(0, 240, 30)]
        assert insight.coverage(runs, expected_minutes=30, since_change=None)["successful"] == 0

    def test_an_old_record_with_no_counters_falls_back_to_the_exit_code(self):
        from autotrader import insight
        runs = [{"at": self.run_at(10)["at"], "ok": True},
                {"at": self.run_at(40)["at"], "ok": False}]
        assert insight.coverage(runs, expected_minutes=30, since_change=None)["successful"] == 1

    def test_the_gap_is_measured_between_checks_that_looked(self):
        from autotrader import insight
        runs = [self.run_at(0), self.run_at(60, ok=False,
                                            errors=["bookkeeping"])]
        cov = insight.coverage(runs, expected_minutes=30, window_hours=24, since_change=None)
        # Not 24 hours: the complaining run an hour ago still read the site.
        assert cov["longest_gap_minutes"] < 24 * 60

    def test_the_complaints_are_reported_rather_than_absorbed(self):
        """Otherwise this change is just a nicer-looking number."""
        from pathlib import Path
        app = Path("docs/app.js").read_text()
        assert "cov.complained" in app, (
            "coverage no longer counts these against the percentage, so the "
            "page has to say how many there were")

    def test_two_checks_in_one_slot_cover_one_slot(self):
        """A burst of manual runs is not coverage the schedule delivered."""
        from autotrader import insight
        runs = [self.run_at(1), self.run_at(2), self.run_at(3), self.run_at(4)]
        cov = insight.coverage(runs, expected_minutes=30, window_hours=24, since_change=None)
        assert cov["checks"] == 4
        assert cov["slots_covered"] == 1
        assert cov["pct"] == round(1 / 48 * 100, 1)

    def test_a_check_every_slot_is_a_hundred_percent(self):
        from autotrader import insight
        # Mid-slot, not on the edge. Written as range(0, ...) this passed only
        # because the test's clock and the code's clock were microseconds
        # apart; under a frozen clock the newest check landed exactly on the
        # boundary and fell into the slot in progress, and 48 checks read
        # 97.9%. The check below pins that boundary on purpose.
        runs = [self.run_at(m) for m in range(15, 24 * 60, 30)]
        cov = insight.coverage(runs, expected_minutes=30, window_hours=24, since_change=None)
        assert cov["pct"] == 100.0

    def test_a_check_landing_this_instant_belongs_to_the_slot_in_progress(self):
        """The half hour we are standing in has not finished being watched.

        Counting it flatters the figure the moment a run lands and would let
        a single check in a fresh slot claim that slot was covered before the
        slot could possibly have been missed.
        """
        from autotrader import insight
        # Frozen, or this test is about microseconds: unfrozen, the code's
        # clock runs a few microseconds past the test's and the "instant"
        # check lands in the previous slot after all.
        clock.freeze(clock.now())
        cov = insight.coverage([self.run_at(0)], expected_minutes=30,
                               window_hours=24, since_change=None)
        assert cov["checks"] == 1, "it still counts as a check that happened"
        assert cov["slots_covered"] == 0, "but not as a slot already covered"


class TestCoverageIsAboutTheScheduleThatIsRunning:
    """A coverage figure is a statement about a schedule, so it may only be
    measured over a period when that schedule was the one running.

    Say the interval goes from 30 minutes to 120 one morning. For the 24
    hours after that, the window still holds 54 half-hourly runs, and
    bucketing them into 12 two-hour slots fills every one: the Status tab
    reads "100% - 12 of 12" about a schedule that has produced two checks.
    Every word of it is arithmetically true.
    """

    def runs(self, stamps):
        return [{"at": t.isoformat(timespec="seconds"), "ok": True,
                 "searches_run": 1, "listings_seen": 5} for t in stamps]

    def test_the_old_schedules_runs_do_not_fill_the_new_schedules_slots(self):
        from datetime import datetime, timedelta, timezone
        from autotrader import insight
        now = datetime(2026, 3, 4, 7, 40, tzinfo=timezone.utc)
        changed = datetime(2026, 3, 4, 5, 30, tzinfo=timezone.utc)
        # 48 half-hourly runs before the change, two after it.
        old = [changed - timedelta(minutes=30 * n) for n in range(1, 49)]
        new = [changed + timedelta(minutes=10), changed + timedelta(minutes=126)]
        runs = self.runs(old + new)

        blind = insight.coverage(runs, 120, now=now, since_change=None)
        assert blind["pct"] == 100.0, "the bug this exists to stop"

        honest = insight.coverage(runs, 120, now=now,
                                  since_change=changed.isoformat())
        assert honest["partial"] is True
        assert honest["window_hours"] < 3
        assert honest["successful"] == 2, "it should only see the new runs"

    def test_a_window_too_short_to_judge_says_so_instead_of_a_number(self):
        from datetime import datetime, timedelta, timezone
        from autotrader import insight
        now = datetime(2026, 3, 4, 7, 40, tzinfo=timezone.utc)
        changed = now - timedelta(hours=2, minutes=10)
        out = insight.coverage(self.runs([now - timedelta(minutes=5)]), 120,
                               now=now, since_change=changed.isoformat())
        assert out["too_short"] is True, "one complete slot is not a measurement"

    def test_three_complete_slots_is_enough_to_judge(self):
        from datetime import datetime, timedelta, timezone
        from autotrader import insight
        now = datetime(2026, 3, 4, 11, 40, tzinfo=timezone.utc)
        changed = now - timedelta(hours=6, minutes=10)
        runs = self.runs([changed + timedelta(hours=h) for h in (0.2, 2.2, 4.2)])
        out = insight.coverage(runs, 120, now=now, since_change=changed.isoformat())
        assert out["too_short"] is False
        assert out["expected"] == 3 and out["slots_covered"] == 3
        assert out["pct"] == 100.0

    def test_the_slot_in_progress_is_not_counted_against_the_schedule(self):
        """Half an hour into a two-hour slot, no check is late yet."""
        from datetime import datetime, timedelta, timezone
        from autotrader import insight
        now = datetime(2026, 3, 4, 11, 40, tzinfo=timezone.utc)
        changed = now - timedelta(hours=6, minutes=30)
        runs = self.runs([changed + timedelta(hours=h) for h in (0.2, 2.2, 4.2)])
        out = insight.coverage(runs, 120, now=now, since_change=changed.isoformat())
        assert out["expected"] == 3, "the fourth slot has not finished"

    def test_without_a_change_stamp_nothing_changes(self):
        from datetime import datetime, timedelta, timezone
        from autotrader import insight
        now = datetime(2026, 3, 4, 11, 0, tzinfo=timezone.utc)
        runs = self.runs([now - timedelta(minutes=30 * n) for n in range(1, 48)])
        out = insight.coverage(runs, 30, now=now, since_change=None)
        assert out["partial"] is False and out["window_hours"] == 24.0


class TestTheScheduleStampItself:
    def test_it_is_recorded_the_first_time(self, tmp_path):
        from autotrader.state import State
        state = State(path=tmp_path / "state.json")
        assert state.note_schedule(120) is True
        assert state.schedule_changed_at

    def test_it_does_not_move_when_nothing_changed(self, tmp_path):
        from autotrader.state import State
        state = State(path=tmp_path / "state.json")
        state.note_schedule(120)
        first = state.schedule_changed_at
        assert state.note_schedule(120) is False
        assert state.schedule_changed_at == first

    def test_it_moves_when_the_interval_does(self, tmp_path):
        from autotrader.state import State
        state = State(path=tmp_path / "state.json")
        state.note_schedule(30)
        state.data["schedule"]["since"] = "2026-01-01T00:00:00+00:00"
        assert state.note_schedule(120) is True
        assert state.schedule_changed_at != "2026-01-01T00:00:00+00:00"
        assert state.data["schedule"]["was"] == 30


def test_a_coverage_figure_cannot_be_asked_for_without_saying_which_schedule():
    """The stamp has no default.

    A coverage percentage is a statement about a schedule, and a caller that
    does not say when the current one started gets a figure measured over a
    period that may have been running a different one. The Status tab read
    "100% - 12 of 12" about a schedule that had produced two checks, and it
    read that because the argument was optional and the caller had not been
    updated. A new caller now has to decide, and can still say None.
    """
    import inspect
    from autotrader import insight

    p = inspect.signature(insight.coverage).parameters["since_change"]
    assert p.default is inspect.Parameter.empty, "it went back to optional"
    assert p.kind is inspect.Parameter.KEYWORD_ONLY


class TestWhoKeptTime:
    """A coverage figure built out of runs that only happened because
    somebody pushed is a measurement of that person.

    A page can read "2 of 2 slots served" over a schedule GitHub has fired
    zero times.
    """

    CHANGED = "2026-03-04T05:30:00+00:00"

    def runs(self, *pairs):
        from datetime import datetime, timedelta, timezone
        base = datetime(2026, 3, 4, 5, 30, tzinfo=timezone.utc)
        return [{"at": (base + timedelta(hours=h)).isoformat(timespec="seconds"),
                 "ok": True, "searches_run": 1, "listings_seen": 5,
                 "trigger": how}
                for h, how in pairs]

    def at(self, hours):
        from datetime import datetime, timedelta, timezone
        return datetime(2026, 3, 4, 5, 30, tzinfo=timezone.utc) + timedelta(hours=hours)

    def test_a_schedule_that_never_fired_is_reported_as_such(self):
        from autotrader import insight
        runs = self.runs((0.2, "push"), (2.2, "workflow_dispatch"),
                         (4.2, "push"), (6.2, "workflow_dispatch"))
        cov = insight.coverage(runs, 120, now=self.at(8.1),
                               since_change=self.CHANGED)
        assert cov["slots_covered"] == 4 and cov["pct"] == 100.0
        assert cov["slots_scheduled"] == 0 and cov["pct_scheduled"] == 0.0
        assert cov["propped_up"] is True
        assert cov["by_trigger"] == {"push": 2, "workflow_dispatch": 2}

    def test_a_schedule_doing_its_job_is_not_flagged(self):
        from autotrader import insight
        runs = self.runs((0.2, "schedule"), (2.2, "schedule"),
                         (4.2, "schedule"), (6.2, "schedule"))
        cov = insight.coverage(runs, 120, now=self.at(8.1),
                               since_change=self.CHANGED)
        assert cov["slots_scheduled"] == 4 and cov["pct_scheduled"] == 100.0
        assert cov["propped_up"] is False

    def test_an_outside_timer_counts_as_a_schedule(self):
        """repository_dispatch is the documented way to drive this from a
        machine that is actually on. It is somebody's schedule."""
        from autotrader import insight
        runs = self.runs((0.2, "repository_dispatch"), (2.2, "repository_dispatch"),
                         (4.2, "repository_dispatch"))
        cov = insight.coverage(runs, 120, now=self.at(6.1),
                               since_change=self.CHANGED)
        assert cov["slots_scheduled"] == 3
        assert cov["propped_up"] is False

    def test_a_run_with_no_trigger_recorded_is_not_credited_to_the_schedule(self):
        """Runs from before the bot noted what started them. Counting them as
        scheduled is the one direction that can flatter the schedule."""
        from autotrader import insight
        runs = [dict(r) for r in self.runs((0.2, "x"), (2.2, "x"))]
        for r in runs:
            r.pop("trigger")
        cov = insight.coverage(runs, 120, now=self.at(4.1),
                               since_change=self.CHANGED)
        assert cov["slots_covered"] == 2
        assert cov["slots_scheduled"] == 0
        assert cov["by_trigger"] == {"unattributed": 2}

    def test_the_mixed_case_counts_only_what_the_schedule_filled(self):
        from autotrader import insight
        runs = self.runs((0.2, "schedule"), (2.2, "push"),
                         (4.2, "schedule"), (6.2, "push"))
        cov = insight.coverage(runs, 120, now=self.at(8.1),
                               since_change=self.CHANGED)
        assert cov["slots_covered"] == 4
        assert cov["slots_scheduled"] == 2
        assert cov["propped_up"] is False, "some is not none"


def test_a_deduplicated_scheduled_firing_still_proves_the_cron_is_alive():
    """A push at 23:00 suppresses the 23:41 cron via the 90-minute floor.
    Counting only slots FILLED would read that as "the schedule did nothing" -
    so the person measuring the schedule would be the person hiding it."""
    from datetime import datetime, timedelta, timezone
    from autotrader import insight
    base = datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)

    def run(hours, trigger, read=True):
        return {"at": (base + timedelta(hours=hours)).isoformat(timespec="seconds"),
                "ok": True, "trigger": trigger,
                "searches_run": 1 if read else 0,
                "listings_seen": 5 if read else 0, "skipped": not read}

    cov = insight.coverage(
        [run(0.1, "push"), run(0.7, "schedule", read=False),
         run(2.1, "schedule"), run(2.7, "schedule", read=False)],
        120, now=base + timedelta(hours=4.05), since_change=base.isoformat())
    assert cov["schedule_fired"] == 3, "deduplicated firings are still firings"
    assert cov["slots_scheduled"] == 1, "only one slot was actually filled by it"
    assert cov["slots_covered"] == 2


class TestACountOfThingsThatHappenedIsNeverNegative:
    """The Status card read "-2 of 7 checks complained".

    `clean` counted every run in the window whose exit code was zero;
    `complained` was the number that read the site minus that. A firing that
    stands down because a check just happened exits zero and never reads the
    site, so it landed in one population and not the other. Two numbers
    subtracted across different populations, and the giveaway was a negative
    count of a thing that had happened.
    """

    @staticmethod
    def at(minutes_ago, **over):
        from datetime import timedelta
        row = {"at": (clock.now() - timedelta(minutes=minutes_ago)).isoformat(),
               "ok": True, "searches_run": 2, "searches_failed": 0}
        row.update(over)
        return row

    def coverage(self, runs):
        from autotrader import insight
        return insight.coverage(runs, expected_minutes=120, window_hours=24,
                                since_change=None)

    def test_standing_down_does_not_make_the_complaint_count_negative(self):
        runs = [self.at(30, skipped=True), self.at(90, skipped=True),
                self.at(150), self.at(210, ok=False)]
        cov = self.coverage(runs)
        assert cov["complained"] == 1
        assert cov["clean"] == 1
        assert cov["stood_down"] == 2

    def test_the_two_halves_add_up_to_the_checks_that_looked(self):
        """Whatever the mix, clean + complained is `successful`.

        `successful` is the denominator the page prints beside `complained`,
        which is the whole reason they have to be counted over one set.
        """
        import itertools
        for pattern in itertools.product([True, False], repeat=4):
            runs = [self.at(30 + 60 * n, ok=ok, skipped=not ok and n % 2 == 0)
                    for n, ok in enumerate(pattern)]
            cov = self.coverage(runs)
            assert cov["clean"] + cov["complained"] == cov["successful"], (
                pattern, cov)
            assert cov["complained"] >= 0, (pattern, cov)

    def test_a_run_that_could_not_read_anything_is_in_neither(self):
        """It did not cover its slot and it did not complain about itself.

        It is still a check that was attempted, which is a different number
        and is reported as one.
        """
        cov = self.coverage([self.at(30, ok=False, searches_run=2,
                                     searches_failed=2)])
        assert cov["checks"] == 1, "a firing that tried"
        assert cov["successful"] == 0, "and read nothing"
        assert cov["clean"] == 0 and cov["complained"] == 0
