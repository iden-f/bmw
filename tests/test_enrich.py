"""Detail pages carry schema.org data - the richest, most stable source."""
import pytest

from autotrader.enrich import detail_from_html, enrich
from autotrader.listing import Listing


def test_real_archived_pages_all_yield_structured_data(archive_html, archive_ids):
    if not archive_ids:
        pytest.skip("no archived pages available")
    read = 0
    for listing_id in archive_ids:
        detail = detail_from_html(archive_html(listing_id), listing_id)
        assert detail is not None, listing_id
        if detail.price and detail.mileage_km:
            read += 1
    # A handful of real listings genuinely say "please call" instead of a price.
    assert read >= len(archive_ids) - 6


def test_a_known_page_is_read_exactly(archive_html):
    detail = detail_from_html(archive_html("68819631"), "68819631")
    assert detail.year == 2022 and detail.make == "BMW" and detail.model == "M5"
    assert detail.price == 102199 and detail.currency == "CAD"
    assert detail.mileage_km == 30244
    assert detail.drivetrain == "AWD"
    assert detail.transmission == "Automatic"
    assert detail.color == "Black Sapphire Metallic"
    assert detail.price_source == "detail"


def test_photos_come_from_structured_data_not_page_images(archive_html):
    detail = detail_from_html(archive_html("13166607"), "13166607")
    assert detail.images, "expected real photo URLs"
    assert all("vehicleimages" in url for url in detail.images)
    assert not any("manufacturer/logo" in url for url in detail.images)


def test_masked_vins_are_discarded(archive_html, archive_ids):
    for listing_id in archive_ids[:10]:
        detail = detail_from_html(archive_html(listing_id), listing_id)
        assert "XXXX" not in (detail.vin or "")


def test_placeholder_values_are_dropped(archive_html):
    detail = detail_from_html(archive_html("13176580"), "13176580")
    assert detail.color == ""          # the page says "Not Specified"


def test_the_body_style_is_not_repeated_in_the_trim(archive_html):
    detail = detail_from_html(archive_html("68819631"), "68819631")
    if detail.body and detail.trim:
        assert not detail.trim.lower().endswith(detail.body.lower())


def test_enrichment_overrides_a_guessed_card_price(archive_html):
    card = Listing(id="68819631", url="https://www.autotrader.ca/a/x/5_68819631_z/",
                   title="2022 Example Coupe", price=99999, price_source="search",
                   mileage_km=1, search_id="s1")
    enrich(card, archive_html("68819631"))
    assert card.price == 102199 and card.price_source == "detail"
    assert card.mileage_km == 30244
    assert card.search_id == "s1"      # search association is preserved
    assert card.enriched


def test_enrichment_of_an_unusable_page_leaves_the_listing_alone():
    card = Listing(id="1", url="u", title="x", price=100, price_source="search")
    enrich(card, "<html><body>nothing useful</body></html>")
    assert card.price == 100 and card.price_source == "search"


# ------------------------------------------------- the page has to be the car

CAR_ID = "aaaaaaaa-1111-2222-3333-444444444444"
OTHER_ID = "bbbbbbbb-1111-2222-3333-444444444444"


def _detail_page(listing_id, price):
    return (
        f'<html><head><link rel="canonical" '
        f'href="https://www.autotrader.ca/offers/honda-civic-{listing_id}">'
        f'<script type="application/ld+json">'
        f'{{"@context":"https://schema.org","@type":"Car","name":"Honda Civic",'
        f'"offers":{{"@type":"Offer","price":{price},"priceCurrency":"CAD"}}}}'
        f'</script></head><body>for sale</body></html>')


def test_a_page_for_another_car_is_refused():
    """A removed listing is often answered with a redirect to something else.

    Writing that car's price onto this one gives a wrong price that looks
    entirely real, and fires a price-drop alert on the next comparison.
    """
    from autotrader.enrich import enrich

    card = Listing(id=CAR_ID, url=f"https://www.autotrader.ca/offers/honda-civic-{CAR_ID}",
                   title="2021 Honda Civic", price=40000, price_source="search")
    enrich(card, _detail_page(OTHER_ID, 24000))

    assert card.price == 40000, "another car's price was written onto this one"
    assert card.enriched is False


def test_the_right_page_still_enriches():
    from autotrader.enrich import enrich

    card = Listing(id=CAR_ID, url=f"https://www.autotrader.ca/offers/honda-civic-{CAR_ID}",
                   title="2021 Honda Civic", price=40000, price_source="search")
    enrich(card, _detail_page(CAR_ID, 38500))

    assert card.price == 38500 and card.enriched is True


def test_a_page_that_does_not_name_itself_is_still_used():
    """Most of the archived listing pages carry no canonical link at all."""
    from autotrader.enrich import enrich, page_identifies

    page = ('<html><head><script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"Car","name":"Honda Civic",'
            '"offers":{"@type":"Offer","price":39000,"priceCurrency":"CAD"}}'
            '</script></head><body>x</body></html>')
    assert page_identifies(page, CAR_ID) is None

    card = Listing(id=CAR_ID, url="u", title="t", price=40000, price_source="search")
    enrich(card, page)
    assert card.price == 39000


def test_real_archived_pages_still_identify_or_stay_silent(archive_html, archive_ids):
    """The guard must not start refusing pages that have always worked."""
    from autotrader.enrich import page_identifies

    for listing_id in archive_ids:
        assert page_identifies(archive_html(listing_id), listing_id) is not False
