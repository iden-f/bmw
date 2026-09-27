"""A real browser, signed in to Facebook, that keeps what each page received.

It opens the same search a person would and keeps the page's own data: the
results the page arrived with, and the API replies it fetched while being
scrolled. Nothing is replayed or forged, so a change to Facebook's internal
calls changes nothing here; only a change to the shape of what a listing
looks like would, and that is read in autotrader/marketplace.py.

The browser has its own profile, used for nothing else. Signing in to it is
the one thing done by hand (collector/run login).
"""

from __future__ import annotations

import random
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

from . import settings as S

# What a page arriving somewhere other than where it was sent means.
_SIGNED_OUT = ("/login", "login.php")
_CHECKPOINT = ("/checkpoint",)


class Page:
    """What one visit produced."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.landed = ""
        self.status = 0
        self.texts: list[str] = []
        self.session = "ok"
        self.error = ""


def _where(url: str) -> str:
    """ "https://host/a/b?c" -> "/a/b": enough to see a redirect, and no query."""
    return urlsplit(url).path


class Browser:
    def __init__(self, cfg: S.Settings, *, visible: bool | None = None) -> None:
        self.cfg = cfg
        self.visible = cfg.show_browser if visible is None else visible
        self.profile = S.home() / "browser"

    @contextmanager
    def open(self) -> Iterator["Browser"]:
        from playwright.sync_api import sync_playwright
        self.profile.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            kwargs = dict(headless=not self.visible, locale="en-CA",
                          viewport={"width": 1280, "height": 900},
                          args=["--disable-blink-features=AutomationControlled"])
            if self.cfg.channel:
                kwargs["channel"] = self.cfg.channel
            path = self.cfg.extra.get("executable_path")
            if path:
                kwargs["executable_path"] = path
            try:
                self.context = pw.chromium.launch_persistent_context(
                    str(self.profile), **kwargs)
            except Exception:
                if not self.cfg.channel:
                    raise
                # No Google Chrome here: Playwright's own Chromium will do.
                kwargs.pop("channel", None)
                self.context = pw.chromium.launch_persistent_context(
                    str(self.profile), **kwargs)
            try:
                yield self
            finally:
                self.context.close()

    def signed_in(self) -> bool:
        """Whether the profile holds a Facebook session at all."""
        return any(c.get("name") == "c_user"
                   for c in self.context.cookies(self.cfg.facebook))

    def _pause(self, low: float, high: float, page=None) -> None:
        seconds = random.uniform(low, high) * float(self.cfg.extra.get("pace", 1.0))
        if page is not None:
            # Playwright's own wait, so the page's replies keep arriving.
            page.wait_for_timeout(seconds * 1000)
        else:
            time.sleep(seconds)

    def _settle(self, page, replies: list, *, quiet_ms: int = 1200,
                limit_ms: int = 8000) -> None:
        """Wait until the page has stopped fetching results, within reason."""
        waited, seen = 0, -1
        while waited < limit_ms:
            if len(replies) == seen:
                break
            seen = len(replies)
            page.wait_for_timeout(quiet_ms)
            waited += quiet_ms

    def visit(self, url: str, *, scrolls: int = 0) -> Page:
        """Open ``url`` as a person would, and keep what the page received."""
        url = url.replace("https://www.facebook.com", self.cfg.facebook.rstrip("/"))
        out = Page(url)
        page = self.context.new_page()
        replies = []

        def keep(response) -> None:
            if "/api/graphql" in response.url:
                replies.append(response)

        page.on("response", keep)
        try:
            first = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            out.status = first.status if first else 0
            if first is not None:
                try:
                    out.texts.append(first.text())
                except Exception:        # noqa: BLE001 - a body can vanish
                    pass
            self._pause(2.0, 4.0, page)
            for _ in range(max(0, scrolls)):
                page.mouse.wheel(0, random.randint(2400, 4200))
                self._pause(1.5, 3.5, page)
                self._settle(page, replies)
            out.landed = page.url
            where = _where(out.landed)
            if any(mark in where for mark in _CHECKPOINT):
                out.session = "checkpoint"
            elif any(mark in where for mark in _SIGNED_OUT) or not self.signed_in():
                out.session = "signed_out"
            for response in replies:
                try:
                    out.texts.append(response.text())
                except Exception:        # noqa: BLE001
                    continue
        except Exception as exc:         # noqa: BLE001 - one page never ends a cycle
            out.error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
        finally:
            page.close()
        return out

    def rest(self) -> None:
        """Between pages, the pause a person takes."""
        self._pause(6.0, 15.0)


def login(cfg: S.Settings) -> bool:
    """Open a window on Facebook for the owner to sign in, and wait.

    Closing the window ends it. Returns whether a session is now held.
    """
    browser = Browser(cfg, visible=True)
    with browser.open():
        page = browser.context.pages[0] if browser.context.pages \
            else browser.context.new_page()
        page.goto(f"{cfg.facebook.rstrip('/')}/marketplace/", wait_until="domcontentloaded")
        print("A browser window is open. Sign in to Facebook there, then open\n"
              "Marketplace and set its location to where you want to search.\n"
              "Close the window when you are done.")
        try:
            while browser.context.pages:
                time.sleep(1)
                if browser.signed_in() and not getattr(login, "_said", False):
                    print("Signed in. Close the window when you are done.")
                    login._said = True    # type: ignore[attr-defined]
        except KeyboardInterrupt:
            pass
        return browser.signed_in()


def profile_exists() -> bool:
    return (S.home() / "browser").is_dir()


def forget_profile() -> Path:
    import shutil
    path = S.home() / "browser"
    shutil.rmtree(path, ignore_errors=True)
    return path
