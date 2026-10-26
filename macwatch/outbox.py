"""Delivering what ingest decided: the outbox worker.

Rows are written in the ingest transaction; this module only moves them
from pending to accepted or failed. A retry sends the same row again under
the same key, so a restart between send and acknowledgement can duplicate a
push but never lose one, and the row says which happened.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any, Callable

import requests

from autotrader import clock
from autotrader.notifiers import _ascii_header, _fits_ntfy, _utf8_header, is_permanent_failure

from .db import Database, now

log = logging.getLogger("macwatch.outbox")

TIMEOUT = 25
MAX_ATTEMPTS = 8


class NtfyPoster:
    """Posts one notification to the trial topic. Raises on refusal."""

    def __init__(self, server: str, topic: str, token: str = "") -> None:
        self.server, self.topic, self.token = server.rstrip("/"), topic.strip().strip("/"), token

    def post(self, payload: dict[str, Any]) -> str:
        if not self.topic:
            raise RuntimeError("no ntfy topic configured")
        headers = {"Title": _utf8_header(str(payload.get("title") or "")[:200]),
                   "Tags": str(payload.get("tags") or "car")}
        if payload.get("click"):
            headers["Click"] = str(payload["click"])
        if payload.get("attach"):
            headers["Attach"] = str(payload["attach"])
        if payload.get("actions"):
            headers["Actions"] = json.dumps(payload["actions"], separators=(",", ":"))
        priority = str(payload.get("priority") or "")
        if priority and priority != "default":
            headers["Priority"] = priority
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        headers = {k: _ascii_header(v) for k, v in headers.items()}
        response = requests.post(f"{self.server}/{self.topic}",
                                 data=_fits_ntfy(str(payload.get("body") or "")),
                                 headers=headers, timeout=TIMEOUT)
        if not response.ok:
            refused = 400 <= response.status_code < 500 and response.status_code != 429
            how = "rejected the request" if refused else "failed"
            raise RuntimeError(f"HTTP {response.status_code}: ntfy {how} - "
                               f"{response.text[:200]}")
        try:
            return str(response.json().get("id") or "")
        except ValueError:
            return ""


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(3600, 60 * (2 ** max(0, attempts - 1))))


def deliver(db: Database, settings, poster: Callable[[dict[str, Any]], str] | None = None,
            *, limit: int = 20) -> dict[str, int]:
    """Send every pending row that is due. Returns counts."""
    counts = {"sent": 0, "failed": 0, "retry": 0, "held": 0}
    topic = str(getattr(settings, "ntfy_topic", "") or "").strip()
    if poster is None:
        if not topic:
            return counts
        import os
        poster = NtfyPoster(settings.ntfy_server, topic, os.environ.get("NTFY_TOKEN", "")).post
    stamp = now()
    rows = db.q("SELECT * FROM outbox WHERE status='pending' AND (next_try_at IS NULL OR "
                "next_try_at<=?) ORDER BY id LIMIT ?", (stamp, limit))
    for row in rows:
        payload = json.loads(row["payload_json"] or "{}")
        attempts = int(row["attempts"]) + 1
        try:
            provider_id = poster(payload)
        except Exception as exc:  # noqa: BLE001 - a channel never stops the worker
            detail = str(exc)[:300]
            permanent = is_permanent_failure(detail) or attempts >= MAX_ATTEMPTS
            with db.tx() as conn:
                conn.execute(
                    "UPDATE outbox SET attempts=?, error=?, status=?, next_try_at=? WHERE id=?",
                    (attempts, detail, "failed" if permanent else "pending",
                     None if permanent else clock.stamp(clock.now() + _backoff(attempts)),
                     row["id"]))
            counts["failed" if permanent else "retry"] += 1
            log.warning("outbox %s: %s (%s)", row["event_key"], detail,
                        "given up" if permanent else "will retry")
            continue
        with db.tx() as conn:
            conn.execute("UPDATE outbox SET attempts=?, status='accepted', accepted_at=?, "
                         "error=? WHERE id=?", (attempts, now(), provider_id, row["id"]))
        counts["sent"] += 1
    return counts


def send_text(settings, title: str, body: str, *, tags: str = "warning",
              priority: str = "high") -> str:
    """A one-off message to the trial topic (tests, service warnings)."""
    import os
    poster = NtfyPoster(settings.ntfy_server, settings.ntfy_topic,
                        os.environ.get("NTFY_TOKEN", ""))
    label = str(getattr(settings, "ntfy_label", "") or "")
    return poster.post({"title": label + title, "body": body, "tags": tags,
                        "priority": priority})
