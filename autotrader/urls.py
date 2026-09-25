"""Parsing and normalising autotrader.ca URLs.

A watch is configured by pasting a search link, which already encodes what to
watch. This module:

* recognises a pasted link and explains it in plain English,
* pulls the canonical listing id out of a listing link,
* builds page-2, page-3, ... variants of a search link for pagination.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlunparse

# autotrader.ca and its French sister site share the same markup and ids.
KNOWN_HOSTS = {"www.autotrader.ca", "autotrader.ca", "www.autohebdo.net", "autohebdo.net"}

# Listing links of the form /a/<make>/<model>/<city>/<province>/<ids>/, the
# last segment being e.g. "19_10000001_". The middle group is the stable
# listing id; the first is a dealer/source code and the third (often empty) an
# opaque reference that changes between renders.
LISTING_PATH_RE = re.compile(r"/a/(?:[^/]+/)*?(\d+)_(\d{5,})_([^/]*)/?", re.I)

# Listing links of the form /offers/<slug>-<uuid> (the AutoScout24 platform).
# The trailing UUID is the listing's identity; the slug is SEO text that
# changes when the ad is edited, and its "cat_..." part is a search token
# shared by every result on a page.
OFFER_PATH_RE = re.compile(
    r"/offers?/[^/?#]*?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.I)

# Fallback for links we cannot fully structure but that still carry an id.
LOOSE_ID_RE = re.compile(r"[_/-](\d{5,})(?:[_/?#]|$)")

_TRUE = {"1", "true", "yes", "on"}


def is_autotrader_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in KNOWN_HOSTS


def is_listing_url(url: str) -> bool:
    return bool(listing_id_from_url(url))


def listing_id_from_url(url: str) -> str | None:
    """Return the canonical listing id (numeric, or a UUID), or None.

    Only the path is considered: query strings on autotrader.ca carry tracking
    numbers (``urp=3``, ``sprx=-2``) that a looser regex would happily mistake
    for an id.
    """
    if not url:
        return None
    try:
        path = urlparse(url).path
    except ValueError:
        return None
    m = LISTING_PATH_RE.search(path)
    if m:
        return m.group(2)
    m = OFFER_PATH_RE.search(path)
    if m:
        return m.group(1).lower()
    m = LOOSE_ID_RE.search(path)
    return m.group(1) if m else None


def _place(slug: str) -> str:
    """Title-case a place name, keeping hyphens (Trois-Rivieres, Saint-Hubert)."""
    text = unquote(slug).replace("+", " ")
    return "-".join(_titlecase(part) for part in text.split("-") if part)


def is_offer_url(url: str) -> bool:
    """True for /offers/<slug>-<uuid> listing links."""
    try:
        return bool(OFFER_PATH_RE.search(urlparse(url).path))
    except ValueError:
        return False


def location_from_url(url: str) -> tuple[str, str]:
    """Pull ``(city, province)`` out of a listing path.

    Works for ``/a/<make>/<model>/<city>/<province>/<ids>/`` links, whose
    page data carries no location; other links give ``("", "")``.
    """
    try:
        path = urlparse(url).path
    except ValueError:
        return "", ""
    segments = [s for s in path.split("/") if s]
    if not segments or segments[0].lower() != "a":
        # /offers/<slug>-<uuid> has no location in the path; the page's
        # structured data carries the seller's address instead.
        return "", ""
    # The id segment looks like "19_10000001_"; city and province are the two
    # segments immediately before it.
    for index, segment in enumerate(segments):
        if re.fullmatch(r"\d+_\d{5,}_.*", segment):
            if index < 3:
                return "", ""
            return (_place(segments[index - 2]), _place(segments[index - 1]))
    return "", ""


def canonical_listing_url(url: str) -> str:
    """Strip tracking query parameters so the same car always has one URL."""
    try:
        parts = urlparse(url)
    except ValueError:
        return url
    host = (parts.hostname or "www.autotrader.ca").lower()
    if host in {"autotrader.ca", "www.autohebdo.net", "autohebdo.net"}:
        host = "www.autotrader.ca"
    # Some pages emit hrefs with a literal space in the city segment.
    path = quote(unquote(parts.path), safe="/%")
    return urlunparse(("https", host, path, "", "", ""))


def normalise_search_url(url: str) -> str:
    """Clean a pasted search URL without changing what it searches for."""
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        parts = urlparse(url)
    except ValueError:
        return url
    host = (parts.hostname or "").lower()
    if host in {"autotrader.ca"}:
        host = "www.autotrader.ca"
    path = parts.path or "/"
    if not path.endswith("/") and "." not in path.rsplit("/", 1)[-1]:
        path += "/"
    # Drop analytics-only parameters so two pastes of the same search match.
    drop = {"gclid", "fbclid", "msclkid", "utm_source", "utm_medium",
            "utm_campaign", "utm_term", "utm_content", "ursrc", "urp", "urm"}
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in drop]
    return urlunparse(("https", host, path, "", urlencode(query), ""))


def page_url(url: str, page: int, per_page: int | None = None) -> str:
    """Return ``url`` for a 1-indexed results page.

    Both pagination schemes are written: ``rcs`` (zero-based index of the
    first result) with ``rcp`` (results per page), and the AutoScout24
    platform's ``page`` (fixed at 20 per page). Each platform ignores the
    other's parameters, and without ``page`` every later page repeats page 1.
    """
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if per_page:
        query["rcp"] = str(per_page)
    rcp = _as_int(query.get("rcp")) or per_page or 100
    query["rcp"] = str(rcp)
    query["rcs"] = str(max(0, (page - 1) * rcp))
    if page > 1:
        query["page"] = str(page)
    else:
        query.pop("page", None)
    return urlunparse((parts.scheme or "https", parts.netloc, parts.path, "",
                       urlencode(query), ""))


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# Tagged path segments on the AutoScout24 platform: va_<trim>, reg_<province>,
# cit_<city>, mcat_<category token>. None of them is a make or a model.
_TAGGED_SEGMENT = re.compile(r"^(?:va|reg|cit|mcat)_", re.I)


def _tagged(segment: str) -> bool:
    return bool(_TAGGED_SEGMENT.match(segment or ""))


def _body(value: str | None) -> str | None:
    """What the link says about body style, without inventing a legend.

    Named styles (``body=Coupe``) are shown as-is. Numeric codes
    (``body=2,5``) have no published key, so they read as "some body styles
    only" rather than a guessed name or nothing at all.
    """
    if not value:
        return None
    text = unquote(value).replace("+", " ").strip()
    if not text:
        return None
    if re.fullmatch(r"[\d\s,]+", text):
        return "some body styles only"
    return text


def _range(value: str | None) -> tuple[int | None, int | None]:
    """Parse autotrader's ``low,high`` range syntax (either side may be blank)."""
    if not value:
        return None, None
    raw = unquote(value)
    low, _, high = raw.partition(",")
    return _as_int(low), _as_int(high)


@dataclass
class SearchSummary:
    """A plain-English reading of a pasted search link, for the UI."""

    make: str | None = None
    model: str | None = None
    year_min: int | None = None
    year_max: int | None = None
    price_min: int | None = None
    price_max: int | None = None
    mileage_max: int | None = None
    location: str | None = None
    radius_km: int | None = None   # None means "no distance limit"
    province: str | None = None
    body: str | None = None
    condition: str | None = None
    keywords: str | None = None
    excludes_damaged: bool = False
    per_page: int | None = None
    valid: bool = True
    problems: list[str] = field(default_factory=list)

    def title(self) -> str:
        """A short default name for the watch, e.g. '2021+ Honda Civic'."""
        bits: list[str] = []
        if self.year_min and self.year_max:
            bits.append(f"{self.year_min}-{self.year_max}")
        elif self.year_min:
            bits.append(f"{self.year_min}+")
        elif self.year_max:
            bits.append(f"up to {self.year_max}")
        if self.make:
            bits.append(self.make)
        if self.model:
            bits.append(self.model)
        if not bits:
            bits.append("AutoTrader search")
        return " ".join(bits)

    def describe(self) -> list[str]:
        """Human-readable chips describing the search, for the settings UI."""
        out: list[str] = []
        if self.price_min and self.price_max:
            out.append(f"${self.price_min:,}-${self.price_max:,}")
        elif self.price_max:
            out.append(f"under ${self.price_max:,}")
        elif self.price_min:
            out.append(f"over ${self.price_min:,}")
        if self.mileage_max:
            out.append(f"under {self.mileage_max:,} km")
        if self.body:
            out.append(self.body)
        if self.condition:
            out.append(self.condition)
        if self.excludes_damaged:
            out.append("no damaged listings")
        if self.location:
            out.append(f"near {self.location}"
                       + (f" ({self.radius_km:,} km)" if self.radius_km else ""))
        elif self.province:
            out.append(self.province)
        elif self.radius_km is None:
            out.append("Canada-wide")
        if self.keywords:
            out.append(f'"{self.keywords}"')
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "make": self.make, "model": self.model,
            "year_min": self.year_min, "year_max": self.year_max,
            "price_min": self.price_min, "price_max": self.price_max,
            "mileage_max": self.mileage_max, "location": self.location,
            "radius_km": self.radius_km, "province": self.province,
            "body": self.body, "condition": self.condition,
            "keywords": self.keywords, "per_page": self.per_page,
            "excludes_damaged": self.excludes_damaged,
            "valid": self.valid, "problems": self.problems,
            "title": self.title(), "chips": self.describe(),
        }


# Makes and trims that are acronyms, so ".title()" would mangle them.
_ACRONYMS = {"bmw", "gmc", "ram", "mg", "vw", "amg", "srt", "gti", "sti",
             "gt", "rs", "ev", "suv", "gls", "glc", "gle", "cx"}


def _titlecase(slug: str) -> str:
    words = [w for w in re.split(r"[-_+]", unquote(slug)) if w]
    out = []
    for w in words:
        low = w.lower()
        # Short designations with a digit, like "a4" or "rx8", read better
        # upper-cased.
        if low in _ACRONYMS or (len(w) <= 3 and any(c.isdigit() for c in w)):
            out.append(w.upper())
        else:
            out.append(w.title())
    return " ".join(out)


def describe_search(url: str) -> SearchSummary:
    """Read a pasted search URL and explain what it will watch."""
    s = SearchSummary()
    url = normalise_search_url(url)
    if not url:
        s.valid = False
        s.problems.append("Paste an AutoTrader search link first.")
        return s
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    if host not in KNOWN_HOSTS:
        s.valid = False
        s.problems.append(
            f"{host or 'That link'} is not autotrader.ca - paste a link from "
            "autotrader.ca (or autohebdo.net)."
        )
        return s
    if is_listing_url(url) or is_offer_url(url):
        s.valid = False
        s.problems.append(
            "That is a single car listing, not a search. Run the search on "
            "autotrader.ca first, then copy the address bar."
        )
        return s

    segments = [p for p in parts.path.split("/") if p]
    q = dict(parse_qsl(parts.query, keep_blank_values=True))

    # Path shapes: /cars/<make>/<model>/<province>/<city>/, or on the
    # AutoScout24 platform /cars/<make>/<model>/va_<trim>/reg_<prov>/cit_<city>/
    city_in_path = ""
    if segments and segments[0].lower() in {"cars", "autos"}:
        rest = segments[1:]
        provinces = {"ab", "bc", "mb", "nb", "nl", "ns", "nt", "nu",
                     "on", "pe", "qc", "sk", "yt"}
        if rest and rest[0].lower() not in provinces and not _tagged(rest[0]):
            s.make = _titlecase(rest[0])
            rest = rest[1:]
            if rest and rest[0].lower() not in provinces and not _tagged(rest[0]):
                s.model = _titlecase(rest[0])
                rest = rest[1:]
        if rest and rest[0].lower() in provinces:
            s.province = rest[0].upper()
        for segment in rest:
            tag, _, value = segment.partition("_")
            if not value:
                continue
            if tag.lower() == "va":
                # The trim has its own segment and belongs in the model name;
                # without it the search would be named after a broader model.
                trim = _titlecase(value)
                if s.model and trim.lower().startswith(s.model.lower()):
                    s.model = trim
                elif s.model:
                    s.model = f"{s.model} {trim}"
                else:
                    s.model = trim
            elif tag.lower() == "reg" and value.lower() in provinces:
                s.province = value.upper()
            elif tag.lower() == "cit":
                city_in_path = _place(value)
    elif not segments:
        s.problems.append(
            "That looks like the autotrader.ca home page. Run a search first, "
            "then copy the address bar."
        )
        s.valid = False
        return s

    s.year_min, s.year_max = _range(q.get("yRng"))
    s.price_min, s.price_max = _range(q.get("pRng"))
    _, s.mileage_max = _range(q.get("odRng"))
    s.location = unquote(q["loc"]).replace("+", " ").strip() if q.get("loc") else None
    s.province = s.province or (unquote(q["prv"]).replace("+", " ") if q.get("prv") else None)
    s.body = _body(q.get("body"))
    s.keywords = unquote(q["kwd"]).replace("+", " ") if q.get("kwd") else None
    s.per_page = _as_int(q.get("rcp")) or _as_int(q.get("size"))

    prx = _as_int(q.get("prx"))
    # Negative proximity means "no radius limit" (province-wide or national).
    s.radius_km = prx if prx is not None and prx > 0 else None

    sts = unquote(q.get("sts", "")).strip()
    if sts and sts.lower() not in {"new-used", "used-new"}:
        s.condition = sts.replace("-", " & ")

    # The AutoScout24 platform names these parameters differently; without
    # them its links would read as "any year, Canada-wide".
    if s.year_min is None:
        s.year_min = _as_int(q.get("modelyearfrom"))
    if s.year_max is None:
        s.year_max = _as_int(q.get("modelyearto"))
    if s.price_min is None:
        s.price_min = _as_int(q.get("pricefrom"))
    if s.price_max is None:
        s.price_max = _as_int(q.get("priceto"))
    if s.mileage_max is None:
        s.mileage_max = _as_int(q.get("kmto")) or _as_int(q.get("mileageto"))
    if not s.location:
        # zip= carries whatever was typed into the location box, which is a
        # place name as often as a postal code.
        s.location = (unquote(q["zip"]).replace("+", " ").strip()
                      if q.get("zip") else None) or city_in_path or None
    if s.radius_km is None:
        zipr = _as_int(q.get("zipr"))
        s.radius_km = zipr if zipr and zipr > 0 else None
    if not s.condition:
        offer = {p.strip().upper() for p in unquote(q.get("offer", "")).split(",") if p.strip()}
        if offer == {"U"}:
            s.condition = "used"
        elif offer == {"N"}:
            s.condition = "new"
    if unquote(q.get("damaged_listing", "")).strip().lower() == "exclude":
        s.excludes_damaged = True

    if not q:
        s.problems.append(
            "This link has no search filters on it, so it will match a very "
            "broad set of listings."
        )
    return s
