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


class TestTheSetCommand:
    """`set` turns what was typed into the JSON type it obviously is."""

    URL = "https://www.autotrader.ca/cars/honda/civic/?rcp=25"

    @pytest.fixture
    def path(self, tmp_path):
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.add_search(self.URL, "Civic")
        cfg.save()
        return tmp_path / "config.json"

    def _set(self, path, *args):
        from autotrader import cli
        assert cli.main(["--config", str(path), "set", *args]) == 0
        return Config.load(path)

    def test_a_place_keeps_its_comma(self, path):
        """Every comma used to make a list, so "Toronto, ON" was stored as
        two places and read back everywhere as "['Toronto', 'ON']"."""
        cfg = self._set(path, "near", "Toronto, ON", "--search", "Civic")
        assert cfg.searches[0].filters["near"] == "Toronto, ON"
        assert self._set(path, "filters.near", "Toronto, ON").get("filters.near") \
            == "Toronto, ON"

    def test_a_rule_that_holds_names_is_still_a_list(self, path):
        cfg = self._set(path, "include_keywords", "manual,sunroof", "--search", "Civic")
        assert cfg.searches[0].filters["include_keywords"] == ["manual", "sunroof"]
        cfg = self._set(path, "filters.exclude_sellers", "A Dealer, Another")
        assert cfg.get("filters.exclude_sellers") == ["A Dealer", "Another"]

    def test_a_search_price_drop_floor_is_the_one_it_reads(self, path):
        """Written into the search's filters, where nothing reads it."""
        cfg = self._set(path, "price_drop_min_abs", "500", "--search", "Civic")
        assert cfg.rules_for(cfg.searches[0])["price_drop_min_abs"] == 500

    @pytest.mark.parametrize("key, hint", [
        ("price_drop_min_abs", "did you mean notifications.price_drop_min_abs?"),
        ("notifications.price_drop_min_ab", "did you mean notifications.price_drop_min_abs?"),
        ("max_price", "did you mean filters.max_price, or max_price with --search?"),
        ("notifications.channels.pager.enabled", ""),
        ("notifications.digest.every", ""),
        ("colour", "a setting is named with its section")])
    def test_a_key_nothing_reads_is_refused(self, path, key, hint, capsys):
        """`set price_drop_min_abs 500` wrote a key at the top of the file,
        said it had, and the floor stayed at $250."""
        from autotrader import cli
        before = path.read_text()
        assert cli.main(["--config", str(path), "set", key, "500"]) == 1
        assert path.read_text() == before
        out = capsys.readouterr().out
        assert "nothing was changed" in out and hint in out, out

    @pytest.mark.parametrize("key, value, read", [
        ("notifications.price_drop_min_abs", "500", 500),
        ("filters.max_distance_km", "100", 100),
        ("dashboard.photos", "false", False),
        ("marketplace.gone_after_days", "14", 14),
        ("notifications.channels.ntfy.icon", "https://example.org/car.png",
         "https://example.org/car.png"),
        ("notifications.channels.telegram.enabled", "false", False)])
    def test_a_key_something_reads_is_still_set(self, path, key, value, read):
        assert self._set(path, key, value).get(key) == read


def test_a_bare_command_runs_against_the_files_it_was_given(tmp_path, monkeypatch):
    """With no command a check runs; the --config and --state typed before
    it were dropped on the way, and it read and wrote ./config.json."""
    import sys
    from autotrader import cli
    seen = {}
    monkeypatch.setattr(cli, "cmd_run", lambda args: seen.update(
        config=args.config, state=args.state) or 0)
    config, state = tmp_path / "elsewhere.json", tmp_path / "kept.json"
    monkeypatch.setattr(sys, "argv", ["autotrader", "--config", str(config),
                                      "--state", str(state)])
    assert cli.main() == 0
    assert seen == {"config": str(config), "state": str(state)}
