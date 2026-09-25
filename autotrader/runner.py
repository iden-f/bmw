"""One full pass: scrape every search, work out what changed, tell the user.

State is saved in a ``finally`` block and every network step is wrapped, so
a single failure costs that search's results for one run, never the stored
history.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import clock, words
from . import archive as archive_mod
from . import thumbs as thumbs_mod
from . import dashboard, diagnose, filters, invariants, notifiers
from . import provision, render, shape, validate
from .config import Config
from .enrich import detail_from_html, enrich
from .http import BlockedError, BudgetExhausted, FetchError, Fetcher
from .listing import Listing
from .parser import looks_like_no_results, parse_search_page
from .state import HIDDEN_REASON_PREFIX, Change, State, utcnow
from .urls import normalise_search_url, page_url

log = logging.getLogger(__name__)


_many = words.many


@dataclass
class RunReport:
    started_at: float = field(default_factory=time.time)
    searches_run: int = 0
    searches_failed: int = 0
    requests_made: int = 0
    budget_exhausted: bool = False
    # What started the run (schedule, push, workflow_dispatch,
    # repository_dispatch, manual), so coverage can tell scheduled runs from
    # ones a person started.
    trigger: str = "manual"

    empty_parses: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    shape_drift: list[dict[str, Any]] = field(default_factory=list)
    disabled_channels: list[str] = field(default_factory=list)
    channel_results: list[Any] = field(default_factory=list)
    first_run: bool = False
    validation_ok: bool = True
    validation_report: str = ""
    provisioned: dict[str, Any] = field(default_factory=dict)
    listings_seen: int = 0
    new: int = 0
    price_drops: int = 0
    price_rises: int = 0
    removed: int = 0
    filtered_out: int = 0
    # Cars held back for having no published price, which only happens when a
    # search sets `require_price`. The number of call-for-price cars watched
    # is a different figure: `health.counts.unpriced` on the dashboard.
    unpriced: int = 0
    priced: int = 0
    relisted: int = 0
    qualified: int = 0
    # Searches that established a starting point this run instead of alerting.
    baselines: list[str] = field(default_factory=list)
    invariants: list[str] = field(default_factory=list)
    # What the run did about photographs. Nothing here can fail a check.
    photos: dict[str, Any] = field(default_factory=dict)
    # What this run cost and where the month stands - see budget.py.
    budget: dict[str, Any] = field(default_factory=dict)
    # Changes held back because you said you did not want them.
    your_call: int = 0
    # Searches that read the site fine and kept nothing after your rules.
    shut_out: list[str] = field(default_factory=list)
    # Changes on cars your rules hide: real, deliberately unannounced, counted.
    hidden_events: dict[str, int] = field(default_factory=dict)
    # Stored cars thrown away this run because no search was for them.
    discarded: int = 0
    # Results the site returned that the search is not for, per search name.
    # Counted rather than dropped silently, so a change in how the site or the
    # parser handles the model shows up as a change in this number.
    not_this_car: dict[str, int] = field(default_factory=dict)
    # The schedule fired twice, so this run stood down.
    skipped: bool = False
    # Minutes since the last successful run, when that is longer than planned.
    missed_by: int = 0
    notified: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    strategies: dict[str, str] = field(default_factory=dict)
    quiet: bool = False
    dry_run: bool = False

    @property
    def duration_s(self) -> float:
        return round(time.time() - self.started_at, 1)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "duration_s": self.duration_s,
            "searches_run": self.searches_run, "searches_failed": self.searches_failed,
            "listings_seen": self.listings_seen, "new": self.new,
            "price_drops": self.price_drops, "price_rises": self.price_rises,
            "removed": self.removed, "filtered_out": self.filtered_out,
            "unpriced": self.unpriced, "priced": self.priced,
            "relisted": self.relisted, "qualified": self.qualified,
            "baselines": self.baselines,
            "invariants": self.invariants, "shut_out": self.shut_out,
            "hidden_events": self.hidden_events, "skipped": self.skipped,
            # Wrong-model results and discarded rows stay in the run record,
            # so a sudden fall in the number of watched cars stays explained.
            "not_this_car": self.not_this_car, "discarded": self.discarded,
            "missed_by": self.missed_by,
            "requests_made": self.requests_made,
            "budget_exhausted": self.budget_exhausted,
            "empty_parses": self.empty_parses,
            "diagnostics": self.diagnostics,
            "shape_drift": self.shape_drift,
            "disabled_channels": self.disabled_channels,
            "first_run": self.first_run,
            "validation_ok": self.validation_ok,
            "notified": self.notified, "errors": self.errors[:10],
            "warnings": self.warnings[:10], "strategies": self.strategies,
            "quiet": self.quiet, "dry_run": self.dry_run,
            "photos": self.photos, "your_call": self.your_call,
            "trigger": self.trigger,
            # This run's minutes. Not keyed "charged": the ledger uses that
            # word for whether minutes draw on the allowance.
            "minutes": (self.budget or {}).get("this_run_minutes"),
            "minutes_label": (self.budget or {}).get("label"),
        }

    def summary(self) -> str:
        """One line. Everything that happened, nothing that did not.

        It opens the Actions log and is quoted in the watchdog's alert, so it
        is user-facing copy and every count agrees with its noun.
        """
        def many(count: int, one: str, more: str = "") -> str:
            return f"{count} {one if count == 1 else (more or one + 's')}"

        bits = [many(self.searches_run, "search", "searches"),
                many(self.listings_seen, "listing")]
        for count, one, more in ((self.new, "new", "new"),
                                 (self.price_drops, "price drop", ""),
                                 (self.price_rises, "price rise", ""),
                                 (self.priced, "price published", "prices published"),
                                 (self.removed, "removed", "removed"),
                                 (self.relisted, "back on sale", "back on sale"),
                                 (self.qualified, "back inside your rules",
                                  "back inside your rules"),
                                 (self.unpriced, "call for price", "call for price"),
                                 (self.filtered_out, "hidden by your rules",
                                  "hidden by your rules"),
                                 (self.searches_failed, "failed", "failed")):
            if count:
                bits.append(many(count, one, more))
        if self.baselines:
            bits.append(f"{len(self.baselines)} baselined")
        for kind, count in sorted(self.hidden_events.items()):
            bits.append(f"{count} {kind.replace('_', ' ')} on "
                        + ("a hidden car" if count == 1 else "hidden cars"))
        if len(bits) == 2:
            bits.append("nothing changed")
        return (", ".join(bits)
                + f" - {_many(self.requests_made, 'request')} in {self.duration_s}s")


# Below this a search is too small for "half of last time" to mean anything.
MIN_COUNT_FOR_COLLAPSE = 6

def scope_of(search, cfg: Config) -> str:
    """A fingerprint of what a search is asking for.

    Covers everything that changes which cars a search returns: the link, the
    rules layered on it, and how many pages are read. When it changes, the
    cars that appear were always in scope, so they are a baseline rather
    than new listings.
    """
    scraping = cfg.get("scraping", {}) or {}
    rules = cfg.rules_for(search)
    parts = {
        "url": normalise_search_url(search.url),
        "filters": {k: v for k, v in sorted((rules["filters"] or {}).items())
                    if v not in (None, "", [], {})},
        "pages": int(search.max_pages or scraping.get("max_pages", 3) or 1),
        "per_page": int(scraping.get("results_per_page", 50) or 50),
    }
    blob = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

# A cap, so a search whose window rotates heavily cannot spend a whole run's
# budget proving that nothing has changed.
REMOVAL_CHECKS_PER_RUN = 12

# Runs that must independently reach "this page is gone" before it is believed.
GONE_EVIDENCE_NEEDED = 2

# More of a watch than this vanishing in one run is treated as suspicious
# rather than as news, however complete the read looked.
MASS_REMOVAL_FRACTION = 0.25

# Runs of "could not tell" before absence is believed on its own. Without a
# limit an unreadable listing page leaves a car pending for good.
GONE_UNKNOWN_LIMIT = 5

# Wording a listing page uses once the car has sold or been withdrawn.
GONE_MARKERS = (
    "no longer available", "no longer for sale", "this listing has ended",
    "listing not found", "ad has been removed", "has been sold",
    "n'est plus disponible", "annonce n'existe plus", "page not found",
)


def _still_listed(url: str, fetcher: Fetcher) -> bool | None:
    """Is this car still on the site?  True, False, or None for "cannot tell".

    A car missing from a sample of a larger result set has not necessarily
    gone anywhere, so before announcing a sale we ask the listing page itself.
    """
    if not url:
        return None
    try:
        response = fetcher.get(url, allow_block=True)
    except BudgetExhausted:
        raise
    except FetchError as exc:
        # 404/410 is the site telling us plainly. Anything else - a timeout,
        # a 503 - says nothing about the car.
        text = str(exc)
        if "HTTP 404" in text or "HTTP 410" in text:
            return False
        return None
    except Exception:  # noqa: BLE001 - a check that fails proves nothing
        return None

    body = (response.text or "")[:200000].lower()
    if any(marker in body for marker in GONE_MARKERS):
        return False
    if detail_from_html(response.text, url=response.url) is not None:
        return True
    return None


@dataclass
class SearchResult:
    """What one search returned, and how much of it we actually saw."""

    listings: list[Listing]
    strategy: str
    said_no_results: bool
    candidates: dict[str, int]
    first_page: Any
    # True when the last page added nothing new, i.e. we reached the end of the
    # results rather than stopping at max_pages with more still to read.
    complete: bool
    # Set when a page after the first could not be read. What was read is
    # kept; `complete` is False, so absence from it is not treated as
    # evidence that a car has gone.
    partial_error: str | None = None
    # True when every page read named its own results, so this is the search
    # rather than the search plus whatever the page was advertising beside
    # it. See parser._declared_results.
    confined: bool = False
    # How many rows the strategies found that the page did not count as
    # results, summed over the pages read.
    off_list: int = 0


def scrape_search(search, cfg: Config, fetcher: Fetcher) -> "SearchResult":
    """Fetch and parse every page of one search.  Raises on a hard failure."""
    scraping = cfg.get("scraping", {}) or {}
    max_pages = max(1, int(search.max_pages or scraping.get("max_pages", 3) or 1))
    per_page = int(scraping.get("results_per_page", 50) or 50)

    found: dict[str, Listing] = {}
    strategy = "none"
    candidates: dict[str, int] = {}
    said_no_results = False
    first_page = None
    complete = False
    confined_pages = 0
    pages_read = 0
    off_list = 0
    referer = "https://www.autotrader.ca/"

    partial_error: str | None = None
    for page in range(1, max_pages + 1):
        url = page_url(search.url, page, per_page)
        try:
            response = fetcher.get(url, referer=referer)
        except (FetchError, BlockedError) as exc:
            # A failure on page one fails the search; on a later page it only
            # shortens the read. What was read is kept, and `complete` stays
            # False, so the removal path asks each car's listing page before
            # believing it has gone.
            if page == 1 or not found:
                raise
            partial_error = str(exc)
            log.warning("%s: stopped at page %d of %d - %s",
                        search.name, page, max_pages, exc)
            break
        referer = url
        result = parse_search_page(response.text, url)
        # A page with no rows is neutral for confinement. The empty last page
        # that proves the end of a search would otherwise make every complete
        # read look unconfined.
        if result.listings:
            pages_read += 1
            if result.confined:
                confined_pages += 1
        off_list += result.off_list
        if page == 1:
            strategy = result.strategy
            candidates = dict(result.candidates)
            said_no_results = looks_like_no_results(response.text)
            first_page = response
            if not result.listings:
                # An empty first page is either a genuinely empty search or a
                # parser that has fallen behind the site.  Both need saying.
                log.warning("no listings on page 1 of %s (strategies tried: %s)",
                            search.name, result.candidates)
        fresh = 0
        for listing in result.listings:
            if listing.id in found:
                continue
            listing.search_id = search.id
            listing.search_name = search.name
            found[listing.id] = listing
            fresh += 1

        # Absence from a complete read is evidence a car has gone; absence
        # from a partial one is not. A page that parsed nothing ends the read
        # but counts as the end only when the page itself says there are no
        # results, or a broken parser would authorise removals for every car
        # on the unread pages.
        if not result.listings:
            if result.declared == 0 or looks_like_no_results(response.text):
                complete = True
            break

        # A page that adds nothing new is the other reliable end. A short page
        # is not: page sizes vary, and stopping on one would make the cars on
        # later pages look gone. Proving the end costs a single request.
        if fresh == 0:
            complete = True
            break

    # Confined only if every page read was: dropping a stored car as never
    # having been in this search must not rest on a partial answer.
    return SearchResult(list(found.values()), strategy, said_no_results,
                        candidates, first_page, complete, partial_error,
                        confined=bool(pages_read) and confined_pages == pages_read,
                        off_list=off_list)


# What each rule is called when telling the user it turned everything away.
# Keyed on the rule, not its reason text, which can contain any word (a
# seller's name, for one).
RULE_LABELS = {
    "models": "a different model",
    "max_distance_km": "too far away",
    "provinces": "in the wrong province",
    "min_year": "outside the year range",
    "max_year": "outside the year range",
    "max_price": "over your price ceiling",
    "min_price": "under your price floor",
    "max_mileage_km": "over your odometer limit",
    "require_price": "no price published",
    "include_keywords": "a keyword rule",
    "exclude_keywords": "a keyword rule",
    "exclude_sellers": "an excluded seller",
}


def _why_none_survived(dropped: list[tuple[Listing, filters.Verdict]]) -> str:
    """Which rule turned everything away, in the order it hurt most."""
    buckets: dict[str, int] = {}
    for _, verdict in dropped:
        label = RULE_LABELS.get(verdict.rule, "other rules")
        buckets[label] = buckets.get(label, 0) + 1
    ranked = sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{n} {label}" for label, n in ranked) or "every one of them"


def _minutes_since_last_ok(state: State) -> float | None:
    """How long since the site was last actually read, or None if never.

    Uses State.last_check, not the newest ok run: a firing that stands down
    is recorded as ok but read nothing, and counting it would understate the
    gap.
    """
    return clock.minutes_since((state.last_check or {}).get("at"))


def _minutes_since_any_run(state: State) -> float | None:
    """How long since a run happened at all, working or not."""
    for run in (state.data.get("runs") or []):
        age = clock.minutes_since(run.get("at"))
        if age is not None:
            return age
    return None


def _price_disagrees(listing: Listing, state: State) -> bool:
    """True when the results card has changed its mind about the price.

    Comparing the card against the stored (detail-page) price would re-fetch
    every car whose card and detail figures permanently disagree - once per
    run, forever. Comparing card against card only spends a request when the
    site actually changed something.
    """
    stored = state.listings.get(listing.id) or {}
    if listing.card_price is None:
        return False
    seen_card = stored.get("card_price")
    if seen_card is None:
        # Never confirmed against the listing page: worth doing once.
        return stored.get("price_source") != "detail" and stored.get("price") != listing.card_price
    return seen_card != listing.card_price


def enrich_listings(listings: list[Listing], cfg: Config, fetcher: Fetcher,
                    report: RunReport) -> None:
    """Fill in price/odometer/photos from each detail page's JSON-LD."""
    scraping = cfg.get("scraping", {}) or {}
    if not scraping.get("enrich_details", True):
        return
    cap = int(scraping.get("enrich_limit", 25) or 25)
    # Leave enough budget for the remaining searches' result pages.
    room = max(0, getattr(fetcher, "budget_left", cap) - 5)
    for listing in listings[:min(cap, room)]:
        try:
            response = fetcher.get(listing.url, referer=listing.url)
            enrich(listing, response.text)
        except BudgetExhausted:
            report.warnings.append("stopped looking up listing details: "
                                   "request budget spent")
            break
        except (FetchError, BlockedError) as exc:
            report.warnings.append(f"could not enrich {listing.id}: {exc}")
        except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
            report.warnings.append(f"enrichment error on {listing.id}: {exc}")


def run(cfg: Config | None = None, state: State | None = None, *,
        dry_run: bool = False, env: dict[str, str] | None = None,
        notify: bool = True, fetcher: Fetcher | None = None,
        force: bool = False) -> RunReport:
    """Execute one full pass and return what happened."""
    cfg = cfg or Config.load()
    state = state or State.load()
    env = env if env is not None else dict(os.environ)
    report = RunReport(dry_run=dry_run)

    # Make the bot reachable before doing anything else, so a fresh install
    # works without being configured first.
    if not dry_run:
        try:
            report.provisioned = provision.bootstrap(cfg, env)
            for step in report.provisioned.get("steps", []):
                if step.get("changed"):
                    report.warnings.append(f"setup: {step['reason']}")
        except Exception as exc:  # noqa: BLE001 - never block a run on setup
            log.warning("automatic setup failed: %s", exc)
            report.warnings.append(f"automatic setup failed: {exc}")

    # "First run" means no run has ever succeeded, so the parser has never been
    # shown to work against the live site.
    report.first_run = not any(r.get("ok") for r in (state.data.get("runs") or []))

    # Scheduled firings arrive late, early, twice or not at all. One that
    # lands too soon after the last check stands down, and a long gap is
    # recorded.
    health_conf = cfg.get("health", {}) or {}
    expected = int(health_conf.get("expected_interval_minutes", 30) or 0)
    gap = _minutes_since_last_ok(state)
    if gap is not None and expected > 0:
        floor = float(health_conf.get("min_interval_minutes", expected / 3.0) or 0)
        # Only scheduled firings are deduplicated; a manual run always runs.
        on_a_schedule = str(env.get("AUTOTRADER_SCHEDULED") or "").strip().lower() \
            not in ("", "0", "false", "no")
        if gap < floor and on_a_schedule and not force:
            # Not a warning: the schedule fires more often than the check
            # interval so a dropped firing costs little, and the floor turns
            # those firings back into the intended interval. Most firings are
            # meant to stand down.
            report.skipped = True
            # Still recorded: it proves the timer is alive, and the runner it
            # held has to be billed.
            if not dry_run:
                _write_the_run_down(cfg, state, report, env, notify)
            return report
        if gap > expected * 2:
            report.missed_by = round(gap)
            # A bot that runs and fails looks the same, from the last
            # successful run alone, as a schedule that stopped firing. Ask
            # when any run last happened before blaming the schedule.
            since_any = _minutes_since_any_run(state)
            if since_any is not None and since_any < gap - expected:
                report.warnings.append(
                    f"no check has succeeded for {gap / 60:.1f} hours, but "
                    f"the last one ran {_many(round(since_any), 'minute')} ago - so the "
                    f"bot is being started and is failing, which is not the "
                    f"same as not being started.")
            # A schedule that simply stopped firing is not a warning: the
            # user cannot act on it, `report.missed_by` records the gap, and
            # the dashboard already shows a late check.

    settings = cfg.get("notifications", {}) or {}
    scraping = cfg.get("scraping", {}) or {}
    archive_conf = cfg.get("archive", {}) or {}

    fetcher = fetcher or Fetcher(
        timeout=int(scraping.get("timeout_seconds", 30) or 30),
        retries=int(scraping.get("retries", 3) or 0),
        delay_ms=int(scraping.get("delay_ms", 1200) or 0),
        user_agent=str(scraping.get("user_agent", "auto")),
        budget=int(scraping.get("request_budget", 250) or 0),
    )

    changes: list[Change] = []
    # Ids this run has already assigned to a search, and ids seen by any
    # search at all - the first decides who owns a car, the second decides
    # whether it can possibly have gone.
    owned: set[str] = set()
    seen_anywhere: set[str] = set()
    # Results that were never this search's car, gathered across every search
    # so one search's reject can still be another's keeper before any of them
    # are thrown away.
    wrong_car_ids: set[str] = set()
    # Searches whose read this run actually produced model names. Only those
    # may have their stored rows swept, for the same reason the live rule
    # stands down: a parse that names nothing must not empty a watch.
    read_with_models: set[str] = set()
    # Searches whose read this run was confined to the page's own declared
    # results and ran to the end of them. Only those can settle whether a
    # stored car was ever in the search at all.
    read_completely_confined: set[str] = set()
    # Confined, whether or not it reached the end. Enough to know the site is
    # still naming its own results, which is all the models rule needs in
    # order to stand aside.
    read_confined: set[str] = set()
    # Every id a confined read named as one of its own results this run.
    declared_seen: set[str] = set()
    removal_plan: list[tuple[Any, bool, bool, dict[str, Any]]] = []
    rejected: dict[str, tuple[Listing, filters.Verdict]] = {}
    blocked_searches: list[str] = []
    assessments: list[validate.Assessment] = []
    drifted: list[tuple[str, list[str], dict[str, Any]]] = []

    def silence(listing_id: str, reason: str) -> None:
        """Say nothing about this car, on purpose, and write down why."""
        if not dry_run:
            state.silence(listing_id, reason)

    def queue(change: Change) -> None:
        """Record a change as owed to the user, then remember to send it.

        Writing the intent to state first means an interruption before
        delivery costs nothing: the next run finds it pending and sends it.

        A change on a car the user muted or dismissed stops here. It is
        recorded and stays on the dashboard, silenced with the user's
        instruction as the reason.
        """
        entry = state.listings.get(change.listing.id) or {}
        yours = entry.get("you") or {}
        if yours.get("muted") or yours.get("dismissed"):
            word = "muted" if yours.get("muted") else "dismissed"
            report.your_call += 1
            silence(change.listing.id, f"you {word} this car")
            return
        changes.append(change)
        if not dry_run:
            state.defer([change])

    try:
        searches = cfg.active_searches
        if not searches:
            report.warnings.append(
                "No searches configured. Paste an AutoTrader search link into "
                "config.json, or run: python -m autotrader add <url>")
            # Not an early return: with no searches left, housekeeping still
            # has to retire their cars and audit what remains.

        for search in searches:
            try:
                result = scrape_search(search, cfg, fetcher)
                listings = result.listings
                strategy = result.strategy
                said_no_results = result.said_no_results
                candidates = result.candidates
                first_page = result.first_page
                report.strategies[search.id] = strategy
                if result.partial_error:
                    report.warnings.append(
                        f"{search.name}: read {_many(len(listings), 'listing')} "
                        f"and then could not reach the next page "
                        f"({result.partial_error}). Keeping what was read; "
                        f"nothing will be called gone on a search this run "
                        f"did not finish.")
            except BudgetExhausted as exc:
                # Not a failure: we deliberately stopped. Leave the remaining
                # searches for the next run rather than marking them broken.
                report.warnings.append(str(exc))
                report.budget_exhausted = True
                break
            except BlockedError as exc:
                report.searches_failed += 1
                report.errors.append(f"{search.name}: {exc}")
                blocked_searches.append(search.name)
                state.record_search_error(search.id, str(exc))
                continue
            except FetchError as exc:
                report.searches_failed += 1
                report.errors.append(f"{search.name}: {exc}")
                state.record_search_error(search.id, str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 - one search must not end the run
                report.searches_failed += 1
                report.errors.append(f"{search.name}: unexpected error: {exc}")
                log.exception("unexpected failure scraping %s", search.name)
                state.record_search_error(search.id, str(exc))
                continue

            report.searches_run += 1
            report.listings_seen += len(listings)

            # Each search may override the global filters and alert rules.
            # Resolved before the page checks, because the models rule must run
            # before any request is spent enriching a car that is not wanted.
            rules = cfg.rules_for(search)
            search_filters = rules["filters"]
            search_notify = rules["notify_on"]
            # Read before recording, or "the usual count" is this run's count
            # and the collapse check below can never fire.
            usual_count = int((state.search_health(search.id) or {}).get(
                "last_count", 0) or 0)
            # Counts are comparable only when both reads were measured the
            # same way (confined or not), as in shape.compare: a confined read
            # is naturally smaller than an unconfined one.
            counted_the_same_way = (
                bool((state.search_shape(search.id) or {}).get("confined"))
                == bool(result.confined))
            state.record_search_ok(search.id, len(listings), strategy)

            assessments.append(validate.assess(
                listings, strategy, search.name,
                said_no_results=said_no_results,
                candidates=candidates))
            # After the first run, a parse that still reads but has degraded
            # (a links-only fallback, missing names, prices or years) is a
            # warning, not an error: half a parse is better than none.
            if not report.first_run and assessments[-1].concerns:
                report.warnings.append(
                    f"{search.name}: the page still reads, but not cleanly - "
                    + "; ".join(assessments[-1].concerns))

            if health_conf.get("watch_page_shape", True) and first_page is not None:
                try:
                    current = shape.fingerprint(first_page.text, strategy,
                                                candidates, len(listings),
                                                confined=result.confined)
                    reasons, serious = shape.compare(
                        state.search_shape(search.id), current)
                    state.record_shape(search.id, current)
                    if reasons:
                        # Always keep the evidence when the shape moves: by the
                        # time the parser actually breaks, the page that broke
                        # it is long gone.
                        report.shape_drift.append(
                            {"search": search.name, "reasons": reasons,
                             "serious": serious})
                        try:
                            data = diagnose.capture(
                                first_page.url, first_page.text,
                                first_page.status, first_page.elapsed_ms,
                                search.name)
                            report.diagnostics.append(
                                str(diagnose.write(data, search.id)))
                            if serious:
                                # A description of the page is enough to see
                                # that something moved; only the page itself is
                                # enough to rewrite a strategy against.
                                raw = diagnose.write_raw(first_page.text, search.id)
                                if raw:
                                    report.diagnostics.append(str(raw))
                        except Exception as exc:  # noqa: BLE001
                            log.warning("could not capture the page: %s", exc)
                    if serious:
                        drifted.append((search.name, reasons, current))
                except Exception as exc:  # noqa: BLE001 - never fatal
                    log.warning("shape check failed for %s: %s", search.name, exc)

            if not listings and said_no_results:
                # The site itself says the search matched nothing. That is a
                # narrow search, not a broken parser.
                report.warnings.append(
                    f"{search.name}: AutoTrader reports no results for this search.")
            elif not listings:
                # A 200 with listings we could not read, and no "no results"
                # message: every strategy has fallen behind the site. Capture
                # what the page actually looked like, because a log line saying
                # "found 0" is almost useless to fix a parser from.
                if first_page is not None:
                    try:
                        data = diagnose.capture(
                            first_page.url, first_page.text, first_page.status,
                            first_page.elapsed_ms, search.name)
                        written = diagnose.write(data, search.id)
                        report.diagnostics.append(str(written))
                        raw = diagnose.write_raw(first_page.text, search.id)
                        if raw:
                            report.diagnostics.append(str(raw))
                        log.warning("wrote a page capture to %s", written)
                    except Exception as exc:  # noqa: BLE001 - never fatal
                        log.warning("could not capture the page: %s", exc)
                report.errors.append(
                    f"{search.name}: the page loaded ({strategy}) but no listings "
                    f"could be read, and the site did not say the search was empty. "
                    f"Run: python -m autotrader doctor --live")
                report.empty_parses.append(search.name)
                state.record_search_error(search.id, "HTTP 200 but zero listings parsed")

            # Results for a different model are not cars a rule is hiding, so
            # they are dropped here: before enrichment, so they cost no
            # requests, and before recording, so they cost no state. `listings`
            # stays as returned, since the page checks are about the read.
            search_filters = dict(search_filters)
            if result.confined and search_filters.get("models"):
                # On a confined read the site has already named the search's
                # results, so the models rule stands aside. It discards rather
                # than hides, and the site may file a model under its parent
                # series, so its mistakes would vanish without trace. The rule
                # still guards unconfined reads.
                search_filters.pop("models", None)
            # Whether the model rule could act at all this run. A parse that
            # produced no model for anything is a parser problem, and a rule
            # that then turns away the entire watch would bury it.
            model_readable = filters.model_is_readable(listings)
            if listings and not model_readable and search_filters.get("models"):
                report.warnings.append(
                    f"{search.name}: not one of the {_many(len(listings), 'result')} "
                    f"read carries a model, so the models rule is standing down "
                    f"this check rather than turning the whole search away. The "
                    f"parser is reading the page with '{strategy}', which fills "
                    f"in no model - run: python -m autotrader doctor --live")
            for_me, not_mine = filters.not_this_car(listings, search_filters)
            # Kept per search so the Searches tab can explain a count that
            # shrank.
            state.search_health(search.id)["not_this_car"] = len(not_mine)
            if model_readable:
                read_with_models.add(search.id)
            if not_mine:
                report.not_this_car[search.name] = len(not_mine)
                wrong_car_ids.update(l.id for l, _ in not_mine)
            if listings and not for_me:
                # Every result was a different car: a very narrow rule or a
                # parser that stopped reading the model field. They look the
                # same from here, so say so rather than report an empty search.
                report.warnings.append(
                    f"{search.name}: all {_many(len(listings), 'result')} the site "
                    f"returned were a different model "
                    f"({not_mine[0][1].reason}). Either the search link is "
                    f"pointing somewhere else now, or the models rule needs "
                    f"widening.")

            # Enrich unrecorded cars, plus any whose card price changed, so a
            # price move is confirmed on the listing page before it is
            # announced. Disputed prices go first so an exhausted budget does
            # not leave them stale.
            disputed = [l for l in for_me if state.known(l.id)
                        and _price_disagrees(l, state)]
            unknown = [l for l in for_me if not state.known(l.id)]
            # A card with no price often sits above a listing page that has
            # one, and a price may be published later. Recheck a few times,
            # not every run, or a genuine call-for-price car is re-fetched
            # forever.
            recheck_limit = int(scraping.get("unpriced_rechecks", 3) or 0)
            still_unpriced = [
                l for l in for_me
                if l.price is None and state.known(l.id)
                and (state.listings.get(l.id) or {}).get("price") is None
                and int((state.listings.get(l.id) or {}).get("price_checks", 0)) < recheck_limit
            ]
            for listing in still_unpriced:
                entry = state.listings.get(listing.id) or {}
                entry["price_checks"] = int(entry.get("price_checks", 0)) + 1
            enrich_listings(disputed + unknown + still_unpriced, cfg, fetcher, report)

            if report.first_run and not assessments[-1].trustworthy:
                # Keep the evidence: a parse that produced nonsense is as hard
                # to fix from a log line as one that produced nothing.
                if first_page is not None and not report.diagnostics:
                    try:
                        data = diagnose.capture(
                            first_page.url, first_page.text, first_page.status,
                            first_page.elapsed_ms, search.name)
                        report.diagnostics.append(str(diagnose.write(data, search.id)))
                        raw = diagnose.write_raw(first_page.text, search.id)
                        if raw:
                            report.diagnostics.append(str(raw))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("could not capture the page: %s", exc)
                # The parser is unproven against the live site and this parse
                # looks wrong. Recording it would fill state and the archive
                # with bad rows, so stop: nothing is written and the next run
                # retries.
                report.errors.append(
                    f"{search.name}: first-run parse check failed - "
                    + "; ".join(assessments[-1].concerns))
                continue

            # A search whose scope just changed (a new link, a relaxed filter,
            # more pages) is establishing what it watches, not discovering
            # cars. Record everything, announce nothing, say so.
            health = state.search_health(search.id)
            scope = scope_of(search, cfg)
            known_scope = health.get("scope")
            # Only a change of scope is a baseline. A search's first run
            # genuinely shows cars the user has not seen.
            baseline = bool(known_scope) and known_scope != scope
            health["scope"] = scope
            if baseline:
                report.baselines.append(search.name)
                report.warnings.append(
                    f"{search.name}: what this search covers has changed, so "
                    f"what it finds now is being recorded as a starting point "
                    f"rather than announced as new.")

            # A car returned by two searches belongs to the first that keeps
            # it. Otherwise the last search to run would take it over and apply
            # its own rules to a car another search had already accepted.
            mine = [l for l in for_me if l.id not in owned]
            # Every id the site returned, wrong cars included. A car that was
            # on the page is never "missing", whichever search it belongs to.
            seen_anywhere.update(l.id for l in listings)

            # Cars the site listed as this search's results, not merely found
            # on the page. Stamped only after recording below, because a car
            # seen for the first time has no stored row yet, and a missing
            # stamp would later read as "never in this search".
            if result.confined:
                read_confined.add(search.id)
                if result.complete:
                    read_completely_confined.add(search.id)
                declared_seen.update(l.id for l in listings)

            kept, unpriced, dropped, wrong = filters.apply(mine, search_filters)
            # Empty in practice, since the split above took them, but a reader
            # of `apply` should not have to know that.
            if wrong:
                report.not_this_car[search.name] = (
                    report.not_this_car.get(search.name, 0) + len(wrong))
                wrong_car_ids.update(l.id for l, _ in wrong)
            # Only cars this search keeps are claimed. One it rejects is still
            # offered to later searches: kept if any search wants it.
            owned.update(l.id for l in kept)
            owned.update(l.id for l in unpriced)
            report.unpriced += len(unpriced)

            # A search that reads fine and keeps nothing looks broken from the
            # outside, and a rule that matches nothing is worth knowing about.
            # Say what it read and what turned it all away.
            if for_me and not kept and not unpriced and not baseline:
                why = _why_none_survived(dropped)
                report.shut_out.append(search.name)
                report.warnings.append(
                    f"{search.name}: read {_many(len(for_me), 'listing')} and none "
                    f"passed your rules for it ({why}). It is working - it just "
                    f"has nothing to show you.")
                state.search_health(search.id)["shut_out"] = why

            if kept or unpriced:
                state.search_health(search.id).pop("shut_out", None)

            for listing in kept:
                # A car the rules hid and now admit is news, though it has been
                # stored all along. Otherwise it would change state silently and
                # keep "hidden by your rules" as its reason for being quiet.
                was_hidden = str((state.listings.get(listing.id) or {}).get(
                    "quiet_reason") or "").startswith(HIDDEN_REASON_PREFIX)
                change = state.record(listing, filtered=False)
                if change is None and was_hidden:
                    # Not new: it was on the market all along, hidden by a
                    # rule. What changed is that it now qualifies.
                    change = Change(Change.QUALIFIED, listing,
                                    new_price=listing.price)
                if change is None:
                    continue
                if change.kind == Change.NEW:
                    report.new += 1
                    if baseline:
                        silence(listing.id, "recorded as a starting point when "
                                            "this search's scope changed")
                    elif search_notify.get("new", True):
                        queue(change)
                    else:
                        silence(listing.id,
                                "new listings are switched off for this search")
                    archive_mod.archive_listing(listing, archive_conf, fetcher)
                elif change.kind == Change.PRICE_DROP:
                    # Every branch either sends or records why not, so no car
                    # keeps a stale delivery mark from an earlier alert.
                    #
                    # Measured from the price the user last heard, not the
                    # last run, so a series of small cuts adds up to an alert.
                    heard = state.heard_price(listing.id)
                    now = change.new_price or 0
                    was = (heard if heard is not None else change.old_price) or 0
                    alert_abs = int(rules.get("price_drop_alert_abs") or 0)
                    worth_mentioning = filters.is_significant_drop(
                        was, now,
                        rules["price_drop_min_pct"], rules["price_drop_min_abs"])
                    big_enough_alone = (worth_mentioning and alert_abs <= 0) or (
                        alert_abs > 0 and was - now >= alert_abs)
                    if worth_mentioning or big_enough_alone:
                        report.price_drops += 1
                    if heard is not None and now >= heard:
                        # Down since the last run but not below the price the
                        # user last heard, so announcing a drop would be false.
                        silence(listing.id, (
                            f"came down ${abs(change.delta or 0):,}, but is "
                            f"still ${now - heard:,} above the ${heard:,} you "
                            f"last heard"))
                    elif not (worth_mentioning or big_enough_alone):
                        silence(listing.id, (
                            f"the price moved ${abs(change.delta or 0):,}, under "
                            f"the ${int(rules['price_drop_min_abs']):,} and "
                            f"{rules['price_drop_min_pct']:g}% you asked to hear about"))
                    elif baseline:
                        silence(listing.id, "recorded as a starting point "
                                            "when this search's scope changed")
                    elif not search_notify.get("price_drop", True):
                        silence(listing.id, "price drops are switched off "
                                            "for this search")
                    elif big_enough_alone:
                        queue(Change(Change.PRICE_DROP, listing,
                                     old_price=was, new_price=now))
                    elif rules.get("small_drops_ride_along", True):
                        state.hold_for_ride_along(listing.id, (
                            f"${was - now:,} off since you last heard, under the "
                            f"${alert_abs:,} that sends an alert on its own - it "
                            f"goes out with the next new car"))
                    else:
                        silence(listing.id, (
                            f"${was - now:,} off since you last heard, under the "
                            f"${alert_abs:,} you asked to be alerted about"))
                elif change.kind == Change.PRICE_RISE:
                    report.price_rises += 1
                    if baseline:
                        silence(listing.id, "recorded as a starting point when "
                                            "this search's scope changed")
                    elif search_notify.get("price_rise", False):
                        queue(change)
                    else:
                        silence(listing.id, "you asked not to be told about "
                                            "prices going up")
                elif change.kind == Change.PRICED:
                    report.priced += 1
                    if baseline:
                        silence(listing.id, "recorded as a starting point when "
                                            "this search's scope changed")
                    elif search_notify.get("priced", True):
                        queue(change)
                    else:
                        silence(listing.id, "a price appearing is switched off "
                                            "for this search")
                elif change.kind == Change.RELISTED:
                    report.relisted += 1
                    # A relist that comes back meaningfully cheaper is also
                    # announced under the price-drop switch: relists are off by
                    # default, and these are often the best drops there are.
                    came_back_cheaper = bool(
                        change.delta and change.delta < 0
                        and filters.is_significant_drop(
                            change.old_price or 0, change.new_price or 0,
                            rules["price_drop_min_pct"], rules["price_drop_min_abs"])
                        # The same bar a price drop has to clear on its own.
                        and -(change.delta or 0) >= int(
                            rules.get("price_drop_alert_abs") or 0))
                    if baseline:
                        pass
                    elif search_notify.get("relisted", False):
                        queue(change)
                    elif came_back_cheaper and search_notify.get("price_drop", True):
                        report.price_drops += 1
                        queue(change)
                elif change.kind == Change.QUALIFIED:
                    report.qualified += 1
                    if baseline:
                        silence(listing.id, "recorded as a starting point when "
                                            "this search's scope changed")
                    elif search_notify.get("qualified", True):
                        queue(change)
                    else:
                        silence(listing.id, "it came back inside your rules, "
                                            "and that is switched off here")

            # Call-for-price cars are tracked and stay visible: they are not
            # rejections, just cars that cannot be judged yet. An explicit
            # notify_on "unpriced" decides whether they alert; unset follows
            # require_price.
            want_unpriced = search_notify.get("unpriced")
            tell_me_about_unpriced = (
                not bool(search_filters.get("require_price"))
                if want_unpriced is None else bool(want_unpriced))
            for listing in unpriced:
                # As in the kept loop: a car the rules stop hiding may arrive
                # with no price on its card, and it still needs an
                # announcement or a reason.
                was_hidden = str((state.listings.get(listing.id) or {}).get(
                    "quiet_reason") or "").startswith(HIDDEN_REASON_PREFIX)
                change = state.record(listing)
                if change is None and was_hidden:
                    change = Change(Change.QUALIFIED, listing,
                                    new_price=listing.price)
                if change is None:
                    continue
                if change.kind == Change.NEW:
                    report.new += 1
                    if baseline:
                        silence(listing.id, "recorded as a starting point when "
                                            "this search's scope changed")
                    elif tell_me_about_unpriced and search_notify.get("new", True):
                        queue(change)
                    else:
                        # Seen, recorded, deliberately quiet - so it cannot
                        # come back as "new" once a price appears on it.
                        silence(listing.id, "no price published, and this "
                                            "search asks for a price")
                    archive_mod.archive_listing(listing, archive_conf, fetcher)
                elif change.kind == Change.PRICED:
                    # A car that was call-for-price now has a figure. That is
                    # worth hearing about whatever require_price says: it is
                    # the moment the car becomes judgeable.
                    report.priced += 1
                    if search_notify.get("priced", True):
                        queue(change)
                elif change.kind == Change.RELISTED:
                    report.relisted += 1
                    if (tell_me_about_unpriced and not baseline
                            and search_notify.get("relisted", False)):
                        queue(change)
                elif change.kind == Change.QUALIFIED:
                    report.qualified += 1
                    if baseline:
                        silence(listing.id, "recorded as a starting point when "
                                            "this search's scope changed")
                    elif tell_me_about_unpriced and search_notify.get("qualified", True):
                        queue(change)
                    else:
                        silence(listing.id, "it came back inside your rules "
                                            "but still has no price on it")

            # Held until every search has been read: recording a rejection now
            # would mark the car silenced before a later search that wants it
            # could announce it.
            for listing, verdict in dropped:
                rejected.setdefault(listing.id, (listing, verdict))

            # A car is only gone if the search was actually read. An unread
            # page proves nothing, and one that collapsed to a fraction of its
            # usual size far more often means a half-working parser than a
            # cleared lot.
            trustworthy = bool(listings) or said_no_results
            if (trustworthy and not report.first_run
                    and counted_the_same_way
                    and usual_count >= MIN_COUNT_FOR_COLLAPSE
                    and len(listings) * 2 < usual_count):
                trustworthy = False
                report.warnings.append(
                    f"{search.name}: only {len(listings)} of the usual "
                    f"{usual_count} listings were read, so removals are not "
                    f"being called this run.")
            # Held until every search has been read: a car missing from this
            # search may have been returned by another, and calling that a
            # sale would be wrong.
            removal_plan.append((search, trustworthy and not baseline,
                                 result.complete, search_notify))

        # Cars no search wanted. They still have to be recorded - a car you
        # hid by filter is still on the site, and forgetting it makes the next
        # run report it as removed - but quietly, and only now that every
        # search has had its say.
        for lid, (listing, verdict) in rejected.items():
            if lid in owned:
                continue
            why = verdict.reason
            change = state.record(listing, filtered=True, filter_reason=why,
                                  filter_rule=verdict.rule)
            # Changes on hidden cars are not announced but are counted: they
            # are still facts about the market.
            if change is not None and change.kind != Change.NEW:
                report.hidden_events[change.kind] = (
                    report.hidden_events.get(change.kind, 0) + 1)
            silence(lid, f"{HIDDEN_REASON_PREFIX}{why}")

        # Now that every search has had its say, drop the results none of
        # them was for: a car one search does not want may be another's.
        #
        # Stored rows get the same question, answered from the model already
        # recorded, with no requests. Only for searches read this run, and
        # only by the models rule, which says which car it is rather than
        # whether it is wanted.
        for search in searches:
            rules_now = cfg.rules_for(search)["filters"]
            if not any(filters.normalise_model(m)
                       for m in (rules_now.get("models") or [])):
                continue
            if search.id not in report.strategies:
                continue          # this search did not get read this run
            if search.id not in read_with_models:
                continue          # and its last read could not name a model
            if search.id in read_confined:
                # The site named its own results for this search, so which
                # stored cars belong to it is settled by the listing, not by
                # model matching. Any confined read counts, complete or not:
                # it still proves the site names its results.
                continue
            for lid, entry in state.listings.items():
                if entry.get("search_id") != search.id or lid in owned:
                    continue
                stored = Listing(id=lid, model=str(entry.get("model") or ""))
                if filters.check(stored, {"models": rules_now["models"]}).wrong_car:
                    wrong_car_ids.add(lid)

        # Every search has now been read and every car recorded, so the rows
        # exist to be stamped.
        stamped_at = utcnow()
        for lid in declared_seen:
            entry = state.listings.get(lid)
            if entry is not None:
                entry["in_results_at"] = stamped_at

        # Cars that were never in the search. A results page also carries
        # recommendation rails, and an unstamped stored row cannot tell those
        # cars from real results. Every listed result is stamped
        # `in_results_at`, so a stored car with no stamp that is absent from a
        # confined, complete read is dropped. Never a shortlisted car or one
        # with a message pending; a sale is still handled by the removal path.
        never_in_the_search: set[str] = set()
        for search in searches:
            if search.id not in read_completely_confined:
                continue
            for lid, entry in state.listings.items():
                if entry.get("search_id") != search.id or lid in owned:
                    continue
                if entry.get("in_results_at") or lid in seen_anywhere:
                    continue
                if str(entry.get("status") or "") != "active":
                    # A car already recorded as gone keeps that record;
                    # `prune` clears it on age.
                    continue
                never_in_the_search.add(lid)

        doomed = (wrong_car_ids | never_in_the_search) - owned
        # How many of them the user was alerted about, counted before the
        # rows go. `notified_at`, not `notified`: the flag is also set on
        # hidden cars, while the timestamp means a message actually went out.
        told = sum(1 for lid in doomed
                   if (state.listings.get(lid) or {}).get("notified_at"))
        discarded = state.discard_wrong_cars(doomed)
        if discarded:
            report.discarded = len(discarded)
            if never_in_the_search:
                # Say only what is known: an unstamped row may be a real
                # result that has since sold, so its alert is not called a
                # mistake.
                note = (f"dropped {_many(len(discarded), 'stored car')} the "
                        f"searches are not returning and have no record of "
                        f"ever having returned")
                if told:
                    note += (f", and you were sent an alert at the time about "
                             f"{_many(told, 'of them', 'of them')}")
            else:
                note = (f"dropped {_many(len(discarded), 'stored result')} that "
                        f"were never the car a search was for")
                if told:
                    note += (f", including {_many(told, 'car')} the bot had "
                             f"already told you about by mistake")
            report.warnings.append(note + ".")

        report.filtered_out = sum(
            1 for lid in seen_anywhere
            if (state.listings.get(lid) or {}).get("filtered"))

        for search, trustworthy, complete, search_notify in removal_plan:
            if trustworthy:
                # Absence from a partial read is not evidence, since the site
                # rotates which listings surface, and most of a watch vanishing
                # at once is more often a bad read than a cleared lot. Either
                # way, ask the listing pages before calling a sale.
                watched = [lid for lid, e in state.listings.items()
                           if e.get("search_id") == search.id
                           and e.get("status") == "active"]
                vanished = [lid for lid in watched if lid not in seen_anywhere]
                mass = (len(watched) >= MIN_COUNT_FOR_COLLAPSE
                        and len(vanished) > len(watched) * MASS_REMOVAL_FRACTION)
                if mass:
                    report.warnings.append(
                        f"{search.name}: {len(vanished)} of {len(watched)} watched "
                        f"cars are missing at once - checking their listing pages "
                        f"before calling any of them sold.")

                confirm = None
                if not complete or mass:
                    checked = [0]

                    def ask_the_listing_page(entry: dict[str, Any]) -> bool | None:
                        if checked[0] >= REMOVAL_CHECKS_PER_RUN:
                            return None       # ask again next run
                        checked[0] += 1
                        try:
                            live = _still_listed(str(entry.get("url") or ""), fetcher)
                        except BudgetExhausted:
                            return None
                        if live is None:
                            # Nobody could say. Ask again, but not forever: a
                            # car long absent whose page never reads is gone,
                            # not pending.
                            tries = int(entry.get("gone_checks", 0)) + 1
                            entry["gone_checks"] = tries
                            if tries < GONE_UNKNOWN_LIMIT:
                                return None
                            entry.pop("gone_checks", None)
                            entry.pop("gone_evidence", None)
                            return True
                        if live:
                            entry.pop("gone_evidence", None)
                            entry.pop("gone_checks", None)
                            return False
                        # The page says gone, but a listing URL's slug changes
                        # when the seller edits the ad, so one 404 may be a
                        # retitled car. It takes more than one run to believe.
                        seen = int(entry.get("gone_evidence", 0)) + 1
                        entry["gone_evidence"] = seen
                        if seen < GONE_EVIDENCE_NEEDED:
                            return None
                        entry.pop("gone_evidence", None)
                        return True

                    confirm = ask_the_listing_page

                # The removal grace is a length of time, not a count of runs:
                # the dedupe floor, read from the same setting, so a car cannot
                # be called gone faster than two real checks could establish.
                for change in state.mark_missing(
                        search.id, seen_anywhere, confirm=confirm,
                        grace_minutes=int(cfg.get("health.min_interval_minutes")
                                          or 0)):
                    report.removed += 1
                    if search_notify.get("removed", False):
                        queue(change)
            # Otherwise nothing happens at all - not even a miss. The grace
            # period exists to absorb a car dropping off one page, not to be
            # spent on a page we could not read.

        # ---- notify -------------------------------------------------
        # Anything an earlier run detected but could not deliver (quiet hours,
        # a channel outage) is picked back up here rather than being lost.
        if not dry_run:
            already = {c.listing.id for c in changes}
            held = [c for c in state.pending_changes() if c.listing.id not in already]
            if held:
                report.warnings.append(f"re-sending {_many(len(held), 'held alert')}")
                changes = held + changes

        # Small drops ride along with a new car, and only with one: they
        # follow the new cars in the next notification that has any, so the
        # phone never buzzes for a small drop alone.
        if not dry_run and any(c.kind in Change.NEW_TO_YOU for c in changes):
            already = {c.listing.id for c in changes}
            changes = changes + [c for c in state.ride_along_changes()
                                 if c.listing.id not in already]
        # One order for every channel, and the order the digest cap cuts in.
        changes = render.in_order(changes)

        report.quiet = notifiers.in_quiet_hours(settings)
        if changes and notify and not dry_run and not report.quiet:
            results = notifiers.dispatch(cfg, changes, report.to_dict(), env)
            report.notified = [str(r) for r in results]
            if any(r.ok for r in results):
                # Mark only the cars the message named. A capped digest ends
                # "...and N more", so the overflow stays owed and the next run
                # leads with it.
                told_about = changes[:max(0, int(
                    settings.get("max_listings_per_message", 12) or 0))] or changes
                state.mark_notified(c.listing.id for c in told_about)
                overflow = len(changes) - len(told_about)
                if overflow > 0:
                    # A rider the cap left out stays a rider. Deferring it
                    # would make it an ordinary owed alert, and it would buzz
                    # the phone on its own next run.
                    state.defer(c for c in changes[len(told_about):] if not c.rider)
            else:
                # Every channel failed. Hold on to them and try again next run.
                state.defer(c for c in changes if not c.rider)
            report.channel_results.extend(results)
            for result in results:
                if not result.ok and not result.skipped:
                    report.warnings.append(f"notification failed: {result}")
        elif changes and not dry_run and (report.quiet or not notify):
            state.defer(changes)
            report.warnings.append(
                _many(len(changes), "change") + " held"
                + (" until quiet hours end." if report.quiet else "."))
        elif changes and dry_run:
            report.notified = ["dry run: nothing sent"]

        # ---- first-run self-check ------------------------------------
        if report.first_run and assessments:
            report.validation_ok = all(a.trustworthy for a in assessments)
            report.validation_report = validate.report_markdown(
                assessments, ok=report.validation_ok)
            _publish_validation(report, assessments)
            if notify and not dry_run:
                subject = ("AutoTrader watcher is working"
                           if report.validation_ok
                           else "AutoTrader watcher: the first check looks wrong")
                results = notifiers.alert(
                    cfg, subject, validate.report_text(assessments, ok=report.validation_ok), env)
                report.channel_results.extend(results)
                report.warnings.append("sent the first-run check: "
                                       + ", ".join(str(r) for r in results))

        # ---- shape drift ---------------------------------------------
        if drifted and notify and not dry_run:
            body = "\n\n".join(shape.describe(name, reasons, current)
                                 for name, reasons, current in drifted)
            results = notifiers.alert(
                cfg, "AutoTrader changed how its pages are built", body, env)
            report.channel_results.extend(results)
            report.warnings.append("sent a page-shape warning: "
                                   + ", ".join(str(r) for r in results))

        # ---- where alerts go ----------------------------------------
        # A changed ntfy topic means everything now arrives somewhere the
        # phone on the other end is not subscribed to. That is silence that
        # looks exactly like "nothing happened", so it gets said out loud on
        # the channel that can still be reached.
        if not dry_run:
            topic = str(cfg.get("notifications.channels.ntfy.topic") or "")
            previous = str(state.data.get("ntfy_topic") or "")
            state.data["ntfy_topic"] = topic
            if previous and topic and previous != topic:
                report.warnings.append("alerts moved to a new ntfy topic - "
                                       "resubscribe from the dashboard's Status tab")
                if notify:
                    subject = "AutoTrader watcher: your alerts moved"
                    report.channel_results.extend(notifiers.alert(
                        cfg, subject, "Alerts arrive on this topic from now on.", env))
                    # The old topic is the one the phone is subscribed to, so
                    # it is told too - that they moved, never where to. It
                    # may be one other people can read.
                    report.channel_results.extend(
                        notifiers.alert(cfg, subject, provision.MOVED_NOTICE, env,
                                        notifiers=_old_topic(cfg, previous, env)))

        # ---- health -------------------------------------------------
        _health_check(cfg, state, report, env, blocked_searches,
                      int(health_conf.get("alert_after_failures", 3) or 0), notify and not dry_run)

        # ---- retire channels that cannot work ------------------------
        if notify and not dry_run:
            _retire_dead_channels(
                cfg, state, report, env,
                int(health_conf.get("disable_channel_after", 2) or 0))

        # ---- housekeeping -------------------------------------------
        if not dry_run:
            forgotten = state.forget_searches({s.id for s in cfg.searches})
            if forgotten:
                report.warnings.append(
                    f"forgot {len(forgotten)} search(es) no longer configured")
            pruned = archive_mod.prune(archive_conf)
            if pruned:
                report.warnings.append(f"pruned {_many(len(pruned), 'old archive folder')}")
            state.prune()

            # Our own copy of the photos, so the page works offline and a
            # delisted car still has a picture. Uses the same fetcher, so it
            # queues behind the same rate limit and the same budget; never
            # raises, because a photo is a nicety and a check is the job.
            if cfg.get("dashboard.photos", True):
                try:
                    shots = thumbs_mod.sync(state.listings.values(), fetcher)
                    report.photos = {
                        "kept": shots.kept, "fetched": shots.fetched,
                        "failed": shots.failed, "pruned": shots.pruned,
                        "bytes": shots.total_bytes, "samples": shots.samples[:6],
                    }
                    for note in shots.notes:
                        report.warnings.append(note)
                except Exception as exc:  # noqa: BLE001
                    log.warning("photo sync failed: %s", exc)

        # ---- does the bookkeeping still make sense? ------------------
        # The dangerous failures are silently wrong state rather than crashes,
        # so the run checks its own bookkeeping and fails if it does not add up.
        if not dry_run:
            try:
                payload = dashboard.build_payload(cfg, state, env)
            except Exception as exc:  # noqa: BLE001 - never the reason a run fails
                log.warning("could not build the dashboard payload: %s", exc)
                payload = None
            broken = invariants.check(cfg, state, report, payload, seen_anywhere)
            if broken:
                report.invariants = [str(v) for v in broken]
                written = invariants.write(broken, cfg, state)
                if written:
                    report.diagnostics.append(str(written))
                report.errors.append(
                    "the bot's own bookkeeping is inconsistent: "
                    + "; ".join(report.invariants[:3])
                    + (f" (+{len(broken) - 3} more)" if len(broken) > 3 else ""))

                # Alert at once: this is rare by construction and means the
                # bot's record of what it told the user is unreliable. Only
                # when the set of broken rules changes, so a persistent fault
                # is not repeated every run.
                fingerprint = ",".join(sorted({v.rule for v in broken}))
                if notify and state.data.get("invariants_told") != fingerprint:
                    state.data["invariants_told"] = fingerprint
                    report.channel_results.extend(notifiers.alert(
                        cfg, "AutoTrader watcher: its own records do not add up",
                        "The watcher checks its bookkeeping after every run and "
                        "this one did not hold:\n\n"
                        + "\n".join(f"- {line}" for line in report.invariants[:6])
                        + "\n\nUntil this is fixed, treat what it has and has "
                          "not told you as unreliable. The offending entries are "
                          "in diagnostics/invariants.json.", env))
            elif state.data.pop("invariants_told", None):
                report.warnings.append("the bookkeeping problem has cleared")


    finally:
        report.requests_made = getattr(fetcher, "spent", 0)
        if fetcher is not None:
            fetcher.close()
        if not dry_run:
            _write_the_run_down(cfg, state, report, env, notify)

    return report


def _write_the_run_down(cfg: Config, state: State, report: "RunReport",
                        env: dict[str, str], notify: bool) -> None:
    """Everything a finished run owes the ledger, however it finished.

    A run that stands down early still held a runner and proves its timer
    fired, so it is recorded like any other; `schedule_fired` counts on it.
    """
    # What GitHub says this repository is, so the minute ledger can tell
    # minutes that are merely spent from minutes that are charged. Absent
    # outside Actions, and absence means "assume charged" - see
    # budget.draws_on_the_allowance.
    visibility = (env or {}).get("REPO_VISIBILITY", "").strip()
    if visibility:
        state.data.setdefault("repo", {})["visibility"] = visibility
    # The runner label too: a larger runner is billed even on a public
    # repository, so "public" alone does not make a minute free.
    runner_label = (env or {}).get("REPO_RUNNER", "").strip()
    if runner_label:
        state.data.setdefault("repo", {})["runner"] = runner_label
    # What started this check, so coverage can tell a schedule doing its job
    # from somebody pushing to the repository.
    report.trigger = _trigger_of(env or {})
    # The interval this run was asked for, so coverage can be measured
    # against the schedule that was actually running.
    try:
        if state.note_schedule(
                int(cfg.get("health.expected_interval_minutes", 30) or 30)):
            log.info("the check interval changed; coverage restarts from now")
    except Exception as exc:   # noqa: BLE001
        log.warning("could not record the schedule: %s", exc)
    # What this run cost, before the run is written down, so a run that
    # crashed or stood down early still pays for the runner it held.
    try:
        report.budget = _charge_the_budget(cfg, state, report, env, notify)
    except Exception as exc:   # noqa: BLE001 - never fail a check over accounting
        log.warning("could not update the minute ledger: %s", exc)
    # This is the whole point: state is written even if something above blew
    # up, so a failure costs one run, never the entire history.
    state.record_run(report.to_dict())
    try:
        state.save()
    except OSError as exc:
        log.error("could not save state: %s", exc)
        report.errors.append(f"could not save state: {exc}")


# Which timer started this check, named so a person can find it. An outside
# repository_dispatch caller names itself in client_payload.from, and that
# name reaches the dashboard, so it is limited to lowercase letters, digits,
# dot and dash, at most 24 characters.
_TRIGGER_FROM = re.compile(r"[^a-z0-9.\-]+")


def _trigger_of(env: dict[str, str]) -> str:
    how = str(env.get("RUN_TRIGGER") or "").strip() or "manual"
    if how != "repository_dispatch":
        return how
    who = _TRIGGER_FROM.sub("", _caller_name(env).strip().lower())
    return f"{how}:{who[:24]}" if who else how


def _caller_name(env: dict[str, str]) -> str:
    """client_payload.from, read from the event file rather than passed in
    the step's environment, which Actions prints in its public log."""
    if env.get("RUN_TRIGGER_FROM"):
        return str(env["RUN_TRIGGER_FROM"])
    try:
        event = json.loads(Path(env.get("GITHUB_EVENT_PATH") or "").read_text(encoding="utf-8"))
        return str((event.get("client_payload") or {}).get("from") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def _charge_the_budget(cfg: Config, state: State, report: "RunReport",
                       env: dict[str, str], notify: bool) -> dict[str, Any]:
    """Bill this run to the month, and stop the bot if the month is spent.

    The stop is a file rather than an API call to disable a workflow: it is
    visible in the repository, needs no actions: write token, and is deleted
    by hand to resume. watch.yml reads it before installing anything. The
    check runs every time, so a new month clears it on its own.
    """
    from . import budget as budget_mod

    if not cfg.get("budget.enabled", True):
        return {}

    # Billed on the job's wall-clock life, not the run's working time: the
    # runner is charged from checkout to teardown.
    overhead = float(cfg.get("budget.job_overhead_seconds", 25) or 0)
    minutes = budget_mod.minutes_for(report.duration_s + overhead)
    verdict = budget_mod.record(state, minutes, cfg=cfg)
    verdict["this_run_minutes"] = minutes

    stop_file = Path(budget_mod.STOP_FILE)
    if verdict["should_stop"]:
        stop_file.write_text(
            verdict["text"] + "\n\n"
            f"Drawing: {verdict['drawing_minutes']:,.0f} minutes in "
            f"{verdict['month']} over "
            f"{verdict['days_elapsed']} days ({verdict['per_day']:,.1f}/day).\n"
            f"Allowance: {verdict['allowance']:,}. This bot stops at "
            f"{verdict['ceiling']:,.0f}.\n\n"
            "Delete this file to start checking again. It will be rewritten "
            "on the next run if the month is still over.\n",
            encoding="utf-8")
        report.warnings.append(verdict["text"])
        if notify and not state.data.get("budget_told") == verdict["month"]:
            notifiers.alert(cfg, "The watcher has stopped: this month's minutes are spent",
                            verdict["text"], env)
            state.data["budget_told"] = verdict["month"]
    else:
        if stop_file.exists():
            stop_file.unlink()
            report.warnings.append("the budget stop has cleared")
        state.data.pop("budget_told", None)
        if verdict["state"] == "over":
            report.warnings.append(verdict["text"])
            # Once per month, not once per run: a warning that arrives every
            # two hours for a fortnight is a warning nobody reads.
            told = state.data.get("budget_warned")
            if notify and told != verdict["month"]:
                notifiers.alert(cfg, "This month's runner minutes are heading over",
                                verdict["text"], env)
                state.data["budget_warned"] = verdict["month"]
        else:
            state.data.pop("budget_warned", None)
    return verdict


def _retire_dead_channels(cfg: Config, state: State, report: RunReport,
                          env: dict[str, str], threshold: int) -> None:
    """Switch off a channel whose credentials are being rejected.

    A wrong password is wrong every time, so retrying it every run only
    repeats the same error and buries the failures that matter.

    Judged once per run over every send attempted, not per message: a channel
    that fails an alert is as broken as one that fails a digest, and a quiet
    week should not keep a dead channel alive.
    """
    if not report.channel_results:
        return

    outcomes: dict[str, dict[str, Any]] = {}
    for result in report.channel_results:
        if result.skipped:
            continue
        entry = outcomes.setdefault(result.channel, {"ok": False, "permanent": False,
                                                     "detail": ""})
        if result.ok:
            entry["ok"] = True
        else:
            entry["detail"] = entry["detail"] or result.detail
            if result.permanent:
                entry["permanent"] = True

    alive = sorted(name for name, o in outcomes.items() if o["ok"])

    for channel, outcome in sorted(outcomes.items()):
        strikes = state.record_channel(channel, outcome["ok"], outcome["detail"],
                                       outcome["permanent"])
        if outcome["ok"] or threshold <= 0 or strikes < threshold:
            continue

        cfg.set(f"notifications.channels.{channel}.enabled", False)
        cfg.set(f"notifications.channels.{channel}.disabled_reason",
                f"Switched off automatically after {strikes} runs: "
                f"{outcome['detail'][:160]}")
        state.mark_channel_disabled(channel)
        report.disabled_channels.append(channel)
        try:
            cfg.save()
        except OSError as exc:
            log.warning("could not persist the disabled channel: %s", exc)

        # Say so once, through whatever still works. Deliberately not recorded
        # against the channels, or this notice would count as its own strike.
        if alive:
            notifiers.alert(
                cfg,
                f"Switched off {channel} notifications",
                f"{channel} has been rejecting our credentials:\n\n"
                f"  {outcome['detail'][:300]}\n\n"
                f"It failed that way {strikes} runs in a row, so it is now off "
                f"and will stop filling the log.\n\n"
                f"Still delivering via: {', '.join(alive)}.\n\n"
                f"To bring it back, fix the credentials and set "
                f"notifications.channels.{channel}.enabled to true in "
                f"config.json, or switch it on under the dashboard's Searches tab.",
                env)
        report.warnings.append(
            f"switched off {channel} after {strikes} runs of credential failures")


def _publish_validation(report: RunReport, assessments: list[validate.Assessment]) -> None:
    """Leave the first-run check where the user will find it.

    Only in files the vault seals - validation-report.md and the run's own
    output, which the workflow sends to run.log. Never the job summary: the
    report names the searches and samples real cars, and in a public
    repository GitHub shows the summary to anyone who opens the run.
    """
    try:
        Path("validation-report.md").write_text(report.validation_report, encoding="utf-8")
    except OSError as exc:
        log.warning("could not write validation-report.md: %s", exc)

    print("\n" + validate.report_text(assessments, ok=report.validation_ok) + "\n")


def _old_topic(cfg: Config, previous: str,
               env: dict[str, str]) -> list[notifiers.Notifier]:
    """A channel pointed at the topic the bot has just stopped using.

    Built from the current ntfy settings with the old topic put back, so the
    server, priority and auth are whatever they were - only the address
    changes. Returns nothing when ntfy is not the channel that moved, which
    is the only case this is for.
    """
    channel = dict(cfg.get("notifications.channels.ntfy") or {})
    if not channel or not previous:
        return []
    channel["topic"] = previous
    channel["enabled"] = True
    return [notifiers.NtfyNotifier(channel, env,
                                   cfg.get("notifications", {}) or {})]


def _health_check(cfg: Config, state: State, report: RunReport,
                  env: dict[str, str], blocked: list[str],
                  threshold: int, may_notify: bool) -> None:
    """Tell the user when the bot itself is broken.

    A watcher that stops watching without saying so is worse than no
    watcher at all: it can fail for a month before anybody notices.
    """
    if threshold <= 0 or not may_notify:
        return
    # Checks are not evenly spaced, so a failure count alone says nothing
    # about how long a search has been unreadable. Alert on enough failures
    # or long enough in the dark, and say how long.
    dark_after = float(cfg.get("health.silent_after_hours") or 0)
    from .insight import _span
    broken = []
    for search in cfg.active_searches:
        health = state.search_health(search.id)
        failures = int(health.get("consecutive_failures", 0))
        if failures <= 0:
            continue
        dark = clock.hours_since(health.get("last_ok"))
        long_enough = dark_after > 0 and dark is not None and dark >= dark_after
        if failures < threshold and not long_enough:
            continue
        when = (f"last read successfully {_span(dark)} ago"
                if dark is not None else "it has never been read successfully")
        broken.append(f"- {search.name}: {_many(failures, 'failed check')} in "
                      f"a row, {when}. "
                      f"Last error: {health.get('last_error') or 'unknown'}")
    if not broken:
        return
    if not (cfg.get("notifications.notify_on.errors", True)):
        return
    body = ("Your AutoTrader watcher cannot read one or more searches:\n\n"
            + "\n".join(broken))
    if blocked:
        body += ("\n\nautotrader.ca served an anti-bot page. Try increasing "
                 "scraping.delay_ms or running the bot less often.")
    if report.empty_parses:
        body += ("\n\nThe pages loaded but nothing could be read from them, which "
                 "usually means AutoTrader changed its markup. Run "
                 "'python -m autotrader doctor --live' to see which parser "
                 "strategies still work.")
    body += "\n\nNothing is lost - it will resume as soon as the pages load again."
    results = notifiers.alert(cfg, "AutoTrader watcher needs attention", body, env)
    report.channel_results.extend(results)
    report.warnings.append("sent a health alert: " + ", ".join(str(r) for r in results))
