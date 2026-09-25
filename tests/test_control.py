"""The write channel: a phone changing config.json, with no token.

This module parses text a person typed on a phone and then writes the file
that decides which cars get watched. It shipped with no tests at all, which
is the wrong way round: of everything here, this is the part where a bug is
both easiest to write and hardest to notice - a change that silently does
nothing looks exactly like a change that worked.

Three properties, and every test below is one of them:

* whole or nothing. A list whose third instruction is nonsense leaves the
  first two unapplied. A half-applied config is worse than a refused one
  because nobody goes looking for it.
* every refusal names what was wrong with the input, not that something was.
* nothing is trusted. The body is only as trustworthy as whoever could
  commit the file, so every value is parsed and bounded.
"""

from __future__ import annotations

import json

import pytest

from autotrader import control
from autotrader.config import Config
from autotrader.state import State


def cfg_with(**over) -> Config:
    data = {
        "version": 2,
        "searches": [
            {"id": "alpha", "name": "Alpha", "enabled": True,
             "url": "https://www.autotrader.ca/cars/honda/civic/",
             "filters": {"max_price": 50000}},
            {"id": "beta", "name": "Beta", "enabled": True,
             "url": "https://www.autotrader.ca/cars/audi/rs6/", "filters": {}},
        ],
        "filters": {},
        "notifications": {"channels": {"ntfy": {"enabled": True},
                                       "discord": {"enabled": "auto"}}},
    }
    data.update(over)
    return Config(data)


def state_with(*ids) -> State:
    st = State({"version": 2, "listings": {
        i: {"id": i, "title": f"car {i}"} for i in ids}, "searches": {}, "runs": []})
    return st


class TestFindingTheInstruction:
    def test_a_committed_control_file_is_bare_json(self):
        """A file committed through GitHub's web editor needs no repository
        setting, no token and no terminal."""
        items = control.parse('[{"action": "set-rule", "rule": "min_year",'
                              ' "value": 2015}]')
        assert items[0]["value"] == 2015

    def test_surrounding_whitespace_is_not_a_problem(self):
        """GitHub's editor adds a final newline; a person may add more."""
        items = control.parse('\n  {"action": "shortlist", "listing": "abc123"}\n\n')
        assert items[0]["listing"] == "abc123"

    def test_a_single_object_is_a_list_of_one(self):
        assert len(control.parse('{"action": "shortlist", "listing": "abc123"}')) == 1

    def test_prose_with_no_instruction_says_so(self):
        with pytest.raises(control.Rejected) as exc:
            control.parse("I would like the bot to watch cheaper cars please")
        assert "JSON" in str(exc.value) and "dashboard" in str(exc.value)

    def test_json_wrapped_in_prose_is_not_dug_out(self):
        """Only the dashboard writes these, and it writes JSON alone. Text
        around an instruction means somebody else wrote it."""
        with pytest.raises(control.Rejected):
            control.parse('Please do this:\n{"action": "shortlist", "listing": "abc123"}')

    def test_an_empty_body(self):
        with pytest.raises(control.Rejected):
            control.parse("   ")

    def test_broken_json_names_the_line_and_column(self):
        with pytest.raises(control.Rejected) as exc:
            control.parse('[\n {"action": }\n]')
        message = str(exc.value)
        assert "line 2" in message and "column" in message

    def test_an_empty_list(self):
        with pytest.raises(control.Rejected) as exc:
            control.parse("[]")
        assert "no instructions" in str(exc.value)

    def test_an_unknown_action_lists_the_known_ones(self):
        with pytest.raises(control.Rejected) as exc:
            control.parse('{"action": "delete-everything"}')
        assert "set-rule" in str(exc.value)

    def test_a_thousand_instructions_is_not_a_change(self):
        with pytest.raises(control.Rejected) as exc:
            control.parse(json.dumps([{"action": "shortlist"}] * 40))
        assert "Split it up" in str(exc.value)

    def test_an_instruction_that_is_not_an_object(self):
        with pytest.raises(control.Rejected) as exc:
            control.parse('["set-rule"]')
        assert "object" in str(exc.value)


class TestApplyingRules:
    def test_a_rule_on_one_search(self):
        cfg, st = cfg_with(), state_with()
        out = control.apply(cfg, st, [{"action": "set-rule", "search": "Alpha",
                                       "rule": "max_price", "value": 60000}])
        assert out.changed and not out.rejected
        assert cfg.data["searches"][0]["filters"]["max_price"] == 60000
        assert cfg.data["searches"][1]["filters"] == {}

    def test_a_rule_everywhere(self):
        cfg, st = cfg_with(), state_with()
        out = control.apply(cfg, st, [{"action": "set-rule",
                                       "rule": "min_year", "value": 2018}])
        assert out.changed
        assert cfg.get("filters.min_year") == 2018

    def test_null_removes_a_rule_rather_than_setting_it_to_nothing(self):
        cfg, st = cfg_with(), state_with()
        control.apply(cfg, st, [{"action": "set-rule", "search": "Alpha",
                                 "rule": "max_price", "value": None}])
        assert "max_price" not in cfg.data["searches"][0]["filters"]

    def test_a_misspelled_rule_is_named_not_ignored(self):
        cfg, st = cfg_with(), state_with()
        out = control.apply(cfg, st, [{"action": "set-rule",
                                       "rule": "maximum_price", "value": 5}])
        assert not out.changed
        assert "maximum_price" in out.rejected[0]

    def test_a_rule_outside_its_bounds(self):
        cfg, st = cfg_with(), state_with()
        out = control.apply(cfg, st, [{"action": "set-rule",
                                       "rule": "max_price", "value": 99_000_000}])
        assert not out.changed and "between" in out.rejected[0]

    def test_a_rule_that_is_not_a_number(self):
        cfg, st = cfg_with(), state_with()
        out = control.apply(cfg, st, [{"action": "set-rule",
                                       "rule": "max_price", "value": "cheap"}])
        assert not out.changed and "number" in out.rejected[0]

    def test_a_search_that_does_not_exist(self):
        cfg, st = cfg_with(), state_with()
        out = control.apply(cfg, st, [{"action": "set-rule", "search": "Gamma",
                                       "rule": "max_price", "value": 5}])
        assert not out.changed and "Gamma" in out.rejected[0]

    def test_nothing_is_written_when_any_instruction_is_refused(self):
        """The one that matters: two good, one bad, none applied."""
        cfg, st = cfg_with(), state_with()
        out = control.apply(cfg, st, [
            {"action": "set-rule", "search": "Alpha", "rule": "max_price",
             "value": 60000},
            {"action": "set-rule", "rule": "min_year", "value": 2018},
            {"action": "set-rule", "rule": "max_price", "value": -7},
        ])
        assert not out.changed
        assert cfg.data["searches"][0]["filters"]["max_price"] == 50000
        assert cfg.get("filters.min_year") is None
        assert len(out.rejected) == 1


class TestSearches:
    def test_adding_one(self):
        cfg, st = cfg_with(), state_with()
        out = control.apply(cfg, st, [{
            "action": "add-search", "name": "Gamma",
            "url": "https://www.autotrader.ca/cars/toyota/corolla/"}])
        assert out.changed and len(cfg.searches) == 3

    def test_a_link_to_somewhere_else_entirely(self):
        cfg, st = cfg_with(), state_with()
        out = control.apply(cfg, st, [{
            "action": "add-search", "name": "x",
            "url": "https://evil.example.com/steal"}])
        assert not out.changed and "autotrader.ca" in out.rejected[0]

    def test_a_javascript_url(self):
        cfg, st = cfg_with(), state_with()
        out = control.apply(cfg, st, [{
            "action": "add-search", "url": "javascript:alert(1)"}])
        assert not out.changed

    def test_removing_one(self):
        cfg, st = cfg_with(), state_with()
        out = control.apply(cfg, st, [{"action": "remove-search", "search": "Beta"}])
        assert out.changed and [s.id for s in cfg.searches] == ["alpha"]

    def test_removing_the_last_one_would_leave_it_watching_nothing(self):
        cfg = cfg_with(searches=[{"id": "only", "name": "Only", "enabled": True,
                                  "url": "https://www.autotrader.ca/cars/",
                                  "filters": {}}])
        out = control.apply(cfg, state_with(), [
            {"action": "remove-search", "search": "Only"}])
        assert not out.changed and "only search" in out.rejected[0]


class TestMarksOnACar:
    ID = "00000000-0000-4000-8000-000000000042"

    def test_muting(self):
        cfg, st = cfg_with(), state_with(self.ID)
        out = control.apply(cfg, st, [{"action": "mute-listing", "listing": self.ID}])
        assert out.changed and st.listings[self.ID]["you"]["muted"] is True

    def test_shortlisting_clears_a_dismissal(self):
        cfg, st = cfg_with(), state_with(self.ID)
        st.listings[self.ID]["you"] = {"dismissed": True}
        control.apply(cfg, st, [{"action": "shortlist", "listing": self.ID}])
        marks = st.listings[self.ID]["you"]
        assert marks["shortlisted"] is True and "dismissed" not in marks

    def test_a_note_is_kept_and_bounded(self):
        cfg, st = cfg_with(), state_with(self.ID)
        control.apply(cfg, st, [{"action": "note", "listing": self.ID,
                                 "text": "x" * 900}])
        assert len(st.listings[self.ID]["you"]["note"]) == 400

    def test_a_car_we_have_never_seen(self):
        cfg, st = cfg_with(), state_with(self.ID)
        out = control.apply(cfg, st, [{"action": "dismiss",
                                       "listing": "aaaaaaaaaaaa"}])
        assert not out.changed and "no listing" in out.rejected[0]

    @pytest.mark.parametrize("nasty", [
        "../../config.json",
        "a" * 500,
        "id with spaces",
        "",
        "<script>alert(1)</script>",
        "'; DROP TABLE listings; --",
    ])
    def test_a_listing_id_that_is_not_one(self, nasty):
        cfg, st = cfg_with(), state_with(self.ID)
        out = control.apply(cfg, st, [{"action": "dismiss", "listing": nasty}])
        assert not out.changed, nasty
        assert "not a listing id" in out.rejected[0]


class TestChannels:
    def test_turning_one_off(self):
        cfg, st = cfg_with(), state_with()
        out = control.apply(cfg, st, [{"action": "set-channel",
                                       "channel": "ntfy", "enabled": False}])
        assert out.changed
        assert cfg.get("notifications.channels.ntfy.enabled") is False

    def test_a_channel_that_is_not_configured_here(self):
        cfg, st = cfg_with(), state_with()
        out = control.apply(cfg, st, [{"action": "set-channel",
                                       "channel": "carrier-pigeon", "enabled": True}])
        assert not out.changed and "ntfy" in out.rejected[0]

    def test_enabled_has_to_be_a_boolean(self):
        cfg, st = cfg_with(), state_with()
        out = control.apply(cfg, st, [{"action": "set-channel",
                                       "channel": "ntfy", "enabled": "yes"}])
        assert not out.changed and "true or false" in out.rejected[0]


class TestNothingHereTrustsTheText:
    """A change file is text from outside the bot, so none of it is taken
    on trust - even from a sealed file, which only proves who sealed it."""

    def test_a_rule_name_that_is_a_config_path(self):
        cfg, st = cfg_with(), state_with()
        out = control.apply(cfg, st, [{
            "action": "set-rule",
            "rule": "notifications.channels.webhook.url",
            "value": "https://evil.example.com/"}])
        assert not out.changed
        assert cfg.get("notifications.channels.webhook.url") is None

    def test_a_rule_name_that_is_a_dunder(self):
        cfg, st = cfg_with(), state_with()
        out = control.apply(cfg, st, [{"action": "set-rule",
                                       "rule": "__class__", "value": 1}])
        assert not out.changed

    @pytest.mark.parametrize("given,want", [
        ("false", False), ("no", False), ("0", False), (0, False),
        ("true", True), ("yes", True), (1, True), (True, True),
    ])
    def test_a_yes_no_rule_reads_the_word_not_its_truthiness(self, given, want):
        """bool("false") is True, and that is how a rule turns itself on."""
        cfg, st = cfg_with(), state_with()
        control.apply(cfg, st, [{"action": "set-rule",
                                 "rule": "require_price", "value": given}])
        assert cfg.get("filters.require_price") is want

    def test_a_yes_no_rule_refuses_anything_else(self):
        cfg, st = cfg_with(), state_with()
        out = control.apply(cfg, st, [{"action": "set-rule",
                                       "rule": "require_price", "value": "maybe"}])
        assert not out.changed and "yes/no" in out.rejected[0]


class TestTheRuleAPersonIsMostLikelyToChange:
    """"I moved" - and it crashed the run.

    `near` is typed str in the rule table, and the value path had no branch
    for a string: it fell through to the numeric one, which compares the
    value against a float bound and raises TypeError out of the command
    itself. Not a refusal, not a wrong answer - a traceback, in a workflow
    whose whole job is to either apply a change or explain why it did not.
    It survived because this module had no tests.
    """

    @pytest.mark.parametrize("place", [
        "K1P 1J1", "k1p1j1", "K1P", "Toronto, ON", "Ottawa",
        "St. John's", "Trois-Rivieres, QC",
    ])
    def test_somewhere_it_can_measure_from(self, place):
        cfg, st = cfg_with(), state_with()
        out = control.apply(cfg, st, [{"action": "set-rule", "search": "Alpha",
                                       "rule": "near", "value": place}])
        assert out.changed, out.rejected
        assert cfg.data["searches"][0]["filters"]["near"] == place.strip()

    @pytest.mark.parametrize("nasty", [
        "", "   ", "x" * 80, 12345, {"a": 1}, ["K1P 1J1"],
        "<script>alert(1)</script>", "'; DROP TABLE --",
        "https://evil.example.com/",
    ])
    def test_anything_else_is_refused_rather_than_raised(self, nasty):
        cfg, st = cfg_with(), state_with()
        out = control.apply(cfg, st, [{"action": "set-rule",
                                       "rule": "near", "value": nasty}])
        assert not out.changed, nasty
        assert out.rejected and cfg.get("filters.near") is None

    def test_clearing_it_is_a_real_request(self):
        cfg, st = cfg_with(), state_with()
        cfg.data["searches"][0]["filters"]["near"] = "K1P 1J1"
        control.apply(cfg, st, [{"action": "set-rule", "search": "Alpha",
                                 "rule": "near", "value": None}])
        assert "near" not in cfg.data["searches"][0]["filters"]

    def test_every_rule_in_the_table_can_be_set_without_raising(self):
        """The general version. A new rule type must not reintroduce this.

        The value used is the midpoint of each rule's own bounds, so adding a
        rule with a range this test knows nothing about still exercises it.
        """
        for name, kind in control.RULE_TYPES.items():
            if kind is bool:
                value = True
            elif kind is str:
                value = "K1P 1J1"
            else:
                low, high = control.RULE_BOUNDS[name]
                value = kind((low + high) / 2)
            cfg, st = cfg_with(), state_with()
            out = control.apply(cfg, st, [{"action": "set-rule", "rule": name,
                                           "value": value}])
            assert out.changed, f"{name}={value!r}: {out.rejected}"

    def test_every_numeric_rule_has_bounds(self):
        """A rule with no bounds accepts a billion, which is not a rule."""
        unbounded = [n for n, k in control.RULE_TYPES.items()
                     if k in (int, float) and n not in control.RULE_BOUNDS]
        assert not unbounded, unbounded


class TestWhatItSaysBack:
    def test_a_refusal_does_not_assume_how_the_change_travelled(self):
        """It is shown on the dashboard beside the change, not posted back
        wherever the change came from - "edit the issue" once reached people
        who had never opened one."""
        cfg, st = cfg_with(), state_with()
        out = control.apply(cfg, st, [{"action": "set-rule",
                                       "rule": "nonsense", "value": 1}])
        text = out.comment().lower()
        assert "issue" not in text and "commit" not in text, out.comment()

    def test_an_applied_change_lists_what_it_did(self):
        cfg, st = cfg_with(), state_with()
        out = control.apply(cfg, st, [{"action": "set-rule", "search": "Alpha",
                                       "rule": "max_price", "value": 60000}])
        text = out.comment()
        assert "Applied" in text and "60000" in text and "Alpha" in text

    def test_a_refusal_names_the_input_not_just_the_failure(self):
        cfg, st = cfg_with(), state_with()
        out = control.apply(cfg, st, [{"action": "set-rule",
                                       "rule": "max_price", "value": 99_000_000}])
        assert "99,000,000" in out.comment()

    def test_no_number_in_a_refusal_is_in_scientific_notation(self):
        """"between 1 and 1e+07" is a message for a debugger, read on a phone."""
        cfg, st = cfg_with(), state_with()
        for rule in control.RULE_BOUNDS:
            out = control.apply(cfg, st, [{"action": "set-rule", "rule": rule,
                                           "value": 99_000_000}])
            assert "e+" not in out.comment(), (rule, out.comment())


class TestTakingAMarkBack:
    """Every mark needs an undo, and the undo has to undo that mark.

    The page's "Dismissed" button sent `unshortlist` as its undo - an action
    that clears a mark the car does not have and leaves the dismissal exactly
    where it was. The button reported success, the car stayed dismissed, and
    nothing anywhere said otherwise.
    """
    ID = "00000000-0000-4000-8000-000000000042"

    def test_undismiss_clears_the_dismissal(self):
        cfg, st = cfg_with(), state_with(self.ID)
        control.apply(cfg, st, [{"action": "dismiss", "listing": self.ID}])
        assert st.listings[self.ID]["you"]["dismissed"] is True
        control.apply(cfg, st, [{"action": "undismiss", "listing": self.ID}])
        assert "dismissed" not in st.listings[self.ID]["you"]

    def test_an_empty_note_clears_it_rather_than_storing_nothing(self):
        cfg, st = cfg_with(), state_with(self.ID)
        control.apply(cfg, st, [{"action": "note", "listing": self.ID,
                                 "text": "ask about the ceramics"}])
        control.apply(cfg, st, [{"action": "note", "listing": self.ID, "text": ""}])
        assert "note" not in st.listings[self.ID]["you"]

    @pytest.mark.parametrize("mark,undo", [
        ("shortlist", "unshortlist"),
        ("mute-listing", "unmute-listing"),
        ("dismiss", "undismiss"),
    ])
    def test_every_mark_round_trips(self, mark, undo):
        cfg, st = cfg_with(), state_with(self.ID)
        control.apply(cfg, st, [{"action": mark, "listing": self.ID}])
        assert st.listings[self.ID].get("you"), mark
        control.apply(cfg, st, [{"action": undo, "listing": self.ID}])
        assert not st.listings[self.ID]["you"], (mark, undo)

    def test_the_page_offers_an_undo_for_every_mark_it_can_set(self):
        """The bug, stated so the page cannot drift from the module again."""
        import re
        from pathlib import Path
        js = Path("docs/app.js").read_text()
        rows = re.findall(
            r"\['(\w+)', '[^']*', '[^']*', '([\w-]+)', '([\w-]+)'\]", js)
        assert rows, "the marks table in the sheet could not be found"
        for key, action, undo in rows:
            assert action in control.ACTIONS, (key, action)
            assert undo in control.ACTIONS, (key, undo)
            # An undo that is not the inverse of the action is the whole bug.
            assert undo.replace("un", "", 1).rstrip("-listing") in action \
                or action.replace("un", "", 1) in undo \
                or {action, undo} == {"dismiss", "undismiss"}, (key, action, undo)
