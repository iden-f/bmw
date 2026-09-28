"""Asking the site whether the ledger is right.

Four consecutive checks reported nothing changed. That has two explanations -
a quiet market, or a bot that cannot see change - and they look identical from
the inside, because both of them are the bot reporting on itself.

The first version of this command shipped with `Fetcher(cfg)`, which is not
the constructor's signature, and it failed on the runner in four seconds. That
is what these are for: the command is only ever run by hand, so nothing else
would have found it.
"""

from __future__ import annotations

import pytest

from autotrader.cli import main
from autotrader.config import Config
from autotrader.listing import Listing
from autotrader.state import State

PAGE = """<html><head><title>car</title></head><body>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":["Car","Product"],
 "name":"Honda Civic Type R","offers":{"@type":"Offer","price":%s,
 "priceCurrency":"CAD"},"mileageFromOdometer":{"value":50000,"unitCode":"KMT"}}
</script></body></html>"""


class Site:
    """The live site, as far as this command can tell."""

    # What the bot holds, unless a test says the site has changed its mind.
    HOLDS = {"aaa": 60000, "bbb": 70000}

    def __init__(self, prices=None, missing=(), boom=()):
        self.prices = {**self.HOLDS, **(prices or {})}
        self.missing = set(missing)
        self.boom = set(boom)
        self.asked = []
        self.spent = 0

    def get(self, url, referer=None, allow_block=False):
        self.asked.append(url)
        self.spent += 1
        lid = url.rstrip("/").split("_")[-2] if "_" in url else url
        for key in list(self.prices) + list(self.missing) + list(self.boom):
            if key in url:
                lid = key
                break
        if lid in self.boom:
            # As the real client raises it: the message and the status.
            from autotrader.http import FetchError
            raise FetchError(f"HTTP 404 from {url}", status=404)
        if lid in self.missing:
            return self._as_the_real_one_would(
                url, "<html><body>This listing is no longer available</body></html>")
        return self._as_the_real_one_would(url, PAGE % self.prices.get(lid, 60000))

    @staticmethod
    def _as_the_real_one_would(url, html):
        """The real Response, not a string that reads like one.

        This stand-in returned bare markup, every test passed, and the job
        died on the runner handing a Response to a parser that takes markup.
        A stand-in is only worth anything if it returns what the thing it
        stands in for returns, so it builds the real class.
        """
        from autotrader.http import Response
        return Response(url=url, status=200, text=html, elapsed_ms=1)

    def close(self):
        pass


@pytest.fixture
def bench(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Config.defaults(tmp_path / "config.json")
    search = cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=25", "Civic")
    cfg.set("scraping.delay_ms", 0)
    cfg.save()
    state = State(path=tmp_path / "state.json")
    for lid, price in (("aaa", 60000), ("bbb", 70000)):
        state.record(Listing(
            id=lid, url=f"https://www.autotrader.ca/a/honda/civic/x/on/19_{lid}_/",
            title="Honda Civic", year=2018, make="Honda", model="Civic",
            price=price, price_source="detail", search_id=search.id))
    state.save()
    return type("B", (), {"cfg": cfg, "path": tmp_path})


def run(monkeypatch, site, *args):
    from autotrader import cli
    monkeypatch.setattr(cli, "Fetcher", lambda **kw: site)
    return main(["verify", *args])


class TestItActuallyRuns:
    def test_the_stand_in_returns_what_the_real_fetcher_returns(self):
        """The second bug that shipped: a Response handed to a parser that
        takes markup, because the stand-in returned a str and nothing here
        compared the two.

        Read from the real annotation rather than repeating it, so this stays
        true if Fetcher.get ever returns something else.
        """
        import typing
        from autotrader.http import Fetcher

        want = typing.get_type_hints(Fetcher.get)["return"]
        got = Site().get("https://www.autotrader.ca/a/honda/civic/x/on/19_aaa_/")
        assert isinstance(got, want), (type(got), want)
        assert isinstance(got.text, str)

    def test_the_fetcher_is_built_the_way_the_fetcher_wants(self, bench, monkeypatch):
        """The bug that shipped: Fetcher(cfg), four seconds into a job."""
        seen = {}
        from autotrader import cli
        from autotrader.http import Fetcher as Real

        def spy(*args, **kwargs):
            seen["args"], seen["kwargs"] = args, kwargs
            return Site()

        monkeypatch.setattr(cli, "Fetcher", spy)
        main(["verify"])
        assert not seen["args"], "Fetcher takes keyword arguments only"
        import inspect
        allowed = set(inspect.signature(Real.__init__).parameters) - {"self"}
        assert set(seen["kwargs"]) <= allowed, set(seen["kwargs"]) - allowed

    def test_it_asks_once_per_car(self, bench, monkeypatch):
        site = Site()
        assert run(monkeypatch, site) == 0
        assert len(site.asked) == 2

    def test_the_budget_is_sized_to_the_question(self, bench, monkeypatch):
        """Not the whole run's budget: this is a question, not a check."""
        seen = {}
        from autotrader import cli
        monkeypatch.setattr(cli, "Fetcher",
                            lambda **kw: (seen.update(kw), Site())[1])
        main(["verify"])
        assert seen["budget"] < 100


class TestWhatItReports:
    def test_agreement_is_reported_as_agreement(self, bench, monkeypatch, capsys):
        assert run(monkeypatch, Site(), "--verbose") == 0
        out = capsys.readouterr().out
        assert "2 cars agree" in out

    def test_a_price_that_moved_is_named_with_both_figures(self, bench, monkeypatch, capsys):
        run(monkeypatch, Site(prices={"aaa": 57000}))
        out = capsys.readouterr().out
        assert "$60,000" in out and "$57,000" in out
        assert "1 differ" in out or "differ" in out

    def test_a_car_the_site_has_dropped_is_named(self, bench, monkeypatch, capsys):
        run(monkeypatch, Site(boom=["aaa"]))
        out = capsys.readouterr().out
        assert "gone from the site" in out

    def test_a_404_from_the_real_client_is_a_car_gone(self, bench, monkeypatch, capsys):
        """The stand-in above once set .status by hand on an error the real
        client never gave one, so a sold car always read as unreadable."""
        import requests

        from autotrader import cli
        from autotrader.http import Fetcher as Real

        class Answers404:
            headers = {}

            def get(self, url, **kwargs):
                response = requests.Response()
                response.status_code = 404
                response._content = b"<html>not here</html>"
                response.url = url
                return response

            def close(self):
                pass

        def build(**kw):
            real = Real(**{**kw, "delay_ms": 0, "retries": 0})
            real.session = Answers404()
            return real

        monkeypatch.setattr(cli, "Fetcher", build)
        assert main(["verify"]) == 0
        out = capsys.readouterr().out
        assert "gone from the site (HTTP 404)" in out, out
        assert "2 gone, 0 unreadable" in out, out

    def test_a_page_that_says_it_is_gone_counts_as_gone(self, bench, monkeypatch, capsys):
        run(monkeypatch, Site(missing=["bbb"]))
        assert "gone" in capsys.readouterr().out

    def test_disagreement_is_information_not_failure(self, bench, monkeypatch):
        """The command answers a question. An answer is not an error."""
        assert run(monkeypatch, Site(prices={"aaa": 1})) == 0

    def test_limit_stops_early(self, bench, monkeypatch):
        site = Site()
        run(monkeypatch, site, "--limit", "1")
        assert len(site.asked) == 1


class TestThePageAsAPersonReadsIt:
    """The whole command reported "the page shows no price" for all 28 cars.

    That reads as a blind bot. It was the site not putting the price in the
    structured data it puts the odometer, the colour, the transmission and
    the photographs in - and every price this bot holds came off the search
    results card, never off a detail page. A command that cannot tell those
    two apart cannot answer the question it exists to answer.
    """

    # The data block the site does publish: everything except a price.
    NO_PRICE = """<html><body>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":["Car","Product"],
     "name":"Honda Civic Type R",
     "mileageFromOdometer":{"value":50000,"unitCode":"KMT"}}
    </script>
    %s</body></html>"""

    def site(self, body):
        site = Site()
        site.get = lambda url, referer=None, allow_block=False: (
            Site._as_the_real_one_would(url, self.NO_PRICE % body))
        site.spent = 0
        site.close = lambda: None
        return site

    def test_a_figure_on_the_page_that_matches_is_agreement(self, bench, monkeypatch, capsys):
        rc = run(monkeypatch, self.site(
            "<div class='price'>$60,000</div>"), "--limit", "1", "--verbose")
        out = capsys.readouterr().out
        assert "1 car agree" in out or "1 cars agree" in out, out
        assert "not in its data block" in out
        assert rc == 0

    def test_figures_that_do_not_include_it_is_a_disagreement(self, monkeypatch, bench, capsys):
        run(monkeypatch, self.site(
            "<div>$44,900</div><div>$799/mo</div>"), "--limit", "1")
        out = capsys.readouterr().out
        assert "does not show it" in out, out
        assert "$44,900" in out

    def test_no_figure_anywhere_is_unreadable(self, monkeypatch, bench, capsys):
        run(monkeypatch, self.site("<p>Call for price</p>"), "--limit", "1")
        out = capsys.readouterr().out
        assert "no price anywhere on the page" in out, out

    def test_a_monthly_payment_is_not_mistaken_for_an_asking_price(self):
        from autotrader.cli import _dollar_figures
        # Four digits and up, grouped. "$799" and "$99" are not asking prices
        # and are not in the set; "$1,299" is, and that is the honest cost of
        # not parsing: it can be in the set without being the price. It only
        # ever decides whether the bot's own figure is present.
        assert _dollar_figures("$799/mo $99 down $60,000") == {60000}
        assert _dollar_figures("$1,299 bi-weekly") == {1299}


class TestItLeavesMarketplaceAlone:
    """This client is not a browser, and Facebook is only ever read by the
    collector on a computer at home - slowly, as a person would."""

    def test_no_marketplace_page_is_asked_for(self, bench, monkeypatch, capsys):
        state = State.load(bench.path / "state.json")
        state.record(Listing(
            id="fb-1234567890",
            url="https://www.facebook.com/marketplace/item/1234567890/",
            title="Honda Civic", year=2018, make="Honda", model="Civic",
            price=58000, search_id=bench.cfg.searches[0].id))
        state.save()
        site = Site()
        assert run(monkeypatch, site) == 0
        assert not [u for u in site.asked if "facebook.com" in u], site.asked
        assert len(site.asked) == 2
        out = capsys.readouterr().out
        assert "1 on Marketplace (the collector reports those)" in out, out
