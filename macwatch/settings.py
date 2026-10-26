"""Where the service keeps its things, and the settings it owns.

Runtime data lives under ``~/Library/Application Support/AutoTrader Watch/
macwatch/`` (``MACWATCH_HOME`` overrides it): ``data/`` for the database,
``media/`` for saved photos, ``backups/`` and ``logs/``. Never in the clone,
never in a synced folder.

The service owns these settings; the watch's searches and rules still come
from the vault config (see ``vaultsync``), so a search changed on the
dashboard is read here too.
"""

from __future__ import annotations

import json
import os
import platform
import secrets
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any


def home() -> Path:
    if os.environ.get("MACWATCH_HOME"):
        return Path(os.environ["MACWATCH_HOME"])
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "AutoTrader Watch" / "macwatch"
    return Path.home() / ".local" / "share" / "autotrader-watch" / "macwatch"


def data_dir() -> Path:
    return home() / "data"


def media_dir() -> Path:
    return home() / "media"


def backups_dir() -> Path:
    return home() / "backups"


def logs_dir() -> Path:
    return home() / "logs"


def db_path() -> Path:
    return data_dir() / "macwatch.sqlite3"


def ensure_dirs() -> None:
    for folder in (data_dir(), media_dir(), backups_dir(), logs_dir()):
        folder.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(home(), 0o700)
    except OSError:
        pass


# The ntfy topic the trial posts to is the watch's own topic with this
# suffix, so the phone can subscribe to both and tell them apart.
TRIAL_SUFFIX = "-mac-trial"
TRIAL_LABEL = "[Mac] "


@dataclass
class Settings:
    # Collection cadence. The AutoTrader read is plain HTTP and cheap; the
    # Marketplace read drives the collector's browser and shares its profile
    # lock with the existing collector, so it is slower by default.
    interval_minutes: int = 15
    marketplace_interval_minutes: int = 20
    autotrader_enabled: bool = True
    marketplace_enabled: bool = True
    # Overnight, both sources slow to this interval. Local time in `timezone`.
    quiet_start: str = "23:00"
    quiet_end: str = "08:00"
    quiet_interval_minutes: int = 60
    timezone: str = "America/Vancouver"
    # Marketplace reading, mirroring the collector's own knobs.
    details_per_cycle: int = 3
    scrolls: int = 2
    # Where alerts go. Empty topic: alerts are recorded but not sent.
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_label: str = TRIAL_LABEL
    # The private API. Bound to localhost; Tailscale Serve fronts it.
    api_host: str = "127.0.0.1"
    api_port: int = 8765
    api_token: str = ""
    # Where the app is reachable from the phone (the Tailscale Serve URL),
    # used for the links in alerts. Empty: alerts link to the ad.
    app_url: str = ""
    # Photos saved per listing and the total the media folder may grow to.
    photos_per_listing: int = 12
    media_budget_mb: int = 20000
    # How often the vault config is re-read from GitHub. A failure keeps the
    # cached copy, so GitHub being down changes nothing here.
    config_refresh_minutes: int = 30
    # An external heartbeat (Healthchecks.io or similar) pinged after each
    # successful pass. Empty: none.
    heartbeat_url: str = ""
    # "shadow": this service alerts only to its trial topic. The GitHub
    # watcher keeps the real channels. Nothing else is implemented yet; the
    # field exists so a cutover is an explicit setting, not a code change.
    ownership: str = "shadow"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return home() / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        path = home() / "settings.json"
        data: dict[str, Any] = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                data = {}
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return self.path

    def ensure_token(self) -> str:
        if not self.api_token:
            self.api_token = secrets.token_urlsafe(24)
        return self.api_token

    # Fields the API may change, with their types, so a PATCH is validated.
    EDITABLE = {
        "interval_minutes": int, "marketplace_interval_minutes": int,
        "autotrader_enabled": bool, "marketplace_enabled": bool,
        "quiet_start": str, "quiet_end": str, "quiet_interval_minutes": int,
        "timezone": str, "details_per_cycle": int, "scrolls": int,
        "ntfy_server": str, "ntfy_topic": str, "ntfy_label": str,
        "app_url": str, "photos_per_listing": int, "media_budget_mb": int,
        "config_refresh_minutes": int, "heartbeat_url": str,
    }

    def apply(self, changes: dict[str, Any]) -> dict[str, str]:
        """Apply validated edits. Returns field-level problems, empty if none."""
        problems: dict[str, str] = {}
        for key, value in changes.items():
            kind = self.EDITABLE.get(key)
            if kind is None:
                problems[key] = "not a setting that can be changed here"
                continue
            if kind is bool:
                if not isinstance(value, bool):
                    problems[key] = "must be true or false"
                    continue
            elif kind is int:
                if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                    problems[key] = "must be a whole number"
                    continue
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    problems[key] = "must be a whole number"
                    continue
                if key.endswith("_minutes") and not 1 <= value <= 24 * 60:
                    problems[key] = "must be between 1 and 1440 minutes"
                    continue
                if key in ("details_per_cycle", "scrolls") and not 0 <= value <= 20:
                    problems[key] = "must be between 0 and 20"
                    continue
                if key in ("photos_per_listing",) and not 0 <= value <= 40:
                    problems[key] = "must be between 0 and 40"
                    continue
            elif kind is str:
                if not isinstance(value, str):
                    problems[key] = "must be text"
                    continue
                value = value.strip()
                if key in ("quiet_start", "quiet_end") and not _is_time(value):
                    problems[key] = "must be HH:MM"
                    continue
                if key in ("ntfy_server", "app_url", "heartbeat_url") and value \
                        and not value.startswith(("http://", "https://")):
                    problems[key] = "must be an http(s) URL"
                    continue
                if key == "timezone":
                    try:
                        from zoneinfo import ZoneInfo
                        ZoneInfo(value)
                    except Exception:  # noqa: BLE001
                        problems[key] = "not a known timezone"
                        continue
            if not problems.get(key):
                setattr(self, key, value)
        return problems

    def public(self) -> dict[str, Any]:
        """The settings as the API shows them: no token."""
        out = asdict(self)
        out["api_token"] = "set" if self.api_token else ""
        return out


def _is_time(value: str) -> bool:
    try:
        hours, _, minutes = value.partition(":")
        return 0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59
    except (TypeError, ValueError):
        return False
