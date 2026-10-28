"""Noticing a collector that is failing, and asking for it to be fixed.

A second launchd agent runs this every 15 minutes. When the collector has
failed or read nothing for long enough, it opens (or adds to) an issue in the
watch's repository, which is the report, and starts a headless Claude on this
Mac with a brief to diagnose, fix, merge and deploy. The issue carries counts
and error text only, never a search or a car: the repository is public.

It never retries more than ``MAX_PER_DAY`` times a day, and the brief forbids
getting around Facebook's limits: when Facebook is slowing the account, the
fix is to read less, not to read differently.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import settings as S

LABEL = "com.autotrader-watch.watchdog"
EMPTY_AFTER = 6       # passes in a row that read no car
FAILED_AFTER = 3      # passes in a row that threw
COOLDOWN = timedelta(hours=3)
MAX_PER_DAY = 3
ISSUE_LABEL = "collector-failure"

BRIEF = """\
The Marketplace collector on this Mac (idenair) is failing. Issue #{issue} in
{repo} has the report: {why}.

Work on this Mac only (never mac.lan). Diagnose from {log}, the collector's
memory.json beside it, and `collector/run explain --capture`. Fix the cause in
the repository at {root}, run the tests ({root}/.venv/bin/python -m pytest
tests/ -q), push a branch, open a pull request, and merge it once CI is green
(Iden allows autonomous merges). Then deploy: git pull on main, pip install
into .venv, `collector/run install` (retry once if launchctl exits 5).
Comment the result on the issue and close it when the log shows cars again.

Hard rule: never evade Facebook's rate limits or detection. If Facebook is
slowing or restricting the account (empty results, a checkpoint, a sign-out),
the answer is to read less or wait, or to tell Iden to run `collector/run
login`, never to disguise the collector. If the cause is not fixable in code,
say so on the issue and stop. Read CLAUDE.md first and follow its rules on
Actions use.
"""


def state_path() -> Path:
    return S.home() / "watchdog.json"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def problem(memory: dict) -> str:
    """What is wrong, in a sentence, or an empty string."""
    empty, failed = int(memory.get("empty_passes") or 0), int(memory.get("failed_passes") or 0)
    if failed >= FAILED_AFTER:
        err = str((memory.get("last_error") or {}).get("error") or "unknown")
        return f"{failed} passes in a row failed; the last said: {err}"
    if empty >= EMPTY_AFTER:
        return (f"{empty} passes in a row read no cars at all though Facebook "
                "had not signed the collector out")
    return ""


def due(state: dict, now: datetime) -> bool:
    runs = [datetime.fromisoformat(t) for t in state.get("runs", [])]
    runs = [t for t in runs if now - t < timedelta(days=1)]
    if len(runs) >= MAX_PER_DAY:
        return False
    return not runs or now - max(runs) >= COOLDOWN


def _gh(*args: str) -> str:
    done = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if done.returncode:
        raise RuntimeError(done.stderr.strip()[:300])
    return done.stdout.strip()


def report(repo: str, why: str) -> int:
    """Open the failure issue, or add to the one already open. Its number."""
    found = _gh("issue", "list", "-R", repo, "--label", ISSUE_LABEL, "--state", "open",
                "--json", "number", "--jq", ".[0].number")
    body = f"{why}\n\nHost: {S.Settings.load().host}. Reported {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC."
    if found:
        _gh("issue", "comment", found, "-R", repo, "--body", body)
        return int(found)
    try:
        _gh("label", "create", ISSUE_LABEL, "-R", repo, "--color", "d73a4a")
    except RuntimeError:
        pass                                     # it exists
    url = _gh("issue", "create", "-R", repo, "--label", ISSUE_LABEL,
              "--title", "Marketplace collector is failing", "--body", body)
    return int(re.search(r"/(\d+)$", url).group(1))


def wake(repo: str, issue: int, why: str) -> None:
    prompt = BRIEF.format(issue=issue, repo=repo, why=why, log=S.home() / "collector.log",
                          root=S.repo_root())
    out = open(S.home() / "watchdog.log", "ab")
    subprocess.Popen(
        ["claude", "-p", prompt, "--permission-mode", "acceptEdits",
         "--allowedTools", "Bash", "Read", "Edit", "Write", "Grep", "Glob"],
        cwd=S.repo_root(), stdout=out, stderr=out, start_new_session=True)


def check(now: datetime | None = None) -> str:
    from .cycle import load_memory
    now = now or datetime.now(timezone.utc)
    why = problem(load_memory())
    if not why:
        return "healthy"
    state = _load(state_path())
    if not due(state, now):
        return f"unhealthy, already reported: {why}"
    repo = S.Settings.load().repo
    issue = report(repo, why)
    wake(repo, issue, why)
    runs = [t for t in state.get("runs", [])
            if now - datetime.fromisoformat(t) < timedelta(days=1)]
    state["runs"] = runs + [now.isoformat()]
    state_path().write_text(json.dumps(state), encoding="utf-8")
    return f"reported on issue {issue}: {why}"


def agent() -> dict:
    run = S.repo_root() / "collector" / "run"
    return {
        "Label": LABEL,
        "ProgramArguments": [str(run), "watchdog"],
        "StartInterval": 900,
        "RunAtLoad": False,
        "ProcessType": "Background",
        "StandardOutPath": str(S.home() / "watchdog.log"),
        "StandardErrorPath": str(S.home() / "watchdog.log"),
        "WorkingDirectory": str(S.repo_root()),
        "EnvironmentVariables": {"PATH": f"{Path.home()}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"},
    }


def install() -> Path:
    path = plist_path()
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    path.write_bytes(plistlib.dumps(agent()))
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True)
    return path


def uninstall() -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    plist_path().unlink(missing_ok=True)
