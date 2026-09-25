"""First-run provisioning: make the bot reachable with nothing configured.

Every notification channel but ntfy needs a token. ntfy needs only a topic
name, so a fresh install gets a random one and can reach its owner at once.
The topic lives in config.json, which private mode keeps encrypted, and the
dashboard shows it once unlocked.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import secrets
from typing import Any

from .config import Config

log = logging.getLogger(__name__)

# Unambiguous characters only: this ends up typed into a phone by hand.
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
_TOPIC_LENGTH = 20


def generate_topic(prefix: str = "autotrader") -> str:
    """Return a random ntfy topic.

    Whoever knows a topic receives its messages, so it must be unguessable:
    20 characters from a 31-character alphabet is about 99 bits.
    """
    tail = "".join(secrets.choice(_ALPHABET) for _ in range(_TOPIC_LENGTH))
    return f"{prefix}-{tail}"


def subscribe_url(cfg: Config) -> str:
    server = str(cfg.get("notifications.channels.ntfy.server")
                 or "https://ntfy.sh").rstrip("/")
    topic = str(cfg.get("notifications.channels.ntfy.topic") or "").strip()
    return f"{server}/{topic}" if topic else ""


def ensure_notifications(cfg: Config, env: dict[str, str] | None = None
                         ) -> dict[str, Any]:
    """Guarantee the bot can reach the user somehow.

    Does nothing if any channel is already working, so adding a Telegram token
    later does not disturb this and running it twice is harmless.
    """
    env = env if env is not None else dict(os.environ)
    active = cfg.active_channels(env)
    if active:
        return {"changed": False, "reason": f"already reachable via {', '.join(active)}",
                "channels": active}

    topic = str(cfg.get("notifications.channels.ntfy.topic") or "").strip()
    if not topic:
        topic = generate_topic()
        cfg.set("notifications.channels.ntfy.topic", topic)
    cfg.set("notifications.channels.ntfy.enabled", True)
    return {"changed": True, "reason": "no channel was configured, so ntfy was set up",
            "topic": topic, "url": subscribe_url(cfg), "channels": ["ntfy"]}


MOVED_NOTICE = ("These alerts have moved to a new private topic. Open your "
                "dashboard's Status tab and scan the code under \"Get these on "
                "your phone\".")


def rotate_ntfy_topic(cfg: Config) -> bool:
    """Move ntfy to a new topic.

    Used when the watch switches to private mode, because the old topic is
    readable in the public repository. The next check tells the old topic
    that alerts moved (MOVED_NOTICE), never where to.
    """
    if not str(cfg.get("notifications.channels.ntfy.topic") or "").strip():
        return False
    cfg.set("notifications.channels.ntfy.topic", generate_topic())
    return True


def find_dashboard_url(env: dict[str, str] | None = None) -> str:
    """Return the repository's GitHub Pages address, or "" if unknown.

    Alerts link to the dashboard, so the address is derived rather than typed:
    from GITHUB_REPOSITORY inside Actions, otherwise from the git remote.
    """
    env = env if env is not None else dict(os.environ)
    slug = str(env.get("GITHUB_REPOSITORY") or "").strip()
    if not slug:
        try:
            slug = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    match = re.search(r"(?:github\.com[:/])?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$", slug)
    if not match:
        return ""
    return f"https://{match.group(1).lower()}.github.io/{match.group(2)}"


def ensure_dashboard_url(cfg: Config, env: dict[str, str] | None = None
                         ) -> dict[str, Any]:
    """Keep the address alerts link to in step with the repository.

    A custom address is left alone. A github.io address is corrected after a
    repository rename, because Pages does not redirect the old one.
    """
    current = str(cfg.get("notifications.dashboard_url") or "").strip().rstrip("/")
    found = find_dashboard_url(env)
    if not found:
        return {"changed": False,
                "reason": "no git remote to work the dashboard address out from"}
    if current == found:
        return {"changed": False, "reason": "the dashboard address is already set"}
    if current and ".github.io/" not in current:
        return {"changed": False, "reason": "a custom dashboard address is set"}
    cfg.set("notifications.dashboard_url", found)
    return {"changed": True, "reason": "alerts link to the dashboard"}


def bootstrap(cfg: Config, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Do everything a first run needs, idempotently.

    Ensures a way to reach the owner and the address alerts link to, and
    saves config.json only if it changed.
    """
    env = env if env is not None else dict(os.environ)
    steps: list[dict[str, Any]] = []

    channels = ensure_notifications(cfg, env)
    channels["step"] = "notifications"
    steps.append(channels)

    where = ensure_dashboard_url(cfg, env)
    where["step"] = "dashboard"
    steps.append(where)

    changed = any(s.get("changed") for s in steps)
    if changed:
        cfg.save()
    return {"changed": changed, "steps": steps,
            "subscribe_url": subscribe_url(cfg),
            "channels": cfg.active_channels(env)}
