"""Saving photos: durable files, content-addressed, verified before counted.

Separate from the bot's thumbnail cache on purpose. A photo saved here is
kept when the listing goes, when a search is removed and when thumbnails are
evicted. Only the owner deletes it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import struct
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from autotrader import clock

from . import settings as SETTINGS
from .db import Database, now

log = logging.getLogger("macwatch.media")

MAX_BYTES = 8_000_000
TIMEOUT = 30
MAX_ATTEMPTS = 5
# Where listing photos come from. Anything else is refused: a listing is
# untrusted content and must not make this Mac fetch arbitrary addresses.
# autotrader.ca serves its listing photos from autoscout24.net (same group).
ALLOWED_HOST_SUFFIXES = ("autotrader.ca", "autotradercdn.ca", "autotrader-cdn.ca",
                         "trader.ca", "autoscout24.net", "fbcdn.net", "facebook.com",
                         "fbsbx.com", "cloudfront.net")

_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
)


def sniff(data: bytes) -> tuple[str, str] | None:
    """(mime, suffix) from the bytes themselves, or None if not an image."""
    for magic, mime, suffix in _MAGIC:
        if data.startswith(magic):
            return mime, suffix
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif", ".avif"
    return None


def dimensions(data: bytes, mime: str) -> tuple[int | None, int | None]:
    try:
        if mime == "image/png" and len(data) >= 24:
            w, h = struct.unpack(">II", data[16:24])
            return int(w), int(h)
        if mime == "image/gif" and len(data) >= 10:
            w, h = struct.unpack("<HH", data[6:10])
            return int(w), int(h)
        if mime == "image/jpeg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                length = struct.unpack(">H", data[i + 2:i + 4])[0]
                if marker in (0xC0, 0xC1, 0xC2):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return int(w), int(h)
                i += 2 + length
        if mime == "image/webp" and len(data) >= 30:
            chunk = data[12:16]
            if chunk == b"VP8X":
                w = 1 + int.from_bytes(data[24:27], "little")
                h = 1 + int.from_bytes(data[27:30], "little")
                return w, h
            if chunk == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if chunk == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
    except (struct.error, IndexError, ValueError):
        pass
    return None, None


def allowed(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        return False
    return any(host == s or host.endswith("." + s) for s in ALLOWED_HOST_SUFFIXES)


def _fetch(url: str, referer: str = "") -> bytes:
    headers = {"Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
               "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"}
    if referer:
        headers["Referer"] = referer
    with requests.get(url, headers=headers, timeout=TIMEOUT, stream=True) as r:
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}")
        out = bytearray()
        for chunk in r.iter_content(65536):
            out += chunk
            if len(out) > MAX_BYTES:
                raise RuntimeError(f"larger than {MAX_BYTES} bytes")
        return bytes(out)


def used_bytes(root: Path | None = None) -> int:
    root = root or SETTINGS.media_dir()
    total = 0
    if root.exists():
        for path in root.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
    return total


def store(data: bytes, root: Path | None = None) -> tuple[str, str, Path, int | None, int | None]:
    """Write verified bytes under their hash. Returns (id, mime, path, w, h)."""
    kind = sniff(data)
    if kind is None:
        raise ValueError("not an image")
    mime, suffix = kind
    digest = hashlib.sha256(data).hexdigest()
    root = root or SETTINGS.media_dir()
    folder = root / digest[:2] / digest[2:4]
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{digest}{suffix}"
    if not target.exists():
        tmp = target.with_suffix(suffix + ".part")
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    w, h = dimensions(data, mime)
    return digest, mime, target, w, h


def capture(db: Database, settings, fetch: Callable[[str, str], bytes] | None = None,
            *, limit: int = 30, root: Path | None = None) -> dict[str, int]:
    """Download the photos queued by ingest. Returns counts."""
    fetch = fetch or _fetch
    root = root or SETTINGS.media_dir()
    counts = {"saved": 0, "failed": 0, "retry": 0, "refused": 0, "paused": 0}
    budget = int(getattr(settings, "media_budget_mb", 0) or 0) * 1_000_000
    if budget and used_bytes(root) >= budget:
        counts["paused"] = 1
        db.set("media_paused", {"at": now(), "why": "media budget reached"})
        return counts
    db.set("media_paused", None)
    requeue_refused(db)
    stamp = now()
    rows = db.q("""SELECT m.*, l.url AS listing_url FROM listing_media m
                   JOIN listings l ON l.id=m.listing_id
                   WHERE m.status='pending' AND (m.next_try_at IS NULL OR m.next_try_at<=?)
                   ORDER BY m.queued_at, m.position LIMIT ?""", (stamp, limit))
    for row in rows:
        url = row["source_url"]
        attempts = int(row["attempts"]) + 1
        if not allowed(url):
            with db.tx() as conn:
                conn.execute("UPDATE listing_media SET status='refused', error=?, attempts=? "
                             "WHERE listing_id=? AND source_url=?",
                             ("host not allowed", attempts, row["listing_id"], url))
            counts["refused"] += 1
            continue
        try:
            data = fetch(url, row["listing_url"] or "")
            asset_id, mime, path, w, h = store(data, root)
        except Exception as exc:  # noqa: BLE001 - one photo never stops the rest
            detail = str(exc)[:200]
            permanent = attempts >= MAX_ATTEMPTS or "HTTP 404" in detail \
                or "HTTP 410" in detail or "not an image" in detail
            with db.tx() as conn:
                conn.execute(
                    "UPDATE listing_media SET status=?, error=?, attempts=?, next_try_at=? "
                    "WHERE listing_id=? AND source_url=?",
                    ("failed" if permanent else "pending", detail, attempts,
                     None if permanent else clock.stamp(
                         clock.now() + timedelta(minutes=min(720, 15 * 2 ** attempts))),
                     row["listing_id"], url))
            counts["failed" if permanent else "retry"] += 1
            continue
        with db.tx() as conn:
            conn.execute(
                """INSERT INTO media_assets(id, mime, bytes, width, height, path, captured_at)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING""",
                (asset_id, mime, len(data), w, h, str(path.relative_to(root)), now()))
            conn.execute("UPDATE listing_media SET status='saved', asset_id=?, attempts=?, "
                         "error='' WHERE listing_id=? AND source_url=?",
                         (asset_id, attempts, row["listing_id"], url))
        counts["saved"] += 1
    return counts


def requeue_refused(db: Database) -> int:
    """A host added to the allowlist later lets its refused photos be tried."""
    rows = db.q("SELECT listing_id, source_url FROM listing_media WHERE status='refused'")
    again = [(r["listing_id"], r["source_url"]) for r in rows if allowed(r["source_url"])]
    if again:
        with db.tx() as conn:
            conn.executemany("UPDATE listing_media SET status='pending', error='', next_try_at=NULL "
                             "WHERE listing_id=? AND source_url=?", again)
    return len(again)


def asset_path(db: Database, asset_id: str, root: Path | None = None) -> tuple[Path, str] | None:
    row = db.one("SELECT path, mime FROM media_assets WHERE id=?", (asset_id,))
    if row is None:
        return None
    root = root or SETTINGS.media_dir()
    path = (root / row["path"]).resolve()
    if root.resolve() not in path.parents or not path.is_file():
        return None
    return path, row["mime"]


def photos_of(db: Database, listing_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in db.q(
        """SELECT m.position, m.source_url, m.status, m.asset_id, m.error,
                  a.mime, a.bytes, a.width, a.height
           FROM listing_media m LEFT JOIN media_assets a ON a.id=m.asset_id
           WHERE m.listing_id=? ORDER BY m.position""", (listing_id,))]
