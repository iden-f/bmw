"""Optional archiving of listings, with a retention policy.

The default is ``metadata``: a small JSON file per car with its facts and
photo URLs. Whole listing pages are large and mostly page furniture, so
``full`` (the HTML) and image downloads are opt-in. Everything is pruned on a
schedule so the archive cannot grow without limit.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import clock
from .listing import Listing

log = logging.getLogger(__name__)

ARCHIVE_DIR = Path("archives")
VALID_MODES = ("off", "metadata", "full")


def archive_listing(listing: Listing, config: dict[str, Any], fetcher=None,
                    html: str | None = None, root: Path = ARCHIVE_DIR) -> Path | None:
    """Write an archive entry for ``listing``.  Never raises."""
    mode = str(config.get("mode", "metadata")).lower()
    if mode not in VALID_MODES or mode == "off":
        return None
    try:
        folder = root / listing.id
        folder.mkdir(parents=True, exist_ok=True)

        payload = listing.to_dict()
        payload["archived_at"] = clock.now().isoformat(timespec="seconds")
        (folder / "metadata.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

        if mode == "full" and html:
            (folder / "page.html").write_text(html, encoding="utf-8")

        wanted = int(config.get("images", 0) or 0)
        if wanted > 0 and fetcher is not None and listing.images:
            for index, url in enumerate(listing.images[:wanted], start=1):
                # One unreachable photo must not throw away the metadata we
                # have already written for this car.
                try:
                    blob = fetcher.get_bytes(url, referer=listing.url)
                except Exception as exc:  # noqa: BLE001
                    log.debug("photo %s failed for %s: %s", index, listing.id, exc)
                    continue
                if not blob:
                    continue
                suffix = ".jpg"
                for candidate in (".jpg", ".jpeg", ".png", ".webp", ".avif"):
                    if candidate in url.lower():
                        suffix = candidate
                        break
                (folder / f"photo_{index}{suffix}").write_bytes(blob)
        return folder
    except Exception as exc:  # noqa: BLE001 - archiving is a nicety, never fatal
        log.warning("could not archive %s: %s", listing.id, exc)
        return None


def prune(config: dict[str, Any], root: Path = ARCHIVE_DIR,
          *, dry_run: bool = False) -> list[str]:
    """Delete archive folders past the retention policy.  Returns their ids."""
    keep_last = int(config.get("keep_last", 400) or 0)
    keep_days = int(config.get("keep_days", 730) or 0)
    if not root.exists() or (keep_last <= 0 and keep_days <= 0):
        return []

    folders = [p for p in root.iterdir() if p.is_dir()]

    def when(folder: Path) -> str:
        meta = folder / "metadata.json"
        if meta.exists():
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                stamp = data.get("archived_at") or ""
                if stamp:
                    return str(stamp)
            except (json.JSONDecodeError, OSError):
                pass
        return datetime.fromtimestamp(folder.stat().st_mtime,
                                      timezone.utc).isoformat(timespec="seconds")

    dated = sorted(((when(f), f) for f in folders), reverse=True)
    doomed: list[Path] = []

    if keep_last > 0 and len(dated) > keep_last:
        doomed.extend(f for _, f in dated[keep_last:])
    if keep_days > 0:
        # Compared on the date alone; retention is counted in days.
        cutoff = (clock.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
        doomed.extend(f for stamp, f in dated if stamp[:10] < cutoff and f not in doomed)

    removed: list[str] = []
    for folder in doomed:
        removed.append(folder.name)
        if not dry_run:
            shutil.rmtree(folder, ignore_errors=True)
    return removed


def size_report(root: Path = ARCHIVE_DIR) -> dict[str, Any]:
    """What the archive currently costs, for the dashboard's Status tab."""
    if not root.exists():
        return {"folders": 0, "bytes": 0, "html_bytes": 0, "image_bytes": 0}
    total = html = images = 0
    folders = 0
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        folders += 1
        for item in folder.rglob("*"):
            if not item.is_file():
                continue
            size = item.stat().st_size
            total += size
            if item.suffix == ".html":
                html += size
            elif item.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".avif"}:
                images += size
    return {"folders": folders, "bytes": total, "html_bytes": html, "image_bytes": images}
