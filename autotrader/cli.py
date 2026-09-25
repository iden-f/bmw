"""Command line: every operation, without editing a config file by hand."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from . import dashboard, notifiers, words
from .archive import prune as prune_archives
from .archive import size_report
from .config import CHANNEL_SECRETS, Config, ConfigError
from .http import Fetcher
from .listing import Listing, name_of
from .parser import parse_search_page
from .state import Change, State
from .urls import describe_search, page_url

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")


def _colour(enabled: bool) -> None:
    if not enabled:
        globals().update(GREEN="", RED="", YELLOW="", DIM="", BOLD="", RESET="")


def _ok(text: str) -> str:
    return f"{GREEN}OK{RESET} {text}"


def _bad(text: str) -> str:
    return f"{RED}!!{RESET} {text}"


def _warn(text: str) -> str:
    return f"{YELLOW}--{RESET} {text}"


# ----------------------------------------------------------------- commands


#: Defined in autotrader.words with the rest of the vocabulary.
_many = words.many


def cmd_run(args: argparse.Namespace) -> int:
    from .lock import AlreadyRunning, run_lock
    from .runner import run as run_once
    cfg = Config.load(args.config)
    state = State.load(args.state)
    try:
        with run_lock(enabled=not args.no_lock and not args.dry_run):
            report = run_once(cfg, state, dry_run=args.dry_run,
                              notify=not args.no_notify,
                              force=getattr(args, "force", False))
    except AlreadyRunning as exc:
        print(_warn(str(exc)))
        return 0        # not an error: the other run is doing the work
    print(report.summary())
    for warning in report.warnings:
        print(_warn(warning))
    for error in report.errors:
        print(_bad(error))
    for line in report.notified:
        print(f"   {line}")
    # What the photo CDN returned, with samples: the run log is the one place
    # that shows how photo fetching behaves on the runner's network.
    if report.photos:
        shots = report.photos
        print(f"   photos: {shots['kept']} kept ({shots['bytes'] / 1e6:.1f} MB), "
              f"{shots['fetched']} new, {shots['failed']} failed, "
              f"{shots['pruned']} pruned")
        for sample in shots.get("samples", []):
            detail = (f"{sample.get('status')} {sample.get('type') or '?'} "
                      f"{sample.get('bytes', 0)}B "
                      f"{sample.get('w', '?')}x{sample.get('h', '?')}")
            if sample.get("error"):
                print(f"   {DIM}  {detail} - {sample['error']}{RESET}")
            else:
                print(f"   {DIM}  {detail}{RESET}")
    if not args.dry_run:
        written = dashboard.write(cfg, state)
        if written:
            print(f"   dashboard data written to {written}")
    return 0 if report.ok else 1


def cmd_add(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    url = " ".join(args.url).strip()
    try:
        search = cfg.add_search(url, args.name or "", strict=not args.force)
    except ConfigError as exc:
        print(_bad(str(exc)))
        return 1
    cfg.save()
    summary = describe_search(search.url)
    print(_ok(f'added "{search.name}" ({search.id})'))
    if summary.chips if hasattr(summary, "chips") else summary.describe():
        print(f"   {DIM}{' | '.join(summary.describe())}{RESET}")
    for problem in summary.problems:
        print(_warn(problem))
    print(f"   {DIM}{search.url}{RESET}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    state = State.load(args.state)
    searches = cfg.searches
    if not searches:
        print("No searches yet. Add one with:")
        print("   python -m autotrader add \"<paste your autotrader.ca search link>\"")
        return 0
    for search in searches:
        health = (state.data.get("searches") or {}).get(search.id, {})
        mark = f"{GREEN}on {RESET}" if search.enabled else f"{DIM}off{RESET}"
        fails = health.get("consecutive_failures", 0)
        status = (f"{RED}{_many(fails, 'failed run')}{RESET}" if fails
                  else f"{_many(health.get('last_count', 0), 'listing')} last run")
        print(f"{mark} {BOLD}{search.name}{RESET}  {DIM}[{search.id}]{RESET}")
        print(f"     {' | '.join(describe_search(search.url).describe()) or 'no filters'}")
        own = []
        for key, value in (search.filters or {}).items():
            if value not in (None, [], ""):
                own.append(f"{key}={value}")
        for key, value in (search.notify_on or {}).items():
            own.append(f"{key}={'on' if value else 'off'}")
        if search.price_drop_min_abs is not None:
            own.append(f"drops>=${search.price_drop_min_abs:,}")
        if own:
            print(f"     {YELLOW}own rules:{RESET} {', '.join(own)}")
        print(f"     {status}   {DIM}{search.url}{RESET}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    if cfg.remove_search(args.id):
        cfg.save()
        print(_ok(f"removed {args.id}"))
        if getattr(args, "forget", False):
            return _forget(cfg, Path(args.state), yes=True)
        orphans = _orphans(cfg, Path(args.state))
        if orphans:
            print(f"   {DIM}{_many(len(orphans), 'car')} from it are still in "
                  f"state. The next check retires them; "
                  f"'forget --yes' drops them instead.{RESET}")
        return 0
    print(_bad(f"no search with id {args.id}"))
    return 1


def _orphans(cfg: Config, state_path: Path) -> list[str]:
    if not state_path.exists():
        return []
    state = State.load(state_path)
    keep = {s.id for s in cfg.searches}
    return [lid for lid, entry in state.listings.items()
            if str(entry.get("search_id") or "") not in keep
            and not entry.get("pending")]


def _forget(cfg: Config, state_path: Path, *, yes: bool) -> int:
    """Drop, or offer to drop, the cars that no search watches."""
    state = State.load(state_path)
    keep = {s.id for s in cfg.searches}
    owed = [lid for lid, entry in state.listings.items()
            if str(entry.get("search_id") or "") not in keep and entry.get("pending")]
    if not yes:
        doomed = _orphans(cfg, state_path)
        if not doomed:
            print(_ok("every car in state belongs to a search you are watching"))
            return 0
        print(f"{_many(len(doomed), 'car')} belong to no search any more:")
        for lid in doomed[:5]:
            entry = state.listings[lid]
            print(f"   {DIM}{lid}{RESET}  {entry.get('title') or 'untitled'}")
        if len(doomed) > 5:
            print(f"   {DIM}... and {len(doomed) - 5} more{RESET}")
        print(f"   {DIM}run again with --yes to forget them{RESET}")
        return 0
    dropped = state.forget_listings(keep)
    state.save()
    print(_ok(f"forgot {_many(len(dropped), 'car')} no search watches"))
    if owed:
        print(f"   {DIM}kept {_many(len(owed), 'car')} still owed an alert{RESET}")
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    return _forget(Config.load(args.config), Path(args.state), yes=args.yes)


def cmd_enable(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    for search in cfg.data.get("searches", []):
        if search.get("id") == args.id:
            search["enabled"] = not args.off
            cfg.save()
            print(_ok(f"{args.id} is now {'off' if args.off else 'on'}"))
            return 0
    print(_bad(f"no search with id {args.id}"))
    return 1


def _coerce(text: str) -> Any:
    """Turn a command-line value into the JSON type it obviously is."""
    lowered = text.strip().lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"none", "null", ""}:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    if "," in text:
        return [part.strip() for part in text.split(",") if part.strip()]
    return text


def cmd_set(args: argparse.Namespace) -> int:
    """Change one setting, globally or for a single search."""
    cfg = Config.load(args.config)
    value = _coerce(args.value)

    if args.search:
        matches = [s for s in cfg.searches
                   if s.id == args.search or s.name.lower() == args.search.lower()]
        if not matches:
            print(_bad(f"no search called {args.search!r}"))
            print(f"   {DIM}known: {', '.join(s.id for s in cfg.searches) or 'none'}{RESET}")
            return 1
        target = matches[0]
        for raw in cfg.data["searches"]:
            if raw["id"] != target.id:
                continue
            # Bare keys belong to that search's own filter block; dotted keys
            # address its other settings directly.
            if "." in args.key:
                head, _, tail = args.key.partition(".")
                raw.setdefault(head, {})[tail] = value
            else:
                raw.setdefault("filters", {})[args.key] = value
        cfg.save()
        print(_ok(f"{target.name}: {args.key} = {value!r}"))
        return 0

    before = cfg.get(args.key, "<unset>")
    cfg.set(args.key, value)
    cfg.save()
    print(_ok(f"{args.key}: {before!r} -> {value!r}"))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check the setup end to end and say exactly what is wrong."""
    cfg = Config.load(args.config)
    state = State.load(args.state)
    problems = 0
    warnings = 0

    # ---- config file -------------------------------------------------
    print(f"{BOLD}Config{RESET}")
    path = Path(args.config)
    if path.exists():
        print(_ok(f"{path} ({path.stat().st_size} bytes)"))
    else:
        print(_warn(f"{path} does not exist yet - defaults are being used"))
        warnings += 1
    for key, want in (("scraping.max_pages", int), ("scraping.delay_ms", int),
                      ("archive.mode", str), ("notifications.timezone", str)):
        value = cfg.get(key)
        if not isinstance(value, want):
            print(_bad(f"{key} should be {want.__name__}, found {value!r}"))
            problems += 1
    mode = cfg.get("archive.mode")
    if mode not in ("off", "metadata", "full"):
        print(_bad(f"archive.mode must be off/metadata/full, found {mode!r}"))
        problems += 1
    tz = str(cfg.get("notifications.timezone") or "")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        print(_warn(f"unknown time zone {tz!r} - quiet hours will use UTC"))
        warnings += 1

    # ---- searches ----------------------------------------------------
    print(f"\n{BOLD}Searches{RESET}")
    if not cfg.active_searches:
        print(_bad("no searches configured"))
        print(f'   {DIM}python -m autotrader add "<paste your autotrader.ca link>"{RESET}')
        problems += 1
    for search in cfg.active_searches:
        summary = describe_search(search.url)
        if summary.valid:
            print(_ok(f"{search.name}: {' | '.join(summary.describe()) or 'no filters'}"))
        else:
            print(_bad(f"{search.name}: {'; '.join(summary.problems)}"))
            problems += 1
        for problem in (summary.problems if summary.valid else []):
            print(_warn(f"   {problem}"))
            warnings += 1
    disabled = [s for s in cfg.searches if not s.enabled]
    if disabled:
        print(f"   {DIM}{len(disabled)} search(es) paused: "
              f"{', '.join(s.name for s in disabled)}{RESET}")

    # ---- notification channels --------------------------------------
    print(f"\n{BOLD}Notifications{RESET}")
    status = cfg.channel_status()
    active = [n for n, i in status.items() if i["active"]]
    if not active:
        print(_bad("no channel is configured - you will never be told anything"))
        print(f"   {DIM}Free options: Telegram, Discord, ntfy, Slack, email.{RESET}")
        print(f"   {DIM}The README's Alerts section has the exact steps.{RESET}")
        problems += 1
    for name, info in status.items():
        cost = "" if info["free"] else f" {YELLOW}(costs money){RESET}"
        if info["active"]:
            print(_ok(f"{info['label']}{cost}"))
        elif info["setting"] in (False, "false"):
            print(f"{DIM}-- {info['label']}: switched off{RESET}")
        else:
            print(_warn(f"{info['label']}: missing {', '.join(info['missing'])}"))

    if active and not args.offline:
        print(f"\n{BOLD}Channel reachability{RESET} {DIM}(no messages are sent){RESET}")
        for result in notifiers.verify_all(cfg):
            if result.ok and result.skipped:
                print(_warn(f"{result.channel}: {result.detail}"))
            elif result.ok:
                print(_ok(f"{result.channel}: {result.detail}"))
            else:
                print(_bad(f"{result.channel}: {result.detail}"))
                problems += 1

    # ---- stored data -------------------------------------------------
    print(f"\n{BOLD}Stored data{RESET}")
    stats = state.stats()
    print(_ok(f"{_many(stats['total'], 'listing')} tracked, {stats['active']} live, "
              f"{stats['gone']} gone"))
    if stats["median_price"]:
        print(f"   {DIM}prices ${stats['min_price']:,} - ${stats['max_price']:,} "
              f"(median ${stats['median_price']:,}){RESET}")
    if Path("state.corrupt.json").exists():
        print(_warn("state.corrupt.json exists - a previous state file was unreadable"))
        warnings += 1
    held = len(state.pending_changes())
    if held:
        print(_warn(f"{_many(held, 'alert')} waiting to be delivered"))
    # Report the last run that read the site, not the last firing: a firing
    # that stands down reads nothing, and its zero counts describe no check.
    last = state.last_check
    firing = state.last_run
    if last:
        mark = _ok if last.get("ok") else _bad
        print(mark(f"last check {last.get('at')}: {last.get('listings_seen', 0)} seen, "
                   f"{last.get('new', 0)} new, {last.get('searches_failed', 0)} failed"))
        for error in (last.get("errors") or [])[:3]:
            print(f"   {RED}{error}{RESET}")
        if firing and firing.get("at") != last.get("at"):
            what = ("stood down" if firing.get("skipped")
                    else "failed" if firing.get("ok") is False else "ran")
            print(f"   {DIM}a firing {what} at {firing.get('at')}{RESET}")
    elif firing:
        print(_warn(f"nothing has read the site yet; the last firing "
                    f"{'stood down' if firing.get('skipped') else 'failed'} "
                    f"at {firing.get('at')}"))
    else:
        print(_warn("no run recorded yet - try: python -m autotrader run --dry-run"))

    # ---- does the bookkeeping hold together? --------------------------
    print(f"\n{BOLD}Bookkeeping{RESET}")
    from . import invariants
    try:
        payload = dashboard.build_payload(cfg, state, dict(os.environ))
    except Exception:  # noqa: BLE001 - the check is more useful than the payload
        payload = None
    broken = invariants.check(cfg, state, None, payload)
    if broken:
        for violation in broken:
            print(_bad(str(violation)))
            problems += 1
        written = invariants.write(broken, cfg, state)
        if written:
            print(f"   {DIM}details written to {written}{RESET}")
    else:
        print(_ok("every car is owned by one search and accounted for"))

    # ---- disk --------------------------------------------------------
    print(f"\n{BOLD}Archive{RESET}")
    report = size_report()
    print(f"   {_many(report['folders'], 'folder')}, {report['bytes'] / 1048576:.1f} MB "
          f"({report['html_bytes'] / 1048576:.1f} MB saved pages)")
    if report["html_bytes"] > 20 * 1048576:
        print(_warn("saved pages are large; set archive.mode to 'metadata' and run: "
                    "python -m autotrader prune"))
        warnings += 1
    free = _free_disk_mb()
    if free is not None:
        line = f"   {free:,} MB free on this disk"
        print(_bad(line) if free < 50 else f"{line}")
        if free < 50:
            problems += 1

    # ---- live -------------------------------------------------------
    if args.live:
        problems += _live_check(cfg, sample=not args.no_sample)

    print()
    if problems:
        print(_bad(f"{_many(problems, 'problem')} "
                   f"{'needs' if problems == 1 else 'need'} attention"))
    elif warnings:
        print(_warn(f"usable, with {_many(warnings, 'thing')} worth a look"))
    else:
        print(_ok("everything checks out"))
    if not args.live:
        print(f"   {DIM}Add --live to fetch autotrader.ca and test the parser.{RESET}")
    return 1 if problems else 0


def _free_disk_mb() -> int | None:
    try:
        import shutil as _shutil
        return _shutil.disk_usage(".").free // 1048576
    except OSError:
        return None


def _live_check(cfg: Config, sample: bool = True) -> int:
    """Fetch each search for real and show exactly what the parser made of it."""
    print(f"\n{BOLD}Live check{RESET} {DIM}(fetching autotrader.ca){RESET}")
    scraping = cfg.get("scraping", {}) or {}
    fetcher = Fetcher(timeout=int(scraping.get("timeout_seconds", 30)),
                      retries=int(scraping.get("retries", 3)),
                      delay_ms=int(scraping.get("delay_ms", 1200)),
                      user_agent=str(scraping.get("user_agent", "auto")))
    problems = 0
    try:
        for search in cfg.active_searches:
            url = page_url(search.url, 1, int(scraping.get("results_per_page", 50)))
            print(f"\n  {BOLD}{search.name}{RESET}")
            print(f"  {DIM}{url}{RESET}")
            try:
                response = fetcher.get(url)
            except Exception as exc:  # noqa: BLE001
                print(_bad(f"  could not fetch: {exc}"))
                problems += 1
                continue

            size_kb = len(response.text) // 1024
            print(f"  HTTP {response.status} - {size_kb} KB in {response.elapsed_ms} ms")
            result = parse_search_page(response.text, url)

            print(f"  {BOLD}Strategy results{RESET}")
            for name, count in result.candidates.items():
                won = " <-- used" if name == result.strategy else ""
                mark = GREEN if count else DIM
                print(f"    {mark}{name:<16}{RESET} {count:>3} listing"
              f"{'' if count == 1 else 's'}{BOLD}{won}{RESET}")

            if not result.listings:
                print(_bad("  the page loaded but NO listings were parsed."))
                print(f"     {DIM}Either the search genuinely has no results, or every"
                      f" parser strategy has fallen behind the site.{RESET}")
                print(f"     {DIM}A real run treats this as a failure and alerts you.{RESET}")
                problems += 1
                continue

            print(_ok(f"  {_many(len(result.listings), 'listing')} via '{result.strategy}'"))
            if not sample:
                continue

            print(f"  {BOLD}Sample parse{RESET} {DIM}(eyeball these against the site){RESET}")
            for listing in result.listings[:3]:
                print(f"    {BOLD}{listing.display_title}{RESET}")
                print(f"      id         {listing.id}")
                source = (f"   {DIM}(from the {listing.price_source} page){RESET}"
                          if listing.price is not None and listing.price_source else "")
                print(f"      price      {listing.price_text}{source}")
                print(f"      odometer   {listing.mileage_text}")
                print(f"      where      {listing.location or '?'}"
                      f"{', ' + listing.province if listing.province else ''}")
                print(f"      photos     {len(listing.images)}")
                print(f"      url        {DIM}{listing.url}{RESET}")

            missing_price = sum(1 for l in result.listings if l.price is None)
            missing_km = sum(1 for l in result.listings if l.mileage_km is None)
            if missing_price:
                print(f"    {DIM}{missing_price}/{len(result.listings)} have no price "
                      f"(normal for 'call for price' listings){RESET}")
            if missing_km > len(result.listings) / 2:
                print(_warn(f"    {missing_km}/{len(result.listings)} have no odometer "
                            f"- the parser may be missing it"))

            print(f"  {DIM}requests so far: {fetcher.stats['requests']}, "
                  f"retries: {fetcher.stats['retries']}, "
                  f"blocked: {fetcher.stats['blocked']}{RESET}")
    finally:
        fetcher.close()
    return problems


def cmd_test_notify(args: argparse.Namespace) -> int:
    """Send a sample alert through every configured channel."""
    cfg = Config.load(args.config)
    channels = notifiers.build(cfg)
    if not channels:
        print(_bad("no channel is configured - nothing to test"))
        for name, spec in CHANNEL_SECRETS.items():
            tag = "free" if spec["free"] else "paid"
            print(f"   {BOLD}{spec['label']}{RESET} ({tag}): "
                  f"set {', '.join(spec['required']) or 'no secrets'}")
            print(f"      {DIM}{spec['help']}{RESET}")
        return 1

    sample = Listing(
        id="0", url="https://www.autotrader.ca/", title="Test message",
        year=2019, make="Example", model="Coupe", trim="Sport", price=41000,
        mileage_km=52000, location="Toronto", drivetrain="AWD",
        transmission="Automatic", color="Grey",
        search_name="Test", price_source="detail",
    )
    changes = [Change(Change.NEW, sample),
               Change(Change.PRICE_DROP, sample, old_price=44000, new_price=41000)]
    failures = 0
    for channel in channels:
        result = channel.send(changes, {"test": True})
        print(_ok(str(result)) if result.ok else _bad(str(result)))
        failures += 0 if result.ok else 1
    return 1 if failures else 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    state = State.load(args.state)
    path = dashboard.write(cfg, state)
    if path is None:
        print(_warn("dashboard is disabled in config (dashboard.enabled)"))
        return 0
    print(_ok(f"wrote {path} ({path.stat().st_size / 1024:.0f} KB)"))
    index = path.parent / "index.html"
    if index.exists():
        print(f"   {DIM}open {index} in a browser, or publish docs/ with GitHub Pages{RESET}")
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    from .ui import serve
    return serve(host=args.host, port=args.port, config_path=Path(args.config),
                 state_path=Path(args.state), open_browser=not args.no_browser)


# Every figure on a page shaped like an asking price. Deliberately a set, not
# a parse: a listing page also shows payments, fees and other cars' prices,
# and a containment check cannot pick the wrong one.
_DOLLARS = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+)")


def _dollar_figures(html: str) -> set[int]:
    return {int(m.replace(",", "")) for m in _DOLLARS.findall(html or "")}


def cmd_verify(args: argparse.Namespace) -> int:
    """Re-read every tracked car from the site and compare it with state.

    A quiet market and a bot that cannot see change look the same from the
    inside, so this asks the site. Every car is fetched once through the
    same rate-limited client, and the answer is given per car.
    """
    # FetchError only: a local import of Fetcher would shadow the module-level
    # name that tests patch.
    from .http import FetchError

    cfg = Config.load(args.config)
    state = State.load(args.state)
    watched = [e for e in state.listings.values()
               if e.get("status") == "active"
               and (args.hidden or not e.get("filtered"))]
    watched.sort(key=lambda e: str(e.get("search_name") or ""))
    if args.limit:
        watched = watched[: args.limit]
    if not watched:
        print(_warn("no live cars to verify"))
        return 0

    # Say what is left out as well, so the count does not read as all of
    # state: hidden and gone cars are skipped to spare the site requests.
    total = len(state.listings)
    hidden = sum(1 for e in state.listings.values()
                 if e.get("status") == "active" and e.get("filtered"))
    gone = sum(1 for e in state.listings.values()
               if e.get("status") != "active")
    print(f"Checking {_many(len(watched), 'car')} against the site.")
    if not args.hidden and (hidden or gone):
        left_out = []
        if hidden:
            left_out.append(f"{hidden} hidden by a rule (--hidden includes them)")
        if gone:
            left_out.append(f"{gone} already gone")
        print(f"  Not checked: {', '.join(left_out)}. "
              f"State holds {_many(total, 'row')} in all.")
    print()
    scraping = cfg.get("scraping", {}) or {}
    fetcher = Fetcher(
        timeout=int(scraping.get("timeout_seconds", 30)),
        retries=int(scraping.get("retries", 3)),
        delay_ms=int(scraping.get("delay_ms", 1200)),
        user_agent=str(scraping.get("user_agent", "auto")),
        # One request per car plus some headroom, not a full check's budget.
        budget=len(watched) + 10)
    agreed = moved = vanished = unreadable = 0
    try:
        for entry in watched:
            name = name_of(entry)[:46]
            url = entry.get("url") or ""
            if not url:
                print(f"  {YELLOW}?{RESET} {name:46} no link recorded")
                unreadable += 1
                continue
            try:
                # A Response, not markup: the parsers below take its .text.
                response = fetcher.get(url)
            except FetchError as exc:
                # A 404 or a 410 is the site saying the car is gone, which is
                # an answer rather than a failure.
                status = getattr(exc, "status", None)
                if status in (404, 410):
                    print(f"  {RED}-{RESET} {name:46} gone from the site "
                          f"(HTTP {status}); the bot still lists it")
                    vanished += 1
                else:
                    print(f"  {YELLOW}?{RESET} {name:46} could not read: {exc}")
                    unreadable += 1
                continue

            from .enrich import detail_from_html
            from .parser import looks_like_no_results
            html = response.text
            fresh = detail_from_html(html, str(entry.get("id") or ""), url)
            if fresh is None:
                if looks_like_no_results(html):
                    print(f"  {RED}-{RESET} {name:46} the page says it is gone")
                    vanished += 1
                else:
                    print(f"  {YELLOW}?{RESET} {name:46} page did not parse")
                    unreadable += 1
                continue

            held, live_price = entry.get("price"), fresh.price
            if live_price is None:
                # The structured data may omit the price while the page
                # still shows it, so check whether the held price appears
                # anywhere on the page (see _DOLLARS).
                figures = _dollar_figures(html)
                if held is not None and held in figures:
                    agreed += 1
                    if args.verbose:
                        print(f"  {GREEN}={RESET} {name:46} ${held:,} "
                              f"(on the page, not in its data block)")
                elif figures:
                    shown = ", ".join(f"${f:,}" for f in sorted(figures)[:4])
                    print(f"  {YELLOW}~{RESET} {name:46} the bot holds "
                          f"{held and f'${held:,}' or 'no price'}, and the "
                          f"page does not show it. On the page: {shown}")
                    moved += 1
                else:
                    print(f"  {YELLOW}?{RESET} {name:46} no price anywhere on "
                          f"the page (the bot holds "
                          f"{held and f'${held:,}' or 'none'})")
                    unreadable += 1
            elif held == live_price:
                agreed += 1
                if args.verbose:
                    print(f"  {GREEN}={RESET} {name:46} ${live_price:,}")
            else:
                moved += 1
                delta = (live_price - held) if held else None
                print(f"  {YELLOW}~{RESET} {name:46} bot says "
                      f"{held and f'${held:,}' or 'no price'}, site says "
                      f"${live_price:,}"
                      + (f" ({delta:+,})" if delta else ""))
    finally:
        fetcher.close()

    print(f"\n{_many(agreed, 'car')} agree, {moved} differ, "
          f"{vanished} gone, {unreadable} unreadable "
          f"({fetcher.spent} requests).")
    if moved or vanished:
        print(_warn("the next check should pick these up; run it and look again"))
    # Disagreement is information, not failure. Only being unable to read the
    # site at all is a fault worth an exit code.
    return 1 if unreadable and not (agreed or moved) else 0


def cmd_prune(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    conf = dict(cfg.get("archive", {}) or {})

    if args.keep_last is not None:
        conf["keep_last"] = args.keep_last
    if args.keep_days is not None:
        conf["keep_days"] = args.keep_days
    removed = prune_archives(conf, dry_run=args.dry_run)
    verb = "would remove" if args.dry_run else "removed"
    print(_ok(f"{verb} {len(removed)} archive folder" + ("" if len(removed) == 1 else "s")))
    if removed[:10]:
        print(f"   {DIM}{', '.join(removed[:10])}{'...' if len(removed) > 10 else ''}{RESET}")
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    """Save what the live site is serving right now.

    The bot captures a page by itself when a parse fails, but a strategy that
    scores zero while another carries the run does not trigger that. This
    captures the evidence on demand.
    """
    from . import diagnose
    from .http import Fetcher
    from .urls import page_url

    cfg = Config.load(args.config)
    searches = [s for s in cfg.active_searches
                if not args.search or args.search in (s.id, s.name)]
    if not searches:
        print(_bad("no matching enabled search"))
        return 2

    scraping = cfg.get("scraping", {}) or {}
    fetcher = Fetcher(
        timeout=int(scraping.get("timeout_seconds", 30) or 30),
        retries=int(scraping.get("retries", 3) or 0),
        delay_ms=int(scraping.get("delay_ms", 1200) or 0),
        user_agent=str(scraping.get("user_agent", "auto")),
        budget=int(scraping.get("request_budget", 250) or 0),
    )

    failed = 0
    for search in searches:
        url = page_url(search.url, args.page,
                       int(scraping.get("results_per_page", 50) or 50))
        try:
            response = fetcher.get(url)
        except Exception as exc:  # noqa: BLE001 - report, do not traceback
            print(_bad(f"{search.name}: {exc}"))
            failed += 1
            continue
        data = diagnose.capture(response.url, response.text, response.status,
                                response.elapsed_ms, search.name)
        slug = search.id if args.page == 1 else f"{search.id}-p{args.page}"
        written = diagnose.write(data, slug)
        print(_ok(f"{search.name}: {_many(data['listings_found'], 'listing')}, "
                  f"strategy {data['strategy_used']}, scores {data['strategy_scores']}"))
        print(f"   {DIM}{written}{RESET}")
        if args.raw:
            raw = diagnose.write_raw(response.text, slug)
            if raw:
                print(f"   {DIM}{raw} ({raw.stat().st_size:,} bytes gzipped){RESET}")
    return 1 if failed else 0


def cmd_events(args: argparse.Namespace) -> int:
    """Record the first time each kind of market event happened.

    It reads only stored state and writes a ledger. It never scrapes, so it
    cannot hold up or wedge a check.
    """
    from . import events

    cfg = Config.load(args.config)
    state = State.load(args.state)
    record = events.update(state)
    first = record.get("first", {})

    # A watcher cannot report its own absence, so this job, on its own
    # schedule, raises the alarm instead.
    quiet = events.silence(cfg, state, record)
    if quiet:
        print(_bad(f"no successful check for {_many(quiet['hours'], 'hour')} "
                   f"(since {quiet['since']})"))
        if args.notify:
            results = notifiers.alert(cfg, quiet["subject"], quiet["body"],
                                      dict(os.environ))
            for result in results:
                print(f"   {result}")
            if any(r.ok for r in results):
                record["silence_reported"] = quiet["since"]
                events.save(record)
    elif state.last_check:
        # last_check, not last_run: a firing that stands down is not a check.
        print(_ok(f"last successful check {state.last_check.get('at')}"))

    # Running is not the same as covering the market: a watcher that gets a
    # fraction of its scheduled checks is never silent but still misses most.
    thin = events.thin_coverage(cfg, state, record)
    if thin:
        print(_bad(
            f"only {thin['pct']}% of the expected checks happened"
            + (f" - {thin['slots_covered']} of {thin['expected']} slots in "
               f"{_many(round(thin['window_hours']), 'hour')}"
               if thin.get("window_hours") else "")))
        if args.notify:
            results = notifiers.alert(cfg, thin["subject"], thin["body"],
                                      dict(os.environ))
            for result in results:
                print(f"   {result}")
            if any(r.ok for r in results):
                record["coverage_reported"] = thin["at"]
                events.save(record)

    for kind, label in events.KINDS.items():
        seen = first.get(kind)
        if seen:
            fresh = " (new)" if kind in record.get("new_kinds", []) else ""
            print(_ok(f"{label}{fresh}: {str(seen['at'])[:19]} - "
                      f"{seen.get('detail') or ''}"))
            print(f"   {DIM}{seen.get('title', '')[:70]}{RESET}")
            print(f"   {DIM}{seen.get('delivered', '')}{RESET}")
        else:
            print(f"-- {label}: still waiting")
    print(f"\n   {DIM}{len(first)} of {len(events.KINDS)} seen; "
          f"ledger written to {events.LEDGER_PATH}{RESET}")
    return 0


def cmd_control(args: argparse.Namespace) -> int:
    """Apply changes sent from the dashboard.

    Each argument is a file, or a directory of them. In a directory only
    sealed ``.enc`` files count: anything else there is refused, because the
    repository is public and a change must prove it came from someone who
    holds the key. A sealed change carries a one-time id and the time it was
    made, so an old one cannot be replayed. Every outcome is kept in state for
    the dashboard to show. Exits non-zero if any change was refused.
    """
    from . import control
    from . import vault as V

    files: list[tuple[Path, bool]] = []
    for item in args.files:
        path = Path(item)
        if path.is_dir():
            files.extend((p, True) for p in sorted(path.iterdir()) if p.is_file())
        elif path.is_file():
            files.append((path, False))
    if not files:
        print("no changes waiting")
        return 0

    cfg = Config.load(args.config)
    state = State.load(args.state)
    key = None
    refused = applied = 0
    for path, from_dir in files:
        try:
            body = path.read_text(encoding="utf-8")
            if path.suffix == ".enc":
                if key is None:
                    key = V.Vault.unlock(Path(".")).key
                sealed = V.unseal(key, base64.b64decode(body.strip()), "control")
                body = _open_envelope(state, sealed)
            elif from_dir:
                raise control.Rejected(
                    "only sealed changes sent from the dashboard are accepted here")
            outcome = control.apply(cfg, state, control.parse(body))
            text, ok = outcome.comment(), bool(outcome.changed)
        except (control.Rejected, V.VaultError, ValueError, OSError) as exc:
            text, ok = str(exc), False
        state.record_change(ok=ok, text=text)
        print(text)
        applied += ok
        refused += not ok
    if not args.dry_run:
        cfg.save()
        state.save()
    print(f"{_many(applied, 'change')} applied, {refused} refused")
    return 1 if refused else 0


CHANGE_MAX_AGE_DAYS = 14


def _open_envelope(state: State, sealed: bytes) -> str:
    """The instructions inside a sealed change, once it is known to be new.

    The page wraps them as {"id", "at", "changes"}. A change already applied,
    or older than CHANGE_MAX_AGE_DAYS, is refused: the sealed file stays in
    the repository's public history and could be committed again.
    """
    from datetime import timedelta
    from . import clock, control

    try:
        envelope = json.loads(sealed.decode("utf-8"))
    except ValueError as exc:
        raise control.Rejected("the sealed change is not valid JSON") from exc
    if not isinstance(envelope, dict) or not isinstance(envelope.get("changes"), list):
        raise control.Rejected("the sealed change was not made by the dashboard")
    change_id = str(envelope.get("id") or "")
    if not re.fullmatch(r"[0-9a-f]{16,64}", change_id):
        raise control.Rejected("the sealed change has no id")
    seen = state.data.setdefault("change_ids", [])
    if change_id in seen:
        raise control.Rejected("this change was already applied once")
    made = clock.parse(envelope.get("at"))
    if made is None:
        raise control.Rejected("the sealed change has no time")
    now = clock.now()
    if not now - timedelta(days=CHANGE_MAX_AGE_DAYS) <= made <= now + timedelta(days=1):
        raise control.Rejected(f"the sealed change is older than "
                               f"{CHANGE_MAX_AGE_DAYS} days, or from the future")
    seen.append(change_id)
    del seen[:-500]
    return json.dumps(envelope["changes"])


def cmd_vault(args: argparse.Namespace) -> int:
    """Private mode: move the watch's data in and out of the encrypted vault.

    Prints nothing personal - this runs in a public Actions log.
    """
    from . import vault as V

    root = Path(".")
    base_file = Path(".git") / "autotrader-vault-base"
    try:
        if args.action == "pull":
            commit = V.pull_branch(root, V.VAULT_BRANCH, root / V.VAULT_DIR)
            if base_file.parent.is_dir():
                base_file.write_text(commit or "", encoding="utf-8")
            print("vault: fetched" if commit else "vault: none yet")
            return 0

        if args.action == "paths":
            for path in V.private_paths():
                print(path)
            return 0

        if args.action == "push":
            lease = None
            if args.lease:
                lease = base_file.read_text(encoding="utf-8").strip() \
                    if base_file.is_file() else ""
            V.push_branch(root, V.VAULT_BRANCH, root / V.VAULT_DIR, "Vault", lease=lease)
            print("vault: saved")
            return 0

        if args.action == "publish":
            V.push_branch(root, V.SITE_BRANCH, Path(args.dir), "Publish")
            print("site: published")
            return 0

        created = not V.Vault.exists(root)
        vault = V.Vault.unlock(root, create=(args.action == "open"))

        if args.action == "open":
            if created:
                print("vault: created")
            else:
                opened = vault.open_all(include_log=args.log)
                print(f"vault: opened {_many(sum(opened.values()), 'file')}"
                      + ("; re-encrypted under the new passphrase" if vault.rekeyed else ""))
            # Moving in from the clear, or changing the passphrase: whoever
            # could read the old data may know the ntfy topic, so it moves.
            if (created or vault.rekeyed) and Path(args.config).is_file():
                from . import provision
                cfg = Config.load(args.config)
                if provision.rotate_ntfy_topic(cfg):
                    cfg.save()
                    print("ntfy: moved to a new topic")
            return 0

        if args.action == "seal":
            changed = vault.seal_all()
            page = vault.page_changed(root / "docs" / "data.json")
            print(f"vault: {_many(changed, 'file')} changed" + ("; page changed" if page else ""))
            out = os.environ.get("GITHUB_OUTPUT")
            if out:
                with open(out, "a", encoding="utf-8") as fh:
                    fh.write(f"page={'1' if page else ''}\n")
            return 0

        if args.action == "site":
            from .dashboard import find_secrets
            data = root / "docs" / "data.json"
            if data.is_file():
                leaks = find_secrets(json.loads(data.read_text(encoding="utf-8")))
                if leaks:
                    print(_bad("refusing to publish: credential-shaped data in the payload"))
                    return 1
            counts = vault.publish(root / "docs", Path(args.dir))
            (Path(args.dir) / ".nojekyll").touch()
            print(f"site: built, {_many(counts['photos'], 'photo')}")
            return 0
    except V.VaultError as exc:
        print(_bad(f"vault: {exc}"), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - see below
        # A traceback would name the file it failed on (a photo named for its
        # listing, a capture named for its search) in a public log, so only
        # the kind of failure is printed.
        print(_bad(f"vault {args.action}: failed ({type(exc).__name__})"),
              file=sys.stderr)
        return 2
    return 2


def cmd_weekly(args: argparse.Namespace) -> int:
    """Summarise what the market did this week, rather than the bot.

    Like `events`, it reads only stored state, so it cannot hold up or wedge
    a check.
    """
    from . import insight

    cfg = Config.load(args.config)
    state = State.load(args.state)
    summary = insight.weekly(state.listings.values(),
                             state.data.get("runs") or [], days=args.days)
    text = insight.weekly_text(summary)
    print(text)
    if args.notify:
        results = notifiers.alert(
            cfg, f"AutoTrader: your last {args.days} days", text, dict(os.environ))
        for result in results:
            print(f"   {result}")
        return 0 if any(r.ok for r in results) else 1
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Get from nothing to working, with or without a human present."""
    from . import provision

    cfg = Config.load(args.config)

    if args.new_topic:
        topic = provision.generate_topic()
        cfg.set("notifications.channels.ntfy.topic", topic)
        cfg.set("notifications.channels.ntfy.enabled", True)
        cfg.save()
        print(_ok(f"new ntfy topic: {topic}"))
        print(f"   subscribe at {BOLD}{provision.subscribe_url(cfg)}{RESET}")
        return 0

    interactive = not args.non_interactive and sys.stdin.isatty()

    if interactive:
        print(f"{BOLD}AutoTrader watcher setup{RESET}\n")
        print("Open autotrader.ca, run the search you want to watch, then copy the")
        print("address bar and paste it here.\n")
        while True:
            try:
                url = input("Search link (blank when done): ").strip()
            except EOFError:
                break
            if not url:
                break
            try:
                search = cfg.add_search(url)
            except ConfigError as exc:
                print(_bad(str(exc)))
                continue
            summary = describe_search(search.url)
            print(_ok(f'watching "{search.name}" - '
                      f'{" | ".join(summary.describe()) or "no filters"}'))
            name = input(f"   Name it [{search.name}]: ").strip()
            if name:
                for raw in cfg.data["searches"]:
                    if raw["id"] == search.id:
                        raw["name"] = name
        cfg.save()

    for url in (args.add or []):
        try:
            search = cfg.add_search(url)
            print(_ok(f'watching "{search.name}"'))
        except ConfigError as exc:
            print(_warn(str(exc)))
    if args.add:
        cfg.save()

    # What the workflow runs unattended: make sure something can reach the
    # owner, and that alerts link to the right dashboard.
    result = provision.bootstrap(cfg, dict(os.environ))
    for step in result["steps"]:
        mark = _ok if step.get("changed") else _warn
        # .get, not []: setup runs before every check, so a step that names
        # its reason differently must not raise KeyError and stop the bot.
        why = step.get("reason") or step.get("detail") or "done"
        print(mark(f"{step.get('step', '?')}: {why}"))

    # Only to a person at a terminal: the topic is as good as a password.
    if result["subscribe_url"] and interactive:
        print()
        print(f"{BOLD}Your alerts go here:{RESET}")
        print(f"   {BOLD}{result['subscribe_url']}{RESET}")
        print(f"   {DIM}Open it in a browser, or subscribe to it in the ntfy app.{RESET}")

    print()
    if not cfg.active_searches:
        print(_warn("no searches yet - add one with:"))
        print(f'   {DIM}python -m autotrader add "<paste an autotrader.ca link>"{RESET}')
    else:
        print(_ok(f"{len(cfg.active_searches)} search(es) being watched"))
    print(f"   {DIM}Check everything with: python -m autotrader doctor{RESET}")
    return 0


# ----------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m autotrader",
        description="Watch AutoTrader.ca searches and get told about new listings.",
    )
    parser.add_argument("--config", default=os.getenv("AUTOTRADER_CONFIG", "config.json"))
    parser.add_argument("--state", default=os.getenv("AUTOTRADER_STATE", "state.json"))
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--no-colour", action="store_true")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("run", help="check every search once")
    p.add_argument("--force", action="store_true",
                   help="check even if one just ran (the schedule fired twice)")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would happen, change nothing, send nothing")
    p.add_argument("--no-notify", action="store_true", help="update state but stay silent")
    p.add_argument("--no-lock", action="store_true",
                   help="skip the single-run lock (not recommended)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("add", help="watch a pasted AutoTrader search link")
    p.add_argument("url", nargs="+")
    p.add_argument("--name", default="")
    p.add_argument("--force", action="store_true", help="accept a link that fails validation")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("list", help="show watched searches")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("set", help="change a setting, globally or per search")
    p.add_argument("key", help="e.g. filters.max_price, or max_price with --search")
    p.add_argument("value")
    p.add_argument("--search", help="apply to this search only (id or name)")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("remove", help="stop watching a search")
    p.add_argument("id")
    p.add_argument("--forget", action="store_true",
                   help="also drop its cars from state, instead of retiring them")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("forget", help="drop cars no search is watching any more")
    p.add_argument("--yes", action="store_true", help="actually drop them")
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser("enable", help="turn a search on or off")
    p.add_argument("id")
    p.add_argument("--off", action="store_true")
    p.set_defaults(func=cmd_enable)

    p = sub.add_parser("doctor", help="check the whole setup and explain problems")
    p.add_argument("--live", action="store_true",
                   help="also fetch autotrader.ca and show what the parser made of it")
    p.add_argument("--offline", action="store_true",
                   help="skip the channel reachability checks")
    p.add_argument("--no-sample", action="store_true",
                   help="with --live, skip the sample listing dump")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("test-notify", help="send a sample alert to every channel")
    p.set_defaults(func=cmd_test_notify)

    p = sub.add_parser("dashboard", help="regenerate docs/data.json")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("ui", help="open the settings and dashboard UI in a browser")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("verify",
                       help="re-read every tracked car from the site and "
                            "compare it with what the bot believes")
    p.add_argument("--limit", type=int, default=0,
                   help="only check this many (0 = all)")
    p.add_argument("--hidden", action="store_true",
                   help="include cars your rules hide")
    p.add_argument("--verbose", action="store_true",
                   help="print the cars that agree as well")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("prune", help="delete old archive folders")
    p.add_argument("--keep-last", type=int)
    p.add_argument("--keep-days", type=int)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("capture", help="save what the live search page looks like now")
    p.add_argument("--search", help="only this search id or name")
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--raw", action="store_true",
                   help="also save the page itself, gzipped, for fixture work")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("events", help="record the first real market event of each kind")
    p.add_argument("--notify", action="store_true",
                   help="also raise the alarm if the bot has gone quiet")
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("weekly", help="what the market did over the last week")
    p.add_argument("--notify", action="store_true",
                   help="send the summary to your channels")
    p.add_argument("--days", type=int, default=7,
                   help="how many days to summarise (default 7)")
    p.set_defaults(func=cmd_weekly)

    p = sub.add_parser("control", help="apply changes sent from the dashboard")
    p.add_argument("files", nargs="+", help="change files, or a directory of them")
    p.add_argument("--dry-run", action="store_true",
                   help="say what would change without writing anything")
    p.set_defaults(func=cmd_control)

    p = sub.add_parser("vault", help="private mode: the encrypted copy of the data")
    p.add_argument("action", choices=("pull", "open", "seal", "site", "push",
                                      "publish", "paths"))
    p.add_argument("dir", nargs="?", default="site",
                   help="for site and publish: the directory holding the site")
    p.add_argument("--log", action="store_true",
                   help="with open: also decrypt the last run's log")
    p.add_argument("--lease", action="store_true",
                   help="with push: refuse if the vault changed since pull")
    p.set_defaults(func=cmd_vault)

    p = sub.add_parser("setup", help="get from nothing to working")
    p.add_argument("--non-interactive", action="store_true",
                   help="never prompt; take sane defaults (what CI runs)")
    p.add_argument("--add", action="append", metavar="URL",
                   help="also watch this search link (repeatable)")
    p.add_argument("--new-topic", action="store_true",
                   help="generate a fresh ntfy topic and stop")
    p.set_defaults(func=cmd_setup)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _colour(sys.stdout.isatty() and not args.no_colour and not os.getenv("NO_COLOR"))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s" if args.verbose else "%(message)s",
        stream=sys.stderr,
    )
    if not getattr(args, "func", None):
        # Bare invocation is the common case in CI: just do a run.
        args = parser.parse_args((argv or []) + ["run"])
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(_bad(str(exc)), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
