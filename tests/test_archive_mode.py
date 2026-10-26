"""Archive-only searches, the permanent record, and listings saved by link.

An "archive" search records every car, price and photo it finds and says
nothing about any of it. "Silent really means silent": a new car, a drop, a
rise, a relisting, a removal and an alert held from before the switch all
end in a quiet reason that names the change, never in a notification. The
service alarms are separate and still go out.
"""
from __future__ import annotations

import json

import pytest

from autotrader import clock, control, insight, invariants
from autotrader import marketplace as M
from autotrader import runner as runner_mod
from autotrader.config import PINNED_SEARCH_ID, Config
from autotrader.dashboard import build_payload
from autotrader.http import FetchError
from autotrader.runner import run
from autotrader.state import ARCHIVE_ONLY_PREFIX, Change, State, archived_quietly

from .helpers import Capture, FakeFetcher, use_channels

SEARCH = "https://www.autotrader.ca/cars/honda/civic/?rcp=15&srt=35&prx=-2&loc=K1P"


def archive_only(cfg: Config, on: bool = True) -> None:
    cfg.data["searches"][0]["notify"] = "archive" if on else "alert"
    cfg.save()


def cheaper_pages(archive_html):
    pages = {i: archive_html(i) for i in ("13166607", "13221555")}
    pages["68819631"] = (archive_html("68819631")
                         .replace('"price":"102199"', '"price":"94000"')
                         .replace('"price": "102199"', '"price": "94000"'))
    return pages


class TestSilentReallyMeansSilent:

    def test_a_new_car_is_recorded_and_not_announced(self, bench):
        archive_only(bench.cfg)
        report = bench.run()
        assert report.new == 3 and report.archived == 3
        assert bench.sink.digests == [], "an archive-only search sent a digest"
        state = State.load(bench.path / "state.json")
        assert len(state.listings) == 3
        for entry in state.listings.values():
            assert archived_quietly(entry), entry.get("quiet_reason")
            assert "New listing" in entry["quiet_reason"]
            assert not entry.get("pending") and not entry.get("notified_at")

    def test_a_price_drop_is_recorded_and_not_announced(self, bench, archive_html):
        archive_only(bench.cfg)
        bench.run()
        report = run(bench.cfg, State.load(bench.path / "state.json"),
                     fetcher=FakeFetcher(bench.cards.replace("$102,199", "$94,000"),
                                         cheaper_pages(archive_html)))
        assert report.price_drops == 1, "the drop still happened and is counted"
        assert bench.sink.digests == []
        entry = State.load(bench.path / "state.json").listings["68819631"]
        assert [p["price"] for p in entry["price_history"]] == [102199, 94000]
        assert entry["quiet_reason"].startswith(ARCHIVE_ONLY_PREFIX)
        assert "Price down" in entry["quiet_reason"]
        assert not entry.get("pending") and not entry.get("ride_along")

    def test_a_small_drop_does_not_wait_to_ride_along(self, bench, archive_html):
        archive_only(bench.cfg)
        bench.cfg.set("notifications.price_drop_alert_abs", 50_000)
        bench.cfg.save()
        bench.run()
        run(bench.cfg, State.load(bench.path / "state.json"),
            fetcher=FakeFetcher(bench.cards.replace("$102,199", "$94,000"),
                                cheaper_pages(archive_html)))
        entry = State.load(bench.path / "state.json").listings["68819631"]
        assert not entry.get("ride_along"), "a rider would go out with the next new car"
        assert archived_quietly(entry)

    def test_a_removal_is_recorded_and_not_announced(self, bench, fixture_html):
        archive_only(bench.cfg)
        bench.cfg.set("notifications.notify_on.removed", True)
        bench.cfg.save()
        bench.run()
        bench.run(search_html=fixture_html("search_single"))
        report = bench.run(search_html=fixture_html("search_single"))
        assert report.removed >= 1
        assert bench.sink.digests == []
        state = State.load(bench.path / "state.json")
        gone = [e for e in state.listings.values() if e["status"] == "gone"]
        assert gone, "nothing was marked gone"
        for entry in gone:
            assert not entry.get("pending")
            assert entry["revisions"][-1]["field"] == "status"
            assert entry["revisions"][-1]["to"] == "gone"

    def test_an_alert_held_from_before_the_switch_is_dropped(self, bench, monkeypatch):
        """Pending work is re-checked against the mode just before delivery."""
        bench.run()
        bench.sink.digests.clear()
        # Hold the next run's alerts: quiet hours.
        bench.cfg.set("notifications.quiet_hours",
                      {"enabled": True, "start": "00:00", "end": "23:59"})
        bench.cfg.save()
        bench.run(search_html=bench.cards.replace("$102,199", "$94,000")
                  .replace("19_13166607_", "19_13166699_"))
        state = State.load(bench.path / "state.json")
        owed = [lid for lid, e in state.listings.items() if e.get("pending")]
        assert owed, "the test needs an alert waiting"
        # Then switch the search to archive only and lift the quiet hours.
        archive_only(bench.cfg)
        bench.cfg.set("notifications.quiet_hours", {"enabled": False})
        bench.cfg.save()
        report = bench.run(search_html=bench.cards.replace("$102,199", "$94,000")
                           .replace("19_13166607_", "19_13166699_"))
        assert bench.sink.digests == []
        assert report.archived >= 1
        state = State.load(bench.path / "state.json")
        for lid in owed:
            assert not state.listings[lid].get("pending")
            assert archived_quietly(state.listings[lid])

    def test_the_bookkeeping_still_holds(self, bench):
        archive_only(bench.cfg)
        report = bench.run()
        assert not report.invariants
        state = State.load(bench.path / "state.json")
        assert invariants.check(bench.cfg, state, report) == []

    def test_an_alerting_search_beside_it_still_alerts(self, bench, fixture_html):
        """Two searches, one silent: the other's cars go out as before."""
        bench.cfg.add_search("https://www.autotrader.ca/cars/bmw/m5/?rcp=15", "Loud")
        bench.cfg.data["searches"][0]["notify"] = "archive"
        bench.cfg.save()
        report = bench.run()
        # Both searches read the same fake page; the first (archive-only) one
        # claims the cars, so the loud one finds nothing new to announce. Then
        # swap: the loud one goes first.
        assert bench.sink.digests == []
        bench.cfg.data["searches"].reverse()
        bench.cfg.save()
        bench.sink.digests.clear()
        report = bench.run(search_html=bench.cards.replace("19_13166607_", "19_13166699_"))
        assert report.new == 1
        assert len(bench.sink.digests) == 1, "the loud search's new car was not sent"

    def test_switching_back_to_alerts_does_not_replay_the_backlog(self, bench):
        archive_only(bench.cfg)
        bench.run()
        archive_only(bench.cfg, on=False)
        report = bench.run()
        assert report.new == 0 and bench.sink.digests == []
        state = State.load(bench.path / "state.json")
        for entry in state.listings.values():
            assert archived_quietly(entry), "the old quiet reasons stand"

    def test_the_run_summary_says_what_it_archived(self, bench):
        archive_only(bench.cfg)
        report = bench.run()
        assert "3 archived quietly" in report.summary()
        assert report.to_dict()["archived"] == 3


class TestTheWeeklyDigestLeavesSilentSearchesOut:

    def test_archive_only_cars_are_not_the_weeks_news(self, bench, monkeypatch, capsys):
        from autotrader import cli
        archive_only(bench.cfg)
        bench.run()
        clock.freeze(clock.now())
        # Pretend the cars arrived this week: a starting point is left out of
        # the digest already, so give them an announced look.
        state = State.load(bench.path / "state.json")
        for entry in state.listings.values():
            entry.pop("quiet_reason", None)
            entry["notified_at"] = entry["first_seen"]
        state.save()
        assert cli.main(["--no-colour", "--config", str(bench.path / "config.json"),
                         "--state", str(bench.path / "state.json"), "weekly"]) == 0
        out = capsys.readouterr().out
        assert "3 new" not in out, out
        archive_only(bench.cfg, on=False)
        assert cli.main(["--no-colour", "--config", str(bench.path / "config.json"),
                         "--state", str(bench.path / "state.json"), "weekly"]) == 0
        assert "3 new" in capsys.readouterr().out


class TestThePermanentRecord:

    def test_more_than_forty_price_changes_survive(self, tmp_path):
        from autotrader.listing import Listing
        state = State(path=tmp_path / "state.json")
        for n in range(60):
            state.record(Listing(id="abcdef123456", url="u", title="car",
                                 price=50_000 - n * 100, price_source="search"))
        state.save()
        history = State.load(tmp_path / "state.json").listings["abcdef123456"]["price_history"]
        assert len(history) == 60
        assert history[0]["price"] == 50_000 and history[-1]["price"] == 44_100

    def test_prune_keeps_everything_unless_told(self, tmp_path):
        from autotrader.listing import Listing
        state = State(path=tmp_path / "state.json")
        for n in range(30):
            state.record(Listing(id=f"abcdef{n:06d}", url="u", title="car", price=1000))
            state.mark_gone(f"abcdef{n:06d}", "sold")
        for entry in state.listings.values():
            entry["removed_at"] = "2000-01-01T00:00:00+00:00"
        assert state.prune() == 0
        assert len(state.listings) == 30
        assert state.prune(keep_days=30, protect={"abcdef000001"}) == 29
        assert set(state.listings) == {"abcdef000001"}

    def test_a_marked_or_pinned_car_is_never_pruned(self, tmp_path):
        from autotrader.listing import Listing
        state = State(path=tmp_path / "state.json")
        for lid in ("abcdef000001", "abcdef000002", "abcdef000003"):
            state.record(Listing(id=lid, url="u", title="car", price=1000))
            state.mark_gone(lid, "sold")
            state.listings[lid]["removed_at"] = "2000-01-01T00:00:00+00:00"
        state.listings["abcdef000001"]["you"] = {"shortlisted": True}
        state.listings["abcdef000002"]["pinned"] = True
        assert state.prune(keep_days=1) == 1
        assert set(state.listings) == {"abcdef000001", "abcdef000002"}

    def test_the_sellers_edits_are_written_down(self, tmp_path):
        from autotrader.listing import Listing
        state = State(path=tmp_path / "state.json")
        state.record(Listing(id="abcdef123456", url="u", title="2020 Car", price=1000,
                             mileage_km=50_000))
        state.record(Listing(id="abcdef123456", url="u", title="2020 Car M Sport",
                             price=1000, mileage_km=50_500))
        # A page that did not carry the mileage is not a seller clearing it.
        state.record(Listing(id="abcdef123456", url="u", title="2020 Car M Sport",
                             price=1000, mileage_km=None))
        revisions = state.listings["abcdef123456"]["revisions"]
        assert [(r["field"], r["from"], r["to"]) for r in revisions] == [
            ("mileage_km", 50_000, 50_500), ("title", "2020 Car", "2020 Car M Sport")]
        assert all(r["at"] for r in revisions)

    def test_the_whole_history_is_published(self, bench, archive_html):
        bench.run()
        state = State.load(bench.path / "state.json")
        entry = state.listings["68819631"]
        entry["price_history"] = [{"at": f"2026-01-{d:02d}T00:00:00+00:00",
                                   "price": 100_000 - d} for d in range(1, 31)]
        entry["price"] = entry["price_history"][-1]["price"]
        state.save()
        payload = build_payload(bench.cfg, state, {})
        car = next(l for l in payload["listings"] if l["id"] == "68819631")
        assert len(car["price_history"]) == 30
        assert car["photos_saved"] == 0 and car["archive_only"] is False

    def test_the_page_knows_which_search_only_archives(self, bench):
        archive_only(bench.cfg)
        bench.run()
        payload = build_payload(bench.cfg, State.load(bench.path / "state.json"), {})
        assert payload["searches"][0]["notify"] == "archive"
        assert all(l["archive_only"] for l in payload["listings"])
        assert "options" in payload["bounds"]
        assert payload["bounds"]["options"]["dashboard.photos_per_car"] == ["int", 1, 24]


class TestAnOlderConfigIsBroughtUpToDate:

    def test_version_two_retention_becomes_permanent(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({
            "version": 2, "searches": [{"url": SEARCH}],
            "archive": {"mode": "metadata", "images": 0, "keep_last": 400, "keep_days": 730},
            "dashboard": {"enabled": True, "max_listings": 500}}))
        cfg = Config.load(path)
        assert cfg.data["version"] == 3
        assert cfg.get("archive.keep_last") == 0 and cfg.get("archive.keep_days") == 0
        assert cfg.get("dashboard.max_listings") == 2000
        assert cfg.get("dashboard.photos_per_car") == 12

    def test_a_limit_the_owner_chose_is_kept(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({
            "version": 2, "searches": [{"url": SEARCH}],
            "archive": {"keep_last": 50, "keep_days": 90},
            "dashboard": {"max_listings": 3000}}))
        cfg = Config.load(path)
        assert cfg.get("archive.keep_last") == 50 and cfg.get("archive.keep_days") == 90
        assert cfg.get("dashboard.max_listings") == 3000

    def test_an_unknown_mode_reads_as_alert(self):
        cfg = Config.from_data({"searches": [{"url": SEARCH, "notify": "shout"}]})
        assert cfg.searches[0].notify == "alert"
        assert cfg.searches[0].to_dict().get("notify") is None


# ---------------------------------------------------------------- pinned

PIN = "https://www.autotrader.ca/a/bmw/m5/winnipeg/manitoba/19_13166607_/?ursrc=hp"


class Gone(FakeFetcher):
    """A fetcher whose listing pages answer 404."""

    def get(self, url, referer=None, allow_block=False):
        if "/a/" in url:
            self.urls.append(url)
            raise FetchError(f"HTTP 404 from {url}", status=404)
        return super().get(url, referer, allow_block)


@pytest.fixture
def quiet_bench(bench, fixture_html):
    """A watch whose search finds nothing, so a pinned car is the bot's own."""
    bench.empty = fixture_html("search_empty")
    return bench


class TestAListingSavedByItsLink:

    def test_it_is_read_recorded_and_kept_quiet(self, quiet_bench):
        b = quiet_bench
        b.cfg.pin_listing(PIN, "the Winnipeg M5")
        b.cfg.save()
        report = b.run(search_html=b.empty)
        assert report.pinned_checked == 1 and report.pinned_recorded == 1
        assert b.sink.digests == []
        state = State.load(b.path / "state.json")
        entry = state.listings["13166607"]
        assert entry["search_id"] == PINNED_SEARCH_ID
        assert entry["pinned"] is True and entry["price"] == 109999
        assert entry["mileage_km"] == 52000 and len(entry["images"]) == 4
        assert archived_quietly(entry)
        assert invariants.check(b.cfg, state, report) == []

    def test_its_price_moves_are_kept_and_not_announced_by_default(self, quiet_bench, archive_html):
        b = quiet_bench
        b.cfg.pin_listing(PIN)
        b.cfg.save()
        b.run(search_html=b.empty)
        cheaper = {"13166607": archive_html("13166607")
                   .replace('"price":"109999"', '"price":"99999"')
                   .replace('"price": "109999"', '"price": "99999"')}
        report = run(b.cfg, State.load(b.path / "state.json"),
                     fetcher=FakeFetcher(b.empty, cheaper))
        assert report.archived == 1 and b.sink.digests == []
        entry = State.load(b.path / "state.json").listings["13166607"]
        assert [p["price"] for p in entry["price_history"]] == [109999, 99999]

    def test_a_pin_set_to_alert_announces_a_drop(self, quiet_bench, archive_html):
        b = quiet_bench
        b.cfg.pin_listing(PIN, notify="alert")
        b.cfg.save()
        b.run(search_html=b.empty)
        assert b.sink.digests == [], "the first read is a starting point"
        cheaper = {"13166607": archive_html("13166607")
                   .replace('"price":"109999"', '"price":"99999"')
                   .replace('"price": "109999"', '"price": "99999"')}
        report = run(b.cfg, State.load(b.path / "state.json"),
                     fetcher=FakeFetcher(b.empty, cheaper))
        assert report.price_drops == 1
        [[change]] = b.sink.digests
        assert change.kind == Change.PRICE_DROP and change.new_price == 99999

    def test_a_car_a_search_found_stays_that_searchs(self, bench):
        bench.cfg.pin_listing(PIN)
        bench.cfg.save()
        report = bench.run()
        state = State.load(bench.path / "state.json")
        entry = state.listings["13166607"]
        assert entry["search_id"] != PINNED_SEARCH_ID
        assert entry["pinned"] is True
        assert report.pinned_checked == 0, "its search read it; no page was spent"
        assert invariants.check(bench.cfg, state, report) == []

    def test_a_page_that_says_gone_is_believed_on_the_second_check(self, quiet_bench):
        b = quiet_bench
        b.cfg.pin_listing(PIN)
        b.cfg.save()
        b.run(search_html=b.empty)

        def go():
            return run(b.cfg, State.load(b.path / "state.json"), fetcher=Gone(b.empty))
        go()
        assert State.load(b.path / "state.json").listings["13166607"]["status"] == "active"
        report = go()
        entry = State.load(b.path / "state.json").listings["13166607"]
        assert entry["status"] == "gone" and report.removed == 1
        assert b.sink.digests == []
        assert invariants.check(b.cfg, State.load(b.path / "state.json"), report) == []

    def test_unpinning_keeps_the_record(self, quiet_bench):
        from autotrader.pinned import UNPINNED
        from autotrader.state import retired_by_owner
        b = quiet_bench
        b.cfg.pin_listing(PIN)
        b.cfg.save()
        b.run(search_html=b.empty)
        b.cfg.unpin_listing("13166607")
        b.cfg.save()
        report = b.run(search_html=b.empty)
        state = State.load(b.path / "state.json")
        entry = state.listings["13166607"]
        assert entry["status"] == "gone" and entry["gone_reason"] == UNPINNED
        assert not entry.get("pinned")
        assert retired_by_owner(entry), "it left the watch, not the market"
        assert invariants.check(b.cfg, state, report) == []

    def test_a_link_that_cannot_be_read_yet_still_has_a_record(self, quiet_bench):
        b = quiet_bench
        b.cfg.pin_listing("https://www.autotrader.ca/a/bmw/m5/x/y/19_99999999_/")
        b.cfg.save()
        report = b.run(search_html=b.empty)
        state = State.load(b.path / "state.json")
        entry = state.listings["99999999"]
        assert entry["pinned"] and entry["status"] == "active"
        assert "could not be read yet" in entry["quiet_reason"]
        assert invariants.check(b.cfg, state, report) == []

    def test_a_marketplace_link_waits_for_the_mac(self, quiet_bench):
        from autotrader.state import AWAITING_MAC
        b = quiet_bench
        b.cfg.pin_listing("https://www.facebook.com/marketplace/item/123456789012/?ref=x")
        b.cfg.save()
        report = b.run(search_html=b.empty)
        state = State.load(b.path / "state.json")
        entry = state.listings["fb-123456789012"]
        assert entry["awaiting_mac"] is True and entry["quiet_reason"] == AWAITING_MAC
        assert report.pinned_checked == 0, "GitHub cannot read Facebook"
        payload = build_payload(b.cfg, state, {})
        [pin] = payload["pinned"]
        assert pin["site"] == "marketplace" and pin["awaiting_mac"] is True
        assert pin["known"] is False
        assert invariants.check(b.cfg, state, report) == []

    def test_only_so_many_pages_are_read_per_check(self, quiet_bench, monkeypatch):
        from autotrader import pinned
        monkeypatch.setattr(pinned, "CHECKS_PER_RUN", 2)
        b = quiet_bench
        for n in range(5):
            b.cfg.pin_listing(f"https://www.autotrader.ca/a/bmw/m5/x/y/19_{13166600 + n}_/")
        b.cfg.save()
        report = b.run(search_html=b.empty)
        assert report.pinned_checked == 2
        report = b.run(search_html=b.empty)
        assert report.pinned_checked == 2
        state = State.load(b.path / "state.json")
        checked = [lid for lid, e in state.listings.items() if e.get("pinned_checked_at")]
        assert len(checked) == 4, "the same two were read twice"

    def test_the_page_lists_what_is_pinned(self, quiet_bench):
        b = quiet_bench
        b.cfg.pin_listing(PIN, "the Winnipeg M5")
        b.cfg.save()
        b.run(search_html=b.empty)
        payload = build_payload(b.cfg, State.load(b.path / "state.json"), {})
        [pin] = payload["pinned"]
        assert pin["name"] == "the Winnipeg M5" and pin["known"] is True
        assert pin["status"] == "active" and pin["found_by"] is None
        car = next(l for l in payload["listings"] if l["id"] == "13166607")
        assert car["pinned"] is True and car["archive_only"] is True


class TestAPinnedMarketplaceCarArrivesInABatch:

    def test_a_batch_that_carries_it_fills_it_in(self, tmp_path, monkeypatch):
        from .test_marketplace import KEY, batch, part, rec
        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.add_search(SEARCH, "Civics")
        cfg.data["searches"][0]["filters"] = {"models": ["Civic"]}
        cfg.set("archive.mode", "off")
        cfg.pin_listing("https://www.facebook.com/marketplace/item/100000009/")
        cfg.save()
        sink = Capture()
        use_channels(monkeypatch, runner_mod, [sink])
        state = State(path=tmp_path / "state.json")
        sid = cfg.searches[0].id
        # A different model: the search's rules would discard it. The pin keeps it.
        b = batch(part(sid, rec("100000001"), rec("100000009", title="2019 Audi RS 5",
                                                   price=61000)))
        M.open_batch(KEY, M.seal_batch(KEY, b), state)
        report = M.ingest(cfg, state, b, env={})
        entry = state.listings["fb-100000009"]
        assert entry["search_id"] == PINNED_SEARCH_ID and entry["pinned"] is True
        assert entry["price"] == 61000 and entry["status"] == "active"
        assert archived_quietly(entry)
        assert sink.digests == []
        assert invariants.check(cfg, state, report) == []


# ---------------------------------------------------------------- control

def cfg_for_control() -> Config:
    return Config({"version": 3, "searches": [
        {"id": "alpha", "name": "Alpha", "enabled": True,
         "url": "https://www.autotrader.ca/cars/honda/civic/"}]})


class TestChangesFromTheDashboard:

    def test_a_search_can_be_made_archive_only_and_back(self):
        cfg, st = cfg_for_control(), State()
        out = control.apply(cfg, st, [{"action": "set-notify", "search": "alpha",
                                       "mode": "archive"}])
        assert out.changed and cfg.searches[0].archive_only
        assert "archive only" in out.applied[0]
        out = control.apply(cfg, st, [{"action": "set-notify", "search": "alpha",
                                       "mode": "alert"}])
        assert not cfg.searches[0].archive_only
        assert "notify" not in cfg.data["searches"][0]

    def test_a_mode_that_is_not_one(self):
        out = control.apply(cfg_for_control(), State(),
                            [{"action": "set-notify", "search": "alpha", "mode": "loud"}])
        assert not out.changed and "alert, archive" in out.rejected[0]

    def test_a_listing_can_be_pinned_and_unpinned(self):
        cfg, st = cfg_for_control(), State()
        out = control.apply(cfg, st, [{"action": "pin-listing", "url": PIN,
                                       "name": "Winnipeg"}])
        assert out.changed and cfg.pinned[0].id == "13166607"
        assert cfg.pinned[0].notify == "archive" and "archive only" in out.applied[0]
        out = control.apply(cfg, st, [{"action": "pin-listing", "url": PIN}])
        assert not out.changed and "already saved" in out.rejected[0]
        out = control.apply(cfg, st, [{"action": "unpin-listing", "listing": "13166607"}])
        assert out.changed and cfg.pinned == []

    def test_a_pin_can_ask_for_alerts(self):
        cfg = cfg_for_control()
        control.apply(cfg, State(), [{"action": "pin-listing", "url": PIN, "notify": "alert"}])
        assert cfg.pinned[0].notify == "alert"

    @pytest.mark.parametrize("url", [
        "http://www.autotrader.ca/a/bmw/m5/x/y/19_13166607_/",
        "https://www.autotrader.ca/cars/bmw/m5/",
        "https://example.com/a/bmw/m5/x/y/19_13166607_/",
        "javascript:alert(1)", "",
    ])
    def test_a_link_that_is_not_a_listing_is_refused(self, url):
        cfg = cfg_for_control()
        out = control.apply(cfg, State(), [{"action": "pin-listing", "url": url}])
        assert not out.changed and cfg.pinned == []

    def test_a_plain_setting_within_its_bounds(self):
        cfg = cfg_for_control()
        out = control.apply(cfg, State(), [
            {"action": "set-option", "option": "dashboard.photos_per_car", "value": 6},
            {"action": "set-option", "option": "notifications.digest", "value": False}])
        assert out.changed
        assert cfg.get("dashboard.photos_per_car") == 6
        assert cfg.get("notifications.digest") is False

    def test_a_setting_outside_its_bounds_or_not_offered(self):
        cfg = cfg_for_control()
        out = control.apply(cfg, State(), [
            {"action": "set-option", "option": "dashboard.photos_per_car", "value": 99}])
        assert not out.changed and "between 1 and 24" in out.rejected[0]
        out = control.apply(cfg, State(), [
            {"action": "set-option", "option": "notifications.channels.ntfy.topic",
             "value": "x"}])
        assert not out.changed and "not a setting I can change" in out.rejected[0]
        out = control.apply(cfg, State(), [
            {"action": "set-option", "option": "notifications.digest", "value": "yes"}])
        assert not out.changed and "true or false" in out.rejected[0]
