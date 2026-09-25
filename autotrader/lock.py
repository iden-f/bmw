"""A single-run lock, so two copies of the bot cannot fight over state.

GitHub Actions serialises scheduled runs with a `concurrency` group, but a
manual `python -m autotrader run` can still overlap a scheduled job on your
own machine, and two runs interleaving writes to state.json cause duplicate
notifications.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

LOCK_PATH = Path(os.getenv("AUTOTRADER_LOCK", ".autotrader.lock"))
# A run that has held the lock longer than this crashed without cleaning up.
STALE_AFTER_SECONDS = 1800


class AlreadyRunning(RuntimeError):
    """Another copy of the bot holds the lock."""


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        # EPERM means it exists but belongs to another user.
        return exc.errno == errno.EPERM
    except (AttributeError, ValueError):
        return True   # cannot tell; assume it is alive and stay out of the way
    return True


@contextmanager
def run_lock(path: Path = LOCK_PATH, *, stale_after: int = STALE_AFTER_SECONDS,
             enabled: bool = True):
    """Hold an exclusive lock for the duration of a run."""
    if not enabled:
        yield False
        return

    held = False
    try:
        # O_EXCL makes creation atomic: whoever creates the file owns the lock.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        existing = _read(path)
        age = time.time() - float(existing.get("started_at", 0) or 0)
        pid = int(existing.get("pid", 0) or 0)
        if age < stale_after and _process_alive(pid):
            raise AlreadyRunning(
                f"another run (pid {pid}) started {int(age)}s ago holds {path}. "
                f"If that is wrong, delete {path} and try again.") from None
        # The holder is gone or has been stuck far too long; take it over.
        log.warning("taking over a stale lock at %s (pid %s, age %ss)", path, pid, int(age))
        try:
            path.unlink()
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except (FileExistsError, OSError) as exc:
            raise AlreadyRunning(f"could not take over {path}: {exc}") from exc
    except OSError as exc:
        # A read-only or full disk should not stop the bot from working.
        log.warning("could not create %s (%s); running without a lock", path, exc)
        yield False
        return

    held = True
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"pid": os.getpid(), "started_at": time.time()}, handle)
        yield True
    finally:
        if held:
            try:
                path.unlink()
            except OSError:
                pass
