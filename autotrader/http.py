"""HTTP fetching with retries, polite pacing and honest block detection.

A failure is reported rather than fatal, so one bad response never ends a run
before state is saved.
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

# A plain, current desktop Chrome UA rather than one that announces a scraper.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9,fr-CA;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}

# Phrases that only appear on an actual interstitial.  Matching bare words like
# "captcha" or "cloudflare" gives false positives, because ordinary pages load
# reCAPTCHA and CDN scripts.
BLOCK_MARKERS = (
    "request unsuccessful. incapsula incident",
    "incapsula incident id",
    "_incapsula_resource",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
    "attention required! | cloudflare",
    "please verify you are a human",
    "access denied</title>",
    "pardon our interruption",
    "px-captcha",
    "unusual traffic from your computer",
)


class FetchError(RuntimeError):
    """A URL could not be fetched after retrying."""


class BlockedError(FetchError):
    """The site served an anti-bot interstitial instead of the page."""


class BudgetExhausted(FetchError):
    """The run has spent its allowance of HTTP requests."""


@dataclass
class Response:
    url: str
    status: int
    text: str
    elapsed_ms: int
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def looks_blocked(text: str, status: int = 200) -> bool:
    """True if this response is an anti-bot page rather than real content."""
    if status in {401, 403, 429}:
        return True
    head = text[:20000].lower()
    if any(marker in head for marker in BLOCK_MARKERS):
        return True
    # A real autotrader.ca page is hundreds of KB. A tiny HTML body with a
    # meta-refresh is the classic "please wait" bounce page.
    if len(text) < 3000 and re.search(r"http-equiv=[\"']?refresh", head):
        return True
    return False


class Fetcher:
    """A small, well-behaved HTTP client shared across a run."""

    def __init__(
        self,
        *,
        timeout: int = 30,
        retries: int = 3,
        delay_ms: int = 1200,
        user_agent: str = "auto",
        budget: int = 0,
        session: requests.Session | None = None,
    ) -> None:
        self.timeout = max(5, int(timeout))
        self.retries = max(0, int(retries))
        self.delay_ms = max(0, int(delay_ms))
        self.user_agent = DEFAULT_USER_AGENT if user_agent in ("", "auto", None) else user_agent
        # A hard ceiling on requests per run, so a bad config (many searches x
        # many pages x detail lookups) cannot become a crawl of someone else's
        # site.
        self.budget = max(0, int(budget))
        self.session = session or requests.Session()
        self.session.headers.update({**BASE_HEADERS, "User-Agent": self.user_agent})
        self._last_request_at = 0.0
        self.spent = 0
        self.stats = {"requests": 0, "retries": 0, "failures": 0, "blocked": 0,
                      "budget": self.budget, "spent": 0}

    @property
    def budget_left(self) -> int:
        return max(0, self.budget - self.spent) if self.budget else 1_000_000

    def _spend(self) -> None:
        """Count a request against the budget, refusing once it runs out.

        Retries count too: three attempts at one URL are three requests as far
        as the site is concerned.
        """
        if self.budget and self.spent >= self.budget:
            raise BudgetExhausted(
                f"this run has used its allowance of {self.budget} requests. "
                "Raise scraping.request_budget, or lower max_pages/enrich_limit.")
        self.spent += 1
        self.stats["spent"] = self.spent

    # ------------------------------------------------------------------

    def _pace(self) -> None:
        """Space requests out, with jitter, so we look like a person browsing."""
        if not self.delay_ms:
            return
        wait = (self.delay_ms / 1000.0) - (time.monotonic() - self._last_request_at)
        wait += random.uniform(0, self.delay_ms / 4000.0)
        if wait > 0:
            time.sleep(wait)

    def get(self, url: str, *, referer: str | None = None,
            allow_block: bool = False) -> Response:
        """Fetch ``url``.  Raises FetchError/BlockedError once retries run out."""
        headers = {}
        if referer:
            headers["Referer"] = referer
            headers["Sec-Fetch-Site"] = "same-origin"
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            if attempt:
                # Exponential backoff with jitter, capped so a run cannot hang.
                backoff = min(30.0, 2.0 ** attempt) + random.uniform(0, 1.5)
                log.info("retry %d/%d for %s in %.1fs", attempt, self.retries, url, backoff)
                self.stats["retries"] += 1
                time.sleep(backoff)
            self._spend()
            self._pace()
            started = time.monotonic()
            try:
                raw = self.session.get(url, timeout=self.timeout, headers=headers,
                                       allow_redirects=True)
            except requests.RequestException as exc:
                last_error = exc
                self._last_request_at = time.monotonic()
                continue
            self._last_request_at = time.monotonic()
            self.stats["requests"] += 1
            elapsed = int((time.monotonic() - started) * 1000)
            text = raw.text or ""

            if raw.status_code in RETRY_STATUS:
                last_error = FetchError(f"HTTP {raw.status_code} from {url}")
                continue
            if not (200 <= raw.status_code < 300):
                last_error = FetchError(f"HTTP {raw.status_code} from {url}")
                break  # 404 and friends will not improve on retry.
            if looks_blocked(text, raw.status_code) and not allow_block:
                self.stats["blocked"] += 1
                last_error = BlockedError(
                    f"autotrader.ca served an anti-bot page for {url}. "
                    "This usually clears on its own; if it persists, slow the "
                    "bot down (scraping.delay_ms) or run it less often."
                )
                continue
            return Response(url=raw.url, status=raw.status_code, text=text,
                            elapsed_ms=elapsed)

        self.stats["failures"] += 1
        if isinstance(last_error, FetchError):
            raise last_error
        raise FetchError(f"Could not fetch {url}: {last_error}")

    def get_bytes(self, url: str, *, referer: str | None = None) -> bytes | None:
        """Fetch a binary asset.  Returns None instead of raising."""
        try:
            self._spend()
            self._pace()
            headers = {"Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                       "Sec-Fetch-Dest": "image", "Sec-Fetch-Mode": "no-cors"}
            if referer:
                headers["Referer"] = referer
            raw = self.session.get(url, timeout=self.timeout, headers=headers)
            self._last_request_at = time.monotonic()
            self.stats["requests"] += 1
            if raw.ok and raw.content:
                return raw.content
        except BudgetExhausted:
            log.info("skipping asset %s: request budget spent", url)
        except requests.RequestException as exc:
            log.debug("asset fetch failed for %s: %s", url, exc)
        return None

    def get_asset(self, url: str, *, referer: str | None = None) -> dict[str, Any]:
        """Fetch a binary asset and say what came back.

        Unlike get_bytes, the result says why a fetch failed (an HTTP status,
        a non-image content type, a spent budget), since each needs a
        different fix. Never raises; always returns a dict with at least
        ``error`` or ``content``.
        """
        out: dict[str, Any] = {"url": url}
        try:
            self._spend()
            self._pace()
            headers = {"Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                       "Sec-Fetch-Dest": "image", "Sec-Fetch-Mode": "no-cors"}
            if referer:
                headers["Referer"] = referer
            raw = self.session.get(url, timeout=self.timeout, headers=headers)
            self._last_request_at = time.monotonic()
            self.stats["requests"] += 1
            out["status"] = raw.status_code
            out["type"] = str(raw.headers.get("Content-Type") or "").split(";")[0].strip()
            out["content"] = raw.content if raw.ok else b""
            if not raw.ok:
                out["error"] = f"HTTP {raw.status_code}"
        except BudgetExhausted:
            out["error"] = "request budget spent"
        except requests.RequestException as exc:
            out["error"] = str(exc)[:140]
        return out

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # noqa: BLE001 - closing must never break a run
            pass
