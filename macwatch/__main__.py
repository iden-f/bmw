"""macwatch/run <command>: set up, run and inspect the Mac-hosted watcher."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from pathlib import Path

from . import VERSION
from . import settings as S


def _db():
    from .db import Database
    S.ensure_dirs()
    return Database(S.db_path())


def cmd_setup(args) -> int:
    """Create the settings, pick the trial topic, import the bot's state."""
    from .vaultsync import VaultConfig
    S.ensure_dirs()
    settings = S.Settings.load()
    settings.ensure_token()
    vault = VaultConfig(settings)
    try:
        cfg = vault.refresh(force=True)
    except Exception as exc:  # noqa: BLE001
        print(f"fix  could not read the vault: {exc}")
        print("     The collector's token and passphrase must be in the Keychain "
              "(collector/run setup).")
        return 1
    print(f"ok   the vault opens: {len(cfg.active_searches)} active search(es)")
    for s in cfg.active_searches:
        print(f"       {s.name}")
    if args.topic:
        settings.ntfy_topic = args.topic
    elif not settings.ntfy_topic:
        base = str(cfg.get("notifications.channels.ntfy.topic") or "").strip()
        if base:
            settings.ntfy_topic = base + S.TRIAL_SUFFIX
        else:
            import secrets
            settings.ntfy_topic = "autotrader-mac-" + secrets.token_urlsafe(12)
    if args.app_url:
        settings.app_url = args.app_url
    settings.save()
    print(f"ok   alerts from this Mac go to ntfy topic: {settings.ntfy_topic}")
    print("     Subscribe to it in the ntfy app, beside the watch's own topic.")
    db = _db()
    from .ingest import sync_collections
    sync_collections(db, cfg)
    if not db.get("legacy_import") and not args.no_import:
        from .legacy import import_state
        try:
            state = vault.legacy_state()
            counts = import_state(db, state, settings)
            print(f"ok   imported the GitHub watcher's records: {counts['listings']} cars, "
                  f"{counts['price_events']} price points, {counts['marks']} marks")
        except Exception as exc:  # noqa: BLE001
            print(f"warn could not import the bot's state ({exc}); the first pass "
                  "records what is for sale without announcing it")
    print(f"ok   settings in {settings.path}")
    print(f"     app token: {settings.api_token}")
    print("next macwatch/run once        one pass now")
    print("     macwatch/run install     run it from now on")
    print("     macwatch/run tailscale   the command that puts it on your tailnet")
    return 0


def cmd_once(args) -> int:
    from .service import Service
    from .vaultsync import VaultConfig
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                        stream=sys.stdout)
    settings = S.Settings.load()
    db = _db()
    service = Service(settings, db, VaultConfig(settings))
    sites = ["autotrader", "marketplace"] if args.site == "all" else [args.site]
    for site in sites:
        if not service.enabled(site):
            print(f"{site}: switched off in settings")
            continue
        report = service.run_site(site, trigger="manual", wait=True)
        print(report.line() if report else f"{site}: crashed (see log)")
    from . import media, outbox
    print("outbox:", outbox.deliver(db, settings))
    print("photos:", media.capture(db, settings))
    return 0


def cmd_serve(args) -> int:
    from .api import create_app
    from .service import Service
    from .vaultsync import VaultConfig
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                        stream=sys.stdout)
    settings = S.Settings.load()
    if not settings.api_token:
        settings.ensure_token()
        settings.save()
    db = _db()
    service = Service(settings, db, VaultConfig(settings))
    app = create_app(db, settings, service)
    worker = threading.Thread(target=service.serve_forever, name="macwatch-scheduler",
                              daemon=True)
    worker.start()
    import uvicorn
    try:
        uvicorn.run(app, host=settings.api_host, port=int(settings.api_port),
                    log_level="warning", proxy_headers=True, forwarded_allow_ips="127.0.0.1")
    finally:
        service.stop()
    return 0


def cmd_status(args) -> int:
    from . import launchd
    settings = S.Settings.load()
    db = _db()
    print(f"macwatch {VERSION}")
    print(f"data:        {S.home()}")
    print(f"running:     {'yes' if launchd.running() else 'no - macwatch/run install'}")
    print(f"api:         http://{settings.api_host}:{settings.api_port}/  token {settings.api_token or '(none: run setup)'}")
    print(f"ntfy topic:  {settings.ntfy_topic or '(none)'}   label {settings.ntfy_label!r}")
    print(f"cadence:     AutoTrader every {settings.interval_minutes} min, Marketplace every "
          f"{settings.marketplace_interval_minutes} min, quiet {settings.quiet_start}-"
          f"{settings.quiet_end} every {settings.quiet_interval_minutes} min ({settings.timezone})")
    for site in ("autotrader", "marketplace"):
        last = db.get(f"last:{site}") or {}
        print(f"{site:12} last: {last.get('at', 'never')} {'ok' if last.get('ok') else ''} "
              f"{last.get('line', '')[:160]}")
        print(f"{'':12} next due: {db.get(f'next_due:{site}') or 'now'}")
    print("archive:    ", json.dumps(db.counts()))
    legacy = db.get("legacy_import")
    if legacy:
        print("imported:   ", json.dumps(legacy))
    print(f"log:         {launchd.log_path()}")
    return 0


def cmd_install(args) -> int:
    from . import launchd
    path = launchd.install()
    print(f"Installed {path}. It runs now, at every login, and restarts if it stops.")
    print(f"Its log is {launchd.log_path()}")
    return 0


def cmd_uninstall(args) -> int:
    from . import launchd
    launchd.uninstall()
    print("Stopped, and it will not start at login. Its data is untouched.")
    return 0


def cmd_test_alert(args) -> int:
    from . import outbox
    settings = S.Settings.load()
    if not settings.ntfy_topic:
        print("no ntfy topic: run macwatch/run setup")
        return 1
    try:
        pid = outbox.send_text(settings, "Test from the Mac watcher",
                               "If you can read this, the trial topic works.",
                               tags="white_check_mark", priority="default")
    except Exception as exc:  # noqa: BLE001
        print(f"not sent: {exc}")
        return 1
    print(f"sent to {settings.ntfy_topic} (id {pid or '?'})")
    return 0


def cmd_import_legacy(args) -> int:
    from .ingest import sync_collections
    from .legacy import import_state
    from .vaultsync import VaultConfig
    settings = S.Settings.load()
    db = _db()
    vault = VaultConfig(settings)
    cfg = vault.refresh(force=True)
    sync_collections(db, cfg)
    if args.file:
        state = json.loads(Path(args.file).read_text(encoding="utf-8"))
    else:
        state = vault.legacy_state()
    print(json.dumps(import_state(db, state, settings)))
    return 0


def cmd_backup(args) -> int:
    from autotrader import clock
    db = _db()
    target = S.backups_dir() / f"macwatch-{clock.now().strftime('%Y%m%d-%H%M%S')}.sqlite3"
    db.backup(target)
    # Keep the last fourteen.
    kept = sorted(S.backups_dir().glob("macwatch-*.sqlite3"))
    for old in kept[:-14]:
        old.unlink()
    print(f"backup written: {target} ({target.stat().st_size} bytes)")
    if args.verify:
        from .db import Database
        copy = Database(target)
        print("restore check:", json.dumps(copy.counts()))
    return 0


def cmd_tailscale(args) -> int:
    settings = S.Settings.load()
    print("Run this once on the Mac to put the app on your tailnet over HTTPS "
          "(private to your devices; this is Serve, not Funnel):")
    print(f"  tailscale serve --bg --https=443 http://127.0.0.1:{settings.api_port}")
    print("Then on the phone (on Tailscale) open https://<this-mac>.<tailnet>.ts.net/login?token="
          + (settings.api_token or "<token>"))
    print("To stop sharing it: tailscale serve reset")
    return 0


def cmd_token(args) -> int:
    settings = S.Settings.load()
    if args.rotate or not settings.api_token:
        settings.api_token = ""
        settings.ensure_token()
        settings.save()
    print(settings.api_token)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="macwatch/run", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("setup", help="create settings, pick the trial topic, import the bot's state")
    s.add_argument("--topic", default="", help="ntfy topic to use instead of <watch topic>-mac-trial")
    s.add_argument("--app-url", default="", help="the app's address, for links in alerts")
    s.add_argument("--no-import", action="store_true")
    s.set_defaults(func=cmd_setup)
    o = sub.add_parser("once", help="one pass now")
    o.add_argument("--site", choices=["all", "autotrader", "marketplace"], default="all")
    o.set_defaults(func=cmd_once)
    sub.add_parser("serve", help="the service: scheduler, workers and API").set_defaults(func=cmd_serve)
    sub.add_parser("status", help="what is set up and what the last passes did").set_defaults(func=cmd_status)
    sub.add_parser("install", help="run it now and at every login").set_defaults(func=cmd_install)
    sub.add_parser("uninstall", help="stop it running").set_defaults(func=cmd_uninstall)
    sub.add_parser("test-alert", help="send one test alert to the trial topic").set_defaults(func=cmd_test_alert)
    i = sub.add_parser("import-legacy", help="import the GitHub watcher's records again")
    i.add_argument("--file", default="", help="a state.json instead of the vault's")
    i.set_defaults(func=cmd_import_legacy)
    b = sub.add_parser("backup", help="a consistent copy of the database into backups/")
    b.add_argument("--verify", action="store_true", help="open the copy and count its rows")
    b.set_defaults(func=cmd_backup)
    sub.add_parser("tailscale", help="the command that shares the app on your tailnet").set_defaults(func=cmd_tailscale)
    t = sub.add_parser("token", help="print the app token")
    t.add_argument("--rotate", action="store_true")
    t.set_defaults(func=cmd_token)
    args = p.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
