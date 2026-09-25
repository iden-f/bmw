"""The workflow files themselves, checked the way GitHub checks them.

Two of these shipped broken and nothing noticed, because a workflow that does
not compile does not fail a job - it fails the whole run before any job
exists, so there is no log, no annotation in anything the bot reads, and no
test touches it. The watcher was off for eleven minutes, and another workflow
had been unstartable for five hours, before either was spotted by hand.

Both were ordinary mistakes that a parser catches instantly:

* a step given two `env:` blocks, so the second silently replaced the first
  under a plain YAML load and made the file invalid under GitHub's;
* `hashFiles()` in a job-level `if`, where it is not available - which is a
  compile error for the file, not a false condition for the job.

So the suite reads them now. It is not a full implementation of the Actions
expression language; it is the handful of shapes that have actually cost this
repository a run.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from .helpers import WATCH_CONFIGS, watch_config

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / ".github" / "workflows"
WORKFLOWS = sorted(HERE.glob("*.yml"))
WATCH = HERE / "watch.yml"

# Functions GitHub only exposes to a step. Called from a job-level `if`, the
# workflow does not compile.
STEP_ONLY_FUNCTIONS = ("hashFiles",)
# Contexts that do not exist yet when a job's `if` is evaluated.
JOB_IF_FORBIDDEN_CONTEXTS = ("steps.", "job.", "runner.", "env.")


class _NoDuplicates(yaml.SafeLoader):
    """A loader that refuses what GitHub refuses.

    PyYAML's default is to let a later key win, which is exactly why the
    duplicate `env:` looked fine locally and killed the watcher on push.
    """


def _mapping(loader, node, deep=False):
    seen: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise AssertionError(
                f"duplicate key {key!r} at line {key_node.start_mark.line + 1}"
            )
        seen[key] = loader.construct_object(value_node, deep=deep)
    return seen


_NoDuplicates.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _load(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_NoDuplicates)


def test_there_are_workflows_to_check():
    assert WORKFLOWS, "no workflow files found - is the test running from the repo root?"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_parses_with_no_duplicate_keys(path: Path):
    """The watcher bug: a second `env:` on the same step."""
    doc = _load(path)
    assert isinstance(doc, dict), f"{path} is not a mapping"
    # `on` is the YAML 1.1 boolean True once parsed, which is fine - it just
    # has to be there.
    assert "jobs" in doc, f"{path} has no jobs"
    assert True in doc or "on" in doc, f"{path} has no triggers"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_job_conditions_only_use_what_a_job_can_see(path: Path):
    """The other bug: hashFiles() in a job-level `if`.

    It reads as a condition that is simply false. It is not - the file does
    not compile, so committing the marker that workflow waited for would have
    started nothing at all.
    """
    for name, job in (_load(path).get("jobs") or {}).items():
        condition = str(job.get("if", ""))
        if not condition:
            continue
        for fn in STEP_ONLY_FUNCTIONS:
            assert f"{fn}(" not in condition, (
                f"{path.name}: job '{name}' calls {fn}() in its `if`. That is "
                f"only available to a step, and using it here makes the whole "
                f"workflow invalid - every run fails before any job starts."
            )
        for ctx in JOB_IF_FORBIDDEN_CONTEXTS:
            assert ctx not in condition, (
                f"{path.name}: job '{name}' reads `{ctx}` in its `if`, which "
                f"does not exist when a job's condition is evaluated"
            )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_job_can_actually_run(path: Path):
    for name, job in (_load(path).get("jobs") or {}).items():
        assert "runs-on" in job or "uses" in job, (
            f"{path.name}: job '{name}' has neither runs-on nor uses")
        # A job that can hang forever is a job that holds a concurrency group
        # forever, which is how one wedged long job silenced the watcher.
        if "runs-on" in job:
            assert "timeout-minutes" in job, (
                f"{path.name}: job '{name}' has no timeout-minutes")


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_needs_point_at_jobs_that_exist(path: Path):
    jobs = _load(path).get("jobs") or {}
    for name, job in jobs.items():
        needs = job.get("needs") or []
        if isinstance(needs, str):
            needs = [needs]
        for dep in needs:
            assert dep in jobs, (
                f"{path.name}: job '{name}' needs '{dep}', which is not a job "
                f"in this file")
        # And a condition that names a job it does not wait for is reading an
        # output that will never be there.
        for referenced in re.findall(r"needs\.([A-Za-z0-9_-]+)",
                                     str(job.get("if", ""))):
            assert referenced in needs, (
                f"{path.name}: job '{name}' reads needs.{referenced} but does "
                f"not list it in `needs`")


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_local_reusable_workflows_exist(path: Path):
    for name, job in (_load(path).get("jobs") or {}).items():
        uses = str(job.get("uses", ""))
        if uses.startswith("./"):
            assert (ROOT / uses[2:]).is_file(), (
                f"{path.name}: job '{name}' reuses {uses}, which is not there")


def test_the_watcher_is_still_scheduled():
    """The one workflow whose absence is the bot not existing."""
    doc = _load(WATCH)
    triggers = doc.get(True) or doc.get("on") or {}
    assert "schedule" in triggers, "the watcher has no schedule"
    assert triggers["schedule"], "the watcher's schedule is empty"


def _triggers_on(doc: dict) -> list[str]:
    """Names of the workflows whose completion starts this one."""
    on = doc.get(True) or doc.get("on") or {}
    if not isinstance(on, dict):
        return []
    ran = on.get("workflow_run") or {}
    return [str(n) for n in (ran.get("workflows") or [])]


GUARD = "github.event.workflow_run.event"


def _job_guarded(name: str, jobs: dict, seen: frozenset = frozenset()) -> bool:
    """Will this job stay put when the chain it did not ask for arrives?

    Either it asks about the triggering run itself, or it waits on a job that
    does and stands down when that job stands down. `always()` alone is not
    enough: a job that runs even when its dependency was skipped still
    completes the workflow, and completing is what starts the next one.
    """
    job = jobs.get(name) or {}
    condition = str(job.get("if", ""))
    if GUARD in condition:
        return True
    needs = job.get("needs") or []
    if isinstance(needs, str):
        needs = [needs]
    return bool(needs) and all(
        f"needs.{dep}.result != 'skipped'" in condition
        and dep not in seen
        and _job_guarded(dep, jobs, seen | {name})
        for dep in needs)


def _guarded(doc: dict) -> bool:
    """Does every job here refuse a chained trigger it did not want?"""
    jobs = doc.get("jobs") or {}
    return bool(jobs) and all(_job_guarded(name, jobs) for name in jobs)


def test_chained_workflows_cannot_bounce_forever():
    """The watcher starts the ledger, and the ledger starts the watcher.

    That arrangement exists because GitHub serves neither schedule reliably
    and they are not dropped together - but on its own it is two workflows
    taking turns for ever, a runner each, until somebody notices the bill.
    One side of any such loop has to refuse a chain it did not want.
    """
    docs = {}
    for path in WORKFLOWS:
        doc = _load(path)
        docs[str(doc.get("name") or path.stem)] = doc

    starts: dict[str, set[str]] = {name: set() for name in docs}
    for name, doc in docs.items():
        for upstream in _triggers_on(doc):
            if upstream in starts:
                starts[upstream].add(name)

    def cycle_from(start: str) -> list[str] | None:
        stack = [(start, [start])]
        while stack:
            node, path = stack.pop()
            for nxt in starts.get(node, ()):
                if nxt == start:
                    return path
                if nxt not in path:
                    stack.append((nxt, path + [nxt]))
        return None

    seen: set[frozenset[str]] = set()
    for name in docs:
        loop = cycle_from(name)
        if not loop or frozenset(loop) in seen:
            continue
        seen.add(frozenset(loop))
        assert any(_guarded(docs[n]) for n in loop), (
            "workflow_run loop with nothing to break it: "
            + " -> ".join(loop + [loop[0]])
            + ". One of them must gate every job on "
            "github.event.workflow_run.event, so only a run the scheduler "
            "started can start the next one."
        )


class TestNothingHoldsARunner:
    """The pattern this repository spent a week paying for, kept out.

    Three "pacemaker" workflows each held a GitHub runner for up to five and a
    half hours, sleeping in a loop to dispatch checks on a timer, because
    GitHub drops scheduled runs and a job that is already alive can ask for
    work reliably. It worked. Measured over 24 hours it also cost up to
    sixteen hours of runner a day - for a watch whose checks total about
    twelve minutes - and it was justified in writing with "the minutes are
    free because the repository is public".

    That justification is true and it is not a property of this code: it is a
    repository setting that one click changes. These tests are what stops the
    idea coming back the next time the schedule looks thin.
    """

    # GitHub cancels a job at 360 minutes - measured, by a probe that counted
    # out loud and got to 361. Nothing here should want a tenth of it.
    RUNNER_CEILING = 360
    SANE_CEILING = 30

    @staticmethod
    def workflows():
        return WORKFLOWS

    def test_the_pacemakers_are_gone(self):
        names = {p.name for p in self.workflows()}
        assert not (names & {"pacemaker.yml", "pacemaker-b.yml",
                             "pacemaker-c.yml"}), sorted(names)

    def test_nothing_long_running_is_on_a_timer(self):
        """A long job is fine. A long job GitHub starts by itself is not.

        A job asked for by hand may hold a runner for as long as its work
        needs, provided it is bounded. What must never exist again is a job
        with a big ceiling and a cron in front of it.
        """
        import re, yaml
        for path in self.workflows():
            body = path.read_text()
            longest = max((int(t) for t in
                           re.findall(r"timeout-minutes:\s*(\d+)", body)),
                          default=0)
            if longest <= self.SANE_CEILING:
                continue
            doc = yaml.safe_load(body)
            on = doc[True] if True in doc else doc.get("on") or {}
            triggers = set(on) if isinstance(on, dict) else {on}
            assert "schedule" not in triggers, (
                f"{path.name} may run for {longest} minutes AND is on a "
                f"schedule. Every minute of it is billed.")
            assert longest < self.RUNNER_CEILING, (
                f"{path.name}'s {longest}-minute ceiling is past the "
                f"{self.RUNNER_CEILING} the runner allows, so it is cancelled "
                f"rather than finishing.")

    def test_a_scheduled_job_is_minutes_not_hours(self):
        import re, yaml
        for path in self.workflows():
            body = path.read_text()
            doc = yaml.safe_load(body)
            on = doc[True] if True in doc else doc.get("on") or {}
            if not isinstance(on, dict) or "schedule" not in on:
                continue
            for timeout in re.findall(r"timeout-minutes:\s*(\d+)", body):
                assert int(timeout) <= self.SANE_CEILING, (
                    f"{path.name} is scheduled and allows a job to run for "
                    f"{timeout} minutes")

    def test_nothing_sleeps_its_way_through_a_shift(self):
        """Retry backoff is seconds. A pacemaker is minutes, in a loop."""
        import re
        for path in self.workflows():
            for line in path.read_text().splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or "sleep" not in stripped:
                    continue
                assert "INTERVAL_MINUTES" not in stripped, f"{path.name}: {stripped}"
                literal = re.search(r"\bsleep\s+(\d+)\b", stripped)
                if literal:
                    assert int(literal.group(1)) <= 60, f"{path.name}: {stripped}"
                assert not re.search(r"\bsleep\s+\$\(\(\s*\w+\s*\*\s*60", stripped), \
                    f"{path.name}: {stripped}"

    def test_nothing_starts_the_watcher_on_a_timer(self):
        """A change from the dashboard starts a check by being pushed, which
        is a person's action. A dispatch from another workflow, inside a loop,
        is a clock, and a clock is a held runner."""
        for path in self.workflows():
            body = path.read_text()
            if "gh workflow run watch.yml" not in body:
                continue
            assert path.name == "watch.yml", (
                f"{path.name} starts the watcher")
            after = body[body.index("gh workflow run watch.yml"):]
            assert "while" not in after.split("\n")[0], f"{path.name}: in a loop"
            assert "INTERVAL" not in body, f"{path.name} dispatches on a timer"

    def test_the_watcher_is_one_job(self):
        """Every job rounds up to a whole minute, so two jobs is two minutes."""
        doc = yaml.safe_load(WATCH.read_text())
        assert list(doc["jobs"]) == ["check"], list(doc["jobs"])

    @staticmethod
    def _firing_minutes(crons):
        """Every minute of the day a cron line fires, as a sorted list.

        Only the shapes this repository uses: a minute field that is a number
        or a comma-separated list of them, and an hour field that is `*` or
        `*/N`. Anything else raises rather than being quietly read as
        something it is not.
        """
        import re
        out = set()
        for cron in crons:
            parts = cron.split()
            minute, hour = parts[0], parts[1]
            if hour == "*":
                hours = list(range(24))
            else:
                step = re.fullmatch(r"\*/(\d+)", hour)
                assert step, f"cannot read the hour field of {cron!r}"
                hours = list(range(0, 24, int(step.group(1))))
            mins = [int(m) for m in minute.split(",")]
            assert all(0 <= m < 60 for m in mins), f"bad minute in {cron!r}"
            for h in hours:
                for m in mins:
                    out.add(h * 60 + m)
        return sorted(out)

    @classmethod
    def _gaps(cls, crons):
        """The gap in minutes between each firing and the next, wrapping
        round midnight so a schedule cannot look dense by ignoring the seam."""
        fires = cls._firing_minutes(crons)
        assert fires, "no firings at all"
        return [(b - a) % 1440 or 1440
                for a, b in zip(fires, fires[1:] + [fires[0] + 1440])]

    @pytest.mark.parametrize("where", WATCH_CONFIGS)
    def test_the_schedule_fires_often_enough_for_the_interval_it_promises(
            self, where, tmp_path, monkeypatch):
        """The config promises a check every N minutes. The cron has to be
        able to deliver one.

        This used to demand that the cron interval EQUAL the config interval,
        which encoded a plan that measurement has since killed: two offsets
        inside one two-hourly window, on the theory that GitHub drops
        individual firings so the second is a second chance at the same slot.
        Ninety-nine runs later the record says GitHub serves both offsets of a
        window or neither - every long gap in the history is followed by its
        twin half an hour later - so the pair was one chance wearing two hats,
        and the watch ran with gaps of up to seven and a quarter hours against
        a two-hour promise.

        The contract now is the honest one: fire at least as often as the
        promise, and let the deduplication floor below hold the actual cadence
        down to it. A firing that is dropped then costs at most the distance
        to the next firing, not the distance to the next window.
        """
        doc = yaml.safe_load(WATCH.read_text())
        on = doc[True] if True in doc else doc["on"]
        crons = [c["cron"] for c in on["schedule"]]
        assert crons, "the watcher has no schedule at all"
        worst = max(self._gaps(crons))
        cfg = watch_config(where, tmp_path, monkeypatch)
        expected = int(cfg.get("health.expected_interval_minutes"))
        assert worst <= expected, (
            f"the cron leaves up to {worst} minutes between firings and "
            f"the config expects a check every {expected} - a single "
            f"dropped firing already breaks the promise")

    @pytest.mark.parametrize("where", WATCH_CONFIGS)
    def test_firing_more_often_cannot_scrape_more_often(self, where, tmp_path,
                                                        monkeypatch):
        """Firing every half hour is insurance, not a shorter interval.

        It only stays insurance while consecutive firings land inside the
        deduplication floor: past it, the extra firing is a second real check
        on somebody else's site. Measured across the whole day including the
        seam at midnight, because a schedule that is dense from 00:07 to 23:37
        and sparse across the join is still sparse.
        """
        doc = yaml.safe_load(WATCH.read_text())
        on = doc[True] if True in doc else doc["on"]
        crons = [c["cron"] for c in on["schedule"]]
        cfg = watch_config(where, tmp_path, monkeypatch)
        expected = int(cfg.get("health.expected_interval_minutes"))
        floor = float(cfg.get("health.min_interval_minutes") or expected / 3.0)
        worst = max(self._gaps(crons))
        assert worst < floor, (
            f"firings are up to {worst} minutes apart, past the "
            f"{floor:.0f}-minute floor - the extra one would scrape again")

    @pytest.mark.parametrize("where", WATCH_CONFIGS)
    def test_the_deduplication_floor_cannot_outlast_the_promise(
            self, where, tmp_path, monkeypatch):
        """The floor throttles; it must not throttle past the promise.

        A floor at or above the expected interval would reject the very
        firing that was due, turning an on-time schedule into a late one -
        which is the failure the floor exists to survive, arriving by the
        front door.
        """
        cfg = watch_config(where, tmp_path, monkeypatch)
        expected = int(cfg.get("health.expected_interval_minutes"))
        floor = float(cfg.get("health.min_interval_minutes") or 0)
        assert floor < expected, (
            f"the floor is {floor:.0f} minutes and the promise is "
            f"{expected} - a check that arrived on time would be refused")


class TestTheWorkflowTellsTheBotWhatItNeedsToKnow:
    """Three facts only GitHub has, and the bot cannot guess any of them."""

    def watch(self):
        return yaml.safe_load(WATCH.read_text())

    def test_the_runner_label_the_bot_is_told_is_the_one_it_runs_on(self):
        """Half of the billing exemption is the runner - a larger runner is
        charged on a public repository like any other - and nothing at
        runtime can see the label that was asked for. So the workflow passes
        it, which means it is written twice, which means this has to exist."""
        job = self.watch()["jobs"]["check"]
        assert job["env"]["REPO_RUNNER"] == job["runs-on"], (
            f"runs-on is {job['runs-on']} and the bot is told "
            f"{job['env']['REPO_RUNNER']}")

    def test_the_check_is_told_how_it_was_started(self):
        step = [s for s in self.watch()["jobs"]["check"]["steps"]
                if s.get("id") == "bot"]
        assert step, "no step with id 'bot'"
        assert step[0]["env"]["RUN_TRIGGER"] == "${{ github.event_name }}"

    def test_it_is_still_told_the_visibility(self):
        step = [s for s in self.watch()["jobs"]["check"]["steps"]
                if s.get("id") == "bot"][0]
        assert "github.event.repository.visibility" in step["env"]["REPO_VISIBILITY"]
