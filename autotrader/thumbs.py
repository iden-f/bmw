"""Local copies of listing photos.

Photos hotlinked from the seller's CDN do not show offline or in the
installed app, and vanish when the car is delisted. So the bot fetches small
copies itself on runs with network access and publishes them beside the data.
The CDN serves resized variants, so there is nothing to re-encode and no image
library to install: the work is requesting a small variant, checking that the
response is really an image, and staying inside a budget.

Nothing here may fail a run. A photo is a nicety; a check is the job.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

THUMB_DIR = Path("docs/thumbs")
INDEX = THUMB_DIR / "index.json"

# Photos are committed to the published branch, so every byte stays in its
# history for anyone who clones it.
MAX_BYTES_EACH = 60_000
MAX_TOTAL_BYTES = 12_000_000
# How many new photos one check may fetch, so a first run does not pull every
# car's photos at once on top of its own requests.
MAX_PER_RUN = 24
# Photos kept per car, for the card and the detail gallery: on most listings
# a front, a side and an interior.
KEEP_PER_CAR = 3
# What an image is allowed to claim to be.
OK_TYPES = ("image/webp", "image/jpeg", "image/png", "image/avif")
EXT = {"image/webp": ".webp", "image/jpeg": ".jpg",
       "image/png": ".png", "image/avif": ".avif"}

_ID_OK = re.compile(r"^[A-Za-z0-9_-]{6,64}$")

# The CDN sizes a photo in its own path:
#   .../listing-images/<id>_<id>.jpg/1920x1080.jpg
# Listing pages link the full-size original, which is over MAX_BYTES_EACH,
# so a smaller variant is requested by rewriting that suffix.
_SIZED = re.compile(r"/(\d{2,5})x(\d{2,5})\.(jpe?g|png|webp)$", re.I)
# Big enough for a phone screen at 2x, small enough for repository history.
# Tried in order, original last.
THUMB_SIZES = ("480x360", "250x188")


def variants(url: str) -> list[str]:
    """The URLs to try for one photo, smallest useful first, original last."""
    match = _SIZED.search(url or "")
    if not match:
        return [url] if url else []
    have = f"{match.group(1)}x{match.group(2)}".lower()
    out = [_SIZED.sub(f"/{size}.{match.group(3)}", url)
           for size in THUMB_SIZES if size != have]
    out.append(url)
    return out


@dataclass
class Report:
    """What actually happened, in enough detail to be worth reading."""
    fetched: int = 0
    skipped: int = 0
    failed: int = 0
    pruned: int = 0
    bytes_added: int = 0
    total_bytes: int = 0
    kept: int = 0
    notes: list[str] = field(default_factory=list)
    samples: list[dict[str, Any]] = field(default_factory=list)

    def line(self) -> str:
        return (f"photos: {self.kept} kept ({self.total_bytes / 1e6:.1f} MB), "
                f"{self.fetched} new, {self.failed} failed, {self.pruned} pruned")


def _load_index() -> dict[str, Any]:
    try:
        return json.loads(INDEX.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_index(index: dict[str, Any]) -> None:
    try:
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        INDEX.write_text(json.dumps(index, indent=1, sort_keys=True),
                         encoding="utf-8")
    except OSError as exc:
        log.warning("could not write the photo index: %s", exc)


def _dimensions(blob: bytes) -> tuple[int, int] | None:
    """Width and height, read from the file's own header.

    Parses just enough of WebP, PNG and JPEG to avoid an image library.
    """
    try:
        if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
            chunk = blob[12:16]
            if chunk == b"VP8X":
                w = int.from_bytes(blob[24:27], "little") + 1
                h = int.from_bytes(blob[27:30], "little") + 1
                return w, h
            if chunk == b"VP8 ":
                return (int.from_bytes(blob[26:28], "little") & 0x3FFF,
                        int.from_bytes(blob[28:30], "little") & 0x3FFF)
            if chunk == b"VP8L":
                bits = int.from_bytes(blob[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if blob[:8] == b"\x89PNG\r\n\x1a\n":
            return (int.from_bytes(blob[16:20], "big"),
                    int.from_bytes(blob[20:24], "big"))
        if blob[:2] == b"\xff\xd8":
            i = 2
            while i < len(blob) - 9:
                if blob[i] != 0xFF:
                    i += 1
                    continue
                marker = blob[i + 1]
                # SOI, EOI, TEM and the restart markers carry no length. Adding
                # the two bytes after them as if they did reads the image data
                # as a segment size and jumps off the end of the file.
                if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                if marker == 0xFF:          # fill byte
                    i += 1
                    continue
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    return (int.from_bytes(blob[i + 7:i + 9], "big"),
                            int.from_bytes(blob[i + 5:i + 7], "big"))
                length = int.from_bytes(blob[i + 2:i + 4], "big")
                if length < 2:
                    return None
                i += 2 + length
    except (IndexError, ValueError):
        return None
    return None


def _safe_name(listing_id: str, content_type: str, n: int = 0) -> str | None:
    """A filename that cannot escape the directory it belongs in.

    The first photo of a car is named by its bare id; later ones get a suffix.
    """
    if not _ID_OK.match(str(listing_id).replace("-", "")[:64]):
        return None
    suffix = "" if n == 0 else f"-{int(n)}"
    return f"{listing_id}{suffix}{EXT.get(content_type, '.img')}"


def _files_of(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Every kept photo for a car, including rows that hold a single file."""
    if not isinstance(row, dict):
        return []
    if row.get("files"):
        return [f for f in row["files"] if isinstance(f, dict) and f.get("file")]
    return [row] if row.get("file") else []


def _existing_bytes() -> int:
    if not THUMB_DIR.exists():
        return 0
    return sum(p.stat().st_size for p in THUMB_DIR.glob("*")
               if p.is_file() and p.name != INDEX.name)


def sync(entries: Iterable[dict[str, Any]], fetcher: Any,
         *, limit: int = MAX_PER_RUN, budget: int = MAX_TOTAL_BYTES,
         per_car: int = KEEP_PER_CAR, dry_run: bool = False) -> Report:
    """Fetch what is missing, drop what is no longer watched.

    ``fetcher`` is the bot's own rate-limited, retrying, budgeted HTTP client,
    so photos queue behind the same politeness the searches use rather than
    opening a second uncontrolled connection to somebody's CDN.
    """
    report = Report()
    entries = list(entries)
    index = _load_index()

    # Only visible cars: a hidden car's photos are not published in the data
    # file either, so fetching them would be wasted.
    wanted = {str(e["id"]): e for e in entries
              if e.get("status") == "active" and not e.get("filtered")
              and (e.get("images") or [])}

    # Prune first, so space freed by delisted cars goes to live ones.
    for listing_id in list(index):
        if listing_id in wanted:
            continue
        for row in _files_of(index[listing_id]):
            try:
                (THUMB_DIR / row["file"]).unlink(missing_ok=True)
                report.pruned += 1
            except OSError:
                pass
        del index[listing_id]

    total = _existing_bytes()
    keep = max(1, int(per_car))
    for listing_id, entry in wanted.items():
        have = [f for f in _files_of(index.get(listing_id, {}))
                if (THUMB_DIR / f["file"]).exists()]
        urls = [u for u in (entry.get("images") or []) if u][:keep]
        if len(have) >= min(keep, len(urls)):
            continue

        for n, url in enumerate(urls):
            if any(f.get("n", 0) == n for f in have):
                continue
            if report.fetched >= limit:
                report.skipped += 1
                break
            if total + MAX_BYTES_EACH > budget:
                report.notes.append(
                    f"photo budget of {budget / 1e6:.0f} MB is full - "
                    f"{report.skipped + 1} more would not fit")
                report.skipped += 1
                break

            got = _fetch_best(url, fetcher, report)
            if got is None or got[0] is None:
                report.failed += 1
                # Report the failure and move on rather than spend more
                # requests on the same car.
                break
            blob, content_type, note = got
            name = _safe_name(listing_id, content_type, n)
            if not name:
                report.failed += 1
                report.notes.append(f"{listing_id} is not a safe filename")
                break
            if not dry_run:
                try:
                    THUMB_DIR.mkdir(parents=True, exist_ok=True)
                    (THUMB_DIR / name).write_bytes(blob)
                except OSError as exc:
                    report.failed += 1
                    report.notes.append(f"could not write {name}: {exc}")
                    break
            have.append({"file": name, "bytes": len(blob), "n": n,
                         "w": note.get("w"), "h": note.get("h")})
            report.fetched += 1
            report.bytes_added += len(blob)
            total += len(blob)

        if have:
            have.sort(key=lambda f: f.get("n", 0))
            index[listing_id] = {"files": have, "file": have[0]["file"],
                                 "bytes": sum(f.get("bytes", 0) for f in have),
                                 "w": have[0].get("w"), "h": have[0].get("h")}

    report.kept = len(index)
    report.total_bytes = total
    if not dry_run:
        _save_index(index)
    return report


def _fetch_best(url: str | None, fetcher: Any, report: "Report"
                ) -> tuple[bytes | None, str, dict[str, Any]] | None:
    """The smallest variant of this photo that actually comes back.

    Usually one request, since the CDN serves the size the path asks for. The
    original is tried last, so an unfamiliar URL shape costs an extra request
    rather than a missing photo.
    """
    last: tuple[bytes | None, str, dict[str, Any]] | None = None
    for candidate in variants(url or ""):
        got = _fetch_one(candidate, fetcher)
        if got is None:
            last = None
            continue
        last = got
        if got[0] is not None:
            report.samples.append(got[2])
            return got
    if last is not None:
        report.samples.append(last[2])
    return last


def _fetch_one(url: str | None, fetcher: Any
               ) -> tuple[bytes | None, str, dict[str, Any]] | None:
    """One photo, with everything worth reporting about it.

    Uses the fetcher's binary path: the ordinary get() decodes to text, which
    corrupts the image and loses its content type.
    """
    if not url:
        return None
    note: dict[str, Any] = {"url": url[:120]}

    if hasattr(fetcher, "get_asset"):
        # get_asset returns the HTTP errors it expects, but a reset
        # connection, a DNS failure or a TLS timeout still raises, and a
        # photo may never fail a check.
        try:
            got = fetcher.get_asset(url)
        except Exception as exc:  # noqa: BLE001 - a photo may never fail a check
            note.update(status=None, error=f"{type(exc).__name__}: {exc}"[:120])
            return None, "", note
    else:
        # A fetcher without get_asset, such as a test stand-in.
        try:
            response = fetcher.get(url)
        except Exception as exc:  # noqa: BLE001 - a photo may never fail a check
            note.update(status=None, error=str(exc)[:120])
            return None, "", note
        headers = getattr(response, "headers", {}) or {}
        got = {
            "status": getattr(response, "status_code", None)
                      or getattr(response, "status", None),
            "type": str(headers.get("Content-Type")
                        or headers.get("content-type") or "").split(";")[0].strip(),
            "content": getattr(response, "content", b"") or b"",
        }
        if got["status"] and int(got["status"]) >= 400:
            got["error"] = f"HTTP {got['status']}"

    content_type = str(got.get("type") or "")
    blob = got.get("content") or b""
    note.update(status=got.get("status"), type=content_type, bytes=len(blob))

    if got.get("error"):
        note["error"] = got["error"]
        return None, content_type, note
    if content_type not in OK_TYPES:
        note["error"] = f"not an image ({content_type or 'no content-type'})"
        return None, content_type, note
    if len(blob) > MAX_BYTES_EACH:
        note["error"] = f"{len(blob)} bytes is over the {MAX_BYTES_EACH} cap"
        return None, content_type, note

    size = _dimensions(blob)
    if size:
        note["w"], note["h"] = size
    else:
        note["error"] = "header does not parse as an image"
        return None, content_type, note
    return blob, content_type, note


def local_for(listing_id: str, index: dict[str, Any] | None = None) -> str | None:
    """The published path for a car's first photo, if we kept one."""
    kept = locals_for(listing_id, index)
    return kept[0] if kept else None


def locals_for(listing_id: str, index: dict[str, Any] | None = None) -> list[str]:
    """Every photo of a car we hold a copy of, in the seller's order."""
    index = _load_index() if index is None else index
    return [f"thumbs/{f['file']}" for f in _files_of(index.get(str(listing_id), {}))]
