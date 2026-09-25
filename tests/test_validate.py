"""The first real run has to judge its own output - nobody else can."""

from autotrader.listing import Listing
from autotrader.parser import parse_search_page
from autotrader.validate import assess, report_markdown, report_text

BASE = "https://www.autotrader.ca/cars/honda/civic/"


def cars(n, *, price=30000, km=50000, title="2021 Honda Civic"):
    return [Listing(id=str(i), url=f"https://www.autotrader.ca/a/x/19_{i}_/",
                    title=title, year=2021, make="Honda", model="Civic",
                    price=price, mileage_km=km, location="Toronto")
            for i in range(1, n + 1)]


def parsed(fixture_html, name):
    result = parse_search_page(fixture_html(name), BASE)
    return result, assess(result.listings, result.strategy, name,
                          candidates=result.candidates)


class TestTrustworthy:
    def test_a_good_parse_passes(self, fixture_html):
        _, verdict = parsed(fixture_html, "search_cards")
        assert verdict.trustworthy and verdict.count == 3

    def test_structured_data_passes(self, fixture_html):
        _, verdict = parsed(fixture_html, "search_jsonld")
        assert verdict.trustworthy

    def test_one_result_is_not_suspicious(self, fixture_html):
        _, verdict = parsed(fixture_html, "search_single")
        assert verdict.trustworthy

    def test_a_grid_of_call_for_price_cars_is_allowed(self, fixture_html):
        """Three unpriced listings is plausible; it is the ratio at scale that matters."""
        _, verdict = parsed(fixture_html, "search_call_for_price")
        assert verdict.trustworthy

    def test_an_empty_search_that_says_so_is_fine(self, fixture_html):
        result = parse_search_page(fixture_html("search_empty"), BASE)
        verdict = assess(result.listings, result.strategy, "x", said_no_results=True)
        assert verdict.trustworthy
        assert "narrow search" in " ".join(verdict.notes)


class TestSuspicious:
    def test_unreadable_markup_is_caught(self, fixture_html):
        _, verdict = parsed(fixture_html, "search_unreadable")
        assert not verdict.trustworthy
        assert "fallen behind" in " ".join(verdict.concerns)

    def test_regex_only_is_caught(self, fixture_html):
        _, verdict = parsed(fixture_html, "search_broken_primary")
        assert not verdict.trustworthy
        assert "raw-regex" in " ".join(verdict.concerns)

    def test_zero_listings_with_no_explanation_is_caught(self):
        assert not assess([], "none", "x", said_no_results=False).trustworthy

    def test_almost_nothing_priced_at_scale_is_caught(self):
        listings = cars(10, price=None)
        assert not assess(listings, "anchors", "x").trustworthy

    def test_almost_nothing_with_an_odometer_is_caught(self):
        listings = cars(10, km=None)
        assert not assess(listings, "anchors", "x").trustworthy

    def test_placeholder_titles_are_caught(self):
        listings = [Listing(id=str(i), url="u", title=f"AutoTrader listing {i}")
                    for i in range(10)]
        assert not assess(listings, "anchors", "x").trustworthy

    def test_an_absurd_price_is_caught(self):
        """The classic symptom of matching the wrong number on a card."""
        listings = cars(6)
        listings[0].price = 5_121_400
        assert not assess(listings, "anchors", "x").trustworthy

    def test_an_absurd_odometer_is_caught(self):
        listings = cars(6)
        listings[0].mileage_km = 5_121_400
        assert not assess(listings, "anchors", "x").trustworthy

    def test_a_small_sample_is_not_judged_on_ratios(self):
        """Two unpriced cars is normal; nine in ten is not."""
        assert assess(cars(2, price=None), "anchors", "x").trustworthy


class TestReporting:
    def test_the_sample_is_included_for_eyeballing(self, fixture_html):
        _, verdict = parsed(fixture_html, "search_cards")
        assert len(verdict.sample) == 3
        assert verdict.sample[0]["price"] == "$98,995"
        assert verdict.sample[0]["url"].startswith("https://")

    def test_markdown_names_the_problem(self, fixture_html):
        _, verdict = parsed(fixture_html, "search_unreadable")
        text = report_markdown([verdict], ok=False)
        assert "Something looks wrong" in text
        assert "Nothing was recorded" in text

    def test_markdown_confirms_a_good_run(self, fixture_html):
        _, verdict = parsed(fixture_html, "search_cards")
        text = report_markdown([verdict], ok=True)
        assert "Looks right" in text
        assert "2021 BMW M5 Competition Sedan" in text
        assert "| Price |" in text

    def test_the_notification_text_avoids_markdown_tables(self, fixture_html):
        _, verdict = parsed(fixture_html, "search_cards")
        text = report_text([verdict], ok=True)
        assert "|---" not in text
        assert "$98,995" in text

    def test_only_one_lone_strategy_is_flagged_as_fragile(self, fixture_html):
        _, verdict = parsed(fixture_html, "search_jsonld")
        result = parse_search_page(fixture_html("search_jsonld"), BASE)
        lonely = assess(result.listings, "jsonld", "x", candidates={"jsonld": 2, "regex": 0})
        assert "no fallback left" in " ".join(lonely.notes)
