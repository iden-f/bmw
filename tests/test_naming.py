"""What a car is called, everywhere a person reads it.

This exists because of one screenshot. The Feed, whose entire job is to be
readable at a glance, was forty rows of:

    2025 AutoTrader listing 00000000-0000-4000-8000-000000000001

Nothing had failed. The parser writes that placeholder when a results card
carries no readable name, enrichment then fills in year, make, model and trim,
and nothing ever goes back to fix the title. It showed up on hidden cars
because those are deliberately never enriched from their own detail pages - so
the rows a person scrolls past fastest were the ones named worst.
"""

from __future__ import annotations

import pytest

from autotrader import insight
from autotrader.config import Config
from autotrader.dashboard import build_payload
from autotrader.listing import Listing, name_of
from autotrader.state import State


class TestNamingACarFromAStateRow:
    def test_a_placeholder_is_replaced_by_what_we_know(self):
        assert name_of({"id": "00000000", "title": "AutoTrader listing 00000000",
                        "year": 2025, "make": "Honda", "model": "Civic"}) == "2025 Honda Civic"

    def test_a_real_title_keeps_the_dealers_words(self):
        """Dealer titles carry detail no reconstruction has - but only up to
        the first pipe, after which it is a feature list, and with the year in
        front because a card without one is a car of unknown age."""
        assert name_of({"id": "x", "year": 2019, "make": "Honda", "model": "Civic",
                        "title": "Honda Civic Type R | Carbon roof | One owner"
                        }) == "2019 Honda Civic Type R"

    def test_a_title_that_already_opens_with_the_year_gets_no_second_one(self):
        assert name_of({"id": "x", "year": 2018, "make": "Honda", "model": "Civic",
                        "title": "2018 Honda Civic Si"
                        }) == "2018 Honda Civic Si"

    def test_a_car_we_know_nothing_about_keeps_its_placeholder(self):
        """Better an id than a confident lie about which car this is."""
        row = {"id": "x", "title": "AutoTrader listing x"}
        assert name_of(row) == "AutoTrader listing x"

    def test_the_trim_is_included_when_it_reads_as_a_name(self):
        assert name_of({"id": "x", "title": "", "year": 2020, "make": "Honda",
                        "model": "Civic", "trim": "Type R"}) == \
            "2020 Honda Civic Type R"


class TestTrimsDealersActuallyType:
    def name(self, trim: str) -> str:
        return Listing(id="x", year=2019, make="Honda", model="Civic",
                       trim=trim).display_title

    def test_a_capital_i_used_as_a_pipe_is_a_separator(self):
        """"Type R I Tech PKG I Winter Tire PKG" is how some dealers type a
        list."""
        assert self.name("Type R I Tech PKG I Winter Tire") == \
            "2019 Honda Civic Type R"

    def test_a_long_trim_is_cut_at_a_word(self):
        """"...Every Option Li" reads as a rendering fault, and would be one."""
        out = self.name("Touring Premium Enhanced Package With Every Option Listed")
        assert len(out) <= 60 and not out.endswith(("Ca", "Op", "List"))
        assert out.split()[-1] in {"With", "Every", "Package", "Enhanced", "Option"}

    def test_a_trim_that_starts_on_its_own_separator(self):
        """A trim like "I Tech PKG I Winter Tire PKG Remote Start" must not
        name a car "2025 Honda Civic I Tech PKG"."""
        assert self.name("I Tech PKG I Winter Tire PKG") == \
            "2019 Honda Civic Tech PKG"

    def test_a_leading_pipe_is_not_part_of_the_name(self):
        assert self.name("| Type R | Carbon roof") == "2019 Honda Civic Type R"

    def test_a_trim_that_merely_starts_with_i_is_not_mangled(self):
        assert self.name("I4 Turbo") == "2019 Honda Civic I4 Turbo"

    def test_a_trim_that_is_just_the_model_again_is_dropped(self):
        """make Honda, model Civic, trim Civic would read "2010 Honda Civic
        Civic"."""
        assert Listing(id="x", year=2010, make="Honda", model="Civic",
                       trim="Civic").display_title == "2010 Honda Civic"

    def test_a_star_separated_feature_list_is_cut_at_the_first_star(self):
        """A trim like "Touring * NO ACCIDENTS * ONE OWNER * CERTIFIED"."""
        assert self.name("Touring * NO ACCIDENTS * ONE OWNER") == \
            "2019 Honda Civic Touring"

    def test_a_short_trim_is_untouched(self):
        assert self.name("Type R") == "2019 Honda Civic Type R"

    def test_a_trim_with_no_spaces_is_still_cut_rather_than_dropped(self):
        assert self.name("A" * 80).startswith("2019 Honda Civic A")


class TestEverywhereItIsRead:
    def bench(self, tmp_path):
        cfg = Config.defaults(tmp_path / "config.json")
        search = cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=25",
                                "Civic")
        state = State(path=tmp_path / "state.json")
        state.record(Listing(id="nameless", title="AutoTrader listing nameless",
                             year=2025, make="Honda", model="Civic", price=28000,
                             price_source="search", search_id=search.id),
                     filtered=True, filter_reason="year 2025 above maximum 2020")
        return cfg, state

    def test_the_feed_never_shows_a_placeholder(self, tmp_path):
        _, state = self.bench(tmp_path)
        events = insight.events(state.listings.values())
        assert events and all(not e["title"].startswith("AutoTrader listing")
                              for e in events), [e["title"] for e in events]
        assert events[0]["title"] == "2025 Honda Civic"

    def test_the_published_listing_never_shows_one_either(self, tmp_path):
        cfg, state = self.bench(tmp_path)
        payload = build_payload(cfg, state, {})
        assert payload["listings"][0]["title"] == "2025 Honda Civic"

    def test_state_keeps_what_was_really_parsed(self, tmp_path):
        """The published name is a presentation choice; the record is a record."""
        _, state = self.bench(tmp_path)
        assert state.listings["nameless"]["title"] == "AutoTrader listing nameless"


class TestThePageAndTheAlertAgree:
    """The naming bug was a fork in the road, not a broken function.

    render.py builds a notification from a Listing and reads
    ``listing.display_title``, which composes a name from year, make and model
    - so alerts were always right. dashboard.py and insight.py build the page
    from state ROWS, which are dicts, and read ``entry["title"]`` - so the
    page was always wrong. Two answers to "what is this car called", one of
    them correct, and the difference invisible because nobody compares a push
    notification to a web page.
    """

    def car(self) -> Listing:
        return Listing(id="00000000-0000-4000-8000-000000000001",
                       title="AutoTrader listing 00000000-0000-4000-8000-000000000001",
                       year=2025, make="Honda", model="Civic", price=28000,
                       price_source="detail")

    def test_they_produce_the_same_name(self):
        listing = self.car()
        assert name_of(listing.to_dict()) == listing.display_title

    def test_and_for_a_car_with_a_real_title(self):
        listing = Listing(id="x", title="Honda Civic Type R | One owner",
                          year=2019, make="Honda", model="Civic")
        assert name_of(listing.to_dict()) == listing.display_title

    def test_and_for_a_car_nothing_is_known_about(self):
        listing = Listing(id="deadbeef", title="AutoTrader listing deadbeef")
        assert name_of(listing.to_dict()) == listing.display_title

    def test_the_notification_never_said_the_placeholder(self, tmp_path):
        """Worth pinning: the alerts were the half that was right."""
        from autotrader import render
        from autotrader.state import Change
        text = render.as_text([Change(kind=Change.NEW, listing=self.car())])
        assert "AutoTrader listing" not in text, text
        assert "2025 Honda Civic" in text, text


class TestOneSeparatorListNotTwo:
    """A dealer's feature list, wherever they type one.

    short_trim cut a TRIM at five separators. The TITLE was cut at a pipe and
    nothing else, so a dealer who types slashes put "2020 HONDA CIVIC TYPE R
    SPORT PKG / WINTER TIRES / REMOTE START / LOW KMS DEALER SERV" across the
    Feed - seventy characters of marketing copy, truncated mid-word by the
    column it landed in. Both are the same rule and there is now one of it.
    """

    CASES = [
        ("2020 HONDA CIVIC TYPE R SPORT PKG / WINTER TIRES / REMOTE START / LOW KMS DEALER SERV",
         "2020 HONDA CIVIC TYPE R SPORT PKG"),
        ("Honda Civic | Sport | Carbon", "2020 Honda Civic"),
        ("Honda Civic I Sport PKG I Carbon", "2020 Honda Civic"),
        ("Touring * NO ACCIDENTS * ONE OWNER", "2020 Touring"),
        ("Honda Civic Type R, Track Package", "2020 Honda Civic Type R"),
        # Not a feature list. An unspaced slash is part of a word.
        ("Honda Civic w/ Sport Package", "2020 Honda Civic w/ Sport Package"),
    ]

    @pytest.mark.parametrize("title,want", CASES)
    def test_the_title_is_cut_where_the_trim_would_be(self, title, want):
        from autotrader.listing import name_of
        got = name_of({"id": "1", "title": title, "year": 2020,
                       "make": "Honda", "model": "Civic"})
        assert got == want

    def test_the_trim_uses_the_very_same_list(self):
        from autotrader.listing import FEATURE_LIST_SEPARATORS, Listing
        for sep in FEATURE_LIST_SEPARATORS:
            listing = Listing(id="1", url="u", make="Honda", model="Civic",
                              trim=f"Type R{sep}Premium Package")
            assert listing.short_trim == "Type R", sep

    def test_the_page_does_not_keep_its_own_copy_of_the_rule(self):
        """It reads the name the bot published. A second implementation is a
        second chance to know about fewer separators than the first."""
        from pathlib import Path
        source = (Path(__file__).resolve().parent.parent
                  / "docs" / "app.js").read_text()
        start = source.index("function carName(")
        body = source[start:source.index("}", source.index("return", start))]
        for sep in ("'|'", '"|"', "split("):
            assert sep not in body, f"carName is deriving the name again: {sep}"
