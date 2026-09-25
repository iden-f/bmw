"""State is what stops the bot re-announcing cars or forgetting them."""
import json

from autotrader.listing import Listing
from autotrader.state import Change, State
from .helpers import a_check_later


def car(price=None, **kw):
    base = dict(id="1", url="https://www.autotrader.ca/a/x/19_1_/", title="2021 Honda Civic",
                search_id="s1", price_source="detail" if price else "")
    base.update(kw)
    return Listing(price=price, **base)


def test_a_new_car_is_reported_once(tmp_path):
    s = State(path=tmp_path / "state.json")
    assert s.record(car(100000)).kind == Change.NEW
    assert s.record(car(100000)) is None


def test_a_price_drop_is_reported_with_the_right_numbers(tmp_path):
    s = State(path=tmp_path / "state.json")
    s.record(car(100000))
    change = s.record(car(92000))
    assert change.kind == Change.PRICE_DROP
    assert change.old_price == 100000 and change.new_price == 92000
    assert change.delta == -8000
    assert round(change.delta_pct, 1) == -8.0


def test_a_price_rise_is_distinguished_from_a_drop(tmp_path):
    s = State(path=tmp_path / "state.json")
    s.record(car(100000))
    assert s.record(car(104000)).kind == Change.PRICE_RISE


def test_card_and_detail_prices_are_never_compared(tmp_path):
    """A results card and the listing page can quote different figures; taking
    one for the other produced a phantom price drop on every run."""
    s = State(path=tmp_path / "state.json")
    s.record(car(102199, price_source="detail"))
    assert s.record(car(98995, price_source="search")) is None
    # The better-sourced figure is kept so the next detail lookup can settle it.
    assert s.listings["1"]["price"] == 102199
    assert s.listings["1"]["price_disputed"] == 98995
    # Once a detail price confirms the drop, it is reported.
    change = s.record(car(98995, price_source="detail"))
    assert change.kind == Change.PRICE_DROP
    assert "price_disputed" not in s.listings["1"]


def test_price_history_accumulates(tmp_path):
    s = State(path=tmp_path / "state.json")
    for price in (100000, 96000, 92000):
        s.record(car(price))
    assert [p["price"] for p in s.listings["1"]["price_history"]] == [100000, 96000, 92000]


def test_a_car_must_be_missing_twice_before_it_counts_as_gone(tmp_path):
    s = State(path=tmp_path / "state.json")
    s.record(car(100000))
    assert s.mark_missing("s1", set()) == []
    a_check_later()
    changes = s.mark_missing("s1", set())
    assert [c.kind for c in changes] == [Change.REMOVED]
    assert s.listings["1"]["status"] == "gone"


def test_two_checks_a_minute_apart_do_not_prove_a_sale(tmp_path):
    """A push and a scheduled firing can land seconds apart.

    Only the scheduled run is deduplicated, so "two consecutive misses" was
    satisfiable in under a minute - and a minute of absence from one page of
    results is not evidence that a car sold.
    """
    s = State(path=tmp_path / "state.json")
    s.record(car(100000))
    s.mark_missing("s1", set())
    a_check_later(1)
    assert s.mark_missing("s1", set()) == []
    assert s.listings["1"]["status"] == "active"
    a_check_later(90)
    assert [c.kind for c in s.mark_missing("s1", set())] == [Change.REMOVED]


def test_the_wait_is_measured_from_when_it_was_last_seen(tmp_path):
    """Not from the first miss: a car last seen this morning and missed for
    the first time this afternoon has already been unaccounted for."""
    s = State(path=tmp_path / "state.json")
    s.record(car(100000))
    s.listings["1"]["last_seen"] = "2000-01-01T00:00:00+00:00"
    assert s.mark_missing("s1", set()) == []      # one miss is still one miss
    changes = s.mark_missing("s1", set())
    assert [c.kind for c in changes] == [Change.REMOVED]


def test_a_car_that_comes_back_is_not_marked_gone(tmp_path):
    s = State(path=tmp_path / "state.json")
    s.record(car(100000))
    s.mark_missing("s1", set())
    a_check_later()
    s.mark_missing("s1", {"1"})
    a_check_later()
    assert s.mark_missing("s1", set()) == []      # the miss counter reset


def test_other_searches_are_untouched_by_a_removal_sweep(tmp_path):
    s = State(path=tmp_path / "state.json")
    s.record(car(100000, id="1", search_id="s1"))
    s.record(car(100000, id="2", search_id="s2"))
    s.mark_missing("s1", set())
    a_check_later()
    s.mark_missing("s1", set())
    assert s.listings["2"]["status"] == "active"


def test_state_survives_a_round_trip(tmp_path):
    path = tmp_path / "state.json"
    s = State(path=path)
    s.record(car(100000))
    s.save()
    assert State.load(path).listings["1"]["price"] == 100000


def test_a_corrupt_state_file_does_not_re_announce_everything(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "state.json"
    path.write_text("{ this is not json")
    s = State.load(path)
    assert s.listings == {}
    assert (tmp_path / "state.corrupt.json").exists()   # kept for inspection


def test_saving_is_atomic(tmp_path):
    path = tmp_path / "state.json"
    s = State(path=path)
    s.record(car(1000))
    s.save()
    assert not list(tmp_path.glob("*.tmp"))
    json.loads(path.read_text())


def test_old_removed_cars_are_eventually_forgotten(tmp_path):
    s = State(path=tmp_path / "state.json")
    s.record(car(1000))
    s.listings["1"].update(status="gone", removed_at="2000-01-01T00:00:00+00:00")
    assert s.prune(keep_days=30) == 1
    assert s.listings == {}


def test_active_cars_are_never_pruned(tmp_path):
    s = State(path=tmp_path / "state.json")
    s.record(car(1000))
    s.listings["1"]["last_seen"] = "2000-01-01T00:00:00+00:00"
    assert s.prune(keep_days=1) == 0
