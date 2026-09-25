"""Durable state: what we have seen, what it cost, and what changed.

A record per car, so price drops, removals and relistings can be told apart
from new arrivals. It is written atomically, and the runner saves it in a
``finally`` block, so a crash mid-run costs at most the work of that run
instead of re-announcing everything on the next pass.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from . import clock
from .listing import Listing

# The opening of every "a rule of yours hid it" quiet reason. A prefix rather
# than a flag, because the rest of the sentence names the rule and the
# dashboard and the ledger quote it verbatim.
HIDDEN_REASON_PREFIX = "hidden by your rules: "

log = logging.getLogger(__name__)

STATE_PATH = Path(os.getenv("AUTOTRADER_STATE", "state.json"))
MAX_RUN_HISTORY = 60
# Keys record() sets itself; everything else on an entry is carried forward.
_MANAGED_KEYS = {"first_seen", "last_seen", "status", "notified", "price_history",
                 "price_disputed"}
MAX_PRICE_POINTS = 40


def utcnow() -> str:
    """The ISO stamp this bot writes into state; see autotrader.clock."""
    return clock.stamp()



class Change:
    """Something worth telling the user about."""

    NEW = "new"
    PRICE_DROP = "price_drop"
    PRICE_RISE = "price_rise"
    PRICED = "priced"
    REMOVED = "removed"
    RELISTED = "relisted"
    # A known car that a rule hid and that now qualifies. It is neither new
    # nor a price drop the user saw, so this is the only moment they hear
    # about it.
    QUALIFIED = "qualified"

    #: The kinds that put a car in front of you that you have not been told
    #: about. They lead every notification, set its priority, and are what a
    #: small price drop waits for before it is sent.
    NEW_TO_YOU = (NEW, QUALIFIED, RELISTED)

    def __init__(self, kind: str, listing: Listing, *, old_price: int | None = None,
                 new_price: int | None = None, held_since: str | None = None,
                 rider: bool = False) -> None:
        self.kind = kind
        self.listing = listing
        self.old_price = old_price
        self.new_price = new_price
        #: When this was worked out, if it could not be delivered then. An
        #: alert held through quiet hours or an outage arrives late, so the
        #: renderers say how old it is.
        self.held_since = held_since
        #: A small price drop travelling with a new-listing notification.
        #: It must never become an ordinary owed alert, or it would be sent
        #: alone on the next run - exactly what holding it back avoids.
        self.rider = rider

    @property
    def delta(self) -> int | None:
        if self.old_price is None or self.new_price is None:
            return None
        return self.new_price - self.old_price

    @property
    def delta_pct(self) -> float | None:
        if not self.old_price or self.delta is None:
            return None
        return self.delta / self.old_price * 100.0

    def describe(self) -> str:
        if self.kind == Change.NEW:
            return "New listing"
        if self.kind in (Change.PRICE_DROP, Change.PRICE_RISE):
            delta = self.delta or 0
            arrow = "down" if delta < 0 else "up"
            pct = self.delta_pct or 0.0
            # Still produce a sentence if a figure is missing: a TypeError
            # here would lose every alert in the digest, not just this one.
            if self.old_price is None or self.new_price is None:
                return f"Price {arrow}" + (f" ${abs(delta):,}" if delta else "")
            return (f"Price {arrow} ${abs(delta):,} ({abs(pct):.1f}%) - "
                    f"${self.old_price:,} to ${self.new_price:,}")
        if self.kind == Change.PRICED:
            return (f"Price published - ${self.new_price:,}"
                    if isinstance(self.new_price, int) else "Price published")
        if self.kind == Change.REMOVED:
            return "Listing removed"
        if self.kind == Change.RELISTED:
            # A relisting at a different price says more than one at the same
            # price, which usually just means the listing expired.
            if self.delta:
                direction = "cheaper" if self.delta < 0 else "dearer"
                return (f"Back on the market ${abs(self.delta):,} {direction} "
                        f"- ${self.old_price:,} to ${self.new_price:,}")
            return "Back on the market"
        if self.kind == Change.QUALIFIED:
            return "Now within your rules"
        return self.kind

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "listing_id": self.listing.id,
            "old_price": self.old_price,
            "new_price": self.new_price,
            "delta": self.delta,
            "at": utcnow(),
        }


class State:
    def __init__(self, data: dict[str, Any] | None = None, path: Path = STATE_PATH) -> None:
        self.data: dict[str, Any] = data or {
            "version": 2, "updated_at": None, "listings": {}, "searches": {}, "runs": [],
        }
        self.data.setdefault("listings", {})
        self.data.setdefault("searches", {})
        self.data.setdefault("runs", [])
        self.path = path

    # ---------------- persistence ----------------

    @classmethod
    def load(cls, path: Path | str | None = None) -> "State":
        path = Path(path) if path else STATE_PATH
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8") or "{}")
                if isinstance(raw, dict) and raw.get("version"):
                    state = cls(raw, path)
                    state.upgrade()
                    return state
                log.warning("%s has no version marker; starting fresh", path)
            except json.JSONDecodeError as exc:
                # A crash here would re-notify everything, so keep the bad
                # file for inspection and start over.
                broken = path.with_suffix(".corrupt.json")
                try:
                    path.replace(broken)
                    log.error("%s was corrupt (%s); moved to %s", path, exc, broken)
                except OSError:
                    log.error("%s was corrupt and could not be moved: %s", path, exc)
        return cls(path=path)

    def upgrade(self) -> dict[str, int]:
        """Fill in bookkeeping that older entries were written without.

        Idempotent and conservative: it reconstructs implicit records, and
        never invents a delivery for a car that was never handled - those
        stay visible as violations.
        """
        filled = {"unpriced": 0, "quiet_reason": 0, "notified_at": 0}
        for entry in self.listings.values():
            if "unpriced" not in entry:
                entry["unpriced"] = entry.get("price") is None
                filled["unpriced"] += 1
            if entry.get("notified_at") or entry.get("quiet_reason") or entry.get("pending"):
                continue
            if entry.get("filtered"):
                why = entry.get("filter_reason") or "one of your rules"
                entry["quiet_reason"] = f"{HIDDEN_REASON_PREFIX}{why}"[:200]
                filled["quiet_reason"] += 1
            elif entry.get("notified"):
                # Handled, but with no record of whether it was delivered or
                # kept quiet: use last_seen, marked as reconstructed.
                entry["notified_at"] = entry.get("last_seen") or entry.get("first_seen")
                entry["notified_at_backfilled"] = True
                filled["notified_at"] += 1

        # Unmark anything since delivered for real. A reconstructed stamp
        # equals last_seen (or first_seen); any other stamp was written by a
        # run that saw itself send, so it is observed.
        for entry in self.listings.values():
            if not entry.get("notified_at_backfilled"):
                continue
            reconstructed = entry.get("last_seen") or entry.get("first_seen")
            if entry.get("notified_at") and entry["notified_at"] != reconstructed:
                entry.pop("notified_at_backfilled", None)
                filled["notified_at_observed"] = \
                    filled.get("notified_at_observed", 0) + 1
        return filled

    def save(self, path: Path | None = None) -> Path:
        """Write atomically so an interrupted run cannot corrupt state."""
        target = Path(path) if path else self.path
        self.data["updated_at"] = utcnow()
        self.data["version"] = 2
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, target)
        return target

    # ---------------- listings ----------------

    @property
    def listings(self) -> dict[str, dict[str, Any]]:
        return self.data["listings"]

    def known(self, listing_id: str) -> bool:
        return listing_id in self.listings

    def get_listing(self, listing_id: str) -> Listing | None:
        raw = self.listings.get(listing_id)
        return Listing.from_dict(raw) if raw else None

    def record(self, listing: Listing, *, seen_at: str | None = None,
               filtered: bool = False, filter_reason: str = "",
               filter_rule: str = "") -> Change | None:
        """Store a listing and return what changed about it, if anything.

        Returns a Change for a genuinely new car or a price move, and None when
        nothing noteworthy happened.
        """
        now = seen_at or utcnow()
        existing = self.listings.get(listing.id)

        if existing is None:
            entry = listing.to_dict()
            entry.update({
                "first_seen": now, "last_seen": now, "status": "active",
                "notified": False, "filtered": filtered,
                "filter_reason": filter_reason, "filter_rule": filter_rule,
                # A fact about the car, not the filters: it lets the dashboard
                # and the "price published" alert tell a first price from a drop.
                "unpriced": entry.get("price") is None,
                "price_history": ([{"at": now, "price": listing.price}]
                                  if listing.price is not None else []),
            })
            self.listings[listing.id] = entry
            return Change(Change.NEW, listing)

        old_price = existing.get("price")
        merged = Listing.from_dict(existing)
        merged.merge(listing)
        # Your own mark travels with the car, so a notifier can lead with a
        # shortlisted one without reading state.
        merged.shortlisted = bool((existing.get("you") or {}).get("shortlisted"))
        for field in ("price", "mileage_km", "title", "url", "images", "search_id",
                      "search_name", "source", "enriched", "card_price"):
            value = getattr(listing, field)
            if value not in (None, "", [], False):
                setattr(merged, field, value)

        entry = merged.to_dict()
        # to_dict() only knows Listing fields; carry across everything else on
        # the entry (a held notification, the miss counter, your own marks).
        for key, value in existing.items():
            if key not in entry and key not in _MANAGED_KEYS:
                entry[key] = value
        entry["first_seen"] = existing.get("first_seen") or now
        entry["last_seen"] = now
        entry["status"] = "active"
        # Seeing a car resets its miss counter, so earlier misses cannot carry
        # a reappearing car across the removal threshold.
        entry["misses"] = 0
        # A car written off as gone has come back. Recorded even without an
        # alert: frequent returns mean removal detection is wrong.
        came_back = existing.get("status") == "gone"
        if came_back:
            entry["relisted_at"] = now
        entry["filtered"] = filtered
        # Why it was hidden, so the dashboard can say so instead of just
        # showing a smaller number than the site does.
        entry["filter_reason"] = filter_reason
        # And which rule, as its config key, so the settings page can name the
        # exact rule hiding a car.
        entry["filter_rule"] = filter_rule
        was_unpriced = (existing.get("unpriced") if "unpriced" in existing
                        else existing.get("price") is None)
        entry.pop("removed_at", None)
        entry["notified"] = existing.get("notified", False)
        # An unfiltered car cannot keep a hidden-by-a-rule quiet reason; the
        # invariants fail the run on that contradiction. Clearing `notified`
        # too is deliberate: with nothing accounting for the car, the run must
        # announce it or record why it stays quiet.
        if not filtered and str(existing.get("quiet_reason") or "").startswith(
                HIDDEN_REASON_PREFIX):
            entry.pop("quiet_reason", None)
            entry["notified"] = False
        # A car that moved between a private seller and a dealer. Both values
        # must be known, since an empty seller type was simply not read.
        # Recorded, not alerted: it often precedes a price move, but is too
        # rare for a notification about it to be useful.
        was_kind = str(existing.get("seller_type") or "")
        now_kind = str(getattr(listing, "seller_type", "") or "")
        if was_kind and now_kind and was_kind != now_kind:
            entry["seller_changed_at"] = now
            entry["seller_was"] = was_kind

        # The first photos on a car that had none: the moment it can be
        # judged. Recorded, not alerted, because the car itself did not change.
        had_photos = bool(existing.get("images"))
        if not had_photos and listing.images:
            entry["photos_at"] = now

        # A hidden car crossing back into the rules. Recorded regardless of
        # other changes, so the dashboard can mark it even when a price drop
        # caused it.
        crossed_in = bool(existing.get("filtered")) and not filtered and not came_back
        if crossed_in:
            entry["qualified_at"] = now
            entry["qualified_from"] = str(existing.get("filter_reason") or "")[:200]
        history = list(existing.get("price_history") or [])

        change: Change | None = None
        old_source = existing.get("price_source") or ""
        new_source = listing.price_source or ""
        # A search card and a detail page can quote different figures for the
        # same car (taxes, incentives, "from" pricing), so a change is only
        # believed when both observations come from the same place.
        comparable = (old_price is None or not old_source or not new_source
                      or old_source == new_source)

        if listing.price is not None and listing.price != old_price and not comparable:
            # Keep the older, better-sourced figure until the detail page is
            # re-checked; the runner queues this listing first for enrichment.
            entry["price"] = old_price
            entry["price_source"] = old_source
            entry["price_disputed"] = listing.price
        elif listing.price is not None and listing.price != old_price:
            history.append({"at": now, "price": listing.price})
            entry.pop("price_disputed", None)
            if came_back:
                # Coming back at a different price is one event, and the
                # stronger one, so it is tested before the price-drop branch
                # (which would otherwise swallow the relisting).
                change = Change(Change.RELISTED, merged, old_price=old_price,
                                new_price=listing.price)
            elif old_price is not None:
                kind = Change.PRICE_DROP if listing.price < old_price else Change.PRICE_RISE
                change = Change(kind, merged, old_price=old_price, new_price=listing.price)
            elif was_unpriced:
                # A "call for price" car now has a figure. Not a price drop -
                # there is nothing to compare against - but the moment the car
                # can be judged.
                entry["priced_at"] = now
                change = Change(Change.PRICED, merged, new_price=listing.price)
        else:
            entry.pop("price_disputed", None)
            if not history and listing.price is not None:
                history.append({"at": now, "price": listing.price})
            if came_back:
                change = Change(Change.RELISTED, merged, old_price=old_price,
                                new_price=listing.price)

        # A car that crossed into the rules with no other change to report.
        # When a price drop caused the crossing, the drop already says it;
        # announcing both would list the car twice.
        if change is None and crossed_in:
            change = Change(Change.QUALIFIED, merged, old_price=old_price,
                            new_price=entry.get("price"))

        entry["price_history"] = history[-MAX_PRICE_POINTS:]
        # Set last, from the entry rather than this observation: a card with
        # no price does not make a car call-for-price when its listing page
        # gave one.
        entry["unpriced"] = entry.get("price") is None
        self.listings[listing.id] = entry
        return change

    def silence(self, listing_id: str, reason: str) -> None:
        """Record that we deliberately said nothing about this car, and why.

        Every listing ends up delivered, owed, or deliberately quiet with a
        reason, so "we never told you" is always a decision that can be read
        back, never an accident.
        """
        entry = self.listings.get(listing_id)
        if entry is None:
            return
        entry["quiet_reason"] = reason[:200]
        if entry.get("pending"):
            # Already owed an alert. Keeping quiet from now on does not cancel
            # it: a change detected while the car qualified is real news.
            return
        entry["notified"] = True

    def mark_notified(self, listing_ids: Iterable[str]) -> None:
        for lid in listing_ids:
            if lid in self.listings:
                self.listings[lid]["notified_at"] = utcnow()
                self.listings[lid].pop("quiet_reason", None)
                self.listings[lid]["notified"] = True
                self.listings[lid].pop("pending", None)
                self.listings[lid].pop("ride_along", None)
                # What you were just told this car costs. Later drops are
                # measured from here, so they are relative to the figure you
                # know, and a car sliding in small steps still reaches the bar.
                price = self.listings[lid].get("price")
                if price is not None:
                    self.listings[lid]["heard_price"] = price
                # This time was observed, not reconstructed.
                self.listings[lid].pop("notified_at_backfilled", None)

    def defer(self, changes: Iterable[Change]) -> None:
        """Remember an alert we could not send yet, so it is not lost.

        Quiet hours and a channel outage both need this: without it, a change
        detected once and not delivered would never be mentioned again, because
        the next run sees no change at all.
        """
        for change in changes:
            entry = self.listings.get(change.listing.id)
            if entry is None:
                continue
            entry["notified"] = False
            entry["pending"] = {"kind": change.kind, "old_price": change.old_price,
                                "new_price": change.new_price, "since": utcnow()}

    def heard_price(self, listing_id: str) -> int | None:
        """The price you were last told this car costs.

        Stored when a notification names the car. Without a stored value it is
        read off the price history as of the last notification, or, for a car
        never named (a starting-point car, or one a rule hid), it is the first
        price seen - the one the dashboard showed.
        """
        entry = self.listings.get(listing_id) or {}
        if entry.get("heard_price") is not None:
            return int(entry["heard_price"])
        history = [h for h in (entry.get("price_history") or [])
                   if h.get("price") is not None]
        told_at = str(entry.get("notified_at") or "")
        if told_at:
            before = [h for h in history if str(h.get("at") or "") <= told_at]
            if before:
                return int(before[-1]["price"])
        return int(history[0]["price"]) if history else None

    def hold_for_ride_along(self, listing_id: str, reason: str) -> None:
        """A drop too small to send alone, kept for the next new car.

        Written as a quiet car with a reason, because until it goes out that
        is what it is; the reason says it goes out with the next new car, so
        it does not read as decided against.
        """
        entry = self.listings.get(listing_id)
        if entry is None:
            return
        self.silence(listing_id, reason)
        entry["ride_along"] = {"since": utcnow()}

    def ride_along_changes(self) -> list[Change]:
        """Small drops still waiting, worked out against what you last heard.

        Recomputed rather than stored, so a car that went back up, sold, or
        was hidden by a rule in the meantime is not sent as a drop; those are
        cleared on the way past.
        """
        out: list[Change] = []
        for lid, entry in self.listings.items():
            if not entry.get("ride_along"):
                continue
            if entry.get("pending"):
                continue          # already owed an alert, which will name it
            heard = self.heard_price(lid)
            price = entry.get("price")
            live = (str(entry.get("status") or "") == "active"
                    and not entry.get("filtered"))
            if not live or heard is None or price is None or price >= heard:
                entry.pop("ride_along", None)
                continue
            out.append(Change(Change.PRICE_DROP, Listing.from_dict(entry),
                              old_price=heard, new_price=price, rider=True))
        return out

    def pending_changes(self) -> list[Change]:
        """Alerts that were worked out on an earlier run but never delivered."""
        out: list[Change] = []
        for entry in self.listings.values():
            pending = entry.get("pending")
            if not pending:
                continue
            out.append(Change(pending.get("kind", Change.NEW),
                              Listing.from_dict(entry),
                              old_price=pending.get("old_price"),
                              new_price=pending.get("new_price"),
                              held_since=pending.get("since")))
        return out

    def mark_missing(self, search_id: str, seen_ids: set[str],
                     *, grace_runs: int = 2, grace_minutes: int = 90,
                     confirm=None, now: datetime | None = None) -> list[Change]:
        """Flag listings from ``search_id`` that stopped appearing.

        A single results page can drop a car for reasons unrelated to a sale,
        so a car counts as gone only when at least ``grace_runs`` consecutive
        checks missed it and at least ``grace_minutes`` have passed since it
        was last seen. Runs alone are not enough because checks are unevenly
        spaced (a manual run can land seconds after a scheduled one); minutes
        alone are not enough because nothing may have looked in between. The
        default matches ``health.min_interval_minutes``, the closest two
        scheduled checks may be.

        Absence is only evidence when the whole result set was read. When a
        search has more results than the bot samples, the site rotates which
        listings surface, so ``confirm`` is asked about each candidate and
        answers True (gone), False (still listed) or None (unknown: ask again
        next run rather than guess).
        """
        changes: list[Change] = []
        now = now or clock.now()
        candidates: list[tuple[str, dict[str, Any]]] = []
        for lid, entry in self.listings.items():
            if entry.get("search_id") != search_id or entry.get("status") != "active":
                continue
            if lid in seen_ids:
                entry["misses"] = 0
                entry.pop("gone_evidence", None)
                entry.pop("gone_checks", None)
                continue
            misses = int(entry.get("misses", 0)) + 1
            entry["misses"] = misses
            if misses < grace_runs:
                continue

            # Held at the threshold, so the "grace-bounded" invariant
            # (misses <= grace_runs) holds while a car waits on the clock.
            entry["misses"] = max(grace_runs, 0)
            waited = clock.minutes_since(entry.get("last_seen"), now)
            if waited is not None and waited < grace_minutes:
                continue
            candidates.append((lid, entry))

        # Least-recently-asked first. `confirm` costs a request and the caller
        # caps how many it spends per run; state is saved with sorted keys, so
        # dictionary order would ask about the same few cars every run.
        candidates.sort(key=lambda pair: str(pair[1].get("gone_asked_at") or ""))
        for lid, entry in candidates:
            if confirm is None:
                verdict: bool | None = True
            else:
                verdict = confirm(entry)
                if verdict is not None:
                    # Stamped only on an answer, so a candidate the budget
                    # skipped keeps its place in the queue.
                    entry["gone_asked_at"] = utcnow()
            if verdict is False:
                # It fell out of the sample, not off the market. Recorded as a
                # confirmation, not a sighting: `last_seen` means "a search
                # returned this car", and days-on-sale is read from it.
                entry["misses"] = 0
                entry["confirmed_at"] = utcnow()
                entry.pop("gone_evidence", None)
                entry.pop("gone_checks", None)
                continue
            if verdict is None:
                # Hold at the threshold: the next run asks again rather than
                # announce an unconfirmed sale.
                entry["misses"] = grace_runs
                continue

            entry["status"] = "gone"
            entry["removed_at"] = utcnow()
            changes.append(Change(Change.REMOVED, Listing.from_dict(entry)))
        return changes

    # ---------------- run + search health ----------------

    def search_health(self, search_id: str) -> dict[str, Any]:
        return self.data["searches"].setdefault(
            search_id, {"consecutive_failures": 0, "last_ok": None,
                        "last_error": None, "last_count": 0, "last_strategy": None},
        )

    def record_search_ok(self, search_id: str, count: int, strategy: str) -> None:
        health = self.search_health(search_id)
        health.setdefault("first_ok", utcnow())
        health.update({"consecutive_failures": 0, "last_ok": utcnow(),
                       "last_error": None, "last_count": count,
                       "last_strategy": strategy})

    def record_search_error(self, search_id: str, error: str) -> int:
        health = self.search_health(search_id)
        health["consecutive_failures"] = int(health.get("consecutive_failures", 0)) + 1
        health["last_error"] = error[:500]
        health["last_error_at"] = utcnow()
        return health["consecutive_failures"]

    # ---------------- channel health ----------------

    def channel_health(self, name: str) -> dict[str, Any]:
        channels = self.data.setdefault("channels", {})
        return channels.setdefault(name, {"consecutive_failures": 0,
                                          "permanent_failures": 0,
                                          "last_error": None, "last_ok": None,
                                          "disabled_at": None})

    def record_channel(self, name: str, ok: bool, detail: str = "",
                       permanent: bool = False) -> int:
        """Track a channel's outcome. Returns consecutive permanent failures."""
        health = self.channel_health(name)
        if ok:
            health.update({"consecutive_failures": 0, "permanent_failures": 0,
                           "last_ok": utcnow(), "last_error": None})
            return 0
        health["consecutive_failures"] = int(health.get("consecutive_failures", 0)) + 1
        health["last_error"] = (detail or "")[:300]
        health["last_error_at"] = utcnow()
        if permanent:
            health["permanent_failures"] = int(health.get("permanent_failures", 0)) + 1
        else:
            # A transient error does not count towards giving up on a channel.
            health["permanent_failures"] = 0
        return int(health["permanent_failures"])

    def mark_channel_disabled(self, name: str) -> None:
        health = self.channel_health(name)
        health["disabled_at"] = utcnow()
        health["permanent_failures"] = 0

    # ---------------- page shape ----------------

    def search_shape(self, search_id: str) -> dict[str, Any] | None:
        return (self.data.get("searches", {}).get(search_id) or {}).get("shape")

    def record_shape(self, search_id: str, shape: dict[str, Any]) -> None:
        self.search_health(search_id)["shape"] = shape

    def record_run(self, summary: dict[str, Any]) -> None:
        summary = {"at": utcnow(), **summary}
        runs = self.data.get("runs", [])
        # The run log is a rolling window, so when the watch began is written
        # once here; the market layer needs it to tell floors from
        # measurements.
        if not self.data.get("watch_started"):
            oldest = min([r.get("at") for r in runs if r.get("at")]
                         + [summary["at"]])
            self.data["watch_started"] = oldest
        self.data["runs"] = ([summary] + runs)[:MAX_RUN_HISTORY]

    @property
    def runs(self) -> list[dict[str, Any]]:
        return self.data.get("runs") or []

    def record_change(self, *, ok: bool, text: str) -> None:
        """Log a change sent from the dashboard, applied or refused.

        Kept newest first; the page shows them so a refused change says why.
        """
        entry = {"at": utcnow(), "ok": bool(ok), "text": str(text)[:2000]}
        self.data["changes"] = ([entry] + (self.data.get("changes") or []))[:10]

    @property
    def watch_started(self) -> str | None:
        """When this bot first ran, as far as it can still tell."""
        if self.data.get("watch_started"):
            return self.data["watch_started"]
        ats = [r.get("at") for r in (self.data.get("runs") or []) if r.get("at")]
        return min(ats) if ats else None

    @property
    def watching_these_since(self) -> str | None:
        """When the current searches started producing data.

        ``watch_started`` is when the bot first ran, which differs once the
        watch list changes; "how long have we been watching" is measured
        from here.
        """
        firsts = [h.get("first_ok") for h in (self.data.get("searches") or {}).values()
                  if h.get("first_ok")]
        started = self.watch_started
        if not firsts:
            return started
        newest_watch = min(firsts)
        if started and started > newest_watch:
            return started
        return newest_watch

    @property
    def last_run(self) -> dict[str, Any] | None:
        runs = self.data.get("runs") or []
        return runs[0] if runs else None

    @property
    def last_check(self) -> dict[str, Any] | None:
        """The most recent run that actually read the searches.

        A firing that stands down because a check just happened is still
        recorded as a run - it proves the timer is alive - but it read
        nothing, so figures about the last check must not come from it.
        """
        for run in (self.data.get("runs") or []):
            if run.get("skipped"):
                continue
            ran = int(run.get("searches_run") or 0)
            failed = int(run.get("searches_failed") or 0)
            if ran and failed < ran:
                return run
            if not ran and run.get("ok"):
                return run      # an older record without those counters
        return None

    # ---------------- the schedule ----------------

    def note_schedule(self, interval_minutes: int) -> bool:
        """Remember when the check interval last changed.

        Coverage may only be measured while the current schedule was running;
        this stamp is what the coverage window is clamped to, so old runs are
        not counted against the new schedule's slots.
        """
        interval = max(1, int(interval_minutes or 0))
        current = self.data.get("schedule") or {}
        if int(current.get("interval_minutes") or 0) == interval:
            return False
        self.data["schedule"] = {"interval_minutes": interval,
                                 "since": utcnow(),
                                 "was": current.get("interval_minutes")}
        return True

    @property
    def schedule_changed_at(self) -> str | None:
        return (self.data.get("schedule") or {}).get("since")

    # ---------------- housekeeping ----------------

    def forget_searches(self, keep_ids: set[str]) -> list[str]:
        """Drop health rows for searches that are not configured.

        Without this, a search you removed keeps its last failure forever and
        shows up on the dashboard as permanently broken.
        """
        searches = self.data.get("searches") or {}
        gone = [sid for sid in searches if sid not in keep_ids]
        for sid in gone:
            del searches[sid]

        # Its cars stop being live too. They are not sold, but an active car
        # owned by a missing search is a listing nothing checks, and the run's
        # self-audit fails on it.
        released = 0
        for entry in self.listings.values():
            if entry.get("search_id") not in keep_ids and entry.get("status") == "active":
                entry["status"] = "gone"
                entry["removed_at"] = utcnow()
                entry["quiet_reason"] = "the search that was watching this was removed"
                entry["notified"] = True
                released += 1
        if released:
            log.info("released %d listing(s) from removed searches", released)
        return gone

    def forget_listings(self, keep_ids: set[str]) -> list[str]:
        """Drop the cars no configured search is watching.

        ``forget_searches`` retires them instead, which is the right default:
        a retired car is still a real record of the market. This is for a
        changed hunt (a different make or area), where the old cars would only
        clutter the dashboard and skew the market view.

        A car still owed an alert is never dropped.
        """
        doomed = [lid for lid, entry in self.listings.items()
                  if str(entry.get("search_id") or "") not in keep_ids
                  and not entry.get("pending")]
        for lid in doomed:
            del self.listings[lid]
        for sid in [s for s in (self.data.get("searches") or {}) if s not in keep_ids]:
            del self.data["searches"][sid]
        if doomed:
            log.info("forgot %d listing(s) no search watches", len(doomed))
        return doomed

    def discard_wrong_cars(self, listing_ids: Iterable[str]) -> list[str]:
        """Drop results that were never the car the search is for.

        Unlike ``forget_listings`` (cars whose search was deleted), these are
        cars the search never asked for, such as a different trim returned by
        a broad query. Keeping them would bury the hidden-by-a-rule count,
        fill the feed, and grow the state file and page without end.

        A car you have marked is never dropped. A car still owed an alert is
        dropped along with the alert, which would otherwise present it as a
        car you are watching.
        """
        gone: list[str] = []
        for lid in list(listing_ids):
            entry = self.listings.get(lid)
            if entry is None or entry.get("you"):
                continue
            del self.listings[lid]
            gone.append(lid)
        if gone:
            log.info("discarded %d stored results that no search returned",
                     len(gone))
        return gone

    def prune(self, *, keep_days: int = 730, keep_max: int = 5000) -> int:
        """Forget cars that went away a long time ago, so state stays small."""
        if keep_days <= 0 and keep_max <= 0:
            return 0
        removed = 0
        if keep_days > 0:
            # Zero means "no age limit", as it does for keep_max.
            cutoff = (clock.now()
                      - timedelta(days=keep_days)).isoformat()
            for lid, entry in list(self.listings.items()):
                if entry.get("status") != "gone" or entry.get("pending"):
                    continue          # never forget a car still owed an alert
                if (entry.get("removed_at") or entry.get("last_seen") or "") < cutoff:
                    del self.listings[lid]
                    removed += 1
        if keep_max and len(self.listings) > keep_max:
            # Only history is disposable: dropping a car still for sale would
            # make the next run re-announce it.
            ordered = sorted((kv for kv in self.listings.items()
                              if kv[1].get("status") == "gone"
                              and not kv[1].get("pending")),
                             key=lambda kv: kv[1].get("last_seen") or "")
            for lid, _ in ordered[: len(self.listings) - keep_max]:
                del self.listings[lid]
                removed += 1

        # Thin old price points: every observation for a month, then one a
        # week. The endpoints are always kept, since the history's numbers
        # depend on them.
        from . import insight
        for entry in self.listings.values():
            history = entry.get("price_history") or []
            if len(history) > 8:
                entry["price_history"] = insight.compact_history(history)
        return removed

    def stats(self) -> dict[str, Any]:
        # A car hidden by the user's own filters is not something they are
        # watching, so it does not count towards what is live.
        active = [e for e in self.listings.values()
                  if e.get("status") == "active" and not e.get("filtered")]
        priced = [e["price"] for e in active if e.get("price")]
        return {
            "total": len(self.listings),
            "active": len(active),
            "gone": sum(1 for e in self.listings.values() if e.get("status") == "gone"),
            "with_price": len(priced),
            "min_price": min(priced) if priced else None,
            "max_price": max(priced) if priced else None,
            "median_price": sorted(priced)[len(priced) // 2] if priced else None,
        }
