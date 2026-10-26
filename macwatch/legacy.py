"""One-time import of the GitHub watcher's state into the archive.

Every car the bot has on record arrives with its first and last sighting,
its price history as the bot kept it (capped at 40 points there, so what is
missing cannot be invented), its membership and your marks. Provenance says
where each row came from. Running it again adds nothing twice.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from autotrader import clock
from autotrader.listing import Listing, on_marketplace

from .db import Database, now
from .ingest import FACT_FIELDS

log = logging.getLogger("macwatch.legacy")

PROVENANCE = "legacy:github-state"


def import_state(db: Database, state: dict[str, Any], settings=None) -> dict[str, int]:
    """Import ``state`` (the bot's state.json as a dict). Idempotent."""
    counts = {"listings": 0, "price_events": 0, "memberships": 0, "marks": 0, "skipped": 0}
    entries = (state or {}).get("listings") or {}
    known_collections = {r["id"] for r in db.q("SELECT id FROM collections")}
    stamp = now()
    with db.tx() as conn:
        for listing_id, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            if conn.execute("SELECT 1 FROM listings WHERE id=?", (listing_id,)).fetchone():
                counts["skipped"] += 1
                continue
            listing = Listing.from_dict({**entry, "id": str(listing_id)})
            facts = {k: entry.get(k) for k in FACT_FIELDS
                     if entry.get(k) not in (None, "", [], False)}
            facts["legacy"] = {k: entry.get(k) for k in
                               ("notified", "notified_at", "filtered", "filter_reason",
                                "quiet_reason", "status", "gone_reason", "unpriced")
                               if k in entry}
            first = str(entry.get("first_seen") or stamp)
            last = str(entry.get("last_seen") or first)
            status = str(entry.get("status") or "active")
            gone_at = None
            if status != "active":
                status, gone_at = "unavailable", str(entry.get("gone_at") or last)
            qualified = not bool(entry.get("filtered"))
            conn.execute(
                """INSERT INTO listings(id, site, url, title, year, make, model, trim, price,
                       price_source, currency, mileage_km, location, province, seller,
                       seller_type, facts_json, images_json, status, first_seen, last_seen,
                       gone_at, qualified, hidden_reason, hidden_rule, provenance)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (listing.id, "marketplace" if on_marketplace(listing.id) else "autotrader",
                 listing.url, listing.title, listing.year, listing.make, listing.model,
                 listing.trim, listing.price, listing.price_source, listing.currency,
                 listing.mileage_km, listing.location, listing.province, listing.seller,
                 listing.seller_type, json.dumps(facts, default=str),
                 json.dumps(listing.images), status, first, last, gone_at, int(qualified),
                 str(entry.get("filter_reason") or "") if not qualified else "",
                 str(entry.get("filter_rule") or "") if not qualified else "", PROVENANCE))
            counts["listings"] += 1
            conn.execute("INSERT INTO listing_revisions(listing_id, at, run_id, changes_json, "
                         "provenance) VALUES(?,?,?,?,?)",
                         (listing.id, first, None, json.dumps({"imported": True}), PROVENANCE))
            seen_points: set[tuple[str, Any]] = set()
            for point in entry.get("price_history") or []:
                if not isinstance(point, dict):
                    continue
                at = str(point.get("at") or first)
                price = point.get("price")
                if (at, price) in seen_points:
                    continue
                seen_points.add((at, price))
                conn.execute("INSERT INTO price_events(listing_id, at, price, currency, "
                             "price_source, kind, run_id, provenance) VALUES(?,?,?,?,?,?,?,?)",
                             (listing.id, at, price, listing.currency or "CAD",
                              str(point.get("source") or listing.price_source or ""),
                              "asking", None, PROVENANCE))
                counts["price_events"] += 1
            search_id = str(entry.get("search_id") or "")
            if search_id in known_collections:
                conn.execute(
                    """INSERT INTO collection_listings(collection_id, listing_id, matched_at,
                           last_seen, qualified, reason) VALUES(?,?,?,?,?,?)
                       ON CONFLICT(collection_id, listing_id) DO NOTHING""",
                    (search_id, listing.id, first, last, int(qualified),
                     str(entry.get("filter_reason") or "") if not qualified else ""))
                counts["memberships"] += 1
            if entry.get("shortlisted") or entry.get("note") or entry.get("dismissed"):
                conn.execute(
                    """INSERT INTO marks(listing_id, shortlisted, muted, dismissed, note, updated_at)
                       VALUES(?,?,?,?,?,?) ON CONFLICT(listing_id) DO NOTHING""",
                    (listing.id, int(bool(entry.get("shortlisted"))), 0,
                     int(bool(entry.get("dismissed"))), str(entry.get("note") or ""), stamp))
                counts["marks"] += 1
            if settings is not None and listing.images:
                from .ingest import _queue_media
                _queue_media(conn, listing, settings, stamp)
        conn.execute("INSERT INTO kv(key, value) VALUES('legacy_import', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (json.dumps({"at": stamp, **counts}),))
    return counts
