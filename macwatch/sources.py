"""Source adapters: each returns observations and touches nothing else.

AutoTrader is read over HTTP with the bot's own fetcher, parser and
enrichment. Marketplace is read through the collector's signed-in browser,
sharing its profile and its lock, so this service and the existing
collector take turns rather than opening the profile twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from autotrader import clock
from autotrader import marketplace as M
from autotrader.enrich import detail_from_html, enrich
from autotrader.http import BlockedError, BudgetExhausted, FetchError, Fetcher
from autotrader.listing import Listing
from autotrader.runner import scrape_search
from autotrader.urls import listing_id_from_url

from . import ingest
from .db import Database

log = logging.getLogger("macwatch.sources")


@dataclass
class PassReport:
    site: str
    started_at: str = field(default_factory=clock.stamp)
    runs: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    session: str = "ok"

    @property
    def ok(self) -> bool:
        return not self.error and self.session == "ok" and \
            all(r.get("ok") for r in self.runs)

    def line(self) -> str:
        if self.error:
            return f"{self.site}: {self.error}"
        if self.session != "ok":
            return f"{self.site}: Facebook session {self.session}"
        good = sum(1 for r in self.runs if r.get("ok"))
        return (f"{self.site}: read {good} of {len(self.runs)} searches, "
                + "; ".join(f"{r['name']}: {r['summary']}" for r in self.runs))


def _fetcher(cfg) -> Fetcher:
    scraping = cfg.get("scraping", {}) or {}
    return Fetcher(timeout=int(scraping.get("timeout_seconds", 30) or 30),
                   retries=int(scraping.get("retries", 3) or 3),
                   delay_ms=int(scraping.get("delay_ms", 1200) or 1200),
                   user_agent=str(scraping.get("user_agent") or "auto"),
                   budget=int(scraping.get("request_budget", 250) or 250),
                   run_seconds=float(scraping.get("run_seconds", 720) or 720))


# ------------------------------------------------------------ AutoTrader

def autotrader_pass(db: Database, cfg, settings, *, trigger: str = "schedule",
                    fetcher: Fetcher | None = None) -> PassReport:
    """Read every active search on AutoTrader and take the results in."""
    report = PassReport("autotrader")
    fetcher = fetcher or _fetcher(cfg)
    scraping = cfg.get("scraping", {}) or {}
    enrich_cap = int(scraping.get("enrich_limit", 25) or 25) \
        if scraping.get("enrich_details", True) else 0
    cols = {c.id: c for c in ingest.sync_collections(db, cfg)}
    blocked = False
    try:
        for search in cfg.active_searches:
            collection = cols.get(search.id)
            if collection is None or not collection.enabled:
                continue
            run = ingest.start_run(db, "autotrader", collection.id, trigger)
            entry: dict[str, Any] = {"name": search.name, "ok": False, "summary": ""}
            report.runs.append(entry)
            if blocked:
                ingest.end_run(db, run, ok=False, error="skipped: the site turned an "
                               "earlier search away this pass")
                entry["summary"] = "skipped after an anti-bot page"
                continue
            try:
                result = scrape_search(search, cfg, fetcher)
            except BlockedError as exc:
                blocked = True
                ingest.end_run(db, run, ok=False, error=str(exc), requests=fetcher.spent)
                entry["summary"] = "anti-bot page"
                continue
            except (FetchError, BudgetExhausted) as exc:
                ingest.end_run(db, run, ok=False, error=str(exc), requests=fetcher.spent)
                entry["summary"] = f"failed: {exc}"[:160]
                continue
            listings = list(result.listings)
            # A new car's own page, for the exact price, odometer and photos.
            known = {r["id"] for r in db.q(
                "SELECT id FROM listings WHERE id IN (%s)" % ",".join("?" * len(listings)),
                [l.id for l in listings])} if listings else set()
            fresh = [l for l in listings if l.id not in known]
            if enrich_cap and fresh:
                _enrich(fresh[:enrich_cap], fetcher)
            summary = ingest.ingest(db, run, collection, listings,
                                    complete=result.complete, confined=result.confined,
                                    settings=settings, cfg=cfg)
            ingest.end_run(db, run, ok=True, listings=len(listings), fresh=summary.fresh,
                           complete=result.complete, confined=result.confined,
                           requests=fetcher.spent, strategy=result.strategy,
                           error=result.partial_error or "", note=summary.line())
            entry.update(ok=True, summary=summary.line(), fresh=summary.fresh,
                         queued=summary.queued)
        _pinned_pass(db, cfg, settings, fetcher, cols.get("pinned"), trigger, report)
    except Exception as exc:  # noqa: BLE001 - the pass reports, the service lives
        log.exception("autotrader pass failed")
        report.error = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        fetcher.close()
    return report


def _enrich(listings: list[Listing], fetcher: Fetcher) -> None:
    for listing in listings:
        try:
            response = fetcher.get(listing.url, referer=listing.url)
            enrich(listing, response.text)
        except BudgetExhausted:
            break
        except (FetchError, BlockedError) as exc:
            log.info("could not enrich %s: %s", listing.id, exc)
        except Exception as exc:  # noqa: BLE001
            log.info("enrichment error on %s: %s", listing.id, exc)


def _pinned_pass(db: Database, cfg, settings, fetcher: Fetcher, collection, trigger: str,
                 report: PassReport) -> None:
    """Listing links the owner pasted: each one's page, read on its own."""
    if collection is None or not collection.enabled:
        return
    urls = [u for u in (collection.rules or {}).get("urls", []) if "autotrader.ca" in u]
    if not urls:
        return
    run = ingest.start_run(db, "autotrader", collection.id, trigger)
    entry: dict[str, Any] = {"name": collection.name, "ok": False, "summary": ""}
    report.runs.append(entry)
    found: list[Listing] = []
    errors: list[str] = []
    for url in urls:
        lid = listing_id_from_url(url)
        if not lid:
            errors.append(f"not a listing link: {url[:80]}")
            continue
        try:
            response = fetcher.get(url, referer=url)
        except (FetchError, BlockedError, BudgetExhausted) as exc:
            if getattr(exc, "status", None) == 404:
                continue          # gone: the removal pass below records it
            errors.append(str(exc)[:120])
            continue
        listing = detail_from_html(response.text, listing_id=lid, url=response.url)
        if listing is None:
            errors.append(f"could not read {lid}")
            continue
        listing.id = listing.id or lid
        listing.search_id, listing.search_name = collection.id, collection.name
        found.append(listing)
    summary = ingest.ingest(db, run, collection, found, complete=not errors, confined=True,
                            settings=settings, cfg=cfg)
    ingest.end_run(db, run, ok=not errors, listings=len(found), fresh=summary.fresh,
                   complete=not errors, confined=True, requests=fetcher.spent,
                   error="; ".join(errors), note=summary.line())
    entry.update(ok=not errors, summary=summary.line())


# ----------------------------------------------------------- Marketplace

def marketplace_pass(db: Database, cfg, settings, *, trigger: str = "schedule",
                     browser_factory: Callable[..., Any] | None = None) -> PassReport:
    """Read every search on Marketplace through the collector's browser.

    ``browser_factory`` is the collector's ``Browser`` class unless a test
    supplies something that answers ``open()``, ``signed_in()``, ``visit()``
    and ``rest()`` the same way.
    """
    report = PassReport("marketplace")
    try:
        plan = M.plan(cfg)
    except Exception as exc:  # noqa: BLE001
        report.error = f"could not plan the Marketplace searches: {exc}"[:300]
        return report
    if not plan:
        return report
    cols = {c.id: c for c in ingest.sync_collections(db, cfg)}
    by_id = {s.id: s for s in cfg.active_searches}
    if browser_factory is None:
        from collector import settings as CS
        from collector.browser import Browser
        collector_settings = CS.Settings.load()
        collector_settings.scrolls = int(settings.scrolls)

        def browser_factory() -> Any:
            return Browser(collector_settings)
    try:
        with browser_factory().open() as browser:
            if not browser.signed_in():
                report.session = "signed_out"
                return report
            opened = 0
            worth: list[tuple[Listing, Any, Any]] = []
            for item in plan:
                search = by_id.get(item["search"])
                collection = cols.get(item["search"])
                if search is None or collection is None or not collection.enabled:
                    continue
                run = ingest.start_run(db, "marketplace", collection.id, trigger)
                entry: dict[str, Any] = {"name": search.name, "ok": False, "summary": ""}
                report.runs.append(entry)
                texts: list[str] = []
                errors: list[str] = []
                for q in item["queries"]:
                    if opened:
                        browser.rest()
                    opened += 1
                    page = browser.visit(q["url"], scrolls=int(settings.scrolls))
                    texts += page.texts
                    if page.error:
                        errors.append(page.error)
                    if page.session != "ok":
                        report.session = page.session
                        break
                if report.session != "ok":
                    ingest.end_run(db, run, ok=False, error=f"session {report.session}")
                    entry["summary"] = f"Facebook session {report.session}"
                    break
                records = M.collect(texts)
                if not records and not errors:
                    errors.append("the page loaded but no listings could be read from it"
                                  if any(texts) else "the page came back empty")
                listings: list[Listing] = []
                for rec in records.values():
                    listing = M.to_listing(rec, search, make=item.get("make", ""),
                                           models=item.get("models") or (),
                                           aliases=item.get("aliases") or ())
                    if rec.get("sold"):
                        listing.source = "marketplace-sold"
                    listings.append(listing)
                    if not rec.get("sold"):
                        worth.append((listing, rec, collection))
                if errors and not records:
                    ingest.end_run(db, run, ok=False, error="; ".join(errors)[:300])
                    entry["summary"] = errors[0][:160]
                    continue
                # Cars worth their own page: new to the archive and within the rules.
                known = {r["id"] for r in db.q(
                    "SELECT id FROM listings WHERE id IN (%s)" % ",".join("?" * len(listings)),
                    [l.id for l in listings])} if listings else set()
                fresh_worth = [(l, rec) for l, rec, c in worth
                               if c.id == collection.id and l.id not in known
                               and _within(l, collection)]
                for listing, rec in fresh_worth[:max(0, int(settings.details_per_cycle))]:
                    browser.rest()
                    page = browser.visit(M.ITEM_URL.format(rec["id"]))
                    if page.session != "ok":
                        report.session = page.session
                        break
                    extra = M.collect(page.texts).get(rec["id"])
                    if extra:
                        if extra.get("mileage_rounded"):
                            extra.pop("mileage_km", None)
                        M.merge(rec, extra)
                        richer = M.to_listing(rec, search, make=item.get("make", ""),
                                              models=item.get("models") or (),
                                              aliases=item.get("aliases") or ())
                        listing.merge(richer)
                        listing.enriched = True
                summary = ingest.ingest(db, run, collection, listings, complete=False,
                                        confined=False, settings=settings, cfg=cfg)
                ingest.end_run(db, run, ok=True, listings=len(listings), fresh=summary.fresh,
                               error="; ".join(errors)[:300], note=summary.line())
                entry.update(ok=True, summary=summary.line(), fresh=summary.fresh,
                             queued=summary.queued)
                if report.session != "ok":
                    break
    except Exception as exc:  # noqa: BLE001
        log.exception("marketplace pass failed")
        report.error = f"{type(exc).__name__}: {exc}"[:300]
    return report


def _within(listing: Listing, collection) -> bool:
    from autotrader import filters
    return filters.check(listing, (collection.rules or {}).get("filters") or {}).keep
