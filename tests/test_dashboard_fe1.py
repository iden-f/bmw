"""The page over time: a tab left open, a sheet left open, a mark taken back.

The layout tests open the page once and measure it. Everything here happens
after that: data lands while a car is open, the clock runs on with no check,
a mark the bot already holds is taken back, Back is pressed with a car open.
Each of these went wrong on a page that looked right when it loaded.

The page, the fixed data and the browser are the layout tests' own; the
locked site is the private-site tests' own.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import ThreadingHTTPServer

import pytest

from autotrader import clock

from .test_dashboard_layout import _demo_payload, browser, payload, site  # noqa: F401 - fixtures
from .test_private_site import DOCS, STORED, TITLE, _Quiet, _unlock, private_site  # noqa: F401

UTC = timezone.utc
MARKS = "atw:/:marks"       # the page's namespace, for a page served at /


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def _checked_at(data: dict, at: datetime) -> dict:
    """The payload, as if its one check ran at ``at`` and was published then."""
    d = copy.deepcopy(data)
    stamp = _iso(at)
    d["generated_at"] = stamp
    for run in d["runs"]:
        run["at"] = stamp
    d["last_run"] = d["runs"][0]
    d["last_check"] = d["runs"][0]
    return d


def _with_new_car(data: dict, at: datetime) -> dict:
    """The next publish: one more car, and the event announcing it."""
    d = copy.deepcopy(data)
    car = copy.deepcopy(next(l for l in d["listings"] if l["id"] == "3"))
    car.update(id="7", title="2021 Honda Civic Si coupe", you={})
    d["listings"].append(car)
    d["events"] = [{"kind": "new", "at": _iso(at), "listing_id": "7",
                    "title": car["title"], "price": car["price"]}] + d["events"]
    d["generated_at"] = _iso(at)
    return d


class Served:
    """data.json as the page reads it, republished mid-test."""

    def __init__(self, data: dict):
        self.body = json.dumps(data)
        self.hits = 0

    def __call__(self, route):
        self.hits += 1
        route.fulfill(status=200, content_type="application/json", body=self.body)

    def publish(self, data: dict) -> None:
        self.body = json.dumps(data)


def _open(browser, site, data, route="#/feed", *, width=1440, clock=None, init=None):
    ctx = browser.new_context(viewport={"width": width, "height": 900},
                              service_workers="block")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    if clock is not None:
        page.clock.install(time=clock)
    if init:
        page.add_init_script(init)
    page.set_default_timeout(10_000)
    served = Served(data)
    page.route("**/data.json", served)
    page.goto(site + route, wait_until="networkidle")
    page.wait_for_timeout(200)
    return ctx, page, errors, served


def _refresh(page):
    """What the clock does once a minute near a due check, done now."""
    page.evaluate("async () => { lastFetchAt = 0; await refreshData(); }")
    page.wait_for_timeout(150)


# The demo cars by id, as each card's accessible name begins.
NAMES = {"1": "2018 Honda Civic Type R", "2": "2016 Honda Civic Si 6-speed manual",
         "3": "2020 Honda Civic Type R Limited Edition", "4": "2015 Honda Civic no price yet"}
TOGGLES = ("shortlisted", "muted", "dismissed")


def _card(page, car_id):
    return page.locator(f'.card[aria-label^="{NAMES[car_id]},"]')


def _toggle(page, key):
    """A mark's toggle on the open sheet, found by its place in the row."""
    return page.locator("#sheet-body .chip[aria-pressed]").nth(TOGGLES.index(key))


def _focused(page) -> str:
    """The accessible name of what has the keyboard, or its tag."""
    return page.evaluate("(document.activeElement.getAttribute('aria-label')"
                         " || document.activeElement.tagName)")


def _pressed(page, key):
    return _toggle(page, key).get_attribute("aria-pressed") == "true"


class TestATabLeftOpen:
    """The pill and its alarm judge the age of the last check, and the age
    goes on growing whether or not new data arrives."""

    def test_the_pill_turns_when_no_check_lands(self, browser, site, payload):
        start = datetime.now(UTC).replace(microsecond=0) - timedelta(days=1)
        d = _checked_at(payload, start)
        ctx, page, errors, _ = _open(browser, site, d, clock=start + timedelta(minutes=5))
        try:
            assert page.get_attribute("#trust", "data-state") == "ok"
            assert page.text_content("#trust-text") == "Checked 5m ago"
            # Its words keep time with the strip under it.
            page.clock.run_for(10 * 60_000)
            assert page.text_content("#trust-text") == "Checked 15m ago"
            # Past the bot's own silent_after_hours, with the same file. The
            # page asks for a fresher one first, and judges once it has its
            # answer.
            page.clock.fast_forward("07:00:00")
            page.wait_for_timeout(300)
            page.clock.run_for(1_000)
            assert page.get_attribute("#trust", "data-state") == "stale"
            assert page.text_content("#trust-text") == "Checked 7h ago"
            assert page.is_visible("#alarm")
            assert page.text_content("#alarm-text").startswith("No check has landed")
            # Read out as it turns, and not again each minute after.
            assert page.get_attribute("#trust", "aria-live") == "polite"
            # Within ten minutes the age in the words has moved on.
            page.clock.run_for(10 * 60_000)
            assert page.get_attribute("#trust", "aria-live") == "off"
            assert page.get_attribute("#alarm", "aria-live") == "off"
            assert not errors, errors
        finally:
            ctx.close()

    def test_a_tab_brought_back_waits_for_the_copy_on_its_way(self, browser, site, payload):
        """Hidden overnight while checks went on landing. The copy it holds is
        eight hours old and a fresh one is on its way: until that lands, or
        plainly will not, no alarm says no check has landed."""
        start = datetime.now(UTC).replace(microsecond=0)
        d = _checked_at(payload, start - timedelta(minutes=5))
        ctx, page, errors, served = _open(browser, site, d, "#/feed", clock=start)
        held = []
        try:
            assert page.is_hidden("#alarm")
            page.evaluate("""() => { window.away = true;
                Object.defineProperty(document, 'hidden', {configurable: true,
                                                           get: () => window.away});
                document.dispatchEvent(new Event('visibilitychange')); }""")
            page.clock.fast_forward("08:00:00")
            served.publish(_checked_at(payload, start + timedelta(hours=7, minutes=50)))
            # A phone's network is slow to wake: the file comes when let go.
            page.unroute("**/data.json")
            page.route("**/data.json", lambda route: held.append(route))
            page.evaluate("""() => { window.away = false;
                document.dispatchEvent(new Event('visibilitychange')); }""")
            page.clock.run_for(3_000)
            assert len(held) == 1, "the page did not ask for a fresh copy"
            assert page.is_hidden("#alarm")
            # A copy that never comes does not keep the green up for long.
            page.clock.run_for(15_000)
            assert page.is_visible("#alarm")
            assert page.text_content("#alarm-text").startswith("No check has landed")
            served(held.pop())
            page.wait_for_timeout(300)
            assert page.is_hidden("#alarm")
            assert page.text_content("#trust-text") == "Checked 10m ago"
            assert not errors, errors
        finally:
            ctx.close()

    def test_a_failure_is_not_cleared_by_a_firing_that_stood_down(self, browser, site, payload):
        now = datetime.now(UTC)
        d = copy.deepcopy(payload)
        failed = {"at": _iso(now - timedelta(minutes=35)), "ok": False, "searches_run": 2,
                  "searches_failed": 1, "errors": ["Honda Civic 2014-2021: timed out"]}
        stood_down = {"at": _iso(now - timedelta(minutes=5)), "ok": True, "skipped": True,
                      "searches_run": 0}
        d["runs"] = [stood_down, failed]
        d["last_run"], d["last_check"] = stood_down, failed
        ctx, page, _, _ = _open(browser, site, d)
        try:
            assert page.get_attribute("#trust", "data-state") == "bad"
            assert page.text_content("#trust-text").startswith("Last check failed")
            assert "timed out" in page.text_content("#alarm-detail")
        finally:
            ctx.close()


@pytest.mark.parametrize("gate", ["2027-01-01T00:01:00Z", "2026-01-31T12:00:00Z"])
def test_inside_the_date_gate_the_page_reads_its_data_as_just_made(
        browser, site, tmp_path, monkeypatch, gate):
    """scripts/time-gate.sh moves the bot's clock, and a browser keeps the
    host's. Built there, the demo was months away to the page: every event
    unread, or no car new, and the tests counting them failed at every date
    but today's."""
    monkeypatch.setenv(clock.ENV_VAR, gate)
    monkeypatch.chdir(tmp_path)
    d = _demo_payload(tmp_path)
    # And the gate's clock is back for whatever comes next.
    assert clock.now() == datetime.fromisoformat(gate.replace("Z", "+00:00"))
    ctx, page, errors, _ = _open(browser, site, d, "#/listings")
    try:
        assert page.text_content('[data-view-link="feed"] .tab__n') == ""
        assert page.locator('.chips [data-chip="new"]').count() == 1
        assert not errors, errors
    finally:
        ctx.close()


class TestDataThatLandsWhileACarIsOpen:

    def test_is_drawn_once_the_sheet_closes(self, browser, site, payload):
        now = datetime.now(UTC)
        ctx, page, errors, served = _open(browser, site,
                                          _checked_at(payload, now - timedelta(hours=3)),
                                          "#/listings")
        try:
            assert page.locator(".card").count() == 4
            assert page.text_content("#trust-text") == "Checked 3h ago"
            _card(page, "3").click()
            page.wait_for_selector("#sheet[data-open='1']")
            served.publish(_with_new_car(_checked_at(payload, now),
                                         now + timedelta(minutes=1)))
            _refresh(page)
            # The tabs and the pill sit outside the sheet and take the news
            # at once.
            assert page.text_content('[data-view-link="listings"] .tab__n') == "5"
            assert page.text_content('[data-view-link="feed"] .tab__n') == "1"
            assert page.text_content("#trust-text") == "Checked just now"
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)
            assert page.locator(".card").count() == 5
            # And focus is back on the car it left, in the new drawing.
            assert _focused(page).startswith(NAMES["3"] + ","), _focused(page)
            assert not errors, errors
        finally:
            ctx.close()

    def test_the_badges_follow_a_refresh_with_no_sheet(self, browser, site, payload):
        ctx, page, _, served = _open(browser, site, payload, "#/feed")
        try:
            assert page.text_content('[data-view-link="feed"] .tab__n') == ""
            page.focus('[data-view-link="feed"]')
            served.publish(_with_new_car(payload, datetime.now(UTC) + timedelta(minutes=1)))
            _refresh(page)
            assert page.text_content('[data-view-link="feed"] .tab__n') == "1"
            assert page.text_content('[data-view-link="listings"] .tab__n') == "5"
            # A keyboard resting on a tab is still on it.
            assert page.evaluate("document.activeElement.dataset.viewLink") == "feed"
        finally:
            ctx.close()


class TestYourMarks:
    """The bot's record is the published `you`; this device's taps stand in
    for it only until the bot has them."""

    def test_the_fixture_draws_a_mark_the_bot_holds(self, payload):
        car = next(l for l in payload["listings"] if l["id"] == "2")
        assert car["you"] == {"shortlisted": True, "note": "check the tyres"}

    def test_a_mark_the_bot_holds_can_be_taken_back(self, browser, site, payload):
        ctx, page, errors, _ = _open(browser, site, payload, "#/listings")
        try:
            assert "card--mine" in _card(page, "2").get_attribute("class")
            assert _card(page, "2").locator(".yours").text_content() == "check the tyres"
            _card(page, "2").click()
            page.wait_for_selector("#sheet[data-open='1']")
            assert _pressed(page, "shortlisted")
            _toggle(page, "shortlisted").click()
            assert not _pressed(page, "shortlisted")
            # One word for one toggle: the state is in aria-pressed.
            assert _toggle(page, "shortlisted").text_content() == "Shortlist"
            page.fill("#sheet-body textarea", "")
            page.click("#sheet-body >> text=Save note")
            assert page.input_value("#sheet-body textarea") == ""
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)
            assert "card--mine" not in _card(page, "2").get_attribute("class")
            assert _card(page, "2").locator(".yours").count() == 0
            assert not errors, errors
        finally:
            ctx.close()

    def test_shortlisting_and_dismissing_exclude_each_other(self, browser, site, payload):
        ctx, page, _, _ = _open(browser, site, payload, "#/listings")
        try:
            _card(page, "1").click()
            page.wait_for_selector("#sheet[data-open='1']")
            _toggle(page, "dismissed").click()
            _toggle(page, "shortlisted").click()
            assert _pressed(page, "shortlisted") and not _pressed(page, "dismissed")
            _toggle(page, "dismissed").click()
            assert _pressed(page, "dismissed") and not _pressed(page, "shortlisted")
        finally:
            ctx.close()

    def test_every_chip_counts_what_its_grid_shows(self, browser, site, payload):
        """Car 1 has a price drop. Marked not interested, it leaves every grid
        but "Not interested", and so it must leave every count too."""
        ctx, page, _, _ = _open(browser, site, payload, "#/listings")
        try:
            page.evaluate("marks.set('1', {dismissed: true}); renderListings()")
            labels = page.locator(".chips .chip .n").evaluate_all(
                "ns => ns.map(n => n.parentElement.firstChild.textContent)")
            assert "Price drops" not in labels, labels
            for label in labels:
                chip = page.locator(".chips .chip", has_text=label).first
                chip.click()
                page.wait_for_timeout(100)
                chip = page.locator(".chips .chip[aria-pressed='true']").first
                shown = int(chip.locator(".n").text_content())
                assert shown == page.locator(".card").count(), label
        finally:
            ctx.close()

    def test_a_list_emptied_by_your_marks_says_so(self, browser, site, payload):
        ctx, page, _, _ = _open(browser, site, payload, "#/listings")
        try:
            page.evaluate("""() => { for (const l of app.data.listings)
                marks.set(l.id, {dismissed: true}); renderListings(); }""")
            assert "marked not interested" in page.inner_text(".state h2")
            page.click("text=Show the 6 not interested")
            assert page.locator(".card").count() == 6
        finally:
            ctx.close()

    def test_the_keyboard_stays_on_the_toggle_and_comes_back_to_the_car(
            self, browser, site, payload):
        ctx, page, _, _ = _open(browser, site, payload, "#/listings")
        try:
            _card(page, "3").focus()
            page.keyboard.press("Enter")
            page.wait_for_selector("#sheet[data-open='1']")
            _toggle(page, "shortlisted").focus()
            page.keyboard.press("Enter")
            assert page.evaluate("document.activeElement.dataset.key") == "shortlisted"
            assert _pressed(page, "shortlisted")
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)
            assert _focused(page).startswith(NAMES["3"] + ","), _focused(page)
            assert "card--mine" in _card(page, "3").get_attribute("class")
        finally:
            ctx.close()

    def test_this_device_gives_way_to_the_bot(self, browser, site, payload):
        """A tap the bot has since recorded, one it never got, and one just
        made. This page is not locked, so it cannot send a change: the one
        the bot never got is all there is of it, and it stays. A locked page
        lets it go (test_private_site)."""
        published = datetime.fromisoformat(payload["generated_at"])
        local = {
            # Older than the publish by far, and the bot has no such mark.
            "1": {"shortlisted": True, "at": _iso(published - timedelta(hours=2))},
            # The bot has it now.
            "2": {"shortlisted": True, "at": _iso(published)},
            # Just made; the change is on its way.
            "3": {"muted": True, "at": _iso(published)},
        }
        init = f"localStorage.setItem({json.dumps(MARKS)}, {json.dumps(json.dumps(local))})"
        ctx, page, _, _ = _open(browser, site, payload, "#/listings", init=init)
        try:
            kept = json.loads(page.evaluate(f"localStorage.getItem({json.dumps(MARKS)})"))
            assert set(kept) == {"1", "3"}, kept
            assert "card--mine" in _card(page, "1").get_attribute("class")
            assert "card--mine" in _card(page, "2").get_attribute("class")
            assert "card--muted" in _card(page, "3").get_attribute("class")
            # The sheet says which marks are only on this device.
            _card(page, "3").click()
            page.wait_for_selector("#sheet[data-open='1']")
            assert "on this device" in page.inner_text("#sheet-body")
        finally:
            ctx.close()

    def test_a_tap_on_a_page_that_cannot_send_it_outlasts_a_later_check(
            self, browser, site, payload):
        """The local viewer rebuilds the data on every visit, stamped with
        the time. A mark set there was gone half an hour later."""
        now = datetime.now(UTC).replace(microsecond=0)
        d = _checked_at(payload, now)
        ctx, page, errors, served = _open(browser, site, d, "#/listings")
        try:
            _card(page, "3").click()
            page.wait_for_selector("#sheet[data-open='1']")
            _toggle(page, "shortlisted").click()
            assert "on this device only" in page.inner_text("#sheet-body")
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)
            later = copy.deepcopy(d)
            later["generated_at"] = _iso(now + timedelta(hours=2))
            served.publish(later)
            _refresh(page)
            kept = json.loads(page.evaluate(f"localStorage.getItem({json.dumps(MARKS)})"))
            assert kept["3"]["shortlisted"] is True, kept
            assert "card--mine" in _card(page, "3").get_attribute("class")
            assert not errors, errors
        finally:
            ctx.close()


class TestTheSheet:

    def test_back_closes_a_car_it_opened_over(self, browser, site, payload):
        ctx, page, _, _ = _open(browser, site, payload, "#/feed", width=390)
        try:
            page.click('[data-view-link="listings"]')
            page.wait_for_selector(".card")
            page.locator(".card").first.click()
            page.wait_for_selector("#sheet[data-open='1']")
            page.go_back()
            page.wait_for_timeout(400)
            assert page.evaluate("app.view") == "feed"
            assert page.get_attribute("#sheet", "data-open") != "1"
            assert page.evaluate("document.body.style.overflow") == ""
        finally:
            ctx.close()

    def test_a_quick_reopen_is_not_hidden_by_the_close_before_it(self, browser, site, payload):
        ctx, page, _, _ = _open(browser, site, payload, "#/listings")
        try:
            _card(page, "1").click()
            page.wait_for_selector("#sheet[data-open='1']")
            state = page.evaluate("""async () => {
                closeSheet();
                await new Promise(r => setTimeout(r, 100));
                openSheet('2');
                await new Promise(r => setTimeout(r, 400));
                const s = document.getElementById('sheet');
                return [s.hidden, s.dataset.open, document.body.style.overflow];
            }""")
            assert state == [False, "1", "hidden"], state
            # Nor is a close straight after an open undone by the open's frame.
            state = page.evaluate("""async () => {
                openSheet('3'); closeSheet();
                await new Promise(r => setTimeout(r, 400));
                const s = document.getElementById('sheet');
                return [s.hidden, s.dataset.open, document.body.style.overflow];
            }""")
            assert state == [True, "0", ""], state
        finally:
            ctx.close()

    def test_a_car_the_page_does_not_carry_is_not_named_as_the_last_one(
            self, browser, site, payload):
        d = copy.deepcopy(payload)
        d["events"].append({"kind": "removed", "at": d["generated_at"],
                            "listing_id": "left-out-1", "title": "2014 Honda Civic LX"})
        ctx, page, errors, _ = _open(browser, site, d, "#/listings")
        try:
            _card(page, "1").click()
            page.wait_for_selector("#sheet[data-open='1']")
            page.keyboard.press("Escape")
            page.evaluate("openSheet('no-such-car-123')")
            assert page.text_content("#sheet-title") == "Listing not found"
            page.keyboard.press("Escape")
            page.click('[data-view-link="feed"]')
            row = page.locator(".ev", has_text="2014 Honda Civic LX")
            # It keeps the photo box, so it lines up with every other row.
            assert row.locator(".ev__shot").count() == 1
            row.click()
            page.wait_for_selector("#sheet[data-open='1']")
            assert page.text_content("#sheet-title") == "2014 Honda Civic LX"
            assert not errors, errors
        finally:
            ctx.close()

    def test_a_car_that_has_gone_says_so_at_the_top(self, browser, site, payload):
        ctx, page, _, _ = _open(browser, site, payload, "#/listing/6", width=375)
        try:
            page.wait_for_selector("#sheet[data-open='1']")
            body = page.locator("#sheet-body")
            assert body.locator(".flag--gone").text_content().startswith("Gone from the site")
            struck = body.locator(".num").first.evaluate(
                "e => getComputedStyle(e).textDecorationLine")
            assert struck == "line-through"
            assert "Was on the market" in body.text_content()
            link = body.locator("a", has_text="autotrader.ca").last
            assert "btn--primary" not in link.get_attribute("class")
            assert "may no longer load" in link.text_content()
            # All of it fits a phone.
            assert page.evaluate("document.documentElement.scrollWidth") <= 375
            assert page.eval_on_selector("#sheet", "s => s.scrollWidth <= s.clientWidth")
        finally:
            ctx.close()

    def test_a_mangled_link_does_not_stop_the_page(self, browser, site, payload):
        ctx, page, errors, _ = _open(browser, site, payload, "#/listing/x%")
        try:
            page.wait_for_selector("#sheet[data-open='1']")
            assert page.text_content("#sheet-title") == "Listing not found"
            # Everything start() wires after the first route is wired.
            assert page.evaluate("clockTimer !== null")
            assert page.text_content("#clock-last") != "—"
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)
            assert page.get_attribute("#sheet", "data-open") != "1"
            assert not errors, errors
        finally:
            ctx.close()


def test_a_sent_change_is_looked_for_until_it_lands(browser, site, payload):
    """A committed change is applied and published within minutes, while
    the next check - which is when the page would look - is hours away."""
    now = datetime.now(UTC).replace(microsecond=0)
    d = _checked_at(payload, now - timedelta(minutes=5))
    ctx, page, _, served = _open(browser, site, d, "#/searches", clock=now)
    try:
        page.clock.run_for(2 * 60_000)
        quiet = served.hits
        page.evaluate("""() => {
            window.askUrl = async () => null;
            window.open = () => ({ close() {}, location: {} });
            vault.lock = {}; app.data.repo = 'someone/watch';
            openAsk('shortlist: a car', [{action: 'shortlist', listing: '1'}], '');
            vault.lock = null;
        }""")
        page.clock.run_for(61_000)
        page.wait_for_timeout(300)
        assert served.hits > quiet, "no look after the change was sent"
        applied = copy.deepcopy(d)
        applied["generated_at"] = _iso(now + timedelta(minutes=3))
        applied["changes"] = [{"at": _iso(now + timedelta(minutes=3)), "ok": True,
                               "text": "shortlisted 2018 Honda Civic Type R"}]
        served.publish(applied)
        page.clock.run_for(61_000)
        page.wait_for_selector("text=Your recent changes")
        # Once it has landed, the page goes back to waiting for the check.
        landed = served.hits
        page.clock.run_for(5 * 60_000)
        page.wait_for_timeout(300)
        assert served.hits == landed
    finally:
        ctx.close()


class TestWhatThisDeviceKeeps:

    def test_a_kept_write_is_not_shadowed_by_the_tab_s_copy(self, browser, site, payload):
        ctx, page, _, _ = _open(browser, site, payload, "#/listings")
        try:
            got = page.evaluate("""() => {
                store.persist = false;
                marks.set('1', {shortlisted: true});
                store.set('lastSeen', '2020');
                store.persist = true;
                marks.set('3', {muted: true});
                marks.set('4', {shortlisted: true});
                store.set('lastSeen', '2030');
                return [marks.of('1').shortlisted, marks.of('3').muted,
                        marks.of('4').shortlisted, store.get('lastSeen'),
                        Object.keys(sessionStorage).filter(k => k.startsWith(NS))];
            }""")
            assert got == [True, True, True, "2030", []], got
        finally:
            ctx.close()

    def test_keeping_the_device_moves_the_tab_s_copy_onto_it(self, browser, private_site):
        ctx = browser.new_context(service_workers="block")
        page = ctx.new_page()
        try:
            page.goto(private_site["url"])
            _unlock(page, keep=False)
            page.wait_for_selector(f"text={TITLE}", timeout=30000)
            seen = page.evaluate("sessionStorage.getItem(NS + 'lastSeen')")
            assert seen
            page.reload()
            _unlock(page, keep=True)
            page.wait_for_selector(f"text={TITLE}", timeout=30000)
            assert page.evaluate(
                "Object.keys(sessionStorage).filter(k => k.startsWith(NS))") == []
            assert page.evaluate("localStorage.getItem(NS + 'lastSeen')") == seen
        finally:
            ctx.close()

    def test_an_unkept_device_leaves_no_car_in_the_address(self, browser, private_site):
        ctx = browser.new_context(service_workers="block")
        page = ctx.new_page()
        try:
            page.goto(private_site["url"])
            _unlock(page, keep=False)
            page.wait_for_selector(f"text={TITLE}", timeout=30000)
            page.goto(private_site["url"] + "#/listings")
            page.wait_for_selector(".card", timeout=30000)
            page.click(".card")
            page.wait_for_selector("#sheet[data-open='1']")
            assert "abc-1" not in page.evaluate("location.hash")
            page.keyboard.press("Escape")
            # An alert's link opens the car and leaves the address at once.
            page.goto(private_site["url"] + "#/listing/abc-1")
            page.wait_for_selector("#sheet[data-open='1']")
            assert page.text_content("#sheet-title") == TITLE
            assert "abc-1" not in page.evaluate("location.hash")
            assert page.evaluate(STORED) == []
        finally:
            ctx.close()


@pytest.fixture
def rekeyable_site(tmp_path):
    """A locked site of its own, whose passphrase this test may change."""
    from autotrader import vault as V
    from autotrader.config import Config
    from autotrader.dashboard import build_payload
    from autotrader.listing import Listing
    from autotrader.state import State

    cfg = Config.defaults(tmp_path / "config.json")
    search = cfg.add_search("https://www.autotrader.ca/cars/audi/rs%205/?prx=-2", "Audi RS 5")
    cfg.save()
    state = State(path=tmp_path / "state.json")
    state.record(Listing(id="rk-1", url="https://www.autotrader.ca/offers/rk-1",
                         title=TITLE, price=61000, price_source="detail",
                         search_id=search.id, year=2018))
    state.record_run({"ok": True, "searches": 1, "listings": 1})
    docs = tmp_path / "docs"
    shutil.copytree(DOCS, docs, ignore=shutil.ignore_patterns(
        "data.json", "events.json", "thumbs", "data.enc", "lock.json"))
    (docs / "data.json").write_text(
        json.dumps(build_payload(cfg, state, {"GITHUB_REPOSITORY": "someone/watch"})),
        encoding="utf-8")
    phrase = "a first long test passphrase"
    vault = V.Vault.unlock(tmp_path, {V.ENV_KEY: phrase}, create=True)
    out = tmp_path / "site"
    vault.publish(docs, out)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(out)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield {"url": f"http://127.0.0.1:{server.server_port}/index.html",
               "phrase": phrase, "vault": vault, "docs": docs, "out": out}
    finally:
        server.shutdown()


def test_a_new_passphrase_asks_for_itself_on_an_open_tab(browser, rekeyable_site):
    """The bot re-seals everything under the new passphrase. A tab already
    open, or an installed app kept in memory, must ask for it rather than
    fail every minute until its pill says no check has landed."""
    s = rekeyable_site
    ctx = browser.new_context(service_workers="block")
    page = ctx.new_page()
    try:
        page.goto(s["url"])
        _unlock(page, s["phrase"], keep=True)
        page.wait_for_selector(f"text={TITLE}", timeout=30000)
        page.evaluate("marks.set('rk-1', {note: 'ask about the brakes'})")
        s["vault"].rekey("a second long test passphrase")
        s["vault"].publish(s["docs"], s["out"])
        # The test server revalidates by a Last-Modified to the second, and
        # this all happens inside one: date the new files later.
        later = time.time() + 5
        for path in s["out"].rglob("*"):
            os.utime(path, (later, later))
        page.evaluate("lastFetchAt = 0; refreshData()")
        page.wait_for_selector("#lock:not([hidden])", timeout=30000)
        stored = page.evaluate(STORED)
        # The old key goes; the marks on this device stay.
        assert "atw:/:vaultKey" not in stored, stored
        assert MARKS in stored, stored
    finally:
        ctx.close()
