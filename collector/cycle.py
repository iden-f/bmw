"""One pass: read the watch's searches on Marketplace and send what was seen.

The searches come from the bot's own settings, fetched sealed from the vault
and opened here with the passphrase, so a search changed on the dashboard is
searched here on the next pass with nothing to edit on this computer.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import secrets
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import requests

from autotrader import clock, filters
from autotrader import marketplace as M
from autotrader import vault as V
from autotrader.config import Config

from . import VERSION
from . import settings as S
from .browser import Browser
from .github import GitHub

log = logging.getLogger("collector")

# Kept per car, to send a photo's address only until the bot has surely
# had it.
PHOTO_SENDS = 3
# Facebook's photo links are signed and die within days, and its CDN turns a
# GitHub runner away once they have: the bot could only ever save a photo it
# was handed within minutes of the read. So the collector, which can reach
# the photo while the link is fresh, fetches the card's photo itself and
# carries the picture in the batch, once per car. This many per pass, newest
# cars first; the rest go in the passes after, every five minutes.
PHOTOS_PER_PASS = 40
PHOTO_MAX_BYTES = 200_000
PHOTO_TYPES = ("image/jpeg", "image/webp", "image/png", "image/avif")
# What a sealed batch may grow to as a blob. Far under GitHub's 100 MB, and
# room for a first pass of photos on top of a thousand cars.
BLOB_LIMIT = 8_000_000
# How a pass reaches the photo CDN; tests put a stand-in here.
PHOTO_HTTP: Callable[[], Any] = requests.Session

# What a car's own page says that its card does not. Kept per car and sent
# with it in every later batch: the bot judges each batch as it comes, so a
# card alone would let back in a car the page showed a rule should hide, and
# a batch GitHub drops would lose the page for good.
FACTS = ("mileage_km", "title_status", "transmission", "fuel", "color", "vin",
         "trim", "make", "model", "seller_type", "created", "carfax_url")

# After Facebook signs the collector out or asks the account to confirm who
# it is, passes only check in for this long, or until collector/run login:
# every read before then would land on the same page, on an account Facebook
# is already wary of.
HOLD_HOURS = 12

# Sends refused in a row before a pass stops reading Facebook and only tries
# to check in: nothing it read could be delivered anyway.
SEND_TRIES = 3

# What a collector says when Facebook will not let it read: it holds off,
# and a standby covers for a primary that says it.
CANNOT_READ = ("signed_out", "checkpoint")
# Why it is holding off, for the batch's note.
HELD_BECAUSE = {"signed_out": "Facebook signed the collector out",
                "checkpoint": "Facebook asked the account to confirm who it is"}


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
    """Inside the overnight window, in this computer's own time.

    Resting means the slower overnight pace. Whether a resting pass reads
    or only checks in is `quiet_reads`.
    """
    now = now or datetime.now()
    start, end, at = _minutes(cfg.quiet_start), _minutes(cfg.quiet_end), \
        now.hour * 60 + now.minute
    if start == end:
        return False
    return start <= at < end if start < end else (at >= start or at < end)


def next_wait(cfg: S.Settings, now: datetime | None = None) -> float:
    """Seconds until the next pass: the interval, give or take the jitter.

    While resting the interval is quiet_every_minutes, cut short so the first
    pass after the window is on time rather than up to that long late.
    """
    now = now or datetime.now()
    if cfg.quiet_every_minutes > cfg.every_minutes and resting(cfg, now):
        end, at = _minutes(cfg.quiet_end), now.hour * 60 + now.minute
        until_end = ((end - at) % (24 * 60)) * 60 - now.second
        return max(5.0, min(cfg.quiet_every_minutes * 60, until_end))
    minutes = cfg.every_minutes + random.uniform(-cfg.jitter_minutes, cfg.jitter_minutes)
    return max(5.0, minutes) * 60


def primary_heard(cfg: S.Settings, state: dict[str, Any],
                  now: datetime | None = None) -> str:
    """The primary heard from recently, if any: a standby then stays out.

    Not one that last said Facebook signed it out or stopped it at a
    checkpoint: it checks in but reads nothing, which is what a standby is
    for.
    """
    now = now or clock.now()
    for host, heard in ((state.get("marketplace") or {}).get("hosts") or {}).items():
        if host == cfg.host or heard.get("role") != "primary":
            continue
        if heard.get("session") in CANNOT_READ:
            continue
        age = clock.minutes_since(heard.get("received"), now)
        if age is not None and age < cfg.takeover_after_minutes:
            return host
    return ""


def heard_lately(cfg: S.Settings, state: dict[str, Any],
                 now: datetime | None = None) -> bool:
    """Whether the bot has taken in this computer's own batches lately.

    If it has not, this computer is new, or the bot is not taking batches in
    at all (GitHub is down, the workflow is switched off): then the primary's
    silence says nothing about the primary, and reading as well would only
    double the load on the Facebook account.
    """
    heard = ((state.get("marketplace") or {}).get("hosts") or {}).get(cfg.host) or {}
    age = clock.minutes_since(heard.get("received"), now or clock.now())
    return age is not None and age < 2 * (cfg.every_minutes + cfg.jitter_minutes) + 15


def standing_by(cfg: S.Settings, state: dict[str, Any],
                now: datetime | None = None) -> str:
    """Why a standby stays out this pass, or "" when it should read."""
    primary = primary_heard(cfg, state, now)
    if primary:
        return f"standing by for {primary}"
    if not heard_lately(cfg, state, now):
        return "standing by: the bot has not taken in this computer's batches lately"
    return ""


def held(memory: dict[str, Any], now: datetime | None = None) -> str:
    """What Facebook last said to the collector, while passes hold off for it."""
    hold = memory.get("held") or {}
    hours = clock.hours_since(hold.get("at"), now or clock.now())
    return str(hold.get("session") or "") if hours is not None and hours < HOLD_HOURS else ""


# ------------------------------------------------------------- the pass

def _wanted(rec: dict[str, Any], item: dict[str, Any], cfg: Config, search) -> bool:
    """Whether the bot will keep this car: worth opening its own page."""
    car = M.to_listing(rec, search, make=item.get("make", ""),
                       models=item.get("models") or (),
                       aliases=item.get("aliases") or ())
    return filters.check(car, cfg.rules_for(search)["filters"]).keep


def read(cfg: Config, plan: list[dict[str, Any]], browser: Browser,
         settings: S.Settings, memory: dict[str, Any], *, details: bool = True,
         pages: list[dict[str, Any]] | None = None) -> tuple[list[dict], str]:
    """Every search in the plan, as batch parts, and the session's state.

    Without ``details`` no listing page is opened. ``pages``, when given,
    collects what each search page received, for a local capture.
    """
    by_id = {s.id: s for s in cfg.active_searches}
    cars = memory.setdefault("cars", {})
    parts: list[dict[str, Any]] = []
    # Cars worth opening, newly seen ones first: they are the ones that alert.
    # Once each, though two searches find it.
    worth: list[tuple[bool, dict[str, Any], dict[str, Any], Any]] = []
    queued: set[str] = set()
    session = "ok"
    stamp = clock.stamp()
    opened = 0
    for item in plan:
        texts: list[str] = []
        landed, errors = [], []
        for q in item["queries"]:
            # The pause a person takes between pages, between the models
            # of one search as between searches.
            if opened:
                browser.rest()
            opened += 1
            page = browser.visit(q["url"], scrolls=settings.scrolls)
            texts += page.texts
            if pages is not None:
                pages.append({"search": item["name"], "query": q["query"],
                              "landed": page.landed, "texts": list(page.texts)})
            landed.append(page.landed)
            if page.error:
                errors.append(page.error)
            if page.session != "ok":
                session = page.session
                break
        if session != "ok":
            break
        found = M.collect(texts)
        part = {"search": item["search"], "scope": item["scope"],
                "landed": landed[0] if landed else "",
                "ok": bool(found),
                "error": "; ".join(dict.fromkeys(errors))[:300],
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
            # New this pass, though an earlier search saw it first.
            fresh = mine.setdefault("first", stamp) == stamp
            mine["seen"] = stamp
            if mine.get("detail") or int(mine.get("tries", 0)) >= 2 \
                    or rec.get("sold") or search is None or rec["id"] in queued:
                continue
            if _wanted(rec, item, cfg, search):
                worth.append((not fresh, rec, item, search))
                queued.add(rec["id"])

    # Signed out or stopped at a checkpoint: no listing page either.
    if not details or session != "ok":
        return _with_facts(parts, cars), session
    # A car worth hearing about gets its own page read once, for the exact
    # odometer and the details the results leave out.
    worth.sort(key=lambda w: w[0])
    for _, rec, item, search in worth[:max(0, settings.details_per_cycle)]:
        mine = cars[rec["id"]]
        browser.rest()
        page = browser.visit(M.ITEM_URL.format(rec["id"]))
        if page.session != "ok":
            session = page.session
            break
        mine["tries"] = int(mine.get("tries", 0)) + 1
        extra = M.collect(page.texts).get(rec["id"])
        if extra:
            mine["detail"] = stamp
            facts = {k: extra[k] for k in FACTS if k in extra}
            if extra.get("mileage_rounded"):
                facts.pop("mileage_km", None)     # no better than the card's
            mine["facts"] = facts
            # The card's own photo stays: it is the small one.
            M.merge(rec, extra)
    return _with_facts(parts, cars), session


def _with_facts(parts: list[dict[str, Any]], cars: dict[str, Any]) -> list[dict[str, Any]]:
    """Every car read, with what its own page said, this pass or an earlier one."""
    for part in parts:
        for rec in part["listings"]:
            facts = (cars.get(rec["id"]) or {}).get("facts")
            if facts:
                M.merge(rec, facts)
    return parts


def build(settings: S.Settings, parts: list[dict], *, polled: bool, session: str,
          note: str = "", wait: float | None = None,
          failure: dict[str, Any] | None = None) -> dict[str, Any]:
    """The batch as sent. Besides what was read, it says how this collector
    is set up and when to expect the next one, so the dashboard can show
    both without being able to change either."""
    out = {"v": M.BATCH_VERSION, "id": secrets.token_hex(16), "at": clock.stamp(),
           "host": settings.host, "role": settings.role, "polled": polled,
           "session": session, "note": note, "collector": VERSION,
           "settings": {"every_minutes": settings.every_minutes,
                        "jitter_minutes": settings.jitter_minutes,
                        "quiet_start": settings.quiet_start,
                        "quiet_end": settings.quiet_end,
                        "quiet_every_minutes": settings.quiet_every_minutes,
                        "quiet_reads": settings.quiet_reads,
                        "details_per_cycle": settings.details_per_cycle,
                        "scrolls": settings.scrolls,
                        "takeover_after_minutes": settings.takeover_after_minutes},
           "searches": parts}
    if wait is not None:
        out["next_at"] = (clock.now() + timedelta(seconds=wait)).isoformat(timespec="seconds")
    if failure:
        out["last_failure"] = failure
    return out


def fit(key: bytes, batch: dict[str, Any], memory: dict[str, Any],
        limit: int = BLOB_LIMIT) -> str:
    """Seal the batch, trimmed only if GitHub could not carry it at all.

    A dispatch carries 65,535 characters and a blob 100 MB; the send picks
    whichever the batch needs, so a thousand cars and their photos go whole.
    Only past ``limit`` is anything left out, and then pictures go first
    (they are sent again next pass), then the photo addresses of the oldest
    cars, then the oldest cars of the biggest search. A car left out of a
    first read would come back later as news, so a photo always goes before
    a car.
    """
    cars = memory.setdefault("cars", {})
    for part in batch["searches"]:
        part["listings"].sort(key=lambda r: r.get("created") or 0, reverse=True)
        for rec in part["listings"]:
            if rec.get("photo") and cars.get(rec["id"], {}).get("photos", 0) >= PHOTO_SENDS:
                rec.pop("photo")
    sealed = M.seal_batch(key, batch)
    # Every search's newest cars first, so what is left out is the oldest
    # cars' of every search, not all of the last one's.
    by_age = [rec for _, _, rec in sorted(
        ((n, i, rec) for i, part in enumerate(batch["searches"])
         for n, rec in enumerate(part["listings"])), key=lambda t: t[:2])]
    pictures = [rec for rec in by_age if rec.get("photo_data")]
    while len(sealed) > limit and pictures:
        cut = max(1, len(pictures) // 5)
        for rec in pictures[-cut:]:
            rec.pop("photo_data", None)
            rec.pop("photo_type", None)
        del pictures[-cut:]
        sealed = M.seal_batch(key, batch)
    photos = [rec for rec in by_age if rec.get("photo")]
    while len(sealed) > limit and photos:
        cut = max(1, len(photos) // 5)
        for rec in photos[-cut:]:
            rec.pop("photo")
        del photos[-cut:]
        sealed = M.seal_batch(key, batch)
    while len(sealed) > limit:
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
            if rec.get("photo_data"):
                # Carried once: a picture is most of a batch's weight.
                cars.setdefault(rec["id"], {})["picture"] = clock.stamp()
    return sealed


def pictures(cfg: Config, plan: list[dict[str, Any]], parts: list[dict[str, Any]],
             memory: dict[str, Any], http: Any = None, *,
             limit: int = PHOTOS_PER_PASS, pause: Callable[[], None] | None = None
             ) -> int:
    """Fetch the card photo of the cars the bot will keep, into the batch.

    Newest cars first, once per car, ``limit`` per pass: a first read of a
    big search spreads its pictures over the passes that follow. Only cars
    the bot's own rules keep, since it saves no photo of a hidden car. A
    photo that will not come (gone, too big, not an image) is given up on
    for the car, not asked for again every pass. Nothing here may fail a
    pass: a picture is a nicety, and the read is the job.
    """
    cars = memory.setdefault("cars", {})
    by_id = {s.id: s for s in cfg.active_searches}
    items = {item["search"]: item for item in plan}
    wanted: list[tuple[int, dict[str, Any]]] = []
    seen: set[str] = set()
    for part in parts:
        search = by_id.get(str(part.get("search") or ""))
        item = items.get(str(part.get("search") or ""))
        if search is None or item is None or not part.get("ok"):
            continue
        for rec in part.get("listings") or []:
            if not rec.get("photo") or rec["id"] in seen or rec.get("sold"):
                continue
            mine = cars.get(rec["id"]) or {}
            if mine.get("picture") or mine.get("no_picture"):
                continue
            if not _wanted(rec, item, cfg, search):
                continue
            seen.add(rec["id"])
            wanted.append((int(rec.get("created") or 0), rec))
    wanted.sort(key=lambda t: t[0], reverse=True)
    http = http if http is not None else PHOTO_HTTP()
    carried = 0
    for _, rec in wanted[:max(0, int(limit))]:
        got = _picture(http, rec["photo"])
        if got is None:
            cars.setdefault(rec["id"], {})["no_picture"] = clock.stamp()
            continue
        blob, content_type = got
        rec["photo_data"] = base64.b64encode(blob).decode("ascii")
        rec["photo_type"] = content_type
        carried += 1
        if pause:
            pause()
    return carried


def _picture(http: Any, url: str) -> tuple[bytes, str] | None:
    """One photo off the CDN, if it is a small enough image; else None."""
    try:
        r = http.get(url, timeout=20,
                     headers={"Accept": "image/avif,image/webp,image/*,*/*;q=0.8"})
    except Exception as exc:  # noqa: BLE001 - a photo never fails a pass
        log.info("photo not fetched: %s", str(exc)[:120])
        return None
    kind = str(r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    body = r.content or b""
    if r.status_code != 200 or kind not in PHOTO_TYPES or not body \
            or len(body) > PHOTO_MAX_BYTES:
        log.info("photo not kept: HTTP %s, %s, %d bytes", r.status_code, kind or "no type",
                 len(body))
        return None
    return body, kind


def once(settings: S.Settings, *, github: GitHub | None = None,
         browser_factory: Callable[[S.Settings], Browser] = Browser,
         now: datetime | None = None, force: bool = False,
         wait: float | None = None) -> dict[str, Any]:
    """One pass, start to finish. Returns what happened, for the log.

    ``wait`` is how long until the next pass, when one is scheduled; the
    batch carries it so the dashboard can say when to expect the next.
    """
    github = github or GitHub(settings.repo, S.secret(S.TOKEN))
    keyring = Keyring(github, S.secret(S.PASSPHRASE))
    key, cfg = keyring.config()
    memory = load_memory()
    plan = M.plan(cfg)

    note, polled, session, parts = "", False, "ok", []
    if not plan:
        note = "no searches to read"
    elif (asked := held(memory)) and not force:
        # Still said, so the bot keeps the alarm and a standby covers.
        session = asked
        note = f"held after {HELD_BECAUSE.get(asked, asked)}: run collector/run login"
    elif resting(settings, now) and not settings.quiet_reads and not force:
        note = "overnight pause"
    elif int(memory.get("send_failures") or 0) >= SEND_TRIES and not force:
        note = "waiting for GitHub to accept a batch"
    elif settings.role == "standby" and not force and \
            (why := standing_by(settings, keyring.state())):
        note = why
    else:
        polled = True
        settings, plan = _lighter(settings, plan, memory)
        with browser_factory(settings).open() as browser:
            if not browser.signed_in():
                session = "signed_out"
            else:
                parts, session = read(cfg, plan, browser, settings, memory)
        _count_empty(memory, parts, session)
        if settings.role == "standby":
            note = "the primary is not reading, so this one is"
        if session in CANNOT_READ:
            memory["held"] = {"session": session, "at": clock.stamp()}
        elif session == "ok":
            memory.pop("held", None)
        # Which pages were opened is kept even if the send below fails, so
        # no listing page is opened again for want of a delivery.
        save_memory(memory)

    # A pass that failed before it could send says so in the next one.
    failure = memory.get("last_error")
    batch = build(settings, parts, polled=polled, session=session, note=note,
                  wait=wait, failure=failure)
    carried = 0
    if parts and session == "ok":
        try:
            carried = pictures(cfg, plan, parts, memory, PHOTO_HTTP(),
                               pause=lambda: time.sleep(0.3))
        except Exception as exc:  # noqa: BLE001 - a picture never fails a pass
            log.warning("pictures not fetched this pass: %s", exc)
    sealed = fit(key, batch, memory)
    try:
        sent = github.send(sealed)
    except Exception:
        _send_failed()
        raise
    memory.pop("last_error", None)
    memory.pop("send_failures", None)
    summary = {"at": batch["at"], "polled": polled, "session": session, "note": note,
               "searches": [{"search": p["search"], "ok": p["ok"], "cars": len(p["listings"]),
                             "error": p["error"]} for p in parts],
               "bytes": len(sealed), "pictures": carried,
               "blob": (sent or {}).get("blob") if isinstance(sent, dict) else None}
    memory["last"] = summary
    save_memory(memory)
    return summary


def _count_empty(memory: dict[str, Any], parts: list[dict], session: str) -> None:
    """Passes in a row that read no car at all though the session was fine.

    Facebook answering every search with an empty result is not a quiet
    market: it is the account being slowed. The count lightens the next reads
    and stretches the wait, and the watchdog reports it.
    """
    if session != "ok" or not parts:
        return
    if any(p["listings"] for p in parts):
        memory.pop("empty_passes", None)
    else:
        memory["empty_passes"] = int(memory.get("empty_passes") or 0) + 1


def _lighter(settings: S.Settings, plan: list[dict[str, Any]],
             memory: dict[str, Any]) -> tuple[S.Settings, list[dict[str, Any]]]:
    """After an empty pass, ask for less: fewer scrolls and one order per model."""
    if int(memory.get("empty_passes") or 0) < 1:
        return settings, plan
    lighter = []
    for item in plan:
        seen: set[str] = set()
        once_each = [q for q in item["queries"]
                     if not (q["query"] in seen or seen.add(q["query"]))]
        lighter.append(dict(item, queries=once_each))
    return replace(settings, scrolls=min(settings.scrolls, 1)), lighter


def backoff(planned: float, memory: dict[str, Any]) -> float:
    """The wait doubles with each empty pass in a row, up to an hour."""
    empty = int(memory.get("empty_passes") or 0)
    return planned if not empty else max(planned, min(3600.0, planned * 2 ** min(empty, 4)))


def _send_failed() -> None:
    """Count a refused send against the memory as last saved: what was read
    is in it, the photos this batch would have carried are not."""
    memory = load_memory()
    memory["send_failures"] = int(memory.get("send_failures") or 0) + 1
    save_memory(memory)


# A pass reads a handful of pages and fetches up to 40 pictures: a few
# minutes. One that has run this long is hung (on Oct 26 2026 the Playwright
# driver died under a pass and the process sat in a call that never returned
# for four hours, with launchd showing it running), and nothing inside the
# process can be trusted to notice.
PASS_DEADLINE_SECONDS = 20 * 60


def _give_up(started_at: float) -> None:
    """Leave a hung pass the only way that works: end the process.

    launchd (KeepAlive) starts a fresh one. The browser the pass was driving
    is killed first, since an orphaned Chromium still holding the profile
    would fail the next pass at launch.
    """
    log.error("pass hung for %d minutes; exiting so launchd restarts the collector",
              int((time.monotonic() - started_at) // 60))
    try:
        memory = load_memory()
        memory["last_error"] = {"at": clock.stamp(),
                                "error": "a pass hung and the collector restarted itself"}
        save_memory(memory)
    except Exception:  # noqa: BLE001 - leaving matters more than the note
        pass
    try:
        import subprocess
        subprocess.run(["pkill", "-P", str(os.getpid())], timeout=10, check=False)
    except Exception:  # noqa: BLE001
        pass
    os._exit(75)


def forever(settings: S.Settings, *, deadline: float = PASS_DEADLINE_SECONDS,
            give_up: Callable[[float], None] = _give_up, rounds: int | None = None) -> None:
    """Pass after pass, until stopped. A failed pass waits and tries again.

    A pass that outlives ``deadline`` is hung, and ``give_up`` ends it.
    """
    done = 0
    while rounds is None or done < rounds:
        done += 1
        started = time.monotonic()
        planned = next_wait(settings)
        watchdog = threading.Timer(deadline, give_up, args=(started,))
        watchdog.daemon = True
        watchdog.start()
        try:
            summary = once(settings, wait=planned)
            log.info("%s", _line(summary))
            memory = load_memory()
            if memory.pop("failed_passes", None) is not None:
                save_memory(memory)
        except Exception as exc:          # noqa: BLE001 - the loop must survive
            log.error("pass failed: %s", exc)
            memory = load_memory()
            memory["failed_passes"] = int(memory.get("failed_passes") or 0) + 1
            memory["last_error"] = {"at": clock.stamp(), "error": str(exc)[:300]}
            save_memory(memory)
        finally:
            watchdog.cancel()
        wait = backoff(planned, load_memory()) - (time.monotonic() - started)
        time.sleep(max(60.0, wait))


def _line(summary: dict[str, Any]) -> str:
    if not summary.get("polled"):
        return f"checked in ({summary.get('note') or 'resting'})"
    if summary.get("session") != "ok":
        return f"Facebook: {summary['session']} - run collector/run login"
    reads = summary.get("searches") or []
    return (f"read {sum(1 for r in reads if r['ok'])} of {len(reads)} searches, "
            f"{sum(r['cars'] for r in reads)} cars, {summary.get('pictures', 0)} pictures, "
            f"sent {summary.get('bytes', 0)} bytes"
            + (" as a blob" if summary.get("blob") else ""))
