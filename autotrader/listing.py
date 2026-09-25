"""The Listing record shared by the scraper, the notifier and the dashboard."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

_YEAR_RE = re.compile(r"\b(19[7-9]\d|20[0-5]\d)\b")

_EDGE_SEPARATOR = re.compile(r"^[\s|,/·\-]+|[\s|,/·\-]+$")

# What the parser writes when a results card carries no readable name.
PLACEHOLDER_TITLE = "AutoTrader listing "


# Where a dealer's hand-typed feature list starts, shared by the trim and the
# title so both are cut the same way. Dealers type " I " for a pipe and use
# " * " as a bullet. The slash and star must be spaced: unspaced, they would
# cut real names such as "w/ Sport Package" or a trim ending in "*".
FEATURE_LIST_SEPARATORS = ("|", ",", " / ", " * ", " I ")


def before_the_feature_list(text: str) -> str:
    """Everything up to the first separator a dealer used as a bullet."""
    text = (text or "").strip()
    for separator in FEATURE_LIST_SEPARATORS:
        if separator in text:
            return text.split(separator)[0].strip()
    return text


@dataclass
class Listing:
    id: str
    # Defaulted so a state entry missing a field still loads instead of raising.
    url: str = ""
    title: str = ""
    year: int | None = None
    make: str = ""
    model: str = ""
    trim: str = ""
    price: int | None = None
    # Where the price came from: "search" (a results card) or "detail" (the
    # listing page's JSON-LD). Card and detail prices routinely disagree, so
    # they are never compared with each other - see State.record.
    price_source: str = ""
    # The results card's figure, kept after the detail page overwrites `price`,
    # so a real change can be told from a standing card/detail disagreement
    # without re-fetching the car every run.
    card_price: int | None = None
    currency: str = "CAD"
    mileage_km: int | None = None
    location: str = ""
    province: str = ""
    seller: str = ""
    seller_type: str = ""
    body: str = ""
    color: str = ""
    transmission: str = ""
    drivetrain: str = ""
    fuel: str = ""
    engine: str = ""
    vin: str = ""
    images: list[str] = field(default_factory=list)
    # Your mark on this car, carried onto the Listing so the notifier can see
    # it without reaching back into state.
    shortlisted: bool = False
    search_id: str = ""
    search_name: str = ""
    source: str = ""          # which parser strategy produced this
    enriched: bool = False    # True once detail-page JSON-LD has been merged

    def __post_init__(self) -> None:
        if self.year is None and self.title:
            m = _YEAR_RE.search(self.title)
            if m:
                self.year = int(m.group(1))

    @property
    def short_trim(self) -> str:
        """The first meaningful part of a trim.

        Dealers often fill the trim field with a separated feature list
        ("Sport | Premium | Heated seats"), which is unreadable in a
        notification.
        """
        trim = (self.trim or "").strip()
        if not trim:
            return ""
        # A trim whose leading name was stripped can start on a separator:
        # "I Premium PKG I Tech PKG".
        trim = _EDGE_SEPARATOR.sub("", trim).strip()
        if trim[:2].upper() == "I " and trim[2:3].isupper():
            trim = trim[2:].strip()
        trim = before_the_feature_list(trim)
        # Dealers often repeat the model inside the trim, which would name the
        # car "Make Model Model Sport" once assembled.
        for prefix in (self.model, self.make):
            if not prefix:
                continue
            if trim.lower() == prefix.lower():
                # The trim is only the model; keeping it would name it twice.
                return ""
            if trim.lower().startswith(prefix.lower() + " "):
                trim = trim[len(prefix):].strip()
        if len(trim) <= 40:
            return trim.strip()
        # Cut at a word boundary: letters left dangling by a mid-word cut
        # read as a rendering fault.
        cut = trim[:40]
        head, space, _ = cut.rpartition(" ")
        return (head if space and len(head) >= 12 else cut).strip()

    @property
    def display_title(self) -> str:
        """What to call this car. One definition, shared with ``name_of``.

        The page and the notifications use the same rule, so a car has the
        same name everywhere.
        """
        return name_of(self.to_dict())

    @property
    def composed_title(self) -> str:
        """'YEAR MAKE MODEL TRIM' out of the fields, ignoring any title."""
        parts = [str(self.year) if self.year else "", self.make, self.model,
                 self.short_trim]
        return " ".join(p for p in parts if p).strip()

    @property
    def price_text(self) -> str:
        if self.price is None:
            return "Price not listed"
        return f"${self.price:,}"

    @property
    def mileage_text(self) -> str:
        if self.mileage_km is None:
            return "km not listed"
        return f"{self.mileage_km:,} km"

    @property
    def thumbnail(self) -> str:
        return self.images[0] if self.images else ""

    def summary_line(self) -> str:
        bits = [self.display_title, self.price_text]
        if self.mileage_km is not None:
            bits.append(self.mileage_text)
        if self.location:
            bits.append(self.location)
        return " - ".join(bits)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Listing":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def merge(self, other: "Listing") -> None:
        """Fill blanks from ``other`` (a richer record) without losing data."""
        for name in self.__dataclass_fields__:
            if name in {"id", "search_id", "search_name", "source"}:
                continue
            new = getattr(other, name)
            if new in (None, "", [], False):
                continue
            current = getattr(self, name)
            if current in (None, "", [], False):
                setattr(self, name, new)
            elif name == "images" and len(new) > len(current):
                setattr(self, name, new)


def name_of(entry: dict[str, Any]) -> str:
    """What to call a car, from a state row rather than a Listing object.

    The parser falls back to "AutoTrader listing <id>" when a card has no
    name, and enrichment fills in year, make, model and trim without
    rewriting the title, so a placeholder title is replaced by a name built
    from those fields. Hidden cars are not enriched from their detail page,
    but their card still gives a year, make and model.
    """
    lone = Listing(id=str(entry.get("id") or ""), title="",
                   year=entry.get("year"), make=entry.get("make") or "",
                   model=entry.get("model") or "",
                   trim=entry.get("trim") or "")
    built = lone.composed_title

    title = str(entry.get("title") or "").strip()
    if title and not title.startswith(PLACEHOLDER_TITLE):
        # The dealer's own words carry detail no reconstruction has, but only
        # up to the feature list. The year is prefixed unless the title
        # already opens with it.
        head = before_the_feature_list(title) or title
        year = str(entry.get("year") or "")
        if year and not head.startswith(year):
            head = f"{year} {head}"
        return head
    if len(built.split()) >= 2:
        return built
    return title or f"{PLACEHOLDER_TITLE}{entry.get('id')}"
