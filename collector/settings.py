"""Where the collector keeps its things, and its two secrets.

Settings and the browser profile live in the user's Application Support
folder, never in the clone, so nothing here can be committed by mistake. The
GitHub token and the passphrase live in the macOS Keychain. Elsewhere (tests,
a Linux box) they come from the environment instead.
"""

from __future__ import annotations

import json
import os
import platform
import re
import socket
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SERVICE = "autotrader-watch"
TOKEN = "github-token"
PASSPHRASE = "passphrase"
ENV_TOKEN = "COLLECTOR_GITHUB_TOKEN"
ENV_PASSPHRASE = "WATCH_PASSPHRASE"


def home() -> Path:
    """The collector's own folder."""
    if os.environ.get("COLLECTOR_HOME"):
        return Path(os.environ["COLLECTOR_HOME"])
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "AutoTrader Watch"
    return Path.home() / ".local" / "share" / "autotrader-watch"


def repo_root() -> Path:
    """The clone this collector runs from."""
    return Path(__file__).resolve().parent.parent


def repo_slug(root: Path | None = None) -> str:
    """owner/name of the clone's origin, as GitHub's API wants it."""
    try:
        url = subprocess.run(["git", "-C", str(root or repo_root()), "remote",
                              "get-url", "origin"], capture_output=True, text=True,
                             check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else ""


@dataclass
class Settings:
    repo: str = ""
    # How this computer names itself to the bot. Not the account, not a place.
    host: str = ""
    # "primary" reads Marketplace; "standby" reads only when the primary has
    # gone quiet or Facebook will not let it in, so two computers never
    # double the load on one account.
    role: str = "primary"
    every_minutes: int = 25
    jitter_minutes: int = 5
    # Local time. Overnight it only checks in, which also keeps the bot's own
    # timer going.
    quiet_start: str = "00:30"
    quiet_end: str = "06:30"
    # A standby takes over when the primary has not been heard for this long.
    takeover_after_minutes: int = 75
    # Listing pages opened per cycle for cars that pass your rules, for the
    # exact odometer and details the results page leaves out.
    details_per_cycle: int = 3
    # Scrolls per search: each brings another screenful of results.
    scrolls: int = 2
    show_browser: bool = False
    # "chrome" uses the installed Google Chrome; "" the Playwright Chromium.
    channel: str = "chrome"
    # Only for tests: where "https://www.facebook.com" really is.
    facebook: str = "https://www.facebook.com"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return home() / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        path = home() / "settings.json"
        data: dict[str, Any] = {}
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        s = cls(**known)
        s.repo = s.repo or repo_slug()
        s.host = s.host or default_host()
        return s

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        return self.path


def default_host() -> str:
    """A short, plain name for this computer: letters, digits and dashes."""
    name = socket.gethostname().split(".")[0].lower()
    return re.sub(r"[^a-z0-9-]+", "-", name).strip("-")[:30] or "collector"


# ---------------------------------------------------------------- secrets

def _keychain() -> bool:
    return platform.system() == "Darwin" and not os.environ.get("COLLECTOR_NO_KEYCHAIN")


def secret(name: str) -> str:
    """A secret from the Keychain, or from the environment off a Mac."""
    env = {TOKEN: ENV_TOKEN, PASSPHRASE: ENV_PASSPHRASE}[name]
    if os.environ.get(env):
        return os.environ[env]
    if not _keychain():
        return ""
    out = subprocess.run(["security", "find-generic-password", "-s", SERVICE,
                          "-a", name, "-w"], capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else ""


def store_secret(name: str) -> bool:
    """Ask for a secret and put it in the Keychain.

    The Keychain's own tool does the asking, so the secret never passes
    through this program's arguments, its output or the shell's history.
    """
    if not _keychain():
        print(f"Not a Mac: set {ENV_TOKEN if name == TOKEN else ENV_PASSPHRASE} "
              f"in the environment instead.")
        return False
    print(f"Paste the {'GitHub token' if name == TOKEN else 'passphrase'} "
          f"when asked (it will not show), then again to confirm.")
    done = subprocess.run(["security", "add-generic-password", "-U", "-s", SERVICE,
                           "-a", name, "-w"])
    return done.returncode == 0


def forget_secrets() -> None:
    if _keychain():
        for name in (TOKEN, PASSPHRASE):
            subprocess.run(["security", "delete-generic-password", "-s", SERVICE,
                            "-a", name], capture_output=True)
