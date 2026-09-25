"""The scraper must survive AutoTrader changing its markup."""
import pytest

from autotrader.http import looks_blocked
from autotrader.parser import parse_search_page

BASE = "https://www.autotrader.ca/cars/honda/civic/"


def test_json_ld_is_preferred_when_present(fixture_html):
    result = parse_search_page(fixture_html("search_jsonld"), BASE)
    assert result.strategy == "jsonld"
    assert len(result) == 2
    car = next(l for l in result.listings if l.id == "13166607")
    assert car.price == 98995 and car.mileage_km == 52000
    assert car.make == "BMW" and car.year == 2021


def test_javascript_rendered_page_is_read_from_its_state_blob(fixture_html):
    result = parse_search_page(fixture_html("search_spa"), BASE)
    assert result.strategy == "embedded_json"
    car = next(l for l in result.listings if l.id == "68819631")
    assert car.price == 102199 and car.location == "St. Catharines"


def test_plain_html_cards_are_read_correctly(fixture_html):
    result = parse_search_page(fixture_html("search_cards"), BASE)
    assert result.strategy == "anchors"
    assert len(result) == 3
    by_id = {l.id: l for l in result.listings}
    assert by_id["13166607"].price == 98995
    assert by_id["68819631"].price == 102199


def test_a_card_never_borrows_its_neighbour_data(fixture_html):
    """The third card has no price and its own odometer; it must not pick up
    the first card's title or the second card's price."""
    result = parse_search_page(fixture_html("search_cards"), BASE)
    third = next(l for l in result.listings if l.id == "13221555")
    assert third.price is None                 # the card says "Please Call"
    assert third.mileage_km == 88100
    assert "2020" in third.title


def test_distance_from_you_is_not_mistaken_for_the_odometer(fixture_html):
    """Cards show 'Winnipeg - 3,726 km' (distance) then '52,000 km' (odometer)."""
    result = parse_search_page(fixture_html("search_cards"), BASE)
    car = next(l for l in result.listings if l.id == "13166607")
    assert car.mileage_km == 52000


def test_promotional_dollar_amounts_do_not_beat_the_asking_price(fixture_html):
    """The second card also advertises '$0 down financing'."""
    result = parse_search_page(fixture_html("search_cards"), BASE)
    assert next(l for l in result.listings if l.id == "68819631").price == 102199


def test_manufacturer_logos_are_never_taken_for_car_photos(fixture_html):
    """Taking every <img> on the page once saved 400 of these as 'car photos'."""
    result = parse_search_page(fixture_html("search_cards"), BASE)
    for listing in result.listings:
        for image in listing.images:
            assert "manufacturer/logo" not in image


def test_duplicate_links_to_one_car_collapse(fixture_html):
    result = parse_search_page(fixture_html("search_cards"), BASE)
    assert len({l.id for l in result.listings}) == len(result.listings)


def test_navigation_links_are_not_listings(fixture_html):
    result = parse_search_page(fixture_html("search_cards"), BASE)
    assert all(l.id.isdigit() for l in result.listings)


def test_empty_results_page_yields_nothing_without_raising(fixture_html):
    result = parse_search_page(fixture_html("search_empty"), BASE)
    assert len(result) == 0
    assert result.strategy == "none"


def test_anti_bot_page_is_recognised(fixture_html):
    assert looks_blocked(fixture_html("search_blocked"))


def test_real_pages_are_not_mistaken_for_anti_bot_pages(archive_html, archive_ids):
    if not archive_ids:
        pytest.skip("no archived pages available")
    for listing_id in archive_ids:
        assert not looks_blocked(archive_html(listing_id)), listing_id


def test_garbage_input_does_not_raise():
    for junk in ("", "not html", "<html>", "<div" * 500, "\x00\x01"):
        assert len(parse_search_page(junk, BASE)) == 0


def test_every_strategy_is_attempted_and_reported(fixture_html):
    result = parse_search_page(fixture_html("search_cards"), BASE)
    assert set(result.candidates) == {"jsonld", "embedded_json", "anchors", "regex"}
