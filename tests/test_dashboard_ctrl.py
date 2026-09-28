"""The dashboard's side of the control channel, in a real browser.

Two places where the page and the bot disagreed about a change:

* "Add a search" read a pasted link its own way. It offered to add the
  home page, a single car's page, an http:// or m. link and any host
  ending in autotrader.ca, all of which the bot refuses, and it read a
  newer-platform link as "AUDI RS%205" and nothing more.
* The Searches tab said every area was "enforced here", including a
  distance with no place to measure from, which keeps every car.

Uses the published page and the demo data of test_dashboard_layout.py.
"""
from __future__ import annotations

import pytest

from autotrader.dashboard import _area_of

from .test_dashboard_layout import _demo_payload, _page, browser, site  # noqa: F401


@pytest.fixture(scope="module")
def payload(tmp_path_factory):
    """The demo, with its search given a distance and no place to measure
    from, the way the bot publishes one."""
    root = tmp_path_factory.mktemp("bot")
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(root)
        data = _demo_payload(root)
    data["searches"][0]["area"] = _area_of({"max_distance_km": 100})
    return data


def _paste(browser, site, link, width=390):
    ctx, page, errors = _page(browser, site, width, "light", view="searches")
    page.fill("#paste", link)
    page.wait_for_timeout(100)
    text = page.inner_text("#paste-out")
    offered = page.locator("#paste-out .btn", has_text="Add this search").count()
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    ctx.close()
    assert not errors, errors
    return text, offered, overflow


def test_a_distance_with_nowhere_to_measure_from_says_it_is_not_applied(browser, site):
    ctx, page, errors = _page(browser, site, 375, "light", view="searches")
    try:
        area = page.locator("dt:text-is('Area') + dd").first.inner_text()
        assert "not applied" in area and "no place" in area, area
        assert "enforced here" not in area, area
        assert not errors, errors
    finally:
        ctx.close()


def test_a_newer_platform_link_reads_as_the_bot_reads_it(browser, site):
    text, offered, _ = _paste(
        browser, site,
        "https://www.autotrader.ca/cars/audi/rs%205/reg_on/cit_ottawa/"
        "?modelyearfrom=2019&modelyearto=2023&priceto=90000&zip=Ottawa&zipr=150")
    for part in ("AUDI RS 5", "2019–2023", "under $90,000", "near Ottawa (150 km)"):
        assert part in text, (part, text)
    assert "%20" not in text and offered == 1


def test_an_older_link_reads_its_prices_as_money(browser, site):
    text, offered, _ = _paste(
        browser, site, "https://www.autotrader.ca/cars/honda/civic/on/?pRng=,30000&yRng=2016,")
    assert "under $30,000" in text and "2016 or newer" in text, text
    assert "–30000" not in text and offered == 1


@pytest.mark.parametrize("link, says", [
    ("https://www.autotrader.ca/", "home page"),
    ("https://www.autotrader.ca/a/honda/civic/toronto/ontario/19_12345678_/", "one car"),
    ("https://www.autotrader.ca/offers/honda-civic-si-"
     "00000000-0000-4000-8000-000000000042", "one car"),
    ("http://www.autotrader.ca/cars/honda/civic/", "https://"),
    ("https://m.autotrader.ca/cars/honda/civic/", "not www.autotrader.ca"),
    ("https://notautotrader.ca/cars/honda/civic/", "not www.autotrader.ca"),
    ("https://www.autotrader.ca.example.com/cars/", "not www.autotrader.ca"),
    ("https://www.autotrader.ca:8443/cars/honda/civic/", "not www.autotrader.ca"),
    ("https://someone@www.autotrader.ca/cars/honda/civic/", "not www.autotrader.ca"),
])
def test_a_link_the_bot_would_refuse_is_not_offered(browser, site, link, says):
    text, offered, _ = _paste(browser, site, link)
    assert says in text, text
    assert offered == 0, f"offered to add {link}"


def test_a_long_refusal_fits_a_phone(browser, site):
    _, _, overflow = _paste(
        browser, site, "https://" + "a-very-long-name-" * 6 + "example.com/", width=375)
    assert overflow <= 0, f"overflows by {overflow}px"


def test_the_link_sent_is_one_the_bot_takes(browser, site):
    """The bot checks the link as sent against control.AUTOTRADER_LINK."""
    from autotrader import control
    ctx, page, _ = _page(browser, site, 390, "light")
    try:
        sent = page.evaluate("""() => [
            readSearchLink('https://WWW.AutoTrader.ca:443/cars/honda/civic/?pRng=,30000').href,
            readSearchLink('  https://autotrader.ca/cars/honda/civic/').href]""")
    finally:
        ctx.close()
    for href in sent:
        assert control.AUTOTRADER_LINK.match(href), href
