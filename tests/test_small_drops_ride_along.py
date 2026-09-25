"""Small price drops ride along with the next new car, run end to end.

With the alert bar set at $2,500, a drop of $2,500 or more is an alert of its
own; a smaller one is held and goes out behind the next new car, in the same
notification, and never buzzes the phone by itself. Every drop is measured
from the price you last heard, so a car cut $1,000 at a time reaches the bar
instead of sliding under it forever.

Driven through the real runner against the captured results page the other
live-data tests use: 19 real cars, real ids, real prices.
"""
import uuid

import pytest

from autotrader import runner as runner_mod
from autotrader.config import Config
from autotrader.parser import parse_search_page
from autotrader.runner import run
from autotrader.state import Change, State

from .helpers import Capture, FakeFetcher, next_check, use_channels
from .test_live_data import BASE, drop_price

CAR_PRICE = 89999          # a real car on the captured page


@pytest.fixture
def watch(tmp_path, monkeypatch, fixture_html):
    monkeypatch.chdir(tmp_path)
    cfg = Config.defaults(tmp_path / "config.json")
    cfg.add_search(f"{BASE}?rcp=25&prx=-1", "Example search")
    cfg.set("scraping.delay_ms", 0)
    cfg.set("scraping.retries", 0)
    cfg.set("scraping.enrich_details", False)
    cfg.set("archive.mode", "off")
    cfg.set("notifications.max_listings_per_message", 50)
    cfg.set("notifications.price_drop_alert_abs", 2500)
    cfg.set("notifications.small_drops_ride_along", True)
    cfg.save()

    sink = Capture()
    use_channels(monkeypatch, runner_mod, [sink])
    html = fixture_html("search_next_data")

    def go(page=None, minutes_later=None):
        next_check(minutes_later)
        return run(cfg, State.load(tmp_path / "state.json"),
                   fetcher=FakeFetcher(page or html), env={})

    def state():
        return State.load(tmp_path / "state.json")

    def car_id(page, price):
        for listing in parse_search_page(page, BASE).listings:
            if listing.price == price:
                return listing.id
        raise AssertionError(f"no car at {price} on the page")

    def with_a_new_car(page):
        """The same page with one car it has never seen: a car's id swapped
        for a fresh one, which is exactly how an arrival looks to the bot."""
        victim = parse_search_page(page, BASE).listings[-1].id
        fresh = str(uuid.uuid4())
        return page.replace(victim, fresh), fresh

    w = type("Watch", (), {})()
    w.cfg, w.sink, w.run, w.state, w.html = cfg, sink, go, state, html
    w.car_id, w.with_a_new_car = car_id, with_a_new_car
    go()                      # the first run announces what it finds
    sink.digests.clear()
    return w


def sent(watch):
    return [c for digest in watch.sink.digests for c in digest]


class TestASmallDropNeverBuzzesAlone:

    def test_a_1000_drop_sends_nothing(self, watch):
        watch.run(drop_price(watch.html, CAR_PRICE, CAR_PRICE - 1000))
        assert watch.sink.digests == []

    def test_but_it_is_held_and_the_ledger_says_where_it_is_going(self, watch):
        page = drop_price(watch.html, CAR_PRICE, CAR_PRICE - 1000)
        report = watch.run(page)
        entry = watch.state().listings[watch.car_id(page, CAR_PRICE - 1000)]
        assert entry.get("ride_along")
        assert "next new car" in entry.get("quiet_reason", "")
        assert report.invariants == []

    def test_nor_on_any_run_after_it_with_nothing_new(self, watch):
        page = drop_price(watch.html, CAR_PRICE, CAR_PRICE - 1000)
        for n in range(5):
            watch.run(page, minutes_later=120 * (n + 1))
        assert watch.sink.digests == []


class TestItRidesWithTheNextNewCar:

    def test_same_notification_new_car_first_drop_after(self, watch):
        dropped = drop_price(watch.html, CAR_PRICE, CAR_PRICE - 1000)
        watch.run(dropped)
        page, fresh = watch.with_a_new_car(dropped)
        watch.run(page, minutes_later=240)

        assert len(watch.sink.digests) == 1, "one notification, not two"
        digest = watch.sink.digests[0]
        assert digest[0].kind == Change.NEW and digest[0].listing.id == fresh
        drops = [c for c in digest if c.kind == Change.PRICE_DROP]
        assert len(drops) == 1 and drops[0].rider
        assert digest.index(drops[0]) > 0, "the drop tags along AFTER the new car"
        assert (drops[0].old_price, drops[0].new_price) == (CAR_PRICE, CAR_PRICE - 1000)

    def test_once_sent_it_is_told_and_measured_from_there(self, watch):
        dropped = drop_price(watch.html, CAR_PRICE, CAR_PRICE - 1000)
        watch.run(dropped)
        page, _ = watch.with_a_new_car(dropped)
        watch.run(page, minutes_later=240)

        entry = watch.state().listings[watch.car_id(dropped, CAR_PRICE - 1000)]
        assert entry.get("notified_at")
        assert not entry.get("ride_along")
        assert entry.get("heard_price") == CAR_PRICE - 1000

    def test_a_car_that_went_back_up_does_not_ride(self, watch):
        """A drop that has been undone is not news, and "down $1,000" about a
        car at its old price would be the one false line in the message."""
        down = drop_price(watch.html, CAR_PRICE, CAR_PRICE - 1000)
        watch.run(down)
        watch.run(watch.html, minutes_later=120)           # back up
        page, fresh = watch.with_a_new_car(watch.html)
        watch.run(page, minutes_later=240)
        assert [c.kind for c in sent(watch)] == [Change.NEW]


class TestABigDropAlertsOnItsOwn:

    def test_3500_off_is_a_notification_by_itself(self, watch):
        watch.run(drop_price(watch.html, CAR_PRICE, CAR_PRICE - 3500))
        assert len(watch.sink.digests) == 1
        [change] = watch.sink.digests[0]
        assert change.kind == Change.PRICE_DROP and not change.rider
        assert (change.old_price, change.new_price) == (CAR_PRICE, CAR_PRICE - 3500)

    def test_exactly_2500_counts(self, watch):
        watch.run(drop_price(watch.html, CAR_PRICE, CAR_PRICE - 2500))
        assert len(watch.sink.digests) == 1

    def test_small_cuts_add_up_to_an_alert(self, watch):
        """$1,000, then $1,000, then $1,500: none clears $2,500 on its own,
        and the car is $3,500 cheaper than you last heard. Measured run to
        run the phone would never have buzzed; measured from what you know,
        the third cut is the one that says so."""
        p1 = drop_price(watch.html, CAR_PRICE, CAR_PRICE - 1000)
        p2 = drop_price(p1, CAR_PRICE - 1000, CAR_PRICE - 2000)
        p3 = drop_price(p2, CAR_PRICE - 2000, CAR_PRICE - 3500)
        watch.run(p1)
        watch.run(p2, minutes_later=120)
        assert watch.sink.digests == []
        watch.run(p3, minutes_later=240)
        [change] = sent(watch)
        assert (change.old_price, change.new_price) == (CAR_PRICE, CAR_PRICE - 3500)


class TestTheCapCannotTurnARiderIntoAnAlert:

    def test_a_rider_the_digest_had_no_room_for_waits_for_the_next_new_car(self, watch):
        """A rider left out by the cap must stay a rider. Deferring it as an
        ordinary owed alert would send it on its own on the next run."""
        watch.cfg.set("notifications.max_listings_per_message", 1)
        watch.cfg.save()
        dropped = drop_price(watch.html, CAR_PRICE, CAR_PRICE - 1000)
        watch.run(dropped)
        page, _ = watch.with_a_new_car(dropped)
        watch.run(page, minutes_later=240)

        entry = watch.state().listings[watch.car_id(dropped, CAR_PRICE - 1000)]
        assert entry.get("ride_along"), "still waiting for a new car"
        assert not entry.get("pending"), "and not owed as an alert of its own"

        watch.sink.digests.clear()
        watch.run(page, minutes_later=480)
        assert watch.sink.digests == [], "nothing new, so nothing sent"


class TestTheOrderIsTheSameEverywhere:

    def test_new_cars_first_then_drops_biggest_first_then_riders(self):
        from autotrader import render
        from autotrader.listing import Listing

        def car(i, price):
            return Listing(id=str(i), url=f"https://x/{i}", title=f"Car {i}",
                           price=price)
        small = Change(Change.PRICE_DROP, car(1, 9000), old_price=10000,
                       new_price=9000, rider=True)
        big = Change(Change.PRICE_DROP, car(2, 40000), old_price=44000, new_price=40000)
        bigger = Change(Change.PRICE_DROP, car(3, 50000), old_price=58000, new_price=50000)
        new = Change(Change.NEW, car(4, 70000))
        assert render.in_order([small, big, new, bigger]) == [new, bigger, big, small]
