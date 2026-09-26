"""The published page, opened in a real browser and measured.

Everything here is a property that has actually been broken by a change to
this page during its build, and that no unit test would have caught:

* a selector list with an @media block inside it silently killed the rule
  above it, and the unread badge went dark-on-blue;
* the phone card rules were written before the card itself, so the layout
  they described never applied;
* the tertiary grey used for every timestamp, count and unit sat at 3.2:1;
* the bottom tab bar was drawn after the fetch, so it grew from nothing on
  a phone and shoved the page.

These are checked by rendering, not by reading the CSS, because in every one
of those cases the CSS said the right thing and the browser did something
else. Skipped when playwright or the bundled browser is missing, so the suite
still runs anywhere.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parent.parent / "docs"
PHOTO = Path(__file__).resolve().parent / "fixtures" / "photo.webp"
# What the bot writes into docs/ on a check. The page is tested as published;
# the data it draws is built here, so none of this is copied from a checkout.
GENERATED = ("data.json", "events.json", "thumbs", "data.enc", "lock.json")
CHROME = Path("/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
WIDTHS = (390, 834, 1440)
THEMES = ("light", "dark")

playwright = pytest.importorskip("playwright.sync_api",
                                 reason="playwright is not installed")


def _browser_path() -> str | None:
    if CHROME.exists():
        return str(CHROME)
    found = sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome"))
    return str(found[0]) if found else None


def _demo_payload(root: Path) -> dict:
    """A payload built here, rather than whatever the bot holds this morning.

    These tests used to render the repository's live data.json. That made them
    a measurement of the market: they passed while the watched searches held
    cars and every test that clicks a card failed the morning those searches
    were swapped for different ones. The page under test is still the real
    published page - only the data it draws is fixed.
    """
    from autotrader.config import Config
    from autotrader.dashboard import build_payload
    from autotrader.listing import Listing
    from autotrader.state import State

    cfg = Config.defaults(root / "config.json")
    search = cfg.add_search(
        "https://www.autotrader.ca/cars/honda/civic/reg_on/cit_toronto"
        "?modelyearfrom=2014&modelyearto=2021&zip=Toronto&zipr=500",
        "Honda Civic 2014-2021")
    cfg.save()

    state = State(path=root / "state.json")

    def car(lid, title, price, **kw):
        return Listing(id=lid, url=f"https://www.autotrader.ca/a/honda/civic/toronto/"
                                   f"ontario/19_{lid}_/",
                       title=title, price=price, price_source="detail",
                       search_id=search.id, location="Toronto", province="ON", **kw)

    state.record(car("1", "2018 Honda Civic Type R", 44000, year=2018,
                     mileage_km=48000, seller="A Dealer"))
    state.record(car("2", "2016 Honda Civic Si 6-speed manual", 26000, year=2016,
                     mileage_km=71200, seller="A Dealer"))
    state.record(car("3", "2020 Honda Civic Type R Limited Edition", 60000, year=2020,
                     mileage_km=12000))
    state.record(car("4", "2015 Honda Civic no price yet", None, year=2015))
    # One the rules hide, one that left the market: both are drawn differently
    # and both have had their own layout faults.
    state.record(car("5", "2019 Honda Civic in Alberta", 28000, year=2019))
    state.listings["5"].update(filtered=True,
                               filter_reason="Calgary, AB is 2,700 km from "
                                             "Toronto, ON, beyond the 500 km "
                                             "you asked for")
    state.record(car("6", "2017 Honda Civic sold on", 22000, year=2017))
    state.listings["6"].update(status="gone", notified=True)
    state.record(car("1", "2018 Honda Civic Type R", 42000, year=2018,
                     mileage_km=48000, seller="A Dealer"))
    state.listings["2"]["mark"] = "shortlist"
    # Real image files, served beside the page, so the browser tests render
    # actual photos. Every one of these tests was written after a day in which
    # no photo loaded at all and the page looked completely normal.
    for lid in ("1", "2", "3"):
        state.listings[lid]["_thumb"] = f"thumbs/demo-{lid}.webp"
    state.record_run({"ok": True, "searches": 1, "listings": 6})
    state.record_search_ok(search.id, 5, "jsonld")
    state.save()
    payload = build_payload(cfg, state, {})
    by_id = {l["id"]: l for l in payload["listings"]}
    for lid in ("1", "2", "3"):
        shot = state.listings[lid].get("_thumb")
        if shot and lid in by_id:
            by_id[lid]["thumb"] = shot
            by_id[lid]["thumbs"] = [shot]
            by_id[lid]["photo_count"] = 12
    return payload


@pytest.fixture(scope="module")
def payload(tmp_path_factory):
    root = tmp_path_factory.mktemp("bot")
    # build_payload reads the photo index and the archive from the working
    # directory; this one has neither, which is what a demo should read.
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(root)
        return _demo_payload(root)


@pytest.fixture(scope="module")
def site(tmp_path_factory, payload):
    """Serve a copy of docs/ - real page, fixed data - on a loopback port."""
    root = tmp_path_factory.mktemp("site") / "docs"
    shutil.copytree(DOCS, root, ignore=shutil.ignore_patterns(*GENERATED))
    (root / "data.json").write_text(json.dumps(payload), encoding="utf-8")
    (root / "thumbs").mkdir()
    for lid in ("1", "2", "3"):
        shutil.copy(PHOTO, root / "thumbs" / f"demo-{lid}.webp")
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/index.html"
    finally:
        server.shutdown()


@pytest.fixture(scope="module")
def browser():
    path = _browser_path()
    if not path:
        pytest.skip("no bundled chromium to render with")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=path)
        try:
            yield b
        finally:
            b.close()


CONTRAST_JS = r"""
() => {
  const lum = c => { const f = v => { v/=255; return v<=0.03928? v/12.92 : Math.pow((v+0.055)/1.055,2.4); };
    return 0.2126*f(c[0])+0.7152*f(c[1])+0.0722*f(c[2]); };
  const parse = s => { const m = s.match(/rgba?\(([^)]+)\)/); if(!m) return null;
    return m[1].split(',').map(Number).slice(0,3); };
  const bgOf = el => { let n = el; while (n) { const c = getComputedStyle(n).backgroundColor;
      if (parse(c) && !/rgba\(0, 0, 0, 0\)/.test(c)) return parse(c); n = n.parentElement; }
    return [255,255,255]; };
  const bad = [];
  for (const el of document.querySelectorAll('body *')) {
    if (!el.firstChild || el.firstChild.nodeType !== 3) continue;
    const text = el.textContent.trim();
    if (!text || text.length > 80) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || !el.offsetParent) continue;
    const fg = parse(cs.color); if (!fg) continue;
    const L1 = lum(fg), L2 = lum(bgOf(el));
    const ratio = (Math.max(L1,L2)+0.05)/(Math.min(L1,L2)+0.05);
    const size = parseFloat(cs.fontSize), weight = parseInt(cs.fontWeight)||400;
    const need = (size >= 24 || (size >= 18.66 && weight >= 700)) ? 3 : 4.5;
    if (ratio < need - 0.01) bad.push({text: text.slice(0,40), ratio: Math.round(ratio*100)/100,
                                       need, cls: String(el.className).slice(0,40)});
  }
  return bad;
}
"""


def _page(browser, site, width, theme, view=None):
    ctx = browser.new_context(viewport={"width": width, "height": 900},
                              color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text)
            if m.type == "error" and "ERR_TUNNEL" not in m.text
            and "Failed to load resource" not in m.text else None)
    page.goto(site, wait_until="networkidle")
    page.wait_for_timeout(250)
    if view:
        page.click(f'[data-view-link="{view}"]')
        page.wait_for_timeout(250)
    return ctx, page, errors


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("theme", THEMES)
def test_nothing_scrolls_sideways(browser, site, width, theme):
    """A page that scrolls horizontally on a phone is a broken page."""
    ctx, page, _ = _page(browser, site, width, theme)
    try:
        for view in ("feed", "listings", "searches", "status"):
            page.click(f'[data-view-link="{view}"]')
            page.wait_for_timeout(200)
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            assert overflow <= 0, f"{view} at {width}px/{theme} overflows by {overflow}px"
    finally:
        ctx.close()


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("theme", THEMES)
def test_every_piece_of_text_meets_aa(browser, site, width, theme):
    ctx, page, _ = _page(browser, site, width, theme)
    try:
        for view in ("feed", "listings", "searches", "status"):
            page.click(f'[data-view-link="{view}"]')
            page.wait_for_timeout(200)
            bad = page.evaluate(CONTRAST_JS)
            assert not bad, f"{view} at {width}px/{theme}: {json.dumps(bad[:4])}"
    finally:
        ctx.close()


@pytest.mark.parametrize("width", WIDTHS)
def test_the_tab_labels_sit_on_one_line(browser, site, width):
    """A count badge must not make its tab taller than the one without."""
    ctx, page, _ = _page(browser, site, width, "light")
    try:
        tops = page.evaluate(
            "() => [...document.querySelectorAll('.tab > span:first-child')]"
            ".map(s => Math.round(s.getBoundingClientRect().top))")
        assert tops, "no tabs rendered"
        assert max(tops) - min(tops) <= 2, f"tab labels vary by {max(tops)-min(tops)}px"
    finally:
        ctx.close()


def test_nothing_is_too_small_to_tap(browser, site):
    ctx, page, _ = _page(browser, site, 390, "light", view="listings")
    try:
        small = page.evaluate("""() => [...document.querySelectorAll('button,a,select,input')]
          .filter(e => { const r = e.getBoundingClientRect();
            return r.width > 0 && r.height > 0 && (r.height < 30 || r.width < 30); })
          .map(e => (e.textContent || e.tagName).trim().slice(0, 30))""")
        assert not small, f"tap targets under 30px: {small}"
    finally:
        ctx.close()


def test_the_page_does_not_jump_while_it_loads(browser, site):
    """Cumulative layout shift, measured the way a browser measures it."""
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    try:
        page.add_init_script("""window.__cls = 0;
          new PerformanceObserver(l => { for (const e of l.getEntries())
            if (!e.hadRecentInput) window.__cls += e.value;
          }).observe({type: 'layout-shift', buffered: true});""")
        page.goto(site, wait_until="networkidle")
        page.wait_for_timeout(900)
        cls = page.evaluate("() => window.__cls")
        assert cls < 0.02, f"layout shifted by {cls:.4f}"
    finally:
        ctx.close()


def test_the_detail_sheet_opens_closes_and_hands_focus_back(browser, site):
    ctx, page, errors = _page(browser, site, 390, "light", view="listings")
    try:
        page.click(".card")
        page.wait_for_timeout(250)
        assert page.evaluate("() => document.getElementById('sheet').dataset.open === '1'")
        assert page.evaluate(
            "() => document.activeElement.id === 'sheet-close'"), "focus did not enter the sheet"
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
        assert page.evaluate("() => document.getElementById('sheet').dataset.open !== '1'")
        assert page.evaluate(
            "() => document.activeElement.classList.contains('card')"), "focus did not come back"
        assert not errors, errors
    finally:
        ctx.close()


def test_a_car_is_never_named_with_its_year_twice(browser, site):
    """"2025 2025 Honda Civic Sport PKG", on the view people read most.

    The bot composes a name like "2025 Honda Civic" for a car whose results card
    carried no title, and the Feed prepended the year to whatever it was
    given. There were two copies of that line - one on the cards, one on the
    feed rows - so fixing the first left the second doing it.
    """
    import re
    ctx, page, _ = _page(browser, site, 390, "light")
    try:
        names = page.evaluate(
            "() => [...document.querySelectorAll('.ev__title')].map(e => e.textContent.trim())")
        assert names, "no feed rows to check"
        for name in names:
            assert not re.match(r"^(\d{4})\s+\1\b", name), name
    finally:
        ctx.close()


def test_a_caption_under_a_figure_stays_smaller_than_the_figure(browser, site):
    """Both are <dd> inside .stat, and that is how this broke.

    The captions were <small> until an accessibility fix made them a second
    <dd> - which is the correct markup, and which put them behind `.stat dd`,
    a class plus a type selector that a bare `.stat__note` class cannot beat.
    Every caption on the Market tab rendered at figure size: bold, 32px, three
    lines of explanation shouting over the number it was explaining. It
    shipped, because a caption in the wrong size still looks deliberate.
    """
    ctx, page, _ = _page(browser, site, 1440, "light", view="market")
    try:
        sizes = page.evaluate("""() => [...document.querySelectorAll('.stat')]
            .filter(s => s.querySelector('.stat__note') && s.querySelector('dd.num'))
            .map(s => ({
              label: s.querySelector('dt').textContent.trim(),
              figure: parseFloat(getComputedStyle(s.querySelector('dd.num')).fontSize),
              note: parseFloat(getComputedStyle(s.querySelector('.stat__note')).fontSize),
            }))""")
        assert sizes, "no stat tile had a caption to measure"
        for tile in sizes:
            assert tile["note"] < tile["figure"] * 0.75, tile
    finally:
        ctx.close()


def test_an_alert_link_opens_that_car(browser, site, payload):
    """ntfy sends you to #/listing/<id>; that has to land on the car."""
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    try:
        page.goto(site, wait_until="networkidle")
        page.wait_for_timeout(250)
        target = next(l for l in payload["listings"] if not l.get("filtered"))
        page.goto(f"{site}#/listing/{target['id']}", wait_until="networkidle")
        page.wait_for_timeout(400)
        assert page.evaluate("() => document.getElementById('sheet').dataset.open === '1'")
        title = page.inner_text("#sheet-title")
        assert str(target.get("year") or "") in title or target["id"][:6] in title, title
    finally:
        ctx.close()


def test_the_service_worker_and_manifest_are_publishable():
    """The parts that make it installable, checked without a browser."""
    manifest = json.loads((DOCS / "manifest.webmanifest").read_text())
    assert manifest["start_url"].endswith("#/feed")
    for icon in manifest["icons"]:
        assert (DOCS / icon["src"]).exists(), f"{icon['src']} is missing"
    assert any(i.get("purpose") == "maskable" for i in manifest["icons"])
    sw = (DOCS / "sw.js").read_text()
    assert "data.json" in sw, "the data file needs its own caching rule"


class TestWhatAScreenshotDoesNotCatch:
    """Four defects shipped this month that every test passed and every
    screenshot looked fine with:

      * a caption rendering at headline size, because a class selector lost a
        specificity fight to a class+type one;
      * a placeholder string, "AutoTrader listing <uuid>", on every row of the
        Feed;
      * every photo failing to load, for a day, while the page looked like a
        page whose cars simply had no photos;
      * a number derived one way printed beside a number derived another.

    None of them is visible to a test that asserts on data. All four are
    visible to a browser that is asked the right question, which is what these
    are.
    """

    def test_no_text_is_rendered_at_a_size_its_rule_did_not_ask_for(self, browser, site):
        """Every element whose class names a size gets the size that class
        defines - not one it inherited by losing a specificity fight."""
        ctx, page, _ = _page(browser, site, 1440, "light", view="market")
        try:
            bad = page.evaluate("""() => {
              const out = [];
              // Classes that exist specifically to make text smaller.
              for (const cls of ['stat__note', 'note', 'why', 'count']) {
                for (const el of document.querySelectorAll('.' + cls)) {
                  if (!el.offsetParent) continue;
                  const size = parseFloat(getComputedStyle(el).fontSize);
                  if (size > 20) out.push({cls, size, text: el.textContent.trim().slice(0, 40)});
                }
              }
              return out;
            }""")
            assert not bad, bad
        finally:
            ctx.close()

    def test_a_caption_is_never_painted_in_the_figures_colour(self, browser, site):
        """`.stat[data-tone] dd` is (0,2,1) and so is `.stat dd.stat__note`.

        A tie, broken by source order, and the tone block comes later - so the
        green "100%" painted its caption green too, and a red figure painted a
        whole sentence red. The same trap as the caption SIZE, one rule
        further down the same file, and the size fix walked straight past it.
        """
        ctx, page, _ = _page(browser, site, 1440, "light", view="status")
        try:
            page.wait_for_timeout(300)
            bad = page.evaluate("""() => [...document.querySelectorAll('.stat[data-tone]')]
                .filter(s => s.querySelector('.stat__note') && s.querySelector('dd.num'))
                .map(s => ({
                  tone: s.dataset.tone,
                  figure: getComputedStyle(s.querySelector('dd.num')).color,
                  note: getComputedStyle(s.querySelector('.stat__note')).color,
                }))
                .filter(r => r.figure === r.note)""")
            assert not bad, bad
        finally:
            ctx.close()

    def test_an_input_is_not_styled_like_its_own_label(self, browser, site):
        """`.labelled > span` matched the <span class="field"> wrapper as well
        as the label, so every rules-editor input inherited an 11px uppercase
        letter-spaced tertiary style."""
        ctx, page, _ = _page(browser, site, 1440, "light", view="searches")
        try:
            page.wait_for_timeout(400)
            bad = page.evaluate("""() => [...document.querySelectorAll('.labelled input')]
                .filter(i => i.offsetParent)
                .map(i => ({size: parseFloat(getComputedStyle(i).fontSize),
                            transform: getComputedStyle(i).textTransform,
                            spacing: getComputedStyle(i).letterSpacing}))
                .filter(r => r.size < 14 || r.transform === 'uppercase')""")
            assert not bad, bad
        finally:
            ctx.close()

    def test_no_placeholder_string_reaches_the_page(self, browser, site):
        """The strings this codebase uses when it does not know something.

        Every one of them is correct in its own layer and wrong on a screen.
        """
        ctx, page, _ = _page(browser, site, 390, "light")
        try:
            for view in ("feed", "listings", "market", "searches", "status"):
                page.click(f'[data-view-link="{view}"]')
                page.wait_for_timeout(200)
                text = page.evaluate("() => document.body.innerText")
                for leak in ("AutoTrader listing ", "undefined", "NaN",
                             "[object Object]", "null", "Infinity"):
                    assert leak not in text, f"{view}: {leak!r} is on the page"
        finally:
            ctx.close()

    def test_every_image_actually_loaded(self, browser, site):
        """naturalWidth is 0 for an image the browser could not decode.

        For a day every photo fetch failed - the site had started serving
        1920x1080 originals over a 60 KB cap - and the page looked exactly
        like a page whose cars have no photos, because that is the fallback.
        """
        ctx, page, _ = _page(browser, site, 390, "light", view="listings")
        try:
            page.wait_for_timeout(700)
            broken = page.evaluate("""() => [...document.images]
                .filter(i => i.complete && i.naturalWidth === 0)
                .map(i => i.currentSrc || i.src)""")
            assert not broken, broken
            count = page.evaluate("() => document.images.length")
            assert count, "no images on a listings page built from cars with photos"
        finally:
            ctx.close()

    def test_a_photo_keeps_its_shape(self, browser, site):
        """A 4:3 photo in a portrait slot is the middle third of a car.

        Measured, not asserted from the CSS: the rule that broke this said the
        right thing and lost.
        """
        ctx, page, _ = _page(browser, site, 390, "light", view="listings")
        try:
            page.wait_for_timeout(700)
            shots = page.evaluate("""() => [...document.querySelectorAll('.card__shot')]
                .filter(e => e.offsetParent)
                .map(e => { const r = e.getBoundingClientRect();
                            return {w: r.width, h: r.height}; })""")
            assert shots, "no card photos to measure"
            for box in shots:
                ratio = box["w"] / box["h"]
                assert 1.0 < ratio < 2.0, (
                    f"a photo slot {box['w']:.0f}x{box['h']:.0f} is "
                    f"{ratio:.2f}:1 - a landscape photo is being cropped to "
                    f"a sliver")
        finally:
            ctx.close()

    def test_every_table_can_be_scrolled_to_the_end_of(self, browser, site):
        """A table is the one thing here allowed to be wider than its column -
        dealer search names and score strings have no upper bound - and only
        inside its own scroller.

        Five call sites built a table by hand and none wrapped it. On a phone
        the Status tab's parser ladder was 365px in a 358px column: seven
        pixels of it spilled under body{overflow-x:hidden}, where it was
        invisible and could not be scrolled to.
        """
        ctx, page, _ = _page(browser, site, 390, "light", view="status")
        try:
            page.wait_for_timeout(400)
            bad = page.evaluate("""() => [...document.querySelectorAll('table')]
                .filter(t => t.offsetParent)
                .filter(t => {
                  let n = t.parentElement;
                  while (n && n !== document.body) {
                    const ox = getComputedStyle(n).overflowX;
                    if (ox === 'auto' || ox === 'scroll') return false;
                    n = n.parentElement;
                  }
                  return true;
                })
                .map(t => t.textContent.trim().slice(0, 40))""")
            assert not bad, bad
        finally:
            ctx.close()

    def test_nothing_spills_where_it_cannot_be_reached(self, browser, site):
        """Content past the right edge of the viewport, in an element nobody
        can scroll. The chips row is 390px wide on purpose and scrolls; a
        table that is 7px too wide inside a clipped parent does not."""
        ctx, page, _ = _page(browser, site, 390, "light", view="listings")
        try:
            page.wait_for_timeout(400)
            spills = page.evaluate("""() => {
              const scrollable = el => {
                let n = el;
                while (n && n !== document.body) {
                  const ox = getComputedStyle(n).overflowX;
                  if (ox === 'auto' || ox === 'scroll') return true;
                  n = n.parentElement;
                }
                return false;
              };
              const out = [];
              for (const el of document.querySelectorAll('body *')) {
                if (!el.offsetParent || scrollable(el)) continue;
                const r = el.getBoundingClientRect();
                if (r.right > innerWidth + 1 || r.left < -1)
                  out.push({cls: String(el.className).slice(0, 30),
                            right: Math.round(r.right), win: innerWidth,
                            text: el.textContent.trim().slice(0, 30)});
              }
              return out;
            }""")
            assert not spills, spills
        finally:
            ctx.close()

    def test_the_schedule_is_described_in_the_units_it_uses(self, browser, site, payload):
        """"12 of 12 half-hours", on a bot that checks every two hours.

        The word was written into four separate strings while the schedule
        happened to be half-hourly, and stayed there when it stopped being.
        """
        ctx, page, _ = _page(browser, site, 390, "light", view="status")
        try:
            page.wait_for_timeout(300)
            text = page.evaluate("() => document.body.innerText")
            interval = payload.get("coverage", {}).get("expected_interval_minutes")
            if interval and interval != 30:
                assert "half-hour" not in text, (
                    f"the page says half-hours while the schedule asks for one "
                    f"check every {interval} minutes")
        finally:
            ctx.close()

    def test_every_number_the_page_repeats_agrees_with_itself(self, browser, site, payload):
        """The count in the tab badge is the count on the tab."""
        ctx, page, _ = _page(browser, site, 1440, "light")
        try:
            badge = page.evaluate(
                """() => document.querySelector('[data-view-link="listings"] .tab__n')?.textContent.trim()""")
            page.click('[data-view-link="listings"]')
            page.wait_for_timeout(300)
            chip = page.evaluate(
                """() => [...document.querySelectorAll('.chips button')]
                     .find(b => /live/i.test(b.textContent))?.textContent.match(/\\d+/)?.[0]""")
            assert badge and chip, (badge, chip)
            assert badge == chip, f"tab badge says {badge}, the Live chip says {chip}"
        finally:
            ctx.close()


def test_the_coverage_percentage_is_always_shown_with_its_own_fraction():
    """cov.pct is slots covered over slots expected. Printing it beside the
    number of runs gave "79.2% - 51 of 48 expected checks" in the banner while
    the Status tab, two taps away, said 38 of 48. Both true; one sentence."""
    js = (DOCS / "app.js").read_text()
    for line in js.splitlines():
        if "cov.pct}%" not in line:
            continue
        assert "cov.expected" not in line or "slots_covered" in line, line.strip()


def test_the_app_script_parses():
    """A syntax error here is a blank page, and no other test would see it."""
    node = subprocess.run(["node", "--version"], capture_output=True)
    if node.returncode != 0:
        pytest.skip("no node to parse with")
    out = subprocess.run(["node", "--check", str(DOCS / "app.js")],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


class TestVoiceControlCanSayWhatItSees:
    """WCAG 2.5.3, Label in Name.

    The feed cards carried an aria-label reading "Price drop: 2018 Honda Civic.
    $21,000 $20,000 alerted" while visibly reading "28h ago … 2018 Honda Civic …
    -$1,000 … $21,000 $20,000 alerted". Someone driving the page by voice
    reads what is on screen and says it, and no phrase they could see matched
    the name the button answered to. Lighthouse caught it; the axe ruleset
    this project runs does not include that check.

    The fix is structural rather than a longer label: the name is built from
    the button's own content, so the two cannot disagree again.
    """

    @staticmethod
    def _code(block: str) -> str:
        """The block with // comments stripped.

        The first version of this test matched the word "aria-label" inside
        the comment explaining why there is no aria-label, and failed.
        """
        import re
        return re.sub(r"^\s*//.*$", "", block, flags=re.M)

    def test_a_feed_card_has_no_aria_label_to_disagree_with(self):
        from pathlib import Path
        js = Path("docs/app.js").read_text()
        block = js.split("const b = el('button', 'ev'")[1].split("li.appendChild(b)")[0]
        assert "aria-label" not in self._code(block), (
            "an aria-label here has to repeat every visible word or it fails "
            "Label in Name - name it from the content instead")

    def test_the_kind_still_reaches_a_screen_reader(self):
        from pathlib import Path
        js = Path("docs/app.js").read_text()
        block = js.split("const b = el('button', 'ev'")[1].split("li.appendChild(b)")[0]
        assert 'class="sr"' in block and "KIND[e.kind].label" in block


class TestNoMetricRanksWhatItCannotRead:
    """Every one of these is a number the page used to state about a car
    whose own row says it cannot be known.

    The cheapest-first list put the two "Call for price" cars at the bottom,
    which is a ranking; the specification table showed a blank where the
    price-per-kilometre would be, which reads as "this car has no odometer"
    whatever the actual reason; and the comparison to other cars appeared on
    the sheet but not on the card, so a blank card meant either "no cohort"
    or "a cohort, and an unremarkable answer".
    """

    @staticmethod
    def _sorted_by(page, sort_id):
        page.select_option("#sort", sort_id)
        page.wait_for_timeout(200)
        return page.evaluate("""() => [...document.querySelectorAll(
            '.grid > .card, .grid > .grid__split')].map(n =>
            n.classList.contains('grid__split')
              ? {split: n.textContent.trim()}
              : {car: n.innerText.replace(/\\n/g, ' ').slice(0, 80)})""")

    def test_a_car_with_no_asking_price_is_not_the_dearest_car(self, browser, site):
        ctx, page, _ = _page(browser, site, 1440, "light", view="listings")
        try:
            rows = self._sorted_by(page, "priced")
            split = next((i for i, r in enumerate(rows) if "split" in r), None)
            assert split is not None, "no divider above the unranked cars"
            above = [r["car"] for r in rows[:split]]
            below = [r["car"] for r in rows[split + 1:]]
            assert any("Call for price" in c for c in below), below
            assert not any("Call for price" in c for c in above), above
        finally:
            ctx.close()

    def test_and_is_not_the_cheapest_car_either(self, browser, site):
        """The same rows, the opposite sort. `?? Infinity` got this one
        right by accident and `?? -Infinity` got it wrong, which is the
        tell that neither was a decision."""
        ctx, page, _ = _page(browser, site, 1440, "light", view="listings")
        try:
            rows = self._sorted_by(page, "price")
            split = next((i for i, r in enumerate(rows) if "split" in r), None)
            assert split is not None
            assert not any("Call for price" in r["car"] for r in rows[:split])
        finally:
            ctx.close()

    def test_the_divider_says_how_many_and_what_is_missing(self, browser, site):
        ctx, page, _ = _page(browser, site, 1440, "light", view="listings")
        try:
            for sort_id, missing in (("price", "no asking price"),
                                     ("km", "no odometer reading")):
                rows = self._sorted_by(page, sort_id)
                split = next((r["split"] for r in rows if "split" in r), None)
                assert split, f"{sort_id}: no divider"
                assert missing in split, (sort_id, split)
                # "1 car", never "1 cars".
                n = int(split.split()[0].replace(",", ""))
                assert split.startswith(f"{n} car" + ("" if n == 1 else "s")), split
        finally:
            ctx.close()

    def test_no_sort_leaves_a_car_out_of_the_list(self, browser, site):
        """A partition that drops rows is worse than a bad ranking."""
        ctx, page, _ = _page(browser, site, 1440, "light", view="listings")
        try:
            counts = set()
            for sort_id in ("newest", "price", "priced", "year", "km",
                            "distance", "days"):
                rows = self._sorted_by(page, sort_id)
                counts.add(sum(1 for r in rows if "car" in r))
            assert len(counts) == 1, counts
        finally:
            ctx.close()

    def test_the_ratio_explains_its_own_blank(self, browser, site):
        """A car with 12,000 km on it has no price per 1,000 km, and the
        reason is about the ratio rather than about the car."""
        ctx, page, _ = _page(browser, site, 1440, "light", view="listings")
        try:
            page.click(".grid > .card")
            page.wait_for_timeout(300)
            rows = page.evaluate("""() => {
              const kv = document.querySelector('.sheet .kv');
              if (!kv) return [];
              const out = [];
              const kids = [...kv.children];
              for (let i = 0; i < kids.length - 1; i += 2)
                out.push([kids[i].textContent.trim(), kids[i + 1].textContent.trim()]);
              return out;
            }""")
            assert rows, "no specification table"
            ratio = [v for k, v in rows if "1,000 km" in k]
            assert ratio, [k for k, _ in rows]
            assert ratio[0] != "", "a blank with no reason"
            assert ratio[0] != "—", ratio
        finally:
            ctx.close()

    def test_the_comparison_is_gated_the_same_way_in_both_places(self, browser, site):
        """One gate, read from the page rather than from the source: if a
        card carries a comparison, its sheet carries the sentence, and if a
        card carries none, the sheet says why rather than staying silent."""
        ctx, page, _ = _page(browser, site, 1440, "light", view="listings")
        try:
            n = page.evaluate("() => document.querySelectorAll('.grid > .card').length")
            for i in range(n):
                page.wait_for_timeout(120)
                page.evaluate(f"() => document.querySelectorAll('.grid > .card')[{i}].click()")
                page.wait_for_timeout(250)
                said = page.evaluate(
                    "() => (document.querySelector('.sheet .note--cmp') || {})"
                    ".textContent || ''").strip()
                badge = page.evaluate(
                    f"() => (document.querySelectorAll('.grid > .card')[{i}]"
                    ".querySelector('.card__foot span') || {}).textContent || ''")
                assert said, f"card {i}: the sheet says nothing about comparables"
                if "%" in badge or "cheapest of" in badge:
                    assert badge.split()[0] in said, (badge, said)
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)
        finally:
            ctx.close()


@pytest.mark.parametrize("width", (390, 1440))
def test_the_placeholder_is_the_size_of_the_thing_it_stands_in_for(browser, site, width):
    """The loading rows were 48px on a phone; the rows they stand in for are
    88.5. Eight of them, so the page grew 320px under the reader's thumb the
    moment the data landed.

    The layout-shift test never saw it: a local data.json arrives before the
    first paint, and CLS only counts what was painted. So this measures the
    two heights directly, with the data held open.
    """
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    page = ctx.new_page()
    try:
        page.route("**/data.json", lambda route: None)   # never answered
        page.goto(site, wait_until="domcontentloaded")
        page.wait_for_selector(".skel-row")
        placeholder = page.evaluate(
            "() => document.querySelector('.skel-row').getBoundingClientRect().height")
        page.unroute("**/data.json")
    finally:
        ctx.close()

    ctx, page, _ = _page(browser, site, width, "light")
    try:
        row = page.evaluate(
            "() => document.querySelector('.ev').getBoundingClientRect().height")
    finally:
        ctx.close()
    assert abs(placeholder - row) < 1.5, (placeholder, row)


class TestTheEmptyStateBlamesTheRightControl:
    """The old branch order tested text, then the hidden chip, then the
    search, then the chip - and blamed the first one it reached.

    So a search plus a chip that is empty inside it produced "<search> has
    nothing to show. It read the site fine and every car was turned away", a
    sentence about a search holding twenty cars, over a button that cleared
    the search and left the chip - and the list stayed empty when pressed.
    """

    def test_two_narrowed_controls_offer_two_ways_out(self, browser, site):
        ctx, page, _ = _page(browser, site, 1440, "light", view="listings")
        try:
            page.evaluate("""() => {
              app.search = (app.data.searches || [])[0].id;
              app.chip = 'gone';
              app.q = 'nothing matches this string';
              renderListings();
            }""")
            page.wait_for_timeout(200)
            state = page.evaluate("""() => {
              const s = document.querySelector('[data-view="listings"] .state');
              return s && { text: s.innerText,
                            buttons: [...s.querySelectorAll('button')]
                              .map(b => b.textContent.trim()) };
            }""")
            assert state, "no empty state drawn"
            assert len(state["buttons"]) == 3, state
            assert "Clear the text filter" in state["buttons"]
            assert "Show every live car" in state["buttons"]
            assert "Show all searches" in state["buttons"]
        finally:
            ctx.close()

    def test_pressing_the_one_way_out_actually_shows_cars(self, browser, site):
        """The button that clears the control the page blamed has to be the
        button that ends the emptiness."""
        ctx, page, _ = _page(browser, site, 1440, "light", view="listings")
        try:
            page.evaluate("() => { app.q = 'zzzznothing'; renderListings(); }")
            page.wait_for_timeout(150)
            page.click('[data-view="listings"] .state button')
            page.wait_for_timeout(200)
            cards = page.evaluate("() => document.querySelectorAll('.grid > .card').length")
            assert cards, "the way out led back to the same empty page"
        finally:
            ctx.close()

    def test_an_empty_chip_is_named_by_the_chip(self, browser, site):
        ctx, page, _ = _page(browser, site, 1440, "light", view="listings")
        try:
            page.evaluate("() => { app.chip = 'drops'; renderListings(); }")
            page.wait_for_timeout(150)
            text = page.evaluate(
                "() => (document.querySelector('[data-view=\"listings\"] .state')"
                " || {}).innerText || ''")
            if text:
                assert "price drops" in text.lower(), text
                assert "search" not in text.lower(), text
        finally:
            ctx.close()


class TestOneReaderTwoAnswers:
    """Numbers the page shows in two places at once, which disagreed."""

    def test_the_header_does_not_print_a_percentage_the_tile_withholds(
            self, browser, site):
        """The Status tile learned to say "measuring for 3.6 hours so far"
        rather than a figure from one complete slot. The header went on
        printing "100% covered" three centimetres above it."""
        ctx, page, _ = _page(browser, site, 1440, "light")
        try:
            for too_short in (True, False):
                page.evaluate(f"""() => {{
                  app.data.coverage = Object.assign({{}}, app.data.coverage, {{
                    too_short: {str(too_short).lower()}, pct: 100,
                    window_hours: 3.6, expected: 1, slots_covered: 1,
                    successful: 3, expected_interval_minutes: 120 }});
                  renderTrust(); go('status');
                }}""")
                page.wait_for_timeout(250)
                header = page.evaluate(
                    "() => document.getElementById('trust-cov').textContent")
                tile = page.evaluate("""() => {
                  const d = [...document.querySelectorAll('[data-view="status"] .stat')]
                    .find(s => /coverage/i.test(s.querySelector('dt').textContent));
                  return d ? d.querySelector('dd').textContent.trim() : null; }""")
                if too_short:
                    assert "%" not in header, header
                    assert tile == "—", tile
                else:
                    assert "100%" in header, header
                    assert "100%" in tile, tile
        finally:
            ctx.close()

    def test_the_live_chip_counts_what_the_live_chip_shows(self, browser, site):
        """"Live 28" over a grid of 89, the moment you press Include hidden."""
        ctx, page, _ = _page(browser, site, 1440, "light", view="listings")
        try:
            for _ in range(2):
                said = page.evaluate("""() => {
                  const c = [...document.querySelectorAll('.chips .chip')]
                    .find(b => b.textContent.startsWith('Live'));
                  return c ? parseInt(c.querySelector('.n').textContent.replace(/,/g, ''), 10) : null;
                }""")
                drawn = page.evaluate(
                    "() => document.querySelectorAll('.grid > .card').length")
                assert said == drawn, (said, drawn)
                toggled = page.evaluate("""() => {
                  const b = [...document.querySelectorAll('.chips .chip')]
                    .find(x => /hidden/i.test(x.textContent) && x.hasAttribute('aria-pressed')
                               && x.getAttribute('aria-pressed') !== null
                               && !/Hidden by a rule/.test(x.textContent));
                  if (!b) return false; b.click(); return true; }""")
                if not toggled:
                    break
                page.wait_for_timeout(250)
        finally:
            ctx.close()

    def test_no_two_chips_wear_the_same_number_and_nearly_the_same_word(
            self, browser, site):
        ctx, page, _ = _page(browser, site, 1440, "light", view="listings")
        try:
            chips = page.evaluate("""() => [...document.querySelectorAll('.chips .chip')]
                .map(c => c.textContent.trim())""")
            hidden = [c for c in chips if "hidden" in c.lower()]
            assert len(set(hidden)) == len(hidden), chips
            with_counts = [c for c in hidden if any(ch.isdigit() for ch in c)]
            assert len(with_counts) <= 1, hidden
        finally:
            ctx.close()

    def test_an_unpriced_card_is_headed_by_the_car(self, browser, site):
        """The largest text on three of the first four cards was the absence
        of a figure, and the car it belonged to was the grey line beneath."""
        ctx, page, _ = _page(browser, site, 1440, "light", view="listings")
        try:
            heads = page.evaluate("""() => [...document.querySelectorAll('.grid > .card')]
                .map(c => ({ head: (c.querySelector('.card__price') || {}).textContent || '',
                             sub: (c.querySelector('.card__title') || {}).textContent || '' }))""")
            assert heads, "no cards"
            for card in heads:
                assert "Call for price" not in card["head"], card
                assert card["head"].strip(), card
                # And the name is said once, not twice.
                assert card["head"].strip() != card["sub"].strip(), card
        finally:
            ctx.close()


class TestThePageCannotOverstateCoverage:
    """85.7% of slots covered, 0% of them by the schedule. Both true, and a
    reader given only the first has been told the bot is fine when what is
    keeping it alive is somebody pushing to the repository."""

    def _say(self, page, cov):
        page.evaluate(
            "c => { app.data.coverage = Object.assign({}, app.data.coverage, c);"
            " renderTrust(); go('status'); }", cov)
        page.wait_for_timeout(250)
        return {
            "header": page.evaluate(
                "() => document.getElementById('trust-cov').textContent"),
            "tile": page.evaluate("""() => {
              const d = [...document.querySelectorAll('[data-view="status"] .stat')]
                .find(s => /coverage/i.test(s.querySelector('dt').textContent));
              return d ? d.innerText : ''; }"""),
        }

    BASE = {"too_short": False, "pct": 100.0, "expected": 4,
            "slots_covered": 4, "window_hours": 8, "successful": 4,
            "expected_interval_minutes": 120}

    def test_a_schedule_that_never_fired_is_said_out_loud(self, browser, site):
        ctx, page, _ = _page(browser, site, 1440, "light")
        try:
            said = self._say(page, dict(self.BASE, slots_scheduled=0,
                                        by_trigger={"push": 3, "workflow_dispatch": 1}))
            assert "none of it scheduled" in said["header"].lower(), said["header"]
            assert "push" in said["tile"].lower(), said["tile"]
            assert "goes to zero" in said["tile"], said["tile"]
        finally:
            ctx.close()

    def test_a_schedule_doing_its_job_gets_a_plain_percentage(self, browser, site):
        ctx, page, _ = _page(browser, site, 1440, "light")
        try:
            said = self._say(page, dict(self.BASE, slots_scheduled=4,
                                        by_trigger={"schedule": 4}))
            assert "100%" in said["header"]
            assert "scheduled" not in said["header"].lower(), said["header"]
        finally:
            ctx.close()

    def test_a_mix_names_both_numbers(self, browser, site):
        ctx, page, _ = _page(browser, site, 1440, "light")
        try:
            said = self._say(page, dict(self.BASE, slots_scheduled=2,
                                        by_trigger={"schedule": 2, "push": 2}))
            assert "2 of 4 scheduled" in said["header"], said["header"]
        finally:
            ctx.close()

    def test_no_record_is_not_the_same_as_no_schedule(self, browser, site):
        """A payload written before the bot recorded triggers supports
        "we do not know", not "the schedule did nothing"."""
        ctx, page, _ = _page(browser, site, 1440, "light")
        try:
            said = self._say(page, dict(self.BASE, slots_scheduled=0,
                                        by_trigger={"unattributed": 4}))
            assert "none of it scheduled" not in said["header"].lower(), said["header"]
            assert "not recorded" in said["header"].lower(), said["header"]
            assert "before the bot noted" in said["tile"], said["tile"]
        finally:
            ctx.close()


class TestTheMinutesAreShownHonestly:
    def test_exempt_and_drawing_are_two_tiles_not_one(self, browser, site):
        ctx, page, _ = _page(browser, site, 1440, "light", view="status")
        try:
            page.evaluate("""() => {
              app.data.budget = Object.assign({}, app.data.budget, {
                exempt_minutes: 3700, drawing_minutes: 0, unknown_minutes: 0,
                used: 3700, days_to_reset: 18, can_still_run: true,
                state: 'exempt', why: 'it is public and on standard runners',
                text: 'none of them drawing on the allowance.' });
              renderStatus(); }""")
            page.wait_for_timeout(250)
            tiles = page.evaluate("""() => Object.fromEntries(
              [...document.querySelectorAll('[data-view="status"] .stat')]
                .map(s => [s.querySelector('dt').textContent.trim(),
                           s.innerText]))""")
            assert any("Allowance minutes" in k for k in tiles), list(tiles)
            assert any("Exempt minutes" in k for k in tiles), list(tiles)
            can = [v for k, v in tiles.items() if "Can it still run" in k]
            assert can and "Yes" in can[0], can
            assert "18 days" in can[0], can
        finally:
            ctx.close()

    def test_a_stopped_bot_says_no(self, browser, site):
        ctx, page, _ = _page(browser, site, 1440, "light", view="status")
        try:
            page.evaluate("""() => {
              app.data.budget = Object.assign({}, app.data.budget, {
                exempt_minutes: 0, drawing_minutes: 2600, used: 2600,
                days_to_reset: 18, can_still_run: false, state: 'stop',
                text: 'It has stopped checking.' });
              renderStatus(); }""")
            page.wait_for_timeout(250)
            can = page.evaluate("""() => {
              const d = [...document.querySelectorAll('[data-view="status"] .stat')]
                .find(s => /can it still run/i.test(s.querySelector('dt').textContent));
              return d ? d.innerText : ''; }""")
            assert "No" in can, can
            assert "BUDGET-STOP" in can, can
        finally:
            ctx.close()


class TestThePageNamesTheTimerKeepingTime:
    """"An outside timer is working" is not something you can go and look at
    when it stops. The caller names itself and the page prints the name."""

    BASE = {"too_short": False, "pct": 100.0, "expected": 4,
            "slots_covered": 4, "window_hours": 8, "successful": 4,
            "expected_interval_minutes": 120}

    def _tile(self, page, cov):
        page.evaluate(
            "c => { app.data.coverage = Object.assign({}, app.data.coverage, c);"
            " renderStatus(); }", cov)
        page.wait_for_timeout(250)
        return page.evaluate("""() => {
          const d = [...document.querySelectorAll('[data-view="status"] .stat')]
            .find(s => /keeping time/i.test(s.querySelector('dt').textContent));
          return d ? d.innerText : ''; }""")

    def test_an_outside_timer_is_named(self, browser, site):
        ctx, page, _ = _page(browser, site, 1440, "light", view="status")
        try:
            tile = self._tile(page, dict(
                self.BASE, slots_scheduled=4,
                timekeeper="repository_dispatch:cron-job.org",
                slots_by_trigger={"repository_dispatch:cron-job.org": 3, "schedule": 1},
                by_trigger={"repository_dispatch": 3, "schedule": 1}))
            assert "cron-job.org" in tile, tile
            assert "outside timer" in tile, tile
        finally:
            ctx.close()

    def test_it_says_so_when_a_person_is_the_timer(self, browser, site):
        ctx, page, _ = _page(browser, site, 1440, "light", view="status")
        try:
            tile = self._tile(page, dict(
                self.BASE, slots_scheduled=0, timekeeper="push",
                slots_by_trigger={"push": 4}, by_trigger={"push": 4}))
            assert "push to the repository" in tile, tile
        finally:
            ctx.close()

    def test_the_raw_event_name_never_reaches_the_reader(self, browser, site):
        ctx, page, _ = _page(browser, site, 1440, "light", view="status")
        try:
            tile = self._tile(page, dict(
                self.BASE, slots_scheduled=4, timekeeper="repository_dispatch",
                slots_by_trigger={"repository_dispatch": 4},
                by_trigger={"repository_dispatch": 4}))
            assert "repository_dispatch" not in tile, tile
            assert "outside timer" in tile, tile
        finally:
            ctx.close()


@pytest.mark.parametrize("width", (390, 834, 1440))
def test_no_stat_tile_is_mostly_empty_because_of_its_neighbour(browser, site, width):
    """A grid row is as tall as its tallest tile, so one tile carrying a
    paragraph stretches the short ones beside it into mostly-empty boxes.

    On a phone the coverage explanation was 370px and "Last good check" was
    130px of text inside a 370px card, which reads as a rendering fault.
    """
    ctx, page, _ = _page(browser, site, width, "light", view="status")
    try:
        worst = page.evaluate("""() => {
          let worst = null;
          for (const s of document.querySelectorAll('[data-view="status"] .stat')) {
            const box = s.getBoundingClientRect().height;
            // How much of the card its own content actually occupies.
            let content = 0;
            for (const kid of s.children) content += kid.getBoundingClientRect().height;
            const style = getComputedStyle(s);
            content += parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
            const filled = content / box;
            if (!worst || filled < worst.filled) {
              worst = { name: s.querySelector('dt').textContent.trim(),
                        box: Math.round(box), content: Math.round(content),
                        filled: Math.round(filled * 100) / 100 };
            }
          }
          return worst; }""")
        assert worst, "no stat tiles"
        assert worst["filled"] >= 0.5, (
            f"{width}px: '{worst['name']}' is {worst['content']}px of content "
            f"in a {worst['box']}px card ({worst['filled']:.0%} full)")
    finally:
        ctx.close()


class TestTheFeedShowsTheCarNotJustItsName:
    """A hundred lines of "2017 Honda Civic LX 4dr Sedan CVT".

    The Feed is a list of things that happened to cars and it was text only,
    while the bot had already downloaded a photograph of every one of them.
    You can tell a hatchback from a sedan at a glance from a picture and not
    from a trim string.
    """

    @pytest.mark.parametrize("width", WIDTHS)
    def test_every_row_has_a_photo_box(self, browser, site, width):
        ctx, page, _ = _page(browser, site, width, "light", view="feed")
        try:
            page.wait_for_timeout(400)
            rows = page.evaluate("() => document.querySelectorAll('.ev').length")
            shots = page.evaluate(
                "() => document.querySelectorAll('.ev .ev__shot').length")
            assert rows, "no feed rows to check"
            assert shots == rows, f"{rows - shots} rows have no photo box"
        finally:
            ctx.close()

    @pytest.mark.parametrize("width", WIDTHS)
    def test_the_box_is_one_size_whether_or_not_a_picture_arrives(self, browser,
                                                                  site, width):
        """Or the line under it moves when one does, which is layout shift."""
        ctx, page, _ = _page(browser, site, width, "light", view="feed")
        try:
            page.wait_for_timeout(600)
            sizes = page.evaluate(
                """() => [...document.querySelectorAll('.ev__shot')]
                    .map(e => { const r = e.getBoundingClientRect();
                                return Math.round(r.width) + 'x' + Math.round(r.height); })""")
            assert len(set(sizes)) <= 1, sorted(set(sizes))
        finally:
            ctx.close()

    @pytest.mark.parametrize("width", WIDTHS)
    def test_a_missing_photo_stays_inside_its_own_box(self, browser, site, width):
        """.shot__fallback is position:absolute with inset:0. Without a
        positioned parent it lays itself out against the page - one row with
        no photo covered the entire feed in a grey rectangle."""
        ctx, page, _ = _page(browser, site, width, "light", view="feed")
        try:
            page.wait_for_timeout(600)
            worst = page.evaluate(
                """() => Math.max(0, ...[...document.querySelectorAll(
                    '.ev .shot__fallback')].map(e => e.getBoundingClientRect().width))""")
            assert worst <= 80, f"a fallback is {worst}px wide at {width}px"
        finally:
            ctx.close()

    @pytest.mark.parametrize("width", WIDTHS)
    def test_the_photo_never_sits_on_top_of_the_name(self, browser, site, width):
        ctx, page, _ = _page(browser, site, width, "light", view="feed")
        try:
            page.wait_for_timeout(600)
            overlaps = page.evaluate("""() => {
              const out = [];
              for (const ev of document.querySelectorAll('.ev')) {
                const shot = ev.querySelector('.ev__shot');
                const title = ev.querySelector('.ev__title');
                if (!shot || !title) continue;
                const a = shot.getBoundingClientRect(), b = title.getBoundingClientRect();
                if (b.left < a.right - 1 && b.top < a.bottom && b.bottom > a.top) {
                  out.push(Math.round(a.right - b.left));
                }
              }
              return out.slice(0, 3); }""")
            assert not overlaps, f"overlapping by {overlaps}px at {width}px"
        finally:
            ctx.close()


class TestTheFourKeysWorthHaving:
    """A shortcut nobody can discover is a shortcut nobody uses.

    Four, deliberately: get to a view, find a car, get out, see the list. A
    page with a dozen has none, because nobody learns a dozen.
    """

    def test_the_number_keys_switch_views(self, browser, site):
        ctx, page, errors = _page(browser, site, 1440, "light")
        try:
            for key, want in (("3", "market"), ("5", "status"), ("1", "feed")):
                page.keyboard.press(key)
                page.wait_for_timeout(250)
                assert want in page.evaluate("() => location.hash"), key
            assert not errors, errors
        finally:
            ctx.close()

    def test_slash_finds_a_car(self, browser, site):
        """And lands the cursor in the box, which needs the hash to settle
        first: go() sets location.hash, and the hashchange that follows is a
        separate task that routes again and puts focus back on <main>."""
        ctx, page, _ = _page(browser, site, 1440, "light")
        try:
            page.keyboard.press("/")
            page.wait_for_timeout(400)
            assert page.evaluate("() => (document.activeElement||{}).id") == "q"
        finally:
            ctx.close()

    def test_a_slash_typed_into_the_box_is_a_slash(self, browser, site):
        ctx, page, _ = _page(browser, site, 1440, "light")
        try:
            page.keyboard.press("/")
            page.wait_for_timeout(400)
            page.keyboard.type("bl/ue")
            page.wait_for_timeout(200)
            assert page.evaluate(
                "() => document.getElementById('q').value") == "bl/ue"
        finally:
            ctx.close()

    def test_a_number_typed_into_the_box_does_not_change_view(self, browser, site):
        ctx, page, _ = _page(browser, site, 1440, "light")
        try:
            page.keyboard.press("/")
            page.wait_for_timeout(400)
            page.keyboard.type("330")
            page.wait_for_timeout(250)
            assert "listings" in page.evaluate("() => location.hash")
            assert page.evaluate(
                "() => document.getElementById('q').value") == "330"
        finally:
            ctx.close()

    def test_escape_clears_what_you_typed(self, browser, site):
        ctx, page, _ = _page(browser, site, 1440, "light")
        try:
            page.keyboard.press("/")
            page.wait_for_timeout(400)
            page.keyboard.type("blue")
            page.wait_for_timeout(200)
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            assert page.evaluate("() => document.getElementById('q').value") == ""
        finally:
            ctx.close()

    def test_question_mark_shows_the_list_and_hides_it_again(self, browser, site):
        ctx, page, _ = _page(browser, site, 1440, "light")
        try:
            page.keyboard.press("?")
            page.wait_for_timeout(300)
            assert page.evaluate("() => !!document.getElementById('shortcuts')")
            rows = page.evaluate(
                "() => document.querySelectorAll('#shortcuts dt').length")
            assert rows == 4, f"{rows} shortcuts listed; four is the budget"
            page.keyboard.press("?")
            page.wait_for_timeout(300)
            assert not page.evaluate("() => !!document.getElementById('shortcuts')")
        finally:
            ctx.close()

    def test_every_shortcut_also_has_a_visible_control(self, browser, site):
        """It is an accelerator, not the only way to do something. The panel
        is hidden on a phone, where there is no keyboard to accelerate."""
        ctx, page, _ = _page(browser, site, 390, "light")
        try:
            page.keyboard.press("?")
            page.wait_for_timeout(300)
            shown = page.evaluate("""() => {
                const el = document.getElementById('shortcuts');
                return el ? getComputedStyle(el).display !== 'none' : false; }""")
            assert not shown, "the shortcut panel is showing on a phone"
            for view in ("feed", "listings", "market", "searches", "status"):
                assert page.evaluate(
                    "v => !!document.querySelector(`[data-view-link=\"${v}\"]`)",
                    view), view
        finally:
            ctx.close()


class TestThePageStatesItsOwnLimits:
    """Every figure on the Status tab is bounded by something, and the bounds
    were spread across a document, a commit message and four comments in the
    source - which is to say they were nowhere a person would find them at
    the moment they were deciding whether to believe a number.

    Built from the same values the tiles are built from, so it cannot
    describe a system other than this one.
    """

    def limits(self, page):
        return page.evaluate(
            "() => [...document.querySelectorAll('.limits li')].map(e => e.textContent)")

    def test_the_section_is_there_and_is_not_empty(self, browser, site):
        ctx, page, _ = _page(browser, site, 1440, "light", view="status")
        try:
            page.wait_for_timeout(300)
            lines = self.limits(page)
            assert len(lines) >= 2, lines
        finally:
            ctx.close()

    def test_it_says_the_allowance_figure_is_only_this_repository(self, browser, site):
        ctx, page, _ = _page(browser, site, 1440, "light", view="status")
        try:
            page.wait_for_timeout(300)
            said = " ".join(self.limits(page))
            assert "one meter per account" in said, said
        finally:
            ctx.close()

    def test_it_names_the_longest_gap_rather_than_describing_one(self, browser, site):
        """A generic caveat is furniture. This one carries the number."""
        ctx, page, _ = _page(browser, site, 1440, "light", view="status")
        try:
            page.wait_for_timeout(300)
            said = " ".join(self.limits(page))
            gap = page.evaluate(
                "() => (app.data.coverage || {}).longest_gap_minutes")
            if gap:
                assert "longest gap between checks" in said, said
                # However the page words a duration - "7.8 hours", "24
                # hours", "3 days" - the number in it is the gap's, not a
                # figure from somewhere else.
                shown = page.evaluate("h => hours(h)", gap / 60)
                assert shown in said, (shown, said)
        finally:
            ctx.close()

    def test_it_never_claims_a_car_sold(self, browser, site):
        ctx, page, _ = _page(browser, site, 1440, "light", view="status")
        try:
            page.wait_for_timeout(300)
            said = " ".join(self.limits(page))
            assert "stopped advertising" in said, said
        finally:
            ctx.close()

    def test_the_caveat_is_not_printed_twice(self, browser, site):
        """It used to sit under the tiles as well, which is how a caveat
        becomes furniture that nobody reads."""
        ctx, page, _ = _page(browser, site, 1440, "light", view="status")
        try:
            page.wait_for_timeout(300)
            body = page.evaluate(
                "() => document.querySelector('[data-view=\"status\"]').textContent")
            assert body.count("one meter per account") == 1, \
                "the blind spot appears more than once"
        finally:
            ctx.close()


class TestAPublishedFileFromBeforeTheFix:
    """The page is updated by a push; the data file is only rewritten by a
    check. Between the two - which at this schedule is hours - the new page
    is reading an old file, and it has to be right anyway.

    Found by restoring docs/data.json to what is actually published and
    looking at the page: "-2 of 7 checks complained" was back, along with
    "Requests last check 0" and "Check took 0s", because both fixes lived in
    the code that WRITES the file.
    """

    def old_shape(self, payload):
        """That file, as it was published before either fix."""
        import copy
        d = copy.deepcopy(payload)
        d.pop("last_check", None)
        d["coverage"] = dict(d["coverage"], complained=-2)
        d["runs"] = [
            {"at": "2026-01-14T19:34:27+00:00", "ok": True, "skipped": True,
             "searches_run": 0, "duration_s": 0.0, "requests_made": 0},
            {"at": "2026-01-14T19:19:40+00:00", "ok": True, "searches_run": 3,
             "searches_failed": 0, "duration_s": 58.7, "requests_made": 32},
        ]
        d["last_run"] = d["runs"][0]
        return d

    def page_with(self, browser, site, payload):
        """Routed BEFORE the first navigation.

        Routing after one and reloading does not work: the service worker is
        registered by then and answers data.json from its own cache, so the
        page reads the real fixture and the test passes for the wrong reason.
        It did, until the assertion printed what the page had actually loaded.
        """
        import json
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  color_scheme="light",
                                  service_workers="block")
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("**/data.json", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(self.old_shape(payload))))
        page.goto(site, wait_until="networkidle")
        page.wait_for_timeout(600)
        page.click('[data-view-link="status"]')
        page.wait_for_timeout(400)
        return ctx, page, errors

    def test_it_works_out_the_last_check_from_the_runs(self, browser, site, payload):
        ctx, page, errors = self.page_with(browser, site, payload)
        try:
            body = page.evaluate(
                "() => document.querySelector('[data-view=\"status\"]').textContent")
            # Not the timestamp: the browser renders it in its own zone.
            # These three are the figures that were 0, 0 and 0.
            assert "59s" in body, body[:400]
            assert "32" in body
            assert "a firing stood down" in body, body[:400]
            assert not errors, errors
        finally:
            ctx.close()

    def test_it_never_prints_a_negative_count(self, browser, site, payload):
        ctx, page, _ = self.page_with(browser, site, payload)
        try:
            body = page.evaluate(
                "() => document.querySelector('[data-view=\"status\"]').textContent")
            assert "-2 of" not in body, body[:400]
            assert "complained" not in body or "-" not in body.split("complained")[0][-8:]
        finally:
            ctx.close()


@pytest.mark.parametrize("width", (320, 390, 1440))
def test_the_clock_strip_is_the_same_height_whatever_it_says(browser, site, width):
    """The countdown changes its own words every minute, so its height must
    not depend on them.

    It did. At 390px "in 33 min" fitted on one line and "in 1h 58m" pushed
    "Next check" onto a second: 32.9px against 60.9px, a shift of the whole
    page each time the countdown crossed the hour. The load-shift test above
    caught it only when the clock happened to read a long value while it
    ran, so it failed at some times of day and passed at others. This
    measures every shape of text the strip can show, directly, and checks
    none of it is cut off either.
    """
    ctx, page, _ = _page(browser, site, width, "light")
    try:
        texts = [("—", "—"), ("just now", "in 2h"), ("2m ago", "in 1h 58m"),
                 ("1h ago", "in 33 min"), ("2h ago", "in 2:52"),
                 ("4h ago", "due 2h 20m ago"), ("May 3", "due 14h 5m ago"),
                 ("Not checked yet", "first one pending")]
        heights, cut = set(), []
        for last, nxt in texts:
            h, clipped = page.evaluate("""([a, b]) => {
                const L = document.getElementById('clock-last'),
                      N = document.getElementById('clock-next'),
                      E = document.getElementById('clock-every');
                L.textContent = a; N.textContent = b; E.textContent = 'every 2 hours';
                return [document.getElementById('clock').getBoundingClientRect().height,
                        [L, N, E].filter(e => e.scrollWidth > e.clientWidth + 1)
                                 .map(e => e.textContent)];
            }""", [last, nxt])
            heights.add(round(h, 1))
            cut += clipped
        assert len(heights) == 1, f"the strip changes height with its text: {sorted(heights)}"
        assert not cut, f"cut off at {width}px: {cut}"
    finally:
        ctx.close()


class TestTheStatusTabSaysOnlyWhatCanBeActedOn:
    """A switched-off channel with no credentials set has nothing to switch
    back on; a thin schedule with no outside timer has one obvious fix."""

    def status(self, browser, site, payload):
        import json
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  service_workers="block")
        page = ctx.new_page()
        page.route("**/data.json", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(payload)))
        page.goto(site + "#/status", wait_until="networkidle")
        page.wait_for_selector("text=Alerts")
        return ctx, page

    def retired(self, payload, missing):
        import copy
        d = copy.deepcopy(payload)
        d["channels"]["email"] = dict(d["channels"].get("email") or {},
                                      label="Email (Gmail)", active=False,
                                      missing=missing,
                                      disabled_reason="the credentials were rejected")
        return d

    def test_a_retired_channel_without_credentials_is_not_listed(
            self, browser, site, payload):
        ctx, page = self.status(browser, site, self.retired(payload, ["GMAIL_USER"]))
        assert "credentials were rejected" not in page.inner_text("main")
        ctx.close()

    def test_a_retired_channel_with_credentials_says_why(self, browser, site, payload):
        ctx, page = self.status(browser, site, self.retired(payload, []))
        assert "credentials were rejected" in page.inner_text("main")
        ctx.close()

    def test_the_coverage_alarm_points_at_an_outside_timer_until_there_is_one(
            self, browser, site, payload):
        ctx, page = self.status(browser, site, payload)
        assert page.evaluate("hasOutsideTimer({by_trigger: {schedule: 3}})") is False
        assert page.evaluate(
            "hasOutsideTimer({by_trigger: {schedule: 1, 'repository_dispatch:cron-job': 3}})") is True
        ctx.close()
