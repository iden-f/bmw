"""collector/run <command>: set up, sign in, and run the Marketplace collector."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import settings as S


def cmd_setup(args) -> int:
    cfg = S.Settings.load()
    print(f"This computer will call itself '{cfg.host}' and send to {cfg.repo or '?'}.")
    if not cfg.repo:
        print("Could not tell which repository this clone came from. Clone it "
              "from GitHub, then run this again.")
        return 1
    if args.standby:
        cfg.role = "standby"
    elif args.primary:
        cfg.role = "primary"
    print(f"Role: {cfg.role}" + (" - it reads only when the primary goes quiet"
                                 if cfg.role == "standby" else ""))
    if args.secrets or not S.secret(S.TOKEN):
        print("\n1. A GitHub token. On github.com: Settings > Developer settings > "
              "Personal access tokens > Fine-grained tokens > Generate new token.\n"
              f"   Repository access: only {cfg.repo}. Permissions: Contents - "
              "Read and write. Nothing else.")
        if not S.store_secret(S.TOKEN):
            return 1
    if args.secrets or not S.secret(S.PASSPHRASE):
        print("\n2. The watch's passphrase: the one in the WATCH_PASSPHRASE "
              "repository secret, which also unlocks the dashboard.")
        if not S.store_secret(S.PASSPHRASE):
            return 1
    cfg.save()
    return cmd_check(args)


def cmd_check(args) -> int:
    """Everything the collector needs, tried in turn, said in words."""
    from .browser import profile_exists
    from .cycle import Keyring
    from .github import GitHub, GitHubError
    from autotrader import marketplace as M
    from autotrader import vault as V

    cfg = S.Settings.load()
    try:
        github = GitHub(cfg.repo, S.secret(S.TOKEN))
        print("ok  ", github.check())
        _, watch = Keyring(github, S.secret(S.PASSPHRASE)).config()
        print("ok   the passphrase opens the vault")
    except (GitHubError, V.VaultError) as exc:
        print("fix ", exc)
        return 1
    plan = M.plan(watch)
    print(f"ok   {len(plan)} search{'es' if len(plan) != 1 else ''} to read:")
    for item in plan:
        print(f"       {item['name']}: {', '.join(q['query'] for q in item['queries'])}"
              f" within {item['radius_km']} km")
    if not profile_exists():
        print("next  sign in to Facebook: collector/run login")
    return 0


def cmd_login(args) -> int:
    from .browser import login
    ok = login(S.Settings.load())
    print("Signed in: the collector can read Marketplace." if ok else
          "Not signed in. Run collector/run login again to finish.")
    return 0 if ok else 1


def cmd_once(args) -> int:
    from .cycle import _line, once
    summary = once(S.Settings.load(), force=args.now)
    print(_line(summary))
    for read in summary.get("searches") or []:
        print(f"   {'ok ' if read['ok'] else 'bad'} {read['cars']:>3} cars"
              + (f"  {read['error']}" if read.get("error") else ""))
    return 0


def cmd_daemon(args) -> int:
    from .cycle import forever
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        stream=sys.stdout)
    forever(S.Settings.load())
    return 0


def cmd_status(args) -> int:
    from . import launchd
    from .browser import profile_exists
    from .cycle import load_memory
    cfg = S.Settings.load()
    memory = load_memory()
    print(f"this computer:  {cfg.host} ({cfg.role}), sending to {cfg.repo}")
    print(f"github token:   {'in the Keychain' if S.secret(S.TOKEN) else 'MISSING - collector/run setup'}")
    print(f"passphrase:     {'in the Keychain' if S.secret(S.PASSPHRASE) else 'MISSING - collector/run setup'}")
    print(f"browser:        {'set up' if profile_exists() else 'not signed in - collector/run login'}")
    print(f"running:        {'yes' if launchd.running() else 'no - collector/run install'}")
    if memory.get("last"):
        print("last pass:      " + json.dumps(memory["last"]))
    if memory.get("last_error"):
        print("last failure:   " + json.dumps(memory["last_error"]))
    print(f"log:            {launchd.log_path()}")
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
    print("Stopped, and it will not start at login.")
    return 0


def cmd_forget(args) -> int:
    from . import launchd
    from .browser import forget_profile
    launchd.uninstall(quiet=True)
    S.forget_secrets()
    print(f"Removed the service, the Keychain items and {forget_profile()}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="collector/run", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("setup", help="store the token and passphrase, then check")
    s.add_argument("--standby", action="store_true",
                   help="this computer only reads when the primary goes quiet")
    s.add_argument("--primary", action="store_true")
    s.add_argument("--secrets", action="store_true", help="replace the stored secrets")
    s.set_defaults(func=cmd_setup)
    sub.add_parser("check", help="check the token, passphrase and searches"
                   ).set_defaults(func=cmd_check)
    sub.add_parser("login", help="sign in to Facebook in the collector's browser"
                   ).set_defaults(func=cmd_login)
    o = sub.add_parser("once", help="one pass now, and say what it read")
    o.add_argument("--now", action="store_true",
                   help="read even overnight or on standby")
    o.set_defaults(func=cmd_once)
    sub.add_parser("daemon", help="pass after pass (what the service runs)"
                   ).set_defaults(func=cmd_daemon)
    sub.add_parser("status", help="what is set up and what the last pass did"
                   ).set_defaults(func=cmd_status)
    sub.add_parser("install", help="run it now and at every login"
                   ).set_defaults(func=cmd_install)
    sub.add_parser("uninstall", help="stop it running").set_defaults(func=cmd_uninstall)
    sub.add_parser("forget", help="remove the service, secrets and browser profile"
                   ).set_defaults(func=cmd_forget)
    args = p.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
