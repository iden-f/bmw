"""Counters that disagree with the rows they summarise.

Every one of these was found by replaying the live state file through the code
rather than by reading it: the arithmetic all looked fine, and the numbers were
wrong anyway. They are grouped here because they are one class, not five bugs -
a figure computed over a different population from the one it names.
"""

from __future__ import annotations

import copy
import gzip
import json
import random
import string
from pathlib import Path

import pytest

from autotrader import dashboard, diagnose, insight, invariants, runner
from autotrader.config import Config
from autotrader.state import State


def a_car(i, **kw):
    row = {"id": f"c{i}", "status": "active", "search_id": "s",
           "first_seen": f"2026-09-{(i % 27) + 1:02d}T00:00:00+00:00",
           "title": f"2020 Honda Civic {i}", "make": "Honda", "model": "Civic",
           "year": 2020, "price": 50000 + i,
           # Every car has to be accounted for or a different invariant fires
           # first and this file stops testing what it is about.
           "notified": True, "notified_at": "2026-09-20T00:00:00+00:00"}
    row.update(kw)
    return row


class TestTheCapCountsBeforeItCuts:
    """"Hidden by a rule: 430" over a state holding 479 of them.

    The payload is capped so a browser does not download a megabyte, and the
    cap eats hidden cars first because they sort last. Counting afterwards made
    the page state the smaller number as though it were the true one - and the
    invariant meant to catch that compared the count against the same capped
    list, so it was arithmetic that could not disagree with itself.
    """

    def payload(self, tmp_path, cap, hidden=40, live=5):
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.set("dashboard.max_listings", cap)
        search = cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=25", "Civic")
        rows = {}
        for i in range(live):
            rows[f"c{i}"] = a_car(i, search_id=search.id)
        for i in range(live, live + hidden):
            rows[f"c{i}"] = a_car(
                i, search_id=search.id, filtered=True,
                filter_reason="year 2026 above maximum 2020",
                filter_rule="max_year", notified=False, notified_at=None,
                quiet_reason="hidden by your rules: year 2026 above maximum 2020")
        state = State({"listings": rows, "runs": []}, path=tmp_path / "state.json")
        return cfg, state, dashboard.build_payload(cfg, state, {})

    def test_the_counts_describe_the_state_not_the_slice(self, tmp_path):
        _, _, payload = self.payload(tmp_path, cap=10)
        assert payload["health"]["counts"]["filtered"] == 40
        assert payload["health"]["counts"]["active"] == 5
        assert len(payload["listings"]) == 10

    def test_it_says_how_many_it_left_behind(self, tmp_path):
        _, _, payload = self.payload(tmp_path, cap=10)
        assert payload["health"]["left_out"] == 35

    def test_nothing_is_left_behind_when_everything_fits(self, tmp_path):
        _, _, payload = self.payload(tmp_path, cap=500)
        assert payload["health"]["left_out"] == 0

    def test_the_invariant_can_now_actually_fail(self, tmp_path):
        """The rule was a tautology. Break the count on purpose and it must
        notice - otherwise it is decoration."""
        cfg, state, payload = self.payload(tmp_path, cap=10)
        assert not invariants.check(cfg, state, None, payload, None)
        payload["health"]["counts"]["filtered"] = 430
        broken = invariants.check(cfg, state, None, payload, None)
        assert any(v.rule == "counts-reconcile" for v in broken)

    def test_silently_leaving_cars_out_is_a_violation(self, tmp_path):
        cfg, state, payload = self.payload(tmp_path, cap=10)
        payload["health"]["left_out"] = 0
        broken = invariants.check(cfg, state, None, payload, None)
        assert any("does not say so" in v.detail for v in broken)

    def test_publishing_more_than_state_holds_is_a_violation(self, tmp_path):
        cfg, state, payload = self.payload(tmp_path, cap=500)
        payload["listings"].append(
            a_car(999, search_id=payload["listings"][0]["search_id"],
                  filtered=True, filter_reason="x"))
        broken = invariants.check(cfg, state, None, payload, None)
        assert any(v.rule == "counts-reconcile" for v in broken)


class TestAFiringThatStoodDownIsNotACheck:
    """Ten runs, five of them stand-downs, published as "10 checks" averaging
    52 seconds - on the same screen as a coverage tile reading five."""

    RUNS = [
        {"at": "2026-09-23T18:00:00+00:00", "ok": True, "skipped": True, "duration_s": 0.0},
        {"at": "2026-09-23T17:00:00+00:00", "ok": True, "searches_run": 3, "duration_s": 100.0},
        {"at": "2026-09-23T16:00:00+00:00", "ok": True, "skipped": True, "duration_s": 0.0},
        {"at": "2026-09-23T15:00:00+00:00", "ok": True, "searches_run": 3, "duration_s": 108.0},
    ]

    def spent(self, monkeypatch):
        monkeypatch.setenv("AUTOTRADER_NOW", "2026-09-23T18:30:00+00:00")
        return insight.minutes_spent(self.RUNS)

    def test_the_mean_is_of_the_checks_that_happened(self, monkeypatch):
        out = self.spent(monkeypatch)
        assert out["checks"] == 2
        assert out["mean_seconds"] == 104.0

    def test_the_firings_are_still_counted_separately(self, monkeypatch):
        assert self.spent(monkeypatch)["firings"] == 4

    def test_github_still_bills_for_the_stand_downs(self, monkeypatch):
        """A stand-down starts a job, and every job is rounded up to a whole
        minute. It costs a minute whether or not it read anything."""
        assert self.spent(monkeypatch)["billed_minutes"] == 8

    def test_the_gap_is_measured_from_the_last_real_check(self, monkeypatch):
        monkeypatch.setenv("AUTOTRADER_NOW", "2026-09-23T18:30:00+00:00")
        state = State({"runs": self.RUNS})
        # 17:00, not the 18:00 stand-down.
        assert runner._minutes_since_last_ok(state) == pytest.approx(90.0)


class TestTheFeedKeepsWhatIsRare:
    """347 arrivals in a 400-row budget pushed every price drop off the end."""

    def entries(self):
        rows = []
        for i in range(500):
            rows.append(a_car(i, first_seen=f"2026-09-20T{i % 24:02d}:00:00+00:00"))
        # One old car whose price moved, which must survive the cut.
        rows.append(a_car(999, first_seen="2026-09-01T00:00:00+00:00",
                          price_history=[{"at": "2026-09-01T00:00:00+00:00", "price": 60000},
                                         {"at": "2026-09-02T00:00:00+00:00", "price": 55000}]))
        return rows

    def test_a_price_drop_is_not_crowded_out_by_arrivals(self):
        events = insight.events(self.entries(), limit=50)
        assert len(events) == 50
        assert any(e["kind"] == "price_drop" for e in events)

    def test_it_is_still_one_list_newest_first(self):
        events = insight.events(self.entries(), limit=50)
        stamps = [e["at"] for e in events]
        assert stamps == sorted(stamps, reverse=True)

    def test_nothing_is_dropped_when_everything_fits(self):
        assert len(insight.events(self.entries(), limit=5000)) > 500


class TestACaptureYouCanActuallyRead:
    def test_an_oversized_page_is_shortened_not_corrupted(self, tmp_path):
        """The guard used to slice a finished gzip stream, which produces a
        file that raises rather than a smaller capture - exactly when the
        capture is the only evidence there is."""
        random.seed(3)
        text = "".join(random.choice(string.printable[:62]) for _ in range(200_000))
        out = diagnose.write_raw(text, "demo", root=tmp_path, limit=2000)
        assert out is not None
        assert out.stat().st_size <= 2000
        body = gzip.decompress(out.read_bytes()).decode()
        assert text.startswith(body)
        assert len(body) > 100

    def test_an_ordinary_page_is_kept_whole(self, tmp_path):
        page = "<html>" + "x" * 50_000 + "</html>"
        out = diagnose.write_raw(page, "small", root=tmp_path)
        assert gzip.decompress(out.read_bytes()).decode() == page


class TestTheExampleConfigCannotDrift:
    """It is referenced by no test, no code and no workflow, so it rotted:
    three health settings, four notify_on keys and every per-search rule
    added since it was written were missing from it."""

    def test_it_is_the_defaults_plus_one_example_search(self):
        from autotrader.config import DEFAULTS
        example = json.loads(Path("config.example.json").read_text())
        searches = example.pop("searches")
        expected = copy.deepcopy(DEFAULTS)
        expected.pop("searches", None)
        assert example == expected
        assert len(searches) == 1

    def test_the_example_search_shows_what_the_link_drops(self):
        example = json.loads(Path("config.example.json").read_text())
        rules = example["searches"][0]["filters"]
        assert rules["models"] and rules["min_year"] and rules["max_distance_km"]

    def test_it_loads(self, tmp_path):
        target = tmp_path / "config.json"
        target.write_text(Path("config.example.json").read_text())
        cfg = Config.load(target)
        assert len(list(cfg.active_searches)) == 1
