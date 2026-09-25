"""Capturing what the scraper saw, when it saw nothing.

A parse that returns zero listings is almost impossible to debug from a log
line. This writes a small, targeted description of the page - its shape,
markers, structured data, and the context around anything that looks like a
listing link - so a broken parser can be fixed from evidence.

It is not a full page dump, which would bloat the repository it is committed
to.
"""

from __future__ import annotations

import gzip
import html as htmllib
import json
import re
from pathlib import Path
from typing import Any

from . import clock
from .http import BLOCK_MARKERS, looks_blocked
from .parser import parse_search_page

DIAGNOSTIC_DIR = Path("diagnostics")

HEAD_CHARS = 2500
MAX_JSONLD = 40000
# Big enough to hold several complete listing objects, enough to rebuild the
# payload's shape from.
MAX_NEXT_DATA = 30000
MAX_CONTEXTS = 25
# A gzipped results page is ~70 KB; cap it so a pathological response
# cannot commit megabytes into the repository.
MAX_RAW_BYTES = 400_000
CONTEXT_WINDOW = 180

# Things whose presence or absence says something about how the page is built.
MARKERS = {
    "listing path /a/": r"/a/",
    "listing id pattern": r"\d+_\d{5,}_",
    "offer path /offers/": r"/offers?/",
    "offer uuid": r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    "offer anchor tag": r"(?i)<a\b[^>]*href=[\"'][^\"']*/offers?/",
    "json-ld blocks": r"application/ld\+json",
    "__NEXT_DATA__": r"__NEXT_DATA__",
    "window.__INITIAL_STATE__": r"__INITIAL_STATE__",
    "data-listing-id": r"data-listing-id",
    "srp / results container": r"(?i)search-?results|srp-|result-item|listing-item",
    "vehicle image CDN": r"vehicleimages",
    "autoscout image CDN": r"pictures\.autoscout24",
    "price markup": r"(?i)price-amount|\bprice\b",
    "noscript block": r"<noscript",
    "meta refresh": r"(?i)http-equiv=[\"']?refresh",
    "captcha/challenge word": r"(?i)captcha|challenge|verify you",
}


def capture(url: str, response_text: str, status: int, elapsed_ms: int,
            search_name: str = "") -> dict[str, Any]:
    """Summarise a page in enough detail to fix a parser from."""
    text = response_text or ""
    parsed = parse_search_page(text, url)

    title = ""
    match = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    if match:
        title = htmllib.unescape(re.sub(r"\s+", " ", match.group(1))).strip()[:200]

    markers = {name: len(re.findall(pattern, text))
               for name, pattern in MARKERS.items()}

    jsonld: list[str] = []
    budget = MAX_JSONLD
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>',
                            text, re.S | re.I):
        block = block.strip()
        if not block or budget <= 0:
            continue
        try:
            # Re-serialise compactly: pretty-printed blocks waste the budget.
            block = json.dumps(json.loads(block), separators=(",", ":"))
        except (json.JSONDecodeError, TypeError):
            block = "UNPARSEABLE: " + block[:400]
        jsonld.append(block[:budget])
        budget -= len(block)

    # The context around anything resembling a listing link is what a broken
    # anchor strategy most needs.
    contexts: list[str] = []
    for hit in re.finditer(r"/(?:a|offers?)/[^\"'\s<>]{0,140}", text):
        start = max(0, hit.start() - CONTEXT_WINDOW)
        end = min(len(text), hit.end() + CONTEXT_WINDOW)
        contexts.append(re.sub(r"\s+", " ", text[start:end]))
        if len(contexts) >= MAX_CONTEXTS:
            break

    # The front-end state blob is where anything missing from the structured
    # data (the model year, for one) is most likely to be found.
    next_data: dict[str, Any] = {}
    match = re.search(
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
    if match:
        try:
            parsed_next = json.loads(match.group(1))
            next_data = {
                "top_level_keys": sorted(parsed_next.keys())[:30],
                "key_frequency": _key_frequency(parsed_next),
                "sample": json.dumps(parsed_next, separators=(",", ":"))[:MAX_NEXT_DATA],
            }
        except (json.JSONDecodeError, TypeError, AttributeError) as exc:
            next_data = {"error": f"could not parse: {exc}"}

    blocked_markers = [m for m in BLOCK_MARKERS if m in text[:20000].lower()]

    return {
        "captured_at": clock.now().isoformat(timespec="seconds"),
        "search": search_name,
        "url": url,
        "http_status": status,
        "elapsed_ms": elapsed_ms,
        "bytes": len(text),
        "title": title,
        "looks_blocked": looks_blocked(text, status),
        "block_markers_found": blocked_markers,
        "strategy_scores": parsed.candidates,
        "strategy_used": parsed.strategy,
        "listings_found": len(parsed.listings),
        "markers": markers,
        "head": text[:HEAD_CHARS],
        "json_ld": jsonld,
        "listing_link_contexts": contexts,
        "next_data": next_data,
    }


def _key_frequency(node: Any, counts: dict[str, int] | None = None,
                   depth: int = 0) -> dict[str, int]:
    """Which field names appear in a blob, and how often."""
    counts = counts if counts is not None else {}
    if depth > 12:
        return counts
    if isinstance(node, dict):
        for key, value in node.items():
            counts[str(key)] = counts.get(str(key), 0) + 1
            _key_frequency(value, counts, depth + 1)
    elif isinstance(node, list):
        for value in node[:50]:
            _key_frequency(value, counts, depth + 1)
    return dict(sorted(counts.items(), key=lambda kv: -kv[1])[:60])


def to_markdown(data: dict[str, Any]) -> str:
    lines = [f"# What the scraper saw: {data.get('search') or 'search'}", ""]
    lines.append(f"- Captured: `{data['captured_at']}`")
    lines.append(f"- URL: <{data['url']}>")
    lines.append(f"- HTTP {data['http_status']}, {data['bytes']:,} bytes, "
                 f"{data['elapsed_ms']} ms")
    lines.append(f"- Title: `{data['title'] or '(none)'}`")
    lines.append(f"- Looks like an anti-bot page: **{data['looks_blocked']}**")
    if data["block_markers_found"]:
        lines.append(f"- Block markers matched: {data['block_markers_found']}")
    lines.append(f"- Listings parsed: **{data['listings_found']}** "
                 f"(strategy: `{data['strategy_used']}`)")
    lines.append("")

    lines.append("## Strategy scores")
    lines.append("")
    lines.append("| strategy | listings |")
    lines.append("|---|---|")
    for name, count in data["strategy_scores"].items():
        lines.append(f"| `{name}` | {count} |")
    lines.append("")

    lines.append("## Markers present in the HTML")
    lines.append("")
    lines.append("| marker | occurrences |")
    lines.append("|---|---|")
    for name, count in data["markers"].items():
        lines.append(f"| {name} | {count} |")
    lines.append("")

    if data["json_ld"]:
        lines.append("## Structured data found")
        lines.append("")
        for block in data["json_ld"]:
            lines.append("```json")
            lines.append(block)
            lines.append("```")
            lines.append("")
    else:
        lines.append("## Structured data found")
        lines.append("")
        lines.append("_None. The page carries no schema.org JSON-LD._")
        lines.append("")

    if data["listing_link_contexts"]:
        lines.append("## Context around listing-shaped links")
        lines.append("")
        for context in data["listing_link_contexts"]:
            lines.append("```html")
            lines.append(context)
            lines.append("```")
            lines.append("")
    else:
        lines.append("## Context around listing-shaped links")
        lines.append("")
        lines.append("_No `/a/` links anywhere in the payload._")
        lines.append("")

    if data.get("next_data"):
        lines.append("## Front-end state (`__NEXT_DATA__`)")
        lines.append("")
        nd = data["next_data"]
        if nd.get("error"):
            lines.append(f"_{nd['error']}_")
        else:
            lines.append(f"Top-level keys: `{nd.get('top_level_keys')}`")
            lines.append("")
            lines.append("Most common field names:")
            lines.append("")
            lines.append("```")
            for name, count in list((nd.get("key_frequency") or {}).items())[:40]:
                lines.append(f"{count:>5}  {name}")
            lines.append("```")
            lines.append("")
            lines.append("```json")
            lines.append(nd.get("sample", ""))
            lines.append("```")
        lines.append("")

    lines.append("## First few KB of the page")
    lines.append("")
    lines.append("```html")
    lines.append(data["head"])
    lines.append("```")
    return "\n".join(lines)


def write_raw(text: str, slug: str, root: Path = DIAGNOSTIC_DIR,
              limit: int = MAX_RAW_BYTES) -> Path | None:
    """Save the page itself, gzipped, next to the summary.

    The summary describes the page but is not enough to rewrite a parser
    against; a gzipped results page is small enough to keep for that.
    """
    blob = (text or "").encode("utf-8", "replace")
    if not blob:
        return None
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{slug}.page.html.gz"
    packed = gzip.compress(blob, 9)
    if len(packed) > limit:
        # Shorten the page and compress that, keeping the head, where the
        # structure lives. Never cut the compressed bytes: a truncated gzip
        # stream cannot be decompressed at all.
        keep = len(blob)
        while keep > 1024:
            keep //= 2
            packed = gzip.compress(blob[:keep], 9)
            if len(packed) <= limit:
                break
        else:
            return None
        # Grow back to as much of the page as still fits, rather than
        # settling for wherever the halving landed.
        step = keep // 2
        while step > 1024:
            bigger = gzip.compress(blob[:keep + step], 9)
            if len(bigger) <= limit:
                keep += step
                packed = bigger
            step //= 2
    path.write_bytes(packed)
    return path


def write(data: dict[str, Any], slug: str, root: Path = DIAGNOSTIC_DIR) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{slug}.md"
    path.write_text(to_markdown(data), encoding="utf-8")
    (root / f"{slug}.json").write_text(
        json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return path
