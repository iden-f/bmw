"""The watch's config, read from the vault and cached on this Mac.

The searches, rules and channels still live in the vault, so a change made
on the dashboard reaches this service on its next refresh. The last good
copy is cached here, so GitHub being unreachable changes nothing: the
service keeps collecting with the config it already has.

Secrets come from the collector's Keychain items (the GitHub token and the
passphrase); nothing is asked of the owner twice.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from autotrader import clock
from autotrader.config import Config

from . import settings as SETTINGS

log = logging.getLogger("macwatch.vault")


class VaultConfig:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.cache = SETTINGS.home() / "config-cache.json"
        self.state_cache = SETTINGS.home() / "legacy-state.json"
        self._cfg: Config | None = None
        self.fetched_at: str | None = None
        self.last_error: str = ""

    # ------------------------------------------------------------ fetch

    def _keyring(self):
        from collector import settings as CS
        from collector.cycle import Keyring
        from collector.github import GitHub
        cs = CS.Settings.load()
        return Keyring(GitHub(cs.repo, CS.secret(CS.TOKEN)), CS.secret(CS.PASSPHRASE))

    def refresh(self, *, force: bool = False) -> Config:
        """The current config: fresh from the vault when due, else cached."""
        due = force or self._cfg is None or (
            clock.minutes_since(self.fetched_at) or 1e9) >= \
            max(1, int(self.settings.config_refresh_minutes))
        if due:
            try:
                _, cfg = self._keyring().config()
                self._cfg = cfg
                self.fetched_at = clock.stamp()
                self.last_error = ""
                self._write_cache(cfg.data)
            except Exception as exc:  # noqa: BLE001 - GitHub down is a normal day
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("vault config not refreshed: %s", self.last_error)
                if self._cfg is None:
                    self._cfg = self._read_cache()
        if self._cfg is None:
            raise RuntimeError("no config: the vault could not be read and there is no "
                               f"cached copy ({self.last_error})")
        return self._cfg

    def legacy_state(self) -> dict[str, Any]:
        """The GitHub watcher's state.json, for the one-time import."""
        state = self._keyring().state()
        self._write(self.state_cache, state)
        return state

    # ------------------------------------------------------------ cache

    def _write_cache(self, data: dict[str, Any]) -> None:
        self._write(self.cache, {"fetched_at": self.fetched_at, "config": data})

    def _write(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _read_cache(self) -> Config | None:
        if not self.cache.is_file():
            return None
        try:
            raw = json.loads(self.cache.read_text(encoding="utf-8"))
        except ValueError:
            return None
        self.fetched_at = raw.get("fetched_at")
        return Config.from_data(raw.get("config") or {}, self.cache.with_name("not-written.json"))

    def status(self) -> dict[str, Any]:
        return {"fetched_at": self.fetched_at, "last_error": self.last_error,
                "cached": self.cache.is_file(),
                "searches": len(self._cfg.active_searches) if self._cfg else None}
