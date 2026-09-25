"""Publishing the data the dashboard reads.

The dashboard is a static page with no build step and no server. It loads
``data.json`` from beside itself, which this module writes after every run.
Only non-sensitive fields are exported.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from . import clock
from .archive import size_report
from . import geo, insight, qr, thumbs
from . import budget
from .config import CHANNEL_SECRETS, Config
from .listing import name_of
from .parser import STRATEGIES
from .state import State
from .urls import describe_search

log = logging.getLogger(__name__)

# The parser strategies in order, so the dashboard can show which ones are
# missing, not only which one won.
STRATEGY_ORDER = tuple(name for name, _ in STRATEGIES)

DOCS_DIR = Path("docs")
DATA_FILE = DOCS_DIR / "data.json"

# Fields the dashboard renders. Everything else stays out of the published file.
LISTING_FIELDS = (
    "id", "url", "title", "year", "make", "model", "trim", "price", "currency",
    "mileage_km", "location", "province", "seller", "body", "color",
    "transmission", "drivetrain", "fuel", "engine", "images", "search_id",
    "search_name", "first_seen", "last_seen", "status", "price_history",
    "price_source", "filtered", "filter_reason", "filter_rule", "unpriced", "enriched",
    # When each change happened, so the feed is derived from the listing
    # rather than stored a second time.
    "removed_at", "relisted_at", "priced_at", "qualified_at", "qualified_from",
    "photos_at", "seller_changed_at", "seller_was", "misses", "seller_type",
    # Whether an alert went out and, if not, why: every silence is a
    # recorded decision.
    "notified_at", "quiet_reason",
    # The owner's marks on a car (shortlisted, dismissed, muted, a note). A
    # static page cannot write, so the page sends these as a change file.
    "you",
)


def _area_of(filters: dict[str, Any]) -> dict[str, Any] | None:
    """The distance rule, resolved, so the page can say what is enforced."""
    near = str((filters or {}).get("near") or "").strip()
    try:
        radius = int((filters or {}).get("max_distance_km") or 0)
    except (TypeError, ValueError):
        radius = 0
    provinces = [str(p).strip().upper()
                 for p in ((filters or {}).get("provinces") or []) if str(p).strip()]
    if not near and not radius and not provinces:
        return None
    out: dict[str, Any] = {"provinces": provinces}
    if near and radius:
        out.update(geo.summary(near, radius))
    elif near:
        out["reference"] = near
        out["text"] = f"near {near}"
    elif radius:
        out["radius_km"] = radius
        out["text"] = f"within {radius:,} km"
    if provinces and "text" not in out:
        out["text"] = "in " + ", ".join(provinces)
    return out


def _subscribe_qr(server: str, topic: str) -> str:
    """An SVG QR code for the topic's address, or "" if there is no topic.

    Never fatal: the dashboard builds without it.
    """
    if not topic:
        return ""
    try:
        return qr.encode(f"{server}/{topic}", level="M").to_svg(
            dark="currentColor",
            label=f"Subscribe to {topic} on ntfy")
    except Exception as exc:  # noqa: BLE001 - decoration must never stop a build
        log.warning("could not draw the subscribe QR code: %s", exc)
        return ""


def build_payload(cfg: Config, state: State, env: dict[str, str] | None = None
                  ) -> dict[str, Any]:
    """Assemble everything the dashboard needs, with nothing secret in it."""
    # Normalised once: the local UI server and the dashboard command pass None.
    env = env or {}
    limit = int(cfg.get("dashboard.max_listings", 500) or 500)

    # Where distance is measured from, per search. A search with no distance
    # rule has no reference, so its cars carry no distance at all.
    references: dict[str, Any] = {}
    reference_names: dict[str, str] = {}
    for search in cfg.searches:
        near = str((cfg.rules_for(search)["filters"] or {}).get("near") or "").strip()
        if not near:
            continue
        point = geo.locate_reference(near)
        if point:
            references[search.id] = point
            reference_names[search.id] = near

    photo_index = thumbs._load_index()

    listings: list[dict[str, Any]] = []
    for entry in state.listings.values():
        # Filtered cars are published too, flagged, so the page can show what
        # the rules hid and why. They stay out of the default counts and lists.
        item = {k: entry.get(k) for k in LISTING_FIELDS if k in entry}
        # The published name can differ from the recorded one; see
        # listing.name_of.
        item["title"] = name_of(entry)
        item["filtered"] = bool(entry.get("filtered"))
        item["unpriced"] = entry.get("price") is None
        item["price_history"] = (entry.get("price_history") or [])[-20:]
        item["is_new"] = False
        if item["filtered"]:
            # A hidden car only needs to be listed and explained, so its
            # photos and most of its price history are left out to keep the
            # published file small.
            item.pop("images", None)
            item["price_history"] = item["price_history"][-2:]
        history = item["price_history"]
        if len(history) >= 2 and history[0].get("price") and history[-1].get("price"):
            item["price_change"] = history[-1]["price"] - history[0]["price"]
        # Comparison signals, computed here where the full record is available.
        item["photo_count"] = len(entry.get("images") or [])
        # Local photo copies, when held. The page tries these first so it
        # works offline; the remote URLs remain as a fallback.
        kept = thumbs.locals_for(entry.get("id"), photo_index)
        if kept:
            item["thumb"] = kept[0]
            # Only locally held photos, so the gallery never hotlinks the CDN.
            item["thumbs"] = kept
        item["per_1000km"] = insight.per_1000km(entry.get("price"),
                                                entry.get("mileage_km"))
        # When the ratio is withheld, the reason why; an unknown mileage is
        # only one of them.
        item["per_1000km_why"] = insight.per_1000km_withheld(
            entry.get("price"), entry.get("mileage_km"))
        age = clock.hours_since(entry.get("first_seen"))
        if age is not None:
            item["days_listed"] = int(age // 24)
        reference = references.get(entry.get("search_id") or "")
        here = (geo.locate(entry.get("location"), entry.get("province"))
                if reference else None)
        # A car that cannot be placed carries no distance rather than a
        # guessed one; the radius rule never excludes an unplaceable car.
        if reference and here:
            item["distance_km"] = round(geo.distance_km(here, reference))
            item["distance_from"] = reference_names.get(
                entry.get("search_id") or "")
        listings.append(item)

    listings.sort(key=lambda item: (not item.get("filtered"),
                                    item.get("first_seen") or "", item.get("id")),
                  reverse=True)
    # Count everything, then publish what fits. The cap limits page weight
    # only; counting after it would understate the totals, the hidden count
    # most of all, since the sort puts hidden cars last.
    everything = list(listings)
    listings = everything[:limit]
    left_out = len(everything) - len(listings)

    def counts_for(search_id: str | None = None) -> dict[str, int]:
        rows = [l for l in everything
                if search_id is None or l.get("search_id") == search_id]
        live = [l for l in rows if l.get("status") == "active"]
        return {
            "active": sum(1 for l in live if not l.get("filtered")),
            "filtered": sum(1 for l in live if l.get("filtered")),
            "unpriced": sum(1 for l in live if l.get("unpriced")
                            and not l.get("filtered")),
            "gone": sum(1 for l in rows if l.get("status") == "gone"),
            "total": len(rows),
        }

    searches = []
    for search in cfg.searches:
        health = (state.data.get("searches") or {}).get(search.id, {})
        summary = describe_search(search.url)
        searches.append({
            "id": search.id, "name": search.name, "url": search.url,
            "enabled": search.enabled, "notes": search.notes,
            "summary": summary.to_dict(),
            "health": {
                "last_ok": health.get("last_ok"),
                "last_error": health.get("last_error"),
                "last_count": health.get("last_count", 0),
                "last_strategy": health.get("last_strategy"),
                "consecutive_failures": health.get("consecutive_failures", 0),
                # Reading the site but keeping nothing: not a failure, though
                # it looks like one from the outside.
                "shut_out": health.get("shut_out"),
                # How much of the last read was a different car altogether.
                # Beside last_count, this tells "the market is quiet" apart
                # from "this link has become too broad".
                "not_this_car": int(health.get("not_this_car") or 0),
            },
            "active_count": counts_for(search.id)["active"],
            "counts": counts_for(search.id),
            "rules": {
                "filters": search.filters,
                "notify_on": search.notify_on,
                "price_drop_min_pct": search.price_drop_min_pct,
                "price_drop_min_abs": search.price_drop_min_abs,
            },
            # Spelled out separately: the bot enforces the distance rule
            # itself, so the search link alone does not show it.
            "area": _area_of(cfg.rules_for(search)["filters"]),
            "shape": health.get("shape"),
        })

    channels = {}
    for name, status in cfg.channel_status(env).items():
        channels[name] = {
            "label": status["label"], "free": status["free"], "help": status["help"],
            "required": status["required"], "setting": status["setting"],
            # Only whether a secret is present, never its value.
            "missing": status["missing"], "active": status["active"],
            # Why a channel was switched off, when the config records it, so a
            # disabled channel reads as a decision rather than a fault.
            "disabled_reason": str(
                (cfg.get(f"notifications.channels.{name}", {}) or {})
                .get("disabled_reason") or ""),
        }

    # The bot's own health, so "is a strategy failing?", "did a channel die?"
    # and "is a search being read?" are answered on the page.
    runs = (state.data.get("runs") or [])
    strategies: dict[str, Any] = {}
    for search in cfg.searches:
        shape = ((state.data.get("searches") or {}).get(search.id, {}) or {}).get("shape") or {}
        scores = shape.get("scores") or {}
        strategies[search.id] = {
            "name": search.name,
            "winner": shape.get("strategy"),
            "scores": scores,
            "working": shape.get("working") or [],
            "of": len(STRATEGY_ORDER),
            "order": list(STRATEGY_ORDER),
            "markers": shape.get("markers") or {},
        }

    # The last check, not the last run: shape drift, requests spent and
    # diagnostics come only from a run that read the site, and a firing that
    # stood down carries none of them.
    last = state.last_check or state.last_run or {}
    ok_streak = 0
    for run in runs:
        if not run.get("ok"):
            break
        ok_streak += 1

    # Every car is delivered, owed, or deliberately quiet. Anything else is a
    # car that mattered and was never mentioned.
    accounted = {"delivered": 0, "queued": 0, "quiet": 0, "unexplained": 0}
    for entry in state.listings.values():
        if entry.get("pending"):
            accounted["queued"] += 1
        elif entry.get("notified_at"):
            accounted["delivered"] += 1
        elif entry.get("quiet_reason"):
            accounted["quiet"] += 1
        else:
            accounted["unexplained"] += 1

    health = {
        "counts": counts_for(),
        # Cars counted above but left out of the payload by the size cap, so
        # the page can say so and its numbers still add up.
        "left_out": left_out,
        "accounted": accounted,
        "strategies": strategies,
        "drift": last.get("shape_drift") or [],
        "ok_streak": ok_streak,
        "runs_kept": len(runs),
        "budget": {"used": last.get("requests_made", 0),
                   "limit": int(cfg.get("scraping.request_budget", 0) or 0),
                   "exhausted": bool(last.get("budget_exhausted"))},
        "pending": sum(1 for e in state.listings.values() if e.get("pending")),
        "diagnostics": last.get("diagnostics") or [],
    }

    ntfy = cfg.get("notifications.channels.ntfy", {}) or {}
    topic = str(ntfy.get("topic") or "").strip()
    server = str(ntfy.get("server") or "https://ntfy.sh").rstrip("/")

    return {
        "generated_at": clock.now().isoformat(timespec="seconds"),
        # Where alerts go, so the dashboard can show it. An ntfy topic is a
        # destination, not a credential, but anyone with it can subscribe,
        # so the published page carries it only inside the encrypted payload.
        "notify": {
            "ntfy_topic": topic,
            "ntfy_url": f"{server}/{topic}" if topic else "",
            # The same address as a QR code, so a phone subscribes by camera
            # instead of typing the topic. Built here so it always matches the
            # topic, and inlined so it works on a page opened from the cache.
            "ntfy_qr": _subscribe_qr(server, topic),
            "active": [n for n, c in channels.items() if c["active"]],
        },
        "version": 3,
        # Where a change from the dashboard is committed.
        "repo": _repo_slug(env),
        # The last few changes sent from the dashboard, and what became of them.
        "changes": state.data.get("changes") or [],
        "stats": state.stats(),
        "listings": listings,
        "searches": searches,
        "channels": channels,
        "channel_catalog": {k: {"label": v["label"], "free": v["free"],
                                "required": v["required"], "help": v["help"]}
                            for k, v in CHANNEL_SECRETS.items()},
        "runs": (state.data.get("runs") or [])[:30],
        "channel_health": {
            name: {"last_ok": h.get("last_ok"),
                   "last_error": h.get("last_error"),
                   "disabled_at": h.get("disabled_at"),
                   "consecutive_failures": h.get("consecutive_failures", 0)}
            for name, h in (state.data.get("channels") or {}).items()
        },
        "last_run": state.last_run,
        # The last run that actually read the site (see State.last_check).
        # The page asks both "is the timer alive" and "how old is this data",
        # which have different answers.
        "last_check": state.last_check,
        "health": health,
        # Derived from the listings, not the run counters, which count only
        # cars that pass the filters; changes on hidden cars are included.
        "events": insight.events(state.listings.values()),
        "comparables": insight.comparables(state.listings.values()),
        "coverage": dict(
            insight.coverage(
                runs, int(cfg.get("health.expected_interval_minutes", 30) or 30),
                since_change=state.schedule_changed_at),
            # The threshold the silence alarm uses, so the page and the alarm
            # agree on when a check is overdue.
            silent_after_hours=float(cfg.get("health.silent_after_hours") or 0)),
        "cost": insight.minutes_spent(runs),
        # What the month has cost and where that is heading, shown on the
        # page the owner already reads.
        "budget": budget.load(state, cfg).verdict(clock.now()),
        # Figures across all listings together. It carries the time window it
        # covers, which can be older than the latest check.
        "market": insight.market(state.listings.values(), runs=state.runs,
                                 since=state.watching_these_since),
        "score_check": insight.backtest(state.listings.values()),
        "archive": size_report(),
        "config": _safe_config(cfg),
    }


# Keys whose *value* would be a credential. Key names are matched, not the
# whole document: the payload legitimately contains strings such as
# "TELEGRAM_BOT_TOKEN" and "...bot<TOKEN>/getUpdates" in its help text.
_SECRET_KEY_RE = re.compile(
    r"(token|password|secret|api[_-]?key|credential|auth)", re.I)

# Shapes of real credentials, in case one arrives under an innocent key. The
# first group is what this bot handles; the second is what GitHub push
# protection rejects. A token pasted into a free-text field would be published
# and could block every later push, so it is refused here with the field named.
_SECRET_VALUE_RES = (
    re.compile(r"\b\d{8,}:[A-Za-z0-9_-]{30,}\b"),                 # Telegram bot token
    re.compile(r"https://discord(?:app)?\.com/api/webhooks/\d+/"),  # Discord webhook
    re.compile(r"https://hooks\.slack\.com/services/T[A-Z0-9]+/"),  # Slack webhook
    re.compile(r"\bSK[0-9a-f]{32}\b"),                             # Twilio key
    re.compile(r"\bAC[0-9a-f]{32}\b"),                             # Twilio account SID

    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),                 # GitHub token
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),               # GitHub fine-grained
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),              # Slack token
    re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}\b"),        # AWS access key
    re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"),                      # Google API key
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b"),            # OpenAI-style key
    re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{24,}\b"),          # Stripe
)


def find_secrets(node: Any, path: str = "") -> list[str]:
    """Locate anything credential-shaped in a payload bound for a public page.

    Returns human-readable locations, empty when the payload is clean.
    """
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            where = f"{path}.{key}" if path else str(key)
            if (_SECRET_KEY_RE.search(str(key)) and isinstance(value, str)
                    and value.strip()):
                found.append(f"{where} holds a non-empty value")
            found.extend(find_secrets(value, where))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(find_secrets(value, f"{path}[{index}]"))
    elif isinstance(node, str):
        for pattern in _SECRET_VALUE_RES:
            if pattern.search(node):
                found.append(f"{path} looks like a credential")
                break
    return found


def _repo_slug(env: dict[str, str] | None) -> str:
    """owner/name for this repository, if it can be worked out."""
    env = env or {}
    slug = str(env.get("GITHUB_REPOSITORY") or "").strip()
    if slug:
        return slug
    try:
        import subprocess
        url = subprocess.run(["git", "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, Exception):  # noqa: BLE001 - absence is an answer
        return ""
    match = re.search(r"github\.com[:/]([^/]+/[^/.\s]+)", url or "")
    return match.group(1) if match else ""


def _safe_config(cfg: Config) -> dict[str, Any]:
    """The editable settings, minus anything that could hold a secret."""
    data = json.loads(json.dumps(cfg.data))
    for name, channel in (data.get("notifications", {}).get("channels", {}) or {}).items():
        if isinstance(channel, dict):
            channel.pop("token", None)
            channel.pop("password", None)
    return data


def write(cfg: Config, state: State, env: dict[str, str] | None = None,
          path: Path = DATA_FILE) -> Path | None:
    """Write data.json for the dashboard.  Returns the path, or None if off."""
    if not cfg.get("dashboard.enabled", True):
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_payload(cfg, state, env)
    # This file is published to a public URL. Nothing that looks like a
    # credential may leave here, whatever ended up in config.json.
    leaks = find_secrets(payload)
    if leaks:
        raise ValueError(
            "refusing to write the dashboard: credential-shaped data at "
            + "; ".join(leaks[:5])
            + ". Move it to an environment variable or GitHub secret.")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    stamp_worker(path.parent)
    return path


SW_FILE = "sw.js"
SW_WATCHES = ("index.html", "app.js")
_BUILD_LINE = re.compile(r"^const BUILD = '([^']*)';$", re.M)


def stamp_worker(docs: Path) -> str | None:
    """Write the build id the service worker keys its caches on.

    A browser installs a new service worker only when the bytes of sw.js
    change, and a new worker re-fetches everything it caches, so this is the
    one reliable way an installed app picks up a changed page or script. The
    stamp is derived from the tracked files on every publish, so it never has
    to be bumped by hand.
    """
    worker = docs / SW_FILE
    try:
        source = worker.read_text(encoding="utf-8")
    except OSError:
        return None

    digest = hashlib.sha256()
    for name in SW_WATCHES:
        try:
            digest.update((docs / name).read_bytes())
        except OSError:
            digest.update(b"missing")
    # The worker's own source, with the stamp line removed so hashing it does
    # not depend on the last stamp and change on every single publish.
    digest.update(_BUILD_LINE.sub("", source).encode("utf-8"))
    build = digest.hexdigest()[:12]

    updated = _BUILD_LINE.sub(f"const BUILD = '{build}';", source, count=1)
    if updated == source:
        return build          # already stamped with this build
    try:
        worker.write_text(updated, encoding="utf-8")
    except OSError as exc:
        log.warning("could not stamp %s: %s", worker, exc)
        return None
    return build
