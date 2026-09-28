"""The control channel: configuration changes sent from the dashboard.

The dashboard is a static page and cannot write to the repository, so a
change travels as a file committed through GitHub's web editor, sealed with
the vault key. The next check reads it and this module applies it:

* an instruction set is applied whole or not at all, because a half-applied
  config is worse than a rejected one and harder to notice;
* every rejection says what was wrong with the input;
* the text is only as trustworthy as whoever could commit the file, so every
  value is parsed and bounded, never interpolated.

The module is called control, not requests, so it is never confused with the
requests package.
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from . import geo
from .urls import describe_search, normalise_search_url

ACTIONS = ("set-rule", "add-search", "remove-search", "mute-listing",
           "unmute-listing", "shortlist", "unshortlist", "dismiss",
           # undismiss is the only undo for dismiss: unshortlist leaves a
           # dismissal in place.
           "undismiss", "set-channel", "note", "set-marketplace")

# Rules the dashboard may change, and the type of each value. Anything else is
# refused by name rather than ignored: a silent no-op hides a failed change.
RULE_TYPES: dict[str, type] = {
    "max_price": int, "min_price": int,
    "min_year": int, "max_year": int,
    "max_mileage_km": int, "max_distance_km": int,
    "near": str, "require_price": bool,
    "price_drop_min_pct": float, "price_drop_min_abs": int,
    # Other spellings of the watched model, for a car whose seller wrote it
    # some way the matching does not catch.
    "aliases": list,
}
RULE_BOUNDS: dict[str, tuple[float, float]] = {
    "max_price": (1, 10_000_000), "min_price": (0, 10_000_000),
    "min_year": (1900, 2100), "max_year": (1900, 2100),
    "max_mileage_km": (0, 2_000_000), "max_distance_km": (1, 20_000),
    "price_drop_min_pct": (0, 100), "price_drop_min_abs": (0, 1_000_000),
}
LISTING_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{5,63}$")
_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .+/-]{0,39}$")
MAX_ALIASES = 12

# Marketplace settings the dashboard may change, with their bounds. The
# collector's own pace and overnight pause are not here: they live on the
# Mac, because only the Mac should decide how often it visits Facebook.
MARKETPLACE_SETTINGS: dict[str, tuple[type, float, float]] = {
    "radius_km": (int, 1, 500),
    "new_within_days": (int, 1, 60),
    "gone_after_days": (int, 2, 90),
}
_PLACE_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
# A place the distance rule can measure from: a Canadian postcode (full, or
# the forward sortation area alone) or a place name.
_PLACE = re.compile(r"^(?:[A-Za-z]\d[A-Za-z](?:\s?\d[A-Za-z]\d)?"
                    r"|[A-Za-z][A-Za-z .'-]{1,40}(?:,\s*[A-Za-z .]{2,30})?)$")
AUTOTRADER_LINK = re.compile(r"^https://(www\.)?autotrader\.ca/", re.I)
# What a refusal suggests instead of a place the bot cannot find.
_PLACES_THAT_WORK = ("a Canadian postcode (K1P 1J1), or a city and province "
                     "with a comma between them (Toronto, ON)")
# The two price-drop floors are a search's own settings, or the global ones
# under notifications, not filters: that is where Config.rules_for reads them.
_DROP_FLOORS = ("price_drop_min_pct", "price_drop_min_abs")


class Rejected(Exception):
    """The instruction was not usable, and this says exactly why."""


@dataclass
class Outcome:
    applied: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    changed: bool = False

    def comment(self) -> str:
        """Describe what happened to the change.

        The text is kept in state and shown beside the change on the
        dashboard. It says only what happened; the caller adds how to
        correct it.
        """
        lines = []
        if self.applied:
            lines.append("Applied:")
            lines += [f"- {line}" for line in self.applied]
        if self.rejected:
            if lines:
                lines.append("")
            lines.append("Refused, and nothing was changed by these:")
            lines += [f"- {line}" for line in self.rejected]
        if not lines:
            lines.append("Nothing to do: the instruction was empty.")
        if self.rejected and not self.applied:
            lines.append("")
            lines.append("Nothing was written - not one of these, not partly.")
        return "\n".join(lines)


def parse(body: str) -> list[dict[str, Any]]:
    """Read the instructions out of a change file.

    The dashboard always writes JSON, so the file is read whole and anything
    else is refused.
    """
    text = (body or "").strip()
    if not text:
        raise Rejected("There is nothing here to read.")
    if not text.startswith(("{", "[")):
        raise Rejected(
            "This is not a JSON instruction. The dashboard writes one for "
            "you - open the change from there rather than writing it by hand.")
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise Rejected(
            f"The change is not valid JSON: {exc.msg} at line "
            f"{exc.lineno}, column {exc.colno}.") from exc

    items = loaded if isinstance(loaded, list) else [loaded]
    if not items:
        raise Rejected("The change has no instructions in it.")
    if len(items) > 20:
        raise Rejected(f"{len(items)} instructions in one change is more than "
                       f"this will apply at once. Split it up.")
    for item in items:
        if not isinstance(item, dict):
            raise Rejected(f"Each instruction has to be an object; got "
                           f"{type(item).__name__}.")
        action = str(item.get("action") or "")
        if action not in ACTIONS:
            raise Rejected(
                f"{action or '(no action)'} is not something I can do. "
                f"I know: {', '.join(ACTIONS)}.")
    return items


def _number(value: Any, name: str, kind: type) -> Any:
    try:
        out = kind(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: int() of an infinity, which JSON's 1e999 is.
        raise Rejected(f"{name} needs a number; got {value!r}.") from None
    if not math.isfinite(out):
        raise Rejected(f"{name} needs a number; got {value!r}.")
    low, high = RULE_BOUNDS.get(name, (float("-inf"), float("inf")))
    if not low <= out <= high:
        # Not :g, which would show a person "1e+07".
        raise Rejected(f"{name} has to be between {_figure(low)} and "
                       f"{_figure(high)}; got {_figure(out)}.")
    return out


def _figure(value: Any) -> str:
    """A number the way a person writes it."""
    number = float(value)
    if number == int(number):
        return f"{int(number):,}"
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def _rule_value(name: str, value: Any) -> Any:
    kind = RULE_TYPES.get(name)
    if kind is None:
        raise Rejected(
            f"{name} is not a rule I can change from here. I can change: "
            f"{', '.join(sorted(RULE_TYPES))}.")
    if value is None:
        return None                      # clearing a rule is a real request
    if kind is list:
        items = value if isinstance(value, list) else str(value).split(",")
        names = [str(v).strip() for v in items if str(v).strip()]
        if len(names) > MAX_ALIASES:
            raise Rejected(f"{name} takes at most {MAX_ALIASES}; got {len(names)}.")
        for alias in names:
            if not _ALIAS.match(alias):
                raise Rejected(f"{alias[:40]!r} is not a model name: letters, digits, "
                               f"spaces and - . + / only, up to 40 characters.")
        return names or None
    if kind is bool:
        if isinstance(value, bool):
            return value
        if str(value).lower() in ("true", "yes", "on", "1"):
            return True
        if str(value).lower() in ("false", "no", "off", "0"):
            return False
        raise Rejected(f"{name} is a yes/no rule; got {value!r}.")
    if kind is str:
        # A place is text: it must never reach the numeric bounds check.
        if not isinstance(value, (str, int, float)):
            raise Rejected(f"{name} needs a place, not a "
                           f"{type(value).__name__}.")
        text = str(value).strip()
        if not text:
            raise Rejected(f"{name} cannot be empty. Give a postcode like "
                           f"'K1P 1J1', or clear it with null.")
        if len(text) > 60:
            raise Rejected(f"{name} is {len(text)} characters; a place is "
                           f"shorter than that.")
        if not _PLACE.match(text):
            raise Rejected(
                f"{text!r} does not look like somewhere I can measure from. "
                f"Use {_PLACES_THAT_WORK}.")
        # A place the bot cannot find would switch the distance rule off
        # without a word: every car would be kept, and the page would still
        # show the rule.
        if geo.locate_reference(text) is None:
            raise Rejected(
                f"{text!r} is not a place I can find, so I could not measure "
                f"from it. Use {_PLACES_THAT_WORK}.")
        return text
    return _number(value, name, kind)


def _find_search(cfg, wanted: str):
    wanted = str(wanted or "").strip()
    if not wanted:
        return None
    for search in cfg.searches:
        if search.id == wanted or search.name.lower() == wanted.lower():
            return search
    return None


@dataclass
class _Batch:
    """What the instructions planned so far will have done.

    Each instruction is checked against the config as it is, and these, so
    two instructions in one change cannot each pass a check that together
    they fail.
    """
    added: set[str] = field(default_factory=set)       # search links
    removed: set[str] = field(default_factory=set)     # search ids
    # The place and the distance set by this change, by search id; None is
    # the global rule.
    near: dict[str | None, Any] = field(default_factory=dict)
    radius: dict[str | None, Any] = field(default_factory=dict)


def apply(cfg, state, items: list[dict[str, Any]]) -> Outcome:
    """Carry out a parsed instruction set, or none of it.

    Everything is validated before anything is written, so one bad
    instruction leaves the whole set unapplied.
    """
    out = Outcome()
    planned: list[tuple[str, Any]] = []
    batch = _Batch()

    for item in items:
        action = item["action"]
        try:
            planned.append((action, _plan(cfg, state, item, batch)))
        except Rejected as exc:
            out.rejected.append(f"`{action}`: {exc}")
    out.rejected += _whole_set_problems(cfg, batch)

    if out.rejected:
        return out                        # whole or nothing

    # A write that fails part-way through, for a reason no check above
    # foresaw, must not leave the first half written: both files are copied
    # first and put back.
    before = copy.deepcopy(cfg.data), copy.deepcopy(state.data)
    try:
        for action, work in planned:
            out.applied.append(work())
    except Exception as exc:
        for live, saved in zip((cfg.data, state.data), before):
            live.clear()
            live.update(saved)
        out.applied = []
        if not isinstance(exc, (Rejected, ValueError)):
            raise
        out.rejected.append(f"`{action}`: {exc}")
        return out
    out.changed = bool(out.applied)
    return out


def _whole_set_problems(cfg, batch: _Batch) -> list[str]:
    """What is wrong with the change taken as a whole, once each part passed."""
    problems = []
    left = len(cfg.searches) + len(batch.added) - len(batch.removed)
    if batch.removed and left < 1:
        problems.append(
            "`remove-search`: that is the only search there is; removing it "
            "would leave the bot watching nothing." if len(cfg.searches) == 1
            else "`remove-search`: that removes every search, which would "
                 "leave the bot watching nothing.")

    # A distance needs a place to measure from. Without one the rule is
    # kept and does nothing, and every car it should hide is kept.
    if all(v is None for v in batch.radius.values()):
        return problems
    lacking = []
    for search in cfg.searches:
        if search.id in batch.removed:
            continue
        own = search.filters or {}
        own_radius = batch.radius.get(search.id, own.get("max_distance_km"))
        radius = own_radius if own_radius is not None else batch.radius.get(
            None, cfg.get("filters.max_distance_km"))
        # Only a distance this change sets is judged here; one already
        # saved is the page's to point out.
        if radius is None or not (search.id in batch.radius
                                  or (None in batch.radius and own_radius is None)):
            continue
        near = (batch.near.get(search.id, own.get("near"))
                or batch.near.get(None, cfg.get("filters.near")))
        if not near or geo.locate_reference(str(near)) is None:
            lacking.append(search.name)
    if lacking:
        problems.append(
            f"`set-rule`: max_distance_km needs a place to measure from, and "
            f"{', '.join(repr(n) for n in lacking)} "
            f"{'has' if len(lacking) == 1 else 'have'} none I can find: set "
            f"near first, to {_PLACES_THAT_WORK}.")
    return problems


def _plan(cfg, state, item: dict[str, Any], batch: _Batch | None = None):
    """Validate one instruction and return something that performs it."""
    action = item["action"]
    batch = batch if batch is not None else _Batch()

    if action == "set-rule":
        search = _find_search(cfg, item.get("search"))
        if item.get("search") and not search:
            raise Rejected(f"there is no search called {item['search']!r}.")
        if search and search.id in batch.removed:
            raise Rejected(f"{search.name!r} is removed by this same change.")
        name = str(item.get("rule") or "")
        value = _rule_value(name, item.get("value"))
        if name == "aliases" and not search:
            raise Rejected("aliases belong to one search: say which.")
        scope = search.id if search else None
        if name == "near":
            batch.near[scope] = value
        if name == "max_distance_km":
            batch.radius[scope] = value
        if name in _DROP_FLOORS:
            where = (f"searches.{search.id}.{name}" if search
                     else f"notifications.{name}")
        else:
            where = f"searches.{search.id}.filters.{name}" if search else f"filters.{name}"
        label = f"{name} = {value!r}" + (f" on {search.name}" if search else " everywhere")

        def do():
            if search:
                raw = cfg.data.setdefault("searches", [])
                for row in raw:
                    if row.get("id") == search.id:
                        # A drop floor is a setting of the search itself.
                        held = row if name in _DROP_FLOORS else row.setdefault("filters", {})
                        held.pop(name, None) if value is None else held.update({name: value})
                        break
            elif name in _DROP_FLOORS:
                # Cleared, the global floor goes back to its default rather
                # than to nothing, which would pass every drop.
                from .config import DEFAULTS
                cfg.set(f"notifications.{name}",
                        DEFAULTS["notifications"][name] if value is None else value)
            else:
                if value is None:
                    (cfg.data.get("filters") or {}).pop(name, None)
                else:
                    cfg.set(f"filters.{name}", value)
            return label
        do.__doc__ = where
        return do

    if action == "add-search":
        url = str(item.get("url") or "").strip()
        if not AUTOTRADER_LINK.match(url):
            raise Rejected("a search link has to be an https://www.autotrader.ca/ "
                           "address; got " + (url[:80] or "nothing") + ".")
        # The same reading the command line's `add` gives: a single car's
        # page or the home page is a link, but not a list of results.
        summary = describe_search(url)
        if not summary.valid:
            raise Rejected((summary.problems or ["that is not a search."])[0])
        wanted = normalise_search_url(url)
        for existing in cfg.searches:
            if existing.url == wanted and existing.id not in batch.removed:
                raise Rejected(f"that search is already being watched as "
                               f"{existing.name!r}.")
        if wanted in batch.added:
            raise Rejected("that search is added twice in this change.")
        batch.added.add(wanted)
        name = str(item.get("name") or "").strip()[:80]

        def do():
            search = cfg.add_search(url, name)
            return f"added the search {search.name!r}"
        return do

    if action == "remove-search":
        search = _find_search(cfg, item.get("search"))
        if not search:
            raise Rejected(f"there is no search called {item.get('search')!r}.")
        if search.id in batch.removed:
            raise Rejected(f"{search.name!r} is removed twice in this change.")
        # Whether anything is left to watch is settled over the whole change,
        # in _whole_set_problems.
        batch.removed.add(search.id)

        def do():
            cfg.remove_search(search.id)
            return f"removed the search {search.name!r}"
        return do

    if action in ("mute-listing", "unmute-listing", "shortlist",
                  "unshortlist", "dismiss", "undismiss", "note"):
        listing_id = str(item.get("listing") or "").strip()
        if not LISTING_ID.match(listing_id):
            raise Rejected(f"{listing_id[:40]!r} is not a listing id.")
        if listing_id not in state.listings:
            raise Rejected(f"there is no listing {listing_id[:12]}… in state.")
        text = str(item.get("text") or "")[:400]

        def do():
            entry = state.listings[listing_id]
            marks = entry.setdefault("you", {})
            title = (entry.get("title") or listing_id)[:40]
            if action == "mute-listing":
                marks["muted"] = True
                return f"muted {title} - no more alerts about it"
            if action == "unmute-listing":
                marks.pop("muted", None)
                return f"unmuted {title}"
            if action == "shortlist":
                marks["shortlisted"] = True
                marks.pop("dismissed", None)
                return f"shortlisted {title} - its price drops come through louder"
            if action == "unshortlist":
                marks.pop("shortlisted", None)
                return f"took {title} off the shortlist"
            if action == "dismiss":
                marks["dismissed"] = True
                marks.pop("shortlisted", None)
                return f"dismissed {title} - it stays quiet"
            if action == "undismiss":
                marks.pop("dismissed", None)
                return f"{title} is back in play"
            if not text:
                marks.pop("note", None)
                return f"cleared the note on {title}"
            marks["note"] = text
            return f"noted on {title}: {text[:60]}"
        return do

    if action == "set-channel":
        channel = str(item.get("channel") or "").strip().lower()
        known = set((cfg.get("notifications.channels", {}) or {}))
        if channel not in known:
            raise Rejected(f"{channel or '(none)'} is not a channel here. "
                           f"There is: {', '.join(sorted(known)) or 'none'}.")
        on = item.get("enabled")
        if not isinstance(on, bool):
            raise Rejected("enabled has to be true or false.")

        def do():
            cfg.set(f"notifications.channels.{channel}.enabled", on)
            # Why a channel is off is kept beside it, so the page can say so
            # and setup does not switch it back on (provision.py).
            if on:
                cfg.data["notifications"]["channels"][channel].pop("disabled_reason", None)
            else:
                cfg.set(f"notifications.channels.{channel}.disabled_reason",
                        "Switched off from the dashboard")
            return f"turned {channel} {'on' if on else 'off'}"
        return do

    if action == "set-marketplace":
        return _plan_marketplace(cfg, item, batch)

    raise Rejected("I do not know how to do that.")   # unreachable via parse()


def _plan_marketplace(cfg, item: dict[str, Any], batch: _Batch):
    """Marketplace on or off for one search, or one of its shared settings."""
    if item.get("search") is not None or "enabled" in item:
        search = _find_search(cfg, item.get("search"))
        if not search:
            raise Rejected(f"there is no search called {item.get('search')!r}.")
        if search.id in batch.removed:
            raise Rejected(f"{search.name!r} is removed by this same change.")
        on = item.get("enabled")
        if not isinstance(on, bool):
            raise Rejected("enabled has to be true or false.")

        def do():
            for row in cfg.data.setdefault("searches", []):
                if row.get("id") == search.id:
                    if on:
                        row.pop("marketplace", None)
                    else:
                        row["marketplace"] = False
            return (f"{'reading' if on else 'no longer reading'} {search.name!r} "
                    f"on Marketplace")
        return do

    setting = str(item.get("setting") or "")
    value = item.get("value")
    if setting == "exact":
        if value is not None and not isinstance(value, bool):
            raise Rejected("exact has to be true or false.")
        value = value or None             # off is the default
    elif setting == "place":
        if value in (None, ""):
            value = ""
        else:
            value = str(value).strip().lower()
            if not _PLACE_SLUG.match(value):
                raise Rejected(f"{value[:40]!r} is not a Marketplace place: the "
                               f"word in its address, like 'ottawa'.")
    elif setting in MARKETPLACE_SETTINGS:
        kind, low, high = MARKETPLACE_SETTINGS[setting]
        if value is not None:
            value = _number(value, setting, kind)
            if not low <= value <= high:
                raise Rejected(f"{setting} has to be between {low:g} and {high:g}.")
    else:
        raise Rejected(f"{setting or '(none)'} is not a Marketplace setting I can "
                       f"change. I can change: "
                       f"{', '.join(sorted([*MARKETPLACE_SETTINGS, 'place', 'exact']))}.")

    def do():
        conf = cfg.data.setdefault("marketplace", {})
        if value in (None, ""):
            conf.pop(setting, None)
            return f"Marketplace {setting} back to its default"
        conf[setting] = value
        return f"Marketplace {setting} = {value!r}"
    return do
