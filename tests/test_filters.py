import pytest

from autotrader.filters import apply, check, is_significant_drop
from autotrader.listing import Listing


def car(**kw):
    base = dict(id="1", url="u", title="2021 Honda Civic Touring", year=2021,
                make="Honda", model="Civic", price=38500, mileage_km=52000,
                seller="Big Dealer Inc", color="Alpine White")
    base.update(kw)
    return Listing(**base)


def test_no_filters_keeps_everything():
    assert check(car(), None).keep and check(car(), {}).keep


def test_price_and_year_bounds():
    assert not check(car(), {"max_price": 35000}).keep
    assert not check(car(), {"min_price": 45000}).keep
    assert not check(car(), {"min_year": 2022}).keep
    assert not check(car(), {"max_year": 2020}).keep
    assert check(car(), {"min_price": 35000, "max_price": 40000, "min_year": 2020}).keep


def test_a_rejection_explains_itself():
    verdict = check(car(), {"max_price": 35000})
    assert "$38,500" in verdict.reason and "$35,000" in verdict.reason


def test_cars_with_no_price_are_kept_unless_asked_otherwise():
    assert check(car(price=None), {"max_price": 35000}).keep
    assert not check(car(price=None), {"require_price": True}).keep


def test_keyword_rules():
    assert not check(car(), {"exclude_keywords": ["touring"]}).keep
    assert not check(car(), {"include_keywords": ["manual"]}).keep
    assert check(car(), {"include_keywords": ["touring", "manual"]}).keep


def test_keyword_matching_ignores_case():
    assert not check(car(), {"exclude_keywords": ["TOURING"]}).keep


def test_seller_exclusion_is_a_substring_match():
    assert not check(car(), {"exclude_sellers": ["big dealer"]}).keep
    assert check(car(), {"exclude_sellers": ["other dealer"]}).keep


def test_blank_filter_entries_are_ignored():
    assert check(car(), {"exclude_keywords": ["", "  "], "include_keywords": []}).keep


def test_apply_splits_and_reports_reasons():
    kept, unpriced, dropped, _ = apply([car(id="1"), car(id="2", price=60000)],
                                    {"max_price": 40000})
    assert [l.id for l in kept] == ["1"]
    assert unpriced == []
    assert dropped[0][0].id == "2" and "above maximum" in dropped[0][1].reason
    # And which rule said so, as its config key - the settings page reads this.
    assert dropped[0][1].rule == "max_price"


def test_a_car_with_no_price_gets_its_own_bucket():
    """require_price stops hiding cars and starts categorising them."""
    kept, unpriced, dropped, _ = apply(
        [car(id="1"), car(id="2", price=None)], {"require_price": True})

    assert [l.id for l in kept] == ["1"]
    assert [l.id for l in unpriced] == ["2"]
    assert dropped == []


def test_a_car_excluded_for_another_reason_is_not_called_unpriced():
    """The reason reported has to be the real one."""
    verdict = check(car(price=None, title="2020 Honda Civic salvage"),
                    {"require_price": True, "exclude_keywords": ["salvage"]})
    assert not verdict.keep and not verdict.unpriced
    assert "salvage" in verdict.reason


def test_without_require_price_an_unpriced_car_is_simply_kept():
    kept, unpriced, dropped, _ = apply([car(price=None)], {"max_price": 40000})
    assert len(kept) == 1 and not unpriced and not dropped


def test_a_drop_must_clear_both_thresholds():
    assert is_significant_drop(100000, 95000, 1.0, 250)
    assert not is_significant_drop(100000, 99900, 1.0, 250)   # too small in dollars
    assert not is_significant_drop(100000, 99500, 1.0, 250)   # too small in percent
    assert not is_significant_drop(100000, 105000, 1.0, 250)  # not a drop
    assert not is_significant_drop(0, 0, 1.0, 250)


class TestEveryRejectionNamesItsRule:
    """The reason is written for a person; the rule is written for the
    settings page, which has four of these eleven on screen.

    Without it the page had two bad options: re-implement the other seven in
    JavaScript, or report them as passing. It reported them as passing.
    """

    RULES = {
        "min_price": ({"min_price": 50_000}, dict(price=10_000)),
        "max_price": ({"max_price": 50_000}, dict(price=90_000)),
        "min_year": ({"min_year": 2020}, dict(year=2015)),
        "max_year": ({"max_year": 2015}, dict(year=2020)),
        "max_mileage_km": ({"max_mileage_km": 1000}, dict(mileage_km=90_000)),
        "provinces": ({"provinces": ["QC"]}, dict(province="ON")),
        "max_distance_km": ({"near": "Ottawa, ON", "max_distance_km": 300},
                            dict(location="Toronto", province="ON")),
        "include_keywords": ({"include_keywords": ["manual"]}, {}),
        "exclude_keywords": ({"exclude_keywords": ["salvage"]},
                             dict(title="salvage title Civic")),
        "exclude_sellers": ({"exclude_sellers": ["acme"]},
                            dict(seller="Acme Motors")),
        "require_price": ({"require_price": True}, dict(price=None)),
    }

    @pytest.mark.parametrize("rule", sorted(RULES))
    def test_it_says_which_rule(self, rule):
        filters_, overrides = self.RULES[rule]
        verdict = check(car(**overrides), filters_)
        assert not verdict.keep, rule
        assert verdict.rule == rule, (rule, verdict.rule, verdict.reason)

    def test_a_kept_car_names_no_rule(self):
        assert check(car(), {"max_price": 500_000}).rule == ""

    def test_the_settings_page_knows_which_four_it_shows(self):
        """The editor's four boxes, read out of the page, against the rules
        the engine actually has. A fifth box added to the page without a
        rule behind it would silently do nothing."""
        import re
        from pathlib import Path
        source = (Path(__file__).resolve().parent.parent
                  / "docs" / "app.js").read_text()
        line = re.search(r"const rule = \{([^}]*)\}", source)
        assert line, "the rules editor no longer builds a rule object"
        shown = set(re.findall(r"(\w+):", line.group(1)))
        assert shown <= set(self.RULES), shown - set(self.RULES)
        # And the page must hold out the ones it does not show, rather than
        # counting them as passing.
        assert "l.filtered && !(l.filter_rule in rule)" in source
