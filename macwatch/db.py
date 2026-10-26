"""The archive: one SQLite database, WAL mode, explicit migrations.

Every table has one job. ``listings`` holds the current facts; everything
that ever changed about a car is appended to ``listing_revisions`` and
``price_events`` and never rewritten. ``outbox`` holds every notification
decision, sent or suppressed, with a key that makes a retry idempotent.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from autotrader import clock

SCHEMA_VERSION = 1

MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE collections (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        site TEXT NOT NULL DEFAULT 'both',
        notify_mode TEXT NOT NULL DEFAULT 'alert',
        enabled INTEGER NOT NULL DEFAULT 1,
        rules_json TEXT NOT NULL DEFAULT '{}',
        search_url TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        baseline_done_at TEXT,
        baseline_marketplace_at TEXT
    );
    CREATE TABLE listings (
        id TEXT PRIMARY KEY,
        site TEXT NOT NULL,
        url TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        year INTEGER,
        make TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        trim TEXT NOT NULL DEFAULT '',
        price INTEGER,
        price_source TEXT NOT NULL DEFAULT '',
        currency TEXT NOT NULL DEFAULT 'CAD',
        mileage_km INTEGER,
        location TEXT NOT NULL DEFAULT '',
        province TEXT NOT NULL DEFAULT '',
        seller TEXT NOT NULL DEFAULT '',
        seller_type TEXT NOT NULL DEFAULT '',
        facts_json TEXT NOT NULL DEFAULT '{}',
        images_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'active',
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        gone_at TEXT,
        posted_at TEXT,
        misses INTEGER NOT NULL DEFAULT 0,
        qualified INTEGER NOT NULL DEFAULT 1,
        hidden_reason TEXT NOT NULL DEFAULT '',
        hidden_rule TEXT NOT NULL DEFAULT '',
        revision INTEGER NOT NULL DEFAULT 1,
        provenance TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX listings_last_seen ON listings(last_seen);
    CREATE INDEX listings_status ON listings(status);
    CREATE TABLE collection_listings (
        collection_id TEXT NOT NULL,
        listing_id TEXT NOT NULL,
        matched_at TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        qualified INTEGER NOT NULL DEFAULT 1,
        reason TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (collection_id, listing_id)
    );
    CREATE INDEX collection_listings_listing ON collection_listings(listing_id);
    CREATE TABLE listing_revisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        listing_id TEXT NOT NULL,
        at TEXT NOT NULL,
        run_id TEXT,
        changes_json TEXT NOT NULL,
        provenance TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX listing_revisions_listing ON listing_revisions(listing_id, at);
    CREATE TABLE price_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        listing_id TEXT NOT NULL,
        at TEXT NOT NULL,
        price INTEGER,
        currency TEXT NOT NULL DEFAULT 'CAD',
        price_source TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'asking',
        run_id TEXT,
        provenance TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX price_events_listing ON price_events(listing_id, at);
    CREATE TABLE sightings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        listing_id TEXT NOT NULL,
        collection_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        at TEXT NOT NULL,
        price INTEGER
    );
    CREATE INDEX sightings_listing ON sightings(listing_id, at);
    CREATE TABLE query_runs (
        id TEXT PRIMARY KEY,
        site TEXT NOT NULL,
        collection_id TEXT NOT NULL DEFAULT '',
        trigger TEXT NOT NULL DEFAULT 'schedule',
        started_at TEXT NOT NULL,
        ended_at TEXT,
        ok INTEGER,
        listings INTEGER NOT NULL DEFAULT 0,
        fresh INTEGER NOT NULL DEFAULT 0,
        complete INTEGER NOT NULL DEFAULT 0,
        confined INTEGER NOT NULL DEFAULT 0,
        pages INTEGER NOT NULL DEFAULT 0,
        requests INTEGER NOT NULL DEFAULT 0,
        strategy TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX query_runs_started ON query_runs(started_at);
    CREATE TABLE media_assets (
        id TEXT PRIMARY KEY,
        mime TEXT NOT NULL,
        bytes INTEGER NOT NULL,
        width INTEGER,
        height INTEGER,
        path TEXT NOT NULL,
        captured_at TEXT NOT NULL
    );
    CREATE TABLE listing_media (
        listing_id TEXT NOT NULL,
        source_url TEXT NOT NULL,
        position INTEGER NOT NULL DEFAULT 0,
        asset_id TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_try_at TEXT,
        error TEXT NOT NULL DEFAULT '',
        queued_at TEXT NOT NULL,
        PRIMARY KEY (listing_id, source_url)
    );
    CREATE INDEX listing_media_status ON listing_media(status, next_try_at);
    CREATE TABLE outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key TEXT NOT NULL UNIQUE,
        listing_id TEXT NOT NULL,
        collection_id TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL,
        channel TEXT NOT NULL DEFAULT 'ntfy',
        status TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_try_at TEXT,
        accepted_at TEXT,
        error TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX outbox_status ON outbox(status, next_try_at);
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        requested_at TEXT NOT NULL,
        started_at TEXT,
        ended_at TEXT,
        result_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE marks (
        listing_id TEXT PRIMARY KEY,
        shortlisted INTEGER NOT NULL DEFAULT 0,
        muted INTEGER NOT NULL DEFAULT 0,
        dismissed INTEGER NOT NULL DEFAULT 0,
        note TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    );
    CREATE TABLE kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
}


class Database:
    """A connection per thread over one file, and the migrations."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._local = threading.local()
        self._lock = threading.RLock()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    # ------------------------------------------------------------ plumbing

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30,
                                   isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            if str(self.path) != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One short write transaction. Serialised across threads here, so
        two workers never contend for SQLite's single writer lock."""
        with self._lock:
            conn = self.connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def q(self, sql: str, params: tuple | list | dict = ()) -> list[sqlite3.Row]:
        return self.connect().execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | list | dict = ()) -> sqlite3.Row | None:
        return self.connect().execute(sql, params).fetchone()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ---------------------------------------------------------- migrations

    def version(self) -> int:
        return int(self.connect().execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> int:
        with self._lock:
            conn = self.connect()
            current = self.version()
            for number in sorted(MIGRATIONS):
                if number <= current:
                    continue
                # executescript commits anything pending first, so the
                # transaction is part of the script itself.
                try:
                    conn.executescript("BEGIN IMMEDIATE;" + MIGRATIONS[number]
                                       + f"PRAGMA user_version={number}; COMMIT;")
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                current = number
            return current

    # ------------------------------------------------------------------ kv

    def get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any, conn: sqlite3.Connection | None = None) -> None:
        sql = "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        if conn is not None:
            conn.execute(sql, (key, json.dumps(value)))
        else:
            with self.tx() as c:
                c.execute(sql, (key, json.dumps(value)))

    # ----------------------------------------------------------- summaries

    def counts(self) -> dict[str, int]:
        out = {}
        for table in ("collections", "listings", "price_events", "listing_revisions",
                      "sightings", "query_runs", "media_assets", "outbox"):
            out[table] = int(self.one(f"SELECT COUNT(*) AS n FROM {table}")["n"])
        out["outbox_pending"] = int(self.one(
            "SELECT COUNT(*) AS n FROM outbox WHERE status='pending'")["n"])
        out["listings_active"] = int(self.one(
            "SELECT COUNT(*) AS n FROM listings WHERE status='active'")["n"])
        out["photos_pending"] = int(self.one(
            "SELECT COUNT(*) AS n FROM listing_media WHERE status='pending'")["n"])
        return out

    def backup(self, target: Path) -> Path:
        """A consistent copy via SQLite's online backup API, never a file copy."""
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        dest = sqlite3.connect(str(tmp))
        try:
            with self._lock:
                self.connect().backup(dest)
        finally:
            dest.close()
        tmp.replace(target)
        return target


def now() -> str:
    return clock.stamp()


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for key in ("facts_json", "images_json", "rules_json", "payload_json",
                "changes_json", "result_json"):
        if key in out:
            try:
                out[key[:-5]] = json.loads(out.pop(key) or "null")
            except ValueError:
                out[key[:-5]] = None
    return out
