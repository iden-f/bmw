"""The checks that would have caught every serious bug in this rebuild.

Each one here corresponds to something that actually went wrong and raised
nothing at the time: cars announced as sold that were still on the front page,
a search reporting zero listings while working perfectly, a car stored and
never mentioned because an earlier search had marked it seen.
"""
import json

import pytest

from autotrader import invariants, runner as runner_mod
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
    cfg.save()

    sink = Capture()
    use_channels(monkeypatch, runner_mod, [sink])
    html = fixture_html("search_next_data")

    def go(page=None, **kw):
        return run(cfg, State.load(tmp_path / "state.json"),
                   fetcher=FakeFetcher(page or html), env={}, **kw)

    return type("Bench", (), {
        "cfg": cfg, "sink": sink, "run": staticmethod(go), "path": tmp_path,
        "html": html,
        "state": staticmethod(lambda: State.load(tmp_path / "state.json")),
    })


def rules(violations):
    return sorted({v.rule for v in violations})


class TestAHealthyRunIsClean:
    def test_a_normal_run_violates_nothing(self, bench):
        report = bench.run()
        assert report.ok and not report.invariants
        assert invariants.check(bench.cfg, bench.state(), report) == []

    def test_a_filtered_run_violates_nothing(self, bench):
        bench.cfg.set("filters.max_price", 90000)
        bench.cfg.save()
        report = bench.run()
        assert report.ok and not report.invariants

    def test_a_run_that_hid_call_for_price_cars_violates_nothing(self, bench):
        bench.cfg.set("filters.require_price", True)
        bench.cfg.save()
        report = bench.run()
        assert report.ok and not report.invariants

    def test_a_run_holding_alerts_violates_nothing(self, bench):
        """A queued car is owed, not missing."""
        report = bench.run(notify=False)
        assert not report.invariants
        assert any(e.get("pending") for e in bench.state().listings.values())


class TestOwnership:
    def test_a_car_with_no_owner_is_caught(self, bench):
        bench.run()
        state = bench.state()
        next(iter(state.listings.values()))["search_id"] = ""
        assert "one-owner" in rules(invariants.check(bench.cfg, state))

    def test_a_car_owned_by_a_search_that_is_gone_is_caught(self, bench):
        """The bug this mirrors: a watch reporting zero cars while working."""
        bench.run()
        state = bench.state()
        next(iter(state.listings.values()))["search_id"] = "deleted-search"
        assert "one-owner" in rules(invariants.check(bench.cfg, state))


class TestEveryCarIsAccountedFor:
    def test_a_car_nobody_decided_about_is_caught(self, bench):
        """The silent failure: stored, never mentioned, nothing looks wrong."""
        bench.run()
        state = bench.state()
        entry = next(iter(state.listings.values()))
        for key in ("notified_at", "quiet_reason", "pending"):
            entry.pop(key, None)
        entry["notified"] = False

        broken = invariants.check(bench.cfg, state)
        assert "accounted-for" in rules(broken)

    def test_delivered_and_queued_at_once_is_caught(self, bench):
        bench.run()
        state = bench.state()
        entry = next(iter(state.listings.values()))
        entry["pending"] = {"kind": "new"}
        entry["notified"] = True
        assert "accounted-for" in rules(invariants.check(bench.cfg, state))

    def test_a_hidden_car_carries_the_rule_that_hid_it(self, bench):
        bench.cfg.set("filters.max_price", 90000)
        bench.cfg.save()
        bench.run()

        hidden = [e for e in bench.state().listings.values() if e.get("filtered")]
        assert hidden
        for entry in hidden:
            assert entry["filter_reason"]
            assert entry["quiet_reason"].startswith("hidden by your rules")

    def test_a_hidden_car_with_no_reason_is_caught(self, bench):
        bench.cfg.set("filters.max_price", 90000)
        bench.cfg.save()
        bench.run()
        state = bench.state()
        hidden = next(e for e in state.listings.values() if e.get("filtered"))
        hidden["filter_reason"] = ""
        assert "hidden-has-a-reason" in rules(invariants.check(bench.cfg, state))


class TestStatesThatCannotBothBeTrue:
    def test_active_with_a_removal_time_is_caught(self, bench):
        bench.run()
        state = bench.state()
        next(iter(state.listings.values()))["removed_at"] = "2026-01-01T00:00:00+00:00"
        assert "not-both" in rules(invariants.check(bench.cfg, state))

    def test_a_call_for_price_flag_that_lies_is_caught(self, bench):
        bench.run()
        state = bench.state()
        entry = next(e for e in state.listings.values() if e.get("price"))
        entry["unpriced"] = True
        assert "not-both" in rules(invariants.check(bench.cfg, state))

    def test_a_car_stuck_past_the_grace_period_is_caught(self, bench):
        """Either it came back or it went; it cannot sit at four misses."""
        bench.run()
        state = bench.state()
        next(iter(state.listings.values()))["misses"] = 4
        assert "grace-bounded" in rules(invariants.check(bench.cfg, state))


class TestTheNumbersAgree:
    def test_a_hidden_count_that_disagrees_with_state_is_caught(self, bench):
        bench.cfg.set("filters.max_price", 90000)
        bench.cfg.save()
        report = bench.run()
        assert report.filtered_out > 0

        report.filtered_out += 3
        broken = invariants.check(bench.cfg, bench.state(), report)
        assert "counts-reconcile" in rules(broken)

    def test_a_dashboard_that_disagrees_with_itself_is_caught(self, bench):
        from autotrader.dashboard import build_payload
        bench.run()
        state = bench.state()
        payload = build_payload(bench.cfg, state, {})
        payload["health"]["counts"]["active"] += 5

        broken = invariants.check(bench.cfg, state, None, payload)
        assert "counts-reconcile" in rules(broken)

    def test_the_dashboard_and_state_agree_after_a_real_run(self, bench):
        from autotrader.dashboard import build_payload
        bench.cfg.set("filters.max_price", 90000)
        bench.cfg.save()
        report = bench.run()
        state = bench.state()
        assert invariants.check(bench.cfg, state, report,
                                build_payload(bench.cfg, state, {})) == []


class TestAViolationFailsTheRun:
    def test_the_run_fails_and_says_which_rule(self, bench, monkeypatch):
        bench.run()

        def corrupt(cfg, state, report=None, payload=None, seen=None):
            return [invariants.Violation("one-owner", "invented", ["x"], 1)]

        monkeypatch.setattr(runner_mod.invariants, "check", corrupt)
        report = bench.run()

        assert not report.ok
        assert report.invariants and "one-owner" in report.invariants[0]
        assert any("bookkeeping is inconsistent" in e for e in report.errors)

    def test_the_evidence_is_written_out(self, bench, monkeypatch):
        bench.run()
        monkeypatch.setattr(
            runner_mod.invariants, "check",
            lambda cfg, state, report=None, payload=None, seen=None: [
                invariants.Violation("not-both", "invented", ["x"], 1)])
        report = bench.run()

        written = bench.path / "diagnostics" / "invariants.json"
        assert written.exists()
        assert str(written.relative_to(bench.path)) in " ".join(report.diagnostics)
        saved = json.loads(written.read_text())
        assert saved["violations"][0]["rule"] == "not-both"


class TestBackfillingOlderState:
    def test_bookkeeping_older_entries_never_had_is_reconstructed(self, tmp_path):
        state = State(path=tmp_path / "s.json")
        state.listings["a"] = {"id": "a", "status": "active", "notified": True,
                               "price": 90000, "last_seen": "2026-01-01T00:00:00+00:00"}
        state.listings["b"] = {"id": "b", "status": "active", "notified": True,
                               "price": None, "filtered": True,
                               "filter_reason": "price above maximum"}
        state.save()

        reloaded = State.load(tmp_path / "s.json")
        a, b = reloaded.listings["a"], reloaded.listings["b"]
        assert a["notified_at"] == "2026-01-01T00:00:00+00:00"
        assert a["notified_at_backfilled"] is True
        assert a["unpriced"] is False
        assert b["quiet_reason"].startswith("hidden by your rules")
        assert b["unpriced"] is True

    def test_a_car_that_really_was_never_handled_still_shows_up(self, tmp_path):
        """The backfill must not paper over the failure it exists to expose."""
        state = State(path=tmp_path / "s.json")
        state.listings["lost"] = {"id": "lost", "status": "active",
                                  "notified": False, "price": 90000,
                                  "search_id": "s"}
        state.save()

        reloaded = State.load(tmp_path / "s.json")
        assert not reloaded.listings["lost"].get("notified_at")
        assert not reloaded.listings["lost"].get("quiet_reason")

    def test_upgrading_twice_changes_nothing(self, tmp_path):
        state = State(path=tmp_path / "s.json")
        state.listings["a"] = {"id": "a", "status": "active", "notified": True,
                               "price": 1000, "last_seen": "2026-01-01T00:00:00+00:00"}
        state.upgrade()
        once = json.dumps(state.data, sort_keys=True)
        state.upgrade()
        assert json.dumps(state.data, sort_keys=True) == once


class TestAScopeChangeIsABaseline:
    """Widening a search does not discover cars; it stops ignoring them.

    Reading ten pages instead of three brought in 120 cars that had been in
    scope the whole time, and every one of them arrived labelled as a new
    listing. The cars were real, the label was not.
    """

    def _shrink(self, html, keep):
        import re
        payload = json.loads(re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                                       html, re.S).group(1))
        results = payload["props"]["pageProps"]["searchResults"]
        results["listings"] = results["listings"][:keep]
        return re.sub(r'(id="__NEXT_DATA__"[^>]*>).*?(</script>)',
                      lambda m: m.group(1) + json.dumps(payload) + m.group(2),
                      html, count=1, flags=re.S)

    def test_a_first_run_still_announces_what_it_finds(self, bench):
        """Nothing was in scope before, so these really are new to you."""
        report = bench.run()
        assert report.new > 0
        assert not report.baselines
        assert [c for batch in bench.sink.digests for c in batch]

    def test_relaxing_a_filter_records_rather_than_announces(self, bench):
        bench.cfg.set("filters.max_price", 85000)
        bench.cfg.save()
        bench.run()
        bench.sink.digests.clear()

        bench.cfg.set("filters.max_price", 200000)
        bench.cfg.save()
        report = bench.run()

        # Counted as "back inside your rules", not as "new". They have been on
        # the market the whole time - a rule of yours was hiding them - and a
        # digest that calls them new describes cars the user may have already
        # scrolled past.
        assert report.qualified > 0, \
            "the newly admitted cars were not noticed at all"
        assert report.new == 0, "cars that were always listed were called new"
        assert report.baselines == ["Example search"]
        assert not [c for batch in bench.sink.digests for c in batch], \
            "cars that were always in scope were announced as discoveries"

    def test_and_says_so_rather_than_going_quiet_for_no_reason(self, bench):
        bench.cfg.set("filters.max_price", 85000)
        bench.cfg.save()
        bench.run()

        bench.cfg.set("filters.max_price", 200000)
        bench.cfg.save()
        report = bench.run()
        assert any("what this search covers has changed" in w
                   for w in report.warnings)

    def test_reading_deeper_is_a_scope_change(self, bench):
        """The exact case: three pages to ten."""
        bench.cfg.set("scraping.max_pages", 3)
        bench.cfg.save()
        bench.run()
        bench.sink.digests.clear()

        bench.cfg.set("scraping.max_pages", 10)
        bench.cfg.save()
        assert bench.run().baselines == ["Example search"]

    def test_editing_the_link_is_a_scope_change(self, bench):
        bench.run()
        bench.cfg.data["searches"][0]["url"] = f"{BASE}?rcp=25&prx=-2"
        bench.cfg.save()
        assert bench.run().baselines == ["Example search"]

    def test_an_unchanged_search_is_not_a_baseline(self, bench):
        bench.run()
        assert bench.run().baselines == []
        assert bench.run().baselines == []

    def test_a_baseline_car_carries_its_reason_not_a_hole(self, bench):
        bench.cfg.set("filters.max_price", 85000)
        bench.cfg.save()
        bench.run()
        bench.cfg.set("filters.max_price", 200000)
        bench.cfg.save()
        report = bench.run()

        assert not report.invariants
        fresh = [e for e in bench.state().listings.values()
                 if "starting point" in (e.get("quiet_reason") or "")]
        assert fresh, "baselined cars have no record of why they were quiet"

    def test_the_next_run_announces_normally_again(self, bench):
        """A baseline is one run, not a mute switch."""
        bench.cfg.set("scraping.max_pages", 3)
        bench.cfg.save()
        smaller = self._shrink(bench.html, 10)
        bench.run(smaller)

        bench.cfg.set("scraping.max_pages", 10)
        bench.cfg.save()
        bench.run(smaller)                      # the baseline
        bench.sink.digests.clear()

        report = bench.run(bench.html)          # nine more cars appear
        assert report.new == 9
        assert [c for batch in bench.sink.digests for c in batch]

    def test_a_baseline_does_not_call_anything_removed(self, bench):
        """Narrowing a search must not announce the excluded cars as sold."""
        bench.run()
        bench.cfg.set("filters.max_price", 85000)
        bench.cfg.save()
        for _ in range(3):
            report = bench.run()
        assert report.removed == 0


class TestTheDashboardShowsWhoWasNotToldAbout:
    def test_every_car_is_delivered_queued_or_quiet(self, bench):
        from autotrader.dashboard import build_payload
        bench.cfg.set("filters.max_price", 90000)
        bench.cfg.save()
        bench.run()

        accounted = build_payload(bench.cfg, bench.state(), {})["health"]["accounted"]
        assert accounted["unexplained"] == 0
        assert accounted["delivered"] + accounted["quiet"] > 0

    def test_a_hidden_car_publishes_the_reason_it_was_not_mentioned(self, bench):
        from autotrader.dashboard import build_payload
        bench.cfg.set("filters.max_price", 90000)
        bench.cfg.save()
        bench.run()

        published = build_payload(bench.cfg, bench.state(), {})["listings"]
        hidden = [l for l in published if l.get("filtered")]
        assert hidden
        assert all(l["quiet_reason"] for l in hidden)

    def test_a_car_nobody_decided_about_shows_up_in_the_count(self, bench):
        from autotrader.dashboard import build_payload
        bench.run()
        state = bench.state()
        entry = next(iter(state.listings.values()))
        for key in ("notified_at", "quiet_reason", "pending"):
            entry.pop(key, None)

        accounted = build_payload(bench.cfg, state, {})["health"]["accounted"]
        assert accounted["unexplained"] == 1

    def test_a_hidden_car_counted_as_live_is_caught(self, bench):
        from autotrader.dashboard import build_payload
        bench.cfg.set("filters.max_price", 90000)
        bench.cfg.save()
        bench.run()
        state = bench.state()
        payload = build_payload(bench.cfg, state, {})

        # Pretend a hidden car slipped into the live count.
        payload["health"]["counts"]["filtered"] -= 1
        payload["health"]["counts"]["active"] += 1

        assert "counts-reconcile" in rules(invariants.check(bench.cfg, state, None, payload))

    def test_a_car_the_run_never_looked_at_is_not_a_discrepancy(self, bench):
        """The check must not cry wolf over cars this run did not read."""
        bench.cfg.set("filters.max_price", 90000)
        bench.cfg.save()
        report = bench.run()
        state = bench.state()
        seen = {lid for lid in state.listings}

        # A hidden car from some other search that this run never touched.
        state.listings["elsewhere"] = {
            "id": "elsewhere", "status": "active", "filtered": True,
            "filter_reason": "price above maximum", "notified": True,
            "quiet_reason": "hidden by your rules: price above maximum",
            "search_id": bench.cfg.searches[0].id, "price": 999999,
            "unpriced": False,
        }
        assert invariants.check(bench.cfg, state, report, None, seen) == []


class TestQuietCorruption:
    """Shapes that mean something wrote half a change and stopped."""

    def test_gone_with_no_removal_time_is_caught(self, bench):
        bench.run()
        state = bench.state()
        entry = next(iter(state.listings.values()))
        entry["status"] = "gone"
        entry.pop("removed_at", None)
        assert "not-both" in rules(invariants.check(bench.cfg, state))

    def test_a_price_that_contradicts_its_own_history_is_caught(self, bench):
        """How a phantom price drop starts: two fields written apart."""
        bench.run()
        state = bench.state()
        entry = next(e for e in state.listings.values()
                     if e.get("price") and e.get("price_history"))
        entry["price"] = (entry["price"] or 0) + 5000
        assert "not-both" in rules(invariants.check(bench.cfg, state))

    def test_a_car_last_seen_before_it_was_first_seen_is_caught(self, bench):
        bench.run()
        state = bench.state()
        entry = next(iter(state.listings.values()))
        entry["last_seen"] = "2020-01-01T00:00:00+00:00"
        assert "not-both" in rules(invariants.check(bench.cfg, state))

    def test_a_real_run_satisfies_all_of_them(self, bench):
        bench.cfg.set("filters.max_price", 90000)
        bench.cfg.save()
        for _ in range(3):
            report = bench.run()
        assert report.ok and not report.invariants
        assert invariants.check(bench.cfg, bench.state(), report) == []


class TestPruningOnlyForgetsHistory:
    def test_a_car_still_for_sale_is_never_pruned_away(self, tmp_path):
        """Forgetting a live car makes the next run rediscover and re-announce
        it, which is the one thing state exists to prevent."""
        state = State(path=tmp_path / "s.json")
        for n in range(30):
            state.listings[f"gone{n}"] = {
                "id": f"gone{n}", "status": "gone",
                "last_seen": f"2026-01-{n % 28 + 1:02d}T00:00:00+00:00"}
        for n in range(10):
            state.listings[f"live{n}"] = {
                "id": f"live{n}", "status": "active",
                "last_seen": "2020-01-01T00:00:00+00:00"}   # oldest of all

        state.prune(keep_days=0, keep_max=20)

        assert all(f"live{n}" in state.listings for n in range(10))
        assert len(state.listings) == 20

    def test_switching_the_age_limit_off_does_not_delete_everything(self, tmp_path):
        """Zero means "no limit", the same as it does for the count cap."""
        state = State(path=tmp_path / "s.json")
        state.listings["old"] = {"id": "old", "status": "gone",
                                 "removed_at": "2019-01-01T00:00:00+00:00"}
        assert state.prune(keep_days=0, keep_max=0) == 0
        assert "old" in state.listings


class TestAQueueThatNeverDrains:
    """An alert owed for days is lost with extra steps."""

    def test_a_freshly_queued_alert_is_fine(self, bench):
        report = bench.run(notify=False)
        assert not report.invariants
        assert invariants.check(bench.cfg, bench.state(), report) == []

    def test_one_stuck_for_days_is_caught(self, bench):
        bench.run(notify=False)
        state = bench.state()
        entry = next(e for e in state.listings.values() if e.get("pending"))
        entry["pending"]["since"] = "2026-01-01T00:00:00+00:00"

        broken = invariants.check(bench.cfg, state)
        assert "accounted-for" in rules(broken)
        assert "not delivered" in str(broken[0])

    def test_it_clears_once_the_alert_goes_out(self, bench):
        bench.run(notify=False)
        state = bench.state()
        for entry in state.listings.values():
            if entry.get("pending"):
                entry["pending"]["since"] = "2026-01-01T00:00:00+00:00"
        state.save()

        bench.run()                       # channels are back
        assert invariants.check(bench.cfg, bench.state()) == []


class TestAnOwedAlertSurvives:
    """Deciding to keep quiet from now on is not a licence to cancel a debt."""

    def test_hiding_a_car_does_not_cancel_an_alert_it_is_owed(self, tmp_path):
        from autotrader.state import Change
        from autotrader.listing import Listing

        state = State(path=tmp_path / "s.json")
        car = Listing(id="1", url="u", title="2021 Honda Civic", price=90000,
                      price_source="detail", search_id="s")
        state.record(car)
        state.defer([Change(Change.PRICE_DROP, car, old_price=99000, new_price=90000)])

        state.silence("1", "hidden by your rules: price above maximum")

        assert state.listings["1"]["pending"], "the owed alert was thrown away"
        assert state.listings["1"]["notified"] is False
        assert state.listings["1"]["quiet_reason"]
        assert [c.kind for c in state.pending_changes()] == [Change.PRICE_DROP]

    def test_a_car_with_nothing_owed_is_simply_silenced(self, tmp_path):
        from autotrader.listing import Listing
        state = State(path=tmp_path / "s.json")
        state.record(Listing(id="1", url="u", title="t", price=9, search_id="s"))
        state.silence("1", "hidden by your rules: too cheap")

        assert state.listings["1"]["notified"] is True
        assert not state.listings["1"].get("pending")

    def test_pruning_never_forgets_a_car_still_owed_an_alert(self, tmp_path):
        state = State(path=tmp_path / "s.json")
        state.listings["owed"] = {
            "id": "owed", "status": "gone", "removed_at": "2019-01-01T00:00:00+00:00",
            "pending": {"kind": "removed", "since": "2019-01-01T00:00:00+00:00"}}
        state.listings["done"] = {
            "id": "done", "status": "gone", "removed_at": "2019-01-01T00:00:00+00:00"}

        state.prune(keep_days=30)
        assert "owed" in state.listings and "done" not in state.listings


class TestAlertsMovingHouse:
    """A changed ntfy topic is silence that looks like "nothing happened"."""

    def test_a_new_topic_is_announced_on_both(self, bench):
        bench.cfg.set("notifications.channels.ntfy.topic", "autotrader-first")
        bench.cfg.save()
        bench.run()
        bench.sink.alerts.clear()

        bench.cfg.set("notifications.channels.ntfy.topic", "autotrader-second")
        bench.cfg.save()
        report = bench.run()

        assert any("alerts moved" in s.lower() for s, _ in bench.sink.alerts)
        assert any("resubscribe" in w for w in report.warnings)
        # And at the old address, which is the only one anybody is subscribed
        # to at this point - saying that they moved, never where to. The old
        # topic may be one other people can read.
        assert "autotrader-first" in bench.sink.aimed_at
        for where, subject, body in bench.sink.aimed:
            assert "autotrader-second" not in subject + body, (where, body)
        assert not any("autotrader-second" in w or "autotrader-first" in w
                       for w in report.warnings)

    def test_an_unchanged_topic_says_nothing(self, bench):
        bench.cfg.set("notifications.channels.ntfy.topic", "autotrader-steady")
        bench.cfg.save()
        bench.run()
        bench.sink.alerts.clear()

        report = bench.run()
        assert not any("moved" in s.lower() for s, _ in bench.sink.alerts)
        assert not any("resubscribe" in w for w in report.warnings)


class TestRemovingASearch:
    def test_its_cars_stop_being_live_rather_than_becoming_orphans(self, bench):
        """You stopped watching. They are not sold, but nothing is checking
        them, so they cannot go on counting as live - and a car owned by a
        search that no longer exists fails the audit every run."""
        bench.run()
        assert any(e["status"] == "active" for e in bench.state().listings.values())

        bench.cfg.data["searches"] = []
        bench.cfg.save()
        report = bench.run()

        assert report.ok and not report.invariants
        entries = bench.state().listings
        assert all(e["status"] == "gone" for e in entries.values())
        assert all("was removed" in (e.get("quiet_reason") or "")
                   for e in entries.values())

    def test_the_audit_runs_after_housekeeping_not_before(self, bench):
        """Otherwise it validates a state the run then goes on to change."""
        bench.run()
        bench.cfg.data["searches"] = []
        bench.cfg.save()
        assert bench.run().invariants == []


class TestBeingToldAboutIt:
    def _break(self, monkeypatch, rule="one-owner"):
        monkeypatch.setattr(
            runner_mod.invariants, "check",
            lambda cfg, state, report=None, payload=None, seen=None: [
                invariants.Violation(rule, "invented", ["x"], 1)])

    def test_a_violation_is_reported_at_once(self, bench, monkeypatch):
        bench.run()
        bench.sink.alerts.clear()
        self._break(monkeypatch)
        bench.run()

        assert any("do not add up" in subject for subject, _ in bench.sink.alerts)
        told = " ".join(body for _, body in bench.sink.alerts)
        assert "one-owner" in told and "unreliable" in told

    def test_the_same_fault_does_not_repeat_every_run(self, bench, monkeypatch):
        bench.run()
        self._break(monkeypatch)
        bench.run()
        bench.sink.alerts.clear()
        bench.run()
        assert not [s for s, _ in bench.sink.alerts if "do not add up" in s]

    def test_a_different_fault_is_worth_saying_again(self, bench, monkeypatch):
        bench.run()
        self._break(monkeypatch)
        bench.run()
        bench.sink.alerts.clear()
        self._break(monkeypatch, rule="counts-reconcile")
        bench.run()
        assert any("do not add up" in s for s, _ in bench.sink.alerts)

    def test_clearing_up_is_noted(self, bench):
        bench.run()
        # A nested patcher, not monkeypatch.undo(). undo() reverts *every*
        # patch on the shared fixture - including the chdir into tmp_path -
        # so the third run here executed in the real repository, provisioned
        # itself a new ntfy topic and rewrote the config in the working tree.
        # It was one `git add -A` away from redirecting live alerts to a
        # topic nobody is subscribed to.
        with pytest.MonkeyPatch.context() as broken:
            self._break(broken)
            bench.run()
        report = bench.run()
        assert any("cleared" in w for w in report.warnings)
        assert report.ok


class TestACardThatForgetsThePrice:
    """A results card showing no price does not make the car call-for-price.

    The listing page had already told us what it costs. The flag was being set
    from the incoming observation rather than from the record, so a car with a
    price ended up flagged as having none - found by the audit on its first
    contact with the old platform's markup, not by anyone reading the code.
    """

    def test_the_flag_follows_the_record_not_the_card(self, tmp_path):
        from autotrader.listing import Listing

        state = State(path=tmp_path / "s.json")
        state.record(Listing(id="1", url="u", title="2021 Honda Civic", price=28000,
                             price_source="detail", search_id="s"))

        # Next run: the card omits the price, as they do.
        state.record(Listing(id="1", url="u", title="2021 Honda Civic", price=None,
                             search_id="s"))

        entry = state.listings["1"]
        assert entry["price"] == 28000
        assert entry["unpriced"] is False

    def test_a_car_that_never_had_a_price_is_still_flagged(self, tmp_path):
        from autotrader.listing import Listing
        state = State(path=tmp_path / "s.json")
        state.record(Listing(id="1", url="u", title="t", price=None, search_id="s"))
        state.record(Listing(id="1", url="u", title="t", price=None, search_id="s"))
        assert state.listings["1"]["unpriced"] is True


class TestARuleThatStopsApplying:
    """A car hidden by a rule, then not.

    A car above a price ceiling was correctly hidden and silenced with that
    rule as its reason. Later its card arrived with no price on it - which is
    not a rejection, so the car stopped being filtered while keeping the
    ceiling as its excuse for being quiet. Every run from then on failed the
    bookkeeping check and nobody was told about the car either way. It
    cleared itself only when the price came back.
    """

    # The captured page's car at $109,910, above the ceiling set below.
    ABOVE_CEILING = "d688f2af-dbd2-4e72-be59-0a91499a5a19"

    @pytest.fixture
    def bench(self, tmp_path, monkeypatch, fixture_html):
        from autotrader import runner as runner_mod
        from autotrader.config import Config
        from autotrader.runner import run
        from .helpers import Capture, FakeFetcher, use_channels

        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=25",
                       "Example search")
        cfg.set("scraping.delay_ms", 0)
        cfg.set("scraping.enrich_details", False)
        cfg.set("archive.mode", "off")
        cfg.set("filters.max_price", 105000)
        cfg.set("filters.require_price", True)
        cfg.save()
        sink = Capture()
        use_channels(monkeypatch, runner_mod, [sink])
        priced = fixture_html("search_next_data")
        # The same page, with that one car's price gone from its card.
        priceless = priced.replace(
            '"price":{"priceFormatted":"$ 109,910","priceRaw":109910',
            '"price":{"priceFormatted":"","priceRaw":null')
        assert priceless != priced, "fixture changed - the price markup moved"

        def go(html):
            return run(cfg, State.load(tmp_path / "state.json"),
                       fetcher=FakeFetcher(html), env={})

        return type("Bench", (), {
            "cfg": cfg, "sink": sink, "priced": priced, "priceless": priceless,
            "run": staticmethod(go),
            "state": staticmethod(lambda: State.load(tmp_path / "state.json")),
        })

    def test_the_rule_is_the_reason_only_while_the_rule_applies(self, bench):
        bench.run(bench.priced)
        entry = bench.state().listings[self.ABOVE_CEILING]
        assert entry["filtered"] and entry["quiet_reason"].startswith(
            "hidden by your rules")

        report = bench.run(bench.priceless)

        entry = bench.state().listings[self.ABOVE_CEILING]
        assert not entry.get("filtered"), "no price is not a rejection"
        assert not str(entry.get("quiet_reason") or "").startswith(
            "hidden by your rules"), (
                "a ceiling it is no longer being measured against cannot still "
                "be why it is being kept quiet")
        assert not report.invariants, report.invariants

    def test_and_the_car_is_still_accounted_for(self, bench):
        """Clearing the reason must not leave the car with no reason at all."""
        bench.run(bench.priced)
        bench.run(bench.priceless)

        entry = bench.state().listings[self.ABOVE_CEILING]
        assert (entry.get("notified_at") or entry.get("pending")
                or entry.get("quiet_reason")), (
            "the car is neither delivered, owed, nor deliberately quiet")

    def test_it_stays_clear_over_repeated_runs(self, bench):
        """The failure was not one bad run, it was every run after it."""
        bench.run(bench.priced)
        for _ in range(3):
            report = bench.run(bench.priceless)
            assert not report.invariants, report.invariants
