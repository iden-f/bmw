"""Enriching a listing from its detail page.

AutoTrader.ca listing pages carry a schema.org ``Vehicle`` block with the
price, odometer, colour, transmission, drivetrain, VIN and the real photo
URLs, which makes it the most reliable source of listing detail. Photos come
from that block, not from the page's ``<img>`` tags, which are mostly logos.
"""

from __future__ import annotations

import html as htmllib
import json
import logging
import re
from typing import Any

from bs4 import BeautifulSoup

from .listing import Listing
from .parser import _clean, _titlecase_place, _to_int, _types_of
from .urls import listing_id_from_url, location_from_url

log = logging.getLogger(__name__)

_DRIVETRAIN = {
    "allwheeldriveconfiguration": "AWD",
    "fourwheeldriveconfiguration": "4WD",
    "frontwheeldriveconfiguration": "FWD",
    "rearwheeldriveconfiguration": "RWD",
}


def _vehicle_blocks(html: str) -> list[dict]:
    """All schema.org Vehicle objects on the page."""
    soup = BeautifulSoup(html or "", "html.parser")
    out: list[dict] = []
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _walk(data):
            # @type may be a list, e.g. ["Car","Product"].
            if isinstance(node, dict) and _types_of(node) & {"vehicle", "car"}:
                out.append(node)
    return out


def _walk(node: Any, depth: int = 0):
    if depth > 10:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value, depth + 1)


_CANONICAL_RE = re.compile(
    r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)', re.I)


def _canonical_url(html: str) -> str:
    match = _CANONICAL_RE.search(html or "")
    return htmllib.unescape(match.group(1)).strip() if match else ""


def _og_meta(html: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in re.finditer(
        r'<meta[^>]+(?:property|name)=["\'](og:[a-z:]+)["\'][^>]+content=["\']([^"\']*)["\']',
        html or "", re.I,
    ):
        out.setdefault(match.group(1).lower(), match.group(2))
    return out


def detail_from_html(html: str, listing_id: str = "", url: str = "") -> Listing | None:
    """Build a rich Listing from a detail page.  Returns None if unusable."""
    blocks = _vehicle_blocks(html)
    meta = _og_meta(html)
    if not blocks and not meta:
        return None

    node: dict[str, Any] = blocks[0] if blocks else {}

    offers = node.get("offers") if isinstance(node.get("offers"), dict) else {}
    price = _to_int(offers.get("price"))
    currency = offers.get("priceCurrency") or "CAD"

    odo = node.get("mileageFromOdometer")
    mileage = _to_int(odo.get("value")) if isinstance(odo, dict) else _to_int(odo)

    brand = node.get("brand")
    make = brand.get("name") if isinstance(brand, dict) else brand

    engine = node.get("vehicleEngine") if isinstance(node.get("vehicleEngine"), dict) else {}

    drive = str(node.get("driveWheelConfiguration") or "").rsplit("/", 1)[-1].lower()

    images = [i for i in (node.get("image") or []) if isinstance(i, str) and i.startswith("http")]
    if not images and meta.get("og:image", "").startswith("http"):
        images = [meta["og:image"]]

    vin = _clean(node.get("vehicleIdentificationNumber") or "")
    # AutoTrader masks the VIN on public pages; storing XXXX... helps nobody.
    if set(vin.upper()) <= {"X"} or len(vin) < 11:
        vin = ""

    def _real(value: str) -> str:
        """Drop AutoTrader's placeholder values so the UI can hide the field."""
        return "" if value.strip().lower() in {
            "not specified", "unspecified", "unknown", "n/a", "other", "-",
        } else value

    title = _clean(node.get("name") or meta.get("og:title") or "")
    # og:title is a long SEO string: "<year> <make> <model> | $<price> | <km> | ..."
    if "|" in title:
        title = title.split("|")[0].strip()

    listing = Listing(
        id=str(listing_id or ""),
        url=url or _clean(node.get("url") or meta.get("og:url") or ""),
        title=title,
        year=_to_int(node.get("vehicleModelDate")),
        make=_clean(make or ""),
        model=_clean(node.get("model") or ""),
        trim=_clean(node.get("vehicleConfiguration") or ""),
        price=price,
        price_source="detail" if price is not None else "",
        currency=currency if isinstance(currency, str) else "CAD",
        mileage_km=mileage,
        body=_real(_clean(node.get("bodyType") or "")),
        color=_real(_clean(node.get("color") or "")),
        transmission=_real(_clean(node.get("vehicleTransmission") or "")),
        drivetrain=_DRIVETRAIN.get(drive, ""),
        fuel=_real(_clean(engine.get("fuelType") or "")),
        engine=_real(_clean(engine.get("engineType") or "")),
        vin=vin,
        images=images[:12],
        enriched=True,
    )
    # Legacy listing URLs encode the city in the path; current ones carry the
    # seller's address inside the offer instead.
    listing.location, listing.province = location_from_url(listing.url or url)
    if not listing.location and isinstance(offers, dict):
        seller = offers.get("seller")
        if isinstance(seller, dict):
            listing.seller = _clean(seller.get("name") or "")
            address = seller.get("address")
            if isinstance(address, dict):
                listing.location = _titlecase_place(str(address.get("addressLocality") or ""))
                listing.province = _clean(str(address.get("addressRegion") or ""))
    # The trim often repeats the body style ("Sport Sedan" with body "Sedan").
    if listing.trim and listing.body and listing.trim.lower().endswith(listing.body.lower()):
        trimmed = listing.trim[: -len(listing.body)].strip()
        if trimmed:
            listing.trim = trimmed
    return listing


def page_identifies(html: str, listing_id: str) -> bool | None:
    """Does this page say it is the car we asked for?

    True, False, or None when the page does not name itself. A removed listing
    is often redirected to another car rather than answered with a 404, and
    taking that car's price would look real and fire a false price-drop alert.
    """
    if not listing_id:
        return None
    claimed = ""
    for candidate in (_og_meta(html).get("og:url", ""),
                      _canonical_url(html)):
        if candidate:
            claimed = candidate
            break
    if not claimed:
        return None
    found = listing_id_from_url(claimed)
    return None if not found else found == listing_id


def enrich(listing: Listing, html: str) -> Listing:
    """Merge detail-page facts into ``listing``, preferring the detail page."""
    if page_identifies(html, listing.id) is False:
        # Some other car's page. Leave this listing exactly as the search card
        # described it rather than overwriting it with a stranger's facts.
        log.warning("detail page for %s describes a different listing; "
                    "not enriching", listing.id)
        return listing
    detail = detail_from_html(html, listing.id, listing.url)
    if detail is None:
        return listing
    # Detail-page values are authoritative for the fields a search card guesses
    # badly (price and odometer especially - see _mileage_from_text).
    for name in ("price", "price_source", "mileage_km", "year", "make", "model",
                 "trim", "body", "color", "transmission", "drivetrain", "fuel",
                 "engine", "vin", "currency"):
        value = getattr(detail, name)
        if value not in (None, "", []):
            setattr(listing, name, value)
    if detail.images:
        listing.images = detail.images
    if detail.title and len(detail.title) < len(listing.title or "x" * 999):
        listing.title = detail.title
    listing.enriched = True
    return listing
