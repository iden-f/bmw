"""Seeing what the collector sees, without it sending anything.

``explain`` reads every search the way a pass does, opens no listing page,
sends nothing, and says where each car went and why: another model, hidden
by a rule (and which), or kept. It sorts them with the bot's own code
(marketplace.judge), so what it prints is what the bot would do.

``--capture`` also keeps what each page received, in this Mac's own folder
and never in the clone, and writes an outline of it: which fields a listing
carries and a few example values, with names, ids and addresses masked. The
outline is what to share when a field comes out empty; the raw files stay
here.

``test-alert`` sends one alert the way the bot does, to the channels this
Mac can reach without the repository's secrets: in practice, ntfy.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from autotrader import clock
from autotrader import marketplace as M

from . import settings as S
from .browser import Browser
from .cycle import Keyring, read
from .github import GitHub


def explain(settings: S.Settings, *, github: GitHub | None = None,
            browser_factory: Callable[[S.Settings], Browser] = Browser,
            capture: bool = False) -> dict[str, Any]:
    """Read every search once, send nothing, and sort what was read."""
    github = github or GitHub(settings.repo, S.secret(S.TOKEN))
    _, cfg = Keyring(github, S.secret(S.PASSPHRASE)).config()
    plan = M.plan(cfg)
    by_id = {s.id: s for s in cfg.active_searches}
    out: dict[str, Any] = {"session": "ok", "searches": [], "capture": None}
    if not plan:
        out["note"] = "the watch has no active search to read"
        return out
    pages: list[dict[str, Any]] | None = [] if capture else None
    with browser_factory(settings).open() as browser:
        if not browser.signed_in():
            out["session"] = "signed_out"
            return out
        # A throwaway memory: explaining must not change which cars the next
        # real pass thinks it has already seen.
        parts, out["session"] = read(cfg, plan, browser, settings, {},
                                     details=False, pages=pages)
    owned: set[str] = set()
    for item, part in zip(plan, parts):
        search = by_id.get(item["search"])
        if search is None:
            continue
        judged = M.judge(cfg, search, part["listings"], item, owned)
        owned.update(l.id for l in judged.kept + judged.unpriced)
        out["kept_anywhere"] = owned
        out["searches"].append({
            "name": search.name, "queries": [q["query"] for q in item["queries"]],
            "landed": part.get("landed", ""), "ok": part["ok"], "error": part["error"],
            "records": {M.MARKETPLACE_PREFIX + r["id"]: r for r in part["listings"]},
            "judged": judged})
    if pages is not None:
        out["capture"] = save_capture(pages)
    return out


# ------------------------------------------------------------ the words

def _where(path: str) -> str:
    return re.sub(r"^https?://[^/]+", "", path or "").split("?")[0] or "?"


def _car(listing, rec: dict[str, Any], why: str = "") -> str:
    """One car on one line, with every field the bot reads from it."""
    bits = [listing.title or "(no title)"]
    fb = " / ".join(str(rec[k]) for k in ("make", "model", "trim") if rec.get(k))
    if fb:
        bits.append(f"Facebook says {fb}")
    bits.append(f"model read as {listing.model or 'nothing'}")
    bits.append(listing.price_text)
    if listing.mileage_km is not None:
        bits.append(("~" if rec.get("mileage_rounded") else "") + listing.mileage_text)
    place = ", ".join(p for p in (listing.location, listing.province) if p)
    bits.append(place or "no place")
    if listing.seller_type:
        bits.append(listing.seller_type)
    bits.append("photo" if listing.images else "no photo")
    if rec.get("created"):
        days = (datetime.now(timezone.utc).timestamp() - int(rec["created"])) // 86400
        bits.append(f"listed {int(days)}d ago")
    if rec.get("pending"):
        bits.append("sale pending")
    line = " | ".join(bits)
    return f"      {line}" + (f"\n        -> {why}" if why else "")


def explain_text(result: dict[str, Any]) -> str:
    lines: list[str] = []
    if result.get("note"):
        return result["note"]
    if result["session"] != "ok":
        lines += [f"Facebook: {result['session']}. Run collector/run login, sign in, "
                  f"close the window, and try again.", ""]
    if result["searches"]:
        lines += ["Each car is judged from the search results alone. A real pass also",
                  "opens the page of a car on your list once, and what it finds there",
                  "(the exact kilometres, a rebuilt title) can still hide it.", ""]
    kept_anywhere = result.get("kept_anywhere") or set()
    for s in result["searches"]:
        j: M.Judged = s["judged"]
        b = j.breakdown(kept_anywhere)
        recs = s["records"]
        lines.append(f"{s['name']}")
        lines.append(f"  asked Marketplace for: {', '.join(s['queries'])}"
                     f"  (landed on {_where(s['landed'])})")
        if not s["ok"]:
            lines.append(f"  COULD NOT READ: {s['error']}")
            lines.append("")
            continue
        lines.append(f"  read {b['read']} for sale (+{b['sold']} sold): "
                     f"{b['other_models']} another model, {b['hidden']} hidden by a rule, "
                     f"{b['kept']} on your list, {b['elsewhere']} kept by another search")
        if j.other:
            lines.append(f"  another model ({len(j.other)}): "
                         + ", ".join(f"{n} x{c}" for n, c in j.other_examples(20)))
            for listing, _ in j.other:
                lines.append(_car(listing, recs.get(listing.id, {})))
        elsewhere = j.elsewhere + [l for l, _ in j.hidden if l.id in kept_anywhere]
        if elsewhere:
            lines.append(f"  kept by another search ({len(elsewhere)}):")
            for listing in elsewhere:
                lines.append(_car(listing, recs.get(listing.id, {})))
        hidden = [(l, v) for l, v in j.hidden if l.id not in kept_anywhere]
        if hidden:
            lines.append(f"  hidden by a rule ({len(hidden)}):")
            for listing, verdict in hidden:
                lines.append(_car(listing, recs.get(listing.id, {}), verdict.reason))
        kept = j.kept + j.unpriced
        lines.append(f"  on your list ({len(kept)}):")
        for listing in kept:
            lines.append(_car(listing, recs.get(listing.id, {})))
        if j.sold:
            lines.append(f"  marked sold ({len(j.sold)}): "
                         + ", ".join(sorted(recs.get(i, {}).get("title") or i for i in j.sold)))
        lines.append("")
    if result.get("capture"):
        lines.append(f"Captured to {result['capture']['folder']}")
        lines.append("")
        lines.append(result["capture"]["outline"])
    return "\n".join(lines).rstrip()


# --------------------------------------------------------------- capture

# The only paths whose values the outline shows: what the parser reads, and
# what it might. Everything else - names, ids, addresses, free text anywhere
# in Facebook's data - is shown as its type only, so a shape nobody has seen
# yet cannot leak a person into what the owner shares.
_SHOW = {
    "__typename", "marketplace_listing_title", "custom_title",
    "listing_price.amount", "listing_price.formatted_amount",
    "listing_price.amount_with_offset", "listing_price.amount_with_offset_in_currency",
    "listing_price.currency", "formatted_price.text",
    "strikethrough_price.amount", "strikethrough_price.formatted_amount",
    "custom_sub_titles_with_rendering_flags[].subtitle",
    "location.reverse_geocode.city", "location.reverse_geocode.state",
    "location.reverse_geocode.city_page.display_name", "location_text.text",
    "creation_time", "is_sold", "is_pending", "is_live", "is_hidden",
    "delivery_types[]", "condition", "story_type",
    "vehicle_make_display_name", "vehicle_model_display_name",
    "vehicle_trim_display_name", "vehicle_odometer_data.unit",
    "vehicle_odometer_data.value", "vehicle_transmission_type",
    "vehicle_exterior_color", "vehicle_interior_color", "vehicle_fuel_type",
    "vehicle_seller_type", "vehicle_title_status", "vehicle_condition",
    "marketplace_listing_seller.__typename",
}


def _shown(path: str) -> bool:
    return path in _SHOW or path.endswith(".__typename")


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, list):
        return "list"
    return "object"


def _leaves(node: Any, path: str = "", depth: int = 0) -> Iterator[tuple[str, Any]]:
    """(path, value) for each leaf under one listing, a few levels deep."""
    if depth > 4:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            inner = f"{path}.{key}" if path else key
            if isinstance(value, (dict, list)):
                yield from _leaves(value, inner, depth + 1)
            else:
                yield inner, value
    elif isinstance(node, list):
        for item in node[:3]:
            if isinstance(item, (dict, list)):
                yield from _leaves(item, f"{path}[]", depth + 1)
            else:
                yield f"{path}[]", item


def _paths_to_listings(document: Any) -> Iterator[str]:
    """Where in a document each listing-shaped node sits."""
    stack = [(document, "")]
    while stack:
        node, path = stack.pop()
        if isinstance(node, dict):
            if M._looks_like_listing(node):
                yield path or "(top)"
            for key, value in node.items():
                stack.append((value, f"{path}.{key}" if path else key))
        elif isinstance(node, list):
            for item in node:
                stack.append((item, f"{path}[]"))


def outline(pages: list[dict[str, Any]]) -> str:
    """What listing nodes look like across every captured page, masked."""
    fields: dict[str, dict[str, Any]] = {}
    where: dict[str, int] = {}
    nodes = docs = near = 0
    per_page = []
    for page in pages:
        found = 0
        for text in page["texts"]:
            for document in M.documents(text):
                docs += 1
                for path in _paths_to_listings(document):
                    tail = re.sub(r"(\[\])+", "[]", path)
                    where[tail[-140:]] = where.get(tail[-140:], 0) + 1
                for node in M._nodes(document):
                    nodes += 1
                    found += 1
                    for path, value in _leaves(node):
                        f = fields.setdefault(path, {"n": 0, "kinds": set(), "eg": []})
                        f["n"] += 1
                        f["kinds"].add(_kind(value))
                        if _shown(path) and value not in (None, "") and len(f["eg"]) < 3:
                            shown = str(value)[:60]
                            if shown not in f["eg"]:
                                f["eg"].append(shown)
                # Something that names a listing title but was not read as a
                # listing: the parser's blind spot, if there is one.
                near += _near_misses(document)
        per_page.append(f"  {page['search']} / {page['query']}: {len(page['texts'])} responses, "
                        f"{found} listing nodes (landed on {_where(page['landed'])})")
    lines = [f"Outline: {len(pages)} pages, {docs} JSON documents, {nodes} listing nodes"
             + (f", {near} title-bearing objects NOT read as listings" if near else ""),
             *per_page, "", "Where listings sit:"]
    for path, n in sorted(where.items(), key=lambda kv: -kv[1])[:8]:
        lines.append(f"  {n:>4}  ...{path}")
    lines += ["", f"Fields on listing nodes (of {nodes}):"]
    for path in sorted(fields):
        f = fields[path]
        eg = "; ".join(f["eg"])
        lines.append(f"  {f['n']:>4}  {path}  <{'/'.join(sorted(f['kinds']))}>"
                     + (f"  e.g. {eg}" if eg else ""))
    return "\n".join(lines)


def _near_misses(document: Any) -> int:
    count = 0
    stack = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "marketplace_listing_title" in node and not M._looks_like_listing(node):
                count += 1
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return count


def save_capture(pages: list[dict[str, Any]]) -> dict[str, str]:
    """Keep what the pages received, here only, and an outline of it."""
    folder = S.home() / "captures" / clock.now().strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    index = []
    for i, page in enumerate(pages):
        for j, text in enumerate(page["texts"]):
            name = f"{i:02d}-{j:02d}.{'html' if text.lstrip().startswith('<') else 'json'}"
            (folder / name).write_text(text, encoding="utf-8")
            index.append({"file": name, "search": page["search"], "query": page["query"],
                          "landed": page["landed"]})
    (folder / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    text = outline(pages)
    (folder / "outline.txt").write_text(text + "\n", encoding="utf-8")
    return {"folder": str(folder), "outline": text}


# ------------------------------------------------------------ test alert

def test_alert(settings: S.Settings, *, github: GitHub | None = None) -> list[str]:
    """One alert through the watch's ntfy topic, and whether it went.

    ntfy only: it is the channel a Mac can reach without the repository's
    secrets. A topic protected with NTFY_TOKEN needs that token in this
    shell's environment, and nothing else from it is used.
    """
    import os
    from autotrader import notifiers
    github = github or GitHub(settings.repo, S.secret(S.TOKEN))
    _, cfg = Keyring(github, S.secret(S.PASSPHRASE)).config()
    env = {k: os.environ[k] for k in ("NTFY_TOKEN",) if os.environ.get(k)}
    ntfy = [c for c in notifiers.build(cfg, env) if c.name == "ntfy"]
    if not ntfy:
        return []
    results = notifiers.alert(
        cfg, "AutoTrader Watch: a test alert",
        f"Sent from {settings.host} with collector/run test-alert. If this "
        f"reached your phone, alerts reach you.", env, notifiers=ntfy)
    return [str(r) for r in results]
