"""A search results page is not only its results.

Around them the 2026 platform hangs recommendation rails - "similar
vehicles", "others also viewed", new-car promotions - and those cars sit in
the very same front-end state blob as the real results. A parser that walks
the blob picking up everything shaped like a car picks those up too, and
cannot tell them apart afterwards: they carry a real listing id, a real price
and a real photo.

That is the whole difference between a watch that reads its results and one
that reads the page: a search for a Civic Type R can collect four times as
many cars as it asked for, most of them a plain Civic, while the page's own
schema.org ItemList names only the Type Rs it actually returned.
"""
import json

import pytest

from autotrader.parser import _declared_results, parse_search_page
from bs4 import BeautifulSoup


def page(results, extras=(), *, declare=True):
    """A search page with `results` in its ItemList and `extras` only in the
    front-end blob, exactly as the live site lays them out."""
    def offer(car):
        return (f"https://www.autotrader.ca/offers/"
                f"honda-{car['slug']}-cat_ma13-{car['id']}")
    items = [{"@type": "ListItem", "position": i + 1, "url": offer(c),
              "item": {"@type": ["Car", "Product"], "name": c["name"],
                       "model": c["model"],
                       "offers": {"@type": "Offer", "price": c["price"],
                                  "priceCurrency": "CAD",
                                  "url": offer(c)}}}
             for i, c in enumerate(results)]
    ld = {"@context": "https://schema.org",
          "@graph": [{"@type": "SearchResultsPage",
                      "mainEntity": {"@type": "ItemList",
                                     "numberOfItems": len(results),
                                     "itemListElement": items}}]}
    def row(c):
        return {"id": c["id"], "url": offer(c), "title": c["name"],
                "model": c["model"], "year": c.get("year", 2020),
                "price": c["price"]}
    # Laid out the way autotrader.ca lays it out: the answers under one key,
    # the rails under another, both in the same blob. An earlier version of
    # this fixture put them in one array, which is not a page that exists and
    # made the test agree with a parser that could not have worked.
    blob = {"props": {"pageProps": {
        "listings": [row(c) for c in results],
        "recommendations": [row(c) for c in extras],
    }}}
    head = (f'<script type="application/ld+json">{json.dumps(ld)}</script>'
            if declare else "")
    return ("<html><head>" + head + "</head><body>"
            f'<script id="__NEXT_DATA__" type="application/json">'
            f'{json.dumps(blob)}</script></body></html>')


def car(n, model, name=None, price=30000, year=2020):
    return {"id": f"{n:08d}-0000-4000-8000-{n:012d}", "slug": f"car-{n}",
            "name": name or f"Honda {model}", "model": model,
            "price": price, "year": year}


WANTED = [car(1, "Civic Type R"), car(2, "Civic Type R"), car(3, "Civic Type R")]
RAILS = [car(9, "Civic", year=2005), car(10, "Civic", year=2026),
         car(11, "Civic", year=2013)]


class TestThePageNamesItsOwnResults:

    def test_the_rails_are_not_read_at_all(self):
        result = parse_search_page(page(WANTED, RAILS), "https://x/")
        assert len(result.listings) == 3
        assert {l.model for l in result.listings} == {"Civic Type R"}
        assert result.confined is True
        assert result.declared == 3
        assert result.off_list == 3

    def test_it_says_how_many_the_page_claimed(self):
        result = parse_search_page(page(WANTED, RAILS), "https://x/")
        assert result.declared == len(WANTED)

    def test_a_page_that_declares_nothing_is_read_as_before(self):
        """The fallback has to stay. A page that stops declaring its results
        is a page shape we do not understand yet, and reading nothing from it
        would empty the watch - which is the one failure worse than reading
        too much.

        "Declares nothing" means neither source: no ItemList AND no key the
        blob files its results under. With the blob renamed to something
        unrecognised there is no answer to confine to, and everything on the
        page is read exactly as it was before any of this."""
        html = page(WANTED, RAILS, declare=False).replace(
            '"listings"', '"someNewNameForThem"').replace(
            '"recommendations"', '"someOtherNewName"')
        result = parse_search_page(html, "https://x/")
        assert len(result.listings) == 6
        assert result.confined is False
        assert result.off_list == 0

    def test_the_blob_alone_is_enough_to_tell_them_apart(self):
        """The ItemList can go and the rails still stay out."""
        result = parse_search_page(page(WANTED, RAILS, declare=False),
                                   "https://x/")
        assert result.confined is True
        assert {l.model for l in result.listings} == {"Civic Type R"}
        assert result.off_list == 3

    def test_a_list_naming_cars_no_strategy_can_build_does_not_empty_it(self):
        """An ItemList full of ids that appear nowhere else on the page is
        not authority to report every car sold."""
        html = page(WANTED, RAILS)
        # Point the declared list at ids the blob does not carry.
        html = html.replace("car-1", "ghost-1").replace(
            f'{1:08d}-0000-4000-8000-{1:012d}', "ffffffff-0000-4000-8000-ffffffffffff")
        result = parse_search_page(html, "https://x/")
        assert result.listings, "the page still has cars on it"

    def test_declared_results_reads_the_ids(self):
        html = page(WANTED, RAILS)
        soup = BeautifulSoup(html, "html.parser")
        ids, declared = _declared_results(soup, html, "https://x/")
        assert declared == 3
        assert len(ids) == 3

    def test_a_call_for_price_result_is_kept(self):
        """schema.org Offer requires a price, so autotrader.ca leaves a
        call-for-price car out of the ItemList entirely, while it sits among
        the results in date order beside the cars that do have a figure.
        Confining to the ItemList alone dropped every one of them, which is
        the worst outcome available here - unpriced cars are a category this
        bot deliberately keeps and asks about again."""
        quiet = dict(car(4, "Civic Type R"), price=None)
        html = page(WANTED, RAILS)
        import json as _json
        blob = _json.loads(html.split('type="application/json">')[1]
                           .split("</script>")[0])
        blob["props"]["pageProps"]["listings"].append({
            "id": quiet["id"],
            "url": f"https://www.autotrader.ca/offers/honda-{quiet['slug']}-cat_ma13-{quiet['id']}",
            "title": quiet["name"], "model": "Civic Type R", "year": 2021,
            "price": None})
        html = (html.split('type="application/json">')[0]
                + 'type="application/json">' + _json.dumps(blob)
                + "</script></body></html>")
        result = parse_search_page(html, "https://x/")
        kept = {l.id for l in result.listings}
        assert quiet["id"] in kept, (
            "a result with no published price is still a result")
        assert result.confined is True
        assert len(kept) == 4


class TestARealResultIsNeverLost:

    def test_every_declared_car_survives(self):
        """The confinement may only ever REMOVE rails, never a result."""
        result = parse_search_page(page(WANTED, RAILS), "https://x/")
        got = {l.id for l in result.listings}
        assert got == {c["id"] for c in WANTED}

    def test_a_declared_car_only_the_markup_describes_is_kept(self):
        """The merge used to fold a runner-up strategy into a row the winner
        already had, and drop it otherwise - so a car that only the
        schema.org markup described was parsed and then thrown away."""
        html = page(WANTED, RAILS)
        # Remove one wanted car from the front-end blob, leaving it declared.
        blob_only = json.loads(html.split('type="application/json">')[1]
                               .split("</script>")[0])
        rows = blob_only["props"]["pageProps"]["listings"]
        blob_only["props"]["pageProps"]["listings"] = [
            r for r in rows if r["id"] != WANTED[0]["id"]]
        html = (html.split('type="application/json">')[0]
                + 'type="application/json">' + json.dumps(blob_only)
                + "</script></body></html>")
        result = parse_search_page(html, "https://x/")
        assert WANTED[0]["id"] in {l.id for l in result.listings}, (
            "a car the page declared a result was dropped because the "
            "winning strategy did not happen to find it")


class TestItRefusesAnAnswerItCouldOnlyPartlyRead:

    def test_a_short_itemlist_is_not_believed(self):
        """One readable entry out of three used to be enough to claim the
        page had named its results - and that claim is what later authorises
        dropping stored rows. Falling back to reading the whole page is
        noisier and cannot lose a car, so that is the way to fail."""
        html = page(WANTED, RAILS, declare=True)
        # numberOfItems still says 3; only one entry survives.
        blob = json.loads(html.split('application/ld+json">')[1]
                          .split("</script>")[0])
        items = blob["@graph"][0]["mainEntity"]["itemListElement"]
        blob["@graph"][0]["mainEntity"]["itemListElement"] = items[:1]
        html = (html.split('application/ld+json">')[0]
                + 'application/ld+json">' + json.dumps(blob)
                + "</script>" + html.split("</script>", 1)[1])
        # And the blob's own results key is gone too, so the ItemList is the
        # only answer on offer.
        html = html.replace('"listings"', '"unrecognisedKey"')
        result = parse_search_page(html, "https://x/")
        assert result.confined is False
        assert len(result.listings) == 6


class TestTheNoResultsMarkerIsNotASubstring:
    """It was, and "0 results" is inside "520 results"."""

    @pytest.mark.parametrize("text,empty", [
        ("520 results for Honda in Toronto", False),
        ("10 results", False),
        ("1,230 results for Honda", False),
        ("100 listings", False),
        ("20 matches", False),
        ("0 results", True),
        ("0 results found", True),
        ("0 listings", True),
        ("No vehicles match your current search", True),
    ])
    def test_a_total_ending_in_zero_is_not_an_empty_search(self, text, empty):
        from autotrader.parser import looks_like_no_results
        assert looks_like_no_results(f"<p>{text}</p>") is empty, text
