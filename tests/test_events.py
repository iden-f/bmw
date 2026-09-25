"""The standing record of the first time each market event really happens.

Six hours of live watching produced no price drop, no removal and no
relisting - or so the run counters said. Two of them had in fact happened, on
cars the filters hide, where nothing counts them. This is what notices.
"""
import json

import pytest

from autotrader import clock
from autotrader import events
from autotrader.listing import Listing
from autotrader.state import Change, State
from .helpers import a_check_later


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "EVENTS.md", tmp_path / "events.json"


def car(state, lid="1", price=100000, **kw):
    state.record(Listing(id=lid, url=f"https://www.autotrader.ca/offers/x-{lid}",
                         title="2022 Honda Civic", price=price,
                         price_source="detail", search_id="s"), **kw)
    return state.listings[lid]


class TestFindingTheFirstOfEachKind:
    def test_a_price_coming_down_is_recorded_with_both_figures(self, tmp_path, paths):
        state = State(path=tmp_path / "s.json")
        car(state, price=42399)
        car(state, price=42000)
        state.mark_notified(["1"])

        record = events.update(state, *paths)
        drop = record["first"]["price_drop"]
        assert drop["before"] == 42399 and drop["after"] == 42000
        assert "$399 off" in drop["detail"]
        assert "delivered at" in drop["delivered"]

    def test_a_price_going_up_is_a_different_event(self, tmp_path, paths):
        state = State(path=tmp_path / "s.json")
        car(state, price=100000)
        car(state, price=104000)
        record = events.update(state, *paths)
        assert "price_rise" in record["first"]
        assert "price_drop" not in record["first"]

    def test_a_removal_records_when_it_was_last_seen(self, tmp_path, paths):
        state = State(path=tmp_path / "s.json")
        car(state)
        state.mark_missing("s", set())
        a_check_later()
        state.mark_missing("s", set())

        gone = events.update(state, *paths)["first"]["removed"]
        assert gone["before"] == "active" and gone["after"] == "gone"
        assert "last seen" in gone["detail"]

    def test_a_car_coming_back_is_recorded(self, tmp_path, paths):
        state = State(path=tmp_path / "s.json")
        car(state)
        state.mark_missing("s", set())
        a_check_later()
        state.mark_missing("s", set())
        car(state)                                  # it returns

        back = events.update(state, *paths)["first"]["relisted"]
        assert back["before"] == "gone" and back["after"] == "active"

    def test_a_call_for_price_car_naming_a_figure_is_recorded(self, tmp_path, paths):
        state = State(path=tmp_path / "s.json")
        car(state, price=None)
        car(state, price=37450)

        priced = events.update(state, *paths)["first"]["priced"]
        assert priced["before"] is None and priced["after"] == 37450
        assert "no price" in priced["detail"]

    def test_a_car_that_never_moved_produces_nothing(self, tmp_path, paths):
        state = State(path=tmp_path / "s.json")
        car(state)
        car(state)
        record = events.update(state, *paths)
        assert record["first"] == {}
        assert sorted(record["waiting"]) == sorted(events.KINDS)


class TestWhatItRecordsAboutDelivery:
    def test_a_hidden_car_records_why_nothing_was_sent(self, tmp_path, paths):
        """A drop on a car the rules hide is still recorded, with why
        nothing was sent."""
        state = State(path=tmp_path / "s.json")
        car(state, price=42399, filtered=True, filter_reason="above maximum")
        car(state, price=42000, filtered=True, filter_reason="above maximum")
        state.silence("1", "hidden by your rules: above maximum")

        drop = events.update(state, *paths)["first"]["price_drop"]
        assert "deliberately quiet" in drop["delivered"]
        assert "above maximum" in drop["delivered"]

    def test_an_undelivered_alert_says_it_is_queued(self, tmp_path, paths):
        state = State(path=tmp_path / "s.json")
        entry = car(state, price=100000)
        car(state, price=90000)
        state.defer([Change(Change.PRICE_DROP, Listing.from_dict(entry),
                            old_price=100000, new_price=90000)])

        assert "queued" in events.update(state, *paths)["first"]["price_drop"]["delivered"]

    def test_a_car_nobody_decided_about_is_called_out(self, tmp_path, paths):
        state = State(path=tmp_path / "s.json")
        car(state, price=100000)
        car(state, price=90000)
        for key in ("notified_at", "quiet_reason", "pending"):
            state.listings["1"].pop(key, None)

        assert "no record" in events.update(state, *paths)["first"]["price_drop"]["delivered"]


class TestTheLedgerItself:
    def test_the_first_sighting_is_kept_even_after_later_ones(self, tmp_path, paths):
        state = State(path=tmp_path / "s.json")
        car(state, price=100000)
        car(state, price=95000)
        first = events.update(state, *paths)["first"]["price_drop"]["at"]

        car(state, price=90000)
        again = events.update(state, *paths)["first"]["price_drop"]
        assert again["at"] == first
        assert again["after"] == 95000

    def test_it_says_what_is_still_missing(self, tmp_path, paths):
        state = State(path=tmp_path / "s.json")
        car(state, price=100000)
        car(state, price=95000)
        events.update(state, *paths)

        text = paths[0].read_text(encoding="utf-8")
        assert "still waiting" in text
        assert "a car left the market" in text

    def test_a_new_kind_is_flagged_as_new_exactly_once(self, tmp_path, paths):
        state = State(path=tmp_path / "s.json")
        car(state, price=100000)
        car(state, price=95000)
        assert events.update(state, *paths)["new_kinds"] == ["price_drop"]
        assert events.update(state, *paths)["new_kinds"] == []

    def test_a_corrupt_ledger_does_not_stop_it(self, tmp_path, paths):
        paths[1].parent.mkdir(parents=True, exist_ok=True)
        paths[1].write_text("{not json", encoding="utf-8")
        state = State(path=tmp_path / "s.json")
        car(state, price=100000)
        car(state, price=95000)
        assert "price_drop" in events.update(state, *paths)["first"]


class TestItCannotDisturbTheBot:
    def test_it_never_writes_state(self, tmp_path, paths):
        state = State(path=tmp_path / "s.json")
        car(state, price=100000)
        car(state, price=95000)
        state.save()
        before = (tmp_path / "s.json").read_text(encoding="utf-8")

        events.update(state, *paths)
        assert (tmp_path / "s.json").read_text(encoding="utf-8") == before

    def test_it_reads_a_state_file_it_has_never_seen_before(self, tmp_path, paths):
        state = State(path=tmp_path / "s.json")
        record = events.update(state, *paths)
        assert record["first"] == {}
        assert json.loads(paths[1].read_text())["waiting"]


class TestWhatTheJobReadsBack:
    """The job decides whether to shout from the file, not from memory."""

    def test_new_kinds_is_written_to_the_file(self, tmp_path, paths):
        state = State(path=tmp_path / "s.json")
        car(state, price=100000)
        car(state, price=95000)
        events.update(state, *paths)

        written = json.loads(paths[1].read_text(encoding="utf-8"))
        assert written["new_kinds"] == ["price_drop"]

    def test_it_empties_once_the_event_is_old_news(self, tmp_path, paths):
        state = State(path=tmp_path / "s.json")
        car(state, price=100000)
        car(state, price=95000)
        events.update(state, *paths)
        events.update(state, *paths)

        assert json.loads(paths[1].read_text(encoding="utf-8"))["new_kinds"] == []


class TestASayingItWasAGuessWhenItWasNot:
    """A reconstructed timestamp must not outlive the reconstruction.

    Live, on the first real removal this bot ever announced: the ledger said
    "delivered (time reconstructed)". The alert had genuinely gone out, on
    that run, and been recorded at the moment it did. What was reconstructed
    was the *arrival* notification hours earlier, from before the bot stamped
    delivery times - and the flag saying so stayed on the entry, so every
    honest delivery afterwards was described as a guess.
    """

    def test_a_real_delivery_stops_the_ledger_calling_it_reconstructed(
            self, tmp_path, paths):
        state = State(path=tmp_path / "s.json")
        entry = car(state, price=100000)
        # As upgrade() leaves a car that was handled before delivery times
        # were recorded.
        entry["notified"] = True
        entry["notified_at"] = entry["first_seen"]
        entry["notified_at_backfilled"] = True

        car(state, price=95000)          # a price drop, genuinely delivered
        state.mark_notified(["1"])

        assert "notified_at_backfilled" not in state.listings["1"]
        record = events.update(state, *paths)
        delivered = record["first"]["price_drop"]["delivered"]
        assert delivered.startswith("delivered at")
        assert "reconstructed" not in delivered

    def test_a_car_nobody_has_delivered_about_keeps_the_flag(self, tmp_path):
        state = State(path=tmp_path / "s.json")
        entry = car(state, price=100000)
        entry["notified"] = True
        entry["notified_at"] = entry["first_seen"]
        entry["notified_at_backfilled"] = True

        state.mark_notified(["nobody"])   # a different car
        assert state.listings["1"]["notified_at_backfilled"] is True

    def test_the_ledger_corrects_a_delivery_line_it_already_wrote(
            self, tmp_path, paths):
        """What happened is settled. What was done about it can improve."""
        state = State(path=tmp_path / "s.json")
        car(state, price=100000)
        car(state, price=95000)
        # As a quiet-hours or channel-outage run leaves it: detected, owed,
        # not yet sent.
        state.listings["1"]["pending"] = {"kind": "price_drop",
                                          "since": "2026-02-03T00:00:00+00:00"}
        state.listings["1"].pop("notified_at", None)
        first = events.update(state, *paths)
        assert first["first"]["price_drop"]["delivered"] == "queued, not yet delivered"

        state.mark_notified(["1"])
        again = events.update(state, *paths)
        assert again["first"]["price_drop"]["delivered"].startswith("delivered at")
        assert "queued" not in paths[0].read_text(encoding="utf-8")

    def test_upgrade_takes_the_mark_off_a_delivery_that_was_watched(self, tmp_path):
        """The car it was found on is gone, so nothing else would clear it.

        The reconstruction copies last_seen. A notified_at that is neither
        last_seen nor first_seen was written by a run that watched itself
        send - it is observed, and the flag has no business staying on it.
        """
        state = State(path=tmp_path / "s.json")
        entry = car(state, price=100000)
        entry["last_seen"] = "2026-02-03T04:10:00+00:00"
        entry["notified"] = True
        entry["notified_at"] = "2026-02-03T09:25:30+00:00"   # an observed delivery
        entry["notified_at_backfilled"] = True

        state.upgrade()
        assert "notified_at_backfilled" not in state.listings["1"]

    def test_upgrade_leaves_a_genuinely_reconstructed_mark_alone(self, tmp_path):
        state = State(path=tmp_path / "s.json")
        entry = car(state, price=100000)
        entry["last_seen"] = "2026-02-03T04:10:00+00:00"
        entry["notified"] = True
        entry["notified_at"] = "2026-02-03T04:10:00+00:00"   # copied from it
        entry["notified_at_backfilled"] = True

        state.upgrade()
        assert state.listings["1"]["notified_at_backfilled"] is True


class TestSilenceAndFailureAreDifferentFaults:
    """Live: 32 hours of "the watcher has gone quiet" while it was running.

    It was started every couple of hours the whole time and failed a
    bookkeeping check on every attempt. Both faults leave the same trace in
    "when did a check last succeed", and they need completely different things
    done about them - one is GitHub's schedule, the other is the bot. Asking
    when a run last *happened* separates them.
    """

    def _cfg(self):
        return {"health": {"silent_after_hours": 3, "expected_interval_minutes": 30}}

    def _state(self, tmp_path, runs):
        state = State(path=tmp_path / "s.json")
        state.data["runs"] = runs
        return state

    def _ago(self, hours):
        from datetime import timedelta
        return (clock.now()
                - timedelta(hours=hours)).isoformat(timespec="seconds")

    def test_running_and_failing_is_not_reported_as_silence(self, tmp_path):
        state = self._state(tmp_path, [
            {"at": self._ago(0.2), "ok": False, "errors": ["the bot's own bookkeeping is inconsistent"]},
            {"at": self._ago(2.0), "ok": False, "errors": ["the bot's own bookkeeping is inconsistent"]},
            {"at": self._ago(30.0), "ok": True},
        ])
        said = events.silence(self._cfg(), state, {})

        assert said and said["failing"] is True
        assert "running and failing" in said["subject"]
        assert "not a schedule problem" in said["body"]
        assert "bookkeeping" in said["body"], "say what it actually said"

    def test_nothing_running_at_all_is_still_reported_as_silence(self, tmp_path):
        state = self._state(tmp_path, [{"at": self._ago(30.0), "ok": True}])
        said = events.silence(self._cfg(), state, {})

        assert said and said["failing"] is False
        assert "gone quiet" in said["subject"]
        assert "Nothing has been started since" in said["body"]

    def test_a_recent_success_says_nothing_either_way(self, tmp_path):
        state = self._state(tmp_path, [{"at": self._ago(0.5), "ok": True}])
        assert events.silence(self._cfg(), state, {}) is None


class TestRunningIsNotTheSameAsWatching:
    """A watcher served one check in eight is never silent and still blind.

    Measured on this repository: 12.5% coverage, an 18-hour gap, and a green
    "last check succeeded" the whole time. The silence alarm cannot see that,
    because nothing is silent.
    """

    def _cfg(self, floor=50):
        return {"health": {"expected_interval_minutes": 30,
                           "coverage_floor_pct": floor}}

    def _runs(self, state, n, spread_hours=24):
        from datetime import timedelta
        now = clock.now()
        state.data["runs"] = [
            {"at": (now - timedelta(hours=spread_hours * i / max(1, n))).isoformat(
                timespec="seconds"), "ok": True}
            for i in range(n)]
        return state

    def test_a_thin_schedule_is_reported(self, tmp_path):
        state = self._runs(State(path=tmp_path / "s.json"), 6)
        said = events.thin_coverage(self._cfg(), state, {})
        assert said and said["pct"] < 50
        assert "18" in said["body"] or "hours" in said["body"]
        assert "GitHub's scheduler" in said["body"], "say whose fault it is"

    def test_a_full_schedule_says_nothing(self, tmp_path):
        state = self._runs(State(path=tmp_path / "s.json"), 46)
        assert events.thin_coverage(self._cfg(), state, {}) is None

    def test_it_is_said_once_a_day_not_once_an_hour(self, tmp_path):
        state = self._runs(State(path=tmp_path / "s.json"), 6)
        first = events.thin_coverage(self._cfg(), state, {})
        assert first
        again = events.thin_coverage(
            self._cfg(), state, {"coverage_reported": first["at"]})
        assert again is None
        # Once a DAY, not once ever: yesterday's stamp must not suppress it.
        # The line that used to stand here built a timestamp for the year 2099
        # and then asserted the string was truthy, which it always was. It
        # tested nothing, and it raised ValueError on any leap day.
        assert events.thin_coverage(
            self._cfg(), state, {"coverage_reported": "2000-01-01T00:00:00+00:00"})

    def test_a_floor_of_zero_switches_it_off(self, tmp_path):
        state = self._runs(State(path=tmp_path / "s.json"), 2)
        assert events.thin_coverage(self._cfg(floor=0), state, {}) is None

    def test_too_few_runs_to_judge_says_nothing(self, tmp_path):
        """A fresh install has no history, which is not the same as a fault."""
        state = self._runs(State(path=tmp_path / "s.json"), 1)
        assert events.thin_coverage(self._cfg(), state, {}) is None


class TestACarThatWasAnnouncedAndIsNowHidden:
    """Both records are true, and only one of them is the answer.

    A car came back on the market, and the ledger reported it "delivered
    at 2026-01-10 02:15:46". That timestamp was real - it was when the car
    first appeared, while it was still visible. A radius rule added
    afterwards hid it, so the relisting itself was correctly never sent. The
    ledger claimed an alert that did not happen.
    """

    def _entry(self, state):
        car(state, price=40000)
        e = state.listings["1"]
        e["notified_at"] = "2026-01-10T02:15:46+00:00"
        e["notified"] = True
        e["filtered"] = True
        e["filter_reason"] = "Toronto, ON is 352 km from K1P 1J1"
        e["quiet_reason"] = "hidden by your rules: Toronto, ON is 352 km from K1P 1J1"
        return e

    def test_the_ledger_says_quiet_not_delivered(self, tmp_path):
        state = State(path=tmp_path / "s.json")
        entry = self._entry(state)
        said = events._delivery(entry)
        assert said.startswith("deliberately quiet"), said
        assert "352 km" in said

    def test_and_still_says_when_it_was_announced_before(self, tmp_path):
        """The earlier delivery is not erased - it is just not the answer."""
        state = State(path=tmp_path / "s.json")
        said = events._delivery(self._entry(state))
        assert "2026-01-10T02:15:46" in said
        assert "before that rule applied" in said

    def test_a_visible_car_still_reports_its_delivery(self, tmp_path):
        state = State(path=tmp_path / "s.json")
        car(state, price=90000)
        state.mark_notified(["1"])
        assert events._delivery(state.listings["1"]).startswith("delivered at")

    def test_the_dashboard_feed_agrees_with_the_ledger(self, tmp_path):
        from autotrader import insight
        state = State(path=tmp_path / "s.json")
        entry = self._entry(state)
        assert insight._delivery(entry)["state"] == "quiet"


class TestOneDeliveryRuleNotTwo:
    """The ledger and the dashboard each wrote down the same ordering.

    A car can carry a delivery time AND a quiet reason - told about once when
    it arrived, hidden by a rule added later - and reading the time first
    reports a correctly suppressed relisting as "delivered at 02:15": a real
    timestamp, belonging to a different event, describing a message never
    sent about this one. That was fixed in one of the two copies.
    """

    CASES = {
        "queued": {"pending": True, "quiet_reason": "muted",
                   "notified_at": "2026-09-01T00:00:00+00:00"},
        "quiet": {"quiet_reason": "hidden by a rule",
                  "notified_at": "2026-09-01T00:00:00+00:00"},
        "sent": {"notified_at": "2026-09-01T00:00:00+00:00"},
        "none": {},
    }

    @pytest.mark.parametrize("state", sorted(CASES))
    def test_both_readers_agree(self, state):
        from autotrader import insight
        from autotrader.events import delivery_state

        entry = self.CASES[state]
        assert delivery_state(entry) == state
        assert insight._delivery(entry)["state"] == state

    def test_a_quiet_car_that_was_once_told_about_says_both(self):
        from autotrader import events, insight

        entry = self.CASES["quiet"]
        ledger = events._delivery(entry)
        card = insight._delivery(entry)["text"]
        for text in (ledger, card):
            assert "hidden by a rule" in text
            assert "2026-09-01" in text, text

    def test_the_states_are_the_states(self):
        from autotrader.events import DELIVERY_STATES
        assert set(DELIVERY_STATES) == set(self.CASES)


class TestAPercentageAlwaysSaysWhatItIsOf:
    """"only 25.0% of the expected checks happened" beside a dashboard
    reading 33.3% is two numbers from one bot disagreeing with no
    explanation, and the explanation is only ever that they cover different
    stretches of time."""

    def _state(self, tmp_path, n=3, spread_hours=24, name="s.json"):
        from datetime import timedelta
        state = State(path=tmp_path / name)
        now = clock.now()
        state.data["runs"] = [
            {"at": (now - timedelta(hours=spread_hours * i / max(1, n))).isoformat(
                timespec="seconds"), "ok": True, "searches_run": 2,
             "searches_failed": 0}
            for i in range(n)]
        return state

    def cfg(self):
        return {"health": {"expected_interval_minutes": 120,
                           "coverage_floor_pct": 50}}

    def test_the_window_comes_back_with_the_percentage(self, tmp_path):
        said = events.thin_coverage(self.cfg(), self._state(tmp_path), {})
        assert said
        for key in ("pct", "window_hours", "slots_covered", "expected"):
            assert said.get(key) is not None, key

    def test_the_parts_agree_with_the_percentage(self, tmp_path):
        said = events.thin_coverage(self.cfg(), self._state(tmp_path), {})
        assert said
        assert said["pct"] == round(
            said["slots_covered"] / said["expected"] * 100, 1)

    def test_the_command_prints_the_window(self, tmp_path, monkeypatch, capsys):
        from autotrader.cli import main
        from autotrader.config import Config
        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=25", "Civic")
        cfg.set("health.coverage_floor_pct", 50)
        cfg.save()
        # state.json, which is the file the command reads.
        self._state(tmp_path, name="state.json").save()
        main(["events"])
        out = capsys.readouterr().out
        assert "% of the expected checks happened" in out, out
        assert "slots in" in out, out
