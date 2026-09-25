"""What the bot costs, and refusing to cost more than it may.

Written after a week in which this bot held a GitHub runner for up to sixteen
hours a day and justified it with "the repository is public, so the minutes
are free". The minutes were free - GitHub reports zero billable milliseconds
against every one of those runs - and the reasoning was still wrong: the
exemption is a repository setting, not a property of the code, and nothing in
the repository would have noticed the day it changed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from autotrader import clock
from autotrader import budget
from autotrader.config import Config
from autotrader.state import State

NOW = datetime(2026, 9, 13, 20, 0, tzinfo=timezone.utc)

SEPT = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def bench(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Config.defaults(tmp_path / "config.json")
    cfg.save()
    state = State(path=tmp_path / "state.json")
    # Default the tests to a repository that IS charged, so the arithmetic
    # below is about the arithmetic. The exemption has its own class.
    state.data["repo"] = {"visibility": "private"}
    return cfg, state


def month(days: dict[str, float], runs: int = 0) -> dict:
    return {"month": "2026-09", "days": days, "runs": runs}


class TestWhatGitHubCharges:
    """Every job is rounded up to a whole minute. Getting this wrong in the
    optimistic direction is how a guard reports fine while the bill says
    otherwise."""

    @pytest.mark.parametrize("seconds,expected", [
        (0, 1), (1, 1), (34, 1), (59, 1), (60, 1), (61, 2), (119, 2), (121, 3),
    ])
    def test_a_job_is_charged_by_the_whole_minute(self, seconds, expected):
        assert budget.minutes_for(seconds) == expected

    def test_two_jobs_cost_two_minutes_even_if_both_are_short(self):
        """The reason publishing was folded back into the check job."""
        assert budget.minutes_for(34, jobs=2) == 2
        assert budget.minutes_for(34, jobs=1) == 1

    def test_a_job_that_ran_at_all_is_never_free(self):
        assert budget.minutes_for(0.001) == 1


class TestTheLedger:
    def test_a_run_is_added_to_today(self, bench):
        cfg, state = bench
        budget.record(state, 1.0, cfg=cfg, now=SEPT)
        # Labelled, not bare. Which bucket it lands in is the point.
        assert state.data["actions"]["days"]["2026-09-13"]["drawing"] == 1.0
        assert state.data["actions"]["runs"] == 1

    def test_runs_accumulate(self, bench):
        cfg, state = bench
        for _ in range(5):
            budget.record(state, 1.0, cfg=cfg, now=SEPT)
        assert state.data["actions"]["days"]["2026-09-13"]["drawing"] == 5.0

    def test_a_new_month_starts_from_zero(self, bench):
        cfg, state = bench
        state.data["actions"] = month({"2026-09-30": 2500.0}, runs=900)
        out = budget.record(state, 1.0, cfg=cfg,
                            now=datetime(2026, 10, 1, 0, 5, tzinfo=timezone.utc))
        assert out["used"] == 1.0 and out["month"] == "2026-10"

    def test_the_rate_is_per_day_of_data_not_per_day_of_month(self, bench):
        """A bot installed on the 20th has not used 19 days of allowance."""
        cfg, state = bench
        state.data["actions"] = month({"2026-09-12": 10.0, "2026-09-13": 10.0})
        out = budget.record(state, 10.0, cfg=cfg, now=SEPT)
        assert out["days_elapsed"] == 2
        assert out["per_day"] == 15.0


class TestTheProjection:
    def test_one_day_is_not_a_month(self, bench):
        cfg, state = bench
        out = budget.record(state, 400.0, cfg=cfg, now=SEPT)
        assert out["projected"] is None and out["state"] == "early"
        assert "too little to project" in out["text"]

    def test_the_real_situation_this_was_written_for(self, bench):
        """2,338 minutes, 12 days in, 3,000 allowed: over, and it says so."""
        cfg, state = bench
        state.data["actions"] = month(
            {f"2026-09-{d:02d}": 195.0 for d in range(1, 13)}, runs=500)
        out = budget.record(state, 1.0, cfg=cfg, now=SEPT)
        assert out["state"] == "over"
        assert out["projected"] > 3000
        assert "will stop before it gets there" in out["text"]

    def test_a_cheap_schedule_reads_as_fine(self, bench):
        cfg, state = bench
        state.data["actions"] = month(
            {f"2026-09-{d:02d}": 28.0 for d in range(1, 13)}, runs=150)
        out = budget.record(state, 1.0, cfg=cfg, now=SEPT)
        assert out["state"] == "ok"
        assert out["projected"] < 1000

    def test_spent_is_a_harder_state_than_projected_over(self, bench):
        cfg, state = bench
        state.data["actions"] = month(
            {f"2026-09-{d:02d}": 300.0 for d in range(1, 13)}, runs=900)
        out = budget.record(state, 1.0, cfg=cfg, now=SEPT)
        assert out["state"] == "stop" and out["should_stop"] is True

    def test_the_ceiling_is_below_the_allowance(self, bench):
        cfg, state = bench
        out = budget.record(state, 1.0, cfg=cfg, now=SEPT)
        assert out["ceiling"] < out["allowance"]

    def test_the_allowance_comes_from_config_not_from_code(self, bench):
        """GitHub Free is 2,000 a month, not 3,000."""
        cfg, state = bench
        cfg.set("budget.included_minutes", 2000)
        out = budget.record(state, 1.0, cfg=cfg, now=SEPT)
        assert out["allowance"] == 2000


class TestStoppingRatherThanSpending:
    """The guard writes a file the workflow reads before it installs anything.

    A file, not an API call to disable the workflow: it is visible in the
    repository, it survives a token with no actions: write, and deleting it is
    how a person says "I have looked at this and it is fine".
    """

    @pytest.fixture
    def watcher(self, tmp_path, monkeypatch, fixture_html):
        from autotrader import runner as runner_mod
        from autotrader.runner import run
        from .helpers import Capture, FakeFetcher, use_channels

        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.add_search("https://www.autotrader.ca/cars/toyota/corolla/?rcp=25", "Corolla")
        cfg.set("scraping.delay_ms", 0)
        cfg.set("scraping.enrich_details", False)
        cfg.set("archive.mode", "off")
        cfg.set("dashboard.photos", False)
        cfg.save()
        sink = Capture()
        use_channels(monkeypatch, runner_mod, [sink])
        html = fixture_html("search_next_data")

        def go():
            return run(cfg, State.load(tmp_path / "state.json"),
                       fetcher=FakeFetcher(html), env={})

        return type("W", (), {"cfg": cfg, "sink": sink, "run": staticmethod(go),
                              "path": tmp_path,
                              "state": staticmethod(
                                  lambda: State.load(tmp_path / "state.json"))})

    # Seeded RELATIVE to the ceiling and to how much month is left, never as
    # a rate times the day of the month.
    #
    # The old version wrote 195 minutes a day into every day so far, which on
    # the 13th totalled 2,535 against a 2,550 ceiling and read "heading over",
    # and on the 14th totalled 2,730 and read "already over". The test that
    # distinguishes those two states flipped on a calendar date with no code
    # change at all.
    def seed(self, watcher, *, share_of_ceiling: float, days: int = 2):
        """Write `days` days of history totalling that share of the ceiling."""
        from autotrader import budget
        state = watcher.state()
        now = clock.now()
        ledger = budget.load(state, watcher.cfg, now=now)
        per_day = ledger.ceiling() * share_of_ceiling / days
        state.data["actions"] = {
            "month": f"{now.year:04d}-{now.month:02d}",
            "days": {f"{now.year:04d}-{now.month:02d}-{d:02d}":
                     {"drawing": per_day} for d in range(1, days + 1)},
            "runs": 400}
        state.save()
        return ledger

    def spend_the_month(self, watcher, per_day: float):
        """Past the ceiling outright, whatever the date."""
        self.seed(watcher, share_of_ceiling=1.4)

    def test_an_ordinary_run_leaves_no_stop_file(self, watcher):
        report = watcher.run()
        assert report.ok
        assert not (watcher.path / "BUDGET-STOP").exists()
        assert report.budget["this_run_minutes"] >= 1

    def test_every_run_is_charged_to_the_month(self, watcher):
        watcher.run()
        first = watcher.state().data["actions"]["runs"]
        watcher.run()
        assert watcher.state().data["actions"]["runs"] == first + 1

    def test_a_spent_month_writes_the_stop_file(self, watcher):
        self.spend_the_month(watcher, 400.0)
        watcher.run()
        stop = watcher.path / "BUDGET-STOP"
        assert stop.exists(), "nothing stopped the bot spending past its allowance"
        text = stop.read_text()
        assert "stopped checking" in text
        assert "Delete this file" in text, "a guard with no way out is a trap"

    def test_it_says_so_out_loud_once(self, watcher):
        """Silently stopping is the same failure as silently spending."""
        self.spend_the_month(watcher, 400.0)
        watcher.run()
        subjects = [s for s, _ in watcher.sink.alerts]
        assert any("minutes are spent" in s for s in subjects), subjects
        before = len(watcher.sink.alerts)
        watcher.run()
        assert len(watcher.sink.alerts) == before, "told twice for the same month"

    def test_projected_over_warns_without_stopping(self, watcher):
        """Heading over is a warning; being over is a stop. Different things."""
        # Under the ceiling today, over it by month end. That state only
        # exists while there is month left, so on the last day there is
        # nothing to test and the test says so rather than failing.
        from autotrader import budget
        now = clock.now()
        ledger = budget.load(watcher.state(), watcher.cfg, now=now)
        remaining = ledger.days_remaining(now)
        if remaining < 1:
            pytest.skip("the month ends today; there is no 'heading over'")
        # The run itself adds a THIRD day, which dilutes the rate the
        # projection is built from - so the seed has to be chosen for the
        # ledger as it will be after the run, not as it is now. With 2 seeded
        # days totalling S and one more minute today, per_day is S/3 and the
        # projection is S + (S/3)*remaining. Aiming that 20% past the ceiling:
        self.seed(watcher, share_of_ceiling=3.6 / (3 + remaining))
        report = watcher.run()
        assert not (watcher.path / "BUDGET-STOP").exists()
        assert any("month ends at about" in w for w in report.warnings), report.warnings

    def test_the_stop_clears_itself_when_the_month_does(self, watcher):
        self.spend_the_month(watcher, 400.0)
        watcher.run()
        assert (watcher.path / "BUDGET-STOP").exists()
        state = watcher.state()
        state.data["actions"]["days"] = {"2026-09-01": 1.0}
        state.save()
        report = watcher.run()
        assert not (watcher.path / "BUDGET-STOP").exists()
        assert any("cleared" in w for w in report.warnings), report.warnings

    def test_accounting_never_fails_a_check(self, watcher, monkeypatch):
        """A photo may not fail a check and neither may a spreadsheet."""
        from autotrader import budget as budget_mod
        monkeypatch.setattr(budget_mod, "record",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        report = watcher.run()
        assert report.ok, report.errors


class TestTheWorkflowReadsIt:
    """The file is only a guard if something acts on it before spending."""

    def test_the_watcher_checks_for_the_stop_file_first(self):
        source = Path(".github/workflows/watch.yml").read_text()
        body = source[source.index("steps:"):]
        assert "BUDGET-STOP" in body
        assert body.index("BUDGET-STOP") < body.index("pip install"), \
            "the guard must run before anything is installed"

    def test_every_working_step_is_gated_on_the_guard(self):
        import yaml
        steps = yaml.safe_load(Path(".github/workflows/watch.yml").read_text()
                               )["jobs"]["check"]["steps"]
        for name in ("Install", "Open the vault", "Check"):
            step = next(s for s in steps if s.get("name") == name)
            assert "guard.outputs.stop" in str(step.get("if", "")), name

    def test_the_stop_file_is_committed_so_it_survives_the_runner(self):
        """A guard written to a runner's disk and thrown away with it would
        stop exactly one run."""
        source = Path(".github/workflows/watch.yml").read_text()
        save = source[source.index("- name: Save"):]
        assert "BUDGET-STOP" in save[:save.index("git commit")]



class TestExemptIsNotTheSameAsFree:
    """Public repositories are not charged for Actions. This counted every
    minute as billable "to be conservative", which meant a guard that would
    shout about an allowance nothing was drawing on - and a warning that
    fires when nothing is wrong is a warning that gets muted."""

    # The fixture has to say WHICH KIND of minute it is seeding, because that
    # is the distinction under test. It used to seed a bare number per day,
    # which is the shape that made "exempt" a property of the month rather
    # than of the minute.
    def spent_month(self, state, per_day=195.0, label="drawing"):
        state.data["actions"] = {
            "month": "2026-09",
            "days": {f"2026-09-{d:02d}": {label: per_day} for d in range(1, 13)},
            "runs": 500}

    def test_a_public_repo_is_counted_but_never_alarming(self, bench):
        cfg, state = bench
        state.data["repo"] = {"visibility": "public", "runner": "ubuntu-latest"}
        self.spent_month(state, label="exempt")
        out = budget.record(state, 1.0, cfg=cfg, now=SEPT)
        assert out["state"] == "exempt"
        assert out["should_stop"] is False
        assert out["drawing_minutes"] == 0.0
        assert out["exempt_minutes"] > 2000, "it still counts what it spends"
        assert out["used"] > 2000
        assert "drawing on the allowance" in out["text"]
        assert "one meter per account" in out["text"], "no blind spot named"

    def test_the_same_month_on_a_private_repo_is_a_problem(self, bench):
        cfg, state = bench
        state.data["repo"] = {"visibility": "private"}
        self.spent_month(state)
        out = budget.record(state, 1.0, cfg=cfg, now=SEPT)
        assert out["state"] == "over" and out["drawing_minutes"] > 2000

    def test_not_knowing_means_assuming_you_are_charged(self, bench):
        """Being wrong that way costs a sentence. The other way costs money."""
        cfg, state = bench
        state.data.pop("repo", None)
        self.spent_month(state)
        out = budget.record(state, 1.0, cfg=cfg, now=SEPT)
        assert out["label"] == "unknown"
        assert out["drawing_minutes"] > 2000

    def test_config_can_override_what_github_says(self, bench):
        cfg, state = bench
        state.data["repo"] = {"visibility": "public"}
        cfg.set("budget.charged", True)
        self.spent_month(state)
        assert budget.record(state, 1.0, cfg=cfg, now=SEPT)["state"] == "over"

    def test_an_exempt_repo_never_writes_the_stop_file(self, bench):
        cfg, state = bench
        state.data["repo"] = {"visibility": "public", "runner": "ubuntu-latest"}
        self.spent_month(state, per_day=400.0, label="exempt")
        out = budget.record(state, 1.0, cfg=cfg, now=SEPT)
        assert out["should_stop"] is False, "it would stop a bot costing nothing"

    def test_one_minute_reads_as_one_minute(self, bench):
        cfg, state = bench
        state.data["repo"] = {"visibility": "public", "runner": "ubuntu-latest"}
        out = budget.record(state, 1.0, cfg=cfg, now=SEPT)
        assert "1 runner minute this month" in out["text"], out["text"]


class TestTwoKindsOfMinuteCountedSeparately:
    """An account's 3,000 included minutes can hit 100% with weeks of the
    month left while this bot says nothing - because the minutes that
    exhausted them were spent in private repositories it cannot see.

    What it had been saying, "None of it draws on the allowance", was true of
    its own minutes and read like a statement about the account.
    """

    def ledger(self, **kw):
        from autotrader.budget import Ledger
        return Ledger(month="2026-09", allowance=3000, **kw)

    def test_a_minute_keeps_the_label_it_was_spent_under(self, tmp_path):
        """The repository going private must not relabel the days before it.

        A single `charged` flag on the month did exactly that: read the
        ledger after the change and every minute since the 1st was suddenly
        allowance-drawing.
        """
        from autotrader import budget
        from autotrader.state import State

        state = State(path=tmp_path / "state.json")
        state.data["repo"] = {"visibility": "public", "runner": "ubuntu-latest"}
        budget.record(state, 5.0, now=NOW)
        assert budget.load(state, now=NOW).exempt == 5.0

        # And now it is private.
        state.data["repo"] = {"visibility": "private", "runner": "ubuntu-latest"}
        budget.record(state, 3.0, now=NOW)
        after = budget.load(state, now=NOW)
        assert after.exempt == 5.0, "yesterday's free minutes were relabelled"
        assert after.drawing == 3.0
        assert after.used == 8.0

    def test_an_unlabelled_minute_counts_as_drawing(self):
        """The old ledger was a bare number per day. A number carries no
        label, so it cannot be called exempt now."""
        from autotrader.budget import load
        from types import SimpleNamespace
        state = SimpleNamespace(data={"actions": {
            "month": "2026-09", "days": {"2026-09-13": 10.0}, "runs": 4}})
        led = load(state, None, now=NOW)
        assert led.unknown == 10.0
        assert led.drawing == 10.0, "unlabelled must count against you"
        assert led.exempt == 0.0

    def test_the_exempt_verdict_says_what_it_cannot_see(self):
        led = self.ledger(days={"2026-09-13": {"exempt": 10.0}},
                          label="exempt", why="it is public")
        v = led.verdict(NOW)
        assert v["state"] == "exempt"
        assert v["exempt_minutes"] == 10.0 and v["drawing_minutes"] == 0.0
        assert "one meter per account" in v["text"], v["text"]
        assert "cannot tell you how much of the allowance is left" in v["text"]

    def test_no_verdict_ever_omits_the_blind_spot_when_it_is_reassuring(self):
        """Every state that could be read as "you are fine" has to carry it."""
        from autotrader.budget import Ledger
        for days, label in (
            ({"2026-09-13": {"exempt": 10.0}}, "exempt"),
            ({"2026-09-13": {"drawing": 1.0}, "2026-09-12": {"drawing": 1.0}}, "drawing"),
        ):
            v = Ledger(month="2026-09", days=days, allowance=3000,
                       label=label, why="because").verdict(NOW)
            if v["state"] in ("exempt", "ok", "early"):
                assert Ledger.BLIND_SPOT in v["text"], (v["state"], v["text"])

    def test_days_to_reset_counts_the_day_it_is_asked_on(self):
        from datetime import datetime, timezone
        led = self.ledger()
        # 13 September, 30-day month: the 13th plus 17 more.
        assert led.days_to_reset(
            datetime(2026, 9, 13, 20, 0, tzinfo=timezone.utc)) == 18
        assert led.days_to_reset(
            datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc)) == 1

    def test_a_larger_runner_on_a_public_repo_is_not_exempt(self):
        from autotrader.budget import label_for
        from types import SimpleNamespace
        state = SimpleNamespace(data={"repo": {
            "visibility": "public", "runner": "ubuntu-latest-16-cores"}})
        label, why = label_for(None, state)
        assert label == "drawing", why
        assert "standard runners" in why

    def test_a_public_repo_on_a_standard_runner_is_exempt_and_says_why(self):
        from autotrader.budget import label_for
        from types import SimpleNamespace
        state = SimpleNamespace(data={"repo": {
            "visibility": "public", "runner": "ubuntu-latest"}})
        label, why = label_for(None, state)
        assert label == "exempt"
        assert "public" in why and "standard runners" in why

    def test_knowing_nothing_is_not_the_same_as_knowing_it_is_free(self):
        from autotrader.budget import label_for
        from types import SimpleNamespace
        label, why = label_for(None, SimpleNamespace(data={}))
        assert label == "unknown"
        assert "nothing has told this bot" in why

    def test_every_runner_in_this_repository_is_one_github_gives_away(self):
        """Half of the exemption is the runner, and nothing at runtime can
        see the label that was asked for. This is where that half is held."""
        import re
        from pathlib import Path
        from autotrader.budget import FREE_ON_PUBLIC
        root = Path(__file__).resolve().parent.parent / ".github" / "workflows"
        for path in sorted(root.glob("*.yml")):
            for label in re.findall(r"runs-on:\s*(\S+)", path.read_text()):
                assert label in FREE_ON_PUBLIC, f"{path.name}: {label}"
