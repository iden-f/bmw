"""Nothing personal in a public Actions log.

The repository is public so its Actions minutes are free, which makes every
log, job summary and artifact public too - readable by anyone, signed in or
not, for as long as GitHub keeps them. Private mode keeps the watch's data
encrypted on the `vault` branch; these tests keep it from leaking out of the
side of a run instead. A search link, a car, a price or the ntfy topic in a
log is the whole of private mode undone by one print statement.

So, in every workflow that opens the vault:

* no artifact is uploaded - an artifact is a public download - and no
  files are cached, because a pull request from a fork can restore a cache
  saved on the default branch;
* nothing writes a job summary, or sets a variable through GITHUB_ENV -
  both are shown to anyone who opens the run;
* every run of the bot that is not a `vault` command sends both its output
  and its errors somewhere nobody else can read: run.log (sealed into the
  vault after the run) or /dev/null. So does anything else that could run
  it out of sight - a script from the repository, Python fed in on stdin;
* no other command prints a private file, or lists a private folder;
* `vault` commands are the exception because they are written to print
  nothing personal. That is not taken on trust: a whole cycle of them, run
  against a repository full of personal data, is checked for it, and a
  command the workflows use that the cycle does not run fails here;
* every script is bash, the only shell read here, and no step hands the job
  to code that cannot be read - an action from inside the repository, a
  container, a reusable workflow;
* the passphrase comes from the repository's secrets and nowhere else, and no
  workflow that holds it runs on an event a stranger can raise.

The workflow files are parsed, and their shell read well enough to follow a
redirection on a brace group - `{ ...; } > run.log 2>&1` - which is how the
check step sends everything to run.log. It is not a shell. Anything it cannot
follow counts as printed, so a new construct fails here rather than leaking.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from autotrader import cli
from autotrader import vault as V

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = ROOT / ".github" / "workflows"

#: The workflows that open the vault. Named, so that a rename is noticed
#: rather than quietly leaving one unchecked; any other workflow found
#: opening the vault is held to the same rules.
NAMED = ("watch.yml", "pages.yml", "events.yml")

#: Where the bot's output may go. run.log only because the vault seals it.
SINKS = ("/dev/null", "run.log")

SECRET = re.compile(r"^\$\{\{\s*secrets\.(\w+)\s*\}\}$")

#: Everything a run decrypts or builds from what it decrypted. The retired
#: files are in the list because a run can still make them: docs/data.json is
#: the whole dashboard in the clear, from the moment a run builds it until the
#: runner is thrown away.
PRIVATE_PATHS = (*V.PRIVATE_FILES, *V.PRIVATE_DIRS, *V.RETIRED_FILES)

#: Files the runner shows to anyone who opens the run.
PUBLISHED_BY_THE_RUNNER = ("GITHUB_STEP_SUMMARY", "GITHUB_ENV")

#: Events anyone can raise on a public repository.
OUTSIDERS = {"pull_request", "pull_request_target", "pull_request_review",
             "pull_request_review_comment", "issues", "issue_comment",
             "discussion", "discussion_comment", "fork", "watch"}


def _load(path: Path) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{path.name} is not a workflow"
    return doc


def _steps(doc: dict):
    for job_name, job in (doc.get("jobs") or {}).items():
        for index, step in enumerate(job.get("steps") or []):
            where = f"{job_name}: {step.get('name') or step.get('uses') or f'step {index + 1}'}"
            yield where, step


def _scripts(doc: dict):
    for where, step in _steps(doc):
        if isinstance(step.get("run"), str):
            yield where, step["run"]


def _opens_the_vault(doc: dict) -> bool:
    return any(re.search(r"\bautotrader\s+vault\s+(open|pull)\b", script)
               for _, script in _scripts(doc))


def _private_workflows() -> dict[str, dict]:
    found = {p.name: _load(p) for p in sorted(WORKFLOW_DIR.glob("*.y*ml"))}
    missing = [name for name in NAMED if name not in found]
    assert not missing, (
        f"{', '.join(missing)} not found in .github/workflows - if a workflow "
        f"was renamed, rename it in NAMED too, or it goes unchecked")
    return {name: doc for name, doc in found.items()
            if name in NAMED or _opens_the_vault(doc)}


PRIVATE = _private_workflows()


# ----------------------------------------------------------- reading shell

@dataclass
class _Group:
    """A `{ ... }` or `( ... )` block, and the redirections after it."""
    trailer: str = ""


@dataclass
class _Command:
    text: str
    groups: list[_Group] = field(default_factory=list)

    def destinations(self) -> tuple[str | None, str | None]:
        """Where stdout and stderr end up; None is the job's own log.

        Outer groups first, then inner ones, then the command's own - the
        order the shell applies them in.
        """
        out = err = None
        for group in self.groups:
            out, err = _redirect(group.trailer, out, err)
        return _redirect(self.text, out, err)


_REDIRECT = re.compile(r"(&>>?|[12]?>>?&?)\s*([^\s;|&<>()]+)")


def _redirect(text: str, out: str | None, err: str | None):
    for op, target in _REDIRECT.findall(text):
        target = target.strip("'\"")
        if op.startswith("&"):                       # &> file: both
            out = err = target
        elif op.endswith("&"):                       # 2>&1, >&2: a copy
            source = {"1": out, "2": err}.get(target)
            if op.startswith("2"):
                err = source
            else:
                out = source
        elif op.startswith("2"):
            err = target
        else:
            out = target
    return out, err


_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")
_INTERPRETER = re.compile(r"(?:^|[\s/|;&])(python[\d.]*|bash|sh|dash|zsh|ksh)(?=\s|$)")


def _fold(opener: str, body: list[str]) -> str:
    """The line that starts a here-document, and what to make of the document.

    Fed to a shell or to Python - even through a pipe - it is code, so its
    words go just before the `<<`, behind the name of what runs them, where
    every check below reads them as part of the command. Fed to anything
    else it is only input, and dropped.
    """
    runs = _INTERPRETER.search(opener)
    if not runs:
        return opener
    at = _HEREDOC.search(opener).start()
    words = re.sub(r"[^\w.,:=/+@%-]+", " ", " ".join(body))
    return f"{opener[:at]} {runs.group(1)} {words} {opener[at:]}"


def _logical_lines(script: str) -> list[str]:
    """Expressions blanked, continuations joined, here-documents folded."""
    script = re.sub(r"\$\{\{.*?\}\}", "EXPR", script, flags=re.S)
    script = script.replace("\\\n", " ")
    lines: list[str] = []
    opener, until, body = "", None, []
    for line in script.splitlines():
        if until is not None:
            if line.strip() == until:
                lines.append(_fold(opener, body))
                until, body = None, []
            else:
                body.append(line)
            continue
        heredoc = _HEREDOC.search(line)
        if heredoc:
            opener, until = line, heredoc.group(2)
            continue
        lines.append(line)
    if until is not None:                     # never closed: all of it input
        lines.append(_fold(opener, body))
    return lines


def _split(line: str) -> tuple[list[str], list[str]]:
    """One line into command texts, with $(...) lifted out as commands of
    their own. Quotes are respected and a comment ends the line."""
    pieces: list[str] = []
    lifted: list[str] = []
    buf, quote, i = "", None, 0
    while i < len(line):
        ch = line[i]
        if quote != "'" and line.startswith("$(", i):
            depth, j = 1, i + 2
            while j < len(line) and depth:
                depth += {"(": 1, ")": -1}.get(line[j], 0)
                j += 1
            lifted.append(line[i + 2:j - 1])
            buf += "SUBST"
            i = j
            continue
        if quote:
            buf += ch
            if ch == quote:
                quote = None
            elif ch == "\\" and quote == '"' and i + 1 < len(line):
                buf += line[i + 1]
                i += 1
            i += 1
            continue
        if ch in "'\"":
            quote = ch
        elif ch == "#" and (not buf or buf[-1].isspace()):
            break
        elif line.startswith(("&&", "||"), i):
            pieces.append(buf)
            buf = ""
            i += 2
            continue
        elif ch in ";|" or (ch == "&" and not buf.endswith(">")
                            and not line.startswith("&>", i)):
            pieces.append(buf)
            buf = ""
            i += 1
            continue
        buf += ch
        i += 1
    pieces.append(buf)
    return pieces, lifted


def _commands(script: str) -> list[_Command]:
    out: list[_Command] = []
    stack: list[_Group] = []

    def take(text: str) -> None:
        text = text.strip()
        while text[:1] in ("{", "(") and (len(text) == 1 or text[1].isspace()):
            stack.append(_Group())
            text = text[1:].strip()
        if text[:1] in ("}", ")"):
            if stack:
                stack.pop().trailer = text[1:]
            return
        if text:
            out.append(_Command(text, list(stack)))

    for line in _logical_lines(script):
        pieces, lifted = _split(line)
        for inner in lifted:
            for piece in _split(inner)[0]:
                take(piece)
        for piece in pieces:
            take(piece)
    return out


_WORD = re.compile(r"""(?:[^\s'"]+|'[^']*'|"(?:\\.|[^"\\])*")+""")
_KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until",
             "!", "time"}


def _program(text: str) -> tuple[str, list[str]]:
    """The program a command runs, and its arguments: shell keywords and
    variable assignments in front of it stepped over."""
    words = _WORD.findall(text)
    while words and (words[0] in _KEYWORDS or re.match(r"^\w+=", words[0])):
        words.pop(0)
    if not words:
        return "", []
    return words[0].strip("'\""), words[1:]


# The bot, by module (`-m autotrader`, `-m autotrader.x`) or by import.
_BOT = re.compile(r"(?:^|[\s/])python[\d.]*\s+(?:-[A-Za-z]+\s+)*"
                  r"-m\s+autotrader(?:\.[\w.]+)?(?![\w.])")
_BOT_CODE = re.compile(r"(?:^|[\s/])python[\d.]*\s.*\b(?:import|from)\s+autotrader\b")
# A script from the repository: it can run the bot, and nothing here reads it.
_SCRIPT = re.compile(r"^(?:[\w.-]*/)*[\w.-]+\.(?:sh|bash|py)$")
_VALUED = ("--config", "--state")


def _is_bot(text: str) -> bool:
    return bool(_BOT.search(text) or _BOT_CODE.search(text)
                or any(_SCRIPT.match(w) for w in _WORD.findall(text)
                       if w[:1] not in "'\""))


def _subcommand(command: str) -> str:
    """The bot's subcommand, stepping over its global options. Anything that
    is not a plain `-m autotrader <command>` is held to the rules for `run`."""
    found = _BOT.search(command)
    if not found:
        return "run"
    rest = command[found.end():].split()
    skip = False
    for word in rest:
        if skip:
            skip = False
        elif word in _VALUED:
            skip = True
        elif not word.startswith("-"):
            return word.strip("'\"")
    return "run"                                   # bare invocation runs


def _vault_action(command: str) -> str:
    rest = command[_BOT.search(command).end():].split()
    after = rest[rest.index("vault") + 1:]
    return next((w for w in after if not w.startswith("-")), "")


def _bot_runs(script: str) -> list[_Command]:
    return [c for c in _commands(script) if _is_bot(c.text)]


def _leaks(script: str) -> list[str]:
    """Bot runs whose output or errors would reach the job's log."""
    found = []
    for command in _bot_runs(script):
        if _subcommand(command.text) == "vault":
            continue
        out, err = command.destinations()
        if out not in SINKS or err not in SINKS:
            found.append(f"{command.text.strip()}  (stdout -> {out or 'the log'}, "
                         f"stderr -> {err or 'the log'})")
    return found


_MENTION = re.compile(r"(?<![\w.-])(?:" + "|".join(re.escape(p) for p in PRIVATE_PATHS)
                      + r")(?![\w.-])")
#: Programs that can name a private file without printing any of it.
_QUIET = {"rm", "mv", "cp", "mkdir", "touch", "test", "[", "[[", "echo",
          "printf", "true", ":", "exit"}
_QUIET_GIT = {"rm", "add", "mv", "commit", "restore", "checkout", "config"}
_STANDARD_STREAM = re.compile(r"/dev/(?:std(?:out|err)|fd/|tty)|/proc/self/fd")


def _prints_private(command: _Command) -> bool:
    """Does this command put a private file into the job's log - what is in
    it, or, for a folder, the names in it? A photo is named for its listing,
    and a listing id is enough to find the car."""
    if _is_bot(command.text):
        return False                   # the bot's own rules are above
    text = _REDIRECT.sub(" ", command.text)   # where output goes is not a read
    if not _MENTION.search(text):
        return False
    out, _ = command.destinations()
    if out in SINKS:
        return False
    program, args = _program(text)
    if program == "git":
        quiet = next((a for a in args if not a.startswith("-")), "") in _QUIET_GIT
    else:
        quiet = program in _QUIET
    verbose = any(re.fullmatch(r"-[A-Za-z]*v[A-Za-z]*|--verbose", a) for a in args)
    listed = program in ("echo", "printf") and any(
        c in w for w in _WORD.findall(text) if _MENTION.search(w) and w[:1] not in "'\""
        for c in "*?[")
    return not quiet or verbose or listed or bool(_STANDARD_STREAM.search(text))


def _printed(script: str) -> list[str]:
    return [c.text.strip() for c in _commands(script) if _prints_private(c)]


class TestTheReaderItself:
    """The checks below are only as good as this reading of shell, so it is
    tested on its own - on the leaks it exists to catch as well as on what
    the workflows actually do."""

    @pytest.mark.parametrize("script", [
        "python -m autotrader run",
        "python -m autotrader run > /dev/null",
        "python -m autotrader run 2>/dev/null",
        "python -m autotrader run 2>&1 > /dev/null",
        "python -m autotrader run | tee run.log",
        "python -m autotrader run 2>&1 | tee run.log",
        "python -m autotrader run > run.log 2>&1 &\npython -m autotrader events",
        "python -m autotrader --config vault.json run > /dev/null",
        "python3 -m autotrader weekly --notify > \"$RUNNER_TEMP/out\" 2>&1",
        "x=$(python -m autotrader list)",
        "{\n  python -m autotrader run\n}\npython -m autotrader events",
        "{\n  python -m autotrader run\n} > run.log",
        "if [ -d control ]; then python -m autotrader control control; fi",
        "python -m autotrader \\\n  run",
        "set -e\n.venv/bin/python -m autotrader run --no-notify",
        "timeout 900 python -u -m autotrader run",
        "LOG=run.log; python -m autotrader run > $LOG 2>&1",
        # The bot by any other name.
        "python -m autotrader.__main__ run",
        "python -c 'from autotrader.cli import main; main([\"run\"])'",
        "python - <<'PY'\nimport os\nos.system('python -m autotrader run')\nPY",
        "python - <<'PY'\nfrom autotrader import cli\ncli.main(['vault', 'open'])\nPY",
        "cat <<'PY' | python -\nfrom autotrader import cli\ncli.main(['run'])\nPY",
        "bash scripts/check.sh",
        "./scripts/check.sh > run.log",
    ])
    def test_a_run_that_prints_is_caught(self, script):
        assert _leaks(script), script

    @pytest.mark.parametrize("script", [
        "python -m autotrader run > /dev/null 2>&1",
        "python -m autotrader run &> /dev/null || true",
        "python -m autotrader run >/dev/null 2>&1 || echo 'the run failed'",
        "{\n  python -m autotrader setup\n  if [ -d control ]; then "
        "python -m autotrader control control; fi\n  python -m autotrader run "
        "${{ inputs.dry_run && '--dry-run' || '' }}\n} > run.log 2>&1",
        "{ python -m autotrader run; } > /dev/null 2>&1",
        "( python -m autotrader run ) > run.log 2>&1",
        "timeout 900 python -m autotrader run > run.log 2>&1",
        "python -m autotrader vault seal",
        "python -m autotrader --no-colour vault push --lease || echo 'saved first'",
        "git rm -r -q --cached -- $(python -m autotrader vault paths)",
        "# python -m autotrader run\necho done",
        "echo 'python -m autotrader run is what the check does'",
        "python - <<'PY' > run.log 2>&1\nfrom autotrader import cli\ncli.main(['run'])\nPY",
        "cat > notes.txt <<'EOF'\npython -m autotrader run\nEOF",
        "bash scripts/check.sh > /dev/null 2>&1",
    ])
    def test_a_run_that_prints_nothing_passes(self, script):
        assert not _leaks(script), _leaks(script)

    @pytest.mark.parametrize("script", [
        "cat run.log",
        "tail -n 50 run.log",
        "sed -n p run.log",
        "grep -i civic state.json",
        "jq .searches config.json",
        "base64 -w0 validation-report.md",
        "python -c \"print(open('config.json').read())\"",
        "python - <<'PY'\nprint(open('state.json').read())\nPY",
        "git diff -- config.json",
        "git show HEAD:state.json",
        "git commit -v -m x -- EVENTS.md",
        "python - < state.json",
        "while read line; do echo \"$line\"; done < run.log",
        "echo \"$(cat EVENTS.md)\"",
        "echo $(<run.log)",
        "echo docs/thumbs/*",
        "ls docs/thumbs",
        "find archives -name metadata.json",
        "cp docs/data.json /dev/stdout",
        "rm -rv diagnostics",
        "cat \"$GITHUB_WORKSPACE/docs/events.json\"",
        "cat archives/*/metadata.json",
        "cat NOTIFY.md",
        "if [ -f run.log ]; then cat run.log; fi",
    ])
    def test_a_private_file_that_is_printed_is_caught(self, script):
        assert _printed(script), script

    @pytest.mark.parametrize("script", [
        "git rm -r -q --cached --ignore-unmatch -- config.json state.json",
        "git add -A -- BUDGET-STOP",
        "rm -f run.log",
        "rm -rf archives diagnostics docs/thumbs",
        "test -f state.json || echo 'no state yet'",
        "if [ ! -f vault/meta.json ]; then echo 'No vault yet.'; exit 0; fi",
        "echo 'run.log is sealed with the rest'",
        "cat run.log > /dev/null",
        "grep -c x state.json > run.log 2>&1",
        "{\n  cat state.json\n} > /dev/null",
        "python -m autotrader --config config.json run > /dev/null 2>&1",
        "status=$(cat \"$RUNNER_TEMP/status\")",
        "cat > notes.txt <<'EOF'\ncat run.log\nEOF",
    ])
    def test_a_private_file_that_is_not_printed_passes(self, script):
        assert not _printed(script), _printed(script)

    def test_it_finds_the_bot_in_every_private_workflow(self):
        """A reader that finds nothing passes everything."""
        for name, doc in PRIVATE.items():
            runs = [c for _, s in _scripts(doc) for c in _bot_runs(s)]
            assert runs, f"no run of the bot found in {name}"

    def test_it_finds_the_check_itself(self):
        """The one run that reads the market must be among those checked."""
        subcommands = {_subcommand(c.text) for _, s in _scripts(PRIVATE["watch.yml"])
                       for c in _bot_runs(s)}
        assert "run" in subcommands, subcommands


class TestNothingPersonalReachesTheLog:
    def test_the_sink_is_sealed(self):
        """run.log is only a safe place for output while the vault seals it."""
        assert "run.log" in V.PRIVATE_FILES

    @pytest.mark.parametrize("name", sorted(PRIVATE))
    def test_every_run_of_the_bot_is_redirected(self, name):
        leaks = [f"{where}: {leak}" for where, script in _scripts(PRIVATE[name])
                 for leak in _leaks(script)]
        assert not leaks, (
            f"{name} lets the bot print to a public log:\n  " + "\n  ".join(leaks)
            + "\nSend it to run.log with the check's other output, or to "
              "/dev/null with 2>&1.")

    @pytest.mark.parametrize("name", sorted(PRIVATE))
    def test_no_private_file_is_printed(self, name):
        printed = [f"{where}: {text}" for where, script in _scripts(PRIVATE[name])
                   for text in _printed(script)]
        assert not printed, (
            f"{name} prints a private file to a public log:\n  "
            + "\n  ".join(printed))

    @pytest.mark.parametrize("name", sorted(PRIVATE))
    def test_no_artifact_is_uploaded(self, name):
        """upload-pages-artifact too: it is an artifact like any other, with a
        download link on the run page."""
        uploads = [where for where, step in _steps(PRIVATE[name])
                   if re.search(r"upload[\w-]*artifact",
                                str(step.get("uses") or "").split("@")[0])]
        assert not uploads, f"{name} uploads an artifact, which is public: {uploads}"

    @pytest.mark.parametrize("name", sorted(PRIVATE))
    def test_nothing_is_cached(self, name):
        """A cache is not listed anywhere, but it is not private either: a
        pull request from a fork runs the fork's own workflow, and that can
        restore a cache saved on the default branch. setup-python's `cache:`
        is fine - it holds pip's downloads, keyed on requirements.txt."""
        cached = [where for where, step in _steps(PRIVATE[name])
                  if re.match(r"actions/cache\b", str(step.get("uses") or ""))]
        assert not cached, f"{name} caches files a fork could restore: {cached}"

    @pytest.mark.parametrize("name", sorted(PRIVATE))
    def test_nothing_writes_a_job_summary(self, name):
        """Nor a variable into GITHUB_ENV, which the log lists in the heading
        of every step after the one that set it."""
        text = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
        for channel in PUBLISHED_BY_THE_RUNNER:
            assert channel not in text, f"{name} writes {channel}, which anyone can read"

    def test_the_bot_never_writes_a_job_summary_either(self):
        """The workflows are not the only writer: the runner inherits the
        variable and once put its first-run report - search names and sample
        cars - at the top of the run page."""
        writers = [f"{p.relative_to(ROOT)}: {channel}"
                   for p in sorted((ROOT / "autotrader").rglob("*.py"))
                   for channel in PUBLISHED_BY_THE_RUNNER
                   if channel in p.read_text(encoding="utf-8")]
        assert not writers, writers

    @pytest.mark.parametrize("name", sorted(PRIVATE))
    def test_the_shell_does_not_echo_its_commands(self, name):
        """`set -x` prints every command with its arguments filled in."""
        traced = [where for where, script in _scripts(PRIVATE[name])
                  for line in _logical_lines(script)
                  if re.search(r"\bset\s+(-[a-wyzA-Z]*x|-o\s+xtrace)\b", line)
                  or re.search(r"\b(?:bash|sh)\s+-[a-wyzA-Z]*x", line)]
        assert not traced, f"{name} traces its commands: {traced}"

    @pytest.mark.parametrize("name", sorted(PRIVATE))
    def test_every_step_is_one_these_checks_can_read(self, name):
        """The reading above is of bash. A step run by another shell, or one
        that hands the job to code kept somewhere else - an action from inside
        the repository, a container, a reusable workflow - is code nothing
        here has read, running with the vault open."""
        doc = PRIVATE[name]
        unread = []
        holders = [("workflow", doc), *(doc.get("jobs") or {}).items()]
        for where, holder in holders:
            shell = ((holder.get("defaults") or {}).get("run") or {}).get("shell")
            if shell not in (None, "bash"):
                unread.append(f"{where}: runs its scripts with {shell}")
        for job_name, job in (doc.get("jobs") or {}).items():
            if job.get("uses"):
                unread.append(f"{job_name}: calls {job['uses']}")
            if job.get("container"):
                unread.append(f"{job_name}: runs in a container")
        for where, step in _steps(doc):
            uses = str(step.get("uses") or "")
            if uses.startswith(("./", "docker://")):
                unread.append(f"{where}: uses {uses}")
            if step.get("shell") not in (None, "bash"):
                unread.append(f"{where}: runs with {step['shell']}")
        assert not unread, f"{name}: " + "; ".join(unread)


class TestThePassphrase:
    @staticmethod
    def _envs(doc: dict):
        yield "workflow", doc.get("env") or {}
        for job_name, job in (doc.get("jobs") or {}).items():
            yield job_name, job.get("env") or {}
        for where, step in _steps(doc):
            yield where, step.get("env") or {}
            yield f"{where} (with)", step.get("with") or {}

    @pytest.mark.parametrize("path", sorted(WORKFLOW_DIR.glob("*.y*ml")), ids=lambda p: p.name)
    def test_it_only_ever_comes_from_the_secret(self, path):
        doc = _load(path)
        for where, env in self._envs(doc):
            for key in (V.ENV_KEY, V.ENV_PREVIOUS):
                if key not in env:
                    continue
                found = SECRET.match(str(env[key]).strip())
                assert found and found.group(1) == key, (
                    f"{path.name}, {where}: {key} must be ${{{{ secrets.{key} }}}}, "
                    f"not {env[key]!r}")

    @pytest.mark.parametrize("name", sorted(PRIVATE))
    def test_every_private_workflow_is_given_it(self, name):
        doc = PRIVATE[name]
        given = any(V.ENV_KEY in env for where, env in self._envs(doc)
                    if not where.endswith("(with)"))
        assert given, f"{name} opens the vault without {V.ENV_KEY}"

    @pytest.mark.parametrize("path", sorted(WORKFLOW_DIR.glob("*.y*ml")), ids=lambda p: p.name)
    def test_it_is_never_pasted_into_a_script(self, path):
        """Through the environment only. Interpolated into a script, it is
        part of the script's text; echoed, it is in the log."""
        for where, script in _scripts(_load(path)):
            assert not re.search(r"secrets\.WATCH_PASSPHRASE", script), where
            for line in script.splitlines():
                if re.search(r"\$\{?WATCH_PASSPHRASE", line):
                    assert re.search(r"\[\[?\s+-[zn]\s+\"?\$\{?WATCH_PASSPHRASE\w*\}?\"?\s",
                                     line), f"{path.name}, {where}: {line.strip()}"

    @pytest.mark.parametrize("name", sorted(PRIVATE))
    def test_no_stranger_can_start_a_workflow_that_holds_it(self, name):
        """A schedule, a push, a dispatch: things the owner does. A pull
        request or an issue can be opened by anyone on a public repository,
        and pull_request_target hands the secrets to a run a stranger
        started."""
        doc = PRIVATE[name]
        on = doc.get(True, doc.get("on")) or {}
        events = {on} if isinstance(on, str) else set(on)
        assert not events & OUTSIDERS, (
            f"{name} holds {V.ENV_KEY} and runs on {sorted(events & OUTSIDERS)}")


# ------------------------------------------------- the one exception, earned

PHRASE = "correct horse battery staple"
LISTING = "5-37004411"
SEARCH_NAME = "Nobodys Business Search"
PERSONAL_LINK = "https://www.autotrader.ca/cars/honda/civic/?loc=K1P+1J1&prx=77"
TOPIC = "autotrader-personalmarkerzzzz"
CAR = "Personal Marker Civic Touring"
CAPTURE = "nobodys-business-capture"
LOG_LINE = "a line of the last run nobody else may read"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _workflow_vault_actions() -> set[str]:
    return {_vault_action(c.text) for doc in PRIVATE.values()
            for _, script in _scripts(doc) for c in _bot_runs(script)
            if _subcommand(c.text) == "vault"}


class TestTheVaultCommandsPrintNothingPersonal:
    """`vault` commands print into the public log, so here they are run for
    real - pull, open, seal, push, site, publish, and the ways they fail -
    against a repository full of personal data, and everything they print is
    searched for it."""

    MARKERS = (LISTING, SEARCH_NAME, "loc=K1P", TOPIC, CAR, CAPTURE, LOG_LINE,
               PHRASE)

    @pytest.fixture
    def repo(self, tmp_path, monkeypatch):
        remote, work = tmp_path / "remote.git", tmp_path / "work"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        subprocess.run(["git", "init", "-q", str(work)], check=True)
        _git(work, "remote", "add", "origin", str(remote))
        for key, value in (("user.name", "t"), ("user.email", "t@example.com")):
            _git(work, "config", key, value)
        (work / "README.md").write_text("code\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "code")
        _git(work, "push", "-q", "origin", "HEAD:main")
        monkeypatch.chdir(work)
        monkeypatch.setenv(V.ENV_KEY, PHRASE)
        return work, remote

    @staticmethod
    def _personal(work: Path) -> None:
        from autotrader.config import Config
        cfg = Config.defaults(work / "config.json")
        cfg.add_search(PERSONAL_LINK, SEARCH_NAME)
        cfg.set("notifications.channels.ntfy.topic", TOPIC)
        cfg.set("notifications.channels.ntfy.enabled", True)
        cfg.save()
        (work / "state.json").write_text(json.dumps(
            {"listings": {LISTING: {"id": LISTING, "title": CAR}}}))
        (work / "EVENTS.md").write_text(f"# Events\n\n{CAR}\n")
        (work / "run.log").write_text(LOG_LINE + "\n")
        (work / "validation-report.md").write_text(f"{SEARCH_NAME}: {CAR}\n")
        docs = work / "docs"
        (docs / "thumbs").mkdir(parents=True)
        (docs / "thumbs" / f"{LISTING}.webp").write_bytes(b"RIFF photo")
        (docs / "thumbs" / "index.json").write_text(
            json.dumps({LISTING: {"file": f"{LISTING}.webp"}}))
        (docs / "events.json").write_text(json.dumps([{"title": CAR}]))
        (docs / "data.json").write_text(json.dumps(
            {"listings": [{"id": LISTING, "title": CAR}], "notify": {"ntfy_topic": TOPIC}}))
        (docs / "index.html").write_text("<!doctype html><title>page</title>")
        (work / "diagnostics").mkdir()
        (work / "diagnostics" / f"{CAPTURE}.json").write_text(json.dumps({"search": SEARCH_NAME}))
        (work / "archives" / LISTING).mkdir(parents=True)
        (work / "archives" / LISTING / "metadata.json").write_text(json.dumps({"title": CAR}))

    def _clean(self, capfd) -> str:
        seen = capfd.readouterr()
        text = seen.out + seen.err
        found = [m for m in self.MARKERS if m in text]
        assert not found, f"a vault command printed {found}:\n{text}"
        return text

    def test_a_whole_cycle(self, repo, tmp_path, monkeypatch, capfd):
        work, remote = repo
        self._personal(work)
        ran: set[str] = set()

        def vault(*args: str) -> int:
            ran.add(args[0])
            return cli.main(["--no-colour", "vault", *args])

        # A repository moving in from the clear, then the next run.
        assert vault("pull") == 0
        assert vault("open") == 0
        assert vault("seal") == 0
        assert vault("push", "--lease") == 0
        assert vault("paths") == 0
        assert vault("site", "site") == 0
        assert vault("publish", "site") == 0

        clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
        monkeypatch.chdir(clone)
        assert vault("pull") == 0
        assert vault("open", "--log") == 0
        assert (clone / "docs" / "thumbs" / f"{LISTING}.webp").is_file(), "nothing was opened"
        assert (clone / "run.log").read_text().strip() == LOG_LINE
        (clone / "state.json").write_text(
            json.dumps({"listings": {LISTING: {"title": CAR + " 2"}}}))
        assert vault("seal") == 0
        assert vault("push") == 0
        text = self._clean(capfd)
        assert "vault:" in text, "the capture saw nothing at all"

        unexercised = _workflow_vault_actions() - ran
        assert not unexercised, (
            f"the workflows run `vault {', '.join(sorted(unexercised))}`, which "
            f"prints into a public log and is not run here - add it")

    def test_the_ways_it_fails(self, repo, tmp_path, monkeypatch, capfd):
        work, remote = repo
        self._personal(work)
        for args in (["pull"], ["open"], ["seal"], ["push"]):
            assert cli.main(["vault", *args]) == 0

        # A check saved while this one ran: the lease refuses.
        other = tmp_path / "other"
        subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
        monkeypatch.chdir(other)
        assert cli.main(["vault", "pull"]) == 0
        assert cli.main(["vault", "open"]) == 0
        (other / "EVENTS.md").write_text(f"{CAR} again\n")
        assert cli.main(["vault", "seal"]) == 0
        assert cli.main(["vault", "push"]) == 0
        monkeypatch.chdir(work)
        assert cli.main(["vault", "push", "--lease"]) != 0

        # The wrong passphrase.
        monkeypatch.setenv(V.ENV_KEY, "not the passphrase at all")
        assert cli.main(["vault", "open"]) == 2
        monkeypatch.setenv(V.ENV_KEY, PHRASE)

        # A payload with something credential-shaped in it is not published,
        # and the refusal does not say where it was.
        (work / "docs" / "data.json").write_text(json.dumps(
            {"listings": [{"title": CAR}], "smtp_password": TOPIC}))
        assert cli.main(["vault", "site", "site"]) == 1

        # And a failure nobody planned for. A traceback names the file it
        # was writing, and this one is named for its listing.
        fresh = tmp_path / "fresh"
        subprocess.run(["git", "clone", "-q", str(remote), str(fresh)], check=True)
        monkeypatch.chdir(fresh)
        (fresh / "docs" / "thumbs" / f"{LISTING}.webp").mkdir(parents=True)
        assert cli.main(["vault", "pull"]) == 0
        assert cli.main(["vault", "open"]) == 2
        text = self._clean(capfd)
        assert "IsADirectoryError" in text or "failed" in text, text
