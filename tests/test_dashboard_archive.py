"""The four tabs, the Archive view and the settings the page now changes.

Discover holds the feed and the listings, Watches the searches, Archive the
whole record with its collections and saved listings, Settings the options
and the health report. Every change still leaves as one sealed control file;
a device holding a GitHub token sends it itself.
"""
from __future__ import annotations

import base64
import copy
import json

import pytest

from autotrader import vault as V

from .test_dashboard_fe2 import Served, _asks, _said, _type_into
from .test_dashboard_layout import browser, payload, site  # noqa: F401 - fixtures
from .test_private_site import PHRASE, TITLE, _unlock, private_site  # noqa: F401 - fixture


def _open(browser, site, data, route="#/archive", *, width=1440):
    ctx = browser.new_context(viewport={"width": width, "height": 900},
                              service_workers="block")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(10_000)
    page.route("**/data.json", Served(data))
    page.goto(site + route, wait_until="networkidle")
    page.wait_for_timeout(200)
    return ctx, page, errors


def _with_archive_parts(data: dict) -> dict:
    """The demo, with an archive-only search, a saved listing, a long price
    history and a seller's edit."""
    d = copy.deepcopy(data)
    s = d["searches"][0]
    d["searches"].append({**copy.deepcopy(s), "id": "quiet-one", "name": "Quiet collection",
                          "notify": "archive"})
    d["searches"][0]["notify"] = "alert"
    car = next(l for l in d["listings"] if l["id"] == "1")
    car["price_history"] = [{"at": f"2026-01-{day:02d}T12:00:00+00:00", "price": 50_000 - day * 100}
                            for day in range(1, 46)]
    car["revisions"] = [{"at": "2026-01-20T12:00:00+00:00", "field": "mileage_km",
                         "from": 48000, "to": 48500}]
    car["pinned"] = True
    car["pinned_notify"] = "archive"
    gone = next(l for l in d["listings"] if l["id"] == "6")
    gone["thumbs"] = ["thumbs/demo-1.webp"]
    gone["thumb"] = "thumbs/demo-1.webp"
    gone["photo_count"] = 3
    gone["photos_saved"] = 1
    d["pinned"] = [
        {"id": "1", "url": car["url"], "name": "the Type R", "notify": "archive",
         "site": "autotrader", "found_by": "Honda Civic 2014-2021", "status": "active",
         "known": True, "awaiting_mac": False},
        {"id": "fb-123", "url": "https://www.facebook.com/marketplace/item/123/", "name": "",
         "notify": "alert", "site": "marketplace", "found_by": None, "status": "active",
         "known": False, "awaiting_mac": True},
    ]
    d["repo"] = "someone/watch"
    return d


class TestTheFourTabs:

    def test_the_bar_has_four_tabs_and_each_view_keeps_its_address(self, browser, site, payload):
        ctx, page, errors = _open(browser, site, payload, "#/feed")
        try:
            tabs = page.locator("#tabs .tab")
            assert tabs.count() == 4
            assert [_said(t) for t in tabs.all_inner_texts()][0].startswith("Discover")
            assert page.get_attribute('#tabs [data-view-link="feed"]', "aria-current") == "page"
            # The switch inside Discover: Feed and Listings, with the counts.
            assert page.locator('.seg [data-view-link="listings"]').count() == 1
            page.click('.seg [data-view-link="listings"]')
            page.wait_for_timeout(200)
            assert page.evaluate("location.hash") == "#/listings"
            assert page.get_attribute('#tabs [data-view-link="feed"]', "aria-current") == "page"
            page.click('#tabs [data-view-link="archive"]')
            page.wait_for_timeout(200)
            assert page.evaluate("location.hash") == "#/archive"
            assert page.locator('.seg [data-view-link="market"]').count() == 1
            page.keyboard.press("4")
            page.wait_for_timeout(200)
            assert page.evaluate("location.hash") == "#/status"
            assert page.text_content("#status-h") == "Settings"
            assert not errors, errors
        finally:
            ctx.close()

    def test_an_old_address_still_opens_its_view(self, browser, site, payload):
        ctx, page, _ = _open(browser, site, payload, "#/market")
        try:
            assert not page.is_hidden('[data-view="market"]')
            assert page.get_attribute('#tabs [data-view-link="archive"]', "aria-current") == "page"
        finally:
            ctx.close()

    @pytest.mark.parametrize("width", [375, 1280])
    def test_nothing_on_the_new_views_scrolls_sideways(self, browser, site, payload, width):
        ctx, page, errors = _open(browser, site, _with_archive_parts(payload), "#/archive",
                                  width=width)
        try:
            for view in ("archive", "status"):
                page.click(f'[data-view-link="{view}"]')
                page.wait_for_timeout(200)
                over = page.evaluate(
                    "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
                assert over <= 0, f"{view} at {width}px overflows by {over}px"
                text = page.evaluate("() => document.body.innerText")
                for leak in ("undefined", "NaN", "[object Object]", "null"):
                    assert leak not in text, (view, leak)
            assert not errors, errors
        finally:
            ctx.close()


class TestTheArchive:

    def test_it_counts_the_whole_record_and_shows_the_gone(self, browser, site, payload):
        d = _with_archive_parts(payload)
        ctx, page, errors = _open(browser, site, d)
        try:
            head = _said(page.inner_text("#archive-h + p"))
            assert f"{len(d['listings'])} cars" in head
            assert "1 of them gone from the site" in head
            # Every car recorded, the gone one included, and the chips narrow it.
            assert page.locator('[data-view="archive"] .card').count() == len(d["listings"])
            page.click('[data-view="archive"] .chip:has-text("Gone")')
            page.wait_for_timeout(150)
            assert page.locator('[data-view="archive"] .card').count() == 1
            assert page.locator('[data-view="archive"] .card--gone').count() == 1
            page.click('[data-view="archive"] .chip:has-text("Saved")')
            page.wait_for_timeout(150)
            assert page.locator('[data-view="archive"] .card').count() == 1
            assert not errors, errors
        finally:
            ctx.close()

    def test_collections_say_which_search_only_archives(self, browser, site, payload):
        ctx, page, _ = _open(browser, site, _with_archive_parts(payload))
        try:
            text = _said(page.inner_text('[data-view="archive"]'))
            assert "1 of 2 archive only" in text
            assert "Quiet collection Archive only" in text
            assert "Honda Civic 2014-2021 Alerts" in text
            # One button each way. An unencrypted page offers them disabled.
            assert _asks(page, "Archive only, no alerts") >= 1
            assert _asks(page, "Alert me again") >= 1
        finally:
            ctx.close()

    def test_saved_listings_say_what_is_known_of_each(self, browser, site, payload):
        ctx, page, _ = _open(browser, site, _with_archive_parts(payload))
        try:
            text = _said(page.inner_text('[data-view="archive"]'))
            assert "the Type R Archive only" in text
            assert "also found by Honda Civic 2014-2021" in text
            assert "Marketplace listing Alerts" in text
            assert "waiting for the Mac collector" in text
            assert _asks(page, "Stop following") == 2
            page.click('button:has-text("Open the record")')
            page.wait_for_selector("#sheet[data-open='1']")
            assert "Type R" in page.text_content("#sheet-title")
        finally:
            ctx.close()

    @pytest.mark.parametrize("link, says", [
        ("https://www.autotrader.ca/a/bmw/m5/winnipeg/manitoba/19_13166607_/",
         "autotrader.ca listing 13166607"),
        ("https://www.facebook.com/marketplace/item/4568081393436718/?ref=x",
         "Marketplace item 4568081393436718"),
        ("https://www.autotrader.ca/cars/bmw/m5/", "not one listing"),
        ("https://example.com/19_13166607_/", "Only an autotrader.ca listing"),
        ("not a link", "not a web address"),
    ])
    def test_a_pasted_listing_link_is_read_back(self, browser, site, payload, link, says):
        ctx, page, _ = _open(browser, site, _with_archive_parts(payload))
        try:
            page.fill("#pin-url", link)
            page.wait_for_timeout(150)
            assert says in _said(page.inner_text("#pin-out"))
            offered = _asks(page, "Save this listing")
            assert offered == (1 if "listing 1" in says or "item" in says else 0)
        finally:
            ctx.close()

    def test_a_link_already_saved_is_said_so(self, browser, site, payload):
        d = _with_archive_parts(payload)
        ctx, page, _ = _open(browser, site, d)
        try:
            page.fill("#pin-url", "https://www.facebook.com/marketplace/item/123/?ref=share")
            page.wait_for_timeout(150)
            assert "Already saved." in _said(page.inner_text("#pin-out"))
            assert _asks(page, "Save this listing") == 0
        finally:
            ctx.close()


class TestTheRecordOfOneCar:

    def test_every_price_is_listed_and_the_sellers_edits_too(self, browser, site, payload):
        ctx, page, errors = _open(browser, site, _with_archive_parts(payload), "#/listings")
        try:
            page.evaluate("openSheet('1')")
            page.wait_for_selector("#sheet[data-open='1']")
            body = _said(page.inner_text("#sheet-body"))
            assert "45 observations" in body
            assert page.locator("#sheet-body .pricetab tbody tr").count() == 45
            page.click("#sheet-body .pricetab summary")
            rows = page.locator("#sheet-body .pricetab tbody tr").all_inner_texts()
            assert "$45,500" in rows[0] and "−$100" in rows[0], rows[0]
            assert "first seen" in rows[-1]
            assert "Seller's edits" in body
            assert "Odometer" in body and "48,000 km → 48,500 km" in body
            assert not errors, errors
        finally:
            ctx.close()

    def test_a_gone_car_keeps_its_photos(self, browser, site, payload):
        ctx, page, _ = _open(browser, site, _with_archive_parts(payload), "#/listings")
        try:
            page.evaluate("openSheet('6')")
            page.wait_for_selector("#sheet[data-open='1']")
            assert page.locator("#sheet-body .gallery img").count() == 1
            assert "1 of 3 photos saved here, kept after it left" in _said(page.inner_text("#sheet-body"))
        finally:
            ctx.close()


class TestTheSettings:

    def test_the_options_are_drawn_from_what_the_bot_takes(self, browser, site, payload):
        ctx, page, errors = _open(browser, site, payload, "#/status")
        try:
            boxes = page.locator('[data-view="status"] [data-option]')
            assert boxes.count() == len(payload["bounds"]["options"])
            assert page.input_value('[data-option="dashboard.photos_per_car"]') == \
                str(payload["config"]["dashboard"]["photos_per_car"])
            assert page.get_attribute('[data-option="dashboard.photos_per_car"]', "max") == "24"
            # Untouched, nothing is offered.
            assert _asks(page, "Save these settings") == 0
            _type_into(page, '[data-option="dashboard.photos_per_car"]', "6")
            assert _asks(page, "Save these settings") == 1
            # A value the bot would refuse is said back, not sent.
            _type_into(page, '[data-option="dashboard.photos_per_car"]', "99")
            assert _asks(page, "Save these settings") == 0
            assert "Photos kept per car needs a whole number from 1 to 24" in \
                _said(page.inner_text('[data-view="status"]'))
            page.select_option('[data-option="notifications.digest"]', "false")
            _type_into(page, '[data-option="dashboard.photos_per_car"]', "6")
            assert _asks(page, "Save these settings") == 1
            assert not errors, errors
        finally:
            ctx.close()

    def test_a_local_page_cannot_hold_a_token(self, browser, site, payload):
        ctx, page, _ = _open(browser, site, payload, "#/status")
        try:
            text = _said(page.inner_text("#device"))
            assert "local, unencrypted view" in text
            assert page.locator("#gh-token").count() == 0
        finally:
            ctx.close()


class TestSendingFromThisDevice:

    def _unlocked(self, browser, private_site):
        ctx = browser.new_context(service_workers="block")
        page = ctx.new_page()
        page.goto(private_site["url"] + "#/status")
        _unlock(page)
        page.wait_for_selector("#device", timeout=30000)
        return ctx, page

    def test_a_token_is_kept_sealed_and_a_change_is_put_straight_in(self, browser, private_site):
        ctx, page = self._unlocked(browser, private_site)
        puts: list[dict] = []

        def github(route):
            req = route.request
            puts.append({"url": req.url, "method": req.method,
                         "auth": req.headers.get("authorization"),
                         "body": json.loads(req.post_data or "{}")})
            route.fulfill(status=201, content_type="application/json", body="{}")
        page.route("https://api.github.com/**", github)
        try:
            page.fill("#gh-token", "nonsense")
            page.click("#gh-token-save")
            assert "does not look like a GitHub token" in _said(page.inner_text("#device"))
            token = "github_pat_" + "A1" * 20
            page.fill("#gh-token", token)
            page.click("#gh-token-save")
            page.wait_for_selector("#gh-token-forget")
            assert "sent directly" in _said(page.inner_text("#device"))
            # Sealed: the token itself is nowhere in storage.
            stored = page.evaluate(
                "() => Object.keys(localStorage).map(k => localStorage.getItem(k)).join(' ')")
            assert token not in stored and "ghToken" in page.evaluate("() => Object.keys(localStorage).join(' ')")
            assert page.evaluate("loadToken()") == token

            # A change is now a button that puts the sealed file in itself.
            page.evaluate("document.querySelector('#device').after(askButton('Shortlist it', 'Shortlist', [{action: 'shortlist', listing: 'abc-1'}]))")
            page.click('button.btn--send:has-text("Shortlist it")')
            page.wait_for_selector("button.is-sent")
            assert len(puts) == 1
            put = puts[0]
            assert put["method"] == "PUT" and put["auth"] == f"Bearer {token}"
            assert put["url"].startswith("https://api.github.com/repos/someone/watch/contents/control/")
            assert put["url"].endswith(".enc")
            assert put["body"]["branch"] == "main"
            plain = V.unseal(private_site["vault"].key, base64.b64decode(put["body"]["content"]), "control")
            assert json.loads(plain)["changes"] == [{"action": "shortlist", "listing": "abc-1"}]
            # Neither the path nor the message says what the change is.
            assert "hortlist" not in put["url"] and "hortlist" not in put["body"]["message"]
            assert "The next check applies it" in _said(page.inner_text("body"))

            page.click("#gh-token-forget")
            page.wait_for_selector("#gh-token")
            assert page.evaluate("() => Object.keys(localStorage).join(' ')").count("ghToken") == 0
        finally:
            ctx.close()

    def test_a_refused_token_falls_back_to_the_github_page(self, browser, private_site):
        ctx, page = self._unlocked(browser, private_site)
        page.route("https://api.github.com/**",
                   lambda r: r.fulfill(status=401, content_type="application/json", body="{}"))
        try:
            page.fill("#gh-token", "ghp_" + "b" * 36)
            page.click("#gh-token-save")
            page.wait_for_selector("#gh-token-forget")
            page.evaluate("document.querySelector('#device').after(askButton('Shortlist it', 'Shortlist', [{action: 'shortlist', listing: 'abc-1'}]))")
            page.click('button.btn--send:has-text("Shortlist it")')
            link = page.locator('a.btn:has-text("Shortlist it on GitHub")')
            link.wait_for()
            assert link.get_attribute("href").startswith("https://github.com/someone/watch/new/main?filename=control%2F")
            assert "GitHub refused the token" in _said(page.inner_text("body"))
        finally:
            ctx.close()

    def test_locking_forgets_the_token(self, browser, private_site):
        ctx, page = self._unlocked(browser, private_site)
        try:
            page.fill("#gh-token", "ghp_" + "c" * 36)
            page.click("#gh-token-save")
            page.wait_for_selector("#gh-token-forget")
            page.click("#lock-now")
            page.wait_for_selector("#lock:not([hidden])", timeout=20000)
            assert "ghToken" not in page.evaluate("() => Object.keys(localStorage).join(' ')")
        finally:
            ctx.close()
