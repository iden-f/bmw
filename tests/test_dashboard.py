"""The dashboard is published to a public URL, so nothing secret may reach it."""
import json

import pytest

from autotrader.config import Config
from autotrader.dashboard import build_payload, find_secrets, write
from autotrader.listing import Listing
from autotrader.state import State

# Credential-shaped strings for testing the detector. They are assembled at
# runtime rather than written out: a literal Twilio-shaped SID in a source file
# trips GitHub's push protection, which blocks the push outright even though
# the value is invented.
FAKE_CREDENTIALS = {
    "telegram": lambda: "8123456789" + ":" + "AAH2x-" + "a1b2c3" * 5,
    "discord": lambda: "https://discord" + ".com/api/webhooks/123456789/" + "x" * 20,
    "slack": lambda: "https://hooks.slack" + ".com/services/T00000000/B00000/" + "y" * 16,
    "twilio_sid": lambda: "A" + "C" + "0123456789abcdef" * 2,
    "twilio_key": lambda: "S" + "K" + "fedcba9876543210" * 2,
}


def _payload(tmp_path):
    cfg = Config.defaults(tmp_path / "c.json")
    cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=15", "Civic")
    state = State(path=tmp_path / "s.json")
    state.record(Listing(id="1", url="https://www.autotrader.ca/a/x/19_1_/",
                         title="2021 Honda Civic", price=100000,
                         price_source="detail", search_id=cfg.searches[0].id))
    return cfg, state


class TestSecretDetection:
    def test_a_normal_payload_is_clean(self, tmp_path):
        cfg, state = _payload(tmp_path)
        assert find_secrets(build_payload(cfg, state, {})) == []

    def test_secret_names_in_help_text_are_not_flagged(self, tmp_path):
        """data.json says "TELEGRAM_BOT_TOKEN" as the name of a secret to set."""
        cfg, state = _payload(tmp_path)
        payload = build_payload(cfg, state, {})
        assert "TELEGRAM_BOT_TOKEN" in json.dumps(payload)
        assert find_secrets(payload) == []

    @pytest.mark.parametrize("where", ["token", "password", "api_key", "auth"])
    def test_a_credential_under_a_telltale_key_is_caught(self, where):
        assert find_secrets({"config": {where: "some-value"}})

    @pytest.mark.parametrize("shape", list(FAKE_CREDENTIALS))
    def test_a_credential_under_an_innocent_key_is_still_caught(self, shape):
        assert find_secrets({"notes": FAKE_CREDENTIALS[shape]()}), shape

    def test_an_empty_credential_field_is_not_a_leak(self):
        assert find_secrets({"token": "", "password": None}) == []

    def test_ordinary_listing_text_is_not_flagged(self):
        assert find_secrets({"title": "2021 Honda Civic - ask about the token"}) == []
        assert find_secrets({"help": "open bot<TOKEN>/getUpdates"}) == []

    def test_the_writer_refuses_to_publish_a_leak(self, tmp_path):
        cfg, state = _payload(tmp_path)
        cfg.set("notifications.channels.webhook.url", FAKE_CREDENTIALS["discord"]())
        with pytest.raises(ValueError, match="credential-shaped"):
            write(cfg, state, {}, tmp_path / "docs" / "data.json")

    def test_a_clean_payload_writes_normally(self, tmp_path):
        cfg, state = _payload(tmp_path)
        path = write(cfg, state, {}, tmp_path / "docs" / "data.json")
        assert json.loads(path.read_text())["listings"]


class TestPayloadShape:
    def test_channels_report_presence_never_values(self, tmp_path):
        cfg, state = _payload(tmp_path)
        payload = build_payload(cfg, state, {"TELEGRAM_BOT_TOKEN": "supersecret",
                                             "TELEGRAM_CHAT_ID": "123"})
        assert "supersecret" not in json.dumps(payload)
        assert payload["channels"]["telegram"]["active"] is True

    def test_the_listing_cap_is_respected(self, tmp_path):
        cfg, state = _payload(tmp_path)
        cfg.set("dashboard.max_listings", 2)
        for i in range(10):
            state.record(Listing(id=str(100 + i), url=f"https://x/a/19_{100+i}_/",
                                 title=f"Car {i}", price=1000 * i,
                                 price_source="detail", search_id="s"))
        assert len(build_payload(cfg, state, {})["listings"]) == 2


class TestTheBotsOwnState:
    """The dashboard showed cars and nothing else.

    Every question about whether the watcher was still working - is a parser
    failing? did a channel die? why does it show fewer cars than the site? -
    had to be answered by reading state.json by hand.
    """

    def _bench(self, tmp_path):
        cfg = Config.defaults(tmp_path / "c.json")
        cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=15", "Civic")
        sid = cfg.searches[0].id
        state = State(path=tmp_path / "s.json")
        state.record(Listing(id="keep", url="https://www.autotrader.ca/offers/a-" + "0" * 8
                             + "-1111-2222-3333-444444444444",
                             title="2021 Honda Civic", price=100000, search_id=sid))
        state.record(Listing(id="ask", url="https://www.autotrader.ca/offers/b-" + "1" * 8
                             + "-1111-2222-3333-444444444444",
                             title="2022 Honda Civic", price=None, search_id=sid))
        state.record(Listing(id="nope", url="https://www.autotrader.ca/offers/c-" + "2" * 8
                             + "-1111-2222-3333-444444444444",
                             title="2023 Honda Civic", price=400000, search_id=sid),
                     filtered=True, filter_reason="price $400,000 above maximum $150,000")
        return cfg, state, sid

    def test_hidden_cars_are_published_with_the_reason_they_were_hidden(self, tmp_path):
        cfg, state, _ = self._bench(tmp_path)
        payload = build_payload(cfg, state, env={})

        hidden = [l for l in payload["listings"] if l["filtered"]]
        assert [l["id"] for l in hidden] == ["nope"]
        assert "above maximum" in hidden[0]["filter_reason"]

    def test_a_hidden_car_carries_no_photos_or_history(self, tmp_path):
        """It has to be countable and explainable, not browsable."""
        cfg, state, _ = self._bench(tmp_path)
        hidden = next(l for l in build_payload(cfg, state, env={})["listings"]
                      if l["filtered"])
        assert "images" not in hidden
        assert len(hidden["price_history"]) <= 2

    def test_counts_separate_live_hidden_and_call_for_price(self, tmp_path):
        cfg, state, sid = self._bench(tmp_path)
        payload = build_payload(cfg, state, env={})

        assert payload["health"]["counts"] == {
            "active": 2, "filtered": 1, "unpriced": 1, "gone": 0, "total": 3}
        assert payload["searches"][0]["counts"]["active"] == 2
        assert payload["searches"][0]["counts"]["filtered"] == 1

    def test_the_whole_parser_ladder_is_reported_not_just_the_winner(self, tmp_path):
        """Two of four scoring is the warning that matters, and it is only
        visible if the ones scoring zero are named."""
        cfg, state, sid = self._bench(tmp_path)
        state.record_shape(sid, {
            "strategy": "jsonld",
            "scores": {"jsonld": 20, "embedded_json": 20, "anchors": 0, "regex": 0},
            "working": ["embedded_json", "jsonld"],
            "markers": {"offer_links": "many"},
        })
        ladder = build_payload(cfg, state, env={})["health"]["strategies"][sid]

        assert ladder["winner"] == "jsonld"
        assert ladder["order"] == ["jsonld", "embedded_json", "anchors", "regex"]
        assert ladder["of"] == 4
        assert ladder["scores"]["anchors"] == 0

    def test_a_run_that_drifted_says_so(self, tmp_path):
        cfg, state, _ = self._bench(tmp_path)
        state.record_run({"ok": True, "shape_drift": [
            {"search": "Civic", "reasons": ["the winning strategy changed"], "serious": True}]})
        health = build_payload(cfg, state, env={})["health"]

        assert health["drift"][0]["serious"] is True
        assert health["ok_streak"] == 1

    def test_a_broken_streak_is_reported_honestly(self, tmp_path):
        cfg, state, _ = self._bench(tmp_path)
        state.record_run({"ok": True})
        state.record_run({"ok": False})
        state.record_run({"ok": True})     # most recent
        assert build_payload(cfg, state, env={})["health"]["ok_streak"] == 1

    def test_hidden_cars_do_not_crowd_live_ones_out_of_the_cap(self, tmp_path):
        cfg, state, sid = self._bench(tmp_path)
        cfg.set("dashboard.max_listings", 2)
        for n in range(20):
            state.record(Listing(id=f"junk{n}", url=f"https://www.autotrader.ca/a/x/19_{n}_/",
                                 title="2019 Honda Civic", price=999999, search_id=sid),
                         filtered=True, filter_reason="too dear")

        published = build_payload(cfg, state, env={})["listings"]
        assert len(published) == 2
        assert all(not l["filtered"] for l in published)


class TestTheCallersThatPassNoEnvironment:
    """`python -m autotrader dashboard` and the local `ui` server both do.

    Adding one env lookup that did not guard for it crashed both commands with
    an AttributeError, and nothing in the suite noticed because every test
    passed a dict.
    """

    def test_build_payload_without_an_environment(self):
        from autotrader import dashboard
        from autotrader.config import Config
        from autotrader.state import State
        cfg = Config({"version": 2, "searches": [], "filters": {},
                      "notifications": {"channels": {}},
                      "dashboard": {"enabled": True}})
        state = State({"version": 2, "listings": {}, "searches": {}, "runs": []})
        payload = dashboard.build_payload(cfg, state)
        assert payload["changes"] == []

    def test_write_without_an_environment(self, tmp_path):
        from autotrader import dashboard
        from autotrader.config import Config
        from autotrader.state import State
        cfg = Config({"version": 2, "searches": [], "filters": {},
                      "notifications": {"channels": {}},
                      "dashboard": {"enabled": True}})
        state = State({"version": 2, "listings": {}, "searches": {}, "runs": []})
        assert dashboard.write(cfg, state, path=tmp_path / "docs/data.json")


class TestAFiringThatStoodDownIsNotACheck:
    """Three tiles described a run that read nothing.

    A firing that stands down because a check has just happened is recorded -
    it proves its timer is alive and it held a runner for the fifteen seconds
    it took to decide not to work. It read no pages, made no requests and
    took no time. With the page reading last_run, "Last good check 3h ago",
    "Requests last check 0" and "Check took 0s" all described that non-check,
    and the header said the site had been checked more recently than it had.

    Bounded by the deduplication floor rather than unbounded, which is
    exactly what makes it the kind of wrong nobody notices.
    """

    def state_with(self, tmp_path, runs):
        from autotrader.state import State
        state = State(path=tmp_path / "s.json")
        state.data["runs"] = runs
        return state

    def test_it_is_not_the_last_check(self, tmp_path):
        state = self.state_with(tmp_path, [
            {"at": "2026-01-14T19:34:00+00:00", "ok": True, "skipped": True,
             "searches_run": 0, "duration_s": 0.0},
            {"at": "2026-01-14T19:19:00+00:00", "ok": True, "searches_run": 3,
             "searches_failed": 0, "duration_s": 58.7},
        ])
        assert state.last_run["at"].startswith("2026-01-14T19:34")
        assert state.last_check["at"].startswith("2026-01-14T19:19")
        assert state.last_check["duration_s"] == 58.7

    def test_nor_is_a_run_that_could_not_read_a_single_search(self, tmp_path):
        state = self.state_with(tmp_path, [
            {"at": "2026-01-14T19:34:00+00:00", "ok": False, "searches_run": 2,
             "searches_failed": 2},
            {"at": "2026-01-14T17:19:00+00:00", "ok": True, "searches_run": 2,
             "searches_failed": 0},
        ])
        assert state.last_check["at"].startswith("2026-01-14T17:19")

    def test_a_partial_read_is_still_a_check(self, tmp_path):
        """One search down is a narrower watch, not a blind one."""
        state = self.state_with(tmp_path, [
            {"at": "2026-01-14T19:34:00+00:00", "ok": False, "searches_run": 2,
             "searches_failed": 1},
        ])
        assert state.last_check["at"].startswith("2026-01-14T19:34")

    def test_a_watcher_that_has_only_ever_stood_down_has_no_last_check(self, tmp_path):
        state = self.state_with(tmp_path, [
            {"at": "2026-01-14T19:34:00+00:00", "ok": True, "skipped": True},
        ])
        assert state.last_check is None, \
            "the page must say 'not checked yet' rather than pick the firing"

    def test_the_page_asks_for_the_check_rather_than_the_run(self, tmp_path):
        """It asks two questions - is the timer alive, and how old is this -
        and they have different answers.

        Through lastCheck(), which falls back to working it out from the runs
        when the published file predates the key. Every place that needs the
        answer goes through it; none reads last_run for this.

        Three of them now. The clock strip under the masthead is the third,
        and it is the one that would be most obviously wrong: it counts down
        to the next check from the last one, and counting from a firing that
        stood down without reading the site would show a countdown that had
        already started for a check that never happened.
        """
        from pathlib import Path
        app = Path("docs/app.js").read_text(encoding="utf-8")
        assert app.count("= lastCheck(d);") == 3, (
            "the header, the Status tiles and the clock strip all read the "
            "last CHECK")
        assert "function lastCheck(" in app
        assert "d.last_check || d.last_run" not in app, (
            "the fallback belongs inside lastCheck, where it can work the "
            "answer out from the runs rather than taking the firing")


class TestADeduplicatedFiringDoesNotBlankTheWarnings:
    """Shape drift, the request budget and the diagnostics are all products
    of a run that READ the site. A firing that stood down has none of them,
    so taking them off the last run hid a live warning until the next check.
    """

    def payload(self, tmp_path, runs):
        from autotrader.config import Config
        from autotrader.dashboard import build_payload
        from autotrader.state import State
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=25", "Civic")
        cfg.save()
        state = State(path=tmp_path / "state.json")
        state.data["runs"] = runs
        return build_payload(cfg, state, {})

    def test_the_drift_warning_survives_a_firing_that_stood_down(self, tmp_path,
                                                                 monkeypatch):
        monkeypatch.chdir(tmp_path)
        payload = self.payload(tmp_path, [
            {"at": "2026-01-14T19:34:00+00:00", "ok": True, "skipped": True,
             "searches_run": 0, "requests_made": 0, "shape_drift": [],
             "diagnostics": []},
            {"at": "2026-01-14T17:19:00+00:00", "ok": True, "searches_run": 2,
             "searches_failed": 0, "requests_made": 32,
             "shape_drift": ["jsonld stopped matching"],
             "diagnostics": ["diagnostics/shape.json"]},
        ])
        health = payload["health"]
        assert health["drift"] == ["jsonld stopped matching"]
        assert health["budget"]["used"] == 32
        assert health["diagnostics"] == ["diagnostics/shape.json"]
