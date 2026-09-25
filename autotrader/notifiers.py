"""Notification channels, free ones first.

Every channel's ``send`` returns a Result and never raises, so a channel that
is down cannot fail a run and lose what it found.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from typing import Any

import requests

from . import clock, render
from .state import Change

log = logging.getLogger(__name__)

TIMEOUT = 25


# Failures that retrying cannot fix, such as rejected credentials.
PERMANENT_FAILURE_MARKERS = (
    "535", "5.7.8", "badcredentials", "username and password not accepted",
    "invalid credentials", "authentication failed", "unauthorized",
    "401", "403", "forbidden", "invalid token", "bot token is invalid",
    "chat not found", "bot was blocked", "unknown webhook", "no_service",
    "invalid_auth", "account_inactive", "webhook no longer exists",
    "not accessible", "authenticate",
    # A server refusing the request itself; a retry sends the same request.
    # A phrase rather than "400", because the detail carries the server's
    # reply, and any number in it (a price, say) could match a status code.
    "rejected the request",
)


def is_permanent_failure(detail: str) -> bool:
    """True when retrying this failure cannot possibly help."""
    text = (detail or "").lower()
    return any(marker in text for marker in PERMANENT_FAILURE_MARKERS)


@dataclass
class Result:
    channel: str
    ok: bool
    detail: str = ""
    skipped: bool = False

    @property
    def permanent(self) -> bool:
        """A failure that will recur identically until someone fixes it."""
        return not self.ok and not self.skipped and is_permanent_failure(self.detail)

    def __str__(self) -> str:
        mark = "skipped" if self.skipped else ("sent" if self.ok else "FAILED")
        return f"{self.channel}: {mark}{' - ' + self.detail if self.detail else ''}"


def _count(n: int, word: str, plural: str = "") -> str:
    """A count with the right plural, for the run log and the Status view."""
    return f"{n} {word if n == 1 else (plural or word + 's')}"


class Notifier:
    """Base class.  Subclasses implement ``_send``."""

    name = "base"

    def __init__(self, config: dict[str, Any], env: dict[str, str],
                 settings: dict[str, Any]) -> None:
        self.config = config or {}
        self.env = env
        self.settings = settings or {}

    def send(self, changes: list[Change], run: dict[str, Any] | None = None) -> Result:
        try:
            return self._send(changes, run or {})
        except Exception as exc:  # noqa: BLE001 - a channel may never break a run
            log.warning("%s notification failed: %s", self.name, exc)
            return Result(self.name, False, str(exc)[:300])

    def _send(self, changes: list[Change], run: dict[str, Any]) -> Result:
        raise NotImplementedError

    # Plain-text alert used for health warnings on channels without formatting.
    def send_text(self, subject: str, body: str) -> Result:
        try:
            return self._send_text(subject, body)
        except Exception as exc:  # noqa: BLE001
            return Result(self.name, False, str(exc)[:300])

    def _send_text(self, subject: str, body: str) -> Result:
        raise NotImplementedError

    def verify(self) -> Result:
        """Check the channel is reachable and the credentials work.

        Must not deliver a message: `doctor` runs this on every setup check.
        """
        try:
            return self._verify()
        except Exception as exc:  # noqa: BLE001
            return Result(self.name, False, str(exc)[:300])

    def _verify(self) -> Result:
        return Result(self.name, True, "configured (cannot be checked without sending)",
                      skipped=True)

    @property
    def limit(self) -> int:
        return int(self.settings.get("max_listings_per_message", 12) or 12)


class TelegramNotifier(Notifier):
    """Free, instant and unlimited, with rich formatting."""

    name = "telegram"

    def _api(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        token = self.env["TELEGRAM_BOT_TOKEN"]
        response = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                                 json=payload, timeout=TIMEOUT)
        body = {}
        try:
            body = response.json()
        except ValueError:
            pass
        if not response.ok or not body.get("ok", False):
            raise RuntimeError(body.get("description") or f"HTTP {response.status_code}")
        return body

    def _send(self, changes: list[Change], run: dict[str, Any]) -> Result:
        chat_id = self.env["TELEGRAM_CHAT_ID"]
        text = render.as_telegram_html(changes, limit=self.limit)
        # Telegram rejects messages over 4096 characters outright.
        if len(text) > 4000:
            text = text[:3900].rsplit("\n", 1)[0] + "\n\n<i>...trimmed.</i>"

        photos = [c.listing.thumbnail for c in changes[:10] if c.listing.thumbnail]
        if self.config.get("photos", True) and len(photos) >= 2:
            # A media group shows a photo grid with the caption on the first
            # item. If it fails (a URL Telegram cannot fetch), fall through to
            # the plain message rather than lose the alert.
            try:
                media = [{"type": "photo", "media": url} for url in photos]
                # Telegram caps a media-group caption at 1,024 characters.
                # Either the whole digest fits in the caption, or the photos
                # go without one and the text follows as its own message.
                fits = len(text) <= 1000
                if fits:
                    media[0]["caption"] = text
                    media[0]["parse_mode"] = "HTML"
                self._api("sendMediaGroup", {"chat_id": chat_id, "media": media})
                if not fits:
                    self._api("sendMessage", {
                        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
                        "disable_web_page_preview": True})
                count = len(changes)
                return Result(self.name, True,
                              f"{count} change{'' if count == 1 else 's'} "
                              f"with photos")
            except Exception as exc:  # noqa: BLE001
                log.info("telegram photo group failed (%s); sending text", exc)

        self._api("sendMessage", {"chat_id": chat_id, "text": text,
                                  "parse_mode": "HTML",
                                  "disable_web_page_preview": len(changes) > 1})
        return Result(self.name, True, _count(len(changes), "change"))

    def _send_text(self, subject: str, body: str) -> Result:
        self._api("sendMessage", {"chat_id": self.env["TELEGRAM_CHAT_ID"],
                                  "text": f"{subject}\n\n{body}"[:4000]})
        return Result(self.name, True, "text")

    def _verify(self) -> Result:
        me = self._api("getMe", {}).get("result", {})
        chat_id = self.env["TELEGRAM_CHAT_ID"]
        try:
            chat = self._api("getChat", {"chat_id": chat_id}).get("result", {})
        except RuntimeError as exc:
            return Result(self.name, False,
                          f"bot @{me.get('username', '?')} works, but chat id {chat_id} "
                          f"is wrong ({exc}). Message your bot once, then open "
                          f"https://api.telegram.org/bot<TOKEN>/getUpdates to read the id.")
        who = chat.get("title") or chat.get("username") or chat.get("first_name") or chat_id
        return Result(self.name, True, f"bot @{me.get('username', '?')} will message {who}")


class DiscordNotifier(Notifier):
    """Free.  Rich embeds with thumbnails; only needs a webhook URL."""

    name = "discord"

    def _post(self, payload: dict[str, Any]) -> None:
        url = self.env["DISCORD_WEBHOOK_URL"]
        response = requests.post(url, json=payload, timeout=TIMEOUT)
        if response.status_code not in (200, 204):
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")

    def _send(self, changes: list[Change], run: dict[str, Any]) -> Result:
        embeds = render.as_discord_embeds(changes, limit=self.limit)
        self._post({"content": render.headline(changes)[:1900],
                    "embeds": embeds, "allowed_mentions": {"parse": []}})
        if len(changes) > len(embeds):
            self._post({"content": f"...and {len(changes) - len(embeds)} more.",
                        "allowed_mentions": {"parse": []}})
        return Result(self.name, True, _count(len(embeds), "embed"))

    def _send_text(self, subject: str, body: str) -> Result:
        self._post({"content": f"**{subject}**\n{body}"[:1900],
                    "allowed_mentions": {"parse": []}})
        return Result(self.name, True, "text")

    def _verify(self) -> Result:
        response = requests.get(self.env["DISCORD_WEBHOOK_URL"], timeout=TIMEOUT)
        if response.status_code == 404:
            raise RuntimeError("that webhook no longer exists - create a new one")
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status_code}")
        hook = response.json()
        return Result(self.name, True,
                      f"posts to #{hook.get('name', '?')} as \"{hook.get('name', '?')}\"")


# ntfy's message size limit. A longer body arrives as a file attachment
# rather than readable text, so cut below it, leaving room for a note.
NTFY_MAX_BODY = 4096
NTFY_BODY_ROOM = 120


def _fits_ntfy(body: str) -> bytes:
    """The message as bytes ntfy will deliver as a notification.

    Measured in bytes, since the limit applies to the encoded body. Cut at a
    line boundary so the last car shown is whole.
    """
    blob = body.encode("utf-8")
    if len(blob) <= NTFY_MAX_BODY:
        return blob
    room = NTFY_MAX_BODY - NTFY_BODY_ROOM
    kept = blob[:room].decode("utf-8", "ignore")
    head, sep, _ = kept.rpartition("\n")
    kept = head if sep and len(head) > room // 2 else kept
    return (kept.rstrip()
            + "\n\n...the rest would not fit in one notification. "
              "The dashboard has all of it.").encode("utf-8")


def _utf8_header(text: str) -> str:
    """A header value in full UTF-8, as RFC 2047 encoded-words.

    HTTP headers travel as latin-1, but ntfy decodes encoded-words in any
    header. Chunks of at most 45 bytes, split on character boundaries, keep
    each encoded word within the RFC's 75-character limit. An all-ASCII value
    is returned unchanged.
    """
    import base64
    text = str(text or "")
    if text.isascii():
        return text
    words, chunk = [], b""
    for ch in text:
        piece = ch.encode("utf-8")
        if len(chunk) + len(piece) > 45:
            words.append(chunk)
            chunk = b""
        chunk += piece
    if chunk:
        words.append(chunk)
    return " ".join(f"=?UTF-8?B?{base64.b64encode(w).decode('ascii')}?="
                    for w in words)


def _ascii_header(text: str) -> str:
    """A header value folded to ASCII, which ntfy will deliver intact.

    Accents are stripped so words stay readable; any other non-ASCII
    character becomes a question mark.
    """
    import unicodedata
    folded = unicodedata.normalize("NFKD", str(text or ""))
    out = "".join(c for c in folded if not unicodedata.combining(c))
    return out.encode("ascii", "replace").decode("ascii")


class NtfyNotifier(Notifier):
    """Free push to your phone with no account at all - just a topic name.

    Anyone who knows the topic can read it, so the topic should be long and
    unguessable.
    """

    name = "ntfy"

    def _topic_url(self) -> str:
        server = str(self.config.get("server") or "https://ntfy.sh").rstrip("/")
        topic = str(self.config.get("topic") or "").strip().strip("/")
        if not topic:
            raise RuntimeError("no ntfy topic configured")
        return f"{server}/{topic}"

    def _icon(self) -> str:
        """A URL ntfy can fetch to brand the notification, or nothing.

        Defaults to the dashboard's own icon, derived from the dashboard
        address so a fork gets its own with no configuration. ntfy accepts
        PNG and JPEG and shows the icon on Android only; other clients ignore
        the header, so nothing depends on it.
        """
        override = str(self.config.get("icon") or "").strip()
        if override:
            return override
        home = str(self.settings.get("dashboard_url") or "").strip().rstrip("/")
        return f"{home}/icon-192.png" if home.startswith(("http://", "https://")) else ""

    def _post(self, title: str, body: str, *, click: str = "",
              tags: str = "car", priority: str = "", attach: str = "",
              actions: str = "") -> None:
        # No "Markdown: yes": ntfy renders Markdown only in its web app, where
        # it joins lines not separated by a blank one. Plain text reads the
        # same everywhere.
        headers = {"Title": _utf8_header(title[:200]), "Tags": tags}
        icon = self._icon()
        if icon:
            headers["Icon"] = icon
        if click:
            headers["Click"] = click
        # `Click` opens the car's dashboard card; the action opens the ad.
        if actions:
            headers["Actions"] = actions
        # The car's photo. The phone fetches it, so it costs the bot nothing
        # and is dropped silently if the host is unreachable.
        if attach:
            headers["Attach"] = attach
        priority = priority or str(self.config.get("priority") or "").strip()
        if priority and priority != "default":
            headers["Priority"] = priority
        token = (self.env.get("NTFY_TOKEN") or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        # requests encodes headers as latin-1 and raises UnicodeEncodeError on
        # anything else, which would silently lose the alert, so every header
        # is folded to ASCII.
        headers = {k: _ascii_header(v) for k, v in headers.items()}
        response = requests.post(self._topic_url(), data=_fits_ntfy(body),
                                 headers=headers, timeout=TIMEOUT)
        if not response.ok:
            # A 4xx other than 429 is a request ntfy will refuse again on
            # retry; the wording lets is_permanent_failure recognise it.
            refused = 400 <= response.status_code < 500 and response.status_code != 429
            how = "rejected the request" if refused else "failed"
            raise RuntimeError(f"HTTP {response.status_code}: ntfy {how} - "
                               f"{response.text[:200]}")

    # How urgently to alert: a car you have not seen gets `priority_new`,
    # removals and price rises alone get "low", everything else `priority`.
    _QUIET_KINDS = (Change.PRICE_RISE, Change.REMOVED)

    def _priority(self, changes: list[Change]) -> str:
        if any(c.kind in Change.NEW_TO_YOU for c in changes):
            return str(self.config.get("priority_new") or "high").strip()
        if all(c.kind in self._QUIET_KINDS for c in changes):
            return "low"
        return str(self.config.get("priority") or "default").strip()
    _TAGS = {Change.PRICE_DROP: "chart_with_downwards_trend", Change.NEW: "car",
             Change.PRICED: "label", Change.RELISTED: "arrows_counterclockwise",
             Change.QUALIFIED: "unlock",
             Change.PRICE_RISE: "chart_with_upwards_trend", Change.REMOVED: "ghost"}

    def _lead(self, changes: list[Change]) -> Change:
        """The change the notification is about: the first in render order.

        One ordering, render.in_order, decides the heading, the body and
        which cars a long digest leaves out, so all three agree.
        """
        return render.in_order(changes)[0]

    def _send(self, changes: list[Change], run: dict[str, Any]) -> Result:
        if not changes:
            return Result(self.name, True, _count(0, "change"))
        lead = self._lead(changes)
        listing = lead.listing
        # Tapping opens that car on the dashboard when there is one, else the ad.
        deep = str(self.settings.get("dashboard_url") or "").strip()
        click = (f"{deep.rstrip('/')}/#/listing/{listing.id}"
                 if deep and listing.id else (listing.url or ""))
        # The photo of the car the heading names: always for a car you have
        # not seen, otherwise only when it is the only change.
        photo = ((getattr(listing, "images", None) or [""])[0]
                 if len(changes) == 1 or lead.kind in Change.NEW_TO_YOU else "")
        self._post(render.push_title(changes),
                   render.as_push(changes, limit=self.limit),
                   click=click,
                   tags=self._TAGS.get(lead.kind, "car"),
                   priority=self._priority(changes),
                   attach=photo,
                   actions=self._actions(lead, len(changes), deep))
        return Result(self.name, True, _count(len(changes), "change"))

    def _actions(self, lead: Change, total: int, dashboard: str) -> str:
        """The buttons under the notification, as ntfy's JSON form.

        JSON rather than the comma-separated shorthand, where a comma in a
        listing URL would split one action into two broken ones. The first
        button opens the ad; a digest adds one for the dashboard feed.
        """
        buttons: list[dict[str, Any]] = []
        url = render.short_link(lead.listing).strip()
        if url.startswith(("http://", "https://")):
            buttons.append({"action": "view", "label": "Open ad",
                            "url": url, "clear": True})
        home = dashboard.rstrip("/")
        if total > 1 and home.startswith(("http://", "https://")):
            buttons.append({"action": "view",
                            "label": f"All {total} on the dashboard",
                            "url": f"{home}/#/feed", "clear": True})
        # ntfy takes at most three, and a header must survive latin-1.
        return _ascii_header(json.dumps(buttons[:3], separators=(",", ":"))) \
            if buttons else ""

    def _send_text(self, subject: str, body: str) -> Result:
        self._post(subject, body, tags="warning", priority="high")
        return Result(self.name, True, "text")

    def _verify(self) -> Result:
        url = self._topic_url()
        headers = {}
        token = (self.env.get("NTFY_TOKEN") or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = requests.get(f"{url}/json?poll=1", headers=headers, timeout=TIMEOUT)
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status_code} from {url}")
        topic = str(self.config.get("topic") or "")
        warn = ("  NOTE: anyone who guesses this topic can read your alerts - "
                "use a long random one." if len(topic) < 12 else "")
        return Result(self.name, True, f"topic {topic} is reachable.{warn}")


class SlackNotifier(Notifier):
    """Free via an incoming webhook."""

    name = "slack"

    def _post(self, payload: dict[str, Any]) -> None:
        response = requests.post(self.env["SLACK_WEBHOOK_URL"], json=payload, timeout=TIMEOUT)
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")

    # Slack's block text limit. _fit shows fewer cars rather than cutting the
    # text, so the "...and N more." line survives.
    BLOCK_LIMIT = 2900

    def _send(self, changes: list[Change], run: dict[str, Any]) -> Result:
        text = self._fit(changes)
        self._post({"text": render.headline(changes),
                    "blocks": [{"type": "section",
                                "text": {"type": "mrkdwn", "text": text}}]})
        return Result(self.name, True, _count(len(changes), "change"))

    def _fit(self, changes: list[Change]) -> str:
        """The digest, shortened by showing fewer cars rather than fewer bytes.

        render.as_markdown already knows how to say "and N more"; it just has
        to be asked for a length that fits.
        """
        limit = self.limit
        while limit > 1:
            text = render.as_markdown(changes, limit=limit)
            if len(text) <= self.BLOCK_LIMIT:
                return text
            limit -= 1
        return render.as_markdown(changes, limit=1)[: self.BLOCK_LIMIT]

    def _send_text(self, subject: str, body: str) -> Result:
        self._post({"text": f"*{subject}*\n{body}"[:2900]})
        return Result(self.name, True, "text")


class EmailNotifier(Notifier):
    """Gmail SMTP.  Free, but rate-limited, so we always send one digest."""

    name = "email"

    def _deliver(self, subject: str, text: str, html: str | None) -> None:
        user = self.env["GMAIL_USER"]
        password = self.env["GMAIL_APP_PASSWORD"].replace(" ", "")
        to = str(self.config.get("to") or "").strip() or user
        recipients = [addr.strip() for addr in to.split(",") if addr.strip()]

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr(("AutoTrader Watcher", user))
        message["To"] = ", ".join(recipients)
        message["Date"] = formatdate(localtime=True)
        message.set_content(text)
        if html and self.config.get("html", True):
            message.add_alternative(html, subtype="html")

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=TIMEOUT, context=context) as smtp:
            smtp.login(user, password)
            smtp.send_message(message, from_addr=user, to_addrs=recipients)

    def _send(self, changes: list[Change], run: dict[str, Any]) -> Result:
        limit = max(self.limit, 20)
        self._deliver(
            render.headline(changes),
            render.as_text(changes, limit=limit),
            render.as_email_html(changes, limit=limit,
                                 dashboard_url=str(self.settings.get("dashboard_url") or "")),
        )
        return Result(self.name, True, _count(len(changes), "change"))

    def _send_text(self, subject: str, body: str) -> Result:
        self._deliver(subject, body, None)
        return Result(self.name, True, "text")

    def _verify(self) -> Result:
        user = self.env["GMAIL_USER"]
        password = self.env["GMAIL_APP_PASSWORD"].replace(" ", "")
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=TIMEOUT,
                                  context=ssl.create_default_context()) as smtp:
                smtp.login(user, password)
        except smtplib.SMTPAuthenticationError as exc:
            raise RuntimeError(
                "Gmail rejected those credentials. GMAIL_APP_PASSWORD must be a "
                "16-character App Password (myaccount.google.com/apppasswords), "
                f"not your normal password. [{exc.smtp_code}]") from exc
        to = str(self.config.get("to") or "").strip() or user
        return Result(self.name, True, f"signed in as {user}, will send to {to}")


class WebhookNotifier(Notifier):
    """POST the run as JSON anywhere - Home Assistant, n8n, your own script."""

    name = "webhook"

    def _send(self, changes: list[Change], run: dict[str, Any]) -> Result:
        url = str(self.config.get("url") or "").strip()
        if not url:
            return Result(self.name, False, "no webhook url configured", skipped=True)
        response = requests.post(url, json=render.as_json_payload(changes, run),
                                 timeout=TIMEOUT)
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status_code}")
        return Result(self.name, True, _count(len(changes), "change"))

    def _send_text(self, subject: str, body: str) -> Result:
        url = str(self.config.get("url") or "").strip()
        if not url:
            return Result(self.name, False, "no webhook url configured", skipped=True)
        requests.post(url, json={"headline": subject, "message": body}, timeout=TIMEOUT)
        return Result(self.name, True, "text")

    def _verify(self) -> Result:
        url = str(self.config.get("url") or "").strip()
        if not url:
            raise RuntimeError("no webhook url configured")
        response = requests.head(url, timeout=TIMEOUT, allow_redirects=True)
        # Many endpoints only accept POST; reaching them at all is the point.
        if response.status_code >= 500:
            raise RuntimeError(f"HTTP {response.status_code}")
        return Result(self.name, True, f"{url} answered HTTP {response.status_code}")


class TwilioNotifier(Notifier):
    """SMS. Billed per message; Telegram and ntfy reach the phone for free."""

    name = "twilio"

    def _post(self, body: str) -> None:
        sid = self.env["TWILIO_SID"]
        response = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data={"From": self.env["TWILIO_FROM"], "To": self.env["TWILIO_TO"],
                  "Body": body[:render.MAX_SMS]},
            auth=(sid, self.env["TWILIO_TOKEN"]), timeout=TIMEOUT)
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")

    def _send(self, changes: list[Change], run: dict[str, Any]) -> Result:
        self._post(render.as_sms(changes))
        return Result(self.name, True, _count(len(changes), "change"))

    def _send_text(self, subject: str, body: str) -> Result:
        self._post(f"{subject}\n{body}")
        return Result(self.name, True, "text")

    def _verify(self) -> Result:
        sid = self.env["TWILIO_SID"]
        response = requests.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json",
            auth=(sid, self.env["TWILIO_TOKEN"]), timeout=TIMEOUT)
        if response.status_code == 401:
            raise RuntimeError("Twilio rejected the SID or auth token")
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status_code}")
        return Result(self.name, True,
                      f"account {response.json().get('friendly_name', sid)} "
                      f"(each message is billed)")


REGISTRY: dict[str, type[Notifier]] = {
    "telegram": TelegramNotifier,
    "discord": DiscordNotifier,
    "ntfy": NtfyNotifier,
    "slack": SlackNotifier,
    "email": EmailNotifier,
    "webhook": WebhookNotifier,
    "twilio": TwilioNotifier,
}


def build(config, env: dict[str, str] | None = None) -> list[Notifier]:
    """Instantiate every channel that is switched on and fully configured."""
    env = env if env is not None else dict(os.environ)
    settings = config.get("notifications", {}) or {}
    out: list[Notifier] = []
    for name, status in config.channel_status(env).items():
        if not status["active"]:
            continue
        cls = REGISTRY.get(name)
        if cls:
            out.append(cls(status["config"], env, settings))
    return out


def in_quiet_hours(settings: dict[str, Any], now: datetime | None = None) -> bool:
    """True if the user asked not to be disturbed right now."""
    quiet = (settings or {}).get("quiet_hours") or {}
    if not quiet.get("enabled"):
        return False
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(str(settings.get("timezone") or "UTC"))
    except Exception:  # noqa: BLE001 - a bad tz must not silence notifications
        tz = None
    # An unusable zone falls back to UTC, never the host's local clock.
    now = now or (clock.now().astimezone(tz) if tz else clock.now())

    def parse(value: Any) -> int | None:
        """HH:MM to minutes past midnight, or None if it is not a real time."""
        try:
            hours, _, mins = str(value).partition(":")
            hour, minute = int(hours), int(mins)
        except (ValueError, TypeError, AttributeError):
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour * 60 + minute

    # A half-written or invalid window never silences alerts: an unwanted
    # notification beats a missed car. Both ends must be real times.
    start = parse(quiet.get("start"))
    end = parse(quiet.get("end"))
    if start is None or end is None:
        log.warning("ignoring quiet_hours: start=%r end=%r is not a valid window",
                    quiet.get("start"), quiet.get("end"))
        return False
    minutes = now.hour * 60 + now.minute
    if start == end:
        return False
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end   # window crosses midnight


def dispatch(config, changes: list[Change], run: dict[str, Any] | None = None,
             env: dict[str, str] | None = None,
             notifiers: list[Notifier] | None = None) -> list[Result]:
    """Send one digest per active channel.  Always returns; never raises."""
    if not changes:
        return []
    channels = notifiers if notifiers is not None else build(config, env)
    if not channels:
        return [Result("none", False,
                       "no notification channel is configured - see README", skipped=True)]
    return [channel.send(changes, run) for channel in channels]


def verify_all(config, env: dict[str, str] | None = None,
               notifiers: list[Notifier] | None = None) -> list[Result]:
    """Check every active channel without sending anything."""
    channels = notifiers if notifiers is not None else build(config, env)
    return [channel.verify() for channel in channels]


def alert(config, subject: str, body: str, env: dict[str, str] | None = None,
          notifiers: list[Notifier] | None = None) -> list[Result]:
    """Send a health warning (scraper blocked, search broken) to every channel."""
    channels = notifiers if notifiers is not None else build(config, env)
    return [channel.send_text(subject, body) for channel in channels]
