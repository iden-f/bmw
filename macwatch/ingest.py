"""Taking observations in: one transaction per query run.

A source adapter returns Listings; this module decides what they mean. It
stores the car, appends revisions and price events, keeps collection
membership, works out what changed (new, price drop, back on sale, gone,
now within the rules) and writes every notification decision to the outbox
in the same transaction, whether it is to be sent or suppressed. Workers
deliver after commit.

Rules reused from the bot: ``filters.check`` decides whether a car is within
the owner's rules, ``filters.is_significant_drop`` whether a drop is worth an
alert, and ``render`` writes the push text, so an alert from here reads like
one from GitHub, apart from its label.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from autotrader import clock, filters, render
from autotrader.listing import Listing
from autotrader.state import Change

from .db import Database, now

log = logging.getLogger("macwatch.ingest")

# Facts kept in facts_json rather than their own column: rarely queried.
FACT_FIELDS = ("body", "color", "transmission", "drivetrain", "fuel", "engine",
               "vin", "card_price", "enriched", "source", "search_name")
# Changes to these are a revision worth keeping.
TRACKED = ("title", "year", "make", "model", "trim", "price", "mileage_km",
           "location", "province", "seller", "seller_type", "url", "images")

# Consecutive complete reads a car must be missing from before it is gone.
MISSES_BEFORE_GONE = 2
# A Marketplace car unseen this long, while its searches read fine, is gone.
MARKETPLACE_GONE_AFTER_DAYS = 10
# More changes than this in one run go out as one digest, not a burst.
DIGEST_ABOVE = 3


@dataclass
class Collection:
    id: str
    name: str
    notify_mode: str = "alert"          # "alert" | "archive" | "paused"
    rules: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    site: str = "both"
    search_url: str = ""
    baseline_done_at: str | None = None
    baseline_marketplace_at: str | None = None

    @property
    def silent(self) -> bool:
        return self.notify_mode != "alert"

    def baseline_done(self, site: str) -> bool:
        return bool(self.baseline_marketplace_at if site == "marketplace"
                    else self.baseline_done_at)


def sync_collections(db: Database, cfg) -> list[Collection]:
    """Mirror the vault's searches as collections. Idempotent.

    A per-search ``notify`` key of "archive" (the sibling archive-mode work
    uses that spelling) makes a silent collection; missing means alert. A
    search switched off on the dashboard becomes a paused collection and
    keeps everything it saved.
    """
    out: list[Collection] = []
    stamp = now()
    raw_by_id = {str(s.get("id")): s for s in (cfg.data.get("searches") or [])
                 if isinstance(s, dict)}
    with db.tx() as conn:
        for search in cfg.searches:
            raw = raw_by_id.get(search.id, {})
            mode = str(raw.get("notify") or raw.get("notification_mode") or "alert").lower()
            if mode in ("silent", "archive-only", "archive_only"):
                mode = "archive"
            if mode not in ("alert", "archive"):
                mode = "alert"
            rules = cfg.rules_for(search)
            enabled = bool(search.enabled and search.url)
            site = "both" if search.marketplace else "autotrader"
            conn.execute(
                """INSERT INTO collections(id, name, site, notify_mode, enabled, rules_json,
                       search_url, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, site=excluded.site,
                       notify_mode=excluded.notify_mode, enabled=excluded.enabled,
                       rules_json=excluded.rules_json, search_url=excluded.search_url,
                       updated_at=excluded.updated_at""",
                (search.id, search.name, site, mode, int(enabled), json.dumps(rules),
                 search.url, stamp, stamp))
        pinned = [str(u).strip() for u in (cfg.get("pinned") or []) if str(u).strip()]
        if pinned:
            conn.execute(
                """INSERT INTO collections(id, name, site, notify_mode, enabled, rules_json,
                       search_url, created_at, updated_at)
                   VALUES('pinned','Saved listings','autotrader','archive',1,?,'',?,?)
                   ON CONFLICT(id) DO UPDATE SET rules_json=excluded.rules_json,
                       updated_at=excluded.updated_at""",
                (json.dumps({"urls": pinned}), stamp, stamp))
    for row in db.q("SELECT * FROM collections ORDER BY name"):
        out.append(_collection(row))
    return out


def _collection(row) -> Collection:
    return Collection(id=row["id"], name=row["name"], notify_mode=row["notify_mode"],
                      rules=json.loads(row["rules_json"] or "{}"),
                      enabled=bool(row["enabled"]), site=row["site"],
                      search_url=row["search_url"],
                      baseline_done_at=row["baseline_done_at"],
                      baseline_marketplace_at=row["baseline_marketplace_at"])


def collections(db: Database) -> list[Collection]:
    return [_collection(r) for r in db.q("SELECT * FROM collections ORDER BY name")]


# ------------------------------------------------------------------ runs

@dataclass
class Run:
    id: str
    site: str
    collection_id: str
    trigger: str = "schedule"
    started_at: str = field(default_factory=now)


def start_run(db: Database, site: str, collection_id: str, trigger: str = "schedule") -> Run:
    run = Run(id=uuid.uuid4().hex[:16], site=site, collection_id=collection_id, trigger=trigger)
    with db.tx() as conn:
        conn.execute("INSERT INTO query_runs(id, site, collection_id, trigger, started_at) "
                     "VALUES(?,?,?,?,?)", (run.id, site, collection_id, trigger, run.started_at))
    return run


def end_run(db: Database, run: Run, *, ok: bool, listings: int = 0, fresh: int = 0,
            complete: bool = False, confined: bool = False, pages: int = 0,
            requests: int = 0, strategy: str = "", error: str = "", note: str = "") -> None:
    with db.tx() as conn:
        conn.execute(
            """UPDATE query_runs SET ended_at=?, ok=?, listings=?, fresh=?, complete=?,
               confined=?, pages=?, requests=?, strategy=?, error=?, note=? WHERE id=?""",
            (now(), int(ok), listings, fresh, int(complete), int(confined), pages,
             requests, strategy, (error or "")[:400], (note or "")[:400], run.id))


# ---------------------------------------------------------------- ingest

@dataclass
class Summary:
    fresh: int = 0
    seen: int = 0
    hidden: int = 0
    wrong: int = 0
    gone: int = 0
    changes: list[Change] = field(default_factory=list)
    queued: int = 0          # outbox rows written as pending
    suppressed: int = 0      # outbox rows written as suppressed
    baseline: bool = False

    def line(self) -> str:
        bits = [f"{self.seen} seen", f"{self.fresh} new"]
        if self.hidden:
            bits.append(f"{self.hidden} hidden by a rule")
        if self.wrong:
            bits.append(f"{self.wrong} other models")
        if self.gone:
            bits.append(f"{self.gone} gone")
        if self.queued:
            bits.append(f"{self.queued} alert{'s' if self.queued != 1 else ''} queued")
        if self.suppressed:
            bits.append(f"{self.suppressed} suppressed")
        if self.baseline:
            bits.append("baseline: nothing announced")
        return ", ".join(bits)


def _facts(listing: Listing) -> dict[str, Any]:
    d = listing.to_dict()
    return {k: d.get(k) for k in FACT_FIELDS if d.get(k) not in (None, "", [], False)}


def _row_listing(row) -> Listing:
    """A stored row as a Listing, for comparison and rendering."""
    facts = json.loads(row["facts_json"] or "{}")
    return Listing(
        id=row["id"], url=row["url"], title=row["title"], year=row["year"],
        make=row["make"], model=row["model"], trim=row["trim"], price=row["price"],
        price_source=row["price_source"], card_price=facts.get("card_price"),
        currency=row["currency"], mileage_km=row["mileage_km"], location=row["location"],
        province=row["province"], seller=row["seller"], seller_type=row["seller_type"],
        body=facts.get("body") or "", color=facts.get("color") or "",
        transmission=facts.get("transmission") or "", drivetrain=facts.get("drivetrain") or "",
        fuel=facts.get("fuel") or "", engine=facts.get("engine") or "",
        vin=facts.get("vin") or "", images=json.loads(row["images_json"] or "[]"),
        search_name=facts.get("search_name") or "", source=facts.get("source") or "",
        enriched=bool(facts.get("enriched")),
    )


def _comparable_old_price(old: Listing, new: Listing) -> int | None:
    """The stored price a new observation may be compared with.

    A results card and a detail page routinely disagree by a few hundred
    dollars, so a card figure is compared with the last card figure and a
    detail figure with the last detail figure. Across sources, no alert.
    """
    if new.price_source == "search" and old.card_price is not None:
        return old.card_price
    if new.price_source == old.price_source or not old.price_source:
        return old.price
    if {new.price_source, old.price_source} == {"detail", "search"}:
        return None
    return old.price


def _announce_unpriced(rules: dict[str, Any]) -> bool:
    setting = (rules.get("notify_on") or {}).get("unpriced")
    if setting is None:
        return not bool((rules.get("filters") or {}).get("require_price"))
    return bool(setting)


def ingest(db: Database, run: Run, collection: Collection, listings: list[Listing], *,
           complete: bool, confined: bool, settings, cfg=None) -> Summary:
    """Take one query run's listings in. One transaction; never partial."""
    summary = Summary()
    stamp = now()
    rules = collection.rules or {}
    notify_on = dict(rules.get("notify_on") or {})
    drop_pct = float(rules.get("price_drop_min_pct", 1.0) or 0)
    drop_abs = int(rules.get("price_drop_min_abs", 0) or 0)
    site = run.site
    baseline = not collection.baseline_done(site)
    summary.baseline = baseline
    changes: list[tuple[Change, str, str]] = []   # change, suppress reason, event marker
    seen_ids: set[str] = set()

    # The models rule stands down when the page's model field is unreadable,
    # exactly as filters.apply does for the bot.
    rule_set = dict(rules.get("filters") or {})
    if rule_set.get("models") and not filters.model_is_readable(list(listings)):
        rule_set.pop("models")

    with db.tx() as conn:
        for listing in listings:
            verdict = filters.check(listing, rule_set)
            if verdict.wrong_car:
                summary.wrong += 1
                continue
            seen_ids.add(listing.id)
            summary.seen += 1
            qualified = verdict.keep or verdict.unpriced
            hidden_reason = "" if qualified else verdict.reason
            hidden_rule = "" if qualified else verdict.rule
            if not qualified:
                summary.hidden += 1
            row = conn.execute("SELECT * FROM listings WHERE id=?", (listing.id,)).fetchone()
            facts = _facts(listing)
            if row is None:
                summary.fresh += 1
                conn.execute(
                    """INSERT INTO listings(id, site, url, title, year, make, model, trim,
                           price, price_source, currency, mileage_km, location, province,
                           seller, seller_type, facts_json, images_json, status, first_seen,
                           last_seen, posted_at, qualified, hidden_reason, hidden_rule,
                           provenance)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?,?)""",
                    (listing.id, listing.site, listing.url, listing.title, listing.year,
                     listing.make, listing.model, listing.trim, listing.price,
                     listing.price_source, listing.currency, listing.mileage_km,
                     listing.location, listing.province, listing.seller,
                     listing.seller_type, json.dumps(facts), json.dumps(listing.images),
                     stamp, stamp, facts.pop("posted_at", None) if "posted_at" in facts else None,
                     int(qualified), hidden_reason, hidden_rule, f"run:{run.id}"))
                conn.execute("INSERT INTO listing_revisions(listing_id, at, run_id, changes_json, "
                             "provenance) VALUES(?,?,?,?,?)",
                             (listing.id, stamp, run.id,
                              json.dumps({"first": True, "price": listing.price,
                                          "mileage_km": listing.mileage_km}), site))
                if listing.price is not None:
                    conn.execute("INSERT INTO price_events(listing_id, at, price, currency, "
                                 "price_source, kind, run_id, provenance) VALUES(?,?,?,?,?,?,?,?)",
                                 (listing.id, stamp, listing.price, listing.currency,
                                  listing.price_source, "asking", run.id, site))
                _queue_media(conn, listing, settings, stamp)
                if qualified and (verdict.keep or _announce_unpriced(rules)):
                    changes.append((Change(Change.NEW, listing), "", "first"))
                else:
                    changes.append((Change(Change.NEW, listing),
                                    f"hidden: {hidden_reason}" if hidden_reason
                                    else "call for price, not announced", "first"))
            else:
                old = _row_listing(row)
                old_facts = json.loads(row["facts_json"] or "{}")
                was_qualified = bool(row["qualified"])
                was_status = row["status"]
                diff: dict[str, Any] = {}
                merged = Listing.from_dict(old.to_dict())
                # New facts win where present; blanks never erase.
                for name in TRACKED:
                    new_value = getattr(listing, name)
                    if new_value in (None, "", []):
                        continue
                    if name == "images" and len(new_value) < len(getattr(merged, name) or []):
                        continue
                    if getattr(merged, name) != new_value:
                        diff[name] = {"from": getattr(merged, name), "to": new_value}
                        setattr(merged, name, new_value)
                for name in ("body", "color", "transmission", "drivetrain", "fuel",
                             "engine", "vin", "seller"):
                    new_value = getattr(listing, name)
                    if new_value and getattr(merged, name) != new_value:
                        setattr(merged, name, new_value)
                new_facts = dict(old_facts)
                new_facts.update(_facts(listing))
                if listing.price_source == "search" and listing.price is not None:
                    new_facts["card_price"] = listing.price
                merged.price_source = listing.price_source or old.price_source
                merged.enriched = old.enriched or listing.enriched

                # Price semantics: like with like.
                comparable = _comparable_old_price(old, listing)
                price_change: Change | None = None
                if listing.price is not None and comparable is None and old.price is None:
                    price_change = Change(Change.PRICED, merged, new_price=listing.price)
                elif listing.price is not None and comparable is not None \
                        and listing.price != comparable:
                    if listing.price < comparable:
                        price_change = Change(Change.PRICE_DROP, merged,
                                              old_price=comparable, new_price=listing.price)
                    else:
                        price_change = Change(Change.PRICE_RISE, merged,
                                              old_price=comparable, new_price=listing.price)
                # A detail-page price is the better figure, so a later card
                # does not overwrite it; the card goes to card_price instead.
                card_only = (listing.price_source == "search" and old.price_source == "detail"
                             and old.card_price is None)
                if card_only:
                    merged.price, merged.price_source = old.price, old.price_source
                    diff.pop("price", None)
                moved = (comparable is not None and listing.price is not None
                         and listing.price != comparable) or \
                        (old.price is None and listing.price is not None)
                if moved and not card_only:
                    conn.execute("INSERT INTO price_events(listing_id, at, price, currency, "
                                 "price_source, kind, run_id, provenance) VALUES(?,?,?,?,?,?,?,?)",
                                 (listing.id, stamp, listing.price, listing.currency,
                                  listing.price_source, "asking", run.id, site))
                elif listing.price is None and old.price is not None \
                        and listing.price_source == old.price_source:
                    # A price withdrawn is a fact too; the old one stays shown.
                    conn.execute("INSERT INTO price_events(listing_id, at, price, currency, "
                                 "price_source, kind, run_id, provenance) VALUES(?,?,?,?,?,?,?,?)",
                                 (listing.id, stamp, None, listing.currency,
                                  listing.price_source, "withdrawn", run.id, site))
                    diff["price"] = {"from": old.price, "to": None, "withdrawn": True}
                if diff:
                    conn.execute("INSERT INTO listing_revisions(listing_id, at, run_id, "
                                 "changes_json, provenance) VALUES(?,?,?,?,?)",
                                 (listing.id, stamp, run.id, json.dumps(diff, default=str), site))
                status = "active"
                relisted = was_status in ("unavailable", "not_seen_recently", "source_marked_sold")
                conn.execute(
                    """UPDATE listings SET url=?, title=?, year=?, make=?, model=?, trim=?, price=?,
                           price_source=?, currency=?, mileage_km=?, location=?, province=?,
                           seller=?, seller_type=?, facts_json=?, images_json=?, status=?,
                           last_seen=?, gone_at=NULL, misses=0, qualified=?, hidden_reason=?,
                           hidden_rule=?, revision=revision+?
                       WHERE id=?""",
                    (merged.url, merged.title, merged.year, merged.make, merged.model,
                     merged.trim, merged.price if merged.price is not None else old.price,
                     merged.price_source, merged.currency, merged.mileage_km, merged.location,
                     merged.province, merged.seller, merged.seller_type,
                     json.dumps(new_facts), json.dumps(merged.images), status, stamp,
                     int(qualified), hidden_reason, hidden_rule, 1 if diff else 0, listing.id))
                if diff.get("images") or len(merged.images) > len(old.images):
                    _queue_media(conn, merged, settings, stamp)

                if relisted and was_status != "not_seen_recently":
                    changes.append((Change(Change.RELISTED, merged, old_price=old.price,
                                           new_price=merged.price),
                                    "" if qualified else f"hidden: {hidden_reason}", stamp[:10]))
                elif qualified and not was_qualified and verdict.keep:
                    changes.append((Change(Change.QUALIFIED, merged), "", stamp[:10]))
                if price_change is not None and card_only:
                    price_change = None
                if price_change is not None:
                    why = ""
                    if qualified and not was_qualified and verdict.keep:
                        why = "announced as now within your rules"
                    elif not qualified:
                        why = f"hidden: {hidden_reason}"
                    elif price_change.kind == Change.PRICE_DROP and not filters.is_significant_drop(
                            price_change.old_price or 0, price_change.new_price or 0,
                            drop_pct, drop_abs):
                        why = "small drop, below your threshold"
                    changes.append((price_change, why, str(price_change.new_price)))

            # Membership and the sighting.
            conn.execute(
                """INSERT INTO collection_listings(collection_id, listing_id, matched_at,
                       last_seen, qualified, reason)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(collection_id, listing_id) DO UPDATE SET last_seen=excluded.last_seen,
                       qualified=excluded.qualified, reason=excluded.reason""",
                (collection.id, listing.id, stamp, stamp, int(qualified), hidden_reason))
            conn.execute("INSERT INTO sightings(listing_id, collection_id, run_id, at, price) "
                         "VALUES(?,?,?,?,?)", (listing.id, collection.id, run.id, stamp,
                                              listing.price))

        # Cars of this collection that this read did not show.
        summary.gone = _mark_missing(conn, run, collection, seen_ids, changes, stamp,
                                     complete=complete, confined=confined)

        # Every notification decision, in the same transaction.
        summary.changes = [c for c, _, _ in changes]
        _write_outbox(conn, db, run, collection, changes, notify_on, settings, stamp,
                      baseline=baseline, summary=summary, cfg=cfg)

        if baseline:
            column = "baseline_marketplace_at" if site == "marketplace" else "baseline_done_at"
            conn.execute(f"UPDATE collections SET {column}=? WHERE id=?", (stamp, collection.id))
            if site == "marketplace":
                collection.baseline_marketplace_at = stamp
            else:
                collection.baseline_done_at = stamp
    return summary


def _mark_missing(conn, run: Run, collection: Collection, seen: set[str],
                  changes: list, stamp: str, *, complete: bool, confined: bool) -> int:
    """Absence from a complete, confined AutoTrader read counts against a
    car; absence from a partial one is not evidence. Marketplace reads are
    never complete, so there a car goes after a quiet fortnight or when the
    page says sold."""
    gone = 0
    rows = conn.execute(
        """SELECT l.* FROM listings l JOIN collection_listings m ON m.listing_id=l.id
           WHERE m.collection_id=? AND l.site=? AND l.status IN ('active','not_seen_recently')""",
        (collection.id, run.site)).fetchall()
    for row in rows:
        if row["id"] in seen:
            continue
        if run.site == "marketplace":
            days = (clock.minutes_since(row["last_seen"]) or 0) / 1440.0
            if days < MARKETPLACE_GONE_AFTER_DAYS:
                continue
            new_status = "unavailable"
        else:
            if not (complete and confined):
                continue
            misses = int(row["misses"]) + 1
            new_status = "unavailable" if misses >= MISSES_BEFORE_GONE else "not_seen_recently"
            conn.execute("UPDATE listings SET misses=? WHERE id=?", (misses, row["id"]))
        if new_status == "unavailable":
            conn.execute("UPDATE listings SET status='unavailable', gone_at=? WHERE id=?",
                         (stamp, row["id"]))
            conn.execute("INSERT INTO listing_revisions(listing_id, at, run_id, changes_json, "
                         "provenance) VALUES(?,?,?,?,?)",
                         (row["id"], stamp, run.id,
                          json.dumps({"status": {"from": row["status"], "to": "unavailable"}}),
                          run.site))
            changes.append((Change(Change.REMOVED, _row_listing(row)), "", stamp[:10]))
            gone += 1
        elif row["status"] != new_status:
            conn.execute("UPDATE listings SET status=? WHERE id=?", (new_status, row["id"]))
    return gone


def _queue_media(conn, listing: Listing, settings, stamp: str) -> None:
    limit = int(getattr(settings, "photos_per_listing", 12) or 0)
    if limit <= 0:
        return
    for position, url in enumerate((listing.images or [])[:limit]):
        if not str(url).startswith("https://"):
            continue
        conn.execute(
            """INSERT INTO listing_media(listing_id, source_url, position, status, next_try_at,
                   queued_at) VALUES(?,?,?,'pending',?,?)
               ON CONFLICT(listing_id, source_url) DO NOTHING""",
            (listing.id, url, position, stamp, stamp))


# ---------------------------------------------------------------- outbox

def _payload(changes: list[Change], settings, listing: Listing) -> dict[str, Any]:
    """What the notification will say, fixed at decision time."""
    label = str(getattr(settings, "ntfy_label", "") or "")
    app = str(getattr(settings, "app_url", "") or "").rstrip("/")
    lead = render.in_order(changes)[0]
    car = lead.listing
    click = f"{app}/#/listing/{car.id}" if app else (car.url or "")
    photo = (car.images or [""])[0] if (len(changes) == 1 or lead.kind in Change.NEW_TO_YOU) else ""
    buttons: list[dict[str, Any]] = []
    link = render.short_link(car).strip()
    if link.startswith(("http://", "https://")):
        buttons.append({"action": "view", "label": "Open ad", "url": link, "clear": True})
    if len(changes) > 1 and app:
        buttons.append({"action": "view", "label": f"All {len(changes)} in the app",
                        "url": f"{app}/#/feed", "clear": True})
    tags = {Change.PRICE_DROP: "chart_with_downwards_trend", Change.NEW: "car",
            Change.PRICED: "label", Change.RELISTED: "arrows_counterclockwise",
            Change.QUALIFIED: "unlock", Change.PRICE_RISE: "chart_with_upwards_trend",
            Change.REMOVED: "ghost"}.get(lead.kind, "car")
    if any(c.kind in Change.NEW_TO_YOU for c in changes):
        priority = "high"
    elif all(c.kind in (Change.PRICE_RISE, Change.REMOVED) for c in changes):
        priority = "low"
    else:
        priority = "default"
    return {"title": (label + render.push_title(changes))[:200],
            "body": render.as_push(changes, limit=12), "click": click, "attach": photo,
            "tags": tags, "priority": priority, "actions": buttons[:3],
            "kinds": [c.kind for c in changes], "listing_ids": [c.listing.id for c in changes]}


def _write_outbox(conn, db: Database, run: Run, collection: Collection,
                  changes: list[tuple[Change, str, str]], notify_on: dict[str, Any],
                  settings, stamp: str, *, baseline: bool, summary: Summary, cfg=None) -> None:
    muted = {r["listing_id"] for r in conn.execute(
        "SELECT listing_id FROM marks WHERE muted=1").fetchall()}
    sendable: list[tuple[Change, str]] = []
    rows: list[tuple[str, Change, str, str]] = []     # key, change, status, reason
    for change, why, marker in changes:
        key = f"{change.kind}:{change.listing.id}:{marker}"
        if change.kind == Change.PRICE_RISE and not notify_on.get("price_rise", False):
            why = why or "price rises are switched off"
        elif change.kind == Change.REMOVED and not notify_on.get("removed", False):
            why = why or "removals are switched off"
        elif change.kind == Change.RELISTED and not notify_on.get("relisted", False):
            why = why or "relistings are switched off"
        elif change.kind == Change.PRICED and not notify_on.get("priced", True):
            why = why or "price-published alerts are switched off"
        elif change.kind == Change.NEW and not notify_on.get("new", True):
            why = why or "new-listing alerts are switched off"
        elif change.kind == Change.PRICE_DROP and not notify_on.get("price_drop", True):
            why = why or "price-drop alerts are switched off"
        elif change.kind == Change.QUALIFIED and not notify_on.get("qualified", True):
            why = why or "now-qualifies alerts are switched off"
        if not why and collection.silent:
            why = "archive-only collection"
        if not why and collection.notify_mode == "paused":
            why = "collection paused"
        if not why and change.listing.id in muted:
            why = "muted by you"
        if not why and baseline:
            why = "baseline read: what was already for sale"
        if not why and not str(getattr(settings, "ntfy_topic", "") or "").strip():
            why = "no ntfy topic configured"
        if why:
            rows.append((key, change, "suppressed", why))
        else:
            sendable.append((change, key))
    if len(sendable) > DIGEST_ABOVE:
        digest_key = f"digest:{run.id}"
        payload = _payload([c for c, _ in sendable], settings, sendable[0][0].listing)
        conn.execute(
            """INSERT INTO outbox(event_key, listing_id, collection_id, kind, status, reason,
                   payload_json, created_at, next_try_at)
               VALUES(?,?,?,?,'pending','',?,?,?) ON CONFLICT(event_key) DO NOTHING""",
            (digest_key, sendable[0][0].listing.id, collection.id, "digest",
             json.dumps(payload), stamp, stamp))
        summary.queued += 1
        for change, key in sendable:
            rows.append((key, change, "bundled", f"sent in digest {digest_key}"))
    else:
        for change, key in sendable:
            payload = _payload([change], settings, change.listing)
            cur = conn.execute(
                """INSERT INTO outbox(event_key, listing_id, collection_id, kind, status, reason,
                       payload_json, created_at, next_try_at)
                   VALUES(?,?,?,?,'pending','',?,?,?) ON CONFLICT(event_key) DO NOTHING""",
                (key, change.listing.id, collection.id, change.kind, json.dumps(payload),
                 stamp, stamp))
            if cur.rowcount:
                summary.queued += 1
    for key, change, status, reason in rows:
        payload = {"title": render.push_title([change]), "kinds": [change.kind],
                   "listing_ids": [change.listing.id]}
        cur = conn.execute(
            """INSERT INTO outbox(event_key, listing_id, collection_id, kind, status, reason,
                   payload_json, created_at)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(event_key) DO NOTHING""",
            (key, change.listing.id, collection.id, change.kind, status, reason[:300],
             json.dumps(payload), stamp))
        if cur.rowcount and status == "suppressed":
            summary.suppressed += 1
