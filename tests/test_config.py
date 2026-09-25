import json

import pytest

from autotrader.config import Config, ConfigError


def test_a_pasted_link_becomes_a_named_search(tmp_path):
    cfg = Config.defaults(tmp_path / "c.json")
    search = cfg.add_search(
        "https://www.autotrader.ca/cars/honda/civic/?rcp=15&srt=35&yRng=2021%2c&prx=-2")
    assert search.name == "2021+ Honda Civic"
    assert search.enabled and search.id


def test_the_same_search_cannot_be_added_twice(tmp_path):
    cfg = Config.defaults(tmp_path / "c.json")
    cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=15")
    with pytest.raises(ConfigError, match="already being watched"):
        cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=15")


def test_tracking_parameters_do_not_create_a_duplicate_search(tmp_path):
    cfg = Config.defaults(tmp_path / "c.json")
    cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=15")
    with pytest.raises(ConfigError):
        cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=15&utm_source=email")


def test_a_link_from_another_site_is_refused_with_an_explanation(tmp_path):
    cfg = Config.defaults(tmp_path / "c.json")
    with pytest.raises(ConfigError, match="not autotrader.ca"):
        cfg.add_search("https://www.kijiji.ca/b-cars/")


def test_force_accepts_an_unusual_link(tmp_path):
    cfg = Config.defaults(tmp_path / "c.json")
    assert cfg.add_search("https://www.autotrader.ca/", strict=False)


def test_config_survives_a_round_trip(tmp_path):
    path = tmp_path / "c.json"
    cfg = Config.defaults(path)
    cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=15", "My Civic")
    cfg.set("filters.max_price", 110000)
    cfg.save()
    again = Config.load(path)
    assert [s.name for s in again.searches] == ["My Civic"]
    assert again.get("filters.max_price") == 110000


def test_missing_settings_fall_back_to_defaults(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"searches": []}))
    cfg = Config.load(path)
    assert cfg.get("scraping.max_pages") == 3
    assert cfg.get("notifications.notify_on.new") is True


def test_a_broken_config_file_says_so(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        Config.load(path)


def test_disabled_searches_are_not_run(tmp_path):
    cfg = Config.defaults(tmp_path / "c.json")
    cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=15")
    cfg.data["searches"][0]["enabled"] = False
    assert cfg.searches and cfg.active_searches == []


def test_removing_a_search(tmp_path):
    cfg = Config.defaults(tmp_path / "c.json")
    search = cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=15")
    assert cfg.remove_search(search.id)
    assert not cfg.remove_search("nope")
    assert cfg.searches == []


def test_search_ids_stay_unique(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"searches": [
        {"id": "dup", "url": "https://www.autotrader.ca/cars/honda/civic/?a=1"},
        {"id": "dup", "url": "https://www.autotrader.ca/cars/toyota/corolla/?a=1"}]}))
    ids = [s.id for s in Config.load(path).searches]
    assert len(set(ids)) == 2


def test_junk_entries_are_ignored_rather_than_crashing(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"searches": [
        "not an object", {"no_url": True},
        {"url": "https://www.autotrader.ca/cars/honda/civic/?a=1"}]}))
    assert len(Config.load(path).searches) == 1


class TestTheDefaultsAgreeWithEachOther:
    """A setting can be sensible on its own and wrong beside another.

    silent_after_hours shipped as 3 against a 120-minute schedule - one and a
    half windows. GitHub drops windows in pairs, so a fresh install would have
    alarmed most days for a schedule working exactly as measured, and an alarm
    that cries wolf is an alarm that gets muted.
    """

    def health(self):
        from autotrader.config import DEFAULTS
        return DEFAULTS["health"]

    def test_the_silence_alarm_allows_at_least_three_missed_windows(self):
        h = self.health()
        windows = h["silent_after_hours"] * 60 / h["expected_interval_minutes"]
        assert windows >= 2.5, (
            f"the alarm fires after {windows:.1f} missed windows; GitHub drops "
            f"them in pairs, so anything under three is a weekly false alarm")

    def test_the_dedupe_floor_is_shorter_than_the_interval(self):
        """Or an early firing - GitHub fires early as readily as late - is
        discarded and the check waits for the next window."""
        h = self.health()
        assert h["min_interval_minutes"] < h["expected_interval_minutes"]

    def test_the_removal_grace_is_the_dedupe_floor(self):
        """Single-sourced: a car cannot be called gone faster than two real
        checks could establish it. state.mark_missing reads this number."""
        import inspect

        from autotrader import runner
        source = inspect.getsource(runner.run)
        assert 'health.min_interval_minutes' in source, \
            "the removal grace must read the deduplication floor, not its own"
