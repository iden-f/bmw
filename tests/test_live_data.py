"""Behaviour proven against the real listings the bot pulled from autotrader.ca.

The fixture here is rebuilt from the 19 cars a live run actually returned -
real ids, prices, odometers, dealers and cities - so price drops, removals,
relistings and deduplication are exercised on the shapes the site really
produces rather than on hand-written ones.
"""
import json
import re

import pytest

from autotrader import runner as runner_mod
from autotrader.config import Config
from autotrader.http import FetchError, Response
from autotrader.parser import parse_search_page
from autotrader.runner import run
from autotrader.state import Change, State

from .helpers import Capture, FakeFetcher, next_check, use_channels

BASE = "https://www.autotrader.ca/cars/honda/civic"


@pytest.fixture
def live_html(fixture_html):
    return fixture_html("search_next_data")


@pytest.fixture
def live(tmp_path, monkeypatch, live_html):
    """A watcher pointed at the real captured results page."""
    monkeypatch.chdir(tmp_path)
    cfg = Config.defaults(tmp_path / "config.json")
    cfg.add_search(f"{BASE}?rcp=25&prx=-1", "Example search")
    cfg.set("scraping.delay_ms", 0)
    cfg.set("scraping.retries", 0)
    cfg.set("scraping.enrich_details", False)   # the page already has everything
    cfg.set("archive.mode", "off")
    # This page carries 19 cars and the shipped digest names 12, so the seven
    # it cannot name stay owed and arrive on the following run - see
    # tests/test_notify.py, which is where that behaviour belongs. Every test
    # in this file is about deduplication: whether a car the bot has already
    # told you about comes back. Leaving the cap in would have them measuring
    # the digest's length instead, and "the same page twice announces once"
    # would fail for a car that had never been announced at all.
    cfg.set("notifications.max_listings_per_message", 50)
    cfg.save()

    sink = Capture()
    use_channels(monkeypatch, runner_mod, [sink])

    def go(html=None, **kw):
        # Each run happens a schedule-interval after the last, because every
        # rule this suite exercises - the removal grace, the silence alarm,
        # the deduplication floor - is a statement about elapsed time, and
        # none of them are defined for two runs in the same second.
        next_check(kw.pop("minutes_later", None))
        return run(cfg, State.load(tmp_path / "state.json"),
                   fetcher=FakeFetcher(html or live_html), env={}, **kw)

    return type("Live", (), {"cfg": cfg, "sink": sink, "run": staticmethod(go),
                             "path": tmp_path, "html": live_html})


def drop_price(html, old, new):
    """Change one car's price in the real payload, as the site would."""
    assert f'"priceRaw":{old}' in html
    return (html.replace(f'"priceRaw":{old}', f'"priceRaw":{new}')
                .replace(f'"priceFormatted":"$ {old:,}"', f'"priceFormatted":"$ {new:,}"'))


def publish_price(html, listing_id, price):
    """Give one real call-for-price car a figure, as a dealer eventually does."""
    payload = json.loads(re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                                   html, re.S).group(1))
    listings = payload["props"]["pageProps"]["searchResults"]["listings"]
    target = next(l for l in listings if listing_id in l["url"])
    assert target["price"]["priceRaw"] is None, "that car already had a price"
    target["price"] = {"priceFormatted": f"$ {price:,}", "priceRaw": price}
    return re.sub(r'(id="__NEXT_DATA__"[^>]*>).*?(</script>)',
                  lambda m: m.group(1) + json.dumps(payload, ensure_ascii=False) + m.group(2),
                  html, count=1, flags=re.S)


def unpriced_ids(html):
    payload = json.loads(re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                                   html, re.S).group(1))
    return [re.search(r"([0-9a-f-]{36})$", l["url"]).group(1)
            for l in payload["props"]["pageProps"]["searchResults"]["listings"]
            if l["price"]["priceRaw"] is None]


def remove_car(html, listing_id):
    """Drop one listing object out of the payload."""
    data = json.loads(re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S).group(1))
    listings = data["props"]["pageProps"]["searchResults"]["listings"]
    kept = [l for l in listings if listing_id not in l["url"]]
    assert len(kept) < len(listings), f"{listing_id} was not in the payload"
    data["props"]["pageProps"]["searchResults"]["listings"] = kept
    return re.sub(r'(<script id="__NEXT_DATA__"[^>]*>).*?(</script>)',
                  lambda m: m.group(1) + json.dumps(data, separators=(",", ":")) + m.group(2),
                  html, flags=re.S)


def ids_in(html):
    from autotrader.urls import listing_id_from_url
    return {listing_id_from_url(u) for u in re.findall(r'"url":"(https[^"]+/offers/[^"]+)"', html)}


class TestTheRealPage:
    def test_every_car_is_read_completely(self, live_html):
        result = parse_search_page(live_html, BASE)
        assert len(result.listings) == 19
        assert result.strategy == "embedded_json"
        for listing in result.listings:
            assert listing.year, listing.url
            assert listing.location and listing.province
            assert listing.make == "BMW" and listing.model == "M5"

    def test_ids_are_uuids_from_the_offer_urls(self, live_html):
        result = parse_search_page(live_html, BASE)
        for listing in result.listings:
            assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                                r"[0-9a-f]{4}-[0-9a-f]{12}", listing.id)

    def test_prices_and_odometers_are_plausible(self, live_html):
        result = parse_search_page(live_html, BASE)
        priced = [l for l in result.listings if l.price]
        assert len(priced) >= 10
        for listing in priced:
            assert 20_000 < listing.price < 400_000, listing.price
        for listing in result.listings:
            if listing.mileage_km is not None:
                assert 0 <= listing.mileage_km < 400_000


class TestDedupeAcrossRuns:
    def test_the_same_page_twice_announces_once(self, live):
        first = live.run()
        assert first.new == 19
        assert len(live.sink.digests[0]) == 19

        live.sink.digests.clear()
        second = live.run()
        assert second.new == 0
        assert live.sink.digests == []

    def test_five_identical_runs_stay_quiet(self, live):
        live.run()
        live.sink.digests.clear()
        for _ in range(5):
            live.run()
        assert live.sink.digests == []

    def test_state_does_not_grow_on_repeat_runs(self, live):
        live.run()
        after_first = len(json.loads((live.path / "state.json").read_text())["listings"])
        for _ in range(3):
            live.run()
        assert len(json.loads((live.path / "state.json").read_text())["listings"]) == after_first


class TestPriceDropsOnRealCars:
    def test_a_real_drop_is_announced_once(self, live):
        live.run()
        live.sink.digests.clear()

        cheaper = drop_price(live.html, 134998, 122500)
        report = live.run(cheaper)
        assert report.price_drops == 1

        change = live.sink.digests[0][0]
        assert change.kind == Change.PRICE_DROP
        assert change.old_price == 134998 and change.new_price == 122500
        assert change.delta == -12498

        live.sink.digests.clear()
        assert live.run(cheaper).price_drops == 0
        assert live.sink.digests == []

    def test_the_price_history_records_both_figures(self, live):
        live.run()
        live.run(drop_price(live.html, 134998, 122500))
        state = json.loads((live.path / "state.json").read_text())
        car = next(v for v in state["listings"].values() if v.get("price") == 122500)
        assert [p["price"] for p in car["price_history"]] == [134998, 122500]

    def test_a_trivial_drop_is_ignored(self, live):
        """$100 off a $135,000 car is not news."""
        live.run()
        live.sink.digests.clear()
        report = live.run(drop_price(live.html, 134998, 134898))
        assert report.price_drops == 0
        assert live.sink.digests == []

    def test_a_per_search_threshold_overrides_the_global_one(self, live):
        live.run()
        live.sink.digests.clear()
        live.cfg.data["searches"][0]["price_drop_min_abs"] = 20000
        live.cfg.save()
        report = live.run(drop_price(live.html, 134998, 122500))
        assert report.price_drops == 0, "a $12,498 drop is below this search's floor"

    def test_a_price_rise_is_not_reported_as_a_drop(self, live):
        live.run()
        live.sink.digests.clear()
        report = live.run(drop_price(live.html, 134998, 141000))
        assert report.price_drops == 0
        assert report.price_rises == 1
        assert live.sink.digests == [], "rises are off by default"


class TestRemovalAndRelisting:
    GONE = "727d36cb-6045-44ea-bb97-7e3c022e2674"

    def test_a_car_needs_two_absences_before_it_counts_as_sold(self, live):
        live.run()
        without = remove_car(live.html, self.GONE)
        assert live.run(without).removed == 0, "one absence is within the grace period"
        assert live.run(without).removed == 1

    def test_a_removal_is_announced_when_asked_for(self, live):
        live.cfg.data["searches"][0]["notify_on"] = {"removed": True}
        live.cfg.save()
        live.run()
        without = remove_car(live.html, self.GONE)
        live.run(without)
        live.sink.digests.clear()
        live.run(without)
        kinds = [c.kind for batch in live.sink.digests for c in batch]
        assert Change.REMOVED in kinds

    def test_a_car_that_comes_back_is_not_announced_again(self, live):
        live.run()
        without = remove_car(live.html, self.GONE)
        live.run(without); live.run(without)
        state = json.loads((live.path / "state.json").read_text())
        assert state["listings"][self.GONE]["status"] == "gone"

        live.sink.digests.clear()
        live.run()                                   # it is back on the page
        state = json.loads((live.path / "state.json").read_text())
        assert state["listings"][self.GONE]["status"] == "active"
        assert live.sink.digests == [], "a relisting of a known car is not news"

    def test_a_car_that_returns_cheaper_reports_the_drop(self, live):
        live.run()
        without = remove_car(live.html, self.GONE)
        live.run(without); live.run(without)
        live.sink.digests.clear()
        report = live.run(drop_price(live.html, 134998, 119000))
        assert report.price_drops == 1


class TestQuietHoursOnRealData:
    def test_alerts_are_held_then_delivered_intact(self, live, monkeypatch):
        monkeypatch.setattr(runner_mod.notifiers, "in_quiet_hours", lambda *a, **k: True)
        held = live.run()
        assert held.quiet and held.new == 19
        assert live.sink.digests == []

        monkeypatch.setattr(runner_mod.notifiers, "in_quiet_hours", lambda *a, **k: False)
        live.run()
        delivered = {c.listing.id for batch in live.sink.digests for c in batch}
        assert len(delivered) == 19, "every held car must arrive"

    def test_nothing_is_delivered_twice_after_quiet_hours(self, live, monkeypatch):
        monkeypatch.setattr(runner_mod.notifiers, "in_quiet_hours", lambda *a, **k: True)
        live.run()
        monkeypatch.setattr(runner_mod.notifiers, "in_quiet_hours", lambda *a, **k: False)
        live.run()
        live.sink.digests.clear()
        live.run()
        assert live.sink.digests == []


class TestPendingQueueOnRealData:
    def test_a_total_outage_loses_nothing(self, live, monkeypatch):
        from autotrader.notifiers import Notifier

        class Down(Notifier):
            name = "down"
            def _send(self, changes, run): raise RuntimeError("HTTP 503")

        use_channels(monkeypatch, runner_mod, [Down({}, {}, {})])
        assert live.run().new == 19

        use_channels(monkeypatch, runner_mod, [live.sink])
        live.run()
        delivered = {c.listing.id for batch in live.sink.digests for c in batch}
        assert len(delivered) == 19

    def test_the_queue_drains_exactly_once(self, live, monkeypatch):
        from autotrader.notifiers import Notifier

        class Down(Notifier):
            name = "down"
            def _send(self, changes, run): raise RuntimeError("HTTP 503")

        use_channels(monkeypatch, runner_mod, [Down({}, {}, {})])
        live.run()
        use_channels(monkeypatch, runner_mod, [live.sink])
        live.run()
        live.sink.digests.clear()
        live.run()
        assert live.sink.digests == []


class TestFiltersOnRealCars:
    def test_a_price_ceiling_keeps_only_the_cheap_ones(self, live):
        live.cfg.set("filters.max_price", 105000)
        live.cfg.save()
        live.run()
        announced = [c.listing for batch in live.sink.digests for c in batch]
        assert announced
        for listing in announced:
            assert listing.price is None or listing.price <= 105000

    def test_a_per_search_ceiling_beats_the_global_one(self, live):
        live.cfg.set("filters.max_price", 200000)
        live.cfg.data["searches"][0]["filters"] = {"max_price": 85000}
        live.cfg.save()
        live.run()
        announced = [c.listing for batch in live.sink.digests for c in batch]
        assert announced
        assert all(l.price is None or l.price <= 85000 for l in announced)

    def test_an_odometer_ceiling_works_on_real_readings(self, live):
        live.cfg.set("filters.max_mileage_km", 50000)
        live.cfg.save()
        live.run()
        announced = [c.listing for batch in live.sink.digests for c in batch]
        assert announced
        assert all(l.mileage_km is None or l.mileage_km <= 50000 for l in announced)

    def test_call_for_price_cars_are_tracked_but_not_announced(self, live):
        """require_price used to make them vanish. Eight of these nineteen
        real cars have no figure on them; they are worth knowing about."""
        live.cfg.set("filters.require_price", True)
        live.cfg.save()
        report = live.run()

        assert report.unpriced >= 1
        announced = [c.listing for batch in live.sink.digests for c in batch]
        assert all(l.price for l in announced), "an unpriced car was announced"

        # Recorded, visible, and flagged - not filtered away.
        entries = json.loads((live.path / "state.json").read_text())["listings"]
        unpriced = [e for e in entries.values() if e.get("unpriced")]
        assert len(unpriced) == report.unpriced
        assert not any(e.get("filtered") for e in unpriced)

    def test_an_unpriced_car_can_be_asked_for(self, live):
        live.cfg.set("filters.require_price", True)
        live.cfg.data["searches"][0]["notify_on"] = {"unpriced": True}
        live.cfg.save()
        live.run()

        announced = [c.listing for batch in live.sink.digests for c in batch]
        assert any(l.price is None for l in announced)

    def test_a_year_floor_uses_the_recovered_model_year(self, live):
        live.cfg.set("filters.min_year", 2023)
        live.cfg.save()
        live.run()
        announced = [c.listing for batch in live.sink.digests for c in batch]
        assert announced
        assert all(l.year >= 2023 for l in announced)

    def test_a_filtered_car_is_never_reported_as_removed(self, live):
        live.cfg.set("filters.max_price", 105000)
        live.cfg.data["searches"][0]["notify_on"] = {"removed": True}
        live.cfg.save()
        for _ in range(4):
            report = live.run()
        assert report.removed == 0


class TestFilteredCarsStayOutOfSight:
    def test_a_filtered_car_is_published_only_as_a_hidden_row(self, live):
        """It has to be countable and explainable, so the dashboard can say
        "44 hidden by your rules" instead of quietly showing a smaller number
        than the site does. It must never look like a car you are watching."""
        from autotrader.dashboard import build_payload
        live.cfg.set("filters.max_price", 105000)
        live.cfg.save()
        live.run()

        state = State.load(live.path / "state.json")
        payload = build_payload(live.cfg, state, {})

        shown = [i for i in payload["listings"] if not i["filtered"]]
        hidden = [i for i in payload["listings"] if i["filtered"]]
        assert shown and hidden
        for item in shown:
            assert item.get("price") is None or item["price"] <= 105000
        for item in hidden:
            assert item["price"] > 105000
            assert "above maximum" in item["filter_reason"]
        assert payload["health"]["counts"]["filtered"] == len(hidden)

    def test_the_live_count_excludes_filtered_cars(self, live):
        live.cfg.set("filters.max_price", 105000)
        live.cfg.save()
        live.run()
        state = State.load(live.path / "state.json")
        active = [v for v in state.listings.values()
                  if v.get("status") == "active" and not v.get("filtered")]
        assert state.stats()["active"] == len(active)
        assert all((v.get("price") or 0) <= 105000 for v in active)

    def test_filtered_cars_are_still_tracked_internally(self, live):
        """They must be, or the next run reports them as removed."""
        live.cfg.set("filters.max_price", 105000)
        live.cfg.data["searches"][0]["notify_on"] = {"removed": True}
        live.cfg.save()
        for _ in range(4):
            report = live.run()
        assert report.removed == 0
        state = State.load(live.path / "state.json")
        assert any(v.get("filtered") for v in state.listings.values())

    def test_a_car_that_stops_being_filtered_becomes_visible(self, live):
        live.cfg.set("filters.max_price", 85000)
        live.cfg.save()
        live.run()
        before = State.load(live.path / "state.json").stats()["active"]

        live.cfg.set("filters.max_price", None)
        live.cfg.save()
        live.run()
        after = State.load(live.path / "state.json").stats()["active"]
        assert after > before


class TestForgettingRemovedSearches:
    def test_a_removed_search_stops_being_reported(self, live):
        live.run()
        state = State.load(live.path / "state.json")
        assert set(state.data["searches"])

        stale = live.cfg.searches[0].id
        live.cfg.remove_search(stale)
        live.cfg.add_search("https://www.autotrader.ca/cars/toyota/corolla/?prx=-2",
                            "Corolla")
        live.cfg.save()
        live.run()

        state = State.load(live.path / "state.json")
        assert stale not in state.data["searches"], \
            "a search you deleted must not keep reporting its last failure"

    def test_configured_searches_are_kept(self, live):
        live.run()
        live.run()
        state = State.load(live.path / "state.json")
        assert {s.id for s in live.cfg.searches} <= set(state.data["searches"])


class TestCallForPriceOnRealCars:
    """Eight of these nineteen real cars publish no figure at all.

    Under the old rule, ``require_price`` deleted them from view: not filtered
    into a bucket, not counted, simply absent. On a search whose whole point is
    finding a bargain, the cars a dealer will not price in public are the last
    ones you want silently dropped.
    """

    def test_the_real_payload_really_does_contain_unpriced_cars(self, live_html):
        assert len(unpriced_ids(live_html)) == 8

    def test_they_are_kept_without_require_price(self, live):
        report = live.run()
        assert report.unpriced == 0        # nothing to bucket: they are kept
        entries = json.loads((live.path / "state.json").read_text())["listings"]
        assert sum(1 for e in entries.values() if e.get("price") is None) == 8

    def test_a_price_that_appears_later_is_announced(self, live):
        live.cfg.set("filters.require_price", True)
        live.cfg.save()
        live.run()
        live.sink.digests.clear()

        target = unpriced_ids(live.html)[0]
        live.run(publish_price(live.html, target, 96500))

        announced = [c for batch in live.sink.digests for c in batch]
        priced = [c for c in announced if c.kind == Change.PRICED]
        assert len(priced) == 1
        assert priced[0].listing.id == target
        assert priced[0].new_price == 96500
        assert "96,500" in priced[0].describe()

    def test_a_published_price_is_not_reported_as_a_price_drop(self, live):
        """There is nothing to compare against, so calling it a drop is a lie."""
        live.run()
        live.sink.digests.clear()
        target = unpriced_ids(live.html)[0]
        report = live.run(publish_price(live.html, target, 96500))

        assert report.price_drops == 0 and report.price_rises == 0
        assert report.priced == 1

    def test_an_unpriced_car_is_never_re_announced_as_new(self, live):
        """It was recorded quietly, so it must not resurface as a discovery."""
        live.cfg.set("filters.require_price", True)
        live.cfg.save()
        live.run()
        live.sink.digests.clear()

        report = live.run()
        assert report.new == 0
        assert not [c for batch in live.sink.digests for c in batch]

    def test_an_unpriced_car_is_not_reported_as_removed(self, live):
        """It is still on the page; it just has no figure on it."""
        live.cfg.set("filters.require_price", True)
        live.cfg.data["searches"][0]["notify_on"] = {"removed": True}
        live.cfg.save()
        for _ in range(4):
            report = live.run()
        assert report.removed == 0

    def test_the_detail_page_is_consulted_a_bounded_number_of_times(
            self, live, monkeypatch):
        """Worth a look; not worth a request every run forever."""
        live.cfg.set("scraping.enrich_details", True)
        live.cfg.set("scraping.unpriced_rechecks", 2)
        live.cfg.save()

        fetched: list[str] = []
        target = unpriced_ids(live.html)[0]

        def go():
            fetcher = FakeFetcher(live.html)
            original = fetcher.get

            def spy(url, **kw):
                if "/offers/" in url:
                    fetched.append(url)
                return original(url, **kw)

            fetcher.get = spy
            return run(live.cfg, State.load(live.path / "state.json"),
                       fetcher=fetcher, env={})

        for _ in range(5):
            go()

        looks = [u for u in fetched if target in u]
        # Once as an unknown car, then twice more on the recheck allowance.
        assert len(looks) == 3, looks


def page_of(html, ids):
    """The real payload cut down to a chosen set of listings."""
    payload = json.loads(re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                                   html, re.S).group(1))
    results = payload["props"]["pageProps"]["searchResults"]
    keep = [l for l in results["listings"]
            if re.search(r"([0-9a-f-]{36})$", l["url"]).group(1) in ids]
    results["listings"] = keep
    stripped = re.sub(r'(id="__NEXT_DATA__"[^>]*>).*?(</script>)',
                      lambda m: m.group(1) + json.dumps(payload) + m.group(2),
                      html, count=1, flags=re.S)
    return re.sub(r'<script[^>]*application/ld\+json[^>]*>.*?</script>', "",
                  stripped, flags=re.S)


def all_ids(html):
    payload = json.loads(re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                                   html, re.S).group(1))
    return [re.search(r"([0-9a-f-]{36})$", l["url"]).group(1)
            for l in payload["props"]["pageProps"]["searchResults"]["listings"]]


class RotatingSite:
    """A search bigger than the bot reads, whose pages shuffle between runs.

    This is what autotrader.ca actually does: the results carry
    ``tier_rotation=true``, so which dealers surface on which page changes
    from request to request. Reading 60 of 186 results therefore gives a
    different 60 each time - and the first long live run turned that into
    fourteen "removed" alerts in a single run, for cars still on page one.
    """

    def __init__(self, html, ids, window, sold=None):
        self.html, self.ids, self.window = html, ids, window
        self.offset = 0
        # One car genuinely off the market: absent from every page from now on,
        # and its own listing page answers 404. Everything else stays live, so
        # a test cannot pass by treating the whole site as gone.
        self.sold = sold
        self.detail_hits: list[str] = []
        self.stats = {"requests": 0, "spent": 0, "budget": 0}
        self.spent = 0

    @property
    def budget_left(self):
        return 1_000_000

    def rotate(self):
        self.offset = (self.offset + self.window) % len(self.ids)

    def _slice(self, page):
        """A full page every time, so a short page still means "the end".

        The sold car is simply not among the results any more; the site does
        not serve a page with a hole in it.
        """
        live = [i for i in self.ids if i != self.sold]
        start = (self.offset + (page - 1) * self.window) % len(live)
        return {live[(start + n) % len(live)] for n in range(self.window)}

    def get(self, url, referer=None, allow_block=False):
        self.spent += 1
        self.stats["requests"] = self.spent
        if "/offers/" in url:
            self.detail_hits.append(url)
            if self.sold and self.sold in url:
                raise FetchError(f"HTTP 404 from {url}")
            return Response(url=url, status=200, elapsed_ms=1, text=(
                '<html><head><script type="application/ld+json">'
                '{"@context":"https://schema.org","@type":"Car","name":"Example Coupe",'
                '"offers":{"@type":"Offer","price":90000,"priceCurrency":"CAD"}}'
                '</script></head><body>still for sale</body></html>'))
        page = int(re.search(r"[?&]page=(\d+)", url).group(1)) if "page=" in url else 1
        return Response(url=url, status=200, elapsed_ms=1,
                        text=page_of(self.html, self._slice(page)))

    def get_bytes(self, *a, **k):
        return None

    def close(self):
        pass


class TestARotatingResultWindow:
    def _watch(self, live, site):
        next_check()
        return run(live.cfg, State.load(live.path / "state.json"),
                   fetcher=site, env={})

    def _site(self, live, **kw):
        live.cfg.set("scraping.max_pages", 2)
        live.cfg.set("scraping.results_per_page", 6)
        live.cfg.save()
        return RotatingSite(live.html, all_ids(live.html), window=6, **kw)

    def test_a_car_that_only_left_the_sample_is_not_reported_as_sold(self, live):
        site = self._site(live)
        for _ in range(5):
            site.rotate()
            report = self._watch(live, site)
        assert report.removed == 0, "a rotating window invented a removal"

    def test_it_asks_the_listing_page_before_deciding(self, live):
        site = self._site(live)
        for _ in range(4):
            site.rotate()
            self._watch(live, site)
        assert site.detail_hits, "nothing was verified; removals were guessed"

    def test_the_one_car_that_really_sold_is_reported(self, live):
        """The check has to be able to say yes, or it is just a mute button."""
        live.run()                                  # see everything once
        sold = all_ids(live.html)[0]
        site = self._site(live, sold=sold)

        for _ in range(6):
            site.rotate()
            self._watch(live, site)

        entries = json.loads((live.path / "state.json").read_text())["listings"]
        assert entries[sold]["status"] == "gone"
        others = [e for i, e in entries.items() if i != sold]
        assert all(e["status"] == "active" for e in others), \
            "the rotating cars were swept up with the sold one"

    def test_one_404_is_not_enough_to_call_a_sale(self, live):
        """A listing URL carries an SEO slug in front of its id, and the slug
        changes when the seller edits the ad. A single 404 could be a retitled
        car as easily as a sold one."""
        live.run()
        sold = all_ids(live.html)[0]
        site = self._site(live, sold=sold)

        seen_evidence = False
        for _ in range(6):
            site.rotate()
            self._watch(live, site)
            entry = json.loads((live.path / "state.json").read_text())["listings"][sold]
            if entry.get("gone_evidence"):
                seen_evidence = True
                # One 404 recorded, and the car not yet written off.
                assert entry["status"] == "active"
                break
        assert seen_evidence, "the 404 was not remembered, so it never adds up"

    def test_evidence_is_forgotten_when_the_car_turns_up_again(self, live):
        live.run()
        sold = all_ids(live.html)[0]
        site = self._site(live, sold=sold)
        site.rotate()
        self._watch(live, site)
        site.rotate()
        self._watch(live, site)

        site.sold = None                            # it was there all along
        site.window = len(site.ids)
        self._watch(live, site)

        entry = json.loads((live.path / "state.json").read_text())["listings"][sold]
        assert not entry.get("gone_evidence")
        assert entry["status"] == "active"

    def test_verification_is_capped_so_it_cannot_eat_a_run(self, live):
        site = self._site(live)
        for _ in range(3):
            site.rotate()
            before = len(site.detail_hits)
            self._watch(live, site)
            assert len(site.detail_hits) - before <= 12

    def test_a_search_we_read_to_the_end_still_needs_no_verification(self, live):
        """Nothing changes for a search small enough to read completely.

        There the sample *is* the result set, so absence really is evidence
        and a removal costs no extra request to establish.
        """
        live.run()
        target = unpriced_ids(live.html)[0]
        without = remove_car(live.html, target)

        removed = sum(live.run(without).removed for _ in range(3))
        assert removed == 1
        state = json.loads((live.path / "state.json").read_text())
        assert state["listings"][target]["status"] == "gone"


class TestARealCarComingBack:
    """Removal and relisting are one loop, and it has to close.

    The first long live run marked fourteen cars removed that were still on
    sale. Those cars will reappear in the sample, and what happens then
    decides whether a wrong removal is self-correcting or permanent - and
    whether correcting it costs the user a second round of "new listing"
    alerts for cars they have already been told about.
    """

    def test_a_car_that_comes_back_is_a_relisting_not_a_discovery(self, live):
        target = unpriced_ids(live.html)[0]
        live.run()
        without = remove_car(live.html, target)
        for _ in range(3):
            live.run(without)
        assert json.loads((live.path / "state.json").read_text()
                          )["listings"][target]["status"] == "gone"

        live.sink.digests.clear()
        report = live.run()

        assert report.relisted == 1
        assert report.new == 0, "a returning car was announced as a discovery"
        entry = json.loads((live.path / "state.json").read_text())["listings"][target]
        assert entry["status"] == "active"
        assert entry["relisted_at"]

    def test_it_stays_quiet_unless_asked(self, live):
        target = unpriced_ids(live.html)[0]
        live.run()
        for _ in range(3):
            live.run(remove_car(live.html, target))
        live.sink.digests.clear()

        live.run()
        assert not [c for batch in live.sink.digests for c in batch]

    def test_and_says_so_when_asked(self, live):
        target = unpriced_ids(live.html)[0]
        live.cfg.data["searches"][0]["notify_on"] = {"relisted": True}
        live.cfg.save()
        live.run()
        for _ in range(3):
            live.run(remove_car(live.html, target))
        live.sink.digests.clear()

        live.run()
        announced = [c for batch in live.sink.digests for c in batch]
        assert [c.kind for c in announced] == [Change.RELISTED]
        assert "Back on the market" in announced[0].describe()

    def test_a_car_that_comes_back_cheaper_reports_both_facts(self, live):
        """It is counted as a drop and told as a relisting.

        The drop must reach the user - relist alerts are off by default, so
        letting the relisting swallow the event would have quietly stopped
        announcing a whole class of the best price drops there are. It is
        billed to price_drops and gated by the price-drop switch; only the
        wording changes, to say that the car was withdrawn first.
        """
        priced = [l for l in parse_search_page(live.html, BASE).listings if l.price]
        target, was = priced[0].id, priced[0].price
        live.run()
        for _ in range(3):
            live.run(remove_car(live.html, target))

        report = live.run(drop_price(live.html, was, was - 9000))
        assert report.price_drops == 1
        assert report.relisted == 1
        sent = [c for batch in live.sink.digests for c in batch]
        assert any("cheaper" in c.describe() for c in sent), \
            [c.describe() for c in sent]

    def test_a_call_for_price_car_coming_back_is_counted_too(self, live):
        """The quiet bucket still has to keep its books straight."""
        live.cfg.set("filters.require_price", True)
        live.cfg.save()
        target = unpriced_ids(live.html)[0]
        live.run()
        for _ in range(3):
            live.run(remove_car(live.html, target))

        report = live.run()
        assert report.relisted == 1
        assert report.new == 0


class TestTwoSearchesThatOverlap:
    """Two searches that return the same cars.

    A Canada-wide any-year watch is a superset of a 2021-onwards one, so every
    car the narrow search finds is also found by the broad one. Whichever ran
    last used to take ownership of the car - and with it, which search's rules
    applied. The narrow watch reported zero cars while working perfectly, and
    its cars were being filtered out by a price ceiling it does not have.
    """

    @pytest.fixture
    def pair(self, tmp_path, monkeypatch, live_html):
        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.add_search(f"{BASE}?modelyearfrom=2021", "Narrow")
        cfg.add_search(f"{BASE}?prx=-2", "Broad")
        cfg.set("scraping.delay_ms", 0)
        cfg.set("scraping.retries", 0)
        cfg.set("scraping.enrich_details", False)
        cfg.set("archive.mode", "off")
        # The broad watch will not pay more than $85,000; the narrow one has
        # no ceiling at all.
        cfg.data["searches"][1]["filters"] = {"max_price": 85000}
        cfg.save()

        sink = Capture()
        use_channels(monkeypatch, runner_mod, [sink])

        def go(html=None):
            return run(cfg, State.load(tmp_path / "state.json"),
                       fetcher=FakeFetcher(html or live_html), env={})

        return type("Pair", (), {"cfg": cfg, "sink": sink, "run": staticmethod(go),
                                 "path": tmp_path, "html": live_html})

    def _entries(self, pair):
        return json.loads((pair.path / "state.json").read_text())["listings"]

    def test_the_first_search_to_list_a_car_owns_it(self, pair):
        pair.run()
        owners = {e["search_name"] for e in self._entries(pair).values()}
        assert owners == {"Narrow"}, owners

    def test_a_car_one_search_wants_is_not_filtered_out_by_the_other(self, pair):
        pair.run()
        dear = [e for e in self._entries(pair).values()
                if (e.get("price") or 0) > 85000]
        assert dear, "no car expensive enough to test with"
        assert not any(e["filtered"] for e in dear), \
            "the broad watch's ceiling was applied to the narrow watch's cars"

    def test_the_narrow_search_still_reports_its_own_cars(self, pair):
        pair.run()
        from autotrader.dashboard import build_payload
        payload = build_payload(pair.cfg, State.load(pair.path / "state.json"), {})
        counts = {s["name"]: s["counts"]["total"] for s in payload["searches"]}
        assert counts["Narrow"] > 0

    def test_a_car_the_other_search_still_lists_is_not_called_sold(self, pair):
        """Absence from one search is not absence from the site."""
        pair.run()
        entries = self._entries(pair)
        target = next(i for i, e in entries.items() if e["search_name"] == "Narrow")

        # The narrow search stops matching it; the broad one still returns it.
        narrowed = remove_car(pair.html, target)

        def two_sided(url):
            return narrowed if "modelyearfrom" in url else pair.html

        class Sided(FakeFetcher):
            def get(self, url, referer=None, allow_block=False):
                self.search_html = two_sided(url)
                return super().get(url, referer=referer, allow_block=allow_block)

        for _ in range(4):
            report = run(pair.cfg, State.load(pair.path / "state.json"),
                         fetcher=Sided(pair.html), env={})
        assert report.removed == 0
        assert self._entries(pair)[target]["status"] == "active"

    def test_a_car_one_search_rejects_can_still_be_taken_up_by_the_next(self, pair):
        """Ownership must not become a veto.

        If claiming a car happened on sight rather than on acceptance, an
        older car the broad watch would happily show would be hidden by a rule
        belonging to a watch that does not want it.
        """
        pair.cfg.data["searches"][0]["filters"] = {"min_year": 2023}
        pair.cfg.data["searches"][1]["filters"] = {}
        pair.cfg.save()
        pair.run()

        older = [e for e in self._entries(pair).values()
                 if (e.get("year") or 9999) < 2023]
        assert older, "no older car in the payload to test with"
        assert not any(e["filtered"] for e in older)
        assert {e["search_name"] for e in older} == {"Broad"}

    def test_a_car_no_search_wants_stays_hidden_with_the_last_reason(self, pair):
        pair.cfg.data["searches"][0]["filters"] = {"min_year": 2023}
        pair.cfg.data["searches"][1]["filters"] = {"min_year": 2023}
        pair.cfg.save()
        report = pair.run()

        older = [e for e in self._entries(pair).values()
                 if (e.get("year") or 9999) < 2023]
        assert older and all(e["filtered"] for e in older)
        assert all("below minimum" in e["filter_reason"] for e in older)
        # Counted once, not once per search that turned it down.
        assert report.filtered_out == len(older)

    def test_a_car_the_first_search_rejects_is_still_announced_by_the_second(
            self, pair):
        """The failure this guards against is silent, which is the worst kind.

        Recording a rejection immediately marks the car seen-and-silenced. A
        later search that wants it then finds it already known, produces no
        "new listing" change, and the car is stored and never mentioned.
        """
        pair.cfg.data["searches"][0]["filters"] = {"min_year": 2023}
        pair.cfg.data["searches"][1]["filters"] = {}
        pair.cfg.save()

        report = pair.run()
        announced = [c.listing for batch in pair.sink.digests for c in batch]
        older = [l for l in announced if (l.year or 9999) < 2023]

        assert older, "cars the narrow watch turned down were never announced"
        assert report.new == len(announced)
        entries = self._entries(pair)
        assert all(not entries[l.id]["filtered"] for l in older)
