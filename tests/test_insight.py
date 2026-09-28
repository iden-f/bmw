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
        # searches_run counts only the searches that were read, so a run
        # that read none records 0 and every search as failed.
        from autotrader import insight
        runs = [self.run_at(m, ok=False, searches_run=0, searches_failed=2)
                for m in range(0, 240, 30)]
        assert insight.coverage(runs, expected_minutes=30, since_change=None)["successful"] == 0

    def test_a_partial_failure_still_counts(self):
        """One search down is a narrower watch, not a blind one."""
        from autotrader import insight
        runs = [self.run_at(0, ok=False, searches_run=2, searches_failed=1)]
        assert insight.coverage(runs, expected_minutes=30, since_change=None)["successful"] == 1

    def test_one_of_two_searches_down_still_counts(self):
        """What the runner records for it: one read, one failed.

        The counters never overlap, and reading "1 failed of 1 run" as no
        search read left a hole in the coverage every time one of two
        searches would not load.
        """
        from autotrader import insight
        runs = [self.run_at(0, ok=False, searches_run=1, searches_failed=1)]
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
        cov = self.coverage([self.at(30, ok=False, searches_run=0,
                                     searches_failed=2)])
        assert cov["checks"] == 1, "a firing that tried"
        assert cov["successful"] == 0, "and read nothing"
        assert cov["clean"] == 0 and cov["complained"] == 0


class TestCoverageStopsWhereTheRunLogDoes:
    """The run log kept the newest sixty runs, stand-downs included.

    A cron at :07 and :37 and an outside timer at :15 and :45 record four runs
    an hour, so the log reached back about fifteen hours. Measured over
    twenty-four, the nine hours it no longer held counted as unwatched: a
    schedule that had a check in every slot read 67%, with a nine-hour gap,
    and the thin-coverage alarm blamed GitHub for it. Coverage stopped where
    the log did, which left it measuring fifteen hours of the day. The log
    now keeps the day.
    """

    NOW = "2026-03-10T12:50:00+00:00"

    def firings(self, days=3):
        from datetime import timedelta
        now = clock.parse(self.NOW)
        rows = []
        for hour in range(days * 24):
            top = now.replace(minute=0) - timedelta(hours=hour)
            for minute, how in ((45, "repository_dispatch:timer"),
                                (37, "schedule"),
                                (15, "repository_dispatch:timer"),
                                (7, "schedule")):
                at = top + timedelta(minutes=minute)
                if at > now:
                    continue
                # A check every two hours; every other firing stands down.
                reads = minute == 7 and at.hour % 2 == 0
                rows.append({"at": at.isoformat(timespec="seconds"),
                             "trigger": how, "ok": True,
                             "skipped": not reads,
                             "searches_run": 2 if reads else 0,
                             "searches_failed": 0})
        return rows            # newest first, as State.record_run keeps them

    def cut_at_sixty(self):
        """The log as a check before this change left it."""
        from autotrader.state import State
        state = State({"version": 2, "listings": {}, "searches": {},
                       "runs": self.firings()[:60],
                       "watch_started": self.firings()[-1]["at"]})
        state.upgrade()
        return state.runs

    def recorded(self, days=3):
        """Every firing written down by State.record_run, oldest first."""
        from autotrader.state import State
        state = State({"version": 2, "listings": {}, "searches": {}, "runs": []})
        for run in reversed(self.firings(days)):
            state.record_run(run)
        return state

    def test_the_log_keeps_the_day(self):
        from datetime import timedelta
        from autotrader import insight
        from autotrader.state import RUN_LOG_HOURS
        runs = self.recorded().runs
        # Thirty hours of four firings an hour, both ends, and the one
        # before them.
        assert len(runs) == RUN_LOG_HOURS * 4 + 2
        reach = clock.parse(runs[0]["at"]) - clock.parse(runs[-1]["at"])
        assert timedelta(hours=RUN_LOG_HOURS) < reach < timedelta(hours=RUN_LOG_HOURS + 1)
        cov = insight.coverage(runs, 120, now=clock.parse(self.NOW),
                               since_change=None)
        assert cov["truncated"] is False and cov["window_hours"] == 24.0
        assert cov["pct"] == 100.0 and cov["pct_scheduled"] == 100.0, cov
        assert cov["longest_gap_minutes"] <= 120, cov
        cost = insight.minutes_spent(runs, now=clock.parse(self.NOW))
        assert cost["window_hours"] == 24 and cost["firings"] == 96

    def test_a_stand_down_keeps_only_what_it_has_to_say(self):
        """Twice as many runs kept, in less room than the sixty took."""
        import json
        from autotrader.runner import RunReport
        from autotrader.state import MAX_RUN_HISTORY, State

        def full(run):
            report = RunReport(trigger=run["trigger"], skipped=run["skipped"],
                               searches_run=run["searches_run"],
                               listings_seen=0 if run["skipped"] else 40)
            return {"at": run["at"], **report.to_dict()}

        state = State({"version": 2, "listings": {}, "searches": {}, "runs": []})
        for run in reversed(self.firings()):
            state.record_run(full(run))
        runs = state.runs
        before = [full(r) for r in self.firings()[:60]]
        assert len(runs) > 2 * len(before) and len(runs) < MAX_RUN_HISTORY
        assert len(json.dumps(runs)) < len(json.dumps(before))
        stood = [r for r in runs if r.get("skipped")]
        assert stood and all(set(r) <= {"at", "ok", "skipped", "duration_s",
                                        "trigger", "validation_ok",
                                        "earlier_runs_dropped"} for r in stood)
        # A check is kept whole.
        assert set(next(r for r in runs if not r.get("skipped"))) >= set(before[0])

    def test_a_long_silence_before_the_last_run_is_still_a_gap(self):
        """The log is kept back from its newest run, and keeps the run before
        those hours: after a day and a half with no run, the one run that
        followed would otherwise be the whole log, and the day before it
        would read as unknown rather than as a day with no check."""
        from datetime import timedelta
        from autotrader import insight
        state = self.recorded()
        late = clock.parse(self.NOW) + timedelta(hours=40)
        state.record_run({"at": late.isoformat(timespec="seconds"),
                          "trigger": "schedule", "ok": True, "skipped": False,
                          "searches_run": 2, "searches_failed": 0})
        cov = insight.coverage(state.runs, 120, now=late, since_change=None)
        assert cov["truncated"] is False
        assert cov["longest_gap_minutes"] >= 24 * 60 - 1, cov
        assert cov["pct"] < 10, cov

    def test_a_log_cut_at_sixty_is_measured_from_its_oldest_run(self):
        from autotrader import insight
        kept = self.cut_at_sixty()
        cov = insight.coverage(kept, 120, now=clock.parse(self.NOW),
                               since_change=None)
        assert cov["truncated"] is True
        assert cov["window_hours"] < 24
        assert cov["partial"] is False, "the schedule did not change"
        assert cov["pct"] == 100.0, cov
        assert cov["pct_scheduled"] == 100.0, cov
        assert cov["longest_gap_minutes"] <= 120, cov

    def test_a_log_with_room_left_still_counts_the_hours_before_it(self):
        """Nothing was dropped from it, so an empty stretch really was empty."""
        from autotrader import insight
        few = self.firings()[:20]
        cov = insight.coverage(few, 120, now=clock.parse(self.NOW),
                               since_change=None)
        assert cov["truncated"] is False
        assert cov["window_hours"] == 24.0
        assert cov["pct"] < 50

    def test_the_minutes_spent_say_how_far_back_they_reach(self):
        from autotrader import insight
        kept = self.cut_at_sixty()
        cost = insight.minutes_spent(kept, now=clock.parse(self.NOW))
        assert cost["window_hours"] < 24
        assert cost["firings"] == 60

    def test_a_full_log_is_measured_from_its_oldest_run(self):
        """However it got there: a watch started far more often than planned
        fills the log before a day is out."""
        from datetime import timedelta
        from autotrader import insight
        from autotrader.state import MAX_RUN_HISTORY
        now = clock.parse(self.NOW)
        kept = [{"at": (now - timedelta(minutes=3 * i)).isoformat(),
                 "trigger": "schedule", "ok": True, "skipped": i % 40 != 0,
                 "searches_run": int(i % 40 == 0)}
                for i in range(MAX_RUN_HISTORY)]
        cov = insight.coverage(kept, 120, now=now, since_change=None)
        assert cov["truncated"] is True and cov["window_hours"] < 24


def test_the_card_says_how_far_apart_on_the_odometer_a_peer_can_be():
    """The card said "each within a third of this car's odometer", while a
    car with 60,000 km was judged against peers with 92,000."""
    from pathlib import Path
    from autotrader import insight
    assert insight._similar_wear(60000, 92000), "about one and a half times"
    assert not insight._similar_wear(60000, 93000)
    assert insight._similar_wear(60000, 39000), "about two-thirds"
    assert not insight._similar_wear(60000, 38000)
    page = (Path(__file__).resolve().parent.parent / "docs" / "app.js").read_text(
        encoding="utf-8")
    assert "within a third of this car's odometer" not in page
    assert "about two-thirds to one and a half times this car's kilometres" in page


class TestEachEventSaysWhatHappenedToIt:
    """A car announced on the 1st, told about a $2,000 drop on the 5th, and
    kept quiet about a $100 drop on the 10th.

    Every event on the car carried the car's latest state, so all three read
    "quiet: the price moved $100", the Feed's "sent" count left out the
    $2,000 drop that was sent, and the car's timeline printed the $100 reason
    under the day it was listed.
    """

    def car(self, state, price):
        from autotrader.listing import Listing
        state.record(Listing(id="1", url="https://www.autotrader.ca/offers/honda-civic-1",
                             title="2020 Honda Civic", price=price,
                             price_source="detail", search_id="s"))
        return state.listings["1"]

    def rows(self, state):
        from autotrader import insight
        return {e["kind"] + str(e.get("delta") or ""): e["delivery"]
                for e in insight.events(state.listings.values())}

    def test_each_event_keeps_its_own_delivery(self, tmp_path):
        from autotrader.state import State
        state = State(path=tmp_path / "s.json")
        clock.freeze("2026-09-01T12:00:00+00:00")
        self.car(state, 30000)
        state.mark_notified(["1"])
        clock.freeze("2026-09-05T12:00:00+00:00")
        self.car(state, 28000)
        state.mark_notified(["1"])
        clock.freeze("2026-09-10T12:00:00+00:00")
        self.car(state, 27900)
        state.hold_for_ride_along("1", "the price moved $100, under the bar")

        rows = self.rows(state)
        assert rows["new"]["state"] == "sent"
        assert rows["new"]["at"].startswith("2026-09-01")
        assert rows["price_drop-2000"]["state"] == "sent"
        assert rows["price_drop-2000"]["at"].startswith("2026-09-05")
        assert rows["price_drop-100"]["state"] == "quiet"
        assert "$100" in rows["price_drop-100"]["text"]
        assert "$100" not in rows["new"].get("text", "")

    def test_a_starting_point_told_about_later_was_not_told_on_arrival(self, tmp_path):
        from autotrader.state import BASELINE_REASON, State
        state = State(path=tmp_path / "s.json")
        clock.freeze("2026-09-01T12:00:00+00:00")
        self.car(state, 30000)
        state.silence("1", BASELINE_REASON)
        clock.freeze("2026-09-05T12:00:00+00:00")
        self.car(state, 28000)
        state.mark_notified(["1"])

        rows = self.rows(state)
        assert rows["new"]["state"] == "quiet", rows["new"]
        assert rows["price_drop-2000"]["state"] == "sent"

    def test_the_ledger_agrees(self, tmp_path):
        from autotrader import events
        from autotrader.state import State
        state = State(path=tmp_path / "s.json")
        clock.freeze("2026-09-01T12:00:00+00:00")
        self.car(state, 30000)
        state.mark_notified(["1"])
        clock.freeze("2026-09-05T12:00:00+00:00")
        self.car(state, 28000)
        state.mark_notified(["1"])
        clock.freeze("2026-09-10T12:00:00+00:00")
        self.car(state, 29000)
        state.silence("1", "you asked not to hear about price rises")
        found = events.scan(state)
        assert found["price_drop"].delivered == "delivered at 2026-09-05T12:00:00+00:00"
        assert found["price_rise"].delivered.startswith("deliberately quiet")

    def test_a_car_told_about_before_the_record_began_is_not_guessed(self, tmp_path):
        """Its record starts at the last alert it knew of; an event before
        that keeps the car's answer rather than reading as never sent."""
        from autotrader.state import State
        state = State(path=tmp_path / "s.json")
        clock.freeze("2026-09-01T12:00:00+00:00")
        entry = self.car(state, 30000)
        clock.freeze("2026-09-03T12:00:00+00:00")
        self.car(state, 29000)
        entry = state.listings["1"]
        entry["notified_at"] = "2026-09-03T12:00:00+00:00"   # an older build
        clock.freeze("2026-09-05T12:00:00+00:00")
        self.car(state, 27000)
        state.mark_notified(["1"])
        assert state.listings["1"]["told_since"] == "2026-09-03T12:00:00+00:00"

        rows = self.rows(state)
        assert rows["price_drop-1000"]["at"].startswith("2026-09-03")
        assert rows["price_drop-2000"]["at"].startswith("2026-09-05")
        # 1 Sep is before the record, and nothing in it falls before the 3rd.
        assert rows["new"]["state"] == "sent"
        assert rows["new"]["at"].startswith("2026-09-05"), \
            "the car's own answer, as before the record existed"

    def test_the_record_keeps_only_the_last_few(self, tmp_path):
        from datetime import timedelta
        from autotrader.state import TOLD_KEPT, State
        state = State(path=tmp_path / "s.json")
        clock.freeze("2026-09-01T12:00:00+00:00")
        self.car(state, 30000)
        for n in range(TOLD_KEPT + 3):
            clock.advance(timedelta(hours=1))
            state.mark_notified(["1"])
        entry = state.listings["1"]
        assert len(entry["told"]) == TOLD_KEPT
        assert entry["told_since"] == entry["told"][0]


class TestTheWeeklyDigestIsOwedUntilItIsSent:
    """One Watchdog firing, Monday at 14:20, sent the digest, and nothing
    recorded that it had. GitHub drops scheduled firings, often in clusters,
    so a dropped Monday meant no digest that week and no sign of it. Every
    check asks now, and the first after Monday's hour is told yes."""

    # Monday 5 October 2026, the first day of ISO week 41.
    MONDAY = "2026-10-05T00:00:00+00:00"
    BEGAN = "2026-09-01T00:00:00+00:00"

    def due(self, sent, hours, began=BEGAN):
        from datetime import timedelta

        from autotrader import insight
        return insight.weekly_due(sent, began,
                                  now=clock.parse(self.MONDAY) + timedelta(hours=hours))

    def test_not_before_monday_afternoon(self):
        assert self.due("2026-W40", 13.9) is None
        assert self.due("2026-W40", -1) is None      # still Sunday

    def test_owed_from_monday_afternoon(self):
        assert self.due("2026-W40", 14) == "2026-W41"

    def test_a_dropped_monday_is_made_up_later_in_the_week(self):
        assert self.due("2026-W40", 24 * 3 + 5) == "2026-W41"

    def test_once_a_week(self):
        assert self.due("2026-W41", 15) is None
        assert self.due("2026-W41", 24 * 6 + 23) is None

    def test_owed_again_the_next_week(self):
        assert self.due("2026-W41", 24 * 7 + 14) == "2026-W42"

    def test_a_watch_that_began_after_the_hour_waits_for_next_monday(self):
        """It has no week to report, and a digest on its first day would
        break the README's "every Monday" for nothing."""
        began = "2026-10-05T20:00:00+00:00"
        assert self.due(None, 30, began=began) is None
        assert self.due(None, 24 * 7 + 14, began=began) == "2026-W42"
        assert self.due(None, 30, began=None) is None   # never checked at all

    def test_a_watch_that_was_running_is_owed_its_first(self):
        assert self.due(None, 15) == "2026-W41"

    def test_the_week_is_named_as_iso_names_it_across_new_year(self):
        """Friday 1 January 2027 is in the 53rd week of 2026."""
        from autotrader import insight
        new_year = clock.parse("2027-01-01T12:00:00+00:00")
        assert insight.weekly_due("2026-W52", self.BEGAN, now=new_year) == "2026-W53"
        assert insight.weekly_due("2026-W53", self.BEGAN, now=new_year) is None


class TestTheCheckSendsTheWeek:
    """`weekly --notify --if-due`, which every check runs."""

    MONDAY_AFTERNOON = "2026-10-05T15:07:00+00:00"

    def setup(self, tmp_path, monkeypatch, *, ok=True, **data):
        from autotrader import cli
        from autotrader.config import Config
        from autotrader.notifiers import Result
        from autotrader.state import State
        monkeypatch.chdir(tmp_path)
        if not (tmp_path / "config.json").exists():
            Config.defaults(tmp_path / "config.json").save()
        state = State.load(tmp_path / "state.json")
        state.data.update(data or {"first_ok_at": "2026-09-01T00:00:00+00:00"})
        state.save()
        sent = []

        def alert(cfg, subject, body, env=None, notifiers=None):
            sent.append(subject)
            return [Result("capture", ok)]
        monkeypatch.setattr(cli.notifiers, "alert", alert)
        clock.freeze(self.MONDAY_AFTERNOON)

        def weekly():
            return cli.main(["--no-colour", "--config", str(tmp_path / "config.json"),
                             "--state", str(tmp_path / "state.json"),
                             "weekly", "--notify", "--if-due"])
        return weekly, sent, lambda: State.load(tmp_path / "state.json")

    def test_the_first_check_after_the_hour_sends_it_once(self, tmp_path, monkeypatch):
        weekly, sent, state = self.setup(tmp_path, monkeypatch)
        assert weekly() == 0
        assert sent == ["AutoTrader: your last 7 days"]
        assert state().data["weekly_sent"] == "2026-W41"
        assert weekly() == 0
        assert len(sent) == 1, "sent twice in one week"

    def test_a_week_no_channel_carried_is_still_owed(self, tmp_path, monkeypatch):
        weekly, sent, state = self.setup(tmp_path, monkeypatch, ok=False)
        assert weekly() == 1
        assert "weekly_sent" not in state().data
        assert weekly() == 1
        assert len(sent) == 2, "the next check did not try again"

    def test_before_the_hour_it_sends_nothing(self, tmp_path, monkeypatch):
        weekly, sent, state = self.setup(tmp_path, monkeypatch)
        clock.freeze("2026-10-05T13:37:00+00:00")
        assert weekly() == 0
        assert sent == []
        assert "weekly_sent" not in state().data

    FAILED_ONE = {"ok": False, "searches_run": 1, "searches_failed": 1,
                  "errors": ["Other search: HTTP 404"]}

    def test_a_watch_whose_every_check_has_an_error_still_gets_it(
            self, tmp_path, monkeypatch):
        """The week was anchored on the first check with no error at all. With
        one search broken on every check that never came, and the digest the
        watchdog used to send every Monday stopped without a word."""
        runs = [{"at": f"2026-10-0{d}T12:07:00+00:00", **self.FAILED_ONE}
                for d in (4, 3, 2, 1)]
        weekly, sent, state = self.setup(
            tmp_path, monkeypatch, runs=runs, watch_started="2026-09-01T00:07:00+00:00",
            searches={"a": {"first_ok": "2026-09-01T00:07:00+00:00",
                            "last_ok": "2026-10-04T12:07:00+00:00"},
                      "b": {"consecutive_failures": 30, "last_ok": None}})
        assert weekly() == 0
        assert sent == ["AutoTrader: your last 7 days"]
        assert state().data["weekly_sent"] == "2026-W41"

    def test_a_watch_that_has_read_nothing_is_not_owed_one(self, tmp_path, monkeypatch):
        weekly, sent, state = self.setup(
            tmp_path, monkeypatch, runs=[{"at": "2026-10-04T12:07:00+00:00",
                                          **self.FAILED_ONE, "searches_run": 0}],
            watch_started="2026-09-01T00:07:00+00:00",
            searches={"b": {"consecutive_failures": 30, "last_ok": None}})
        assert weekly() == 0
        assert sent == []

    def test_one_search_that_never_loads_does_not_stop_it(self, tmp_path, monkeypatch,
                                                           fixture_html):
        from autotrader import runner as runner_mod
        from autotrader.config import Config
        from autotrader.http import FetchError
        from autotrader.runner import run
        from autotrader.state import State

        from .helpers import Capture, FakeFetcher, next_check, use_channels

        class OneGone(FakeFetcher):
            def get(self, url, referer=None, allow_block=False):
                if "/toyota/" in url:
                    self.urls.append(url)
                    raise FetchError(f"HTTP 404 from {url}", status=404)
                return super().get(url, referer, allow_block)

        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=25", "Example search")
        cfg.add_search("https://www.autotrader.ca/cars/toyota/corolla/?rcp=25", "Other search")
        cfg.set("scraping.delay_ms", 0)
        cfg.set("scraping.retries", 0)
        cfg.set("scraping.enrich_details", False)
        cfg.set("archive.mode", "off")
        cfg.save()
        use_channels(monkeypatch, runner_mod, [Capture()])
        clock.freeze("2026-09-28T10:00:00+00:00")
        for _ in range(4):
            next_check(60 * 12)
            report = run(cfg, State.load(tmp_path / "state.json"),
                         fetcher=OneGone(fixture_html("search_next_data")), env={})
            assert report.searches_run == 1 and report.errors
        assert not State.load(tmp_path / "state.json").data.get("first_ok_at")
        weekly, sent, state = self.setup(tmp_path, monkeypatch, weekly_sent=None)
        assert weekly() == 0
        assert sent == ["AutoTrader: your last 7 days"]
