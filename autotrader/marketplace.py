"""Facebook Marketplace: what its pages say, what to search, taking a batch in.

Marketplace shows listings only to a signed-in browser on a home connection,
so a computer at home does the reading (see collector/) and sends what it saw
here, sealed with the vault key. The bot stays the only writer: a batch goes
through the same rules, alerts and dashboard as a check.

Nothing here talks to Facebook. The collector imports the reading and planning
halves; the bot uses the planning and ingest halves.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator
from urllib.parse import urlencode

from . import clock, filters, geo
from .listing import MARKETPLACE_PREFIX, Listing, on_marketplace
from .state import Change, State, utcnow
from .urls import describe_search

log = logging.getLogger(__name__)

ITEM_URL = "https://www.facebook.com/marketplace/item/{}/"
SEARCH_URL = "https://www.facebook.com/marketplace/{place}/search/"

# The widest radius Marketplace offers, and the one used when a search sets
# none. Distance is still enforced by the search's own rule on the way in.
MAX_RADIUS_KM = 500
DEFAULT_RADIUS_KM = 100

# Sellers who will not name a price type one of these instead.
_PLACEHOLDER_PRICES = {1234, 12345, 123456, 1111, 11111, 111111}
_LOWEST_REAL_PRICE = 500

_ITEM_ID = re.compile(r"^\d{5,20}$")
_SCRIPT = re.compile(r'<script type="application/json"[^>]*>(.*?)</script>', re.S)
_MILEAGE = re.compile(
    r"^\s*([\d][\d.,]*)\s*(k)?\s*(km|kms|kilometres|kilometers|mi|miles)\b", re.I)
KM_PER_MILE = 1.609344

PROVINCES = {
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB",
    "new brunswick": "NB", "newfoundland and labrador": "NL",
    "newfoundland": "NL", "nova scotia": "NS", "northwest territories": "NT",
    "nunavut": "NU", "ontario": "ON", "prince edward island": "PE",
    "quebec": "QC", "québec": "QC", "saskatchewan": "SK", "yukon": "YT",
}

# ---------------------------------------------------------------- the batch

BATCH_NAME = "marketplace"
BATCH_VERSION = 1
# Long enough for a queued workflow run, short enough that a captured batch
# cannot be replayed into a later week.
BATCH_MAX_AGE_HOURS = 12
BATCH_IDS_KEPT = 500
# repository_dispatch refuses a client_payload over 65,535 characters.
PAYLOAD_LIMIT = 60_000


class BatchError(ValueError):
    """A batch that cannot be taken in, and why."""


class AlreadyTakenIn(BatchError):
    """The same batch twice: a collector retrying a send that had worked."""


def seal_batch(key: bytes, batch: dict[str, Any]) -> str:
    """The batch as sent: sealed with the vault key, as base64 text."""
    from .vault import PAD_SMALL, seal
    blob = seal(key, json.dumps(batch, separators=(",", ":")).encode(),
                BATCH_NAME, pad=PAD_SMALL)
    return base64.b64encode(blob).decode("ascii")


def open_batch(key: bytes, text: str, state: State | None = None,
               *, now=None) -> dict[str, Any]:
    """Unseal a batch and refuse one that is malformed, stale or replayed.

    With ``state``, its id is checked against the batches already taken in,
    but not recorded: ``ingest`` records it once the batch is applied.
    """
    from .vault import VaultError, unseal
    try:
        blob = base64.b64decode(str(text or "").strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BatchError(f"not base64: {exc}") from None
    try:
        batch = json.loads(unseal(key, blob, BATCH_NAME))
    except VaultError as exc:
        raise BatchError(f"does not open with this vault's key: {exc}") from None
    except ValueError as exc:
        raise BatchError(f"not JSON: {exc}") from None
    if not isinstance(batch, dict) or batch.get("v") != BATCH_VERSION:
        raise BatchError(f"not a version {BATCH_VERSION} batch")
    bid = str(batch.get("id") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", bid):
        raise BatchError("no batch id")
    sent = clock.parse(batch.get("at"))
    if sent is None:
        raise BatchError("no time on it")
    now = now or clock.now()
    if now - sent > timedelta(hours=BATCH_MAX_AGE_HOURS):
        raise BatchError(f"sent {sent.isoformat()}, too long ago to trust")
    if sent - now > timedelta(minutes=10):
        raise BatchError(f"dated {sent.isoformat()}, in the future")
    if state is not None and bid in (_section(state).get("batch_ids") or []):
        raise AlreadyTakenIn("already taken in")
    if not isinstance(batch.get("searches", []), list):
        raise BatchError("searches is not a list")
    return batch


# ------------------------------------------------------- reading the page

def documents(text: str) -> Iterator[Any]:
    """Every JSON document in a Marketplace response or page.

    An API response may hold several documents, one per line; a page carries
    its first results in <script type="application/json"> blocks.
    """
    body = (text or "").lstrip()
    if body.startswith("for (;;);"):
        body = body[len("for (;;);"):]
    chunks = _SCRIPT.findall(body) if body.startswith("<") else \
        re.split(r"\r?\n(?=[{\[])", body)
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk.startswith(("{", "[")):
            continue
        try:
            yield json.loads(chunk)
        except ValueError:
            continue


def _looks_like_listing(node: dict[str, Any]) -> bool:
    if not _ITEM_ID.match(str(node.get("id") or "")):
        return False
    return ("marketplace_listing_title" in node or "listing_price" in node
            or "vehicle_odometer_data" in node)


def _nodes(document: Any) -> Iterator[dict[str, Any]]:
    """Listing-shaped nodes in document order: newest first on a sorted page."""
    stack = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if _looks_like_listing(node):
                yield node
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))


def _text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("text")
    return " ".join(str(value).split()) if isinstance(value, (str, int)) else ""


def _amount(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    digits = re.sub(r"[^\d.]", "", str(value or ""))
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


def _price(node: dict[str, Any]) -> tuple[int | None, str]:
    found: float | None = None
    currency = ""
    lp = node.get("listing_price")
    if isinstance(lp, dict):
        currency = str(lp.get("currency") or "")
        found = _amount(lp.get("amount"))
        if found is None:
            offset = _amount(lp.get("amount_with_offset_in_currency")
                             or lp.get("amount_with_offset"))
            found = offset / 100 if offset else None
        if found is None:
            found = _amount(lp.get("formatted_amount"))
    if found is None:
        found = _amount(_text(node.get("formatted_price")))
    if found is None:
        return None, currency
    price = int(round(found))
    if price < _LOWEST_REAL_PRICE or price in _PLACEHOLDER_PRICES:
        return None, currency
    return price, currency


def _km(text: str) -> tuple[int | None, bool]:
    """Kilometres from a card's "45K km", and whether it was rounded."""
    m = _MILEAGE.match(text or "")
    if not m:
        return None, False
    value = _amount(m.group(1).replace(",", ""))
    if value is None:
        return None, False
    if m.group(2):
        value *= 1000
    if m.group(3).lower().startswith("mi"):
        value *= KM_PER_MILE
    return int(round(value)), bool(m.group(2))


def _province(text: str) -> str:
    text = str(text or "").strip()
    if len(text) == 2 and text.isalpha():
        return text.upper()
    return PROVINCES.get(text.lower(), "")


def _photo(node: dict[str, Any]) -> str:
    for key in ("primary_listing_photo", "listing_photo"):
        photo = node.get(key)
        if isinstance(photo, dict):
            for inner in ("image", "listing_image"):
                uri = (photo.get(inner) or {}).get("uri") \
                    if isinstance(photo.get(inner), dict) else None
                if uri:
                    return str(uri)
    photos = node.get("listing_photos")
    if isinstance(photos, list):
        for photo in photos:
            if isinstance(photo, dict) and isinstance(photo.get("image"), dict):
                uri = photo["image"].get("uri")
                if uri:
                    return str(uri)
    return ""


def _word(value: Any) -> str:
    """AUTOMATIC -> Automatic; a colour stays as the seller typed it."""
    text = str(value or "").replace("_", " ").strip()
    return text.capitalize() if text.isupper() else text


def record_of(node: dict[str, Any]) -> dict[str, Any]:
    """One listing, as the collector sends it: plain facts, no Facebook shape."""
    rec: dict[str, Any] = {"id": str(node["id"])}
    title = _text(node.get("marketplace_listing_title")) or _text(node.get("custom_title"))
    if title:
        rec["title"] = title
    price, currency = _price(node)
    if price is not None:
        rec["price"] = price
    if currency:
        rec["currency"] = currency

    loc = node.get("location")
    geocode = loc.get("reverse_geocode") if isinstance(loc, dict) else None
    if isinstance(geocode, dict):
        city = str(geocode.get("city") or "")
        province = _province(geocode.get("state"))
        page = geocode.get("city_page")
        if isinstance(page, dict) and page.get("display_name"):
            name, _, region = str(page["display_name"]).partition(",")
            city = city or name.strip()
            province = province or _province(region)
        if city:
            rec["city"] = city
        if province:
            rec["province"] = province
    if "city" not in rec:
        name, _, region = _text(node.get("location_text")).partition(",")
        if name:
            rec["city"] = name.strip()
            if _province(region):
                rec["province"] = _province(region)

    odometer = node.get("vehicle_odometer_data")
    if isinstance(odometer, dict) and _amount(odometer.get("value")):
        km = _amount(odometer.get("value")) or 0
        if str(odometer.get("unit") or "").upper().startswith("MI"):
            km *= KM_PER_MILE
        rec["mileage_km"] = int(round(km))
    else:
        for sub in node.get("custom_sub_titles_with_rendering_flags") or []:
            km, rounded = _km(_text((sub or {}).get("subtitle")))
            if km is not None:
                rec["mileage_km"] = km
                if rounded:
                    rec["mileage_rounded"] = True
                break

    photo = _photo(node)
    if photo:
        rec["photo"] = photo
    created = node.get("creation_time")
    if isinstance(created, int) and created > 1_000_000_000:
        rec["created"] = created
    for key, name in (("is_sold", "sold"), ("is_pending", "pending")):
        if isinstance(node.get(key), bool):
            rec[name] = node[key]

    for key, name in (("vehicle_make_display_name", "make"),
                      ("vehicle_model_display_name", "model"),
                      ("vehicle_trim_display_name", "trim"),
                      ("vehicle_exterior_color", "color"),
                      ("vehicle_identification_number", "vin")):
        if node.get(key):
            rec[name] = str(node[key]).strip()
    for key, name in (("vehicle_transmission_type", "transmission"),
                      ("vehicle_fuel_type", "fuel"),
                      ("vehicle_title_status", "title_status")):
        if node.get(key):
            rec[name] = _word(node[key])
    seller = str(node.get("vehicle_seller_type") or "").upper()
    if "DEALER" in seller:
        rec["seller_type"] = "dealer"
    elif "PRIVATE" in seller:
        rec["seller_type"] = "private"
    else:
        kind = (node.get("marketplace_listing_seller") or {})
        kind = kind.get("__typename") if isinstance(kind, dict) else ""
        if kind == "User":
            rec["seller_type"] = "private"
        elif kind == "Page":
            rec["seller_type"] = "dealer"
    return rec


def merge(into: dict[str, Any], rec: dict[str, Any]) -> dict[str, Any]:
    """Fill ``into`` from ``rec``. Sold or pending anywhere is sold or pending,
    and an odometer reading beats a card's rounded figure."""
    if into.get("mileage_rounded") and "mileage_km" in rec \
            and not rec.get("mileage_rounded"):
        into.pop("mileage_rounded")
        into["mileage_km"] = rec["mileage_km"]
    for key, value in rec.items():
        if key == "mileage_rounded" and "mileage_rounded" not in into \
                and "mileage_km" in into and into["mileage_km"] != rec.get("mileage_km"):
            continue          # the exact figure is already here
        if key in ("sold", "pending"):
            into[key] = bool(into.get(key)) or bool(value)
        elif key not in into or into[key] in (None, "", []):
            into[key] = value
    return into


def collect(texts: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Every listing in these responses and pages, by Marketplace item id."""
    found: dict[str, dict[str, Any]] = {}
    for text in texts:
        for document in documents(text):
            for node in _nodes(document):
                rec = record_of(node)
                merge(found.setdefault(rec["id"], {}), rec)
    return found


# ------------------------------------------------ from a record to a car

_UPPER = {"bmw", "gmc", "amg", "gt", "gts", "gti", "suv", "awd", "rwd", "fwd",
          "4wd", "vw", "cpo", "ev"}


def tidy_title(title: str) -> str:
    """ "2018 Audi q5 suv 4d" -> "2018 Audi Q5 SUV 4D".

    Marketplace lower-cases the model in the titles it builds. Words the
    seller capitalised themselves are left alone.
    """
    out = []
    for word in str(title or "").split():
        if word != word.lower() or not word.isalnum():
            out.append(word)
        elif word in _UPPER or (len(word) <= 4 and any(c.isdigit() for c in word)
                                and any(c.isalpha() for c in word)):
            out.append(word.upper())
        elif word.isalpha():
            out.append(word.capitalize())
        else:
            out.append(word)
    return " ".join(out)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(text or "").lower())


# A word right after a model that makes it an option, a look or a part
# rather than the car: "X5 M Sport" is an X5 with the M Sport package, an
# "RS5 grille" is on an A5.
_NOT_THE_MODEL = {
    "sport", "sports", "package", "packages", "pkg", "pack", "performance",
    "look", "looks", "style", "styling", "appearance", "line", "badge",
    "badges", "badging", "badged", "wheel", "wheels", "rim", "rims", "kit",
    "bodykit", "bumper", "bumpers", "grille", "grill", "replica", "tribute",
    "clone", "conversion", "exhaust", "seats", "steering",
}
# ...except where the word is a body style: "Sport Utility 4D" is how
# Marketplace names an SUV.
_BODY_AFTER_SPORT = {"utility", "activity"}


def _fused(word: str, want: str) -> bool:
    """ "rs5cs" is an RS 5 with a trim run on; "x5m" is not an X5.

    Only after a number, and one letter only after two digits or more
    ("c63s"), since one letter after one digit is usually a model of its own.
    """
    rest = word[len(want):]
    if not (word.startswith(want) and want[-1:].isdigit() and rest.isalpha()):
        return False
    digits = len(want) - len(want.rstrip("0123456789"))
    return 2 <= len(rest) <= 4 or (len(rest) == 1 and digits >= 2)


def _spelled_at(words: list[str], i: int, want: list[str]) -> int:
    """Where the model ends if the title spells it from word ``i``, else -1.

    A title word may join several of the model's words ("x5m" for "X5 M") but
    never split one ("s 3.0t" is not an S3). A short trim may be run onto the
    last one.
    """
    j = 0
    while j < len(want):
        if i >= len(words):
            return -1
        word = words[i]
        joined = ""
        for k in range(j, len(want)):
            joined += want[k]
            if joined == word:
                j, i = k + 1, i + 1
                break
            if len(joined) >= len(word):
                break
        else:
            joined = None
        if joined == word:
            continue
        if _fused(word, "".join(want[j:])):
            return i + 1
        return -1
    return i


def model_in(title: str, models: Iterable[str]) -> str:
    """The first of ``models`` the title names, word for word.

    "x5 m" is in "2021 X5 M Competition" and in "2021 X5M", but not in
    "2021 X5 M50i", nor in "2021 X5 M Sport Package".
    """
    words = _words(title)
    for model in models:
        want = _words(model)
        if not want:
            continue
        for i in range(len(words)):
            end = _spelled_at(words, i, want)
            if end < 0:
                continue
            after = words[end] if end < len(words) else ""
            if after in _NOT_THE_MODEL and not (
                    after in ("sport", "sports")
                    and end + 1 < len(words) and words[end + 1] in _BODY_AFTER_SPORT):
                continue
            return str(model)
    return ""


def to_listing(rec: dict[str, Any], search, *, make: str = "",
               models: Iterable[str] = (), aliases: Iterable[str] = ()) -> Listing:
    """A collector's record as a car the rest of the bot understands.

    ``aliases`` are other spellings the owner said count as the model. A car
    found by one is stored under the model itself when the search has only
    one, so it is priced and grouped with the rest of that model.
    """
    models = [str(m) for m in models if str(m).strip()]
    aliases = [str(a) for a in aliases if str(a).strip()]
    names = models + aliases
    title = tidy_title(rec.get("title") or "")
    status = str(rec.get("title_status") or "")
    if status and status.lower() not in ("clean", "unknown", "not sure"):
        # Rebuilt and salvage titles are common here, and keyword rules can
        # only see what is in the name.
        title = f"{title} · {status} title" if title else f"{status} title"

    # Facebook's own model field is only as good as the seller's choice from
    # its list, and a seller often picks the parent model ("A5" for an
    # RS 5) and puts the real one in the trim or the title. So each is asked
    # in turn, and the field is trusted as it stands only when none of them
    # names a watched model.
    named = str(rec.get("model") or "")
    model = (model_in(named, names)
             or model_in(f"{named} {rec.get('trim') or ''}", names)
             or model_in(title, names)
             or named)
    if model in aliases and model not in models and len(models) == 1:
        model = models[0]
    if not model and make:
        # Not a model the search wants, so name whatever follows the make:
        # the models rule then says which car it is instead of "unnamed".
        words = _words(title)
        mk = _words(make)
        for i in range(len(words) - len(mk)):
            if words[i:i + len(mk)] == mk:
                model = words[i + len(mk)].upper()
                break
    car_make = str(rec.get("make") or "")
    if not car_make and make and (model in names or make.lower() in title.lower()):
        car_make = make

    listing = Listing(
        id=MARKETPLACE_PREFIX + str(rec["id"]),
        url=ITEM_URL.format(rec["id"]),
        title=title,
        make=car_make,
        model=model,
        trim=str(rec.get("trim") or ""),
        price=rec.get("price"),
        # One source, so every price compares with the last.
        price_source="marketplace",
        currency=str(rec.get("currency") or "CAD"),
        mileage_km=rec.get("mileage_km"),
        location=str(rec.get("city") or ""),
        province=str(rec.get("province") or ""),
        seller_type=str(rec.get("seller_type") or ""),
        color=str(rec.get("color") or ""),
        transmission=str(rec.get("transmission") or ""),
        fuel=str(rec.get("fuel") or ""),
        vin=str(rec.get("vin") or ""),
        images=[rec["photo"]] if rec.get("photo") else [],
        search_id=search.id,
        search_name=search.name,
        source="marketplace",
        enriched=bool(rec.get("make") or rec.get("model")),
    )
    return listing


@dataclass
class Judged:
    """Where every car one search read went, as the bot decides it."""
    on_sale: list[Listing] = field(default_factory=list)
    sold: set[str] = field(default_factory=set)
    pending: set[str] = field(default_factory=set)
    # Another model than the search is for: set aside, not stored.
    other: list[tuple[Listing, "filters.Verdict"]] = field(default_factory=list)
    # Already claimed by an earlier search this batch.
    elsewhere: list[Listing] = field(default_factory=list)
    kept: list[Listing] = field(default_factory=list)
    unpriced: list[Listing] = field(default_factory=list)
    # Stored, but hidden by one of the search's rules, with the reason.
    hidden: list[tuple[Listing, "filters.Verdict"]] = field(default_factory=list)

    def breakdown(self, kept_anywhere: set[str] | None = None) -> dict[str, Any]:
        """The counts, which always add up to ``read``.

        ``kept_anywhere`` is every id some search kept: a car this search's
        rules would hide but a later search keeps is not hidden, and counts
        with the cars kept by another search.
        """
        kept_anywhere = kept_anywhere or set()
        hidden = [(l, v) for l, v in self.hidden if l.id not in kept_anywhere]
        hidden_by: dict[str, int] = {}
        for _, verdict in hidden:
            hidden_by[verdict.rule or "rule"] = hidden_by.get(verdict.rule or "rule", 0) + 1
        return {"read": len(self.on_sale), "sold": len(self.sold),
                "other_models": len(self.other),
                "elsewhere": len(self.elsewhere) + len(self.hidden) - len(hidden),
                "hidden": len(hidden), "hidden_by": hidden_by,
                "kept": len(self.kept) + len(self.unpriced)}

    def other_examples(self, limit: int = 8) -> list[list[Any]]:
        """The models set aside, most common first: [name, count]."""
        counts: dict[str, int] = {}
        for listing, _ in self.other:
            name = listing.model or "no model named"
            counts[name] = counts.get(name, 0) + 1
        return [[name, n] for name, n in
                sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def judge(cfg, search, records: Iterable[dict[str, Any]],
          item: dict[str, Any] | None = None, owned: set[str] | None = None) -> Judged:
    """Sort one search's cars by the search's own rules, without storing any.

    The bot takes a batch in with this, and the collector's ``explain``
    prints it, so the two can never disagree about why a car went where.
    """
    item = item or {}
    owned = owned if owned is not None else set()
    search_filters = dict(cfg.rules_for(search)["filters"])
    records = [r for r in records
               if isinstance(r, dict) and _ITEM_ID.match(str(r.get("id") or ""))]
    listings = [to_listing(r, search, make=item.get("make", ""),
                           models=item.get("models") or (),
                           aliases=item.get("aliases") or ()) for r in records]
    out = Judged()
    out.sold = {MARKETPLACE_PREFIX + str(r["id"]) for r in records if r.get("sold")}
    out.pending = {MARKETPLACE_PREFIX + str(r["id"]) for r in records if r.get("pending")}
    out.on_sale = [l for l in listings if l.id not in out.sold]
    for_me, out.other = filters.not_this_car(out.on_sale, search_filters)
    out.elsewhere = [l for l in for_me if l.id in owned]
    mine = [l for l in for_me if l.id not in owned]
    out.kept, out.unpriced, out.hidden, _wrong = filters.apply(mine, search_filters)
    return out


# ------------------------------------------------------- what to search for

def _place(near: str) -> str:
    """ "Ottawa, ON" -> "ottawa", the form Marketplace uses in its paths."""
    city = str(near or "").split(",")[0].strip().lower()
    return re.sub(r"[^a-z0-9]", "", city)


# What a search asks Marketplace. A change to any of these is a new starting
# point; anything else on a plan entry (its name, its aliases) is not.
SCOPE_KEYS = ("search", "make", "models", "min_year", "max_year", "min_price",
              "max_price", "place", "latitude", "longitude", "radius_km", "queries")
# "exact" changes the queries' addresses, so it moves the scope through them.


def plan(cfg) -> list[dict[str, Any]]:
    """What the collector should look for: one entry per active search.

    Built from the search's own link and rules, so a search changed from the
    dashboard changes what Marketplace is asked, with nothing else to edit.
    """
    conf = cfg.get("marketplace", {}) or {}
    out = []
    for search in cfg.active_searches:
        if not search.marketplace:
            continue          # switched off for this search on the dashboard
        rules = cfg.rules_for(search)["filters"]
        summary = describe_search(search.url)
        make = summary.make or ""
        models = [str(m).strip() for m in filters._as_list(rules.get("models"))
                  if str(m).strip()] or ([summary.model] if summary.model else [])
        if not make and not models:
            continue
        near = str(rules.get("near") or "").strip() or ", ".join(
            p for p in (summary.location, summary.province) if p)
        point = geo.locate_reference(near) if near else None
        radius = int(conf.get("radius_km") or rules.get("max_distance_km")
                     or summary.radius_km or DEFAULT_RADIUS_KM)
        item: dict[str, Any] = {
            "search": search.id,
            "name": search.name,
            "make": make,
            "models": models,
            "min_year": rules.get("min_year") or summary.year_min,
            "max_year": rules.get("max_year") or summary.year_max,
            "min_price": rules.get("min_price") or summary.price_min,
            "max_price": rules.get("max_price") or summary.price_max,
            "place": str(conf.get("place") or _place(near)),
            "latitude": point[0] if point else None,
            "longitude": point[1] if point else None,
            "radius_km": max(1, min(radius, MAX_RADIUS_KM)),
            # Marketplace's own "exact match": fewer cars of other models
            # read, at the risk of missing a car titled some other way.
            "exact": bool(conf.get("exact")),
            # Other spellings that count as the model. They change what is
            # kept, not what is asked, so they are not part of the scope.
            "aliases": [str(a).strip() for a in filters._as_list(rules.get("aliases"))
                        if str(a).strip()] if models else [],
        }
        item["queries"] = [{"query": " ".join(p for p in (make, model) if p),
                            "url": search_url(item, " ".join(p for p in (make, model) if p))}
                           for model in (models or [""])]
        item["scope"] = hashlib.sha256(json.dumps(
            {k: item[k] for k in SCOPE_KEYS}, sort_keys=True,
            default=str).encode()).hexdigest()[:16]
        out.append(item)
    return out


def search_url(item: dict[str, Any], query: str) -> str:
    """The Marketplace search for one query, newest first."""
    params: list[tuple[str, Any]] = [("query", query)]
    for key, name in (("min_price", "minPrice"), ("max_price", "maxPrice"),
                      ("min_year", "minYear"), ("max_year", "maxYear")):
        if item.get(key):
            params.append((name, int(item[key])))
    if item.get("radius_km"):
        params.append(("radius", int(item["radius_km"])))
    params += [("sortBy", "creation_time_descend"),
               ("exact", "true" if item.get("exact") else "false")]
    place = item.get("place") or "search"
    base = SEARCH_URL.format(place=place) if place != "search" \
        else "https://www.facebook.com/marketplace/search/"
    return f"{base}?{urlencode(params)}"


# ------------------------------------------------------- taking a batch in

GONE_AFTER_DAYS = 10
# A car Facebook dates further back than this is not announced as new when it
# first scrolls into view: results are capped, so older cars drift in as
# newer ones sell.
NEW_WITHIN_DAYS = 7
STARTING_POINT = ("already for sale when Marketplace was first read for this "
                  "search, so recorded as a starting point")
SWITCHED_OFF = "Marketplace is switched off for this search"
SESSION_WORDS = {
    "signed_out": "Facebook has signed the collector out",
    "checkpoint": "Facebook wants the collector's account to confirm who it is",
    "blocked": "Facebook is refusing the collector's searches",
}


def _section(state: State) -> dict[str, Any]:
    return state.data.setdefault("marketplace", {})


def ingest(cfg, state: State, batch: dict[str, Any], *,
           env: dict[str, str] | None = None, notify: bool = True,
           dry_run: bool = False, fetcher=None):
    """Apply one batch: record, judge and announce as a check would.

    Returns the same report a check does, so the log line and the alerts read
    the same whichever site the car came from.
    """
    from .runner import RunReport, _deliver, _hide, _take
    report = RunReport(dry_run=dry_run, trigger="marketplace")
    env = env if env is not None else {}
    section = _section(state)
    now = utcnow()
    host = str(batch.get("host") or "collector")[:40]

    changes: list[Change] = []
    new_within = int((cfg.get("marketplace", {}) or {}).get("new_within_days")
                     or NEW_WITHIN_DAYS)
    # When Facebook says each car was listed, where the batch carries it.
    listed_on = {
        MARKETPLACE_PREFIX + str(r.get("id")):
            datetime.fromtimestamp(int(r["created"]), tz=timezone.utc)
        for part in batch.get("searches") or [] for r in part.get("listings") or []
        if isinstance(r, dict) and isinstance(r.get("created"), int)
        and r["created"] > 1_000_000_000}

    def silence(listing_id: str, reason: str) -> None:
        if not dry_run:
            state.silence(listing_id, reason)

    def queue(change: Change) -> None:
        listed = listed_on.get(change.listing.id)
        if change.kind == Change.NEW and listed is not None \
                and clock.now() - listed > timedelta(days=new_within):
            silence(change.listing.id,
                    f"listed on Marketplace {(clock.now() - listed).days} days "
                    f"ago, and only now among the results read")
            return
        entry = state.listings.get(change.listing.id) or {}
        yours = entry.get("you") or {}
        if yours.get("muted") or yours.get("dismissed"):
            report.your_call += 1
            silence(change.listing.id,
                    f"you {'muted' if yours.get('muted') else 'dismissed'} this car")
            return
        changes.append(change)
        if not dry_run:
            state.defer([change])

    # What the collector said about itself, whether or not it read anything.
    heard = {"at": str(batch.get("at") or now), "received": now, "host": host,
             "role": str(batch.get("role") or ""), "polled": bool(batch.get("polled")),
             "session": str(batch.get("session") or "ok")[:20],
             "note": str(batch.get("note") or "")[:300],
             # How the collector is set up, and when it means to send next:
             # shown on the dashboard, changed only on the Mac.
             "settings": _collector_settings(batch.get("settings")),
             "next_at": str(batch.get("next_at") or "")[:40] or None,
             "last_failure": _failure(batch.get("last_failure"))}
    section.setdefault("hosts", {})[host] = heard
    section["last_batch"] = heard
    if not dry_run:
        ids = section.setdefault("batch_ids", [])
        ids.append(str(batch["id"]))
        del ids[:-BATCH_IDS_KEPT]

    _session_alarm(cfg, state, report, heard, env, notify and not dry_run)
    _standby_note(cfg, state, report, heard, env, notify and not dry_run)

    by_id = {s.id: s for s in cfg.active_searches}
    wanted = {item["search"]: item for item in plan(cfg)}
    owned: set[str] = set()
    rejected: dict[str, tuple[Listing, filters.Verdict]] = {}
    read_ok: set[str] = set()
    settled: list[tuple[dict[str, Any], Judged]] = []
    wrong: set[str] = set()
    health_all = section.setdefault("searches", {})

    for part in batch.get("searches") or []:
        search = by_id.get(str(part.get("search") or ""))
        if search is None or not search.marketplace:
            # Deleted, or switched off for Marketplace, since the collector
            # last looked, or a collector not yet updated still reading it.
            continue
        health = health_all.setdefault(search.id, {})
        if not part.get("ok"):
            report.searches_failed += 1
            health["last_error"] = str(part.get("error") or "no reason given")[:300]
            health["last_error_at"] = now
            health["consecutive_failures"] = int(health.get("consecutive_failures", 0)) + 1
            report.warnings.append(f"{search.name}: Marketplace could not be "
                                   f"read ({health['last_error']})")
            continue
        report.searches_run += 1
        read_ok.add(search.id)
        item = wanted.get(search.id) or {}
        rules = cfg.rules_for(search)
        search_filters = dict(rules["filters"])
        search_notify = rules["notify_on"]

        judged = judge(cfg, search, part.get("listings") or [], item, owned)
        on_sale, sold, pending = judged.on_sale, judged.sold, judged.pending
        report.listings_seen += len(on_sale)
        for listing in on_sale:
            entry = state.listings.get(listing.id)
            if entry and entry.get("status") == "gone" \
                    and entry.get("gone_reason") == SWITCHED_OFF:
                entry["status"] = "active"
                entry.pop("gone_reason", None)
                entry.pop("removed_at", None)

        # The first batch for a search, or one read differently, is a starting
        # point: dozens of cars already for sale are not dozens of new ones.
        scope = str(part.get("scope") or "")
        baseline = health.get("scope") != scope
        health.update({"scope": scope, "last_ok": now, "last_count": len(on_sale),
                       "consecutive_failures": 0,
                       "landed": str(part.get("landed") or "")[:400],
                       "other_examples": judged.other_examples()})
        # Where every car read went, for the Status tab: most of what a loose
        # Marketplace search returns is not the model asked for, and that
        # should read as expected. Counted once every search has had its say.
        settled.append((health, judged))
        health.pop("last_error", None)
        if baseline and on_sale:
            report.baselines.append(search.name)

        if judged.other:
            report.not_this_car[search.name] = len(judged.other)
            wrong.update(l.id for l, _ in judged.other
                         if (state.listings.get(l.id) or {}).get("search_id") == search.id)
        kept, unpriced, dropped = judged.kept, judged.unpriced, judged.hidden
        owned.update(l.id for l in kept)
        owned.update(l.id for l in unpriced)
        report.unpriced += len(unpriced)

        _take(kept, unpriced, state=state, report=report, rules=rules,
              search_filters=search_filters, search_notify=search_notify,
              baseline=baseline, queue=queue, silence=silence,
              starting_point=STARTING_POINT)
        for listing, verdict in dropped:
            rejected.setdefault(listing.id, (listing, verdict))

        for lid in {l.id for l in on_sale}:
            entry = state.listings.get(lid)
            if entry is None:
                continue
            if lid in pending:
                entry["sale_pending"] = True
            else:
                entry.pop("sale_pending", None)

        for lid in sold:
            entry = state.listings.get(lid)
            if entry is None or entry.get("status") != "active":
                continue
            change = state.mark_gone(lid, "marked sold on Marketplace")
            if change is not None:
                report.removed += 1
                if search_notify.get("removed", False) and not baseline:
                    queue(change)

    _hide(rejected, owned, state=state, report=report, silence=silence)
    report.filtered_out = sum(1 for lid in rejected if lid not in owned)
    for health, judged in settled:
        health["breakdown"] = judged.breakdown(owned)

    # A car stored for a search that no longer counts it as its model - an
    # alias or a model taken off - is dropped, as a check drops AutoTrader's.
    # Not when another search kept or hid it this batch.
    wrong -= owned | set(rejected)
    if wrong and not dry_run:
        dropped = state.discard_wrong_cars(wrong)
        if dropped:
            report.discarded += len(dropped)
            report.warnings.append(
                f"dropped {len(dropped)} stored Marketplace "
                f"{'car' if len(dropped) == 1 else 'cars'} that the "
                f"{'search no longer counts' if len(dropped) == 1 else 'searches no longer count'}"
                f" as the model it is for.")

    # A search switched off for Marketplace keeps no Marketplace cars as if
    # they were still watched: they are written off, quietly, with the reason.
    # Its health goes too, so an old error does not stay on the Status tab
    # and the first read after it is switched back on is a starting point.
    off = {s.id for s in cfg.active_searches if not s.marketplace}
    for sid in off:
        health_all.pop(sid, None)
    for lid, entry in list(state.listings.items()):
        if on_marketplace(lid) and entry.get("status") == "active" \
                and entry.get("search_id") in off:
            if state.mark_gone(lid, SWITCHED_OFF):
                report.removed += 1

    # A car not seen for days while its search reads fine has gone. Absence
    # proves less here than on AutoTrader - results are capped and ranked -
    # so the wait is long and the removal is quiet unless asked for.
    cutoff = clock.now() - timedelta(days=int(
        (cfg.get("marketplace", {}) or {}).get("gone_after_days") or GONE_AFTER_DAYS))
    for lid, entry in list(state.listings.items()):
        if not on_marketplace(lid) or entry.get("status") != "active":
            continue
        if entry.get("search_id") not in read_ok:
            continue
        seen = clock.parse(entry.get("last_seen"))
        if seen is not None and seen < cutoff:
            change = state.mark_gone(lid, "not seen on Marketplace for "
                                          f"{(clock.now() - seen).days} days")
            if change is not None:
                report.removed += 1
                search = by_id.get(str(entry.get("search_id")))
                notify_on = cfg.rules_for(search)["notify_on"] if search else {}
                if notify_on.get("removed", False):
                    queue(change)

    changes = _deliver(cfg, state, report, changes, env=env, notify=notify,
                       dry_run=dry_run)

    if not dry_run and fetcher is not None and cfg.get("dashboard.photos", True):
        from . import thumbs
        try:
            shots = thumbs.sync(state.listings.values(), fetcher)
            report.photos = {"kept": shots.kept, "fetched": shots.fetched,
                             "failed": shots.failed, "pruned": shots.pruned,
                             "bytes": shots.total_bytes, "samples": shots.samples[:6]}
        except Exception as exc:  # noqa: BLE001 - a photo never fails a batch
            log.warning("photo sync failed: %s", exc)

    if not dry_run:
        runs = section.setdefault("batches", [])
        runs.insert(0, {"at": now, "host": host, "polled": heard["polled"],
                        "session": heard["session"], "searches": report.searches_run,
                        "failed": report.searches_failed, "seen": report.listings_seen,
                        "new": report.new, "removed": report.removed,
                        "notified": bool(report.notified)})
        del runs[48:]
    return report


def _collector_settings(raw: Any) -> dict[str, Any]:
    """The collector's own settings as it reported them, bounded."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("every_minutes", "jitter_minutes", "details_per_cycle", "scrolls",
                "takeover_after_minutes"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) \
                and math.isfinite(value):
            out[key] = max(0, min(int(value), 10_000))
    for key in ("quiet_start", "quiet_end"):
        if re.fullmatch(r"\d{1,2}:\d{2}", str(raw.get(key) or "")):
            out[key] = str(raw[key])
    return out


def _failure(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict) or not raw.get("error"):
        return None
    return {"at": str(raw.get("at") or "")[:40], "error": str(raw["error"])[:300]}


def _session_alarm(cfg, state: State, report, heard: dict[str, Any],
                   env: dict[str, str], notify: bool) -> None:
    """Say once when Facebook stops letting the collector in, and once when
    it lets it back in. Every batch in between is the same news."""
    from . import notifiers
    section = _section(state)
    session = heard["session"]
    told = section.get("session_told")
    if session in SESSION_WORDS:
        # A warning, not a failure: every batch until the owner signs in
        # would otherwise fail its workflow run and email them about it.
        report.warnings.append(f"Marketplace: {SESSION_WORDS[session]}.")
        if told != session and notify:
            body = (f"{SESSION_WORDS[session]}, so Marketplace is not being "
                    f"watched. On the computer that collects it ({heard['host']}), "
                    f"run:\n\n    collector/run login\n\nand sign in to Facebook "
                    f"in the window that opens.")
            report.channel_results.extend(notifiers.alert(
                cfg, "Marketplace needs you to sign in again", body, env))
        section["session_told"] = session
    elif told and session == "ok":
        section.pop("session_told", None)
        if notify:
            report.channel_results.extend(notifiers.alert(
                cfg, "Marketplace is being watched again",
                "The collector is signed in and reading Marketplace again.", env))


def _standby_note(cfg, state: State, report, heard: dict[str, Any],
                  env: dict[str, str], notify: bool) -> None:
    """Say once when a standby computer starts reading in the primary's place."""
    from . import notifiers
    section = _section(state)
    if not heard["polled"]:
        return
    if heard["role"] == "standby":
        if section.get("standby_told") != heard["host"]:
            section["standby_told"] = heard["host"]
            report.warnings.append(f"Marketplace: {heard['host']} has taken over.")
            if notify:
                report.channel_results.extend(notifiers.alert(
                    cfg, "Marketplace: the standby computer has taken over",
                    f"The primary collector went quiet, so {heard['host']} is "
                    f"reading Marketplace now. Nothing is missed; check the "
                    f"primary when you can (collector/run status on it).", env))
    elif heard["role"] == "primary":
        section.pop("standby_told", None)
