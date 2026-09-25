"""The search-results markup is the weakest link: it is inferred, not captured.

Every one of these asserts graceful degradation. A parser that reads nothing
must never crash a run - it must say so loudly enough that the health check
fires.
"""
import pytest

from autotrader.parser import looks_like_no_results, parse_search_page

BASE = "https://www.autotrader.ca/cars/honda/civic/"


def parse(fixture_html, name):
    return parse_search_page(fixture_html(name), BASE)


def test_zero_results_is_read_as_zero_results_not_a_broken_parser(fixture_html):
    result = parse(fixture_html, "search_empty")
    assert len(result) == 0
    assert looks_like_no_results(fixture_html("search_empty"))


def test_a_page_full_of_cars_never_looks_like_zero_results(fixture_html):
    for name in ("search_cards", "search_single", "search_page2",
                 "search_call_for_price", "search_jsonld"):
        assert not looks_like_no_results(fixture_html(name)), name


def test_real_listing_pages_never_look_like_zero_results(archive_html, archive_ids):
    if not archive_ids:
        pytest.skip("no archived pages available")
    for listing_id in archive_ids:
        assert not looks_like_no_results(archive_html(listing_id)), listing_id


def test_a_single_result_is_handled(fixture_html):
    result = parse(fixture_html, "search_single")
    assert len(result) == 1
    only = result.listings[0]
    assert only.id == "68532258"
    assert only.price == 98888 and only.mileage_km == 88500
    assert only.location == "Oakville"


def test_call_for_price_listings_survive_without_a_price(fixture_html):
    result = parse(fixture_html, "search_call_for_price")
    assert len(result) == 3
    assert all(l.price is None for l in result.listings)
    # ...but everything else still parses.
    assert [l.mileage_km for l in result.listings] == [1200, 14000, 9900]
    assert all(l.location for l in result.listings)


def test_a_model_designation_does_not_bleed_into_the_odometer(fixture_html):
    """'BMW M5 1,200 km' must be 1,200 km, not 51,200."""
    result = parse(fixture_html, "search_call_for_price")
    assert next(l for l in result.listings if l.id == "13300001").mileage_km == 1200


def test_a_second_page_yields_its_own_cars(fixture_html):
    result = parse(fixture_html, "search_page2")
    assert {l.id for l in result.listings} == {"13166607", "13400001", "13400002"}
    assert next(l for l in result.listings if l.id == "13400002").mileage_km == 121400


def test_pages_can_be_merged_without_duplicates(fixture_html):
    """Page 2 repeats one car from page 1; the run must keep one copy."""
    page1 = parse(fixture_html, "search_cards").listings
    page2 = parse(fixture_html, "search_page2").listings
    merged = {l.id: l for l in page1}
    for listing in page2:
        merged.setdefault(listing.id, listing)
    assert len(merged) == 5           # 3 + 3 with one shared
    assert merged["13166607"].price == 98995


def test_a_broken_primary_strategy_degrades_to_a_weaker_one(fixture_html):
    """Malformed JSON-LD and JavaScript-built links: only regex can see these."""
    result = parse(fixture_html, "search_broken_primary")
    assert result.candidates["jsonld"] == 0     # the JSON is invalid
    assert result.candidates["regex"] == 2
    assert result.strategy == "regex"
    assert {l.id for l in result.listings} == {"13500001", "13500002"}


def test_a_degraded_parse_still_produces_usable_records(fixture_html):
    """A weak strategy gives no price, but must give a working link and city."""
    result = parse(fixture_html, "search_broken_primary")
    for listing in result.listings:
        assert listing.url.startswith("https://www.autotrader.ca/a/")
        assert listing.price is None
        assert listing.location


def test_invalid_json_ld_never_raises(fixture_html):
    parse(fixture_html, "search_broken_primary")   # would raise if unguarded


def test_markup_no_strategy_understands_yields_nothing_quietly(fixture_html):
    result = parse(fixture_html, "search_unreadable")
    assert len(result) == 0
    assert result.strategy == "none"
    assert all(count == 0 for count in result.candidates.values())


def test_unreadable_markup_is_not_mistaken_for_an_empty_search(fixture_html):
    """This is the distinction that decides whether to raise the alarm."""
    assert not looks_like_no_results(fixture_html("search_unreadable"))


@pytest.mark.parametrize("junk", [
    "", " ", "not html at all", "<html>", "<div class='x'>" * 800,
    "\x00\x01\x02", "<script>{[}</script>", "<!DOCTYPE html>",
    '<a href="/a/">no id</a>', '<a href="/a/honda/civic/x/on/5_1_z/">id too short</a>',
])
def test_hostile_input_never_raises(junk):
    result = parse_search_page(junk, BASE)
    assert isinstance(result.listings, list)


def test_a_strategy_that_throws_does_not_take_the_others_down(monkeypatch, fixture_html):
    import autotrader.parser as parser_mod

    def explode(soup, html, base):
        raise RuntimeError("this strategy is broken")

    monkeypatch.setattr(parser_mod, "STRATEGIES",
                        (("jsonld", explode), ("anchors", parser_mod._strategy_anchors)))
    result = parse_search_page(fixture_html("search_cards"), BASE)
    assert len(result) == 3 and result.strategy == "anchors"
