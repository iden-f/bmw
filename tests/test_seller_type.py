"""Dealer or private, which the bot has been publishing empty since day one.

`seller_type` has been on the Listing dataclass and in the published payload
since the rebuild, and nothing ever set it: 251 live listings, every one of
them the empty string. The answer was in the same JSON the parser was already
reading, in both of the strategies that actually win on this site.

It matters to a buyer. A dealer and a private seller are different
negotiations, different paperwork and different pricing behaviour, and a
filter that cannot tell them apart is the filter a person reaches for first.

The instructive half of this is the near miss: the schema.org path
(`"seller": {"@type": "AutoDealer"}`) was easy to find and fix, and fixing
only that would have changed nothing at all in production - live runs win with
`embedded_json`, where it is nested a level down as
`"seller": {"dealer": {...}, "type": "Dealer"}`. A fix that looks right
against the convenient fixture and does nothing against the real one is worse
than no fix, because it closes the ticket.
"""

from __future__ import annotations

import collections
from pathlib import Path

import pytest

from autotrader.parser import _seller_kind, parse_search_page

BASE = "https://www.autotrader.ca/cars/honda/civic/"
FIXTURES = Path("tests/fixtures")


def parse(name):
    return parse_search_page((FIXTURES / name).read_text(errors="ignore"), BASE)


class TestAgainstTheRealPages:
    @pytest.mark.parametrize("fixture,strategy", [
        ("search_next_data.html", "embedded_json"),
        ("search_2026_full.html", "jsonld"),
        ("search_2026_platform.html", "jsonld"),
    ])
    def test_both_winning_strategies_fill_it(self, fixture, strategy):
        out = parse(fixture)
        assert out.strategy == strategy, "the fixture no longer parses that way"
        assert out.listings
        kinds = collections.Counter(l.seller_type for l in out.listings)
        assert "" not in kinds, f"{fixture}: {dict(kinds)}"
        assert set(kinds) <= {"dealer", "private"}

    def test_the_strategy_the_live_runs_use_is_covered(self):
        """The near miss, stated so it cannot recur quietly."""
        out = parse("search_next_data.html")
        assert out.strategy == "embedded_json"
        assert all(l.seller_type == "dealer" for l in out.listings)


class TestOnlyWhatThePageSays:
    @pytest.mark.parametrize("node,want", [
        ({"@type": "AutoDealer", "name": "Example Motors"}, "dealer"),
        ({"@type": "Organization"}, "dealer"),
        ({"@type": "Person"}, "private"),
        ({"type": "Dealer"}, "dealer"),
        ({"seller": {"type": "Dealer"}}, "dealer"),
        ({"sellerType": "PRIVATE"}, "private"),
        ({"isDealer": True}, "dealer"),
        ({"isDealer": False}, "private"),
    ])
    def test_it_reads_the_shapes_the_site_uses(self, node, want):
        assert _seller_kind(node) == want

    @pytest.mark.parametrize("node", [
        {}, None, "AutoDealer", [], {"name": "Smith Motors Ltd"},
        {"@type": "Thing"}, {"sellerType": ""},
    ])
    def test_anything_else_stays_empty_rather_than_guessing(self, node):
        """A name ending in "Motors Ltd" is a guess, and a wrong guess here
        tells someone to haggle with a franchise or to expect paperwork from
        a private seller."""
        assert _seller_kind(node) == ""

    def test_it_does_not_recurse_forever_on_a_self_referential_payload(self):
        node = {}
        node["seller"] = node
        try:
            _seller_kind(node)
        except RecursionError:
            pytest.fail("a malformed payload should not take the run with it")


class TestItReachesThePerson:
    def test_it_is_published(self):
        from autotrader.dashboard import LISTING_FIELDS
        assert "seller_type" in LISTING_FIELDS

    def test_the_page_shows_it(self):
        js = Path("docs/app.js").read_text()
        assert "seller_type" in js, (
            "parsed, published, and still invisible is where this started")


class TestPlaceNamesArriveShouted:
    """"NORTH YORK" is how the addresses come. "North york" is how the page
    spelled it back, for the life of the project: capitalize() on the whole
    string capitalises the first letter and lowercases everything else."""

    @pytest.mark.parametrize("given,want", [
        ("NORTH YORK", "North York"),
        ("MONTREAL", "Montreal"),
        ("ST CATHARINES", "St. Catharines"),
        ("TROIS-RIVIERES", "Trois-Rivieres"),
        ("SAINT-JEAN-SUR-RICHELIEU", "Saint-Jean-sur-Richelieu"),
        ("L'ANCIENNE-LORETTE", "L'Ancienne-Lorette"),
        # The province codes used to live in the exact-case table, which made
        # this one "Niagara-ON-the-Lake".
        ("NIAGARA-ON-THE-LAKE", "Niagara-on-the-Lake"),
        ("LAC-DES-ILES", "Lac-des-Iles"),
    ])
    def test_every_word_not_just_the_first(self, given, want):
        from autotrader.parser import _titlecase_place
        assert _titlecase_place(given) == want

    @pytest.mark.parametrize("given", ["Richmond Hill", "North York", "Milton"])
    def test_a_name_that_is_already_cased_is_left_alone(self, given):
        from autotrader.parser import _titlecase_place
        assert _titlecase_place(given) == given

    def test_the_province_field_does_not_go_through_it(self):
        """It titlecases a city; "ON" in one is the word "on"."""
        from autotrader.parser import parse_search_page
        out = parse_search_page(
            (FIXTURES / "search_next_data.html").read_text(errors="ignore"), BASE)
        provinces = {l.province for l in out.listings if l.province}
        assert provinces and all(p == p.upper() or len(p) > 2 for p in provinces), \
            provinces
