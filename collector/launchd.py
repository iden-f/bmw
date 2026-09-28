"""Keeping the collector running on a Mac: a launchd agent.

The agent starts at login, restarts the collector if it stops, and holds the
Mac awake while it runs (caffeinate -i), so an idle Mac does not sleep and
end the watch. That does not cover a closed lid: a MacBook still sleeps when
its lid is closed, unless it is on power with an external display attached.
It runs as the signed-in user, which is what lets it read the Keychain.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

from . import settings as S

LABEL = "com.autotrader-watch.collector"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_path() -> Path:
    return S.home() / "collector.log"


def agent() -> dict:
    """The agent's definition."""
    run = S.repo_root() / "collector" / "run"
    return {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/caffeinate", "-i", str(run), "daemon"],
        "RunAtLoad": True,
        "KeepAlive": True,
        # A collector that fails at once is not restarted in a tight loop.
        "ThrottleInterval": 300,
        "ProcessType": "Background",
        "StandardOutPath": str(log_path()),
        "StandardErrorPath": str(log_path()),
        "WorkingDirectory": str(S.repo_root()),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"},
    }


def install() -> Path:
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    log_path().parent.mkdir(parents=True, exist_ok=True)
    uninstall(quiet=True)
    path.write_bytes(plistlib.dumps(agent()))
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True)
    return path


def uninstall(*, quiet: bool = False) -> bool:
    path = plist_path()
    done = subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                          capture_output=True)
    if path.exists():
        path.unlink()
    if not quiet and done.returncode != 0:
        print("it was not running")
    return done.returncode == 0


def running() -> bool:
    try:
        done = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                              capture_output=True, text=True)
    except OSError:
        return False
    return done.returncode == 0 and "state = running" in done.stdout
