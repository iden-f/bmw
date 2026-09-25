"""autotrader.ca moved onto the AutoScout24 platform in 2026.

Listing URLs changed from /a/<make>/<model>/<city>/<province>/<n>_<id>_<ref>/
to /offers/<slug>-<uuid>, the page became a Next.js app, and schema.org @type
started arriving as a list. Every one of those broke the scraper silently; the
fixture here is built from a real captured page.
"""
import pytest

from autotrader.enrich import detail_from_html
from autotrader.listing import Listing
from autotrader.parser import parse_search_page
from autotrader.urls import (canonical_listing_url, describe_search, is_offer_url,
                             listing_id_from_url)
from autotrader.validate import assess

BASE = "https://www.autotrader.ca/cars/honda/civic"
OFFER = ("https://www.autotrader.ca/offers/honda-civic-type-r-sport-aide-a-la-"
         "conduite-avance-gasoline-blue-cat_ma00gr000000tr00000-"
         "00000000-0000-4000-8000-000000000001")
UUID = "00000000-0000-4000-8000-000000000001"
# One of the cars on the captured page, which the parser tests read back.
CAPTURED = "f3b59e36-c73b-4e58-88c4-746af0d18a45"


class TestNewUrlScheme:
    def test_the_trailing_uuid_is_the_listing_id(self):
        assert listing_id_from_url(OFFER) == UUID

    def test_the_shared_campaign_token_is_not_mistaken_for_an_id(self):
        """Every result on a page carries the same cat_ma13gr... segment."""
        other = OFFER.replace(UUID, "00000000-0000-4000-8000-000000000002")
        assert listing_id_from_url(other) != listing_id_from_url(OFFER)

    def test_the_fragment_is_ignored(self):
        assert listing_id_from_url(OFFER + "#vehicle") == UUID

    def test_uuids_are_normalised_to_lower_case(self):
        assert listing_id_from_url(OFFER.replace(UUID, UUID.upper())) == UUID

    def test_the_old_scheme_still_resolves(self):
        """Listings stored under the old scheme must keep their identity."""
        old = "https://www.autotrader.ca/a/honda/civic/winnipeg/manitoba/19_10000001_/"
        assert listing_id_from_url(old) == "10000001"
        assert not is_offer_url(old)

    def test_an_offer_link_is_recognised_as_a_listing_not_a_search(self):
        assert is_offer_url(OFFER)
        summary = describe_search(OFFER)
        assert not summary.valid
        assert "single car listing" in " ".join(summary.problems)

    def test_tracking_parameters_are_stripped(self):
        assert canonical_listing_url(OFFER + "?utm_source=email") == OFFER

    def test_a_slug_without_a_uuid_is_not_a_listing(self):
        assert listing_id_from_url("https://www.autotrader.ca/offers/") is None


class TestNewSearchPage:
    def test_the_search_page_is_read_from_its_structured_data(self, fixture_html):
        result = parse_search_page(fixture_html("search_2026_platform"), BASE)
        assert result.strategy == "jsonld"
        assert len(result) == 3

    def test_a_type_given_as_a_list_still_matches(self, fixture_html):
        """@type is ["Car","Product"] now; str() on that matched nothing."""
        result = parse_search_page(fixture_html("search_2026_platform"), BASE)
        assert result.candidates["jsonld"] == 3

    def test_every_field_that_matters_is_recovered(self, fixture_html):
        result = parse_search_page(fixture_html("search_2026_platform"), BASE)
        car = next(l for l in result.listings if l.id == CAPTURED)
        assert car.price == 125669 and car.currency == "CAD"
        assert car.mileage_km == 27314
        assert car.make == "BMW" and car.model == "M5"
        assert car.transmission == "Automatic"
        assert car.fuel == "Gasoline"
        assert is_offer_url(car.url) and listing_id_from_url(car.url) == CAPTURED
        assert canonical_listing_url(car.url) == car.url

    def test_the_location_comes_from_the_seller_address(self, fixture_html):
        """The new URLs have no city segment; the offer carries an address."""
        result = parse_search_page(fixture_html("search_2026_platform"), BASE)
        car = next(l for l in result.listings if l.id == CAPTURED)
        assert car.location == "Montréal"
        assert car.province == "QC"
        assert car.seller == "BMW MINI Montréal Centre"

    def test_shouted_dealer_cities_are_tidied(self, fixture_html):
        result = parse_search_page(fixture_html("search_2026_platform"), BASE)
        assert all(l.location != l.location.upper() or len(l.location) <= 2
                   for l in result.listings if l.location)

    def test_photos_are_recovered(self, fixture_html):
        result = parse_search_page(fixture_html("search_2026_platform"), BASE)
        for listing in result.listings:
            assert listing.images
            assert all(u.startswith("http") for u in listing.images)

    def test_prices_are_plausible(self, fixture_html):
        result = parse_search_page(fixture_html("search_2026_platform"), BASE)
        for listing in result.listings:
            assert 10_000 < listing.price < 500_000

    def test_the_breadcrumb_block_does_not_produce_phantom_listings(self, fixture_html):
        """The page also carries a BreadcrumbList of ListItems."""
        result = parse_search_page(fixture_html("search_2026_platform"), BASE)
        assert all(l.id != "" and len(l.id) > 8 for l in result.listings)

    def test_this_parse_passes_the_first_run_check(self, fixture_html):
        result = parse_search_page(fixture_html("search_2026_platform"), BASE)
        verdict = assess(result.listings, result.strategy, "Example search",
                         candidates=result.candidates)
        assert verdict.trustworthy, verdict.concerns


class TestReadableOutput:
    def test_a_pipe_stuffed_trim_is_cut_down(self):
        listing = Listing(id="1", url="u", make="Honda", model="Civic",
                          trim="Type R | Premium | Aide a la conduite avance")
        assert listing.short_trim == "Type R"
        assert listing.display_title == "Honda Civic Type R"

    def test_a_comma_stuffed_trim_is_cut_down(self):
        listing = Listing(id="1", url="u", make="Honda", model="Civic",
                          trim="Touring Package, Track Package!")
        assert listing.display_title == "Honda Civic Touring Package"

    def test_a_plain_trim_is_left_alone(self):
        listing = Listing(id="1", url="u", year=2022, make="Honda", model="Civic",
                          trim="Type R")
        assert listing.display_title == "2022 Honda Civic Type R"

    def test_an_absurdly_long_trim_is_bounded(self):
        listing = Listing(id="1", url="u", make="Honda", model="Civic", trim="x" * 300)
        assert len(listing.short_trim) <= 40


class TestOldArchivesStillParse:
    def test_the_previous_platform_detail_pages_still_read(self, archive_html):
        """The 50 archived pages predate the migration and must keep working."""
        detail = detail_from_html(archive_html("68819631"), "68819631")
        assert detail.price == 102199 and detail.mileage_km == 30244
        assert detail.year == 2022


class TestHostileValueShapes:
    """The front-end state nests numbers and places inside objects and lists."""

    @pytest.mark.parametrize("value,expected", [
        (34500, 34500),
        ("34,500", 34500),
        ("$31,200", 31200),
        (31200.0, 31200),
        ({"amount": 34500, "currency": "CAD"}, 34500),
        ({"consumerPrice": {"amount": 29900}}, 29900),
        ([33900, 33800, 33950], 33900),
        ({"nothing": "here"}, None),
        ([], None), (None, None), (True, None), (0, None), ("", None),
    ])
    def test_numbers_are_unwrapped_not_stringified(self, value, expected):
        from autotrader.parser import _to_int
        assert _to_int(value) == expected

    def test_a_list_of_prices_does_not_become_one_absurd_number(self):
        """str([33900, 33800, 33950]) once produced $33,900,33,800,33,950."""
        from autotrader.parser import _to_int
        assert _to_int([33900, 33800, 33950]) == 33900

    def test_deeply_nested_junk_terminates(self):
        from autotrader.parser import _to_int
        nest = {"amount": {"amount": {"amount": {"amount": {"amount": {"amount": 5}}}}}}
        assert _to_int(nest) in (5, None)   # bounded, and never raises

    @pytest.mark.parametrize("value,city,province", [
        ({"countryCode": "CA", "provinceCode": "AB", "city": "CALGARY",
          "street": "34 Heritage Meadows Rd. SE"}, "Calgary", "AB"),
        ({"addressLocality": "MONTRÉAL", "addressRegion": "QC"}, "Montréal", "QC"),
        ("Toronto", "Toronto", ""),
        (None, "", ""), (42, "", ""), ({}, "", ""),
    ])
    def test_places_are_unwrapped_not_stringified(self, value, city, province):
        from autotrader.parser import _place_from
        assert _place_from(value) == (city, province)

    def test_a_location_object_never_reaches_a_notification(self):
        """A raw dict once appeared verbatim in the alert text."""
        from autotrader.parser import _place_from
        rendered = _place_from({"countryCode": "CA", "provinceCode": "AB",
                                "zip": "T2H3C1", "city": "CALGARY"})
        assert "{" not in "".join(rendered)
        assert "countryCode" not in "".join(rendered)


class TestImplausibleValuesAreDropped:
    def _listing(self, price=None, mileage=None):
        from autotrader.parser import _listing_from_json
        node = {"@type": ["Car", "Product"], "name": "Honda Civic",
                "url": OFFER, "offers": {"price": price, "priceCurrency": "CAD"}}
        if mileage is not None:
            node["mileageFromOdometer"] = {"value": mileage}
        return _listing_from_json(node, BASE)

    @pytest.mark.parametrize("price", [1, 499, 34_500_34_500, 33_900_33_800_33_950])
    def test_an_impossible_price_is_discarded(self, price):
        assert self._listing(price=price).price is None

    @pytest.mark.parametrize("price", [500, 18_000, 42_000, 4_999_999])
    def test_a_plausible_price_is_kept(self, price):
        assert self._listing(price=price).price == price

    def test_an_impossible_odometer_is_discarded(self):
        assert self._listing(price=30000, mileage=5_121_400).mileage_km is None

    def test_dropping_a_bad_price_does_not_drop_the_listing(self):
        """A car with an unreadable price is still a car worth telling you about."""
        listing = self._listing(price=34_500_34_500)
        assert listing is not None and listing.id == UUID
        assert listing.price_text == "Price not listed"


class TestTheFallbackLadderOnTheCurrentPlatform:
    """All four strategies must score, or a search runs on one parser.

    ``anchors`` matched ``/a/`` links and ``regex`` matched the old
    ``<n>_<id>_<ref>`` path, so after the migration both returned zero on every
    run. Nothing failed - jsonld and embedded_json carried it - which is
    exactly why it went unnoticed: the ladder had quietly lost two rungs.
    """

    def test_every_strategy_scores_on_a_real_captured_page(self, fixture_html):
        result = parse_search_page(fixture_html("search_2026_full"), BASE)
        assert all(result.candidates[name] > 0 for name in
                   ("jsonld", "embedded_json", "anchors", "regex")), result.candidates

    def test_anchors_reads_a_card_with_no_json_on_the_page_at_all(self, fixture_html):
        """Real card markup, JSON-LD and __NEXT_DATA__ stripped out."""
        result = parse_search_page(fixture_html("search_2026_cards"), BASE)

        assert result.candidates["jsonld"] == 0
        assert result.candidates["embedded_json"] == 0
        assert result.strategy == "anchors"
        assert len(result.listings) == 3

        car = next(l for l in result.listings
                   if l.id == "a5f73b97-2d46-4fbf-9c1b-28b061887307")
        assert car.year == 2025
        assert car.price == 144900
        assert car.mileage_km == 13913
        assert (car.location, car.province) == ("Vancouver", "BC")
        assert car.seller == "Brian Jessel BMW Pre-Owned"
        assert car.transmission == "Automatic"

    def test_anchors_is_the_only_strategy_that_sees_both_year_and_seller(
            self, fixture_html):
        """Which is the argument for keeping it in the ladder, not just alive.

        The current JSON-LD carries no model year at all and the front-end
        blob carries no seller, so the card is the only place both appear.
        """
        from bs4 import BeautifulSoup
        from autotrader.parser import STRATEGIES

        html = fixture_html("search_2026_full")
        soup = BeautifulSoup(html, "html.parser")
        by_name = {name: fn(soup, html, BASE) for name, fn in STRATEGIES}

        anchors = by_name["anchors"]
        assert anchors and all(l.year for l in anchors)
        assert any(l.seller for l in anchors)
        assert not any(l.year for l in by_name["jsonld"])

    def test_anchors_never_reports_the_dealer_logo_as_the_car(self, fixture_html):
        """400 archived 'photos' once, and not one of them was of a vehicle."""
        result = parse_search_page(fixture_html("search_2026_cards"), BASE)
        for listing in result.listings:
            for image in listing.images:
                assert "dealer-info" not in image
                assert "listing-images" in image

    def test_regex_finds_every_car_when_nothing_else_can(self, fixture_html):
        """The last rung: ids and URLs out of raw text, no markup assumptions."""
        from bs4 import BeautifulSoup
        from autotrader.parser import _strategy_regex

        html = fixture_html("search_2026_cards")
        listings = _strategy_regex(BeautifulSoup(html, "html.parser"), html, BASE)

        assert len(listings) == 3
        assert all(l.url.startswith("https://www.autotrader.ca/offers/") for l in listings)
        assert all(is_offer_url(l.url) for l in listings)

    def test_the_ladder_still_reads_the_old_platform(self, fixture_html):
        """autohebdo.net and the archives are still on the previous markup."""
        result = parse_search_page(fixture_html("search_cards"), BASE)
        assert result.candidates["anchors"] > 0
        assert result.candidates["regex"] > 0

    def test_a_weaker_strategy_fills_in_what_the_winner_missed(self, fixture_html):
        """jsonld wins on real pages but has no year; the merge repairs that."""
        result = parse_search_page(fixture_html("search_2026_full"), BASE)
        assert result.strategy in ("jsonld", "embedded_json")
        with_year = [l for l in result.listings if l.year]
        assert len(with_year) == len(result.listings)


class TestTheAnchorStrategyReadsTheRightFigure:
    """A card carries several numbers and only one of them is the asking price."""

    def test_the_msrp_beside_the_price_is_not_mistaken_for_it(self):
        from bs4 import BeautifulSoup
        from autotrader.parser import _strategy_anchors

        card = ('<article><a href="/offers/honda-civic-' + "a" * 8 +
                '-1111-2222-3333-444444444444">go</a>'
                '<h2>2027 Honda Civic Touring</h2>'
                '<p data-testid="regular-price">$ 41,000</p>'
                '<p data-testid="suggested-retail-price">$ 44,000</p>'
                '</article>')
        html = f"<html><body>{card}</body></html>"
        listing = _strategy_anchors(BeautifulSoup(html, "html.parser"), html, BASE)[0]
        assert listing.price == 41000

    def test_a_renamed_price_field_is_still_found(self):
        """The exact test id can change; the fallback has to keep working."""
        from bs4 import BeautifulSoup
        from autotrader.parser import _strategy_anchors

        card = ('<article><a href="/offers/honda-civic-' + "b" * 8 +
                '-1111-2222-3333-444444444444">go</a>'
                '<h2>2020 Honda Civic</h2>'
                '<p data-testid="listing-price-v3">$ 23,000</p></article>')
        html = f"<html><body>{card}</body></html>"
        listing = _strategy_anchors(BeautifulSoup(html, "html.parser"), html, BASE)[0]
        assert listing.price == 23000

    def test_a_monthly_payment_is_never_read_as_the_price(self):
        from bs4 import BeautifulSoup
        from autotrader.parser import _strategy_anchors

        card = ('<article><a href="/offers/honda-civic-' + "c" * 8 +
                '-1111-2222-3333-444444444444">go</a>'
                '<h2>2019 Honda Civic</h2>'
                '<p data-testid="monthly-price">$ 399</p></article>')
        html = f"<html><body>{card}</body></html>"
        listing = _strategy_anchors(BeautifulSoup(html, "html.parser"), html, BASE)[0]
        assert listing.price != 399


class TestNewSearchLinks:
    """The search links the site hands out now spell every filter differently.

    A link copied from the 2026 address bar read as "any year, Canada-wide"
    under the old parameter names - so the dashboard would have said a watch
    covered the country and every model year, while the link itself asked for
    one city and one generation. The bot enforces year and distance itself, so
    nothing was over-fetched; the page just described the wrong search.
    """

    UP_TO = ("https://www.autotrader.ca/cars/honda/civic/reg_on/cit_toronto?body=2%2C5"
             "&offer=N%2CU&modelyearto=2018&cy=CA&damaged_listing=exclude&desc=1"
             "&sort=age&ustate=N%2CU&zip=Toronto&zipr=500&lat=43.65"
             "&lon=-79.38&atype=C&mcat=ma00gr000000&size=20")
    RANGE = ("https://www.autotrader.ca/cars/toyota/corolla/reg_on/cit_ottawa?offer=N%2CU"
             "&modelyearfrom=2012&modelyearto=2016&zip=Ottawa&zipr=500&size=20")
    TRIM = ("https://www.autotrader.ca/cars/honda/civic/va_civic-type-r/reg_on/cit_toronto"
            "?offer=N%2CU&modelyearto=2018&zip=Toronto&zipr=500&size=20")

    def test_the_year_range_is_read(self):
        assert describe_search(self.RANGE).year_min == 2012
        assert describe_search(self.RANGE).year_max == 2016

    def test_an_open_ended_range_stays_open(self):
        summary = describe_search(self.UP_TO)
        assert summary.year_min is None and summary.year_max == 2018
        assert summary.title() == "up to 2018 Honda Civic"

    def test_the_radius_is_not_mistaken_for_canada_wide(self):
        summary = describe_search(self.UP_TO)
        assert summary.radius_km == 500
        assert "near Toronto (500 km)" in summary.describe()
        assert "Canada-wide" not in summary.describe()

    def test_the_trim_segment_is_part_of_the_model(self):
        """/cars/honda/civic/va_civic-type-r is a Civic Type R. A Civic is a
        different car."""
        assert describe_search(self.TRIM).model == "Civic Type R"
        assert describe_search(self.TRIM).title() == "up to 2018 Honda Civic Type R"

    def test_the_province_comes_out_of_the_tagged_segment(self):
        assert describe_search(self.UP_TO).province == "ON"

    def test_body_codes_are_not_given_names_we_do_not_have(self):
        """body=2,5 has no published legend. Naming a code would put a
        confident wrong word on the dashboard; dropping it would let the page
        claim the search is wider than it is."""
        chips = describe_search(self.UP_TO).describe()
        assert "some body styles only" in chips
        assert not any("coupe" in c.lower() for c in chips)

    def test_a_spelled_out_body_style_still_reads_as_itself(self):
        old = "https://www.autotrader.ca/cars/honda/civic/?body=Coupe"
        assert "Coupe" in describe_search(old).describe()

    def test_excluding_damaged_listings_is_reported(self):
        assert "no damaged listings" in describe_search(self.UP_TO).describe()
        assert "no damaged listings" not in describe_search(self.RANGE).describe()

    def test_new_and_used_together_is_not_a_condition_worth_a_chip(self):
        assert describe_search(self.UP_TO).condition is None

    def test_the_city_in_the_path_is_used_when_nothing_was_typed(self):
        link = "https://www.autotrader.ca/cars/honda/civic/reg_on/cit_niagara-falls?zipr=50"
        assert describe_search(link).location == "Niagara-Falls"

    def test_the_link_is_still_recognised_as_a_search(self):
        for link in (self.UP_TO, self.RANGE, self.TRIM):
            assert describe_search(link).valid, link

