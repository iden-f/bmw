"""The page's editors, filters and counts, each against what the bot does.

Every test here is a place where the page said or offered one thing and the
bot did another: a rule box offering to clear a ceiling because a browser
read "45e" as empty, a preview that forgot the rules every search shares, a
count that took in a car the grid leaves out, a filter box that lost the
word being typed into it. Each was found by using the page, not by reading
it.

The page, the fixed data and the browser are the layout tests' own; the
locked site is the private-site tests' own.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from autotrader.dashboard import _area_of

from .test_dashboard_layout import _browser_path, browser, payload, site  # noqa: F401 - fixtures
from .test_dashboard_layout import TestTheCollectorOnThePage as _Collector
from .test_private_site import private_site  # noqa: F401 - fixture

UTC = timezone.utc
# The demo cars by id, as each card's accessible name begins.
NAMES = {"1": "2018 Honda Civic Type R", "2": "2016 Honda Civic Si 6-speed manual",
         "3": "2020 Honda Civic Type R Limited Edition", "4": "2015 Honda Civic no price yet"}


class Served:
    """data.json as the page reads it, republished mid-test."""

    def __init__(self, data: dict | None, status: int = 200):
        self.body = json.dumps(data)
        self.status = status

    def __call__(self, route):
        route.fulfill(status=self.status, content_type="application/json", body=self.body)

    def publish(self, data: dict) -> None:
        self.body = json.dumps(data)


def _open(browser, site, data, route="#/feed", *, width=1440, init=None, status=200):
    ctx = browser.new_context(viewport={"width": width, "height": 900},
                              service_workers="block")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    if init:
        page.add_init_script(init)
    page.set_default_timeout(10_000)
    served = Served(data, status)
    page.route("**/data.json", served)
    page.goto(site + route, wait_until="networkidle")
    page.wait_for_timeout(200)
    return ctx, page, errors, served


def _refresh(page):
    """What the clock does once a minute near a due check, done now."""
    page.evaluate("async () => { lastFetchAt = 0; await refreshData(); }")
    page.wait_for_timeout(150)


def _said(text: str) -> str:
    """Text as a person reads it: one space between words."""
    return " ".join(text.split())


def _card(page, car_id):
    return page.locator(f'.card[aria-label^="{NAMES[car_id]},"]')


def _focused(page) -> dict:
    return page.evaluate("""() => { const a = document.activeElement;
        return { tag: a.tagName, id: a.id, chip: a.dataset.chip || '',
                 pressed: a.getAttribute('aria-pressed'), view: a.dataset.viewLink || '' }; }""")


def _type_into(page, selector, text):
    """Keys, not fill(): a number box is filled by the browser's own rules."""
    page.click(selector)
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    page.keyboard.type(text)
    page.wait_for_timeout(100)


def _record_the_asks(page):
    """Every change the page offers, as the instructions it would send."""
    page.evaluate("""() => { window.__asked = []; const offer = askButton;
        askButton = (label, title, changes, prose) => {
          window.__asked.push(changes); return offer(label, title, changes, prose); }; }""")


def _asks(page, label):
    return page.locator("button, a", has_text=label).count()


def _newer(data: dict, minutes=5) -> dict:
    """The next publish: the same cars and one more, a few minutes on."""
    d = copy.deepcopy(data)
    at = datetime.now(UTC) + timedelta(minutes=minutes)
    car = copy.deepcopy(next(l for l in d["listings"] if l["id"] == "3"))
    car.update(id="7", title="2021 Honda Civic Si coupe", you={})
    d["listings"].append(car)
    d["generated_at"] = at.isoformat(timespec="seconds")
    return d


# ================================================================== searches

class TestTheRulesEditor:

    def with_a_shared_ceiling(self, payload):
        """$30,000 set for every search, and none on this one. The bot hides
        the two dearer cars by it, and says so by rule."""
        d = copy.deepcopy(payload)
        d["config"]["filters"]["max_price"] = 30000
        for car in d["listings"]:
            if car["id"] in ("1", "3"):
                car.update(filtered=True, filter_rule="max_price",
                           filter_reason="over the $30,000 you asked for")
        return d

    def test_a_blank_box_says_the_rule_every_search_shares(self, browser, site, payload):
        ctx, page, errors, _ = _open(browser, site, self.with_a_shared_ceiling(payload),
                                     "#/searches")
        try:
            assert page.get_attribute('input[id^="mx-"]', "placeholder") == "$30,000"
            text = _said(page.inner_text('[data-view="searches"]'))
            assert ("Left blank, a box takes the rule set for every search: "
                    "max asking $30,000.") in text
            # The preview agrees with the Listings tab: of the five live cars,
            # the $26,000 one and the one with no price pass. The fifth is
            # hidden by a rule the preview cannot re-run.
            assert _said(page.inner_text('[id^="pv-"]')).startswith(
                "2 of the 5 cars this search currently holds would pass under the rule as saved.")
            assert not errors, errors
        finally:
            ctx.close()

    def test_a_box_emptied_again_falls_back_to_the_shared_rule(self, browser, site, payload):
        ctx, page, _, _ = _open(browser, site, self.with_a_shared_ceiling(payload),
                                "#/searches")
        try:
            _type_into(page, 'input[id^="mx-"]', "50000")
            assert _said(page.inner_text('[id^="pv-"]')).startswith(
                "3 of the 5 cars this search currently holds would pass under the rule above.")
            assert _asks(page, "Apply this to") == 1
            _type_into(page, 'input[id^="mx-"]', "")
            assert _said(page.inner_text('[id^="pv-"]')).startswith("2 of the 5")
            assert _asks(page, "Apply this to") == 0
        finally:
            ctx.close()

    def with_a_saved_ceiling(self, payload):
        d = copy.deepcopy(payload)
        d["searches"][0]["rules"]["filters"]["max_price"] = 50000
        return d

    def test_text_that_is_not_a_number_never_offers_to_clear_the_rule(self, browser, site,
                                                                        payload):
        """Chrome reads "45e" in a number box as "", as Firefox and Safari read
        "45,000". Taken as empty, it offered to remove the saved ceiling."""
        ctx, page, errors, _ = _open(browser, site, self.with_a_saved_ceiling(payload),
                                     "#/searches")
        try:
            _record_the_asks(page)
            assert page.input_value('input[id^="mx-"]') == "50000"
            _type_into(page, 'input[id^="mx-"]', "45")
            page.evaluate("window.__asked = []")
            page.keyboard.type("e")
            page.wait_for_timeout(100)
            assert page.evaluate("document.querySelector('input[id^=\"mx-\"]').validity.badInput")
            assert _asks(page, "Apply this to") == 0
            assert page.evaluate("window.__asked") == []
            said = _said(page.inner_text('[id^="pv-"]'))
            assert said.startswith("Max asking needs a whole number from $1 to $10,000,000."), said
            # A number it takes offers the change again.
            _type_into(page, 'input[id^="mx-"]', "45000")
            assert _asks(page, "Apply this to") == 1
            assert page.evaluate("window.__asked").pop() == [
                {"action": "set-rule", "search": payload["searches"][0]["id"],
                 "rule": "max_price", "value": 45000}]
            assert not errors, errors
        finally:
            ctx.close()

    @pytest.mark.parametrize("box,typed,words", [
        ("y0", "20", "From year needs a whole number from 1900 to 2100."),
        ("y1", "2019.5", "To year needs a whole number from 1900 to 2100."),
        ("km", "0", "Within km needs a whole number from 1 km to 20,000 km."),
    ])
    def test_a_value_the_bot_would_refuse_is_not_offered(self, browser, site, payload,
                                                         box, typed, words):
        # A place to measure from, or there is no Within km box at all.
        d = copy.deepcopy(payload)
        d["searches"][0]["area"] = _area_of({"near": "Toronto, ON"})
        ctx, page, _, _ = _open(browser, site, d, "#/searches")
        try:
            _type_into(page, f'input[id^="{box}-"]', typed)
            assert _asks(page, "Apply this to") == 0
            assert _said(page.inner_text('[id^="pv-"]')).startswith(words)
        finally:
            ctx.close()


class TestTheOtherEditors:

    def test_a_marketplace_radius_past_its_bound_is_not_offered(self, browser, site, payload):
        ctx, page, _, _ = _open(browser, site, _Collector().payload_with(payload), "#/searches")
        try:
            _type_into(page, "#mp-radius_km", "900")
            assert _asks(page, "Save Marketplace settings") == 0
            assert "Radius, km needs a whole number from 1 to 500." in _said(
                page.inner_text('[data-view="searches"]'))
            _type_into(page, "#mp-radius_km", "250")
            assert _asks(page, "Save Marketplace settings") == 1
            _type_into(page, "#mp-place", "north york!")
            assert _asks(page, "Save Marketplace settings") == 0
        finally:
            ctx.close()

    def test_a_spelling_the_bot_would_refuse_is_not_offered(self, browser, site, payload):
        ctx, page, _, _ = _open(browser, site, _Collector().payload_with(payload), "#/searches")
        try:
            page.fill('input[id^="al-"]', "Civic Si, <Civic>")
            page.wait_for_timeout(100)
            assert _asks(page, "Save these spellings") == 0
            assert "“<Civic>” is not a model name the bot takes" in page.inner_text(
                '[data-view="searches"]')
            page.fill('input[id^="al-"]', "Civic Si, CivicSi")
            page.wait_for_timeout(100)
            assert _asks(page, "Save these spellings") == 1
        finally:
            ctx.close()

    @pytest.mark.parametrize("link,words", [
        ("http://www.autotrader.ca/cars/honda/civic/", "takes only addresses that start with https://"),
        ("https://fakeautotrader.ca/cars/honda/civic/", "That is a link to fakeautotrader.ca"),
        ("https://www.autotrader.ca.example.invalid/cars/", "not www.autotrader.ca"),
        # The home page and one car's page are not searches: the bot refuses both.
        ("https://www.autotrader.ca", "the autotrader.ca home page"),
    ])
    def test_a_link_the_bot_would_refuse_is_not_offered(self, browser, site, payload,
                                                        link, words):
        ctx, page, _, _ = _open(browser, site, payload, "#/searches")
        try:
            page.fill("#paste", link)
            page.wait_for_timeout(100)
            assert words in _said(page.inner_text("#paste-out"))
            assert _asks(page, "Add this search") == 0
        finally:
            ctx.close()

    def test_a_link_is_sent_as_the_browser_reads_it(self, browser, site, payload):
        """In the form the bot's own check reads: the name in lower case."""
        ctx, page, _, _ = _open(browser, site, payload, "#/searches")
        try:
            _record_the_asks(page)
            page.fill("#paste", "https://WWW.AUTOTRADER.CA/cars/honda/civic/")
            page.wait_for_timeout(100)
            assert _asks(page, "Add this search") == 1
            assert page.evaluate("window.__asked").pop() == [
                {"action": "add-search", "url": "https://www.autotrader.ca/cars/honda/civic/"}]
        finally:
            ctx.close()


# ================================================================== listings

class TestTheFilterBox:
    """The box is not redrawn under the fingers typing into it."""

    def test_a_letter_typed_mid_word_stays_where_it_was_typed(self, browser, site, payload):
        ctx, page, errors, _ = _open(browser, site, payload, "#/listings")
        try:
            page.evaluate("window.__q = document.getElementById('q')")
            page.click("#q")
            page.keyboard.type("hond civic")
            for _ in range(6):
                page.keyboard.press("ArrowLeft")
            page.keyboard.type("a")
            assert page.evaluate("document.getElementById('q') === window.__q")
            assert page.input_value("#q") == "honda civic"
            assert page.evaluate("document.getElementById('q').selectionStart") == 5
            # And the grid still follows what is typed.
            page.keyboard.type("zzz")
            assert page.locator(".card").count() == 0
            assert not errors, errors
        finally:
            ctx.close()

    def test_a_word_composed_by_a_phone_keyboard_arrives_whole(self, browser, site, payload):
        """Gboard and every input method for Chinese or Japanese compose a
        word in steps. Each step used to land in a new box, on top of the
        last: "civic" came out as "ccicivcivicivic"."""
        ctx, page, _, _ = _open(browser, site, payload, "#/listings")
        try:
            page.evaluate("window.__q = document.getElementById('q')")
            page.focus("#q")
            cdp = ctx.new_cdp_session(page)
            for step in ("c", "ci", "civ", "civi"):
                cdp.send("Input.imeSetComposition",
                         {"text": step, "selectionStart": len(step), "selectionEnd": len(step)})
            cdp.send("Input.insertText", {"text": "civic"})
            page.wait_for_timeout(150)
            assert page.input_value("#q") == "civic"
            assert page.evaluate("app.q") == "civic"
            assert page.evaluate("document.getElementById('q') === window.__q")
        finally:
            ctx.close()

    def test_escape_empties_the_box_and_leaves_the_keyboard_in_it(self, browser, site, payload):
        ctx, page, _, _ = _open(browser, site, payload, "#/listings")
        try:
            page.click("#q")
            page.keyboard.type("zzz")
            assert page.locator(".card").count() == 0
            page.keyboard.press("Escape")
            assert page.input_value("#q") == ""
            assert _focused(page)["id"] == "q"
            assert page.locator(".card").count() == 4
        finally:
            ctx.close()

    def test_a_new_car_rises_once_not_on_every_keystroke(self, browser, site, payload):
        """On a first visit every car is new to the browser."""
        ctx, page, _, _ = _open(browser, site, payload, "#/listings")
        try:
            assert page.locator(".card.is-fresh").count() > 0
            page.wait_for_timeout(700)
            page.click("#q")
            page.keyboard.type("h")
            assert page.locator(".card").count() == 4
            assert page.locator(".card.is-fresh").count() == 0
        finally:
            ctx.close()


class TestTheChips:

    def test_a_chip_pressed_from_the_keyboard_keeps_the_keyboard(self, browser, site, payload):
        ctx, page, _, _ = _open(browser, site, payload, "#/listings")
        try:
            for chip in ("new", "all", "hidden-toggle"):
                page.focus(f'.chips [data-chip="{chip}"]')
                page.keyboard.press("Enter")
                page.wait_for_timeout(100)
                now = _focused(page)
                assert now["chip"] == chip, now
            assert _focused(page)["pressed"] == "true"      # hidden cars included
        finally:
            ctx.close()

    def test_so_does_a_feed_chip(self, browser, site, payload):
        ctx, page, _, _ = _open(browser, site, payload, "#/feed")
        try:
            page.focus('.chips [data-chip="sent"]')
            page.keyboard.press("Enter")
            page.wait_for_timeout(100)
            now = _focused(page)
            assert (now["chip"], now["pressed"]) == ("sent", "true"), now
        finally:
            ctx.close()

    def test_marking_the_feed_seen_leaves_the_keyboard_in_the_page(self, browser, site, payload):
        ctx, page, _, _ = _open(browser, site, payload, "#/feed",
                                init="localStorage.setItem('atw:/:lastSeen',"
                                     " JSON.stringify('2000-01-01T00:00:00Z'))")
        try:
            button = page.locator("button", has_text=re.compile(r"^Mark \d+ as seen$"))
            button.focus()
            page.keyboard.press("Enter")
            page.wait_for_timeout(100)
            assert button.count() == 0
            assert _focused(page)["id"] == "main"
        finally:
            ctx.close()

    def test_the_shortcut_list_closes_with_escape_and_hands_the_keyboard_back(
            self, browser, site, payload):
        ctx, page, _, _ = _open(browser, site, payload, "#/feed")
        try:
            page.focus('[data-view-link="market"]')
            page.keyboard.press("?")
            page.wait_for_timeout(100)
            assert page.locator("#shortcuts").count() == 1
            page.keyboard.press("Escape")
            page.wait_for_timeout(100)
            assert page.locator("#shortcuts").count() == 0
            assert _focused(page)["view"] == "market"
            page.keyboard.press("?")
            page.click("#shortcuts button")
            assert _focused(page)["view"] == "market"
        finally:
            ctx.close()

    def test_on_a_phone_the_row_starts_at_its_start_and_keeps_the_chip_tapped(
            self, browser, site, payload):
        ctx, page, _, _ = _open(browser, site, payload, "#/listings", width=375)
        try:
            strip = ".chips"
            assert page.evaluate(f"document.querySelector('{strip}').scrollLeft") == 0
            assert page.evaluate(f"""() => {{ const s = document.querySelector('{strip}');
                return s.scrollWidth > s.clientWidth; }}"""), "the row fits; nothing to test"
            page.evaluate(f"""() => {{ const s = document.querySelector('{strip}');
                s.scrollLeft = s.scrollWidth; }}""")
            page.wait_for_timeout(200)
            page.click('.chips [data-chip="gone"]')
            page.wait_for_timeout(200)
            box = page.locator('.chips [aria-pressed="true"]').bounding_box()
            assert box["x"] >= 0 and box["x"] + box["width"] <= 375, box
        finally:
            ctx.close()

    def test_price_drops_leaves_out_a_car_your_rules_hide(self, browser, site, payload):
        """As New, Call for price and Private sellers do. Car 1 has come down;
        so has car 5, which a rule hides."""
        d = copy.deepcopy(payload)
        hidden = next(l for l in d["listings"] if l["id"] == "5")
        hidden["price_history"] = [{"at": "2026-01-01T00:00:00+00:00", "price": 30000},
                                   {"at": "2026-01-02T00:00:00+00:00", "price": 28000}]
        ctx, page, _, _ = _open(browser, site, d, "#/listings")
        try:
            chip = page.locator('.chips [data-chip="drops"]')
            assert chip.locator(".n").text_content() == "1"
            chip.click()
            assert page.locator(".card").count() == 1
            assert _card(page, "1").count() == 1
        finally:
            ctx.close()


class TestTheCounts:

    def test_the_tab_counts_what_the_live_chip_counts(self, browser, site, payload):
        """A car marked not interested leaves the Live chip, and the tab."""
        ctx, page, _, _ = _open(browser, site, payload, "#/listings")
        try:
            badge = '[data-view-link="listings"] .tab__n'
            assert page.text_content(badge) == "4"
            _card(page, "1").click()
            page.wait_for_selector("#sheet[data-open='1']")
            page.click('#sheet-body [data-key="dismissed"]')
            # At once, above the sheet.
            assert page.text_content(badge) == "3"
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)
            live = page.locator('.chips [data-chip="all"] .n').text_content()
            assert page.text_content(badge) == live == "3"
        finally:
            ctx.close()

    def test_the_market_counts_hidden_cars_the_page_does_not_carry(self, browser, site,
                                                                  payload):
        """The cap drops hidden cars first. Two more are stored than carried."""
        d = copy.deepcopy(payload)
        d["health"]["counts"]["filtered"] = 3
        d["health"]["left_out"] = 2
        ctx, page, _, _ = _open(browser, site, d, "#/market")
        try:
            head = _said(page.inner_text("#market-h + p"))
            assert "all 5 listings the searches returned, the 3 your rules hide included" in head
            assert "count only the 2 you could actually buy" in head
            assert "2 live · 3 hidden" in _said(page.inner_text('[data-view="market"] .stats'))
        finally:
            ctx.close()

    @pytest.mark.parametrize("stored,words", [
        (4, "Cars your rules hide are left out first, then the oldest of the cars that "
            "have gone."),
        (6, "2 of them are cars you could buy."),
    ])
    def test_the_note_on_the_cars_left_out_says_which_they_are(self, browser, site, payload,
                                                               stored, words):
        """The page carries four of the cars for sale. The bot counted four,
        or six: past the cap, the cut reaches cars you could buy too."""
        d = copy.deepcopy(payload)
        d["health"]["left_out"] = 2
        d["health"]["counts"]["active"] = stored
        ctx, page, _, _ = _open(browser, site, d, "#/listings")
        try:
            note = _said(page.locator(".note", has_text="stored than this page carries")
                         .inner_text())
            assert note.startswith("2 more cars are stored than this page carries")
            assert words in note, note
            assert "the oldest cars your rules hide" not in note
        finally:
            ctx.close()

    def test_past_300_cars_the_rest_are_a_press_away(self, browser, site, payload):
        d = copy.deepcopy(payload)
        base = next(l for l in d["listings"] if l["id"] == "2")
        for n in range(450):
            car = copy.deepcopy(base)
            car.update(id=f"big-{n}", price=20000 + n, you={})
            for k in ("thumb", "thumbs"):
                car.pop(k, None)
            d["listings"].append(car)
        ctx, page, errors, _ = _open(browser, site, d, "#/listings")
        try:
            page.select_option("#sort", "price")
            page.wait_for_timeout(200)
            assert page.locator(".grid > .card").count() == 300
            more = page.locator("button", has_text="Show 154 more")
            more.click()
            page.wait_for_timeout(200)
            # All 454 live cars, the one with no asking price last, under its
            # own divider.
            assert page.locator(".grid > .card").count() == 454
            assert page.locator("button", has_text=re.compile(r"^Show \d+ more$")).count() == 0
            assert page.text_content(".grid__split") == "1 car with no asking price"
            assert _card(page, "4").count() == 1
            # The keyboard is on the first car that was not there before.
            assert page.evaluate("[...document.querySelectorAll('.grid > .card')]"
                                 ".indexOf(document.activeElement)") == 300
            assert not errors, errors
        finally:
            ctx.close()

    def test_a_csv_cell_is_never_run_as_a_formula(self, browser, site, payload):
        ctx, page, _, _ = _open(browser, site, payload, "#/market")
        try:
            csv = page.evaluate("""() => {
                const car = app.data.listings.find(l => l.id === '3');
                car.seller = '=HYPERLINK("https://example.invalid","Audi RS 5")';
                car.trim = '+Type R';
                car.location = '@Toronto';
                car.filter_reason = 'one\\rtwo';
                return csvOfListings(); }""")
            row = next(line for line in csv.split("\n") if line.startswith("3,"))
            assert "\"'=HYPERLINK(\"\"https://example.invalid\"\",\"\"Audi RS 5\"\")\"" in row
            assert ",'+Type R," in row
            assert ",'@Toronto," in row
            assert '"one\rtwo"' in row
            # A number is a number.
            assert ",60000," in row
        finally:
            ctx.close()


# ================================================================== a refresh

class TestARefreshWaitsForAnEdit:

    def test_a_rule_being_typed_survives_new_data(self, browser, site, payload):
        ctx, page, _, served = _open(browser, site, payload, "#/searches")
        try:
            _type_into(page, 'input[id^="mx-"]', "45000")
            page.fill("#paste", "https://www.autotrader.ca/cars/honda/civic/")
            served.publish(_newer(payload))
            _refresh(page)
            assert page.input_value('input[id^="mx-"]') == "45000"
            # Away from the box, the typing is still unsent: it still waits.
            page.click("#searches-h")
            page.wait_for_timeout(1300)
            assert page.input_value('input[id^="mx-"]') == "45000"
            assert page.input_value("#paste") == "https://www.autotrader.ca/cars/honda/civic/"
            assert _asks(page, "Apply this to") == 1
            # The tab counts took the new car at once.
            assert page.text_content('[data-view-link="listings"] .tab__n') == "5"
        finally:
            ctx.close()

    def test_the_filter_box_keeps_the_keyboard_and_the_grid_catches_up(self, browser, site,
                                                                        payload):
        ctx, page, _, served = _open(browser, site, payload, "#/listings")
        try:
            page.click("#q")
            page.keyboard.type("civ")
            served.publish(_newer(payload))
            _refresh(page)
            # The box and what it holds stay; the cars under it take the news.
            assert _focused(page)["id"] == "q"
            assert page.input_value("#q") == "civ"
            assert page.locator(".card").count() == 5
            page.keyboard.type("ic")
            assert page.input_value("#q") == "civic"
            # Done typing: the bar catches up within a tick.
            page.click("#listings-h")
            page.wait_for_timeout(1300)
            assert page.locator(".card").count() == 5
            assert page.input_value("#q") == "civic"
        finally:
            ctx.close()

    @pytest.mark.parametrize("pick", ["sort", "search-pick"])
    def test_a_pick_does_not_hold_back_new_cars(self, browser, site, payload, pick):
        """A select hands the keyboard back to itself after a pick, and
        nothing moves it on while the owner scrolls or looks away. The grid
        waited with it, under a tab badge that had moved on."""
        ctx, page, errors, served = _open(browser, site, payload, "#/listings")
        try:
            page.select_option(f"#{pick}", index=1)
            page.wait_for_timeout(100)
            value = page.input_value(f"#{pick}")
            assert _focused(page)["id"] == pick
            served.publish(_newer(payload))
            _refresh(page)
            assert _focused(page)["id"] == pick
            assert page.input_value(f"#{pick}") == value
            live = page.locator('.chips [data-chip="all"] .n').text_content()
            badge = page.text_content('[data-view-link="listings"] .tab__n')
            assert page.locator(".card").count() == int(live) == int(badge) == 5
            assert not errors, errors
        finally:
            ctx.close()


# ================================================================== the sheet

class TestTheSheetEscapesOnce:

    def test_a_place_with_an_apostrophe_reads_as_typed(self, browser, site, payload):
        d = copy.deepcopy(payload)
        car = next(l for l in d["listings"] if l["id"] == "3")
        car.update(distance_km=120, distance_from="Ottawa's east end")
        ctx, page, _, _ = _open(browser, site, d, "#/listings")
        try:
            _card(page, "3").click()
            page.wait_for_selector("#sheet[data-open='1']")
            text = page.inner_text("#sheet-body")
            assert "120 km from Ottawa's east end" in text
            assert "&#39;" not in text
        finally:
            ctx.close()

    def test_a_title_s_dealer_copy_is_text_not_markup(self, browser, site, payload):
        """A title that starts with a pipe is published whole, and the part
        after the pipe was put into the page as HTML."""
        d = copy.deepcopy(payload)
        car = next(l for l in d["listings"] if l["id"] == "3")
        car["title"] = "|<img src=https://x.example/t.gif>"
        d["events"].insert(0, {"kind": "<i>odd</i>", "at": d["generated_at"], "listing_id": "3",
                               "title": car["title"]})
        asked: list[str] = []
        ctx, page, errors, _ = _open(browser, site, d, "#/listings")
        page.route("**/x.example/**", lambda r: (asked.append(r.request.url), r.abort()))
        try:
            page.evaluate("openSheet('3')")
            page.wait_for_selector("#sheet[data-open='1']")
            page.wait_for_timeout(200)
            assert page.evaluate("""() => [...document.querySelectorAll('#sheet-body img')]
                .filter(i => !i.closest('.gallery')).length""") == 0
            assert page.locator("#sheet-body i").count() == 0
            text = page.inner_text("#sheet-body")
            assert "Dealer copy: <img src=https://x.example/t.gif>" in text
            assert "<i>odd</i>" in text
            assert asked == []
            assert not errors, errors
        finally:
            ctx.close()


# ================================================================== status

class TestTheStatusTab:

    def test_a_retired_channel_says_it_is_switched_off(self, browser, site, payload):
        d = copy.deepcopy(payload)
        d["channels"]["email"] = dict(d["channels"].get("email") or {},
                                      label="Email (Gmail)", active=False, missing=[],
                                      disabled_reason="the credentials were rejected")
        ctx, page, _, _ = _open(browser, site, d, "#/status")
        try:
            row = page.locator("tr", has_text="Email (Gmail)")
            assert _said(row.inner_text()).endswith(
                "Switched off: the credentials were rejected.")
            # The bot's own words are not labelled twice.
            assert page.evaluate("labelled('Switched off automatically after 2 runs', "
                                 "'Switched off')") == "Switched off automatically after 2 runs"
        finally:
            ctx.close()

    def test_a_named_outside_timer_is_counted_as_the_schedule(self, browser, site, payload):
        d = copy.deepcopy(payload)
        d["coverage"] = dict(d["coverage"], too_short=False, new_install=False, pct=90.0,
                             expected=10, slots_covered=9, slots_scheduled=8,
                             by_trigger={"schedule": 3, "repository_dispatch:laptop": 5,
                                         "push": 1})
        ctx, page, _, _ = _open(browser, site, d, "#/status")
        try:
            tile = _said(page.locator('[data-view="status"] .stat').first.inner_text())
            assert ("8 of those 9 came from the schedule; the rest from a push to "
                    "the repository.") in tile, tile
            assert "laptop" not in tile
        finally:
            ctx.close()

    def schedule(self, payload, *, marketplace):
        d = _Collector().payload_with(payload) if marketplace else copy.deepcopy(payload)
        d["coverage"] = dict(d["coverage"], expected_interval_minutes=60,
                             by_trigger={"repository_dispatch:laptop": 1},
                             timekeeper="repository_dispatch:laptop")
        d["config"]["health"]["min_interval_minutes"] = 45
        return d

    def test_the_schedule_is_described_as_it_is_set(self, browser, site, payload):
        ctx, page, _, _ = _open(browser, site, self.schedule(payload, marketplace=True),
                                "#/status")
        try:
            setup = _said(page.inner_text('[data-view="status"] .setup'))
            assert ("A check is due every hour. Each Marketplace batch also wakes the bot, "
                    "which reads AutoTrader too once it has been 45 minutes, so checks land "
                    "45 to 60 minutes apart.") in setup, setup
            assert "two hours" not in setup and "90 minutes" not in setup
            assert "once it has been 45 minutes" in page.get_attribute("#clock-last", "title")
        finally:
            ctx.close()

    def test_without_a_collector_it_does_not_mention_one(self, browser, site, payload):
        ctx, page, _, _ = _open(browser, site, self.schedule(payload, marketplace=False),
                                "#/status")
        try:
            line = page.locator('[data-view="status"] .setup li', has_text="kept on time")
            assert _said(line.inner_text()).endswith("A check is due every hour.")
        finally:
            ctx.close()


class TestAPageThatCannotLoad:

    def test_every_tab_says_so_and_offers_to_try_again(self, browser, site, payload):
        ctx, page, _, _ = _open(browser, site, {"error": "missing"}, "#/feed", status=404)
        try:
            for view in ("listings", "status", "feed"):
                page.click(f'[data-view-link="{view}"]')
                page.wait_for_timeout(100)
                shown = page.locator(f'[data-view="{view}"]')
                assert "Could not load the data" in shown.inner_text(), view
                assert shown.locator("button", has_text="Try again").count() == 1, view
        finally:
            ctx.close()


class TestALockedPageOverPlainHttp:
    """No browser offers WebCrypto to a page served over http:// from
    anywhere but this machine, and every passphrase then read as wrong."""

    def test_it_says_where_it_can_be_unlocked(self, browser, private_site):
        insecure = browser.browser_type.launch(
            executable_path=_browser_path(),
            args=["--host-resolver-rules=MAP dash.invalid 127.0.0.1"])
        try:
            page = insecure.new_page()
            url = private_site["url"].replace("127.0.0.1", "dash.invalid")
            page.goto(url)
            page.wait_for_selector("#lock:not([hidden])")
            assert page.evaluate("window.isSecureContext") is False
            msg = page.inner_text("#lock-msg")
            assert "only be unlocked over a secure connection" in msg
            assert url.replace("http://", "https://") in msg
            assert "passphrase does not open" not in msg
            assert page.is_disabled("#lock-pass") and page.is_disabled("#lock-go")
        finally:
            insecure.close()
