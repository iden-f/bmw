"""One pass: read the watch's searches on Marketplace and send what was seen.

The searches come from the bot's own settings, fetched sealed from the vault
and opened here with the passphrase, so a search changed on the dashboard is
searched here on the next pass with nothing to edit on this computer.
"""

from __future__ import annotations

import json
import logging
import random
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from autotrader import clock, filters
from autotrader import marketplace as M
from autotrader import vault as V
from autotrader.config import Config

from . import VERSION
from . import settings as S
from .browser import Browser
from .github import GitHub

log = logging.getLogger("collector")

# Kept per car, to send a photo only until the bot has surely had it.
PHOTO_SENDS = 3


# ------------------------------------------------------------ local memory

def _memory_path() -> Path:
    return S.home() / "memory.json"


def load_memory() -> dict[str, Any]:
    try:
        return json.loads(_memory_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_memory(memory: dict[str, Any]) -> None:
    path = _memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Forget cars not seen for a month, so this file stays small.
    cutoff = (clock.now() - timedelta(days=30)).isoformat()
    cars = memory.get("cars") or {}
    memory["cars"] = {k: v for k, v in cars.items() if str(v.get("seen") or "") >= cutoff}
    path.write_text(json.dumps(memory, indent=1, sort_keys=True), encoding="utf-8")


# ------------------------------------------------------------- the vault

class Keyring:
    """The vault key and the bot's settings, fetched sealed and opened here."""

    def __init__(self, github: GitHub, passphrase: str) -> None:
        self.github, self.passphrase = github, passphrase
        self.root = S.home() / "vault-copy"
        self._opened: V.Vault | None = None

    def _vault(self) -> V.Vault:
        if self._opened is None:
            self._opened = self._unlock()
        return self._opened

    def _unlock(self) -> V.Vault:
        meta = self.root / V.VAULT_DIR / V.META_FILE
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_bytes(self.github.file(V.META_FILE))
        return V.Vault.unlock(self.root, {V.ENV_KEY: self.passphrase})

    def open(self, stored_as: str) -> tuple[bytes, Any]:
        """A sealed file off the vault branch, opened in memory only."""
        vault = self._vault()
        plain = V.unseal(vault.key, self.github.file(stored_as), stored_as)
        return vault.key, json.loads(plain)

    def config(self) -> tuple[bytes, Config]:
        key, data = self.open("config.enc")
        return key, Config.from_data(data, self.root / "not-written.json")

    def state(self) -> dict[str, Any]:
        return self.open("state.enc")[1]


# ------------------------------------------------------------- the clock

def _minutes(text: str) -> int:
    hours, _, minutes = str(text).partition(":")
    return int(hours) * 60 + int(minutes or 0)


def resting(cfg: S.Settings, now: datetime | None = None) -> bool:
    """Inside the overnight pause, in this computer's own time."""
    now = now or datetime.now()
    start, end, at = _minutes(cfg.quiet_start), _minutes(cfg.quiet_end), \
        now.hour * 60 + now.minute
    if start == end:
        return False
    return start <= at < end if start < end else (at >= start or at < end)


def next_wait(cfg: S.Settings) -> float:
    """Seconds until the next pass: the interval, give or take the jitter."""
    minutes = cfg.every_minutes + random.uniform(-cfg.jitter_minutes, cfg.jitter_minutes)
    return max(5.0, minutes) * 60


def primary_heard(cfg: S.Settings, state: dict[str, Any],
                  now: datetime | None = None) -> str:
    """The primary heard from recently, if any: a standby then stays out."""
    now = now or clock.now()
    for host, heard in ((state.get("marketplace") or {}).get("hosts") or {}).items():
        if host == cfg.host or heard.get("role") != "primary":
            continue
        age = clock.minutes_since(heard.get("received"), now)
        if age is not None and age < cfg.takeover_after_minutes:
            return host
    return ""


# ------------------------------------------------------------- the pass

def _wanted(rec: dict[str, Any], item: dict[str, Any], cfg: Config, search) -> bool:
    """Whether the bot will keep this car: worth opening its own page."""
    car = M.to_listing(rec, search, make=item.get("make", ""), models=item.get("models", []))
    return filters.check(car, cfg.rules_for(search)["filters"]).keep


def read(cfg: Config, plan: list[dict[str, Any]], browser: Browser,
         settings: S.Settings, memory: dict[str, Any]) -> tuple[list[dict], str]:
    """Every search in the plan, as batch parts, and the session's state."""
    by_id = {s.id: s for s in cfg.active_searches}
    cars = memory.setdefault("cars", {})
    parts: list[dict[str, Any]] = []
    # Cars worth opening, newly seen ones first: they are the ones that alert.
    worth: list[tuple[bool, dict[str, Any], dict[str, Any], Any]] = []
    session = "ok"
    stamp = clock.stamp()
    for n, item in enumerate(plan):
        if n:
            browser.rest()
        texts: list[str] = []
        landed, errors = [], []
        for q in item["queries"]:
            page = browser.visit(q["url"], scrolls=settings.scrolls)
            texts += page.texts
            landed.append(page.landed)
            if page.error:
                errors.append(page.error)
            if page.session != "ok":
                session = page.session
        if session != "ok":
            break
        found = M.collect(texts)
        part = {"search": item["search"], "scope": item["scope"],
                "landed": landed[0] if landed else "",
                "ok": bool(found), "error": "; ".join(errors)[:300],
                "listings": list(found.values())}
        if not found and not errors:
            # A loose Marketplace search always shows something, so a page
            # with nothing readable on it is a page this code no longer
            # understands - said, so it cannot pass for a quiet market.
            part["error"] = ("the page loaded but no listings could be read from it"
                             if any(texts) else "the page came back empty")
        parts.append(part)
        search = by_id.get(item["search"])
        for rec in part["listings"]:
            mine = cars.setdefault(rec["id"], {})
            fresh = "first" not in mine
            mine.setdefault("first", stamp)
            mine["seen"] = stamp
            if mine.get("detail") or int(mine.get("tries", 0)) >= 2 \
                    or rec.get("sold") or search is None:
                continue
            if _wanted(rec, item, cfg, search):
                worth.append((not fresh, rec, item, search))

    # A car worth hearing about gets its own page read once, for the exact
    # odometer and the details the results leave out.
    worth.sort(key=lambda w: w[0])
    for _, rec, item, search in worth[:max(0, settings.details_per_cycle)]:
        mine = cars[rec["id"]]
        browser.rest()
        page = browser.visit(M.ITEM_URL.format(rec["id"]))
        if page.session != "ok":
            break
        mine["tries"] = int(mine.get("tries", 0)) + 1
        extra = M.collect(page.texts).get(rec["id"])
        if extra:
            mine["detail"] = stamp
            # The card's own photo stays: it is the small one.
            M.merge(rec, extra)
    return parts, session


def build(settings: S.Settings, parts: list[dict], *, polled: bool, session: str,
          note: str = "") -> dict[str, Any]:
    return {"v": M.BATCH_VERSION, "id": secrets.token_hex(16), "at": clock.stamp(),
            "host": settings.host, "role": settings.role, "polled": polled,
            "session": session, "note": note, "collector": VERSION,
            "searches": parts}


def fit(key: bytes, batch: dict[str, Any], memory: dict[str, Any]) -> str:
    """Seal the batch, trimmed until GitHub will carry it.

    Photos go first for cars already sent a few times, then the oldest cars
    of the biggest search: the next pass sends them again anyway.
    """
    cars = memory.setdefault("cars", {})
    for part in batch["searches"]:
        part["listings"].sort(key=lambda r: r.get("created") or 0, reverse=True)
        for rec in part["listings"]:
            if rec.get("photo") and cars.get(rec["id"], {}).get("photos", 0) >= PHOTO_SENDS:
                rec.pop("photo")
    sealed = M.seal_batch(key, batch)
    while len(sealed) > M.PAYLOAD_LIMIT:
        biggest = max(batch["searches"], key=lambda p: len(p["listings"]), default=None)
        if not biggest or not biggest["listings"]:
            raise ValueError("a batch with no cars in it does not fit a dispatch")
        del biggest["listings"][max(1, len(biggest["listings"]) * 4 // 5):]
        sealed = M.seal_batch(key, batch)
    for part in batch["searches"]:
        for rec in part["listings"]:
            if rec.get("photo"):
                mine = cars.setdefault(rec["id"], {})
                mine["photos"] = int(mine.get("photos", 0)) + 1
    return sealed


def once(settings: S.Settings, *, github: GitHub | None = None,
         browser_factory: Callable[[S.Settings], Browser] = Browser,
         now: datetime | None = None, force: bool = False) -> dict[str, Any]:
    """One pass, start to finish. Returns what happened, for the log."""
    github = github or GitHub(settings.repo, S.secret(S.TOKEN))
    keyring = Keyring(github, S.secret(S.PASSPHRASE))
    key, cfg = keyring.config()
    memory = load_memory()
    plan = M.plan(cfg)

    note, polled, session, parts = "", False, "ok", []
    if not plan:
        note = "no searches to read"
    elif resting(settings, now) and not force:
        note = "overnight pause"
    elif settings.role == "standby" and not force and \
            (primary := primary_heard(settings, keyring.state())):
        note = f"standing by for {primary}"
    else:
        polled = True
        with browser_factory(settings).open() as browser:
            if not browser.signed_in():
                session = "signed_out"
            else:
                parts, session = read(cfg, plan, browser, settings, memory)
        if settings.role == "standby":
            note = "the primary has gone quiet, so this one is reading"

    batch = build(settings, parts, polled=polled, session=session, note=note)
    sealed = fit(key, batch, memory)
    github.send(sealed)
    summary = {"at": batch["at"], "polled": polled, "session": session, "note": note,
               "searches": [{"search": p["search"], "ok": p["ok"], "cars": len(p["listings"]),
                             "error": p["error"]} for p in parts],
               "bytes": len(sealed)}
    memory["last"] = summary
    save_memory(memory)
    return summary


def forever(settings: S.Settings) -> None:
    """Pass after pass, until stopped. A failed pass waits and tries again."""
    while True:
        started = time.monotonic()
        try:
            summary = once(settings)
            log.info("%s", _line(summary))
        except Exception as exc:          # noqa: BLE001 - the loop must survive
            log.error("pass failed: %s", exc)
            memory = load_memory()
            memory["last_error"] = {"at": clock.stamp(), "error": str(exc)[:300]}
            save_memory(memory)
        wait = next_wait(settings) - (time.monotonic() - started)
        time.sleep(max(60.0, wait))


def _line(summary: dict[str, Any]) -> str:
    if not summary.get("polled"):
        return f"checked in ({summary.get('note') or 'resting'})"
    if summary.get("session") != "ok":
        return f"Facebook: {summary['session']} - run collector/run login"
    reads = summary.get("searches") or []
    return (f"read {sum(1 for r in reads if r['ok'])} of {len(reads)} searches, "
            f"{sum(r['cars'] for r in reads)} cars, sent {summary.get('bytes', 0)} bytes")
