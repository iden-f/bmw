"""The Feed tab's unread count, read on the tab it counts for.

The count wears the new-car colour so it stands out. The rule that greys a
badge on the current tab is more specific, so on the Feed tab itself, which
is where the owner goes to read what is new, the number turned grey on that
colour and could not be read: a contrast of about 1.1 to 1.
"""
from __future__ import annotations

import json

import pytest

from .test_dashboard_layout import CONTRAST_JS
from .test_dashboard_layout import browser, payload, site  # noqa: F401 - fixtures


def _open_with_unread(browser, site, payload, route, scheme, width):
    ctx = browser.new_context(viewport={"width": width, "height": 900},
                              color_scheme=scheme, service_workers="block")
    # Seen long ago, so every event in the demo data counts as unread.
    ctx.add_init_script("""
      const ns = `atw:${location.pathname.replace(/[^/]*$/, '')}:`;
      localStorage.setItem(ns + 'lastSeen', JSON.stringify('2000-01-01T00:00:00Z'));
    """)
    page = ctx.new_page()
    page.route("**/data.json", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(payload)))
    page.goto(site + route, wait_until="networkidle")
    return ctx, page


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", [375, 1280])
@pytest.mark.parametrize("route", ["#/feed", "#/listings"])
def test_the_unread_count_can_be_read_on_every_tab(browser, site, payload, route, scheme, width):
    if not payload.get("events"):
        pytest.skip("the demo data has no events to be unread")
    ctx, page = _open_with_unread(browser, site, payload, route, scheme, width)
    try:
        badge = page.locator('.tab__n[data-unread="1"]').first
        badge.wait_for()
        assert int(badge.text_content()) > 0
        bad = [b for b in page.evaluate(CONTRAST_JS) if "tab__n" in b["cls"]]
        assert not bad, bad
    finally:
        ctx.close()
