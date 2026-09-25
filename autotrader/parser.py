"""Turning an AutoTrader.ca search page into Listing records.

Several independent strategies read the page and the one that finds the most
listings wins, so a markup change degrades the parse instead of zeroing it.
The winner is reported so the dashboard can warn when the reliable strategies
stop working.
"""

from __future__ import annotations

import html as htmllib
import json
import logging
import re
from typing import Any, Iterable

from bs4 import BeautifulSoup

from .listing import Listing
from .urls import canonical_listing_url, listing_id_from_url, location_from_url

log = logging.getLogger(__name__)

# A number is 1-3 digits then comma-separated groups of three. The lookbehind
# keeps a digit that ends a model name from joining the figure after it. Space
# is not a thousands separator: it would make that case ambiguous, and card
# figures are corrected from the listing page's schema.org data anyway.
_NUMBER = r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+)"
PRICE_RE = re.compile(r"\$\s*" + _NUMBER + r"(?:\.\d{2})?")
KM_RE = re.compile(_NUMBER + r"\s*km\b", re.I)
YEAR_RE = re.compile(r"\b(19[7-9]\d|20[0-5]\d)\b")
LISTING_HREF_RE = re.compile(r"""["'(]((?:https?://[^"'()\s]*)?/a/[^"'()\s]*?/\d+_\d{5,}_[^"'()\s]*?/)""")
# Current listing links: /offers/<seo-slug>-<uuid>. The uuid is the identity;
# the slug changes whenever the seller edits the ad.
OFFER_HREF_RE = re.compile(
    r"""["'(](?:https?://[^"'()\s]*)?"""
    r"""(/offers?/[^"'()\s]*?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})""",
    re.I)

# Cheap prefilter before the costlier id parse. Accepts both the legacy /a/
# and the current /offers/ URL schemes.
_LISTING_HREF_HINTS = ("/a/", "/offer")


def _looks_like_listing_href(href: str) -> bool:
    return any(hint in href for hint in _LISTING_HREF_HINTS)

# JSON keys AutoTrader-ish payloads use for the same concepts.
_JSON_KEYS = {
    "price": ("price", "priceRaw", "listingPrice", "askingPrice", "displayPrice",
              "priceValue"),
    "mileage": ("odometer", "mileage", "mileageInKm", "kilometres", "kilometers",
                "odometerValue", "km"),
    "title": ("title", "name", "displayName", "heading", "adTitle"),
    "year": ("year", "modelYear", "vehicleYear", "registrationYear"),
    "make": ("make", "makeName", "brand"),
    "model": ("model", "modelName"),
    "trim": ("trim", "modelVersionInput", "modelVersionCustom", "trimName",
             "series", "variant"),
    "location": ("location", "city", "proximityCity", "sellerCity"),
    "province": ("province", "provinceCode", "region"),
    "seller": ("dealerName", "sellerName", "companyName", "dealer"),
    "image": ("image", "imageUrl", "thumbnail", "photoUrl", "mainPhoto", "images"),
    "url": ("url", "link", "detailUrl", "vdpUrl", "href"),
    "id": ("id", "listingId", "adId", "vehicleId"),
}


# Phrases AutoTrader shows when a search legitimately matches nothing. Only
# consulted once zero listings were parsed, so an ad containing "0 results"
# cannot mislead us.
NO_RESULTS_MARKERS = (
    "no results", "returned 0", "did not match", "no matches",
    "no vehicles", "no listings", "aucun r",
    "try broadening", "try widening", "widen your search", "broaden your search",
    "sorry, we couldn't find", "we could not find any",
)

# "0 results" must match as a whole count, not a substring ("520 results"
# contains it). A false match hides a blind parser behind "nothing matched",
# and this signal also marks a read as complete, which is what lets a missing
# listing be treated as removed.
_NO_RESULTS_PATTERNS = (
    re.compile(r"(?<![\d,.])0\s*(?:results?|listings?|vehicles?|matches|"
               r"r\u00e9sultats?)\b"),
)


def looks_like_no_results(html: str) -> bool:
    """True if the page says, in so many words, that the search matched nothing.

    This is what separates "your filters are too narrow" from "the parser has
    fallen behind the site" - both of which otherwise look like zero listings.
    """
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", (html or "")[:200000],
                  flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text).lower()
    if any(marker in text for marker in NO_RESULTS_MARKERS):
        return True
    return any(p.search(text) for p in _NO_RESULTS_PATTERNS)


class ParseResult:
    """Listings plus a note about how they were found."""

    def __init__(self, listings: list[Listing], strategy: str,
                 candidates: dict[str, int] | None = None, *,
                 declared: int | None = None, confined: bool = False,
                 off_list: int = 0) -> None:
        self.listings = listings
        self.strategy = strategy
        self.candidates = candidates or {}
        # How many results the page said it had, from its own schema.org
        # ItemList. None when the page did not say.
        self.declared = declared
        # True when that list was used to decide which rows are results.
        self.confined = confined
        # How many rows the strategies found that the page did not count as
        # results - the recommendation carousels, mostly.
        self.off_list = off_list

    def __len__(self) -> int:
        return len(self.listings)


# ---------------------------------------------------------------- helpers


# Keys a nested price or measurement object hides its number under.
_NUMERIC_KEYS = ("amount", "value", "price", "priceRaw", "consumerPrice", "raw",
                 "number", "displayValue", "amountInCents")


def _to_int(value: Any, _depth: int = 0) -> int | None:
    """Parse '12,345', 12345.0 or {'amount': 12345} -> 12345.

    Containers are unwrapped rather than stringified: str() on a list of
    prices would join them into one absurd number.
    """
    if value is None or isinstance(value, bool) or _depth > 4:
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    if isinstance(value, dict):
        for key in _NUMERIC_KEYS:
            for variant in (key, key[0].upper() + key[1:]):
                if variant in value:
                    found = _to_int(value[variant], _depth + 1)
                    if found is not None:
                        return found
        return None
    if isinstance(value, (list, tuple, set)):
        for item in value:
            found = _to_int(item, _depth + 1)
            if found is not None:
                return found
        return None
    if not isinstance(value, str):
        return None
    text = re.sub(r"[^\d.]", "", value)
    if not text:
        return None
    try:
        number = int(float(text))
    except ValueError:
        return None
    return number if number > 0 else None


def _year(value: int | None) -> int | None:
    """Reject anything that is not plausibly a model year."""
    if value is None:
        return None
    return value if 1900 <= value <= 2100 else None


def _place_from(value: Any) -> tuple[str, str]:
    """City and province from a string or a location object.

    Front-end state carries a dict (city, provinceCode, zip, street), which is
    read field by field so a raw dict never reaches a notification.
    """
    if isinstance(value, dict):
        city = (value.get("city") or value.get("addressLocality")
                or value.get("name") or value.get("locality") or "")
        province = (value.get("provinceCode") or value.get("addressRegion")
                    or value.get("province") or value.get("region") or "")
        return _titlecase_place(str(city)), _clean(str(province))[:24]
    if isinstance(value, str):
        return _titlecase_place(value), ""
    return "", ""


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(str(text or ""))).strip()


def _seller_kind(node: Any, depth: int = 0) -> str:
    """Dealer or private, from the seller's schema.org type or flags.

    It matters to a buyer: a dealer and a private seller are different
    negotiations. Only what the page says is used; a listing with no seller
    type stays empty rather than being guessed from the name.
    """
    # Bounded, so a payload whose seller refers back to itself cannot recurse
    # forever over a decorative field.
    if not isinstance(node, dict) or depth > 3:
        return ""
    kind = str(node.get("@type") or node.get("type") or "").strip()
    if kind in ("AutoDealer", "Organization", "Store", "LocalBusiness",
                "Corporation", "AutomotiveBusiness"):
        return "dealer"
    if kind in ("Person", "Individual"):
        return "private"
    if kind:
        word = _seller_word(kind)
        if word:
            return word
    # Some payloads carry it as a flag beside the seller rather than as a type.
    for key in ("sellerType", "seller_type", "isDealer", "dealer"):
        value = node.get(key)
        if isinstance(value, bool):
            return "dealer" if value else "private"
        if isinstance(value, str) and value.strip():
            word = _seller_word(value)
            if word:
                return word
    # The embedded front-end payload nests it one level down:
    #   "seller": {"dealer": {...}, "id": "...", "type": "Dealer"}
    inner = node.get("seller")
    if isinstance(inner, dict):
        return _seller_kind(inner, depth + 1)
    return ""


def _seller_word(text: str) -> str:
    low = str(text).strip().lower()
    if "dealer" in low or low in ("d", "franchise", "independent"):
        return "dealer"
    if "private" in low or low in ("p", "individual", "fsbo", "owner"):
        return "private"
    return ""


# Words that stay lowercase inside a place name, and ones that do not
# capitalise the way .capitalize() thinks they do.
_PLACE_SMALL = {"de", "des", "du", "la", "le", "les", "sur", "aux",
                "of", "the", "on", "in", "at", "by", "and", "et"}
# No province codes here: this titlecases a city, where "ON" is the word "on"
# in a hyphenated name. The province field never passes through it.
_PLACE_EXACT = {"st": "St.", "ste": "Ste.", "st.": "St.", "ste.": "Ste.",
                "mt": "Mt.", "ft": "Ft."}


def _titlecase_place(text: str) -> str:
    """Titlecase a place name that arrives in capitals, word by word.

    Dealer addresses often come all upper case. Mixed-case input is returned
    unchanged.
    """
    text = _clean(text)
    if not text or text != text.upper():
        return text

    def word(part: str, first: bool) -> str:
        low = part.lower()
        if low in _PLACE_EXACT:
            return _PLACE_EXACT[low]
        if low in _PLACE_SMALL and not first:
            return low
        # Hyphenated and apostrophe'd names capitalise each piece.
        for sep in ("-", "'", "\u2019"):
            if sep in part:
                return sep.join(word(bit, first and i == 0)
                                for i, bit in enumerate(part.split(sep)))
        return part.capitalize()

    return " ".join(word(part, i == 0)
                    for i, part in enumerate(text.split()) if part)


# Child objects a listing keeps its facts in. The current platform nests the
# car under "vehicle" (the only place the model year appears) and the asking
# price under "price".
_NESTED_FACTS = ("vehicle", "item", "listing", "ad", "details", "specs",
                 "vehicleDetails", "attributes")


def _flatten(node: dict) -> dict:
    """A view of a listing with its nested fact objects lifted to the top.

    Child keys never shadow parent ones, so a top-level value still wins.
    """
    merged = dict(node)
    for name in _NESTED_FACTS:
        child = node.get(name)
        if isinstance(child, dict):
            for key, value in child.items():
                merged.setdefault(key, value)
    return merged


def _first_key(node: dict, names: Iterable[str]) -> Any:
    for name in names:
        for key in (name, name[0].upper() + name[1:]):
            if key in node and node[key] not in (None, "", []):
                return node[key]
    return None


def _price_from_text(text: str) -> int | None:
    """Largest dollar figure in a card - AutoTrader shows the asking price
    biggest, but cards can also carry small figures like '$0 down'."""
    best: int | None = None
    for match in PRICE_RE.finditer(text):
        value = _to_int(match.group(1))
        if value and 500 <= value <= 5_000_000 and (best is None or value > best):
            best = value
    return best


def _mileage_from_text(text: str) -> int | None:
    """Odometer reading from card text.

    A card can show two ``km`` figures, distance from the searcher and then
    the odometer. The last one is taken; detail-page enrichment corrects it.
    """
    values = [v for v in (_to_int(m.group(1)) for m in KM_RE.finditer(text))
              if v is not None and v <= 2_000_000]
    return values[-1] if values else None


def _title_from_text(text: str) -> str:
    """Trim card noise down to something that reads like a car name."""
    text = _clean(text)
    # Cards start with a photo count; the name starts at the model year.
    match = YEAR_RE.search(text)
    if match:
        text = text[match.start():]
    return text[:120].strip(" -|,")


def _images_from(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        out = [value]
    elif isinstance(value, list):
        out = [v for v in value if isinstance(v, str)]
    elif isinstance(value, dict):
        for key in ("url", "src", "contentUrl", "large", "medium"):
            if isinstance(value.get(key), str):
                out = [value[key]]
                break
    return [u for u in out if u.startswith("http")][:12]


def _types_of(node: dict) -> set[str]:
    """schema.org @type, which may be a string or a list of strings."""
    raw = node.get("@type")
    if isinstance(raw, str):
        return {raw.lower()}
    if isinstance(raw, list):
        return {str(t).lower() for t in raw}
    return set()


def _iter_json_objects(node: Any, depth: int = 0) -> Iterable[dict]:
    if depth > 12:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_json_objects(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_json_objects(value, depth + 1)


def _listing_from_json(node: dict, base_url: str) -> Listing | None:
    """Build a Listing from an arbitrary JSON object, if it looks like one."""
    # Depending on the markup, the link is a plain "url", the node's "@id"
    # (with a #vehicle fragment) or inside the offer; all are tried.
    candidates: list[Any] = [_first_key(node, _JSON_KEYS["url"]), node.get("@id")]
    offer = node.get("offers")
    if isinstance(offer, dict):
        candidates.append(offer.get("url"))
    elif isinstance(offer, list) and offer and isinstance(offer[0], dict):
        candidates.append(offer[0].get("url"))

    url = None
    lid = None
    for candidate in candidates:
        if isinstance(candidate, dict):
            candidate = candidate.get("url") or candidate.get("href")
        if not isinstance(candidate, str) or not candidate:
            continue
        cleaned = candidate.split("#", 1)[0]
        found_id = listing_id_from_url(cleaned)
        if found_id:
            url, lid = cleaned, found_id
            break
        if url is None:
            url = cleaned

    if not lid:
        raw_id = _first_key(node, _JSON_KEYS["id"])
        if raw_id is not None and re.fullmatch(r"\d{5,}", str(raw_id)):
            lid = str(raw_id)
    if not lid or not isinstance(url, str):
        return None

    facts = _flatten(node)

    offers = node.get("offers")
    price = None
    currency = "CAD"
    seller_name = ""
    seller_kind = ""
    city = ""
    region = ""
    if isinstance(offers, dict):
        price = _to_int(offers.get("price"))
        currency = offers.get("priceCurrency") or "CAD"
        seller = offers.get("seller")
        if isinstance(seller, dict):
            seller_name = str(seller.get("name") or "")
            seller_kind = _seller_kind(seller)
            address = seller.get("address")
            if isinstance(address, dict):
                city = str(address.get("addressLocality") or "")
                region = str(address.get("addressRegion") or "")
    if price is None:
        price = _to_int(_first_key(facts, _JSON_KEYS["price"]))

    odo = facts.get("mileageFromOdometer")
    mileage = _to_int(odo.get("value")) if isinstance(odo, dict) else _to_int(odo)
    if mileage is None:
        mileage = _to_int(_first_key(facts, _JSON_KEYS["mileage"]))

    make = _first_key(facts, _JSON_KEYS["make"])
    if isinstance(make, dict):
        make = make.get("name")

    # A figure outside these bounds is a parsing accident, not a bargain.
    if price is not None and not (500 <= price <= 5_000_000):
        price = None
    if mileage is not None and not (0 <= mileage <= 2_000_000):
        mileage = None

    json_city, json_region = _place_from(_first_key(facts, _JSON_KEYS["location"]))
    if not json_city:
        json_city, json_region2 = _place_from(facts.get("address"))
        json_region = json_region or json_region2

    return Listing(
        id=lid,
        url=canonical_listing_url(url),
        title=_clean(_first_key(facts, _JSON_KEYS["title"]) or ""),
        year=_year(_to_int(facts.get("vehicleModelDate"))
                   or _to_int(_first_key(facts, _JSON_KEYS["year"]))),
        make=_clean(make or ""),
        model=_clean(_first_key(facts, _JSON_KEYS["model"]) or ""),
        trim=_clean(facts.get("vehicleConfiguration")
                    or _first_key(facts, _JSON_KEYS["trim"]) or ""),
        price=price,
        price_source="search" if price is not None else "",
        card_price=price,
        currency=currency if isinstance(currency, str) else "CAD",
        mileage_km=mileage,
        location=_titlecase_place(city) or json_city,
        province=_clean(region) or json_region
                 or _place_from(_first_key(facts, _JSON_KEYS["province"]))[0],
        seller=_clean(seller_name)
               or _clean(str(_first_key(facts, _JSON_KEYS["seller"]) or ""))[:80],
        seller_type=seller_kind or _seller_kind(facts),
        body=_clean(facts.get("bodyType") or facts.get("body") or ""),
        color=_clean(facts.get("color") or ""),
        transmission=_clean(facts.get("vehicleTransmission")
                            or facts.get("transmission") or ""),
        fuel=_clean(facts.get("fuelType") or facts.get("fuel") or ""),
        images=_images_from(_first_key(facts, _JSON_KEYS["image"])),
    )


# ---------------------------------------------------------------- strategies


# Keys under which the front-end blob files its results and its advertising
# rails (e.g. `props.pageProps.listings` and `props.pageProps.recommendations`).
# Matched on the key rather than the full path, so re-nested page props cannot
# let the rails back in.
_RESULT_KEYS = ("listings", "listing", "searchresults", "results", "items",
                "vehicles", "offers", "ads", "hits")
_RAIL_KEYS = ("recommendation", "recommended", "similar", "related",
              "alsoviewed", "alsolike", "youmightalsolike", "promoted",
              "promotion", "sponsored", "featured", "carousel", "nearby",
              "recentlyviewed", "recentlyseen", "history", "suggestion",
              "popular", "trending", "advert", "banner")


def _key_kind(path: list[str]) -> str:
    """"result", "rail" or "unknown" for a position in the front-end blob."""
    for key in reversed(path):
        flat = re.sub(r"[^a-z]", "", key.lower())
        if any(rail in flat for rail in _RAIL_KEYS):
            return "rail"
        if flat in _RESULT_KEYS:
            return "result"
    return "unknown"


def _blob_results(soup: BeautifulSoup, html: str, base_url: str) -> set[str]:
    """Listing ids the page's own front-end state files under its results."""
    found: set[str] = set()
    blobs: list[str] = []
    for tag in soup.find_all("script", attrs={"id": re.compile("NEXT_DATA|__STATE__", re.I)}):
        blobs.append(tag.string or tag.get_text() or "")
    for match in re.finditer(
        r"(?:window\.)?(?:__INITIAL_STATE__|__NUXT__|__APOLLO_STATE__|__PRELOADED_STATE__|"
        r"searchResults|listingsData)\s*=\s*(\{.*?\})\s*[;<]",
        html, re.S,
    ):
        blobs.append(match.group(1))

    for blob in blobs:
        blob = blob.strip()
        if not blob.startswith(("{", "[")):
            continue
        try:
            data = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            continue
        stack: list[tuple[Any, list[str]]] = [(data, [])]
        while stack:
            node, path = stack.pop()
            if isinstance(node, dict):
                if _key_kind(path) == "result":
                    listing = _listing_from_json(node, base_url)
                    if listing:
                        found.add(listing.id)
                for key, value in node.items():
                    stack.append((value, path + [str(key)]))
            elif isinstance(node, list):
                for value in node:
                    stack.append((value, path))
    return found


def _declared_results(soup: BeautifulSoup, html: str = "",
                      base_url: str = "") -> tuple[set[str], int | None]:
    """Which cars the page itself says are the results of this search.

    A results page also carries recommendation rails ("similar vehicles",
    promotions) in the same front-end blob as the results, with real ids,
    prices and photos, so a strategy that walks the blob cannot tell them
    apart from what was searched for.

    The page answers in two places and neither is complete, so they are
    unioned:

    * the schema.org ItemList is explicit, but schema.org Offer requires a
      price, so call-for-price cars are left out of it. The bot keeps unpriced
      cars on purpose, so losing them is the worst outcome here.
    * the front-end blob files results and rails under different keys, which
      catches those cars, but it is a private structure that can be renamed.

    Either naming a car makes it a result. An empty set means the page did not
    say, which is not the same as saying nothing matched.
    """
    ids: set[str] = set()
    declared: int | None = None
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _iter_json_objects(data):
            if "itemlist" not in _types_of(node):
                continue
            total = _to_int(node.get("numberOfItems"))
            if total is not None:
                declared = total if declared is None else declared + total
            for entry in node.get("itemListElement") or []:
                if not isinstance(entry, dict):
                    continue
                item = entry.get("item")
                item = item if isinstance(item, dict) else {}
                for candidate in (entry.get("url"), item.get("url"),
                                  item.get("@id"), entry.get("@id"),
                                  (item.get("offers") or {}).get("url")
                                  if isinstance(item.get("offers"), dict) else None):
                    if not isinstance(candidate, str):
                        continue
                    found = listing_id_from_url(candidate.split("#")[0])
                    if found:
                        ids.add(found)
                        break

    ids |= _blob_results(soup, html, base_url)
    return ids, declared


def _strategy_jsonld(soup: BeautifulSoup, html: str, base_url: str) -> list[Listing]:
    """schema.org markup.  The most stable thing on the page when present."""
    found: dict[str, Listing] = {}
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _iter_json_objects(data):
            # @type may be a list, e.g. ["Car","Product"], so compare as a set.
            types = _types_of(node)
            if not types & {"vehicle", "car", "product", "offer", "listitem", "itemlist"}:
                continue
            target = node.get("item") if "listitem" in types else node
            if not isinstance(target, dict):
                continue
            listing = _listing_from_json(target, base_url)
            if listing:
                found.setdefault(listing.id, listing)
    return list(found.values())


def _strategy_embedded_json(soup: BeautifulSoup, html: str, base_url: str) -> list[Listing]:
    """Front-end state blobs (__NEXT_DATA__, window.__INITIAL_STATE__, ...)."""
    found: dict[str, Listing] = {}
    blobs: list[str] = []

    for tag in soup.find_all("script", attrs={"id": re.compile("NEXT_DATA|__STATE__", re.I)}):
        blobs.append(tag.string or tag.get_text() or "")
    for match in re.finditer(
        r"(?:window\.)?(?:__INITIAL_STATE__|__NUXT__|__APOLLO_STATE__|__PRELOADED_STATE__|"
        r"searchResults|listingsData)\s*=\s*(\{.*?\})\s*[;<]",
        html, re.S,
    ):
        blobs.append(match.group(1))

    for blob in blobs:
        blob = blob.strip()
        if not blob.startswith(("{", "[")):
            continue
        try:
            data = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _iter_json_objects(data):
            listing = _listing_from_json(node, base_url)
            if listing:
                found.setdefault(listing.id, listing)
    return list(found.values())


def _listing_ids_under(node) -> set[str]:
    ids = set()
    for anchor in node.find_all("a", href=True):
        href = anchor["href"]
        if not _looks_like_listing_href(href):
            continue
        lid = listing_id_from_url(href)
        if lid:
            ids.add(lid)
    return ids


def _card_for(anchor) -> Any:
    """The card element wrapping a listing link.

    Climb as far as possible while the ancestor still covers exactly one
    listing.  Text length is a bad boundary (a terse card and a whole results
    container can be the same size); "does this ancestor swallow a second car"
    is precise and does not depend on class names.
    """
    node = anchor
    for _ in range(8):
        parent = getattr(node, "parent", None)
        if parent is None or getattr(parent, "name", None) in (None, "body", "html", "[document]"):
            break
        if len(_listing_ids_under(parent)) > 1:
            break
        node = parent
    return node


# Results cards label each fact with a data-testid, which survives a restyle
# where class names (carrying a build hash) do not, so test ids are tried
# first. The text-scraping fallbacks cover markup without them.
_TESTID_TITLE = ("listing-title", "ad-title", "title")
_TESTID_PRICE = ("regular-price", "price", "listing-price", "search-price")
_TESTID_MILEAGE = ("VehicleDetails-mileage_odometer", "mileage", "odometer")
_TESTID_GEARBOX = ("VehicleDetails-gearbox", "transmission")
_TESTID_FUEL = ("VehicleDetails-gas_pump", "fuel")
_TESTID_SELLER = ("sellerinfo-company-name", "seller-name", "dealer-name")
_TESTID_ADDRESS = ("sellerinfo-address", "seller-address", "location")

# Figures on a card that are not what the seller is asking today.
_WRONG_FIGURE = ("suggested", "msrp", "retail", "old", "was", "monthly",
                 "finance", "lease", "saving", "discount", "tax")

# Photo hosts, and the paths on them that are the car rather than the furniture.
_PHOTO_HINTS = ("vehicleimages", "/vehicles/", "listing-images")
_NOT_PHOTO_HINTS = ("dealer-info", "/logo", "placeholder", "/assets/", "/icons/")


def _testid_text(card: Any, names: Iterable[str], *, fallback: bool = True) -> str:
    """Text of the first element on the card carrying one of these test ids."""
    if not hasattr(card, "find"):
        return ""
    for name in names:
        node = card.find(attrs={"data-testid": name})
        if node is not None:
            text = _clean(node.get_text(" ", strip=True))
            if text:
                return text
    if not fallback:
        return ""
    # Fall back to a substring match, so "VehicleDetails-mileage_odometer_v2"
    # still answers to "mileage".
    lowered = [n.lower() for n in names]
    for node in card.find_all(attrs={"data-testid": True}):
        tid = str(node.get("data-testid", "")).lower()
        # A loose match on "price" could return an MSRP, an old price or a
        # monthly payment, which is worse than no price because it looks real.
        if any(wrong in tid for wrong in _WRONG_FIGURE):
            continue
        if any(name in tid for name in lowered):
            text = _clean(node.get_text(" ", strip=True))
            if text:
                return text
    return ""


def _card_price(card: Any, card_text: str) -> int | None:
    """The figure the seller is asking, or None.

    A card can also carry an MSRP, an old price or a monthly payment; any of
    them is worse than no price, since it looks real and can trigger a false
    price-drop alert. A labelled asking price is used when present. A card
    that labels prices but not the asking price yields None rather than a
    scraped number; only a card with no labelled prices is read as text.
    """
    exact = _testid_text(card, _TESTID_PRICE, fallback=False)
    if exact:
        return _price_from_text(exact)

    labelled = False
    if hasattr(card, "find_all"):
        labelled = any("price" in str(node.get("data-testid", "")).lower()
                       for node in card.find_all(attrs={"data-testid": True}))
    if labelled:
        return _price_from_text(_testid_text(card, _TESTID_PRICE))

    return _price_from_text(card_text)


def _card_photo(card: Any) -> str:
    """The first image on the card that is actually the car."""
    if not hasattr(card, "find_all"):
        return ""
    candidates = []
    for img in card.find_all("img"):
        src = (img.get("src") or img.get("data-src") or
               img.get("data-original") or img.get("srcset") or "")
        src = src.split()[0].split(",")[0] if " " in src or "," in src else src
        if not src.startswith("http"):
            continue
        low = src.lower()
        if any(bad in low for bad in _NOT_PHOTO_HINTS):
            continue
        # A gallery image is the car by definition; anything else has to look
        # like one, so a dealer logo on the same CDN is not taken for the car.
        if str(img.get("data-testid", "")).startswith("list-gallery"):
            return src
        if any(hint in low for hint in _PHOTO_HINTS):
            candidates.append(src)
    return candidates[0] if candidates else ""


def _strategy_anchors(soup: BeautifulSoup, html: str, base_url: str) -> list[Listing]:
    """Listing links plus whatever the surrounding card says.

    Third choice, because card markup is the part of a page most likely to
    change. Cards carry the seller, city and transmission as well.
    """
    found: dict[str, Listing] = {}
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not _looks_like_listing_href(href):
            continue
        url = href if href.startswith("http") else _join(base_url, href)
        lid = listing_id_from_url(url)
        if not lid or lid in found:
            continue
        card = _card_for(anchor)
        card_text = _clean(card.get_text(" ", strip=True)) if card else ""

        title = _testid_text(card, _TESTID_TITLE)
        if not title:
            heading = card.find(["h1", "h2", "h3", "h4"]) if hasattr(card, "find") else None
            if heading is not None:
                title = _clean(heading.get_text(" ", strip=True))
        if not title:
            title = _clean(anchor.get("title") or "")
        if not title:
            title = _title_from_text(_clean(anchor.get_text(" ", strip=True)) or card_text)

        price = _card_price(card, card_text)
        mileage = _mileage_from_text(_testid_text(card, _TESTID_MILEAGE)) \
            or _mileage_from_text(card_text)

        city = province = ""
        address = _testid_text(card, _TESTID_ADDRESS)
        if address:
            # Usually "CITY, XX", with a province code after the last comma.
            head, _, tail = address.rpartition(",")
            if head and len(tail.strip()) <= 3:
                city, province = _titlecase_place(head), tail.strip().upper()
            else:
                city = _titlecase_place(address)

        image = _card_photo(card)
        found[lid] = Listing(
            id=lid,
            url=canonical_listing_url(url),
            title=title or f"AutoTrader listing {lid}",
            price=price,
            price_source="search" if price is not None else "",
            card_price=price,
            mileage_km=mileage,
            location=city,
            province=province,
            seller=_testid_text(card, _TESTID_SELLER),
            transmission=_testid_text(card, _TESTID_GEARBOX),
            fuel=_testid_text(card, _TESTID_FUEL),
            images=[image] if image else [],
        )
    return list(found.values())


def _strategy_regex(soup: BeautifulSoup, html: str, base_url: str) -> list[Listing]:
    """Last resort: pull listing URLs straight out of the raw HTML.

    Works even on a page rendered entirely by JavaScript, as long as the links
    appear somewhere in the payload. It recovers only that a car exists and
    where, which is what still works when every markup assumption has failed.
    """
    found: dict[str, Listing] = {}
    for pattern in (LISTING_HREF_RE, OFFER_HREF_RE):
        for match in pattern.finditer(html):
            href = htmllib.unescape(match.group(1))
            url = href if href.startswith("http") else _join(base_url, href)
            lid = listing_id_from_url(url)
            if lid and lid not in found:
                found[lid] = Listing(id=lid, url=canonical_listing_url(url),
                                     title=f"AutoTrader listing {lid}")
    return list(found.values())


def _join(base: str, href: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base or "https://www.autotrader.ca/", href)


# Order matters: earlier strategies produce richer, more stable records, so a
# tie is broken in their favour.
STRATEGIES = (
    ("jsonld", _strategy_jsonld),
    ("embedded_json", _strategy_embedded_json),
    ("anchors", _strategy_anchors),
    ("regex", _strategy_regex),
)


def parse_search_page(html: str, base_url: str = "https://www.autotrader.ca/") -> ParseResult:
    """Extract listings from a search results page using every strategy."""
    soup = BeautifulSoup(html or "", "html.parser")
    results: dict[str, list[Listing]] = {}
    counts: dict[str, int] = {}

    for name, strategy in STRATEGIES:
        try:
            listings = strategy(soup, html or "", base_url)
        except Exception as exc:  # noqa: BLE001 - one broken strategy must not
            log.warning("parser strategy %s failed: %s", name, exc)  # kill the run
            listings = []
        results[name] = listings
        counts[name] = len(listings)

    # What the page declares as its results (see _declared_results). The
    # winner is chosen by row count, which recommendation rails inflate.
    declared_ids, declared = _declared_results(soup, html or "", base_url)

    best_name = max(STRATEGIES, key=lambda s: counts.get(s[0], 0))[0]
    best = results.get(best_name, [])
    if not best:
        return ParseResult([], "none", counts, declared=declared)

    # Merge the runners-up in: a weaker strategy may still know a price the
    # winner missed, and it costs nothing to fold that in.
    merged: dict[str, Listing] = {}
    for listing in best:
        listing.source = best_name
        if not listing.location:
            listing.location, listing.province = location_from_url(listing.url)
        merged[listing.id] = listing
    for name, _ in STRATEGIES:
        if name == best_name:
            continue
        for listing in results.get(name, []):
            if listing.id in merged:
                merged[listing.id].merge(listing)
            elif listing.id in declared_ids:
                # A declared result the winner missed. The page says it is a
                # result, so it is kept.
                listing.source = name
                if not listing.location:
                    listing.location, listing.province = location_from_url(listing.url)
                merged[listing.id] = listing

    off_list = 0
    confined = False
    if declared_ids:
        keep = {lid: row for lid, row in merged.items() if lid in declared_ids}
        # Refusing the declared set falls back to the whole page, which is
        # noisier but cannot lose a car; that matters because the confined set
        # later authorises dropping stored rows. Refuse when none of the
        # declared ids could be built (an unfamiliar page shape), or when
        # fewer resolved than the page declares (a sample, not an answer).
        short = declared is not None and declared > 0 and len(keep) < declared
        if keep and not short:
            off_list = len(merged) - len(keep)
            merged = keep
            confined = True

    return ParseResult(list(merged.values()), best_name, counts,
                       declared=declared, confined=confined, off_list=off_list)
