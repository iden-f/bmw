"""Client-side filters applied on top of whatever the search URL already does.

The search link does most of the work. These filters cover what the site's
own form cannot express well: "no dealer whose name contains X", "must
mention manual", a distance the site ignores, and so on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from . import geo
from .listing import Listing

log = logging.getLogger(__name__)


@dataclass
class Verdict:
    keep: bool
    reason: str = ""
    # A car with no published price has not failed a filter: dealers use
    # "call for price" on cars they expect to negotiate on, which are the
    # ones a bargain hunter wants to see.
    unpriced: bool = False
    # Which rule said no, as its config key. ``reason`` is for a person; this
    # is for the settings page, which shows only some of the rules.
    rule: str = ""
    # The result is not the model the search is for: the site ignored part of
    # the link. Unlike a preference (too dear, too far), such a car is
    # discarded rather than stored and explained.
    wrong_car: bool = False


def _number(value: Any) -> int | None:
    """A filter value as a number, or None if it is not one.

    config.json is edited by hand and filled from text boxes, so commas and
    dollar signs are ignored, and an unusable value means "no limit" rather
    than an exception that stops the run.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(float(str(value).replace(",", "").replace("$", "").strip()))
    except (TypeError, ValueError):
        log.warning("ignoring unusable filter value %r", value)
        return None


def normalise_model(text: Any) -> str:
    """A model name reduced to lower-case words, so spelling cannot matter.

    "F-150", "f 150" and "F  150" are the same; "F-150" and "F-150 Raptor"
    are not.
    """
    out: list[str] = []
    word = ""
    for char in str(text or "").lower():
        if char.isalnum():
            word += char
        elif word:
            out.append(word)
            word = ""
    if word:
        out.append(word)
    return " ".join(out)


def _haystack(listing: Listing) -> str:
    return " ".join(str(v) for v in (
        listing.title, listing.display_title, listing.trim, listing.color,
        listing.seller, listing.location, listing.body, listing.transmission,
        listing.drivetrain, listing.fuel, listing.engine,
    )).lower()


def _as_list(value: Any) -> list[Any]:
    """A keyword setting as a list. A bare string is one keyword, not N letters."""
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return []


def check(listing: Listing, filters: dict[str, Any] | None) -> Verdict:
    """Decide whether a listing survives the user's filters."""
    f = filters or {}

    # Asked first, so a car that is not the watched model is set aside rather
    # than reported as "too old" or "too far". Matched on the model field
    # alone, because titles and trims of other models often contain the name.
    wanted = [normalise_model(m) for m in _as_list(f.get("models"))]
    wanted = [m for m in wanted if m]
    if wanted:
        mine = normalise_model(listing.model)
        if mine not in wanted:
            names = ", ".join(str(m) for m in _as_list(f.get("models")) if str(m).strip())
            actual = str(listing.model or "").strip() or "an unnamed model"
            return Verdict(False, f"{actual} is not {names}", wrong_car=True,
                           rule="models")

    min_price = _number(f.get("min_price"))
    max_price = _number(f.get("max_price"))
    if listing.price is not None:
        if min_price and listing.price < min_price:
            return Verdict(False, f"price ${listing.price:,} below minimum ${min_price:,}",
                           rule="min_price")
        if max_price and listing.price > max_price:
            return Verdict(False, f"price ${listing.price:,} above maximum ${max_price:,}",
                           rule="max_price")

    min_year, max_year = _number(f.get("min_year")), _number(f.get("max_year"))
    if listing.year is not None:
        if min_year and listing.year < min_year:
            return Verdict(False, f"year {listing.year} below minimum {min_year}",
                           rule="min_year")
        if max_year and listing.year > max_year:
            return Verdict(False, f"year {listing.year} above maximum {max_year}",
                           rule="max_year")

    max_km = _number(f.get("max_mileage_km"))
    if max_km and listing.mileage_km is not None and listing.mileage_km > max_km:
        return Verdict(False, f"{listing.mileage_km:,} km above maximum {max_km:,} km",
                       rule="max_mileage_km")

    # The site ignores the link's own location radius, so distance is
    # enforced here.
    near = str(f.get("near") or "").strip()
    radius = _number(f.get("max_distance_km"))
    if near and radius:
        reference = geo.locate_reference(near)
        if reference is None:
            log.warning("cannot place %r, so distance is not being enforced", near)
        else:
            far, away = geo.too_far(listing.location, listing.province,
                                    reference, radius)
            if far:
                where = ", ".join(p for p in (listing.location, listing.province) if p)
                return Verdict(False, (
                    f"{where or 'that location'} is {away:,} km from {near}, "
                    f"beyond the {radius:,} km you asked for" if away else
                    f"{where or 'that location'} is beyond the {radius:,} km "
                    f"you asked for around {near}"), rule="max_distance_km")

    provinces = [str(p).strip().upper() for p in _as_list(f.get("provinces"))
                 if str(p).strip()]
    if provinces and listing.province and listing.province.upper() not in provinces:
        return Verdict(False, f"{listing.province} is not one of "
                              f"{', '.join(provinces)}", rule="provinces")

    text = _haystack(listing)

    include = [str(k).strip().lower() for k in _as_list(f.get("include_keywords"))
               if str(k).strip()]
    if include and not any(k in text for k in include):
        return Verdict(False, f"none of the required keywords matched: {', '.join(include)}",
                       rule="include_keywords")

    for word in _as_list(f.get("exclude_keywords")):
        word = str(word).strip().lower()
        if word and word in text:
            return Verdict(False, f"excluded keyword matched: {word}",
                           rule="exclude_keywords")

    for seller in _as_list(f.get("exclude_sellers")):
        seller = str(seller).strip().lower()
        if seller and listing.seller and seller in listing.seller.lower():
            return Verdict(False, f"excluded seller: {listing.seller}",
                           rule="exclude_sellers")

    # Last, so a car excluded for some other reason is reported for that
    # reason rather than being filed under "call for price".
    if listing.price is None and f.get("require_price"):
        return Verdict(False, "call for price - no figure published", unpriced=True,
                       rule="require_price")

    return Verdict(True)


def model_is_readable(listings: list[Listing]) -> bool:
    """True when the parse gave at least one of these results a model.

    The anchor and regex fallback parsers recover no model at all. Judging
    their output by model would reject every car and empty the watch over a
    parser fault, so the model rule stands down unless the field is read.
    One blank model among many is still judged.
    """
    return any(str(getattr(l, "model", "") or "").strip() for l in listings)


def not_this_car(listings: list[Listing], filters: dict[str, Any] | None
                 ) -> tuple[list[Listing], list[tuple[Listing, Verdict]]]:
    """Split off results that are not the model the search is for.

    Separate from ``apply`` so it can run before enrichment: the model is on
    the search card, so no request or state is spent on a car not watched.
    """
    mine: list[Listing] = []
    theirs: list[tuple[Listing, Verdict]] = []
    wanted = [normalise_model(m) for m in _as_list((filters or {}).get("models"))]
    if not any(wanted) or not model_is_readable(listings):
        return list(listings), theirs
    for listing in listings:
        verdict = check(listing, {"models": (filters or {}).get("models")})
        if verdict.wrong_car:
            theirs.append((listing, verdict))
        else:
            mine.append(listing)
    return mine, theirs


def apply(listings: list[Listing], filters: dict[str, Any] | None
          ) -> tuple[list[Listing], list[Listing],
                     list[tuple[Listing, Verdict]], list[tuple[Listing, Verdict]]]:
    """Split listings four ways: kept, call-for-price, hidden, and not-this-car.

    A call-for-price car cannot be judged yet, so it stays tracked and
    visible. A not-this-car result is for the caller to discard, not store.
    """
    kept: list[Listing] = []
    unpriced: list[Listing] = []
    dropped: list[tuple[Listing, Verdict]] = []
    wrong: list[tuple[Listing, Verdict]] = []
    # The same stand-down as `not_this_car`. Done here because it depends on
    # the whole batch, and `check` sees one car at a time.
    rules = dict(filters or {})
    if rules.get("models") and not model_is_readable(list(listings)):
        rules.pop("models")
    for listing in listings:
        verdict = check(listing, rules)
        if verdict.keep:
            kept.append(listing)
        elif verdict.unpriced:
            unpriced.append(listing)
        elif verdict.wrong_car:
            wrong.append((listing, verdict))
        else:
            dropped.append((listing, verdict))
    return kept, unpriced, dropped, wrong


def is_significant_drop(old_price: int, new_price: int,
                        min_pct: float, min_abs: int) -> bool:
    """Is this price change worth a notification, or just a rounding tweak?"""
    if old_price <= 0 or new_price >= old_price:
        return False
    delta = old_price - new_price
    pct = delta / old_price * 100.0
    # Both thresholds must be cleared, so a small nudge is ignored whether
    # the car is cheap or expensive.
    return delta >= max(0, int(min_abs)) and pct >= max(0.0, float(min_pct))
