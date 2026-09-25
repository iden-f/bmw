"""A search for a Civic Type R that comes back with a 2013 Civic LX.

A results page can carry cars that are not the model the pasted link names:
a plain Civic beside the Type R it was asked for, or a car whose model reads
"Unspecified". Left alone, those sit on the dashboard as cars you could buy,
and get pushed to a phone.

Every other rejection in this bot is a preference - too dear, too far, too old
- and those cars are kept and explained, because they are the cars you are
shopping for. A result that is not the model at all was never yours to judge,
so it is discarded: not stored, not published, not counted, not alerted on,
and not silently either - the run says how many and the Status page shows it.
"""

from __future__ import annotations

import pytest

from autotrader import filters, runner
from autotrader.config import Config
from autotrader.listing import Listing
from autotrader.state import State


def car(**kw):
    kw.setdefault("id", "x")
    kw.setdefault("make", "Honda")
    return Listing(**kw)


RULE = {"models": ["Civic Type R"]}


class TestWhichCarIsThis:
    @pytest.mark.parametrize("model", ["Civic Type R", "civic type r",
                                       "Civic-Type-R", "Civic  Type  R"])
    def test_the_car_the_search_is_for_survives_however_it_is_spelt(self, model):
        assert filters.check(car(model=model), RULE).keep

    @pytest.mark.parametrize("model,trim", [
        ("Civic", "TYPE R STYLE EDITION"),
        ("Civic", "Si Sedan"),
        ("Civic", "Sport Touring Type R Package"),
        ("Civic", ""),
        ("Unspecified", "4dr 2.0L"),
        ("", "Type R"),
        ("Civic Si", "Type R"),
    ])
    def test_everything_else_is_turned_away_as_a_different_car(self, model, trim):
        verdict = filters.check(car(model=model, trim=trim), RULE)
        assert not verdict.keep
        assert verdict.wrong_car
        assert verdict.rule == "models"

    def test_the_reason_names_both_cars(self):
        verdict = filters.check(car(model="Civic", trim="Sport"), RULE)
        assert "Civic is not Civic Type R" == verdict.reason

    def test_a_car_with_no_model_at_all_is_not_guessed_at(self):
        """An unnamed model is not a licence to keep the car.

        The parser sets the model from the results card. When it cannot, the
        answer is "I do not know what this is", and a search that has told the
        bot which car it wants does not want an unknown one.
        """
        verdict = filters.check(car(model=""), RULE)
        assert verdict.wrong_car
        assert "an unnamed model" in verdict.reason

    def test_no_rule_means_no_opinion(self):
        assert filters.check(car(model="Civic"), {}).keep
        assert filters.check(car(model="Civic"), {"models": []}).keep
        assert filters.check(car(model="Civic"), {"models": [""]}).keep

    def test_a_single_name_is_one_model_and_not_a_list_of_letters(self):
        assert filters.check(car(model="Civic Type R"), {"models": "Civic Type R"}).keep
        assert filters.check(car(model="Civic"), {"models": "Civic Type R"}).wrong_car

    def test_several_models_are_all_accepted(self):
        rule = {"models": ["Civic Type R", "Civic Si"]}
        assert filters.check(car(model="Civic Si"), rule).keep
        assert filters.check(car(model="Civic Type R"), rule).keep
        assert filters.check(car(model="Civic Hybrid"), rule).wrong_car


class TestItIsAskedFirst:
    """Before price, before year, before distance.

    A 2013 Civic in Halifax failed the distance rule first and was filed as
    "too far away" - a sentence about a car that was never being considered.
    Most of the wrong cars were mislabelled that way, which is what made the
    "Hidden by a rule" count meaningless.
    """

    def test_a_wrong_car_is_not_reported_as_too_old_or_too_far(self):
        rules = {"models": ["Civic Type R"], "min_year": 2017, "max_price": 50000,
                 "max_mileage_km": 100_000, "near": "Toronto, ON",
                 "max_distance_km": 100}
        verdict = filters.check(
            car(model="Civic", year=2013, price=90000, mileage_km=250_000,
                location="Halifax", province="NS"), rules)
        assert verdict.rule == "models"

    def test_the_right_car_is_still_judged_by_every_other_rule(self):
        rules = {"models": ["Civic Type R"], "min_year": 2017}
        verdict = filters.check(car(model="Civic Type R", year=2016), rules)
        assert not verdict.keep
        assert not verdict.wrong_car
        assert verdict.rule == "min_year"


class TestTheFourBuckets:
    def test_apply_separates_a_wrong_car_from_a_hidden_one(self):
        cars = [car(id="right", model="Civic Type R", year=2021, price=40000),
                car(id="old", model="Civic Type R", year=2015, price=30000),
                car(id="wrong", model="Civic", year=2021, price=20000)]
        kept, unpriced, dropped, wrong = filters.apply(
            cars, {"models": ["Civic Type R"], "min_year": 2017})
        assert [l.id for l in kept] == ["right"]
        assert unpriced == []
        assert [l.id for l, _ in dropped] == ["old"]
        assert [l.id for l, _ in wrong] == ["wrong"]

    def test_the_early_split_agrees_with_the_full_one(self):
        cars = [car(id=str(i), model=m, year=2021, price=40000)
                for i, m in enumerate(["Civic Type R", "Civic",
                                       "Civic Type R Limited", "Civic Type R"])]
        mine, theirs = filters.not_this_car(cars, {"models": ["Civic Type R"]})
        _, _, _, wrong = filters.apply(cars, {"models": ["Civic Type R"]})
        assert {l.id for l in mine} == {"0", "3"}
        assert {l.id for l, _ in theirs} == {l.id for l, _ in wrong} == {"1", "2"}

    def test_no_rule_means_the_early_split_does_nothing(self):
        cars = [car(id="a", model="Civic"), car(id="b", model="Accord")]
        mine, theirs = filters.not_this_car(cars, {"max_price": 10})
        assert len(mine) == 2 and theirs == []


class TestItLeavesNothingBehind:
    def test_a_stored_wrong_car_is_dropped(self):
        state = State({"listings": {
            "junk": {"id": "junk", "model": "Civic", "status": "active"},
            "real": {"id": "real", "model": "Civic Type R", "status": "active"},
        }})
        assert state.discard_wrong_cars(["junk"]) == ["junk"]
        assert set(state.listings) == {"real"}

    def test_a_car_still_owed_an_alert_is_dropped_and_so_is_the_alert(self):
        """This used to assert the opposite, and the opposite was wrong.

        Being told is the promise the ledger exists to keep, so a queued
        alert protected its car from being dropped. But the queued message
        says "here is a car you are watching", and the finding that drops it
        is that the car is not one. Keeping the promise to the queue means
        breaking the one to the reader - and then the run has to report the
        alert as a car it told you about by mistake, which is a sentence this
        repository has already had to write once.

        Your own marks still protect a car. They are yours, and a rule the
        bot has changed its mind about is not a reason to edit them."""
        state = State({"listings": {
            "owed": {"id": "owed", "model": "Civic", "pending": [{"kind": "new"}]},
        }})
        assert state.discard_wrong_cars(["owed"]) == ["owed"]
        assert "owed" not in state.listings

    def test_a_car_you_marked_is_never_dropped(self):
        state = State({"listings": {
            "mine": {"id": "mine", "model": "Civic", "you": {"shortlisted": True}},
        }})
        assert state.discard_wrong_cars(["mine"]) == []

    def test_dropping_a_car_that_is_not_there_is_not_an_error(self):
        assert State({"listings": {}}).discard_wrong_cars(["nobody"]) == []


class TestItSaysSo:
    def test_the_rule_has_a_name_for_the_shut_out_warning(self):
        """Without an entry here a search that kept nothing reports
        "40 other rules", which explains nothing to anybody."""
        assert runner.RULE_LABELS["models"] == "a different model"
        verdict = filters.check(car(model="Civic"), RULE)
        assert runner._why_none_survived([(car(model="Civic"), verdict)]) \
            == "1 a different model"

    def test_every_rule_the_filters_can_return_has_a_label(self):
        import re
        source = (__import__("pathlib").Path("autotrader/filters.py")).read_text()
        rules = set(re.findall(r'rule="([a-z_]+)"', source))
        assert rules, "no rules found - has the marker changed?"
        assert rules <= set(runner.RULE_LABELS), rules - set(runner.RULE_LABELS)


class TestAWatchSetUpTheWayItIsMeantToBe:
    """The rule above, applied to a watch configured the way the example
    config tells you to configure one.

    Written to disk and read back through Config.load, the path a real watch's
    config.json takes, and run over listing rows shaped exactly as state.json
    stores them: a pasted 2026 link for a Civic Type R from 2017 on, with the
    model and the years that link loses set again as rules of the search.
    """

    LINK = ("https://www.autotrader.ca/cars/honda/civic/va_civic-type-r/reg_on/"
            "cit_toronto/?offer=N%2CU&modelyearfrom=2017&zipr=500&size=20")

    ROWS = [
        # (model, year, location, province, is really a Civic Type R)
        ("Civic Type R", 2017, "Halifax", "NS", True),
        ("Civic Type R", 2019, "Winnipeg", "MB", True),
        ("Civic Type R", 2021, "Regina", "SK", True),
        ("Civic Type R", 2023, "Kingston", "ON", True),
        ("Civic", 2011, "Barrie", "ON", False),
        ("Civic", 2018, "Hamilton", "ON", False),
        ("Civic", 2025, "Moncton", "NB", False),
        ("Civic", 2022, "Oshawa", "ON", False),
        ("Unspecified", 2006, "Guelph", "ON", False),
    ]

    @pytest.fixture
    def watch(self, tmp_path):
        import json
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"version": 2, "searches": [{
            "id": "honda-civic-type-r", "name": "Honda Civic Type R, 2017 onwards",
            "url": self.LINK, "enabled": True,
            "filters": {"models": ["Civic Type R"], "min_year": 2017},
        }]}), encoding="utf-8")
        cfg = Config.load(path)
        return cfg, [s for s in cfg.active_searches if "civic" in s.id][0]

    def test_the_rule_keeps_every_type_r_and_nothing_else(self, watch):
        cfg, search = watch
        rules = cfg.rules_for(search)["filters"]
        cars = [car(id=str(i), model=m, year=y, location=c, province=p, price=40000)
                for i, (m, y, c, p, _) in enumerate(self.ROWS)]
        _, _, _, wrong = filters.apply(cars, rules)
        wrong_ids = {l.id for l, _ in wrong}
        for i, (model, _, _, _, real) in enumerate(self.ROWS):
            assert (str(i) in wrong_ids) is not real, (i, model)

    def test_the_search_asks_for_2017_onwards(self, watch):
        cfg, search = watch
        rules = cfg.rules_for(search)["filters"]
        assert rules.get("min_year") == 2017
        assert not rules.get("max_year")
        assert rules.get("models") == ["Civic Type R"]

    def test_the_link_asks_for_the_same_years_as_the_rule(self, watch):
        """The platform ignores the link's year, but if it ever stops ignoring
        it the two must not disagree - a link capped at 2017 under a rule that
        starts at 2017 would return exactly one model year."""
        from autotrader.urls import describe_search
        cfg, search = watch
        rules = cfg.rules_for(search)["filters"]
        asked = describe_search(search.url)
        assert asked.year_min == rules["min_year"]
        assert asked.year_max is None

    def test_the_example_config_pins_the_model_of_every_search(self, tmp_path):
        """What a new watch is copied from. A search without a models rule is
        the one this whole file is about: it quietly fills up with other cars."""
        from .helpers import EXAMPLE_CONFIG
        target = tmp_path / "config.json"
        target.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
        cfg = Config.load(target)
        assert cfg.active_searches
        for search in cfg.active_searches:
            models = cfg.rules_for(search)["filters"].get("models")
            assert models, f"{search.name} does not say which car it is for"


class TestARealRunDropsThem:
    """Through the runner, against its fixture site.

    The parts above test the rule; this tests that nothing downstream of it
    keeps the car anyway - not the enrichment pass, not the state ledger, not
    the removal path, and not the bookkeeping audit.

    Driven from `search_next_data.html`, which is the only search fixture the
    bot's primary strategy reads: nineteen cars, every one of them with a
    model on it. The anchor and regex fixtures deliberately carry none, which
    is a different test entirely - see TestWhenTheParserNamesNothing.
    """

    def named_site(self, fixture_html):
        """The same page, read the way a page is read when it does not name
        its own results.

        The models rule only acts on an unconfined read - where the page has
        named its results, its own list is the answer, and the rule steps
        aside rather than risk discarding one of them over a free-text
        model field. So the fixture's results key is renamed to something the
        parser does not recognise, which is precisely the situation the rule
        is kept for: a page whose shape has moved on.
        """
        return (fixture_html("search_next_data")
                .replace('"listings"', '"aKeyWeDoNotKnow"')
                .replace('"searchResults"', '"someOtherKey"'))

    def a_model_rule_for(self, bench, *models):
        search = list(bench.cfg.active_searches)[0]
        raw = [s for s in bench.cfg.data["searches"] if s["id"] == search.id][0]
        raw.setdefault("filters", {})["models"] = list(models)
        bench.cfg.save()
        return search

    def test_nothing_the_rule_turns_away_reaches_state(self, bench, fixture_html):
        site = self.named_site(fixture_html)
        bench.run(search_html=site)
        assert State.load(bench.path / "state.json").listings
        self.a_model_rule_for(bench, "Nothing At All")
        report = bench.run(search_html=site, minutes_later=180)
        assert State.load(bench.path / "state.json").listings == {}
        assert report.discarded > 0

    def test_the_run_says_how_many_and_for_which_search(self, bench, fixture_html):
        site = self.named_site(fixture_html)
        bench.run(search_html=site)
        search = self.a_model_rule_for(bench, "Nothing At All")
        report = bench.run(search_html=site, minutes_later=180)
        assert report.not_this_car.get(search.name)
        assert any("were a different model" in w for w in report.warnings)

    def test_dropping_them_is_not_a_removal_alert(self, bench, fixture_html):
        """Hundreds of cars leaving state in one run must not read as sales."""
        site = self.named_site(fixture_html)
        bench.run(search_html=site)
        bench.sink.digests.clear()
        self.a_model_rule_for(bench, "Nothing At All")
        report = bench.run(search_html=site, minutes_later=180)
        assert report.removed == 0
        announced = [c.kind for digest in bench.sink.digests for c in digest]
        assert "removed" not in announced

    def test_the_bookkeeping_still_adds_up(self, bench, fixture_html):
        site = self.named_site(fixture_html)
        bench.run(search_html=site)
        self.a_model_rule_for(bench, "Nothing At All")
        assert bench.run(search_html=site, minutes_later=180).invariants == []

    def test_keeping_the_right_model_keeps_its_cars(self, bench, fixture_html):
        site = self.named_site(fixture_html)
        bench.run(search_html=site)
        models = {str(e.get("model") or "") for e in
                  State.load(bench.path / "state.json").listings.values()}
        models.discard("")
        assert models, "the fixture site named no models"
        self.a_model_rule_for(bench, *sorted(models))
        report = bench.run(search_html=site, minutes_later=180)
        assert report.discarded == 0
        assert State.load(bench.path / "state.json").listings


class TestWhenTheParserNamesNothing:
    """The failure this rule could cause, and the guard against it.

    Two of the four parser strategies fill in no model. If the primary one
    falls behind the site and the bot drops to an anchor read, a model rule
    would match nothing, turn away every result, and discard every stored car
    - emptying the watch because of a parser fault, and looking exactly like a
    search that has gone quiet.
    """

    def test_the_rule_stands_down_rather_than_emptying_the_watch(self, bench, fixture_html):
        anchors = fixture_html("search_cards")          # no model on any row
        bench.run(search_html=anchors)
        before = dict(State.load(bench.path / "state.json").listings)
        assert before
        search = list(bench.cfg.active_searches)[0]
        raw = [s for s in bench.cfg.data["searches"] if s["id"] == search.id][0]
        raw.setdefault("filters", {})["models"] = ["Nothing At All"]
        bench.cfg.save()
        report = bench.run(search_html=anchors, minutes_later=180)
        assert report.discarded == 0
        assert set(State.load(bench.path / "state.json").listings) == set(before)

    def test_and_says_why_it_stood_down(self, bench, fixture_html):
        anchors = fixture_html("search_cards")
        bench.run(search_html=anchors)
        search = list(bench.cfg.active_searches)[0]
        raw = [s for s in bench.cfg.data["searches"] if s["id"] == search.id][0]
        raw.setdefault("filters", {})["models"] = ["Nothing At All"]
        bench.cfg.save()
        report = bench.run(search_html=anchors, minutes_later=180)
        assert any("standing down" in w for w in report.warnings)
        assert any("doctor --live" in w for w in report.warnings)

    def test_one_unnamed_car_among_named_ones_is_still_judged(self):
        """The question is whether the field is being read, not whether one
        row happens to be blank."""
        cars = [car(id="named", model="Civic Type R"), car(id="blank", model="")]
        mine, theirs = filters.not_this_car(cars, {"models": ["Civic Type R"]})
        assert [l.id for l in mine] == ["named"]
        assert [l.id for l, _ in theirs] == ["blank"]

    def test_a_batch_with_no_models_at_all_is_left_alone(self):
        cars = [car(id="a", model=""), car(id="b", model=None)]
        mine, theirs = filters.not_this_car(cars, {"models": ["Civic Type R"]})
        assert len(mine) == 2 and theirs == []
        kept, _, _, wrong = filters.apply(cars, {"models": ["Civic Type R"]})
        assert len(kept) == 2 and wrong == []

    def test_the_predicate_is_about_the_field_not_the_row(self):
        assert not filters.model_is_readable([])
        assert not filters.model_is_readable([car(model=""), car(model="  ")])
        assert filters.model_is_readable([car(model=""), car(model="Civic")])
