"""Every claim this bot makes about itself, and the evidence behind it.

The class: a number that measures the thing producing it. Four have been
wrong here for that reason.

  * coverage counted runs whose exit code was zero, and called a run that
    read two hundred listings "not a check" because a bookkeeping invariant
    tripped afterwards;
  * the schedule counter recorded 2 firings where the truth was 5, because
    the deduplicated ones exited before the code that writes a run down;
  * `complained` was subtracted across two different populations and went
    negative;
  * the cost ledger says how many minutes this bot spent, and cannot see a
    job that was killed before it could write anything down.

The first three were fixed. The fourth cannot be - not without the API - so
the number says what it is: a floor.

This file is the guard against the fifth. Each test names one claim and
either proves it against something the bot did not produce, or asserts that
the claim says out loud what it cannot see.
"""
from __future__ import annotations

from pathlib import Path


from autotrader import budget, clock, insight


class TestTheCostLedgerSaysItIsAFloor:
    def test_the_blind_spot_names_both_things_it_cannot_see(self):
        said = budget.Ledger.BLIND_SPOT
        assert "one meter per account" in said, \
            "an allowance is per account and this bot watches one repository"
        assert "killed" in said and "billed" in said, \
            "a job killed before it wrote anything is billed and uncounted"

    def test_the_page_prints_it_where_the_number_is(self):
        app = Path("docs/app.js").read_text(encoding="utf-8")
        assert "blind_spot" in app, \
            "the caveat has to be beside the figure, not in a document"

    def test_a_run_that_crashes_is_still_charged(self):
        """Charged from a finally block, so only a KILL escapes it.

        The minutes are spent whether or not the run worked, so they are
        recorded from the same `finally` that writes the run down - not from
        the happy path, which is where a counter of this kind usually lives
        and is exactly why it usually undercounts.
        """
        import inspect

        from autotrader import runner
        source = inspect.getsource(runner.run)
        wrote = source.rindex("_write_the_run_down")
        assert "finally:" in source[:wrote], \
            "the run must be written down from a finally block"
        charged = inspect.getsource(runner._write_the_run_down)
        assert "_charge_the_budget" in charged, \
            "and the minutes charged as part of writing it down"


class TestCoverageMeasuresTheSiteNotItself:
    """Two independent counts of the same window have to agree."""

    @staticmethod
    def runs(n, *, every_minutes=120, **over):
        from datetime import timedelta
        now = clock.now()
        rows = []
        for i in range(n):
            row = {"at": (now - timedelta(minutes=every_minutes * i + 5)).isoformat(),
                   "ok": True, "searches_run": 2, "searches_failed": 0,
                   "trigger": "schedule", "duration_s": 30.0}
            row.update(over)
            rows.append(row)
        return rows

    def test_the_slots_covered_never_exceed_the_slots_expected(self):
        for n in (1, 5, 13, 40):
            cov = insight.coverage(self.runs(n), 120, window_hours=24,
                                   since_change=None)
            assert cov["slots_covered"] <= cov["expected"], (n, cov)
            assert 0 <= cov["pct"] <= 100, (n, cov)

    def test_the_scheduled_share_never_exceeds_the_whole(self):
        rows = self.runs(6) + self.runs(6, trigger="push")
        cov = insight.coverage(rows, 120, window_hours=24, since_change=None)
        assert cov["slots_scheduled"] <= cov["slots_covered"], cov
        assert cov["pct_scheduled"] <= cov["pct"] + 0.05, cov

    def test_the_trigger_tally_adds_up_to_the_checks(self):
        rows = self.runs(4) + self.runs(3, trigger="push") + \
            self.runs(2, trigger="workflow_dispatch")
        cov = insight.coverage(rows, 120, window_hours=24, since_change=None)
        assert sum(cov["by_trigger"].values()) == cov["successful"], cov

    def test_a_burst_cannot_make_coverage_exceed_the_schedule(self):
        """Forty checks in one hour is one covered slot, not forty."""
        rows = self.runs(40, every_minutes=1)
        cov = insight.coverage(rows, 120, window_hours=24, since_change=None)
        assert cov["checks"] == 40
        assert cov["slots_covered"] <= 2, cov

    def test_the_gap_is_never_longer_than_the_window(self):
        for n in (1, 3, 12):
            cov = insight.coverage(self.runs(n), 120, window_hours=24,
                                   since_change=None)
            assert (cov["longest_gap_minutes"] or 0) <= \
                cov["window_hours"] * 60 + 1, cov


class TestEveryClaimOnThePageCarriesItsSample:
    """A percentage from four cars is a coincidence with a percent sign."""

    def entries(self, n, **over):
        rows = []
        for i in range(n):
            row = {"id": f"{i:08d}-0000-0000-0000-00000000000{i % 10}",
                   "make": "Honda", "model": "Civic", "year": 2021,
                   "price": 60000 + i * 1000, "mileage_km": 60000,
                   "status": "active", "filtered": False}
            row.update(over)
            rows.append(row)
        return rows

    def test_a_thin_cohort_gets_a_reason_not_a_percentage(self):
        out = insight.comparables(self.entries(4))
        for row in out.values():
            assert "pct" not in row, row
            assert row.get("why_not"), row

    def test_a_middling_cohort_gets_a_rank_not_a_percentage(self):
        out = insight.comparables(self.entries(9))
        row = next(iter(out.values()))
        assert "pct" not in row and row.get("rank"), row

    def test_a_percentage_only_appears_with_enough_peers(self):
        out = insight.comparables(self.entries(14))
        row = next(iter(out.values()))
        assert row.get("pct") is not None and row["sample"] >= \
            insight.MIN_FOR_A_PERCENTAGE, row

    def test_no_car_is_ever_compared_against_itself(self):
        out = insight.comparables(self.entries(14))
        for row in out.values():
            assert row["sample"] == 13, row
