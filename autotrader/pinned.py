"""Listings you follow by their link rather than through a search.

Paste one car's address and the bot keeps its record: the facts, every
asking price it observes, its photos, and whether it is still for sale. By
default it does so quietly ("archive"); a pin set to "alert" announces its
price moves and its going like any watched car.

An AutoTrader listing is read on its own page, a few per check, oldest
check first. A Marketplace listing can only be read by the collector on the
Mac, so it is recorded as awaiting that, and filled in the moment a batch
carries the car.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from . import clock
from .config import PINNED_SEARCH_ID, PINNED_SEARCH_NAME, Config
from .enrich import detail_from_html, page_identifies
from .http import BlockedError, BudgetExhausted, FetchError, looks_blocked
from .listing import Listing
from .state import AWAITING_MAC, UNPINNED, Change, State, archive_reason
from .urls import listing_id_from_url

log = logging.getLogger(__name__)

# Listing pages read for pinned cars in one check, so a long list of saved
# cars costs a bounded number of requests and the searches go first.
CHECKS_PER_RUN = 20
# A page that says the car is gone has to say so on this many checks running
# before it is believed, as for a watched car (runner.GONE_EVIDENCE_NEEDED).
GONE_EVIDENCE_NEEDED = 2
# Why a car you stopped following was written off: your doing, not the
# market's, so the market figures leave it out.

Queue = Callable[[Change], None]
Silence = Callable[[str, str], None]


def pinned_ids(cfg: Config) -> set[str]:
    return {p.id for p in cfg.pinned}


def _placeholder(pin) -> Listing:
    """The record of a pinned car before its page has been read."""
    site = "Marketplace" if pin.site == "marketplace" else "AutoTrader"
    return Listing(id=pin.id, url=pin.url,
                   title=pin.name or f"{site} listing {pin.id.removeprefix('fb-')}",
                   search_id=PINNED_SEARCH_ID, search_name=PINNED_SEARCH_NAME,
                   source="pinned")


def check(cfg: Config, state: State, fetcher: Any, report: Any, *,
          queue: Queue, silence: Silence, dry_run: bool = False) -> None:
    """Read the pinned cars that are due, and keep their records straight.

    Never raises: a saved link is a nicety beside the searches, and a check
    is the job. ``queue`` and ``silence`` are the run's own, so a pinned car
    is announced or kept quiet by the same rules as every other.
    """
    try:
        _check(cfg, state, fetcher, report, queue=queue, silence=silence,
               dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 - never the reason a run fails
        log.warning("pinned listings: %s", exc)
        report.warnings.append(f"saved listings could not all be checked: {exc}")


def _check(cfg: Config, state: State, fetcher: Any, report: Any, *,
           queue: Queue, silence: Silence, dry_run: bool) -> None:
    pins = {p.id: p for p in cfg.pinned}
    now = clock.stamp()

    # Cars once pinned and no longer. They leave the watch, not the market:
    # the record stays, quietly, and the market figures leave them out.
    for lid, entry in state.listings.items():
        if not entry.get("pinned") or lid in pins:
            continue
        entry.pop("pinned", None)
        entry.pop("pinned_notify", None)
        if entry.get("search_id") == PINNED_SEARCH_ID and entry.get("status") == "active":
            entry.pop("pending", None)
            entry.pop("ride_along", None)
            state.mark_gone(lid, UNPINNED)
            entry["quiet_reason"] = UNPINNED
            entry["notified"] = True

    if not pins:
        return

    # Every pinned car carries the mark, whichever search found it first.
    for lid, pin in pins.items():
        entry = state.listings.get(lid)
        if entry is not None:
            entry["pinned"] = True
            entry["pinned_notify"] = pin.notify

    # Marketplace cars: recorded, and said to be waiting on the Mac.
    for lid, pin in pins.items():
        if pin.site != "marketplace" or lid in state.listings:
            continue
        state.record(_placeholder(pin))
        entry = state.listings[lid]
        entry.update({"pinned": True, "pinned_notify": pin.notify, "awaiting_mac": True})
        silence(lid, AWAITING_MAC)
        report.pinned_recorded += 1

    # AutoTrader cars: a few pages a check, least recently read first. A car
    # a search owns is read by that search, and only marked here.
    due = [pin for lid, pin in pins.items() if pin.site == "autotrader"
           and (state.listings.get(lid) or {}).get("search_id", PINNED_SEARCH_ID)
           in ("", PINNED_SEARCH_ID)]
    due.sort(key=lambda pin: str((state.listings.get(pin.id) or {})
                                 .get("pinned_checked_at") or ""))
    for pin in due[:CHECKS_PER_RUN]:
        entry = state.listings.get(pin.id)
        try:
            verdict, listing = _read(pin, fetcher)
        except BudgetExhausted:
            report.warnings.append("the request budget ran out before every "
                                   "saved listing was read; the rest wait for "
                                   "the next check")
            break
        except BlockedError as exc:
            report.warnings.append(f"saved listings: the site turned the check "
                                   f"away ({exc}); they wait for the next check")
            break
        if entry is not None:
            entry["pinned_checked_at"] = now
        report.pinned_checked += 1

        if listing is not None:
            listing.search_id, listing.search_name = PINNED_SEARCH_ID, PINNED_SEARCH_NAME
            listing.source = listing.source or "pinned"
            was_new = entry is None
            change = state.record(listing)
            entry = state.listings[pin.id]
            entry.update({"pinned": True, "pinned_notify": pin.notify,
                          "pinned_checked_at": now, "misses": 0})
            entry.pop("gone_evidence", None)
            if was_new:
                report.pinned_recorded += 1
            _announce(change, pin, entry, report, queue=queue, silence=silence,
                      first=was_new)
            continue

        if entry is None:
            # Never read successfully: keep a record of the link itself, so
            # the car is on the page with the reason it is empty.
            state.record(_placeholder(pin))
            entry = state.listings[pin.id]
            entry.update({"pinned": True, "pinned_notify": pin.notify,
                          "pinned_checked_at": now})
            silence(pin.id, archive_reason("saved from its link; its page could "
                                           "not be read yet"))
            report.pinned_recorded += 1
        if verdict is False and entry.get("status") == "active":
            # The page says gone. One 404 may be a retitled car, so it takes
            # more than one check to believe, as it does for a watched car.
            seen = int(entry.get("gone_evidence", 0)) + 1
            entry["gone_evidence"] = seen
            if seen < GONE_EVIDENCE_NEEDED:
                continue
            entry.pop("gone_evidence", None)
            change = state.mark_gone(pin.id, "its page says the listing has gone")
            if change is not None:
                report.removed += 1
                _announce(change, pin, entry, report, queue=queue, silence=silence)
        elif verdict is True:
            entry.pop("gone_evidence", None)


def _read(pin, fetcher: Any) -> tuple[bool | None, Listing | None]:
    """One pinned car's page: (still listed?, its facts), either unknown."""
    try:
        response = fetcher.get(pin.url, allow_block=True)
    except (BudgetExhausted, BlockedError):
        raise
    except FetchError as exc:
        text = str(exc)
        if (getattr(exc, "status", None) in (404, 410)
                or "HTTP 404" in text or "HTTP 410" in text):
            return False, None
        return None, None
    except Exception:  # noqa: BLE001 - a page that fails proves nothing
        return None, None
    html = response.text or ""
    if looks_blocked(html, response.status):
        raise BlockedError(f"HTTP {response.status} anti-bot page")
    # A removed listing is often redirected to another car or a results page,
    # and either still parses as a car. Only this car's own page counts.
    wanted = listing_id_from_url(pin.url)
    if wanted and (listing_id_from_url(response.url or pin.url) != wanted
                   or page_identifies(html, wanted) is False):
        return False, None
    detail = detail_from_html(html, pin.id, pin.url)
    if detail is None:
        return None, None
    return True, detail


def _announce(change: Change | None, pin, entry: dict[str, Any], report: Any, *,
              queue: Queue, silence: Silence, first: bool = False) -> None:
    """Announce a pinned car's change, or record why not."""
    if change is None:
        return
    if change.kind == Change.NEW:
        report.new += 1
        # Its first read is the car you asked for, not news: you have seen it.
        silence(entry["id"], archive_reason("saved from its link, so recorded "
                                            "as a starting point"))
        return
    if pin.archive_only:
        report.archived += 1
        silence(entry["id"], archive_reason(change.describe()))
        return
    counter = {Change.PRICE_DROP: "price_drops", Change.PRICE_RISE: "price_rises",
               Change.PRICED: "priced", Change.RELISTED: "relisted"}.get(change.kind)
    if counter:
        setattr(report, counter, getattr(report, counter) + 1)
    queue(change)
