"""The published site, locked: what a stranger gets, and what the owner gets.

Built with the bot's own vault code and opened in a real browser, so this is
the proof that the two halves agree - Python seals, WebCrypto opens, and the
other way round for a change sent from the page.
"""
from __future__ import annotations

import base64
import json
import shutil
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from autotrader import vault as V

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

DOCS = Path(__file__).resolve().parent.parent / "docs"
PHOTO = Path(__file__).resolve().parent / "fixtures" / "photo.webp"
PHRASE = "a long enough test passphrase"
TITLE = "2018 Example Coupe"


def _browser_path() -> str | None:
    found = sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome"))
    return str(found[0]) if found else None


class _Quiet(SimpleHTTPRequestHandler):
    requested: list[str] = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).requested.append(self.path)
        super().do_GET()


@pytest.fixture(scope="module")
def private_site(tmp_path_factory):
    from autotrader.config import Config
    from autotrader.dashboard import build_payload
    from autotrader.listing import Listing
    from autotrader.state import State

    root = tmp_path_factory.mktemp("repo")
    cfg = Config.defaults(root / "config.json")
    search = cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?prx=-2",
                            "Example search")
    cfg.save()
    state = State(path=root / "state.json")
    state.record(Listing(id="abc-1", url="https://www.autotrader.ca/offers/abc-1",
                         title=TITLE, price=41000, price_source="detail",
                         search_id=search.id, location="Somewhere", province="ON",
                         year=2018))
    state.record_run({"ok": True, "searches": 1, "listings": 1})
    payload = build_payload(cfg, state, {"GITHUB_REPOSITORY": "someone/watch"})
    payload["listings"][0]["thumb"] = "thumbs/abc-1.webp"
    payload["listings"][0]["thumbs"] = ["thumbs/abc-1.webp"]

    docs = root / "docs"
    shutil.copytree(DOCS, docs, ignore=shutil.ignore_patterns(
        "data.json", "events.json", "thumbs"))
    (docs / "data.json").write_text(json.dumps(payload), encoding="utf-8")
    (docs / "thumbs").mkdir()
    shutil.copy(PHOTO, docs / "thumbs" / "abc-1.webp")

    vault = V.Vault.unlock(root, {V.ENV_KEY: PHRASE}, create=True)
    out = root / "site"
    vault.publish(docs, out)

    _Quiet.requested = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(out)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield {"url": f"http://127.0.0.1:{server.server_port}/index.html",
               "vault": vault, "out": out}
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


# Everything the page keeps in localStorage, by key.
STORED = "Object.keys(localStorage).filter(k => k.startsWith('atw:'))"


def _unlock(page, phrase=PHRASE, keep=True):
    page.fill("#lock-pass", phrase)
    if not keep:
        page.uncheck("#lock-keep")
    page.click("#lock-go")


class TestAStranger:

    def test_sees_a_lock_and_nothing_else(self, browser, private_site):
        _Quiet.requested = []
        ctx = browser.new_context(service_workers="block")
        page = ctx.new_page()
        page.goto(private_site["url"])
        page.wait_for_selector("#lock:not([hidden])")
        text = page.inner_text("body")
        assert TITLE not in text and "Example search" not in text
        assert page.is_hidden("#tabs")
        # Nothing but the page and the lock was even asked for.
        assert not any("data.enc" in p or "/thumbs/" in p for p in _Quiet.requested)
        ctx.close()

    def test_a_wrong_passphrase_opens_nothing(self, browser, private_site):
        ctx = browser.new_context(service_workers="block")
        page = ctx.new_page()
        page.goto(private_site["url"])
        _unlock(page, "not the right passphrase at all")
        page.wait_for_selector("#lock-msg:not(:empty)", timeout=20000)
        assert page.is_visible("#lock")
        assert TITLE not in page.inner_text("body")
        assert page.evaluate(STORED) == []
        ctx.close()

    def test_the_published_files_hold_no_plaintext(self, private_site):
        for path in private_site["out"].rglob("*"):
            if path.is_file():
                blob = path.read_bytes()
                assert TITLE.encode() not in blob, path
                assert b"abc-1" not in blob, path
                assert b"Example search" not in blob, path


class TestTheOwner:

    def test_unlocks_and_sees_the_cars_and_photos(self, browser, private_site):
        ctx = browser.new_context(service_workers="block")
        page = ctx.new_page()
        page.goto(private_site["url"])
        _unlock(page)
        page.wait_for_selector(f"text={TITLE}", timeout=30000)
        page.wait_for_function(
            "[...document.querySelectorAll('main img')]"
            ".some(i => i.src.startsWith('blob:') && i.naturalWidth > 0)", timeout=20000)
        page.goto(private_site["url"] + "#/listings")
        page.wait_for_selector(".card", timeout=30000)
        ctx.close()

    def test_stays_unlocked_on_this_device_until_locked(self, browser, private_site):
        ctx = browser.new_context(service_workers="block")
        page = ctx.new_page()
        page.goto(private_site["url"])
        _unlock(page)
        page.wait_for_selector(f"text={TITLE}", timeout=30000)
        page.reload()
        page.wait_for_selector(f"text={TITLE}", timeout=30000)
        assert page.is_hidden("#lock")
        page.click("#lock-now")
        page.wait_for_selector("#lock:not([hidden])")
        assert page.evaluate(STORED) == []
        ctx.close()

    def test_not_keeping_the_key_means_asking_again(self, browser, private_site):
        ctx = browser.new_context(service_workers="block")
        page = ctx.new_page()
        page.goto(private_site["url"])
        _unlock(page, keep=False)
        page.wait_for_selector(f"text={TITLE}", timeout=30000)
        page.reload()
        page.wait_for_selector("#lock:not([hidden])")
        ctx.close()

    def test_not_keeping_the_key_leaves_nothing_behind(self, browser, private_site):
        """On a shared device, nothing about the cars may outlive the tab."""
        ctx = browser.new_context(service_workers="block")
        page = ctx.new_page()
        page.goto(private_site["url"])
        _unlock(page, keep=False)
        page.wait_for_selector(f"text={TITLE}", timeout=30000)
        page.goto(private_site["url"] + "#/listings")
        page.wait_for_selector(".card", timeout=30000)
        assert page.evaluate(STORED) == []
        ctx.close()

    def test_locking_one_tab_locks_the_others(self, browser, private_site):
        ctx = browser.new_context(service_workers="block")
        first, second = ctx.new_page(), ctx.new_page()
        first.goto(private_site["url"])
        _unlock(first)
        first.wait_for_selector(f"text={TITLE}", timeout=30000)
        second.goto(private_site["url"])
        second.wait_for_selector(f"text={TITLE}", timeout=30000)
        first.click("#lock-now")
        second.wait_for_selector("#lock:not([hidden])", timeout=20000)
        assert TITLE not in second.inner_text("body")
        ctx.close()

    def test_a_change_from_the_page_is_sealed_and_the_bot_can_read_it(
            self, browser, private_site):
        ctx = browser.new_context(service_workers="block")
        page = ctx.new_page()
        page.goto(private_site["url"])
        _unlock(page)
        page.wait_for_selector(f"text={TITLE}", timeout=30000)
        url = page.evaluate(
            "askUrl('Shortlist: " + TITLE + "', [{action: 'shortlist', listing: 'abc-1'}])")
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        assert parsed.path == "/someone/watch/new/main"
        filename = query["filename"][0]
        assert filename.startswith("control/") and filename.endswith(".enc")
        # Neither the file name nor the commit says what the change is.
        for field in (filename, query["message"][0]):
            assert "abc-1" not in field and "Example" not in field and "hortlist" not in field
        blob = base64.b64decode(query["value"][0])
        plain = V.unseal(private_site["vault"].key, blob, "control")
        assert len(plain) % 1024 == 0, "padded to a whole kilobyte"
        envelope = json.loads(plain)
        assert envelope["changes"] == [{"action": "shortlist", "listing": "abc-1"}]
        assert len(envelope["id"]) == 32 and envelope["at"].endswith("Z")
        ctx.close()
