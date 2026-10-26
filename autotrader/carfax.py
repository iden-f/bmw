"""The Carfax report a listing links to: its address, and our own copy.

A dealer who pays for a Carfax Canada history report often publishes it with
the listing, as a link that opens the report without an account. AutoTrader
carries that link in the page's own data (``carProofReportUrl``); a
Marketplace seller pastes it into the description. The link dies with the
listing, so the bot keeps the address on the car's record and fetches the
report itself into ``docs/reports/``, as a PDF when the report comes as one
or when a headless browser is at hand to print it, and as the page's HTML
otherwise. Copies are sealed into the vault and published beside the photos,
so the saved report opens from the dashboard after the car has gone.

Only what the seller made public is kept: a link to a report that must be
bought (carfax.ca/order) is a shop, not a report, and is not followed.

Nothing here may fail a check. A report is a nicety; a check is the job.
"""

from __future__ import annotations

import html as htmllib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from . import clock

log = logging.getLogger(__name__)

REPORT_DIR = Path("docs/reports")
INDEX = REPORT_DIR / "index.json"

# A report rendered to PDF is a few hundred kilobytes; a few megabytes when
# it carries photos. Anything bigger is not a report.
MAX_BYTES_EACH = 8_000_000
# The default room for every copy together; dashboard.report_budget_mb sets it.
MAX_TOTAL_BYTES = 300_000_000
# Reports fetched per check; dashboard.reports_per_run sets it. Printing one
# through a browser takes several seconds, and a check has a minute.
MAX_PER_RUN = 5
# A report that failed this many times running is left alone for a while.
GIVE_UP_AFTER = 2
TRY_AGAIN_AFTER_DAYS = 7
# How long a headless browser may take over one report.
PRINT_SECONDS = 90

_ID_OK = re.compile(r"^[A-Za-z0-9_-]{6,64}$")

# Carfax Canada's report viewer, and the US one. The order page is a shop.
_LINK = re.compile(
    r"(?:https?:)?//(?:[a-z0-9-]+\.)*carfax\.(?:ca|com)/[^\s\"'<>\\)\]}]*", re.I)
_REPORT_HOST = re.compile(r"^https?://(?:[a-z0-9-]+\.)*carfax\.(?:ca|com)/", re.I)
_NOT_A_REPORT = re.compile(r"carfax\.(?:ca|com)/(?:order|buy|purchase|cart|signin|login|"
                           r"fr/order)\b|\.(?:png|svg|gif|jpe?g|webp|css|js)(?:[?#]|$)"
                           r"|/(?:images|img|static|assets)/", re.I)
# AutoTrader's own field for the report, in the page's JSON. Slashes and
# ampersands arrive escaped, and the field is empty when there is no report.
_FIELD = re.compile(r'"carProofReportUrl"\s*:\s*"((?:[^"\\]|\\.)*)"', re.I)
_ANCHOR = re.compile(r'href=["\']([^"\']*carfax\.(?:ca|com)/[^"\']*)["\']', re.I)


def _unescape(text: str) -> str:
    text = text.strip()
    if "\\" in text:
        try:
            text = json.loads(f'"{text}"')
        except ValueError:
            text = text.replace("\\/", "/").replace("\\u0026", "&")
    return htmllib.unescape(text).strip()


def is_report_link(url: str) -> bool:
    """A link that opens a published report, not one that sells it."""
    url = str(url or "").strip()
    if not _REPORT_HOST.match(url) or _NOT_A_REPORT.search(url):
        return False
    # The viewer needs a report id or a VIN; the site's front page is neither.
    tail = url.split("/", 3)[3] if url.count("/") >= 3 else ""
    return bool(tail.strip("/")) and "XXXX" not in url.upper()


def links_in(text: str) -> list[str]:
    """Every report link in free text, a seller's description say, in order."""
    out: list[str] = []
    for match in _LINK.finditer(str(text or "")):
        url = _unescape(match.group(0)).rstrip(".,;:!?")
        if url.startswith("//"):
            url = "https:" + url
        if is_report_link(url) and url not in out:
            out.append(url)
    return out


def report_link(html: str) -> str:
    """The report an AutoTrader listing page links to, or "".

    The page's own field first, because it is the one the page's button
    opens; a plain anchor as a fallback for a page shaped some other way.
    """
    html = html or ""
    for match in _FIELD.finditer(html):
        url = _unescape(match.group(1))
        if is_report_link(url):
            return url
    for match in _ANCHOR.finditer(html):
        url = _unescape(match.group(1))
        if is_report_link(url):
            return url
    return ""


# ----------------------------------------------------------------- the store

@dataclass
class Report:
    """What happened to the reports this check, in enough detail to read."""
    fetched: int = 0
    skipped: int = 0
    failed: int = 0
    pruned: int = 0
    total_bytes: int = 0
    kept: int = 0
    notes: list[str] = field(default_factory=list)
    samples: list[dict[str, Any]] = field(default_factory=list)

    def line(self) -> str:
        return (f"reports: {self.kept} kept ({self.total_bytes / 1e6:.1f} MB), "
                f"{self.fetched} new, {self.failed} failed, {self.pruned} pruned")

    def to_dict(self) -> dict[str, Any]:
        return {"kept": self.kept, "fetched": self.fetched, "failed": self.failed,
                "pruned": self.pruned, "bytes": self.total_bytes,
                "samples": self.samples[:6]}


def _load_index() -> dict[str, Any]:
    try:
        return json.loads(INDEX.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_index(index: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, INDEX)


def _existing_bytes(index: dict[str, Any]) -> int:
    total = 0
    for row in index.values():
        path = REPORT_DIR / str(row.get("file") or "")
        if row.get("file") and path.is_file():
            total += path.stat().st_size
    return total


def settings(cfg: Any) -> dict[str, int]:
    """The report store's size and pace, as the config sets them: for ``sync``."""
    try:
        budget_mb = float(cfg.get("dashboard.report_budget_mb",
                                  MAX_TOTAL_BYTES / 1e6) or MAX_TOTAL_BYTES / 1e6)
    except (TypeError, ValueError):
        budget_mb = MAX_TOTAL_BYTES / 1e6
    try:
        per_run = int(cfg.get("dashboard.reports_per_run", MAX_PER_RUN) or MAX_PER_RUN)
    except (TypeError, ValueError):
        per_run = MAX_PER_RUN
    return {"budget": int(max(1.0, budget_mb) * 1_000_000),
            "limit": max(0, min(per_run, 50))}


def _given_up(note: dict[str, Any] | None, now: datetime) -> bool:
    if not note or int(note.get("count") or 0) < GIVE_UP_AFTER:
        return False
    when = clock.parse(note.get("at"))
    return when is not None and now - when < timedelta(days=TRY_AGAIN_AFTER_DAYS)


def _fetch(url: str, fetcher: Any) -> dict[str, Any]:
    """The report as the site serves it: a PDF, or the viewer's HTML."""
    if hasattr(fetcher, "get_asset"):
        try:
            got = dict(fetcher.get_asset(url))
        except Exception as exc:  # noqa: BLE001 - a report may never fail a check
            return {"error": f"{type(exc).__name__}: {exc}"[:120]}
    else:
        try:
            response = fetcher.get(url)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:120]}
        headers = getattr(response, "headers", {}) or {}
        got = {"status": getattr(response, "status_code", None)
                         or getattr(response, "status", None),
               "type": str(headers.get("Content-Type")
                           or headers.get("content-type") or "").split(";")[0].strip(),
               "content": getattr(response, "content", b"") or b""}
        if not got["content"] and getattr(response, "text", ""):
            got["content"] = response.text.encode("utf-8", "replace")
        if got["status"] and int(got["status"]) >= 400:
            got["error"] = f"HTTP {got['status']}"
    content = got.get("content") or b""
    kind = str(got.get("type") or "").lower()
    if content[:5] == b"%PDF-":
        got["kind"] = "pdf"
    elif kind.startswith("text/html") or content.lstrip()[:1] == b"<":
        got["kind"] = "html"
    else:
        got["kind"] = ""
    return got


def chrome_binary() -> str | None:
    """A headless browser that can print a page, if the machine has one.

    GitHub's Ubuntu runners ship Google Chrome. ``CHROME_BIN`` names another.
    """
    named = os.environ.get("CHROME_BIN")
    if named and (os.path.isfile(named) or shutil.which(named)):
        return named
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def print_to_pdf(url: str, binary: str | None = None,
                 timeout: float = PRINT_SECONDS) -> bytes | None:
    """The page at ``url`` as the browser would print it, or None."""
    binary = binary or chrome_binary()
    if not binary:
        return None
    with tempfile.TemporaryDirectory(prefix="carfax-") as folder:
        out = Path(folder) / "report.pdf"
        command = [binary, "--headless=new", "--disable-gpu", "--no-sandbox",
                   "--hide-scrollbars", "--no-pdf-header-footer",
                   "--run-all-compositor-stages-before-draw",
                   "--virtual-time-budget=20000",
                   f"--user-data-dir={Path(folder) / 'profile'}",
                   f"--print-to-pdf={out}", url]
        try:
            subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            log.info("printing %s failed: %s", url[:80], exc)
            return None
        try:
            blob = out.read_bytes()
        except OSError:
            return None
    return blob if blob[:5] == b"%PDF-" else None


def sync(entries: Iterable[dict[str, Any]], fetcher: Any, *,
         limit: int = MAX_PER_RUN, budget: int = MAX_TOTAL_BYTES,
         printer: Callable[[str], bytes | None] | None = print_to_pdf,
         dry_run: bool = False) -> Report:
    """Fetch the reports we lack, drop those of forgotten cars.

    ``fetcher`` is the bot's own paced, budgeted client, so reports queue
    behind the same politeness as everything else. ``printer`` turns a
    report served as a page into a PDF; None keeps the page's HTML instead.

    A car that has gone keeps its copy: the report is the record. One never
    fetched while the car was live is still tried, since the viewer link
    sometimes outlives the listing.
    """
    report = Report()
    index = _load_index()
    entries = list(entries)
    wanted = {str(e["id"]): e for e in entries
              if e.get("carfax_url") and not e.get("filtered")
              and e.get("status") in ("active", "gone")
              and _ID_OK.match(str(e.get("id") or ""))}

    # Prune first: the room a forgotten car frees goes to a live one.
    for listing_id in list(index):
        if listing_id in wanted:
            continue
        row = index.pop(listing_id)
        if row.get("file") and not dry_run:
            try:
                (REPORT_DIR / row["file"]).unlink(missing_ok=True)
                report.pruned += 1
            except OSError:
                pass

    total = _existing_bytes(index)
    now = clock.now()
    # Newest cars first: when the budget runs short the new car an alert
    # just pointed at keeps its report.
    order = sorted(wanted, key=lambda lid: str(wanted[lid].get("first_seen") or ""),
                   reverse=True)
    order.sort(key=lambda lid: wanted[lid].get("status") != "active")
    for listing_id in order:
        entry = wanted[listing_id]
        url = str(entry["carfax_url"])
        row = index.get(listing_id) or {}
        if row.get("file") and (REPORT_DIR / row["file"]).is_file() \
                and row.get("url") == url:
            continue
        if _given_up(row.get("failed"), now):
            continue
        if report.fetched >= limit:
            report.skipped += 1
            continue
        if total + MAX_BYTES_EACH > budget:
            report.skipped += 1
            if "the report store is full" not in " ".join(report.notes):
                report.notes.append(
                    f"the report store is full ({total / 1e6:.0f} MB of "
                    f"{budget / 1e6:.0f} MB); raise dashboard.report_budget_mb "
                    f"or the newest reports go unsaved")
            continue
        if dry_run:
            report.skipped += 1
            continue

        got = _fetch(url, fetcher)
        sample: dict[str, Any] = {"url": url[:120], "status": got.get("status"),
                                  "type": got.get("type"),
                                  "bytes": len(got.get("content") or b"")}
        blob, kind = b"", ""
        if got.get("error"):
            sample["error"] = str(got["error"])[:120]
        elif got.get("kind") == "pdf":
            blob, kind = got["content"], "pdf"
        elif got.get("kind") == "html":
            printed = None
            if printer is not None:
                try:
                    printed = printer(url)
                except Exception as exc:  # noqa: BLE001
                    log.info("printing %s failed: %s", url[:80], exc)
            if printed:
                blob, kind = printed, "pdf"
                sample["printed"] = True
            else:
                blob, kind = got["content"], "html"
        else:
            sample["error"] = f"not a report ({got.get('type') or 'no content-type'})"
        if blob and len(blob) > MAX_BYTES_EACH:
            sample["error"] = f"{len(blob)} bytes is over the {MAX_BYTES_EACH} cap"
            blob = b""
        report.samples.append(sample)
        if not blob:
            report.failed += 1
            before = row.get("failed") or {}
            row["failed"] = {"at": clock.stamp(now),
                             "count": int(before.get("count") or 0) + 1}
            index[listing_id] = row
            continue
        if row.get("file"):
            # A new link for the same car replaces the old copy.
            try:
                (REPORT_DIR / row["file"]).unlink(missing_ok=True)
            except OSError:
                pass
        name = f"{listing_id}.{kind}"
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (REPORT_DIR / name).write_bytes(blob)
        index[listing_id] = {"file": name, "type": kind, "url": url,
                             "bytes": len(blob), "at": clock.stamp(now)}
        total += len(blob)
        report.fetched += 1

    if not dry_run:
        _save_index(index)
    report.kept = sum(1 for row in index.values()
                      if row.get("file") and (REPORT_DIR / row["file"]).is_file())
    report.total_bytes = _existing_bytes(index)
    return report


def local_for(listing_id: str, index: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The published copy of a car's report, if we hold one."""
    index = _load_index() if index is None else index
    row = index.get(str(listing_id)) or {}
    if not row.get("file"):
        return None
    return {"file": f"reports/{row['file']}", "type": str(row.get("type") or "pdf"),
            "url": str(row.get("url") or ""), "at": row.get("at")}
