"""The page's look, its words for a screen reader, and what it costs a phone.

Every test here is a place where the page drew one thing and meant another,
or did the same work twice: a passphrase box 200px tall, a red "No" painted
black, a chart whose rows each had their own scale, a caption reading "—h",
a tab switch that drew the view twice, a refresh that downloaded the same
file every minute. Each was found by using the page, not by reading it.

The page, the fixed data and the browser are the layout tests' own; the
locked site is the private-site tests' own.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import threading
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from .test_dashboard_layout import DOCS, GENERATED, PHOTO  # noqa: F401
from .test_dashboard_layout import _browser_path, browser, payload, site  # noqa: F401 - fixtures
from .test_dashboard_layout import TestTheCollectorOnThePage as _Collector
from .test_private_site import PHRASE, private_site  # noqa: F401 - fixture

UTC = timezone.utc


def _open(browser, site, data, route="#/feed", *, width=1440, height=900, init=None,
          theme="light"):
    ctx = browser.new_context(viewport={"width": width, "height": height},
                              color_scheme=theme, service_workers="block")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    if init:
        page.add_init_script(init)
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


def _many(payload, n=60) -> dict:
    """The demo data with n live cars, enough to scroll a phone."""
    d = copy.deepcopy(payload)
    model = next(l for l in d["listings"] if l["id"] == "3")
    for i in range(n):
        car = copy.deepcopy(model)
        car.update(id=f"9{i:03d}", title=f"2019 Honda Civic Sport hatchback {i}",
                   price=30000 + i * 100, you={})
        car.pop("thumb", None)
        car.pop("thumbs", None)
        d["listings"].append(car)
    return d


# ================================================================== the work

class TestADrawingDoesItsWorkOnce:

    def test_marks_are_read_once_and_no_car_is_searched_for(self, browser, site, payload):
        """Each car's marks were asked after several times a drawing, and
        each time the stored text was parsed again and the list searched
        from the top: 500 cars took most of a second on a phone."""
        d = _many(payload, 80)
        init = ("localStorage.setItem('atw:/:marks', JSON.stringify("
                "{'9001': {shortlisted: true, at: new Date().toISOString()},"
                " '9002': {note: 'ask about tyres', at: new Date().toISOString()}}));")
        ctx, page, errors = _open(browser, site, d, "#/listings", init=init)
        try:
            work = page.evaluate("""() => {
                let parsed = 0, searched = 0;
                const parse = JSON.parse;
                JSON.parse = (...a) => { parsed++; return parse(...a); };
                const rows = app.data.listings, find = rows.find;
                rows.find = function (...a) { searched++; return find.apply(this, a); };
                try { renderListings(); } finally { JSON.parse = parse; rows.find = find; }
                return { parsed, searched };
            }""")
            assert work["parsed"] <= 1, work
            assert work["searched"] == 0, work
            assert page.locator(".card--mine").count() >= 1
            assert not errors, errors
        finally:
            ctx.close()

    def test_a_tab_is_drawn_once_per_press(self, browser, site, payload):
        """go() drew the view and set the address; the address change drew
        it again."""
        ctx, page, errors = _open(browser, site, payload, "#/feed")
        try:
            page.evaluate("""() => { window.__drawn = {};
                for (const name of ['renderListings', 'renderStatus', 'renderTabs']) {
                  const draw = window[name];
                  window[name] = (...a) => {
                    window.__drawn[name] = (window.__drawn[name] || 0) + 1;
                    return draw(...a); };
                } }""")
            page.click('[data-view-link="listings"]')
            page.wait_for_timeout(300)
            assert page.evaluate("window.__drawn") == {"renderListings": 1, "renderTabs": 1}
            assert page.evaluate("location.hash") == "#/listings"
            page.evaluate("window.__drawn = {}")
            page.keyboard.press("5")
            page.wait_for_timeout(300)
            assert page.evaluate("window.__drawn") == {"renderStatus": 1, "renderTabs": 1}
            assert not errors, errors
        finally:
            ctx.close()

    def test_back_and_forward_still_change_the_view(self, browser, site, payload):
        ctx, page, _ = _open(browser, site, payload, "#/feed")
        try:
            page.click('[data-view-link="listings"]')
            page.wait_for_timeout(200)
            page.click('[data-view-link="status"]')
            page.wait_for_timeout(200)
            page.go_back()
            page.wait_for_timeout(300)
            assert page.evaluate("app.view") == "listings"
            assert page.is_visible('[data-view="listings"] .card')
            page.go_forward()
            page.wait_for_timeout(300)
            assert page.evaluate("app.view") == "status"
        finally:
            ctx.close()

    def test_slash_still_puts_the_keyboard_in_the_filter_box(self, browser, site, payload):
        ctx, page, _ = _open(browser, site, payload, "#/feed")
        try:
            page.keyboard.press("/")
            page.wait_for_timeout(300)
            assert page.evaluate("document.activeElement.id") == "q"
        finally:
            ctx.close()


class _Recording(SimpleHTTPRequestHandler):
    """The site as GitHub Pages serves it, noting what each request asked."""
    asked: list[tuple[str, str]] = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).asked.append((self.path, self.headers.get("If-Modified-Since") or ""))
        super().do_GET()


@pytest.fixture(scope="module")
def recording_site(tmp_path_factory, payload):
    root = tmp_path_factory.mktemp("recorded") / "docs"
    shutil.copytree(DOCS, root, ignore=shutil.ignore_patterns(*GENERATED))
    (root / "data.json").write_text(json.dumps(payload), encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Recording, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/index.html"
    finally:
        server.shutdown()


class TestARefreshCostsLittleWhenNothingChanged:

    def test_the_page_asks_whether_the_file_changed(self, browser, recording_site):
        """cache: 'no-store' sent no If-Modified-Since, so a page left open on
        an overdue check downloaded the whole file every minute."""
        ctx = browser.new_context(service_workers="block")
        page = ctx.new_page()
        try:
            page.goto(recording_site, wait_until="networkidle")
            page.wait_for_timeout(200)
            _Recording.asked = []
            page.evaluate("async () => { lastFetchAt = 0; await refreshData(); }")
            data = [since for path, since in _Recording.asked if path.startswith("/data.json")]
            assert data and all(data), _Recording.asked
        finally:
            ctx.close()


# ================================================================== the lock

class TestTheLockScreen:

    def open(self, browser, private_site, width=375):
        ctx = browser.new_context(viewport={"width": width, "height": 800},
                                  service_workers="block")
        page = ctx.new_page()
        page.goto(private_site["url"])
        page.wait_for_selector("#lock-pass")
        return ctx, page

    @pytest.mark.parametrize("width", (375, 1440))
    def test_the_passphrase_box_is_the_height_of_one_line(self, browser, private_site, width):
        ctx, page = self.open(browser, private_site, width)
        try:
            height = page.evaluate("document.querySelector('#lock .field')"
                                   ".getBoundingClientRect().height")
            assert height < 48, height
        finally:
            ctx.close()

    def test_a_wrong_passphrase_is_said_in_red(self, browser, private_site):
        ctx, page = self.open(browser, private_site)
        try:
            page.fill("#lock-pass", "not the passphrase at all")
            page.click("#lock-go")
            page.wait_for_selector("#lock-msg:not(:empty)")
            colours = page.evaluate("""() => {
                const probe = document.createElement('span');
                probe.style.color = 'var(--bad)';
                document.body.appendChild(probe);
                const bad = getComputedStyle(probe).color;
                probe.remove();
                return [getComputedStyle(document.getElementById('lock-msg')).color, bad];
            }""")
            assert colours[0] == colours[1], colours
        finally:
            ctx.close()

    def test_the_keyboard_lands_in_the_page_once_unlocked(self, browser, private_site):
        ctx, page = self.open(browser, private_site)
        try:
            page.fill("#lock-pass", PHRASE)
            page.uncheck("#lock-keep")
            page.click("#lock-go")
            page.wait_for_selector("#lock", state="hidden")
            page.wait_for_timeout(300)
            assert page.evaluate("document.activeElement.id") == "main"
        finally:
            ctx.close()


# ================================================================== a phone

class TestNothingPushesAPhoneSideways:

    def test_a_long_error_on_a_search(self, browser, site, payload):
        d = copy.deepcopy(payload)
        d["searches"][0]["health"] = dict(
            d["searches"][0].get("health") or {}, consecutive_failures=2,
            last_error="HTTP 503 from https://www.autotrader.ca/cars/honda/civic/reg_on/"
                       "cit_toronto?modelyearfrom=2014&modelyearto=2021&zip=Toronto&zipr=500"
                       "&srt=35&rcp=100&rcs=0&prx=500&loc=Toronto")
        for width in (375, 390):
            ctx, page, _ = _open(browser, site, d, "#/searches", width=width)
            try:
                assert page.is_visible("dd.err")
                over = page.evaluate("document.documentElement.scrollWidth - innerWidth")
                assert over <= 0, f"{width}px: {over}px sideways"
            finally:
                ctx.close()

    def test_a_long_search_name(self, browser, site, payload):
        """Names set from the command line may be 80 characters, and a
        picker is as wide as its longest option."""
        d = copy.deepcopy(payload)
        name = "Honda Civic Type R 2017-2021 manual under $45,000 within 500 km of Toronto ON"
        name = (name + " hatchback")[:80]
        assert len(name) == 80
        d["searches"][0]["name"] = name
        for car in d["listings"]:
            car["search_name"] = name
        for width in (375, 390):
            ctx, page, _ = _open(browser, site, d, "#/listings", width=width)
            try:
                over = page.evaluate("document.documentElement.scrollWidth - innerWidth")
                assert over <= 0, f"{width}px: {over}px sideways"
                box = page.evaluate("document.getElementById('search-pick')"
                                    ".getBoundingClientRect().right")
                assert box <= width, box
                # And wherever else the name is printed.
                for view in ("feed", "searches", "status"):
                    page.click(f'[data-view-link="{view}"]')
                    page.wait_for_timeout(150)
                    over = page.evaluate("document.documentElement.scrollWidth - innerWidth")
                    assert over <= 0, f"{view} at {width}px: {over}px sideways"
            finally:
                ctx.close()


class TestFocusIsNeverUnderABar:

    def test_tabbing_through_the_cards_on_a_phone(self, browser, site, payload):
        """Nothing set scroll-padding, so a focused card was scrolled only as
        far as the window's edge: under the tab bar going down, under the
        header coming back."""
        ctx, page, _ = _open(browser, site, _many(payload, 40), "#/listings",
                             width=375, height=700)
        try:
            page.focus('[data-view="listings"] .card')
            hidden = []
            for key in ["Tab"] * 18 + ["Shift+Tab"] * 18:
                page.keyboard.press(key)
                page.wait_for_timeout(30)
                where = page.evaluate("""() => {
                    const a = document.activeElement;
                    if (!a.classList.contains('card')) return null;
                    const r = a.getBoundingClientRect();
                    return { top: r.top, bottom: r.bottom,
                      header: document.querySelector('.topbar').getBoundingClientRect().bottom,
                      bar: document.querySelector('.tabs').getBoundingClientRect().top };
                }""")
                if where and (where["top"] < where["header"] or where["bottom"] > where["bar"]):
                    hidden.append((key, where))
            assert not hidden, hidden[:3]
        finally:
            ctx.close()


@pytest.mark.parametrize("width", (834, 1440))
def test_the_clock_strip_lines_up_with_the_page(browser, site, payload, width):
    ctx, page, _ = _open(browser, site, payload, "#/feed", width=width)
    try:
        lefts = page.evaluate("""() => {
            const inner = el => { const r = el.getBoundingClientRect();
              return r.left + parseFloat(getComputedStyle(el).paddingLeft); };
            return { clock: inner(document.querySelector('.clock__in')),
                     main: inner(document.getElementById('main')),
                     brand: document.querySelector('.brand').getBoundingClientRect().left };
        }""")
        assert abs(lefts["clock"] - lefts["main"]) <= 1, lefts
        assert abs(lefts["clock"] - lefts["brand"]) <= 1, lefts
    finally:
        ctx.close()


# ================================================================== colours

def _colour_of(page, var):
    return page.evaluate(f"""() => {{
        const probe = document.createElement('span');
        probe.style.color = 'var({var})';
        document.body.appendChild(probe);
        const c = getComputedStyle(probe).color;
        probe.remove();
        return c; }}""")


def _figures(page):
    """Each Status tile's figure colour and tone, by its term."""
    return page.evaluate("""() => Object.fromEntries(
        [...document.querySelectorAll('[data-view="status"] .stat')].map(s => [
          s.querySelector('dt').textContent.trim(),
          { tone: s.dataset.tone || '',
            figure: getComputedStyle(s.querySelector('dd:not(.stat__note)')).color,
            note: s.querySelector('.stat__note')
              ? getComputedStyle(s.querySelector('.stat__note')).color : null }]))""")


class TestATileSaysItsToneInColour:

    def test_a_stopped_bot_s_no_is_red(self, browser, site, payload):
        d = copy.deepcopy(payload)
        d["budget"] = dict(d.get("budget") or {}, can_still_run=False, state="stop",
                           exempt_minutes=0, drawing_minutes=2600, used=2600,
                           allowance=3000, days_to_reset=18, text="It has stopped checking.")
        ctx, page, _ = _open(browser, site, d, "#/status")
        try:
            tiles = _figures(page)
            assert tiles["Can it still run"]["tone"] == "bad"
            assert tiles["Can it still run"]["figure"] == _colour_of(page, "--bad")
            assert tiles["Can it still run"]["note"] != _colour_of(page, "--bad")
        finally:
            ctx.close()

    def test_a_signed_out_collector_is_red_and_a_late_one_amber(self, browser, site, payload):
        d = _Collector().payload_with(payload, minutes_ago=70, next_in=-45,
                                      session="signed_out")
        ctx, page, _ = _open(browser, site, d, "#/status")
        try:
            tiles = _figures(page)
            assert tiles["Facebook"]["figure"] == _colour_of(page, "--bad")
            assert tiles["Next batch"]["figure"] == _colour_of(page, "--warn")
            # Every toned tile paints its figure, never its caption.
            for term, t in tiles.items():
                if t["tone"] in ("bad", "warn") and t["note"]:
                    assert t["figure"] != t["note"], term
                    assert t["figure"] != _colour_of(page, "--text"), term
        finally:
            ctx.close()

    def test_no_delivery_record_is_a_fault_not_good_news(self, browser, site, payload):
        ctx, page, _ = _open(browser, site, payload, "#/feed")
        try:
            html = page.evaluate("""() => eventRow({ kind: 'new', at: new Date().toISOString(),
                listing_id: '1', title: '2018 Honda Civic Type R', price: 42000,
                delivery: { state: 'none' } }).innerHTML""")
            assert '<span class="err">no delivery record</span>' in html
        finally:
            ctx.close()

    def test_a_car_at_the_median_is_not_called_under_it(self, browser, site, payload):
        """0.3% under rounds to "0% under the median", in the good-deal green."""
        ctx, page, _ = _open(browser, site, payload, "#/feed")
        try:
            says = page.evaluate("comparableSays({pct: -0.3, sample: 8, median: 30000,"
                                 " cohort: '2018 Honda Civic'})")
            assert says["tone"] == ""
            assert "0%" not in says["badge"] and "0%" not in says["sentence"]
            assert says["badge"] == "at the median of 8"
            assert says["sentence"].startswith("At the median $30,000 of 8 comparables")
            under = page.evaluate("comparableSays({pct: -6.4, sample: 8, median: 30000})")
            assert under["tone"] == "drop" and under["badge"] == "6% under the median of 8"
        finally:
            ctx.close()

    def test_why_there_is_no_comparison_starts_with_a_capital(self, browser, site, payload):
        ctx, page, _ = _open(browser, site, payload, "#/feed")
        try:
            for cmp in ("{rank: 3, of: 9, why_not: 'it has left the market - the last price'}",
                        "{why_not: 'its make and model did not parse'}"):
                sentence = page.evaluate(f"comparableSays({cmp}).sentence")
                assert sentence[0].isupper() and sentence.endswith("."), sentence
        finally:
            ctx.close()


# ================================================================== the chart

def test_every_year_on_the_chart_shares_one_scale(browser, site, payload):
    """Each row was its own grid, so its track ended where that row's
    "$X n=Y" began: the same price sat at different places on different
    rows, and the axis ends sat under the year and the price columns."""
    for width in (375, 1440):
        ctx, page, _ = _open(browser, site, payload, "#/market", width=width)
        try:
            got = page.evaluate("""() => {
                const years = [
                  ['2018', {low: 9000, high: 60000, q1: 20000, q3: 40000, median: 30000, n: 5}],
                  ['2019', {low: 30000, high: 109868, q1: 50000, q3: 80000, median: 64000, n: 1234}],
                  ['2020', {low: 25000, high: 90000, q1: 40000, q3: 70000, median: 55000, n: 40}]];
                const chart = rangeChart(years, {min: 9000, max: 109868});
                chart.id = 'probe-chart';
                document.querySelector('[data-view="market"]').appendChild(chart);
                const rows = [...chart.querySelectorAll('[role="img"]')];
                const tracks = rows.map(r => r.children[1].getBoundingClientRect());
                const ends = [...chart.lastElementChild.children].map(e => e.getBoundingClientRect());
                return { tracks: tracks.map(t => [t.left, t.right]),
                         ends: ends.map(e => [e.left, e.right]),
                         labels: rows.map(r => r.getAttribute('aria-label')),
                         over: document.documentElement.scrollWidth - innerWidth };
            }""")
            lefts = {round(t[0]) for t in got["tracks"]}
            rights = [t[1] for t in got["tracks"]]
            assert len(got["tracks"]) == 3
            assert max(lefts) - min(lefts) <= 1, got
            assert max(rights) - min(rights) <= 1, got
            track = (got["tracks"][0][0] - 1, got["tracks"][0][1] + 1)
            for left, right in got["ends"]:
                assert track[0] <= left and right <= track[1], got
            assert abs(got["ends"][0][0] - got["tracks"][0][0]) <= 1, got
            assert abs(got["ends"][-1][1] - got["tracks"][0][1]) <= 1, got
            assert got["labels"][1].startswith("2019: 1234 cars"), got
            assert got["over"] <= 0, got
        finally:
            ctx.close()


# ================================================================== a screen reader

class TestACardSaysWhatItShows:

    def label(self, page, car_id):
        return page.evaluate(f"card(byId('{car_id}')).getAttribute('aria-label')")

    def test_a_gone_card_says_it_has_gone(self, browser, site, payload):
        ctx, page, _ = _open(browser, site, payload, "#/listings")
        try:
            label = self.label(page, "6")
            assert label.startswith("2017 Honda Civic sold on, $22,000"), label
            assert "gone" in label.lower(), label
            assert "Toronto" in label and "listed today" in label, label
        finally:
            ctx.close()

    def test_a_price_drop_says_from_what_and_by_how_much(self, browser, site, payload):
        ctx, page, _ = _open(browser, site, payload, "#/listings")
        try:
            label = self.label(page, "1")
            assert label.startswith("2018 Honda Civic Type R, $42,000"), label
            assert "price drop" in label.lower(), label
            assert "was $44,000" in label and "down $2,000" in label, label
            assert "48,000 kilometres" in label, label
        finally:
            ctx.close()

    def test_the_comparison_on_the_card_is_in_its_name(self, browser, site, payload):
        d = copy.deepcopy(payload)
        d.setdefault("comparables", {})["2"] = {"pct": -12.2, "sample": 9, "median": 29600}
        ctx, page, _ = _open(browser, site, d, "#/listings")
        try:
            assert "12% under the median of 9" in self.label(page, "2")
        finally:
            ctx.close()


# ================================================================== the words

class TestTheStatusTilesReadAsWords:

    def test_no_unit_hangs_off_a_dash_and_one_is_not_plural(self, browser, site, payload):
        d = copy.deepcopy(payload)
        d["cost"] = {"checks": 1, "minutes": 1, "billed_minutes": 1, "window_hours": 24}
        d["coverage"] = dict(d.get("coverage") or {}, longest_gap_minutes=0,
                             too_short=True, new_install=True, successful=1,
                             window_hours=0.001)
        d["last_check"] = dict(d.get("last_check") or d["last_run"], duration_s=None)
        d["budget"] = dict(d.get("budget") or {}, exempt_minutes=0, can_still_run=True,
                           why="nothing has told this bot whether it is charged")
        ctx, page, errors = _open(browser, site, d, "#/status")
        try:
            text = _said(page.inner_text('[data-view="status"] .stats'))
            for wrong in ("—h", "—s", " 1 checks", " 1 billed minutes", "— from here"):
                assert wrong not in text, f"{wrong!r} in {text!r}"
            # "0 minutes spent" is a count; "in its first 0 minutes" is not a time.
            assert not re.search(r"\b1 minutes|first 0 minutes|coverage, 0 minutes", text, re.I), text
            assert "1 check · 1 billed minute in 24h" in text, text
            assert "none labelled yet — nothing has told this bot" in text, text
            assert "a new watch: its first check has just run" in text, text
            assert not errors, errors
        finally:
            ctx.close()

    def test_a_longer_watch_still_says_its_figures(self, browser, site, payload):
        d = copy.deepcopy(payload)
        d["cost"] = {"checks": 3, "minutes": 3, "billed_minutes": 5, "window_hours": 24}
        d["coverage"] = dict(d.get("coverage") or {}, longest_gap_minutes=95)
        d["last_check"] = dict(d.get("last_check") or d["last_run"], duration_s=41.6)
        ctx, page, _ = _open(browser, site, d, "#/status")
        try:
            text = _said(page.inner_text('[data-view="status"] .stats'))
            assert "LONGEST GAP 1.6h" in text, text
            assert "42s" in text and "3 checks · 5 billed minutes in 24h" in text, text
        finally:
            ctx.close()


class TestTwoCountsThatAgree:

    def test_the_rules_preview_counts_a_hidden_car_once(self, browser, site, payload):
        """"4 of the 5 cars ... would pass. 1 car the bot currently hides is
        not counted here" - but it was one of the 5."""
        ctx, page, _ = _open(browser, site, payload, "#/searches")
        try:
            said = _said(page.inner_text('[id^="pv-"]'))
            assert said.startswith("4 of the 5 cars this search currently holds would pass"), said
            assert "not counted" not in said, said
            assert "1 of them is hidden by another rule" in said, said
        finally:
            ctx.close()

    def test_the_market_heading_counts_what_the_models_count(self, browser, site, payload):
        """The heading said the models count "the 4 you could actually buy";
        they counted the 3 of those with an asking price."""
        ctx, page, _ = _open(browser, site, payload, "#/market")
        try:
            head = _said(page.inner_text("#market-h + p"))
            models = page.evaluate("""() => [...document.querySelectorAll(
                '[data-view="market"] .section__head .count')]
                .map(c => parseInt(c.textContent, 10)).filter(n => !isNaN(n))""")
            priced = sum(models)
            assert priced == 3, models
            assert f"count only the {priced} of the 4 you could actually buy that " \
                   f"carry an asking price" in head, head
        finally:
            ctx.close()


class TestTheStripAndThePillAgree:

    def test_a_failed_check_beside_an_older_good_one(self, browser, site, payload):
        now = datetime.now(UTC)
        d = copy.deepcopy(payload)
        good = dict(d["last_run"], at=_iso(now - timedelta(minutes=5)), ok=True,
                    searches_run=1, searches_failed=0, errors=[])
        failed = {"at": _iso(now - timedelta(minutes=3)), "ok": False, "searches_run": 1,
                  "searches_failed": 1, "errors": ["Honda Civic 2014-2021: timed out"]}
        d["runs"] = [failed, good]
        d["last_run"], d["last_check"] = failed, good
        ctx, page, _ = _open(browser, site, d)
        try:
            assert page.text_content("#trust-text").startswith("Last check failed 3m ago")
            term = page.evaluate("document.getElementById('clock-last')"
                                 ".previousElementSibling.textContent")
            assert term == "Last good check"
            assert page.text_content("#clock-last") == "5m ago"
        finally:
            ctx.close()

    def test_a_partly_failed_check_is_still_the_last_check(self, browser, site, payload):
        now = datetime.now(UTC)
        d = copy.deepcopy(payload)
        failed = {"at": _iso(now - timedelta(minutes=3)), "ok": False, "searches_run": 2,
                  "searches_failed": 1, "errors": ["Honda Civic 2014-2021: timed out"]}
        d["runs"] = [failed]
        d["last_run"] = d["last_check"] = failed
        ctx, page, _ = _open(browser, site, d)
        try:
            assert page.text_content("#trust-text").startswith("Last check failed")
            term = page.evaluate("document.getElementById('clock-last')"
                                 ".previousElementSibling.textContent")
            assert term == "Last check"
        finally:
            ctx.close()


# ================================================================== alerts

class TestARetiredChannelCanBeSwitchedBackOn:

    def retired(self, payload, missing=()):
        d = copy.deepcopy(payload)
        d["channels"]["email"] = dict(d["channels"].get("email") or {},
                                      label="Email (Gmail)", active=False, setting=False,
                                      missing=list(missing),
                                      disabled_reason="Switched off automatically after 2 "
                                                      "runs: the credentials were rejected")
        return d

    def test_it_offers_to_switch_it_back_on(self, browser, site, payload):
        ctx, page, errors = _open(browser, site, self.retired(payload), "#/status")
        try:
            page.evaluate("""() => { window.__asked = []; const offer = askButton;
                askButton = (label, title, changes, prose) => {
                  window.__asked.push(changes); return offer(label, title, changes, prose); };
                renderStatus(); }""")
            button = page.locator("button, a", has_text="Switch Email (Gmail) back on")
            assert button.count() == 1
            assert [{"action": "set-channel", "channel": "email", "enabled": True}] \
                in page.evaluate("window.__asked")
            row = page.locator("tr", has_text="Email (Gmail)")
            assert _said(row.inner_text()).endswith("the credentials were rejected.")
            assert not errors, errors
        finally:
            ctx.close()

    def test_not_while_its_secret_is_missing(self, browser, site, payload):
        ctx, page, _ = _open(browser, site, self.retired(payload, ["GMAIL_USER"]), "#/status")
        try:
            assert page.locator("button, a", has_text="back on").count() == 0
        finally:
            ctx.close()


# ================================================================== the worker

@pytest.fixture
def few_photos_site(tmp_path, payload):
    """The page and its real worker, kept to three photos rather than 800."""
    root = tmp_path / "docs"
    shutil.copytree(DOCS, root, ignore=shutil.ignore_patterns(*GENERATED))
    worker = (root / "sw.js").read_text(encoding="utf-8")
    assert "const PHOTO_LIMIT = 800;" in worker
    (root / "sw.js").write_text(worker.replace("const PHOTO_LIMIT = 800;",
                                               "const PHOTO_LIMIT = 3;"), encoding="utf-8")
    d = copy.deepcopy(payload)
    for car in d["listings"]:
        car.pop("thumb", None)
        car.pop("thumbs", None)
    (root / "data.json").write_text(json.dumps(d), encoding="utf-8")
    (root / "thumbs").mkdir()
    for n in range(1, 6):
        (root / "thumbs" / f"p{n}.bin").write_bytes(bytes([n]) * 1024)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Recording, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/index.html"
    finally:
        server.shutdown()


def test_the_worker_keeps_only_the_newest_photos(browser, few_photos_site):
    """Every photo ever shown stayed on the phone, 64 KB and more apiece."""
    ctx = browser.new_context()
    page = ctx.new_page()
    try:
        page.goto(few_photos_site, wait_until="networkidle")
        kept = page.evaluate("""async () => {
            await navigator.serviceWorker.ready;
            for (let i = 0; i < 100 && !navigator.serviceWorker.controller; i++)
              await new Promise(r => setTimeout(r, 50));
            if (!navigator.serviceWorker.controller) return 'no worker';
            for (const n of [1, 2, 3, 4, 5]) await (await fetch(`thumbs/p${n}.bin`)).arrayBuffer();
            const cache = await caches.open('atw-photos');
            for (let i = 0; i < 40 && (await cache.keys()).length > 3; i++)
              await new Promise(r => setTimeout(r, 50));
            const again = await fetch('thumbs/p1.bin');
            return { kept: (await cache.keys()).map(r => new URL(r.url).pathname),
                     again: again.status };
        }""")
        assert kept != "no worker"
        assert kept["kept"] == ["/thumbs/p3.bin", "/thumbs/p4.bin", "/thumbs/p5.bin"], kept
        # One let go is fetched again when it is shown again.
        assert kept["again"] == 200
    finally:
        ctx.close()
