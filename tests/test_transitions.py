"""Every state a listing can move between, and back.

Three separate bugs in this rebuild have been the same shape: a car changing
state and the bookkeeping not following it. A car rejected by one search and
wanted by another, stored and never announced. A car that stopped being
call-for-price and kept a ceiling it was no longer measured against as its
reason for silence. A car marked gone by a rotating result window.

None of them crashed. Each was caught late, by an invariant or by reading
state.json by hand. So the transitions are enumerated here and each one is
driven for real through the runner, in both directions where both directions
exist, and after every step the same three things are asserted:

* the bookkeeping holds - no invariant violation;
* every car is delivered, owed, or deliberately quiet with a reason;
* the reason, where there is one, is *true right now* rather than left over
  from a rule that has stopped applying.
"""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from autotrader import runner as runner_mod
from autotrader.config import Config
from autotrader.runner import run
from autotrader.state import State

from .helpers import Capture, FakeFetcher, next_check, use_channels

BASE = "https://www.autotrader.ca/cars/honda/civic/"


def uuid_for(short: str) -> str:
    """A stable listing id of the shape the 2026 platform actually uses.

    The parser takes a car's identity from the UUID at the end of an /offers/
    URL, so a test that invents "a" as an id is testing nothing: the page
    parses to zero listings, the run treats it as an unreadable page and
    protects itself by changing nothing, and every assertion about state
    afterwards passes for the wrong reason. Hashing to hex means a test can
    name a car "seller-b" and still hand the parser something it accepts.
    """
    return str(uuid.UUID(hashlib.md5(short.encode()).hexdigest()))


def page(cars: list[dict]) -> str:
    """A search page in the shape the site actually serves.

    JSON-LD, because it is the rung of the ladder least likely to be the one
    under test when a transition test fails.
    """
    items = []
    for i, car in enumerate(cars, 1):
        item = {
            "@type": "Vehicle",
            "name": car.get("name", f"{car.get('year', 2022)} Honda Civic"),
            "url": f"https://www.autotrader.ca/offers/honda-civic-{uuid_for(car['id'])}",
            "vehicleModelDate": str(car.get("year", 2022)),
            "model": "Civic", "brand": {"@type": "Brand", "name": "Honda"},
            "color": car.get("color", "Black"),
            "mileageFromOdometer": {"@type": "QuantitativeValue", "unitCode": "KMT",
                                    "value": str(car.get("km", 60000))},
        }
        if car.get("price") is not None:
            item["offers"] = {"@type": "Offer", "price": str(car["price"]),
                              "priceCurrency": "CAD"}
        if car.get("city"):
            item["availableAtOrFrom"] = {
                "@type": "Place",
                "address": {"@type": "PostalAddress",
                            "addressLocality": car["city"],
                            "addressRegion": car.get("region", "ON")}}
        items.append({"@type": "ListItem", "position": i, "item": item})
    payload = {"@context": "https://schema.org", "@type": "ItemList",
               "itemListElement": items}
    return ("<!DOCTYPE html><html><head><script type=\"application/ld+json\">"
            + json.dumps(payload)
            + "</script></head><body><main>results</main></body></html>")


class Bench:
    """One configured bot, driven a page at a time."""

    def __init__(self, tmp_path, monkeypatch, filters=None, searches=None):
        monkeypatch.chdir(tmp_path)
        self.path = tmp_path
        cfg = Config.defaults(tmp_path / "config.json")
        for url, name in (searches or [(BASE + "?rcp=25", "Civic")]):
            cfg.add_search(url, name)
        cfg.set("scraping.delay_ms", 0)
        cfg.set("scraping.enrich_details", False)
        cfg.set("archive.mode", "off")
        cfg.set("dashboard.enabled", False)
        for key, value in (filters or {}).items():
            cfg.set(f"filters.{key}", value)
        cfg.save()
        self.cfg = cfg
        self.sink = Capture()
        use_channels(monkeypatch, runner_mod, [self.sink])

    def check(self, cars=None, *, per_search=None, minutes_later=None):
        """Run once against this page (or one page per search).

        Each check happens a schedule-interval after the last one, because
        every rule about whether a car is gone, a search is failing or the
        bot has gone silent is a statement about elapsed time. Written
        without this, the suite asserted that two absences zero seconds apart
        proved a sale - which is the exact mistake the grace period exists to
        prevent, encoded as a test.
        """
        next_check(minutes_later)
        fetcher = FakeFetcher(page(cars if cars is not None else per_search[0]))
        if per_search is not None:
            # Keyed on each search's own URL. A search reads several pages, so
            # counting calls would hand the second search the first one's
            # results and the ownership assertions would mean nothing.
            by_id = {s.id: page(c) for s, c in zip(self.cfg.searches, per_search)}
            real = fetcher.get

            def get(url, referer=None, allow_block=False):
                for sid, html in by_id.items():
                    search = next(x for x in self.cfg.searches if x.id == sid)
                    tail = search.url.split("/cars/")[-1].split("?")[0]
                    if tail and tail in url and search.url.split("?")[-1].split("&")[0] in url:
                        fetcher.search_html = html
                        break
                return real(url, referer, allow_block)
            fetcher.get = get
        return run(self.cfg, State.load(self.path / "state.json"),
                   fetcher=fetcher, env={}, force=True)

    def state(self):
        return State.load(self.path / "state.json")

    def entry(self, lid):
        return self.state().listings[uuid_for(lid)]


def assert_books_balance(report, state):
    """The three things that must be true after any transition."""
    assert not report.invariants, report.invariants

    for lid, e in state.listings.items():
        accounted = (e.get("notified_at") or e.get("pending") or e.get("quiet_reason"))
        assert accounted, f"{lid} is neither delivered, owed, nor deliberately quiet"

        # A reason that names a rule must be a rule that still applies.
        reason = str(e.get("quiet_reason") or "")
        if reason.startswith("hidden by your rules"):
            assert e.get("filtered"), (
                f"{lid} is not hidden but still blames a rule: {reason}")


@pytest.fixture
def bench(tmp_path, monkeypatch):
    def make(**kw):
        return Bench(tmp_path, monkeypatch, **kw)
    return make


class TestHiddenAndBack:
    """filtered ↔ unfiltered. The one that failed sixteen runs in a row."""

    def test_a_car_that_stops_being_hidden_is_announced_and_loses_the_reason(self, bench):
        b = bench(filters={"max_price": 40000})
        r1 = b.check([{"id": "a", "price": 48000}])
        assert_books_balance(r1, b.state())
        assert b.entry("a")["filtered"]
        assert b.entry("a")["quiet_reason"].startswith("hidden by your rules")

        before = sum(len(d) for d in b.sink.digests)
        r2 = b.check([{"id": "a", "price": 38000}])
        assert_books_balance(r2, b.state())
        assert not b.entry("a")["filtered"]
        assert not str(b.entry("a").get("quiet_reason") or "").startswith("hidden")
        assert sum(len(d) for d in b.sink.digests) > before, "a car that becomes visible is news"

    def test_and_back_again(self, bench):
        b = bench(filters={"max_price": 40000})
        b.check([{"id": "a", "price": 38000}])
        r = b.check([{"id": "a", "price": 48000}])
        assert_books_balance(r, b.state())
        assert b.entry("a")["filtered"]
        assert b.entry("a")["quiet_reason"].startswith("hidden by your rules")

    def test_hidden_then_no_price_at_all(self, bench):
        """The failure seen in use: no price is not a rejection, so it unfilters."""
        b = bench(filters={"max_price": 40000, "require_price": True})
        b.check([{"id": "a", "price": 48000}])
        r = b.check([{"id": "a", "price": None}])
        assert_books_balance(r, b.state())


class TestPricedAndCallForPrice:
    """priced ↔ call for price."""

    def test_a_call_for_price_car_naming_a_figure_is_announced(self, bench):
        b = bench()
        r1 = b.check([{"id": "a", "price": None}])
        assert_books_balance(r1, b.state())
        assert b.entry("a")["unpriced"] is True

        before = sum(len(d) for d in b.sink.digests)
        r2 = b.check([{"id": "a", "price": 74000}])
        assert_books_balance(r2, b.state())
        entry = b.entry("a")
        assert entry["unpriced"] is False and entry["price"] == 74000
        assert entry.get("priced_at"), "the moment it became judgeable is recorded"
        assert sum(len(d) for d in b.sink.digests) > before

    def test_a_car_whose_price_disappears_keeps_the_one_it_had(self, bench):
        """A card that stops quoting is not a car that became free."""
        b = bench()
        b.check([{"id": "a", "price": 74000}])
        r = b.check([{"id": "a", "price": None}])
        assert_books_balance(r, b.state())
        assert b.entry("a")["price"] == 74000


class TestGoneAndBack:
    """active → gone → back. Two strikes before a removal is believed."""

    def test_a_car_needs_two_absences_before_it_counts_as_gone(self, bench):
        b = bench()
        b.check([{"id": "a", "price": 70000}, {"id": "b", "price": 71000}])
        r1 = b.check([{"id": "b", "price": 71000}])
        assert b.entry("a")["status"] == "active", "one absence is a rotating page"
        assert_books_balance(r1, b.state())

        r2 = b.check([{"id": "b", "price": 71000}])
        assert b.entry("a")["status"] == "gone"
        assert b.entry("a")["removed_at"]
        assert_books_balance(r2, b.state())

    def test_a_car_that_comes_back_is_active_with_its_history(self, bench):
        b = bench()
        b.check([{"id": "a", "price": 70000}, {"id": "b", "price": 71000}])
        b.check([{"id": "b", "price": 71000}])
        b.check([{"id": "b", "price": 71000}])
        assert b.entry("a")["status"] == "gone"

        r = b.check([{"id": "a", "price": 68000}, {"id": "b", "price": 71000}])
        entry = b.entry("a")
        assert entry["status"] == "active"
        assert not entry.get("removed_at"), "an active car carries no removal time"
        assert entry.get("relisted_at")
        assert len(entry["price_history"]) >= 2, "it keeps what it cost before"
        assert_books_balance(r, b.state())

    def test_and_it_can_go_again(self, bench):
        b = bench()
        b.check([{"id": "a", "price": 70000}, {"id": "b", "price": 71000}])
        for _ in range(2):
            b.check([{"id": "b", "price": 71000}])
        b.check([{"id": "a", "price": 68000}, {"id": "b", "price": 71000}])
        for _ in range(2):
            r = b.check([{"id": "b", "price": 71000}])
        assert b.entry("a")["status"] == "gone"
        assert_books_balance(r, b.state())


class TestOwnershipMovingBetweenSearches:
    """search A ↔ search B. The one where a watch reported zero while working."""

    SEARCHES = [(BASE + "?rcp=25&y=2021", "Narrow"),
                (BASE + "?rcp=25", "Wide")]

    def test_a_car_belongs_to_exactly_one_search(self, bench):
        b = bench(searches=self.SEARCHES)
        r = b.check(per_search=[[{"id": "a", "price": 70000}],
                                [{"id": "a", "price": 70000}, {"id": "b", "price": 80000}]])
        assert_books_balance(r, b.state())
        owners = {lid: e["search_id"] for lid, e in b.state().listings.items()}
        assert len(set(owners.values())) <= 2
        assert owners[uuid_for("a")] != owners.get(uuid_for("b")) or owners.get(uuid_for("b")) is None

    def test_a_car_the_first_search_rejects_is_still_offered_to_the_second(self, bench):
        """Rejected by the narrow watch's year rule, wanted by the wide one."""
        b = bench(searches=self.SEARCHES, filters={"min_year": 2024})
        b.cfg.data["searches"][1]["filters"] = {}
        b.cfg.save()
        r = b.check(per_search=[[{"id": "a", "price": 70000, "year": 2015}],
                                [{"id": "a", "price": 70000, "year": 2015}]])
        assert_books_balance(r, b.state())
        entry = b.state().listings.get(uuid_for("a"))
        assert entry is not None, "a car one search turned away is not thrown away"

    def test_a_search_that_disappears_releases_its_cars(self, bench):
        b = bench(searches=self.SEARCHES)
        b.check(per_search=[[{"id": "seller-a", "price": 70000}], [{"id": "seller-b", "price": 80000}]])
        gone = b.cfg.searches[0].id
        b.cfg.remove_search(gone)
        b.cfg.save()
        r = b.check([{"id": "seller-b", "price": 80000}])
        assert_books_balance(r, b.state())
        for lid, e in b.state().listings.items():
            if e.get("search_id") != gone:
                continue
            # Its cars stop being live and say why. The search id stays as a
            # record of what found it, which is what the one-owner invariant
            # allows: it objects to an *active* car owned by nothing.
            assert e["status"] == "gone", f"{lid} is still live under a removed search"
            assert e.get("quiet_reason"), f"{lid} went quiet with no reason"


class TestTheWholeCycleAtOnce:
    def test_a_car_through_every_state_keeps_its_books(self, bench):
        """Hidden, visible, call-for-price, priced, gone, back, gone."""
        b = bench(filters={"max_price": 40000})
        steps = [
            ([{"id": "a", "price": 48000}, {"id": "z", "price": 20000}], "hidden"),
            ([{"id": "a", "price": 38000}, {"id": "z", "price": 20000}], "visible"),
            ([{"id": "a", "price": None}, {"id": "z", "price": 20000}], "no price"),
            ([{"id": "a", "price": 34000}, {"id": "z", "price": 20000}], "priced again"),
            ([{"id": "z", "price": 20000}], "absent once"),
            ([{"id": "z", "price": 20000}], "absent twice"),
            ([{"id": "a", "price": 32000}, {"id": "z", "price": 20000}], "back"),
        ]
        for cars, label in steps:
            report = b.check(cars)
            state = b.state()
            assert not report.invariants, f"after {label}: {report.invariants}"
            assert_books_balance(report, state)

        entry = b.entry("a")
        assert entry["status"] == "active"
        assert entry.get("relisted_at")
        assert not entry.get("removed_at")
        assert entry["price"] == 32000


# ---------------------------------------------------------------------------
# Every transition, enumerated rather than chosen.

#: The states a listing can be in, as facts a person could check on the page,
#: and one page of results that puts a car into each. Two rules are in play so
#: that "hidden" and "hidden with no price" are both reachable without
#: reconfiguring the bot between steps.
STATES = {
    "visible":         {"price": 38000, "km": 60000},
    "hidden":          {"price": 48000, "km": 60000},
    "unpriced":        {"price": None, "km": 60000},
    "hidden_unpriced": {"price": None, "km": 200000},
    "gone":            None,             # absent from the results
}
RULES = {"max_price": 40000, "max_mileage_km": 150000}


def put_in(b, state, *, car="a"):
    """Drive the bot until the car is in that state, and check it got there."""
    if state == "gone":
        # A car has to exist before it can leave. Then two consecutive checks
        # that miss it, a schedule-interval apart: both conditions are real -
        # a car is gone when checks have missed it AND time has passed with
        # nobody seeing it.
        b.check([dict(STATES["visible"], id=car), KEEP])
        b.check([KEEP])
        b.check([KEEP])
    else:
        b.check([dict(STATES[state], id=car), KEEP])
    return b.entry(car)


KEEP = {"id": "keep", "price": 20000, "km": 10000}


def describe(entry):
    """Which of the five states this entry is actually in."""
    if entry.get("status") == "gone":
        return "gone"
    priced = entry.get("price") is not None
    if entry.get("filtered"):
        return "hidden" if priced else "hidden_unpriced"
    return "visible" if priced else "unpriced"


def expected_end(start, end):
    """Where the bot should land, which is not always where it was pushed.

    A price the bot has seen is sticky. AutoTrader drops the figure off a
    card and puts it back - the same car, the same ad - and treating that as
    "the seller withdrew the price" would fire a call-for-price alert every
    time the site hiccuped. So a car that has had a price cannot be driven
    back to having none; it keeps the last one it saw, and whether a rule
    hides it is still decided fresh.

    Written out here rather than special-cased inside the assertion, because
    the whole point of enumerating is that the exceptions are visible.
    """
    if end in ("unpriced", "hidden_unpriced"):
        # "gone" included: a car can only leave a market it was seen in, and
        # put_in() seeds it as a visible, priced car before making it vanish.
        # A car that comes back without a figure on its card is the same car.
        had_a_price = start in ("visible", "hidden", "gone")
        if had_a_price:
            return "hidden" if end == "hidden_unpriced" else "visible"
    return end


@pytest.mark.parametrize("start", sorted(STATES))
@pytest.mark.parametrize("end", sorted(STATES))
def test_every_transition_keeps_the_books(bench, start, end):
    """Twenty-five pairs, driven for real, with the same three assertions.

    Three separate bugs in this rebuild have been a car changing state and the
    bookkeeping not following it: a car rejected by one search and wanted by
    another, stored and never announced; a car that stopped being
    call-for-price and kept a ceiling it was no longer measured against as its
    reason for silence; a car marked gone by a rotating result window. None of
    them crashed, and each was found late by reading state.json by hand.

    The hand-picked cycle above covers the seven steps someone thought of.
    This covers the ones nobody did.
    """
    b = bench(filters=RULES)
    before = put_in(b, start)
    assert describe(before) == start, f"could not reach {start}"

    if end == "gone":
        b.check([KEEP])
        report = b.check([KEEP])
    else:
        report = b.check([dict(STATES[end], id="a"), KEEP])

    state = b.state()
    assert not report.invariants, report.invariants
    assert_books_balance(report, state)
    after = b.entry("a")
    want = expected_end(start, end)
    assert describe(after) == want, (
        f"{start} -> {end} ended in {describe(after)}, expected {want}")


@pytest.mark.parametrize("state", sorted(s for s in STATES if s != "gone"))
def test_a_car_already_told_about_is_never_announced_as_new_again(bench, state):
    """Whatever it does next. Re-announcing is the one mistake a watcher
    cannot be forgiven."""
    b = bench(filters=RULES)
    put_in(b, "visible")
    assert b.entry("a").get("notified_at"), "it should have been announced once"
    told = b.entry("a")["notified_at"]

    put_in(b, state)
    entry = b.entry("a")
    assert entry.get("notified_at"), f"{state} lost the record of being told"
    if state == "visible":
        assert entry["notified_at"] == told, "it was announced a second time"
    # And never as a discovery: a car coming back is a relisting.
    kinds = [c.kind for digest in b.sink.digests for c in digest
             if c.listing.id == uuid_for("a")]
    assert kinds.count("new") <= 1, kinds


@pytest.mark.parametrize("state", ["hidden", "hidden_unpriced"])
def test_a_hidden_car_names_a_rule_that_is_true_right_now(bench, state):
    b = bench(filters=RULES)
    put_in(b, state)
    entry = b.entry("a")
    reason = str(entry.get("quiet_reason") or "")
    assert reason.startswith("hidden by your rules"), reason
    # And loses it the moment it stops applying.
    put_in(b, "visible")
    assert not str(b.entry("a").get("quiet_reason") or "").startswith("hidden")
