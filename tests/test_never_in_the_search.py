"""Dropping a stored car is the most destructive thing the bot does.

It is right exactly once: when the car was never an answer to the search it
is filed under. Before the parser learned to read the page's own declared
results it recorded the recommendation rails alongside the results, and
most of what it had stored turned out to be rails - cars the searches had
never returned, which nonetheless sat on the dashboard, were re-checked on
every run, and kept a mass-disappearance warning permanently on screen.

These tests hold the boundary: the sweep fires on rails, never on a car the
site listed, never on a car you have touched, and never on the strength
of a read that could not see the whole search.
"""
from autotrader.listing import Listing
from autotrader.state import State


def a_car(n, **kw):
    return Listing(id=f"c{n}", url=f"https://www.autotrader.ca/a/c{n}",
                   title=f"Car {n}", price=50000, year=2020,
                   make="Honda", model="Civic", **kw)


class TestTheSweepEmptiesItself:

    def test_a_rail_is_dropped_and_a_result_is_not(self, bench, fixture_html):
        """The first run after the parser change reconciles; the second has
        nothing left to do.

        Driven by a page that DECLARES its results, because that is the only
        kind of read the sweep is allowed to act on. The default fixture has
        no schema.org ItemList, which is exactly the fallback case the other
        class below covers.
        """
        declaring = fixture_html("search_2026_full")
        first = bench.run(declaring)
        assert first.ok

        state = State.load(bench.path / "state.json")
        search_id = bench.cfg.searches[0].id
        # A car the searches never returned, filed under one of them, with no
        # record of the site ever having listed it as a result.
        rail = a_car(99)
        state.record(rail)
        state.listings[rail.id]["search_id"] = search_id
        state.listings[rail.id].pop("in_results_at", None)
        state.save()

        second = bench.run(declaring, minutes_later=200)
        after = State.load(bench.path / "state.json")
        assert rail.id not in after.listings, (
            "a stored car the search has never returned should be dropped")
        assert second.discarded >= 1

        third = bench.run(declaring, minutes_later=400)
        assert third.discarded == 0, (
            "the sweep has to empty itself - a second pass with nothing left "
            "to reconcile must drop nothing")

    def test_a_car_the_site_listed_goes_the_removal_route_instead(self, bench, fixture_html):
        """A car that WAS a result and then stopped being one has sold, or
        been taken down. That is the removal path's business, and losing it
        to the sweep would throw away the one signal you asked for."""
        declaring = fixture_html("search_2026_full")
        bench.run(declaring)
        state = State.load(bench.path / "state.json")
        search_id = bench.cfg.searches[0].id
        sold = a_car(77)
        state.record(sold)
        state.listings[sold.id]["search_id"] = search_id
        # The site named it a result once.
        state.listings[sold.id]["in_results_at"] = "2026-09-01T00:00:00+00:00"
        state.save()

        bench.run(declaring, minutes_later=200)
        after = State.load(bench.path / "state.json")
        assert sold.id in after.listings, (
            "a car the site once listed is not a rail; it is a car that has "
            "gone, and the removal path decides that")

    def test_a_car_you_shortlisted_is_never_dropped(self, bench, fixture_html):
        declaring = fixture_html("search_2026_full")
        bench.run(declaring)
        state = State.load(bench.path / "state.json")
        search_id = bench.cfg.searches[0].id
        mine = a_car(55)
        state.record(mine)
        state.listings[mine.id]["search_id"] = search_id
        state.listings[mine.id].pop("in_results_at", None)
        state.listings[mine.id]["you"] = {"shortlisted": True}
        state.save()

        bench.run(declaring, minutes_later=200)
        after = State.load(bench.path / "state.json")
        assert mine.id in after.listings, (
            "a car you put on your own list is yours, whatever the "
            "bot thinks of its provenance")


class TestItWillNotActOnAPartialAnswer:

    def test_a_search_that_could_not_be_read_drops_nothing(self, bench, fixture_html):
        declaring = fixture_html("search_2026_full")
        bench.run(declaring)
        state = State.load(bench.path / "state.json")
        search_id = bench.cfg.searches[0].id
        rail = a_car(42)
        state.record(rail)
        state.listings[rail.id]["search_id"] = search_id
        state.listings[rail.id].pop("in_results_at", None)
        state.save()

        # The site refuses this run, so nothing is known about the search.
        report = bench.run(declaring, fail="boom", minutes_later=200)
        after = State.load(bench.path / "state.json")
        assert rail.id in after.listings, (
            "a run that could not read the search proves nothing about what "
            "the search contains")
        assert report.discarded == 0
