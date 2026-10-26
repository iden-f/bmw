"""The service: a scheduler, two source workers, the outbox and media workers.

One process. Due times are persisted, so a restart resumes rather than
replaying missed ticks, and a slow pass never queues a burst behind it: a
source that is running is simply not started again until it finishes.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

import requests

from autotrader import clock

from . import media, outbox, sources
from .db import Database, now
from .vaultsync import VaultConfig

log = logging.getLogger("macwatch.service")

SITES = ("autotrader", "marketplace")
TICK_SECONDS = 15


def _minutes(text: str) -> int:
    hours, _, minutes = str(text).partition(":")
    return int(hours) * 60 + int(minutes or 0)


def in_quiet_hours(settings, when: datetime | None = None) -> bool:
    try:
        from zoneinfo import ZoneInfo
        local = (when or clock.now()).astimezone(ZoneInfo(settings.timezone))
        start, end = _minutes(settings.quiet_start), _minutes(settings.quiet_end)
    except Exception:  # noqa: BLE001 - a bad setting never slows the watch
        return False
    minutes = local.hour * 60 + local.minute
    if start == end:
        return False
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end


class Service:
    def __init__(self, settings, db: Database, vault: VaultConfig | None = None, *,
                 autotrader: Callable[..., Any] = sources.autotrader_pass,
                 marketplace: Callable[..., Any] = sources.marketplace_pass,
                 config_provider: Callable[[], Any] | None = None) -> None:
        self.settings = settings
        self.db = db
        self.vault = vault
        self._config_provider = config_provider
        self._passes = {"autotrader": autotrader, "marketplace": marketplace}
        self._running: dict[str, threading.Thread | None] = {s: None for s in SITES}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.started_at = now()
        self.last_tick: str | None = None

    # ----------------------------------------------------------- config

    def config(self):
        if self._config_provider is not None:
            return self._config_provider()
        if self.vault is None:
            raise RuntimeError("no config source")
        return self.vault.refresh()

    # --------------------------------------------------------- schedule

    def enabled(self, site: str) -> bool:
        return bool(self.settings.autotrader_enabled if site == "autotrader"
                    else self.settings.marketplace_enabled)

    def interval_minutes(self, site: str, when: datetime | None = None) -> int:
        base = int(self.settings.interval_minutes if site == "autotrader"
                   else self.settings.marketplace_interval_minutes)
        if in_quiet_hours(self.settings, when):
            return max(base, int(self.settings.quiet_interval_minutes))
        return max(1, base)

    def next_due(self, site: str) -> datetime | None:
        return clock.parse(self.db.get(f"next_due:{site}"))

    def _set_next_due(self, site: str, when: datetime | None) -> None:
        self.db.set(f"next_due:{site}", clock.stamp(when) if when else None)

    def is_running(self, site: str) -> bool:
        thread = self._running.get(site)
        return bool(thread and thread.is_alive())

    def due(self, site: str, when: datetime | None = None) -> bool:
        if not self.enabled(site) or self.is_running(site):
            return False
        when = when or clock.now()
        due_at = self.next_due(site)
        return due_at is None or when >= due_at

    def request_check(self, site: str = "all") -> str:
        """Check now: the next tick starts whatever is not already running."""
        job_id = uuid.uuid4().hex[:12]
        with self.db.tx() as conn:
            conn.execute("INSERT INTO jobs(id, kind, status, requested_at) VALUES(?,?,?,?)",
                         (job_id, f"check:{site}", "queued", now()))
        for s in SITES if site == "all" else (site,):
            self._set_next_due(s, clock.now())
            self.db.set(f"job:{s}", job_id)
        return job_id

    def job(self, job_id: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if row is None:
            return None
        out = dict(row)
        import json
        out["result"] = json.loads(out.pop("result_json") or "{}")
        return out

    # ------------------------------------------------------------- runs

    def run_site(self, site: str, *, trigger: str = "schedule", wait: bool = True) -> Any:
        """Run one source now (in this thread when ``wait``)."""
        if wait:
            return self._run(site, trigger)
        thread = threading.Thread(target=self._run, args=(site, trigger),
                                  name=f"macwatch-{site}", daemon=True)
        self._running[site] = thread
        thread.start()
        return thread

    def _run(self, site: str, trigger: str) -> Any:
        job_id = self.db.get(f"job:{site}")
        started = time.monotonic()
        if job_id:
            with self.db.tx() as conn:
                conn.execute("UPDATE jobs SET status='running', started_at=? WHERE id=? "
                             "AND status='queued'", (now(), job_id))
        report = None
        try:
            cfg = self.config()
            report = self._passes[site](self.db, cfg, self.settings, trigger=trigger)
            line = report.line()
            log.info("%s", line)
            self.db.set(f"last:{site}", {"at": now(), "ok": report.ok, "line": line[:600],
                                         "session": getattr(report, "session", "ok"),
                                         "seconds": round(time.monotonic() - started, 1),
                                         "trigger": trigger})
            if report.ok:
                self.db.set(f"last_ok:{site}", now())
                self._heartbeat()
        except Exception as exc:  # noqa: BLE001 - the service outlives a pass
            log.exception("%s pass crashed", site)
            self.db.set(f"last:{site}", {"at": now(), "ok": False,
                                         "line": f"{type(exc).__name__}: {exc}"[:600],
                                         "trigger": trigger})
        finally:
            # The next due time counts from the end of this pass, so a slow
            # site slows the cadence rather than stacking passes.
            self._set_next_due(site, clock.now() + timedelta(
                minutes=self.interval_minutes(site)))
            if job_id:
                import json
                with self.db.tx() as conn:
                    conn.execute("UPDATE jobs SET status=?, ended_at=?, result_json=? WHERE id=?",
                                 ("succeeded" if report is not None and report.ok else "failed",
                                  now(), json.dumps({"line": report.line() if report else
                                                     "crashed"}), job_id))
                self.db.set(f"job:{site}", None)
        return report

    def _heartbeat(self) -> None:
        url = str(self.settings.heartbeat_url or "").strip()
        if not url:
            return
        try:
            requests.get(url, timeout=10)
        except requests.RequestException as exc:
            log.info("heartbeat not delivered: %s", exc)

    # ------------------------------------------------------------- loop

    def tick(self, when: datetime | None = None) -> list[str]:
        """One scheduler step: start what is due, deliver, capture."""
        when = when or clock.now()
        started: list[str] = []
        for site in SITES:
            if self.due(site, when):
                self.run_site(site, trigger="check-now" if self.db.get(f"job:{site}")
                              else "schedule", wait=False)
                started.append(site)
        try:
            sent = outbox.deliver(self.db, self.settings)
            if sent.get("sent") or sent.get("failed"):
                log.info("outbox: %s", sent)
        except Exception:  # noqa: BLE001
            log.exception("outbox worker failed")
        try:
            got = media.capture(self.db, self.settings)
            if got.get("saved") or got.get("failed"):
                log.info("photos: %s", got)
        except Exception:  # noqa: BLE001
            log.exception("media worker failed")
        self.last_tick = now()
        return started

    def serve_forever(self) -> None:
        log.info("macwatch service started; data in %s", self.db.path)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("tick failed")
            self._stop.wait(TICK_SECONDS)

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"started_at": self.started_at, "last_tick": self.last_tick,
                               "quiet_hours": in_quiet_hours(self.settings), "sources": {}}
        for site in SITES:
            due_at = self.next_due(site)
            out["sources"][site] = {
                "enabled": self.enabled(site), "running": self.is_running(site),
                "next_due": clock.stamp(due_at) if due_at else None,
                "interval_minutes": self.interval_minutes(site),
                "last": self.db.get(f"last:{site}"), "last_ok": self.db.get(f"last_ok:{site}"),
            }
        if self.vault is not None:
            out["vault"] = self.vault.status()
        out["counts"] = self.db.counts()
        out["media_paused"] = self.db.get("media_paused")
        return out
