"""Facebook Marketplace: reading its pages, planning its searches, taking a
batch in.

Every page here is invented in the shape Marketplace uses - listings nested
deep in a page's JSON, one document per line in an API reply - with made-up
cars, ids and places.
"""
from __future__ import annotations

import base64
import json
import secrets
from datetime import timedelta
from pathlib import Path

import pytest

from autotrader import clock, events
from autotrader import marketplace as M
from autotrader import runner as runner_mod
from autotrader import vault as V
from autotrader.config import Config
from autotrader.dashboard import build_payload
from autotrader.listing import Listing, on_marketplace
from autotrader.render import headline, push_title
from autotrader.state import Change, State

from .helpers import Capture, use_channels

KEY = bytes(range(32))
SEARCH_URL = "https://www.autotrader.ca/cars/honda/civic/?prx=-2"


def node(item_id, title, *, price="30000.00", city="Ottawa", state="ON",
         sub="45K km", sold=False, pending=False):
    """One result the way a search page nests it."""
    return {
        "__typename": "MarketplaceFeedListingStoryObject", "story_type": "POST",
        "listing": {
            "__typename": "GroupCommerceProductItem", "id": item_id,
            "primary_listing_photo": {"__typename": "ProductImage",
                                      "image": {"uri": f"https://cdn.example/{item_id}.jpg"},
                                      "id": f"9{item_id}"},
            "listing_price": {"formatted_amount": "$30,000", "amount": price,
                              "amount_with_offset_in_currency": "3000000"},
            "location": {"reverse_geocode": {
                "city": city, "state": state,
                "city_page": {"display_name": f"{city}, Ontario", "id": "1"}}},
            "is_hidden": False, "is_live": True,
            "is_pending": pending, "is_sold": sold,
            "marketplace_listing_title": title, "custom_title": None,
            "custom_sub_titles_with_rendering_flags": [{"subtitle": sub}],
            "marketplace_listing_seller": {"__typename": "User", "name": "A Seller",
                                           "id": "100000"},
        },
        "id": f"{item_id}:IN_MEMORY_MARKETPLACE_FEED_STORY_ENT:MarketplaceFeedStoryBase:503",
    }


def search_page(*nodes):
    """A results page: the first results ride in the page's own JSON."""
    doc = {"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"require": [[
        "RelayPrefetchedStreamCache", "next", [], ["adp_x", {"__bbox": {"result": {
            "data": {"marketplace_search": {"feed_units": {"edges": [
                {"node": n, "cursor": None} for n in nodes]}}}}}}]]]}}]]]}
    return ('<html><head></head><body>'
            f'<script type="application/json" data-sjs>{json.dumps(doc)}</script>'
            '<script type="application/json">{"unrelated": [1, 2]}</script>'
            '</body></html>')


def api_reply(*nodes):
    """More results as they arrive while scrolling: several documents."""
    first = {"data": {"marketplace_search": {"feed_units": {"edges": [
        {"node": n} for n in nodes]}}}}
    return "for (;;);" + json.dumps(first) + "\n" + json.dumps({"extensions": {"is_final": True}})


def item_page(item_id, title, **vehicle):
    """A listing's own page, which knows the car's details."""
    target = {"__typename": "GroupCommerceProductItem", "id": item_id,
              "marketplace_listing_title": title,
              "vehicle_make_display_name": vehicle.get("make", "Honda"),
              "vehicle_model_display_name": vehicle.get("model", "Civic"),
              "vehicle_trim_display_name": vehicle.get("trim", "Si Coupe 2D"),
              "vehicle_odometer_data": {"unit": vehicle.get("unit", "KILOMETERS"),
                                        "value": vehicle.get("odometer", 61234)},
              "vehicle_transmission_type": "MANUAL",
              "vehicle_exterior_color": "blue",
              "vehicle_fuel_type": "GASOLINE",
              "vehicle_seller_type": vehicle.get("seller", "PRIVATE_SELLER"),
              "vehicle_title_status": vehicle.get("status"),
              "is_sold": vehicle.get("sold", False), "is_pending": False,
              "listing_price": {"amount": "29500.00", "currency": "CAD"}}
    doc = {"require": [["x", {"__bbox": {"result": {"data": {"viewer": {
        "marketplace_product_details_page": {"target": target}}}}}}]]}
    return f'<script type="application/json">{json.dumps(doc)}</script>'


# ================================================================ reading

class TestReadingAPage:

    def test_finds_the_results_nested_in_the_page(self):
        found = M.collect([search_page(node("100000001", "2018 Honda civic si coupe 2d"),
                                       node("100000002", "2017 Honda civic lx sedan 4d"))])
        assert set(found) == {"100000001", "100000002"}
        car = found["100000001"]
        assert car["title"] == "2018 Honda civic si coupe 2d"
        assert car["price"] == 30000
        assert car["city"] == "Ottawa" and car["province"] == "ON"
        assert car["photo"] == "https://cdn.example/100000001.jpg"
        assert car["seller_type"] == "private"
        assert car["sold"] is False and car["pending"] is False

    def test_a_photo_or_a_story_is_not_a_car(self):
        found = M.collect([search_page(node("100000001", "2018 Honda civic"))])
        # The photo has a numeric id too, and the story a composite one.
        assert list(found) == ["100000001"]

    def test_reads_every_document_in_an_api_reply(self):
        found = M.collect([api_reply(node("100000003", "2016 Honda civic"),
                                     node("100000004", "2019 Honda civic"))])
        assert set(found) == {"100000003", "100000004"}

    def test_the_card_mileage_is_rounded_and_says_so(self):
        car = M.collect([search_page(node("100000001", "2018 Honda civic",
                                          sub="45K km"))])["100000001"]
        assert car["mileage_km"] == 45000 and car["mileage_rounded"] is True
        miles = M.collect([search_page(node("100000001", "2018 Honda civic",
                                            sub="80K miles"))])["100000001"]
        assert miles["mileage_km"] == 128748

    def test_a_listing_page_fills_in_the_car(self):
        found = M.collect([search_page(node("100000001", "2018 Honda civic si coupe 2d")),
                           item_page("100000001", "2018 Honda civic si coupe 2d")])
        car = found["100000001"]
        assert car["mileage_km"] == 61234 and "mileage_rounded" not in car
        assert (car["make"], car["model"], car["trim"]) == ("Honda", "Civic", "Si Coupe 2D")
        assert car["transmission"] == "Manual" and car["fuel"] == "Gasoline"
        assert car["currency"] == "CAD"

    def test_sold_anywhere_is_sold(self):
        found = M.collect([search_page(node("100000001", "2018 Honda civic")),
                           item_page("100000001", "2018 Honda civic", sold=True)])
        assert found["100000001"]["sold"] is True

    @pytest.mark.parametrize("amount", ["1.00", "1234.00", "0.00", "123456.00"])
    def test_a_placeholder_price_is_no_price(self, amount):
        car = M.collect([search_page(node("100000001", "2018 Honda civic",
                                          price=amount))])["100000001"]
        assert "price" not in car

    def test_a_province_by_name_is_its_code(self):
        car = M.collect([search_page(node("100000001", "2018 Honda civic",
                                          state="Ontario"))])["100000001"]
        assert car["province"] == "ON"

    def test_garbage_is_ignored(self):
        assert M.collect(["", "<html>nothing</html>", "{not json", "null"]) == {}


class TestNamingTheCar:

    def test_tidies_the_lower_cased_model(self):
        assert M.tidy_title("2018 Audi q5 suv 4d") == "2018 Audi Q5 SUV 4D"
        assert M.tidy_title("2018 Honda Civic Si") == "2018 Honda Civic Si"

    def test_a_model_is_matched_word_for_word(self):
        assert M.model_in("2021 X5 M Competition", ["X5 M"]) == "X5 M"
        assert M.model_in("2021 X5 M50i", ["X5 M"]) == ""
        assert M.model_in("2019 Honda Civic Type R", ["Civic"]) == "Civic"

    def test_a_record_becomes_a_car_the_rules_can_judge(self):
        search = type("S", (), {"id": "s1", "name": "Example search"})()
        rec = {"id": "100000001", "title": "2018 Honda civic si coupe 2d",
               "price": 25000, "city": "Ottawa", "province": "ON",
               "mileage_km": 45000, "photo": "https://cdn.example/a.jpg"}
        car = M.to_listing(rec, search, make="Honda", models=["Civic"])
        assert car.id == "fb-100000001" and on_marketplace(car.id)
        assert car.url == "https://www.facebook.com/marketplace/item/100000001/"
        assert (car.year, car.make, car.model) == (2018, "Honda", "Civic")
        assert car.title == "2018 Honda Civic Si Coupe 2D"
        assert car.site == "marketplace" and car.source == "marketplace"
        assert car.images == ["https://cdn.example/a.jpg"]

    def test_another_model_is_named_so_the_rule_can_turn_it_away(self):
        search = type("S", (), {"id": "s1", "name": "Example search"})()
        car = M.to_listing({"id": "100000009", "title": "2018 Honda accord ex"},
                           search, make="Honda", models=["Civic"])
        assert car.model == "ACCORD"

    def test_a_rebuilt_title_is_said_in_the_name(self):
        search = type("S", (), {"id": "s1", "name": "Example search"})()
        car = M.to_listing({"id": "100000001", "title": "2018 Honda civic",
                            "title_status": "Rebuilt"}, search)
        assert "Rebuilt title" in car.title

    def test_no_autotrader_id_looks_like_a_marketplace_one(self):
        assert not on_marketplace("00000000-0000-4000-8000-000000000001")
        assert not on_marketplace("5-12345678")
        assert not on_marketplace("fb-12")
        assert on_marketplace("fb-100000001")


# ================================================================= plan

@pytest.fixture
def cfg(tmp_path):
    c = Config.defaults(tmp_path / "config.json")
    c.add_search(SEARCH_URL, "Example search")
    c.data["searches"][0]["filters"] = {"models": ["Civic"], "min_year": 2015,
                                        "near": "Ottawa, ON", "max_distance_km": 1000}
    c.set("archive.mode", "off")
    c.save()
    return c


class TestThePlan:

    def test_each_search_becomes_a_newest_first_query(self, cfg):
        [item] = M.plan(cfg)
        assert item["search"] == cfg.searches[0].id
        assert item["make"] == "Honda" and item["models"] == ["Civic"]
        [query] = item["queries"]
        assert query["query"] == "Honda Civic"
        url = query["url"]
        assert url.startswith("https://www.facebook.com/marketplace/ottawa/search/?")
        for part in ("query=Honda+Civic", "minYear=2015", "sortBy=creation_time_descend",
                     "radius=500"):
            assert part in url, part
        assert item["latitude"] and item["longitude"]

    def test_the_radius_is_capped_at_what_marketplace_offers(self, cfg):
        assert M.plan(cfg)[0]["radius_km"] == M.MAX_RADIUS_KM
        cfg.set("marketplace.radius_km", 80)
        assert M.plan(cfg)[0]["radius_km"] == 80

    def test_the_scope_moves_when_the_search_does(self, cfg):
        before = M.plan(cfg)[0]["scope"]
        assert M.plan(cfg)[0]["scope"] == before
        cfg.data["searches"][0]["filters"]["min_year"] = 2016
        assert M.plan(cfg)[0]["scope"] != before

    def test_a_paused_search_is_not_asked(self, cfg):
        cfg.data["searches"][0]["enabled"] = False
        assert M.plan(cfg) == []


# ================================================================ batch

def batch(*parts, polled=True, session="ok", at=None, host="collector-a"):
    return {"v": 1, "id": secrets.token_hex(16),
            "at": (at or clock.now()).isoformat(timespec="seconds"),
            "host": host, "role": "primary", "polled": polled, "session": session,
            "searches": list(parts)}


def part(search_id, *records, ok=True, scope="scope-1", error=""):
    return {"search": search_id, "scope": scope, "ok": ok, "error": error,
            "listings": list(records)}


def rec(item_id, title="2018 Honda civic si coupe 2d", **extra):
    out = {"id": item_id, "title": title, "price": 25000, "city": "Ottawa",
           "province": "ON", "mileage_km": 45000}
    out.update(extra)
    return out


class TestSealingABatch:

    def test_round_trip(self):
        b = batch()
        assert M.open_batch(KEY, M.seal_batch(KEY, b)) == b

    def test_padded_so_its_size_says_little(self):
        small = M.seal_batch(KEY, batch())
        bigger = M.seal_batch(KEY, batch(part("s", rec("100000001"))))
        assert len(small) == len(bigger)

    def test_another_key_opens_nothing(self):
        with pytest.raises(M.BatchError):
            M.open_batch(bytes(32), M.seal_batch(KEY, batch()))

    def test_a_stale_batch_is_refused(self):
        old = batch(at=clock.now() - timedelta(hours=M.BATCH_MAX_AGE_HOURS + 1))
        with pytest.raises(M.BatchError, match="too long ago"):
            M.open_batch(KEY, M.seal_batch(KEY, old))

    def test_one_from_the_future_is_refused(self):
        with pytest.raises(M.BatchError, match="future"):
            M.open_batch(KEY, M.seal_batch(KEY, batch(at=clock.now() + timedelta(hours=1))))

    def test_tampered_text_is_refused(self):
        text = M.seal_batch(KEY, batch())
        blob = bytearray(base64.b64decode(text))
        blob[-1] ^= 1
        with pytest.raises(M.BatchError):
            M.open_batch(KEY, base64.b64encode(bytes(blob)).decode())
        with pytest.raises(M.BatchError):
            M.open_batch(KEY, "not base64 at all!")

    def test_a_batch_fits_a_dispatch(self):
        many = [rec(str(100000000 + i), photo="https://cdn.example/" + "x" * 300)
                for i in range(150)]
        assert len(M.seal_batch(KEY, batch(part("s", *many)))) < M.PAYLOAD_LIMIT


# =============================================================== ingest

@pytest.fixture
def watch(tmp_path, monkeypatch, cfg):
    monkeypatch.chdir(tmp_path)
    sink = Capture()
    use_channels(monkeypatch, runner_mod, [sink])
    state = State(path=tmp_path / "state.json")
    sid = cfg.searches[0].id

    def send(*parts, **kw):
        b = batch(*parts, **kw)
        M.open_batch(KEY, M.seal_batch(KEY, b), state)
        return M.ingest(cfg, state, b, env={})

    w = type("Watch", (), {})()
    w.cfg, w.state, w.sink, w.sid, w.send = cfg, state, sink, sid, send
    w.sent = lambda: [c for d in sink.digests for c in d]
    return w


class TestTakingABatchIn:

    def test_the_first_batch_is_a_starting_point(self, watch):
        report = watch.send(part(watch.sid, rec("100000001"), rec("100000002")))
        assert report.new == 2 and watch.sent() == []
        entry = watch.state.listings["fb-100000001"]
        assert entry["status"] == "active" and entry["quiet_reason"] == M.STARTING_POINT
        assert entry["search_id"] == watch.sid

    def test_a_new_car_after_that_is_announced(self, watch):
        watch.send(part(watch.sid, rec("100000001")))
        watch.send(part(watch.sid, rec("100000001"), rec("100000002")))
        [change] = watch.sent()
        assert change.kind == Change.NEW and change.listing.id == "fb-100000002"
        assert watch.state.listings["fb-100000002"]["notified_at"]

    def test_a_price_drop_is_judged_like_any_other(self, watch):
        watch.cfg.set("notifications.price_drop_alert_abs", 1000)
        watch.send(part(watch.sid, rec("100000001", price=25000)))
        watch.send(part(watch.sid, rec("100000001", price=23000)))
        [change] = watch.sent()
        assert change.kind == Change.PRICE_DROP and change.delta == -2000

    def test_the_rules_hide_what_they_hide(self, watch):
        watch.send(part(watch.sid, rec("100000001", title="2012 Honda civic lx")))
        entry = watch.state.listings["fb-100000001"]
        assert entry["filtered"] and entry["filter_rule"] == "min_year"

    def test_another_model_is_not_kept_at_all(self, watch):
        watch.send(part(watch.sid, rec("100000001", title="2018 Honda accord ex")))
        assert "fb-100000001" not in watch.state.listings

    def test_sold_on_marketplace_is_gone(self, watch):
        watch.send(part(watch.sid, rec("100000001")))
        report = watch.send(part(watch.sid, rec("100000001", sold=True)))
        entry = watch.state.listings["fb-100000001"]
        assert entry["status"] == "gone" and report.removed == 1
        assert entry["gone_reason"] == "marked sold on Marketplace"
        assert watch.sent() == []           # removals are off by default

    def test_a_sold_car_never_seen_is_not_recorded(self, watch):
        watch.send(part(watch.sid, rec("100000001", sold=True)))
        assert watch.state.listings == {}

    def test_a_pending_sale_is_marked(self, watch):
        watch.send(part(watch.sid, rec("100000001", pending=True)))
        assert watch.state.listings["fb-100000001"]["sale_pending"] is True
        watch.send(part(watch.sid, rec("100000001")))
        assert "sale_pending" not in watch.state.listings["fb-100000001"]

    def test_long_unseen_is_gone(self, watch):
        watch.send(part(watch.sid, rec("100000001")))
        clock.freeze(clock.now() + timedelta(days=M.GONE_AFTER_DAYS + 1))
        try:
            watch.send(part(watch.sid, rec("100000002")))
        finally:
            clock.freeze(None)
        entry = watch.state.listings["fb-100000001"]
        assert entry["status"] == "gone"
        assert entry["gone_reason"].startswith("not seen on Marketplace for")

    def test_a_search_that_failed_removes_nothing(self, watch):
        watch.send(part(watch.sid, rec("100000001")))
        clock.freeze(clock.now() + timedelta(days=M.GONE_AFTER_DAYS + 1))
        try:
            report = watch.send(part(watch.sid, ok=False, error="timed out"))
        finally:
            clock.freeze(None)
        assert watch.state.listings["fb-100000001"]["status"] == "active"
        assert report.searches_failed == 1 and "timed out" in report.warnings[0]

    def test_a_check_in_without_a_read_changes_no_car(self, watch):
        watch.send(part(watch.sid, rec("100000001")))
        watch.send(polled=False)
        last = watch.state.data["marketplace"]["last_batch"]
        assert last["polled"] is False and last["host"] == "collector-a"
        assert watch.state.listings["fb-100000001"]["status"] == "active"

    def test_the_same_batch_twice_is_refused(self, watch):
        b = batch(part(watch.sid, rec("100000001")))
        M.ingest(watch.cfg, watch.state, b, env={})
        with pytest.raises(M.AlreadyTakenIn):
            M.open_batch(KEY, M.seal_batch(KEY, b), watch.state)

    def test_a_deleted_search_is_ignored(self, watch):
        watch.send(part("no-such-search", rec("100000001")))
        assert watch.state.listings == {}

    def test_muted_cars_stay_quiet(self, watch):
        watch.send(part(watch.sid, rec("100000001", price=30000)))
        watch.state.listings["fb-100000001"]["you"] = {"muted": True}
        watch.send(part(watch.sid, rec("100000001", price=20000)))
        assert watch.sent() == []


class TestAutoTraderLeavesMarketplaceCarsAlone:

    def test_absence_from_an_autotrader_read_is_not_a_sale(self, watch):
        watch.send(part(watch.sid, rec("100000001")))
        for _ in range(5):
            clock.freeze(clock.now() + timedelta(hours=3))
            try:
                assert watch.state.mark_missing(watch.sid, set()) == []
            finally:
                clock.freeze(None)
        assert watch.state.listings["fb-100000001"]["status"] == "active"


class TestSignedOut:

    def test_said_once_and_again_when_it_is_back(self, watch):
        watch.send(session="signed_out")
        watch.send(session="signed_out")
        assert len(watch.sink.alerts_matching("sign in again")) == 1
        watch.send(part(watch.sid, rec("100000001")))
        assert len(watch.sink.alerts_matching("watched again")) == 1

    def test_it_is_a_warning_not_a_failed_run(self, watch):
        report = watch.send(session="signed_out")
        assert report.ok and any("signed the collector out" in w for w in report.warnings)


# ======================================================== the watchdog

class TestTheWatchdog:

    def test_silent_until_a_collector_has_ever_reported(self, watch):
        assert events.marketplace_silence(watch.cfg, watch.state, {}) is None

    def test_a_collector_gone_quiet_is_said_once(self, watch):
        watch.send(polled=False)
        later = clock.now() + timedelta(hours=3)
        said = events.marketplace_silence(watch.cfg, watch.state, {}, now=later)
        assert said and said["subject"] == "Marketplace collector has gone quiet"
        record = {"marketplace_reported": said["key"]}
        assert events.marketplace_silence(watch.cfg, watch.state, record, now=later) is None

    def test_checking_in_but_not_reading_is_its_own_alarm(self, watch):
        watch.send(part(watch.sid, rec("100000001")))
        clock.freeze(clock.now() + timedelta(hours=7))
        try:
            watch.send(part(watch.sid, ok=False, error="the page did not load"))
            said = events.marketplace_silence(watch.cfg, watch.state, {})
        finally:
            clock.freeze(None)
        assert said["subject"] == "Marketplace is not being read"
        assert "the page did not load" in said["body"]


# ======================================================== what is shown

class TestShown:

    def test_the_page_knows_where_each_car_is_listed(self, watch):
        watch.send(part(watch.sid, rec("100000001", pending=True)))
        payload = build_payload(watch.cfg, watch.state, {})
        [row] = payload["listings"]
        assert row["site"] == "marketplace" and row["sale_pending"] is True
        mp = payload["marketplace"]
        assert mp["last_batch"]["host"] == "collector-a" and mp["cars"] == 1
        assert mp["searches"][0]["name"] == "Example search"

    def test_nothing_about_marketplace_until_it_is_used(self, cfg):
        assert build_payload(cfg, State(path=Path("unused.json")), {})["marketplace"] is None

    def test_an_alert_says_marketplace(self):
        car = Listing(id="fb-100000001", title="2018 Honda Civic", price=25000,
                      location="Ottawa")
        assert headline([Change(Change.NEW, car)]).startswith("Marketplace: ")
        assert push_title([Change(Change.NEW, car)]).startswith("New on Marketplace: ")
        other = Listing(id="00000000-0000-4000-8000-000000000001",
                        title="2018 Honda Civic", price=25000)
        assert headline([Change(Change.NEW, car), Change(Change.NEW, other)]
                        ).startswith("AutoTrader: ")


# =========================================================== the command

class TestTheCommand:

    @pytest.fixture
    def repo(self, tmp_path, monkeypatch, cfg):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(V.ENV_KEY, "a long enough test passphrase")
        vault = V.Vault.unlock(tmp_path, create=True)
        State(path=tmp_path / "state.json").save()
        return type("Repo", (), {"key": vault.key, "path": tmp_path, "cfg": cfg})()

    def run(self, repo, *args):
        from autotrader.cli import main
        return main(["--config", str(repo.path / "config.json"),
                     "--state", str(repo.path / "state.json"), *args])

    def test_takes_in_a_batch_from_a_file(self, repo, capsys):
        b = batch(part(repo.cfg.searches[0].id, rec("100000001")))
        (repo.path / "b.txt").write_text(M.seal_batch(repo.key, b))
        assert self.run(repo, "marketplace", "ingest", "b.txt", "--no-notify") == 0
        state = State.load(repo.path / "state.json")
        assert "fb-100000001" in state.listings
        assert b["id"] in state.data["marketplace"]["batch_ids"]
        # Twice is harmless: a collector may resend what already arrived.
        assert self.run(repo, "marketplace", "ingest", "b.txt", "--no-notify") == 0
        assert "already taken in" in capsys.readouterr().out

    def test_takes_it_from_the_dispatch_event(self, repo, monkeypatch):
        b = batch(part(repo.cfg.searches[0].id, rec("100000001")))
        event = repo.path / "event.json"
        event.write_text(json.dumps({"action": "marketplace",
                                     "client_payload": {"batch": M.seal_batch(repo.key, b)}}))
        monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
        assert self.run(repo, "marketplace", "ingest", "--no-notify") == 0
        assert "fb-100000001" in State.load(repo.path / "state.json").listings

    def test_a_batch_that_does_not_open_is_refused_and_said(self, repo):
        (repo.path / "b.txt").write_text(M.seal_batch(bytes(32), batch()))
        assert self.run(repo, "marketplace", "ingest", "b.txt") == 1
        refused = State.load(repo.path / "state.json").data["marketplace"]["refused"]
        assert "does not open" in refused["why"]

    def test_no_batch_is_nothing_to_do(self, repo, monkeypatch):
        monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
        assert self.run(repo, "marketplace", "ingest") == 0


class TestTheWorkflow:

    def test_the_check_takes_batches_in_without_pasting_them_into_a_script(self):
        text = (Path(__file__).resolve().parent.parent
                / ".github" / "workflows" / "watch.yml").read_text()
        assert "types: [check, marketplace]" in text
        assert "marketplace ingest" in text
        assert "client_payload" not in text


class TestAStandbyTakingOver:

    def test_said_once_while_it_lasts(self, watch):
        b = batch(part(watch.sid, rec("100000001")), host="collector-b")
        b["role"] = "standby"
        M.ingest(watch.cfg, watch.state, b, env={})
        b2 = dict(b, id=secrets.token_hex(16))
        M.ingest(watch.cfg, watch.state, b2, env={})
        assert len(watch.sink.alerts_matching("standby computer has taken over")) == 1
        watch.send(part(watch.sid, rec("100000001")))       # the primary is back
        assert "standby_told" not in watch.state.data["marketplace"]


class TestAnOldCarDriftingIntoView:

    def test_is_recorded_but_not_announced_as_new(self, watch):
        watch.send(part(watch.sid, rec("100000001")))
        long_ago = int((clock.now() - timedelta(days=30)).timestamp())
        just_now = int(clock.now().timestamp())
        watch.send(part(watch.sid, rec("100000001"),
                        rec("100000002", created=long_ago),
                        rec("100000003", created=just_now)))
        assert [c.listing.id for c in watch.sent()] == ["fb-100000003"]
        quiet = watch.state.listings["fb-100000002"]["quiet_reason"]
        assert quiet.startswith("listed on Marketplace 30 days ago")


class TestPageOrder:

    def test_results_come_out_in_the_order_the_page_lists_them(self):
        ids = [str(100000010 + i) for i in range(8)]
        found = M.collect([search_page(*(node(i, "2018 Honda civic") for i in ids))])
        assert list(found) == ids
