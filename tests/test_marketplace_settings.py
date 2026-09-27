"""Marketplace settings the owner can change, and what the collector says of
itself: other spellings of a model, reading a search on Marketplace or not,
the shared Marketplace settings, and the collector's own pace, shown but not
changed from the dashboard."""
from __future__ import annotations

import secrets
from datetime import timedelta

import pytest

from autotrader import clock, control, filters, insight
from autotrader import marketplace as M
from autotrader.config import Config, Search
from autotrader.dashboard import build_payload
from autotrader.listing import Listing
from autotrader.state import State

from .test_marketplace import KEY, SEARCH_URL, batch, part, rec  # noqa: F401


@pytest.fixture
def cfg(tmp_path):
    c = Config.defaults(tmp_path / "config.json")
    c.add_search(SEARCH_URL, "Example search")
    c.data["searches"][0]["filters"] = {"models": ["Civic"], "min_year": 2015,
                                        "near": "Ottawa, ON", "max_distance_km": 300}
    c.save()
    return c


def apply(cfg, state, *items):
    return control.apply(cfg, state, list(items))


class TestOtherSpellings:

    def test_count_as_the_model_on_both_sites(self, cfg):
        f = {"models": ["Civic"], "aliases": ["Civik"]}
        assert filters.check(Listing(id="a", model="Civik"), f).keep
        assert filters.check(Listing(id="a", model="Accord"), f).wrong_car
        # Only alongside a models rule: on their own they are not a rule.
        assert filters.check(Listing(id="a", model="Accord"), {"aliases": ["Civik"]}).keep

    def test_a_marketplace_title_with_one_is_kept(self, cfg):
        cfg.data["searches"][0]["filters"]["aliases"] = ["Civik"]
        [item] = M.plan(cfg)
        judged = M.judge(cfg, cfg.searches[0], [rec("100000001", title="2018 Honda civik ex")], item)
        assert [l.model for l in judged.kept] == ["Civik"]

    def test_adding_one_does_not_restart_the_search(self, cfg):
        before = M.plan(cfg)[0]["scope"]
        cfg.data["searches"][0]["filters"]["aliases"] = ["Civik"]
        assert M.plan(cfg)[0]["scope"] == before

    def test_are_set_from_the_dashboard_per_search(self, cfg, tmp_path):
        st = State(path=tmp_path / "s.json")
        sid = cfg.searches[0].id
        out = apply(cfg, st, {"action": "set-rule", "search": sid, "rule": "aliases",
                              "value": ["Civik", "Civic Si"]})
        assert out.changed and cfg.searches[0].filters["aliases"] == ["Civik", "Civic Si"]
        out = apply(cfg, st, {"action": "set-rule", "search": sid, "rule": "aliases", "value": None})
        assert out.changed and "aliases" not in cfg.searches[0].filters

    @pytest.mark.parametrize("value, why", [
        (["ok", "<script>"], "not a model name"),
        ([f"m{i}" for i in range(13)], "at most"),
    ])
    def test_are_checked(self, cfg, tmp_path, value, why):
        out = apply(cfg, State(path=tmp_path / "s.json"),
                    {"action": "set-rule", "search": cfg.searches[0].id,
                     "rule": "aliases", "value": value})
        assert not out.changed and why in out.rejected[0]

    def test_belong_to_a_search(self, cfg, tmp_path):
        out = apply(cfg, State(path=tmp_path / "s.json"),
                    {"action": "set-rule", "rule": "aliases", "value": ["Civik"]})
        assert not out.changed and "one search" in out.rejected[0]


class TestReadingASearchOnMarketplaceOrNot:

    def test_is_on_unless_switched_off(self, cfg):
        assert cfg.searches[0].marketplace is True
        assert "marketplace" not in cfg.searches[0].to_dict()
        off = Search.from_dict(dict(cfg.searches[0].to_dict(), marketplace=False))
        assert off.marketplace is False and off.to_dict()["marketplace"] is False

    def test_a_switched_off_search_is_not_planned(self, cfg, tmp_path):
        st = State(path=tmp_path / "s.json")
        out = apply(cfg, st, {"action": "set-marketplace", "search": cfg.searches[0].id,
                              "enabled": False})
        assert out.changed and M.plan(cfg) == []
        apply(cfg, st, {"action": "set-marketplace", "search": cfg.searches[0].id,
                        "enabled": True})
        assert len(M.plan(cfg)) == 1 and "marketplace" not in cfg.data["searches"][0]

    def test_its_marketplace_cars_leave_quietly(self, cfg, tmp_path):
        st = State(path=tmp_path / "s.json")
        sid = cfg.searches[0].id
        M.ingest(cfg, st, batch(part(sid, rec("100000001"))), env={}, notify=False)
        apply(cfg, st, {"action": "set-marketplace", "search": sid, "enabled": False})
        M.ingest(cfg, st, batch(), env={}, notify=False)
        entry = st.listings["fb-100000001"]
        assert entry["status"] == "gone"
        assert entry["gone_reason"] == "Marketplace is switched off for this search"

    def test_needs_a_real_search_and_a_yes_or_no(self, cfg, tmp_path):
        st = State(path=tmp_path / "s.json")
        assert not apply(cfg, st, {"action": "set-marketplace", "search": "nope",
                                   "enabled": False}).changed
        assert not apply(cfg, st, {"action": "set-marketplace",
                                   "search": cfg.searches[0].id, "enabled": "no"}).changed


class TestTheSharedSettings:

    @pytest.mark.parametrize("setting, value", [
        ("radius_km", 250), ("new_within_days", 3), ("gone_after_days", 14),
        ("place", "ottawa")])
    def test_can_be_set(self, cfg, tmp_path, setting, value):
        out = apply(cfg, State(path=tmp_path / "s.json"),
                    {"action": "set-marketplace", "setting": setting, "value": value})
        assert out.changed and cfg.get(f"marketplace.{setting}") == value

    def test_the_radius_reaches_the_plan(self, cfg, tmp_path):
        apply(cfg, State(path=tmp_path / "s.json"),
              {"action": "set-marketplace", "setting": "radius_km", "value": 80})
        assert M.plan(cfg)[0]["radius_km"] == 80

    @pytest.mark.parametrize("setting, value, why", [
        ("radius_km", 900, "between"), ("gone_after_days", 1, "between"),
        ("place", "Ottawa, ON", "not a Marketplace place"),
        ("every_minutes", 5, "not a Marketplace setting"),
    ])
    def test_are_checked(self, cfg, tmp_path, setting, value, why):
        out = apply(cfg, State(path=tmp_path / "s.json"),
                    {"action": "set-marketplace", "setting": setting, "value": value})
        assert not out.changed and why in out.rejected[0]

    def test_clearing_one_goes_back_to_the_default(self, cfg, tmp_path):
        st = State(path=tmp_path / "s.json")
        apply(cfg, st, {"action": "set-marketplace", "setting": "radius_km", "value": 80})
        apply(cfg, st, {"action": "set-marketplace", "setting": "radius_km", "value": None})
        assert M.plan(cfg)[0]["radius_km"] == 300        # the search's own distance


class TestWhatTheCollectorSaysOfItself:

    def test_is_kept_bounded_and_published(self, cfg, tmp_path):
        st = State(path=tmp_path / "s.json")
        b = batch(part(cfg.searches[0].id, rec("100000001")))
        b["settings"] = {"every_minutes": 25, "jitter_minutes": 5, "quiet_start": "00:30",
                         "quiet_end": "06:30", "details_per_cycle": 3, "scrolls": 2,
                         "takeover_after_minutes": 75, "rogue": "<b>no</b>"}
        b["next_at"] = (clock.now() + timedelta(minutes=22)).isoformat(timespec="seconds")
        b["last_failure"] = {"at": clock.stamp(), "error": "the page did not load"}
        M.ingest(cfg, st, b, env={}, notify=False)
        last = build_payload(cfg, st, {})["marketplace"]["last_batch"]
        assert last["settings"]["every_minutes"] == 25 and "rogue" not in last["settings"]
        assert last["next_at"] == b["next_at"]
        assert last["last_failure"]["error"] == "the page did not load"

    def test_nonsense_is_dropped(self, cfg, tmp_path):
        st = State(path=tmp_path / "s.json")
        b = batch()
        b["settings"] = {"every_minutes": "lots", "quiet_start": "midnight"}
        b["last_failure"] = "not a dict"
        M.ingest(cfg, st, b, env={}, notify=False)
        heard = st.data["marketplace"]["last_batch"]
        assert heard["settings"] == {} and heard["last_failure"] is None

    def test_the_collector_sends_its_pace_and_next_time(self):
        from collector.cycle import build
        from collector.settings import Settings
        b = build(Settings(host="collector-a"), [], polled=False, session="ok", wait=600)
        assert b["settings"]["every_minutes"] == 25 and b["settings"]["quiet_start"] == "00:30"
        assert clock.parse(b["next_at"]) > clock.now()


class TestANewWatch:

    def test_is_not_measured_over_hours_before_it_existed(self):
        now = clock.now()
        started = (now - timedelta(hours=3)).isoformat(timespec="seconds")
        runs = [{"at": (now - timedelta(minutes=m)).isoformat(timespec="seconds"),
                 "ok": True, "searches_run": 1} for m in (10, 100, 170)]
        cov = insight.coverage(runs, 120, now=now, since_change=None, started=started)
        assert cov["new_install"] and cov["window_hours"] == 3.0
        old = insight.coverage(runs, 120, now=now, since_change=None)
        assert not old["new_install"] and old["window_hours"] == 24.0


class TestExactMatch:

    def test_is_off_until_asked_for_and_changes_the_address(self, cfg, tmp_path):
        assert "exact=false" in M.plan(cfg)[0]["queries"][0]["url"]
        before = M.plan(cfg)[0]["scope"]
        out = apply(cfg, State(path=tmp_path / "s.json"),
                    {"action": "set-marketplace", "setting": "exact", "value": True})
        assert out.changed
        [item] = M.plan(cfg)
        assert "exact=true" in item["queries"][0]["url"]
        # A new way of asking is a new starting point, announced as nothing.
        assert item["scope"] != before

    def test_takes_a_yes_or_no(self, cfg, tmp_path):
        out = apply(cfg, State(path=tmp_path / "s.json"),
                    {"action": "set-marketplace", "setting": "exact", "value": "yes"})
        assert not out.changed
