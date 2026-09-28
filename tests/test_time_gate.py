"""scripts/time-gate.sh: the dates and zones it promises are the ones it runs.

It said it ran "two DST changeovers", and one of its two dates was Europe's,
on which none of its zones changes: New York was an ordinary evening and
Chatham an ordinary afternoon. The default quiet-hours zone, America/Toronto,
springs forward with New York, and that hour was never run at all.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "scripts" / "time-gate.sh"


def _listed(name: str) -> list[str]:
    return re.search(rf'^{name}="([^"]*)"', GATE.read_text(encoding="utf-8"),
                     re.M | re.S).group(1).split()


def _changes(when: datetime, zone: str) -> timedelta:
    """How the zone's offset moves across a few hours either side of `when`."""
    tz = ZoneInfo(zone)
    return (when + timedelta(hours=3)).astimezone(tz).utcoffset() \
        - (when - timedelta(hours=3)).astimezone(tz).utcoffset()


def _at(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def test_both_changeovers_are_real_ones_in_the_zones_it_runs():
    zones = _listed("ZONES")
    moves = [_changes(_at(d), z) for d in _listed("DATES") for z in zones]
    forward = [m for m in moves if m > timedelta(0)]
    back = [m for m in moves if m < timedelta(0)]
    assert forward, "no date lands on a zone's clocks going forward"
    assert back, "no date lands on a zone's clocks going back"


def test_the_default_quiet_hours_zone_changes_on_both():
    """Quiet hours are kept in this zone unless the owner sets another."""
    from autotrader.config import DEFAULTS
    home = DEFAULTS["notifications"]["timezone"]
    moved = {_changes(_at(d), home) for d in _listed("DATES")} - {timedelta(0)}
    assert moved == {timedelta(hours=1), timedelta(hours=-1)}, (home, moved)


def test_ci_runs_one_of_its_combinations():
    """Nothing ran the gate, so tests that read the host's clock failed at
    six of its eight dates without anyone seeing."""
    doc = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    gated = [s.get("env") or {} for s in doc["jobs"]["test"]["steps"]
             if "AUTOTRADER_NOW" in (s.get("env") or {})]
    assert gated, "CI runs the suite only today, in UTC"
    for env in gated:
        assert env["AUTOTRADER_NOW"] in _listed("DATES"), env
        assert env["TZ"] in _listed("ZONES"), env
