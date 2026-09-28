"""What the page said once the two halves of the bot had each changed.

The back end learned new things and the page had not caught up: it drops
a car's photo copies when the car leaves, keeps the error of a Marketplace
search read only in part, refuses a distance with no place to measure it
from, records a check the site turned away, and says when its run log
reaches back less than a day. The page still said "no photo published",
showed nothing wrong, offered the distance the bot then refused, and
headed the coverage "24 hours". Each test here is one of those, and a few
places where the page claimed more than it knew: a car you stopped
watching shown as gone from the site, "hidden" on events from before a
rule applied, and a heading saying every car is hidden when some were
marked not interested.

The page, the fixed data and the browser are the layout tests' own; the
locked site is the private-site tests' own.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from autotrader import control, state as S
from autotrader.config import Config
from autotrader.dashboard import _area_of
from autotrader.state import State

from .test_dashboard_layout import DOCS
from .test_dashboard_layout import browser, payload, site  # noqa: F401 - fixtures
from .test_dashboard_layout import TestTheCollectorOnThePage as _Collector
from .test_private_site import _unlock, private_site  # noqa: F401 - fixture

UTC = timezone.utc
PHONE = 375


def _open(browser, site, data, route="#/feed", *, width=1440):
    ctx = browser.new_context(viewport={"width": width, "height": 900},
                              service_workers="block")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(10_000)
    body = json.dumps(data)
    page.route("**/data.json", lambda r: r.fulfill(
        status=200, content_type="application/json", body=body))
    page.goto(site + route, wait_until="networkidle")
    page.wait_for_timeout(200)
    return ctx, page, errors


def _said(text: str) -> str:
    """Text as a person reads it: one space between words."""
    return " ".join(text.split())


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


def _sideways(page) -> int:
    return page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")


def _car(d: dict, car_id: str) -> dict:
    return next(l for l in d["listings"] if l["id"] == car_id)


def _chip(page, label):
    return page.locator(".chips .chip", has_text=label).first


# ================================================================== photos

class TestAGoneCarsPhoto:
    """The bot drops its copies of a car's photos once the car leaves
    (thumbs.sync), and the seller's copy goes with the listing. The card
    said "photo not copied yet" and the sheet "no photo published"."""

    def gone_with_photos(self, payload):
        d = copy.deepcopy(payload)
        car = _car(d, "6")
        car.update(images=["https://images.example.invalid/civic-6.jpg"], photo_count=3)
        return d

    def test_the_card_says_its_photo_went_when_it_did(self, browser, site, payload):
        ctx, page, errors = _open(browser, site, self.gone_with_photos(payload), "#/listings")
        page.route("**/images.example.invalid/**", lambda r: r.abort())
        try:
            _chip(page, "Gone").click()
            page.wait_for_selector(".card--gone .shot__fallback")
            assert _said(page.text_content(".card--gone .shot__fallback")) \
                == "photo not kept after it left"
            assert not errors, errors
        finally:
            ctx.close()

    def test_the_sheet_says_so_too(self, browser, site, payload):
        ctx, page, errors = _open(browser, site, self.gone_with_photos(payload), "#/listings")
        try:
            page.evaluate("openSheet('6')")
            page.wait_for_selector("#sheet[data-open='1']")
            said = _said(page.text_content("#sheet-body .shot__fallback"))
            assert said.endswith("photos not kept after it left"), said
            assert "no photo published" not in said
            assert not errors, errors
        finally:
            ctx.close()

    def test_a_car_that_never_had_one_still_says_so(self, browser, site, payload):
        ctx, page, _ = _open(browser, site, payload, "#/listings")
        try:
            _chip(page, "Gone").click()
            page.wait_for_selector(".card--gone .shot__fallback")
            assert _said(page.text_content(".card--gone .shot__fallback")) == "no photo"
            page.evaluate("openSheet('6')")
            page.wait_for_selector("#sheet[data-open='1']")
            assert _said(page.text_content("#sheet-body .shot__fallback")).endswith(
                "no photo published")
        finally:
            ctx.close()

    def test_a_live_car_whose_photos_are_not_copied_yet_does_not_say_none(
            self, browser, site, payload):
        d = copy.deepcopy(payload)
        _car(d, "4").update(photo_count=5)
        ctx, page, _ = _open(browser, site, d, "#/listings")
        try:
            page.evaluate("openSheet('4')")
            page.wait_for_selector("#sheet[data-open='1']")
            assert _said(page.text_content("#sheet-body .shot__fallback")).endswith(
                "photos not copied yet")
        finally:
            ctx.close()


class TestDecryptedPhotosAreLetGo:
    """Each decrypted photo was a blob: URL kept for the life of the tab, so
    a page left open for days held every photo it had ever drawn."""

    def _opened(self, browser, private_site):
        ctx = browser.new_context(service_workers="block")
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(private_site["url"])
        _unlock(page)
        page.wait_for_function("""() => [...document.querySelectorAll('img[data-src]')]
            .some(i => i.complete && i.naturalWidth > 0)""", timeout=20_000)
        return ctx, page, errors

    def test_the_oldest_not_on_the_page_go_and_those_drawn_stay(self, browser, private_site):
        ctx, page, errors = self._opened(browser, private_site)
        try:
            said = page.evaluate("""async () => {
                const revoked = new Set();
                const revoke = URL.revokeObjectURL;
                URL.revokeObjectURL = u => { revoked.add(u); revoke(u); };
                const made = [];
                for (let i = 0; i < 250; i++) {
                  const u = URL.createObjectURL(new Blob(['x'], { type: 'text/plain' }));
                  made.push(u);
                  vault.photos.set(`thumbs/made-${i}.webp`, Promise.resolve(u));
                }
                trimPhotos();
                await new Promise(r => setTimeout(r, 50));
                const drawn = [...document.querySelectorAll('img[data-src]')];
                // Still a picture: a fresh image from the same address decodes.
                const shows = async u => { const i = new Image(); i.src = u;
                  try { await i.decode(); return i.naturalWidth > 0; } catch { return false; } };
                return {
                  size: vault.photos.size,
                  drawnKept: drawn.length > 0 && drawn.every(i => vault.photos.has(i.dataset.src)),
                  drawnShows: (await Promise.all(drawn.map(i => shows(i.src)))).every(Boolean),
                  drawnRevoked: drawn.some(i => revoked.has(i.src)),
                  revoked: made.filter(u => revoked.has(u)).length,
                  oldestGone: revoked.has(made[0]) && !vault.photos.has('thumbs/made-0.webp'),
                  newestKept: !revoked.has(made[249]) && vault.photos.has('thumbs/made-249.webp'),
                };
            }""")
            assert said == {"size": 200, "drawnKept": True, "drawnShows": True,
                            "drawnRevoked": False, "revoked": 51, "oldestGone": True,
                            "newestKept": True}, said
            # A drawing after it still shows the photo.
            page.evaluate("render()")
            page.wait_for_function("""() => [...document.querySelectorAll('img[data-src]')]
                .some(i => i.complete && i.naturalWidth > 0)""")
            assert not errors, errors
        finally:
            ctx.close()

    def test_a_new_photo_past_the_limit_starts_the_trim(self, browser, private_site):
        ctx, page, errors = self._opened(browser, private_site)
        try:
            page.evaluate("""() => {
                for (let i = 0; i < 250; i++) vault.photos.set(`thumbs/made-${i}.webp`,
                  Promise.resolve(URL.createObjectURL(new Blob(['x']))));
                photoUrl('thumbs/one-more.webp').catch(() => {});
            }""")
            page.wait_for_function("() => vault.photos.size <= 200", timeout=5_000)
            assert not errors, errors
        finally:
            ctx.close()


# ================================================================== the status tab

class TestAMarketplaceSearchReadInPart:
    """One of a search's queries failed and the rest read: the bot keeps
    the error with no failure in a row, and the page showed nothing."""

    def partly(self, payload, *, hours_ago=0.0):
        d = _Collector().payload_with(payload)
        at = _iso(datetime.now(UTC) - timedelta(hours=hours_ago))
        d["marketplace"]["searches"][0].update(
            last_ok=at, last_error="Timeout 45000ms exceeded", last_error_at=at,
            consecutive_failures=0)
        return d

    def _section(self, page):
        return page.locator("section.section",
                            has=page.locator("h2", has_text="Facebook Marketplace"))

    def test_it_is_the_last_problem_and_its_row_says_so(self, browser, site, payload):
        ctx, page, errors = _open(browser, site, self.partly(payload), "#/status",
                                  width=PHONE)
        try:
            section = self._section(page)
            section.wait_for()
            tile = _said(section.locator(".stat", has_text="Last problem").text_content())
            assert "Honda Civic 2014-2021: read only in part (Timeout 45000ms exceeded)" \
                in tile, tile
            cell = _said(section.locator('td[data-label="Last read"]').inner_text())
            assert "read only in part: Timeout 45000ms exceeded" in cell, cell
            assert _sideways(page) <= 0
            assert not errors, errors
        finally:
            ctx.close()

    def test_a_day_on_it_is_no_longer_the_last_problem(self, browser, site, payload):
        ctx, page, _ = _open(browser, site, self.partly(payload, hours_ago=30), "#/status")
        try:
            section = self._section(page)
            section.wait_for()
            assert section.locator(".stat", has_text="Last problem").count() == 0
            # The row still speaks of the last read, which was partial.
            assert "read only in part" in section.locator(
                'td[data-label="Last read"]').inner_text()
        finally:
            ctx.close()

    def test_a_whole_read_says_nothing_of_the_kind(self, browser, site, payload):
        ctx, page, _ = _open(browser, site, _Collector().payload_with(payload), "#/status")
        try:
            section = self._section(page)
            section.wait_for()
            assert "read only in part" not in section.inner_text()
        finally:
            ctx.close()


class TestTheCoverageSaysHowFarItReaches:

    def test_a_new_watch_is_not_headed_a_day(self, browser, site, payload):
        """The demo is a watch a moment old: window_hours is 0."""
        assert payload["coverage"]["window_hours"] == 0
        ctx, page, _ = _open(browser, site, payload, "#/status")
        try:
            head = page.locator(".stats .stat dt").first.text_content()
            assert head == "Coverage, under a minute", head
        finally:
            ctx.close()

    def cut_short(self, payload, **more):
        d = copy.deepcopy(payload)
        d["coverage"].update({"window_hours": 7.5, "asked_window_hours": 24,
                              "truncated": True, "too_short": False, "new_install": False,
                              "partial": False, "expected": 3, "slots_covered": 3,
                              "successful": 4, **more})
        return d

    @pytest.mark.parametrize("partial", [False, True])
    def test_a_log_that_reaches_back_less_than_a_day_says_so(self, browser, site, payload,
                                                             partial):
        ctx, page, _ = _open(browser, site, self.cut_short(payload, partial=partial),
                             "#/status", width=PHONE)
        try:
            tile = _said(page.locator(".stats .stat").first.text_content())
            assert tile.startswith("Coverage, 7.5 hours"), tile
            assert "3 of 3 2-hour slots in the 7.5 hours the run log reaches back" in tile
            assert "since the schedule changed" not in tile
            limits = _said(page.locator(".limits").inner_text())
            assert "The run log reaches back only 7.5 hours" in limits
            assert "how long the current schedule has been running" not in limits
            assert _sideways(page) <= 0
        finally:
            ctx.close()


class TestACheckTheSiteTurnedAway:
    """runs[].blocked: autotrader.ca answered with a page meant to stop
    robots. The Status tab showed only a check that failed."""

    def turned_away(self, payload, *, stood_down_since=False):
        d = copy.deepcopy(payload)
        now = datetime.now(UTC)
        runs = [{"at": _iso(now - timedelta(minutes=12)), "ok": False, "blocked": True,
                 "searches_run": 0, "searches_failed": 1, "trigger": "schedule"}]
        if stood_down_since:
            runs.insert(0, {"at": _iso(now - timedelta(minutes=2)), "ok": True,
                            "skipped": True, "trigger": "schedule"})
        d["runs"] = runs + d["runs"]
        d["last_run"] = runs[0]
        return d

    @pytest.mark.parametrize("stood_down_since", [False, True])
    def test_the_status_tab_says_so_plainly(self, browser, site, payload, stood_down_since):
        ctx, page, errors = _open(browser, site,
                                  self.turned_away(payload, stood_down_since=stood_down_since),
                                  "#/status", width=PHONE)
        try:
            text = _said(page.inner_text('[data-view="status"]'))
            assert "autotrader.ca turned the last check away 12m ago" in text, text[:400]
            assert _sideways(page) <= 0
            assert not errors, errors
        finally:
            ctx.close()

    def test_a_check_let_through_says_nothing_of_it(self, browser, site, payload):
        ctx, page, _ = _open(browser, site, payload, "#/status")
        try:
            assert "turned the last check away" not in page.inner_text('[data-view="status"]')
        finally:
            ctx.close()


class TestTheLastCheckFromTheRuns:
    """Without last_check the page works it out from the runs, as
    State.last_check does. searches_run and searches_failed never overlap,
    so `failed < ran` passed over a check that read one search of two."""

    def test_any_search_read_is_a_check(self, browser, site, payload):
        ctx, page, _ = _open(browser, site, payload, "#/feed")
        try:
            got = page.evaluate("""() => [
                lastCheck({ runs: [
                  { at: 'd', skipped: true, ok: true },
                  { at: 'c', searches_run: 0, searches_failed: 1, ok: true },
                  { at: 'b', searches_run: 1, searches_failed: 1, ok: false },
                  { at: 'a', searches_run: 2, searches_failed: 0, ok: true } ] }).at,
                lastCheck({ runs: [ { at: 'b', ok: false }, { at: 'a', ok: true } ] }).at,
                lastCheck({ runs: [ { at: 'a', searches_failed: 2, ok: false } ],
                            last_run: { at: 'z' } }).at,
            ]""")
            assert got == ["b", "a", "z"]
        finally:
            ctx.close()


# ================================================================== the searches tab

def _bot_and_page_area(tmp_path, own: dict, everywhere: dict):
    """Whether the bot takes a distance for this search, and the area the
    page is given for it, from the same settings."""
    cfg = Config.defaults(tmp_path / "config.json")
    search = cfg.add_search("https://www.autotrader.ca/cars/honda/civic/on/", "Honda Civic")
    cfg.data["filters"].update(everywhere)
    cfg.data["searches"][0].setdefault("filters", {}).update(own)
    area = _area_of(cfg.rules_for(cfg.searches[0])["filters"])
    out = control.apply(cfg, State(path=tmp_path / "state.json"), [
        {"action": "set-rule", "search": search.id, "rule": "max_distance_km", "value": 100}])
    return out.changed, area


class TestTheDistanceBoxNeedsAPlace:
    """The bot refuses set-rule max_distance_km for a search with no place
    it can find. The page offered the box anyway, and the change it sent was
    refused on the next check."""

    @pytest.mark.parametrize("own, everywhere, why", [
        ({}, {}, "this search has no place to measure a distance from"),
        ({"near": "Toronto, ON"}, {}, None),
        ({}, {"near": "Ottawa, ON"}, None),
        ({"near": "Anytown, ON"}, {}, "the bot cannot find Anytown, ON"),
    ])
    def test_the_box_is_offered_exactly_when_the_bot_takes_it(
            self, browser, site, payload, tmp_path, own, everywhere, why):
        takes, area = _bot_and_page_area(tmp_path, own, everywhere)
        d = copy.deepcopy(payload)
        d["searches"][0]["area"] = area
        ctx, page, errors = _open(browser, site, d, "#/searches", width=PHONE)
        try:
            offered = page.locator('input[id^="km-"]').count() == 1
            assert offered == takes == (why is None)
            text = _said(page.inner_text('[data-view="searches"]'))
            if why:
                assert f"No Within km box: {why}" in text, text
                assert 'python -m autotrader set near "…" --search' in text
            else:
                assert "No Within km box" not in text
            assert _sideways(page) <= 0
            assert not errors, errors
        finally:
            ctx.close()

    def test_the_payload_says_whether_a_place_alone_can_be_found(self):
        assert _area_of({"near": "Toronto, ON"})["resolved"] is True
        assert _area_of({"near": "Anytown, ON"})["resolved"] is False


# ================================================================== listings

class TestACarNoLongerWatched:
    """A search taken away, or Marketplace switched off for one, writes its
    cars off as gone. They did not leave the market, and the page said they
    had: "Gone" on the card and the chip, "Gone from the site" in the
    sheet."""

    def with_retired(self, payload):
        d = copy.deepcopy(payload)
        model = _car(d, "6")
        for car_id, title, why in (
                ("7", "2017 Honda Civic from a removed search", {"gone_reason": S.SEARCH_REMOVED}),
                ("8", "2018 Honda Civic off Marketplace", {"gone_reason": S.SWITCHED_OFF}),
                ("9", "2016 Honda Civic from an older record",
                 {"quiet_reason": S.SEARCH_REMOVED_QUIET})):
            car = copy.deepcopy(model)
            car.update(id=car_id, title=title, **why)
            d["listings"].append(car)
        return d

    def test_it_has_its_own_chip_and_says_so_on_its_card(self, browser, site, payload):
        ctx, page, errors = _open(browser, site, self.with_retired(payload), "#/listings",
                                  width=PHONE)
        try:
            assert _chip(page, "Gone").locator(".n").inner_text() == "1"
            _chip(page, "No longer watched").click()
            page.wait_for_timeout(100)
            cards = page.locator('[data-view="listings"] .card')
            assert cards.count() == 3
            for i in range(3):
                assert cards.nth(i).locator(".flag").text_content() == "No longer watched"
                heard = cards.nth(i).get_attribute("aria-label")
                assert "no longer watched" in heard and ", gone" not in heard, heard
            _chip(page, "Gone").click()
            page.wait_for_timeout(100)
            assert cards.count() == 1
            assert cards.first.locator(".flag").text_content() == "Gone"
            assert _sideways(page) <= 0
            assert not errors, errors
        finally:
            ctx.close()

    def test_the_sheet_does_not_say_it_left_the_site(self, browser, site, payload):
        ctx, page, _ = _open(browser, site, self.with_retired(payload), "#/listings")
        try:
            page.evaluate("openSheet('7')")
            page.wait_for_selector("#sheet[data-open='1']")
            flag = _said(page.text_content("#sheet-body .flag--gone"))
            assert flag.startswith("No longer watched"), flag
            assert "Gone from the site" not in page.inner_text("#sheet-body")
            page.evaluate("closeSheet(); openSheet('6')")
            assert _said(page.text_content("#sheet-body .flag--gone")).startswith(
                "Gone from the site")
        finally:
            ctx.close()

    def test_the_words_are_the_bot_s(self):
        """The page tells these cars by the reasons the bot records."""
        js = (DOCS / "app.js").read_text(encoding="utf-8")
        for said in (S.SEARCH_REMOVED, S.SWITCHED_OFF, S.SEARCH_REMOVED_QUIET):
            assert f"'{said}'" in js, said


class TestAnEmptyListSaysWhyExactly:
    """"Every car found is hidden by one of your rules" was the heading when
    the other live cars were marked not interested, and "every car it was
    watching has left the market" when live cars were."""

    def _state(self, page):
        return (_said(page.inner_text('[data-view="listings"] .state h2')),
                _said(page.inner_text('[data-view="listings"] .state p')),
                page.locator('[data-view="listings"] .state .btn').all_inner_texts())

    def test_hidden_and_marked_not_interested(self, browser, site, payload):
        ctx, page, _ = _open(browser, site, payload, "#/listings")
        try:
            page.evaluate("""() => { for (const id of ['1', '2', '3', '4'])
                marks.set(id, {dismissed: true}); renderListings(); }""")
            head, body, offers = self._state(page)
            assert head == "Every live car is hidden by a rule or marked not interested"
            assert ("holding 5 live cars. Your rules hide 1 of them, and you marked the "
                    "rest not interested.") in body, body
            assert offers == ["Show the 1 hidden", "Show the 4 not interested"]
        finally:
            ctx.close()

    def test_hidden_alone(self, browser, site, payload):
        d = copy.deepcopy(payload)
        for car_id in ("1", "2", "3", "4"):
            _car(d, car_id).update(filtered=True, filter_reason="over the price you set")
        ctx, page, _ = _open(browser, site, d, "#/listings")
        try:
            head, body, offers = self._state(page)
            assert head == "Every live car is hidden by one of your rules"
            assert "holding 5 live cars, and your rules hide all of them" in body, body
            assert offers == ["Show the 5 hidden"]
        finally:
            ctx.close()

    def test_every_live_car_marked_while_others_have_gone(self, browser, site, payload):
        ctx, page, _ = _open(browser, site, payload, "#/listings")
        try:
            page.evaluate("""() => { for (const id of ['1', '2', '3', '4', '5'])
                marks.set(id, {dismissed: true}); renderListings(); }""")
            head, _, offers = self._state(page)
            assert head == "Every live car is one you marked not interested"
            assert offers == ["Show the 5 not interested"]
        finally:
            ctx.close()


# ================================================================== the feed

class TestHiddenIsSaidAsItIsNow:
    """Each event carries the car's filtered flag as it is now, and a rule
    set since does not reach back: a car hidden today read "hidden" on the
    day it was announced."""

    def test_a_row_says_the_car_is_hidden_now(self, browser, site, payload):
        d = copy.deepcopy(payload)
        car = _car(d, "5")
        d["events"].insert(0, {"kind": "price_drop", "at": d["generated_at"],
                               "listing_id": "5", "title": car["title"],
                               "old_price": 29000, "new_price": 28000, "delta": -1000,
                               "filtered": True, "filter_reason": "over the price you set",
                               "delivery": {"state": "sent", "at": d["generated_at"]}})
        ctx, page, errors = _open(browser, site, d, "#/feed")
        try:
            rows = page.locator('.ev[data-id="5"] .ev__sub')
            assert rows.count() >= 1
            for text in rows.all_inner_texts():
                assert "hidden now — " in text, text
            assert not errors, errors
        finally:
            ctx.close()

    def test_a_car_told_about_before_a_rule_hid_it_says_it_was(self, browser, site, payload):
        d = copy.deepcopy(payload)
        told = d["generated_at"]
        _car(d, "5")["notified_at"] = told
        ctx, page, _ = _open(browser, site, d, "#/listings")
        try:
            page.evaluate("openSheet('5')")
            page.wait_for_selector("#sheet[data-open='1']")
            why = _said(page.inner_text("#sheet-body .why"))
            assert why.startswith("You were told about this at"), why
            assert "It is hidden now by a rule on" in why
            assert "not told" not in why
        finally:
            ctx.close()

