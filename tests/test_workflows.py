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


class TestWhatAQueuedRunSees:
    def test_the_check_reads_the_branch_as_it_is_when_it_starts(self):
        """Runs queue behind each other. Checking out the commit its event
        fired on, a queued run found change files the run before it had
        already read and tidied away, and refused them as replays."""
        doc = yaml.safe_load(WATCH.read_text())
        checkout = [s for s in doc["jobs"]["check"]["steps"]
                    if str(s.get("uses", "")).startswith("actions/checkout@")]
        assert checkout, "no checkout step"
        assert (checkout[0].get("with") or {}).get("ref") == "${{ github.ref }}"


def test_publish_dashboard_never_changes_the_passphrase():
    """Only the check saves the vault. Publish dashboard re-encrypting its
    throwaway copy published a page, and an ntfy topic, that the next check
    replaced, and an owner who subscribed from it heard nothing."""
    from autotrader import vault as V
    text = (HERE / "pages.yml").read_text()
    assert V.ENV_PREVIOUS not in re.sub(r"#.*", "", text)


# ---------------------------------------------------- what reaches a script

def _run_scripts(doc: dict):
    for name, job in (doc.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            if isinstance(step.get("run"), str):
                yield f"{name}: {step.get('name') or step['run'].splitlines()[0]}", step["run"]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_nothing_a_caller_sends_is_pasted_into_a_script(path: Path):
    """`${{ }}` is pasted into a script's text before the shell reads it, so a
    value someone else chose - a dispatch's input, an event's field - is
    code there. Anyone with a token that can start the check chooses its
    inputs. Values reach a script through `env:`, where they stay values."""
    for where, script in _run_scripts(_load(path)):
        assert "${{" not in script, f"{path.name}: {where} pastes an expression"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_workflow_says_what_its_token_may_do(path: Path):
    """Without a `permissions` block the token gets the repository's
    default, which may be read and write."""
    assert "permissions" in _load(path), f"{path.name} leaves its token at the default"


def test_the_tests_cannot_push():
    """ci.yml installs unpinned test requirements on every push. With a token
    that could push, one of them could change the code the next check runs
    with the passphrase."""
    perms = _load(HERE / "ci.yml")["permissions"]
    assert perms == {"contents": "read"}, perms


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_secrets_reach_only_the_steps_that_use_them(path: Path):
    """A secret in a job's `env` is in every step's, the checkout and the
    install of other people's code included."""
    doc = _load(path)
    assert "secrets." not in str(doc.get("env") or ""), f"{path.name}: workflow env"
    for name, job in (doc.get("jobs") or {}).items():
        assert "secrets." not in str(job.get("env") or ""), f"{path.name}: job {name}"


#: The first release of each action that runs on Node 24. GitHub has retired
#: Node 20, which the releases before these run on.
NODE_24 = {"actions/checkout": 5, "actions/setup-python": 6}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_action_runs_on_a_retired_node(path: Path):
    used = re.findall(r"uses:\s*([\w.-]+/[\w.-]+)@v(\d+)", path.read_text(encoding="utf-8"))
    assert used, f"{path.name} uses no action"
    for action, major in used:
        if action in NODE_24:
            assert int(major) >= NODE_24[action], f"{path.name}: {action}@v{major}"


# ------------------------------------------------- running a step's script

#: Stands in for python: writes down how it was called, and fails for any
#: word in SHIM_FAIL.
_PYTHON = r"""#!/bin/sh
printf '%s\n' "$*" >> "$SHIM_CALLS"
case " $* " in
  *" vault pull "*) mkdir -p vault && : > vault/meta.json ;;
  *" vault paths "*) echo config.json ;;
esac
for failing in ${SHIM_FAIL:-}; do
  case " $* " in *" $failing "*) exit 1 ;; esac
done
exit 0
"""


def _step(path: Path, name: str) -> dict:
    for job in (_load(path).get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if step.get("name") == name:
                return step
    raise AssertionError(f"{path.name} has no step named {name!r}")


def _run_step(path: Path, name: str, cwd: Path, tmp_path: Path, **env):
    """A step's own script, run as GitHub runs it (`bash -e`), with python
    and sleep stood in for. Returns the result and each call to python."""
    import os
    import subprocess
    shims = tmp_path / "shims"
    shims.mkdir(exist_ok=True)
    for program, body in (("python", _PYTHON), ("sleep", "#!/bin/sh\nexit 0\n")):
        (shims / program).write_text(body)
        (shims / program).chmod(0o755)
    calls = tmp_path / "calls.txt"
    calls.write_text("")
    script = tmp_path / "step.sh"
    script.write_text(_step(path, name)["run"])
    environ = {**os.environ, "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}",
               "SHIM_CALLS": str(calls), "RUNNER_TEMP": str(tmp_path), **env}
    out = subprocess.run(["bash", "-e", str(script)], cwd=cwd, env=environ,
                         capture_output=True, text=True, timeout=120)
    return out, calls.read_text().splitlines()


class TestTheCheckStep:
    def test_it_sends_the_week_after_the_check(self, tmp_path):
        """The weekly digest rides on the check: every one asks whether the
        week is owed, so no one dropped firing can lose it."""
        out, calls = _run_step(WATCH, "Check", tmp_path, tmp_path,
                               DRY_RUN="false", RUN_TRIGGER="schedule")
        assert out.returncode == 0, out.stdout + out.stderr
        bot = [c.split("--no-colour ", 1)[-1] for c in calls]
        assert bot.index("run") < bot.index("events --notify") \
            < bot.index("weekly --notify --if-due"), bot

    def test_a_dry_run_sends_nothing(self, tmp_path):
        out, calls = _run_step(WATCH, "Check", tmp_path, tmp_path,
                               DRY_RUN="true", RUN_TRIGGER="workflow_dispatch")
        assert out.returncode == 0, out.stdout + out.stderr
        assert any(c.endswith("run --dry-run") for c in calls), calls
        assert not any("--notify" in c for c in calls), calls

    def test_a_dry_run_that_is_not_a_boolean_is_a_word_not_a_command(self, tmp_path):
        """Pasted into the script, `true; touch pwned` ran; through env it
        is only a value that is not "true"."""
        out, calls = _run_step(WATCH, "Check", tmp_path, tmp_path,
                               DRY_RUN="true; touch pwned", RUN_TRIGGER="workflow_dispatch")
        assert out.returncode == 0, out.stdout + out.stderr
        assert not (tmp_path / "pwned").exists()


class TestTheWatchdogStep:
    def test_a_crash_fails_the_run_once_the_vault_is_saved(self, tmp_path):
        """`events` finishes with 0 whenever it ran. It was followed by
        `|| true`, so a crash left the job green, sent no alarm and showed
        nothing - the one thing that reports silence failing silently."""
        out, calls = _run_step(HERE / "events.yml", "Look for silence", tmp_path,
                               tmp_path, SHIM_FAIL="events")
        assert out.returncode == 1, out.stdout + out.stderr
        assert "::error::" in out.stdout, out.stdout
        assert any("vault seal" in c for c in calls), calls
        assert any("vault push --lease" in c for c in calls), calls

    def test_a_watchdog_that_ran_is_green(self, tmp_path):
        out, calls = _run_step(HERE / "events.yml", "Look for silence", tmp_path, tmp_path)
        assert out.returncode == 0, out.stdout + out.stderr
        assert "::error::" not in out.stdout

    def test_it_says_nothing_private_when_it_fails(self, tmp_path):
        out, _ = _run_step(HERE / "events.yml", "Look for silence", tmp_path,
                           tmp_path, SHIM_FAIL="events")
        assert out.stdout.count("\n") <= 2, out.stdout


def _fires(cron: str) -> list[int]:
    """The minutes of the day a daily cron line fires on."""
    minute, hour, day, month, weekday = cron.split()
    assert (day, month, weekday) == ("*", "*", "*"), f"not daily: {cron!r}"

    def field(text: str, span: int) -> list[int]:
        if text == "*":
            return list(range(span))
        if text.startswith("*/"):
            return list(range(0, span, int(text[2:])))
        return [int(v) for v in text.split(",")]
    return sorted(h * 60 + m for h in field(hour, 24) for m in field(minute, 60))


def test_the_silence_alarm_comes_soon_after_the_silence():
    """No check for six hours raises the alarm, and only when the watchdog
    looks. Looking every six hours let it come twelve hours after the last
    check, and eighteen when GitHub dropped one firing."""
    from autotrader.config import DEFAULTS
    silent = float(DEFAULTS["health"]["silent_after_hours"]) * 60
    doc = _load(HERE / "events.yml")
    on = doc.get(True) or doc.get("on")
    fires = sorted(m for entry in on["schedule"] for m in _fires(entry["cron"]))
    gaps = [(b - a) % 1440 or 1440 for a, b in zip(fires, fires[1:] + fires[:1])]
    assert max(gaps) <= silent / 3, (
        f"the watchdog looks up to {max(gaps)} minutes apart, so the alarm for "
        f"{silent:.0f} minutes of silence can come {silent + max(gaps):.0f} after it")


class TestTheTidyUp:
    """The Save step commits the change files it read away, then pulls and
    pushes. A check restamps docs/sw.js when the page changed without its
    stamp, and `git pull --rebase` refuses a changed tracked file: every
    check then failed to push, five times over."""

    def git(self, cwd, *args):
        import subprocess
        return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t",
                               "-c", "user.email=t@example.com", *args],
                              check=True, capture_output=True, text=True).stdout

    def _meanwhile(self, tmp_path, changed):
        """A check that restamped docs/sw.js, and a push to main while it ran,
        of ``changed``: {path: text}. Returns the check's clone and origin."""
        import subprocess
        origin, work, other = tmp_path / "origin.git", tmp_path / "work", tmp_path / "other"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True,
                       capture_output=True)
        (work / "docs").mkdir()
        (work / "docs" / "sw.js").write_text("const BUILD = 'old';\n")
        (work / "control").mkdir()
        (work / "control" / "change.enc").write_text("sealed\n")
        self.git(work, "add", "-A")
        self.git(work, "commit", "-q", "-m", "a change from the page")
        self.git(work, "push", "-q", "origin", "HEAD:main")
        # Someone pushes while the check runs, so the tidy-up has to pull.
        subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True,
                       capture_output=True)
        for name, text in changed.items():
            (other / name).write_text(text)
        self.git(other, "add", "-A")
        self.git(other, "commit", "-q", "-m", "meanwhile")
        self.git(other, "push", "-q", "origin", "HEAD:main")
        (work / "docs" / "sw.js").write_text("const BUILD = 'new';\n")
        return work, origin

    def test_a_restamped_page_does_not_stop_the_push(self, tmp_path):
        work, origin = self._meanwhile(tmp_path, {"README.md": "more\n"})
        out, _ = _run_step(WATCH, "Save", work, tmp_path, GITHUB_REF_NAME="main")
        assert out.returncode == 0, out.stdout + out.stderr
        assert self.git(origin, "log", "-1", "--format=%s", "main").startswith("Tidy up")
        tree = self.git(origin, "ls-tree", "-r", "--name-only", "main").split()
        assert "control/change.enc" not in tree and "README.md" in tree, tree

    def test_nor_does_a_page_changed_on_main_meanwhile(self, tmp_path):
        """Stashed, the check's stamp met main's newer one: the pull left
        conflict markers in sw.js and exited 0, Publish sent a worker that
        does not parse, and a push that failed then could never be retried."""
        work, origin = self._meanwhile(
            tmp_path, {"docs/sw.js": "const BUILD = 'newer';\n"})
        out, _ = _run_step(WATCH, "Save", work, tmp_path, GITHUB_REF_NAME="main")
        assert out.returncode == 0, out.stdout + out.stderr
        assert self.git(work, "ls-files", "-u") == ""
        assert "<<<<<<<" not in (work / "docs" / "sw.js").read_text()
        assert self.git(origin, "log", "-1", "--format=%s", "main").startswith("Tidy up")
        assert self.git(origin, "show", "main:docs/sw.js") == "const BUILD = 'newer';\n"

    def test_the_site_it_publishes_is_stamped_from_the_page(self):
        """The tidy-up sets the check's stamp aside, and may pull a newer
        page: `vault site` stamps the worker it builds the site from
        (tests/test_vault_cycle.py)."""
        step = _step(WATCH, "Publish the dashboard")["run"]
        assert "vault site site" in step


# ------------------------------------------------------ the browser tests

class TestTheBrowserTestsRunInCI:
    """No step installed a browser, so every test that opens the page, the
    locked site or the collector skipped on GitHub's runners - 150 of the 165
    in those files - and a green Tests run said nothing about the page it
    then published."""

    def ci(self):
        return _load(HERE / "ci.yml")

    def test_one_leg_installs_a_browser_and_requires_it(self):
        job = self.ci()["jobs"]["test"]
        legs = [str(v) for v in job["strategy"]["matrix"]["python"]]
        install = [s for s in job["steps"] if "playwright install" in str(s.get("run", ""))]
        assert len(install) == 1, "no step installs a browser"
        leg = re.search(r"matrix\.python == '([\d.]+)'", str(install[0].get("if"))).group(1)
        assert leg in legs, (leg, legs)
        suite = [s for s in job["steps"] if str(s.get("run", "")).startswith("python -m pytest")
                 and "AUTOTRADER_NOW" not in (s.get("env") or {})]
        assert len(suite) == 1, suite
        required = str((suite[0].get("env") or {}).get("REQUIRE_BROWSER"))
        assert f"matrix.python == '{leg}'" in required, required

    def test_a_current_playwright_install_is_found(self, tmp_path, monkeypatch):
        """A current Playwright unpacks Chrome for Testing into chrome-linux64;
        only chrome-linux was looked for."""
        import sys
        import types

        from . import helpers
        for build in ("chromium-1194/chrome-linux", "chromium-1234/chrome-linux64"):
            (tmp_path / build).mkdir(parents=True)
            (tmp_path / build / "chrome").write_text("")
        monkeypatch.setattr(helpers, "BROWSERS", tmp_path)
        # And Playwright has no build of its own here.
        def no_driver():
            raise RuntimeError("no driver")
        broken = types.ModuleType("playwright.sync_api")
        broken.sync_playwright = no_driver
        monkeypatch.setitem(sys.modules, "playwright.sync_api", broken)
        assert helpers.browser_path.__wrapped__() == str(
            tmp_path / "chromium-1234" / "chrome-linux64" / "chrome")

    def test_where_one_is_required_a_missing_browser_fails(self, monkeypatch):
        from . import helpers
        monkeypatch.setattr(helpers, "browser_path", lambda: None)
        monkeypatch.setenv(helpers.REQUIRE_BROWSER, "1")
        with pytest.raises(pytest.fail.Exception):
            helpers.need_browser()
        monkeypatch.delenv(helpers.REQUIRE_BROWSER)
        with pytest.raises(pytest.skip.Exception):
            helpers.need_browser()
