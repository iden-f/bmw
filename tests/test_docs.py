"""The documents, checked against the thing they describe.

A document that names a command, a file or a workflow that does not exist is
worse than none: it is read under pressure, by someone who has forgotten the
details, and it sends them somewhere that is not there.

These are not style checks. Every assertion is "the document claims X; is X
true", and X is read off the code, the workflows or the page rather than
written down a second time here.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from autotrader import vault as V

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

#: The documents a person reads to set this up and understand it.
DOCS = ("README.md", "HOW-IT-WORKS.md")


def text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def flat(name: str) -> str:
    """The text with its line breaks and runs of spaces as single spaces, so
    a phrase is found wherever the paragraph happens to wrap."""
    return " ".join(text(name).split())


def hand_written() -> list[str]:
    """Every Markdown file at the top of the repository that a person wrote.

    The bot's own records (the market ledger, the validation report) are
    Markdown too, and they are data: they live in the vault, not here.
    """
    data = {Path(p).name for p in V.private_paths()}
    return sorted(p.name for p in ROOT.glob("*.md") if p.name not in data)


def workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def triggers(doc: dict) -> dict:
    # YAML 1.1 reads a bare `on:` as True.
    return doc[True] if True in doc else doc["on"]


def all_workflows() -> dict[str, dict]:
    return {p.name: workflow(p) for p in sorted(WORKFLOWS.glob("*.yml"))}


class TestTheDocumentsThemselves:
    def test_there_are_only_a_few(self):
        """One front page and one explanation. Every extra document is one
        more place for a fact to go out of date."""
        extra = set(hand_written()) - {"README.md", "HOW-IT-WORKS.md", "CLAUDE.md"}
        assert not extra, f"fold these into README.md or HOW-IT-WORKS.md: {sorted(extra)}"

    def test_the_readme_leads_to_the_rest(self):
        readme = text("README.md")
        for name in DOCS[1:]:
            assert f"]({name}" in readme, f"README.md does not link to {name}"


# ------------------------------------------------------------------ commands

def _subcommands() -> dict[str, argparse.ArgumentParser]:
    from autotrader import cli
    parser = cli.build_parser()
    return {name: sub for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
            for name, sub in action.choices.items()}


def _options(parser: argparse.ArgumentParser) -> set[str]:
    return {opt for action in parser._actions for opt in action.option_strings}


def _choices(parser: argparse.ArgumentParser) -> list[tuple[str, ...]]:
    """The fixed choices of each positional argument, in order."""
    return [tuple(action.choices) if action.choices else ()
            for action in parser._actions
            if not action.option_strings and action.dest != "help"]


def _invocations(doc: str) -> list[list[str]]:
    """Every command the text tells you to run, as tokens after the program.

    Two shapes: a whole line or span that starts "python -m autotrader", and
    a backticked span that starts with a subcommand and carries more than one
    word ("`vault pull`", "`doctor --live`"). A one-word span like "`set`" is
    left alone: it is as likely to be a Python name as a command.
    """
    known = _subcommands()
    found = []
    for line in doc.splitlines():
        for rest in re.findall(r"python -m autotrader\s+([^`#\n]*)", line):
            found.append(re.findall(r'"[^"]*"|\'[^\']*\'|\S+', rest))
    for span in re.findall(r"`([^`\n]+)`", doc):
        words = span.split()
        if len(words) > 1 and words[0] in known:
            found.append(words)
    return found


class TestEveryCommandTheyTellYouToRun:
    @pytest.mark.parametrize("doc", DOCS)
    def test_each_command_exists_with_the_flags_given(self, doc):
        from autotrader import cli
        known = _subcommands()
        top = _options(cli.build_parser())
        commands = _invocations(text(doc))
        assert commands, f"{doc} names no commands at all"
        wrong = []
        for tokens in commands:
            while tokens and tokens[0].startswith("-"):
                tokens = tokens[1:]              # --no-colour and friends
            if not tokens:
                continue
            name, args = tokens[0], tokens[1:]
            if name not in known:
                wrong.append(f"no command {name!r}")
                continue
            sub = known[name]
            flags = _options(sub) | top
            for flag in (a for a in args if a.startswith("--")):
                if flag.split("=")[0] not in flags:
                    wrong.append(f"{name} has no {flag}")
            positional = [a for a in args if not a.startswith("-")]
            for value, allowed in zip(positional, _choices(sub)):
                if allowed and value not in allowed:
                    wrong.append(f"{name} {value!r} is not one of {allowed}")
        assert not wrong, f"{doc}: " + "; ".join(wrong)

    def test_every_vault_step_the_check_runs_is_explained(self):
        """The check's own sequence, read from the workflow, is the one
        HOW-IT-WORKS.md walks through."""
        body = (WORKFLOWS / "watch.yml").read_text(encoding="utf-8")
        steps = set(re.findall(r"python -m autotrader vault (\w+)", body))
        assert steps, "watch.yml no longer runs the vault"
        doc = text("HOW-IT-WORKS.md")
        for step in steps:
            assert f"`vault {step}" in doc, f"vault {step} is not explained"


# ------------------------------------------------------------------ files

#: Extensions that make a backticked word a file name rather than a setting.
_FILE = re.compile(r"\.(py|ya?ml|md|json|sh|txt|html|js|enc|bin|log|webmanifest)$")


def _runtime_paths() -> set[str]:
    """Paths that are real but not on main: made by a run, kept in the vault,
    or published to gh-pages. Read off the code that makes them."""
    from autotrader import budget
    names: set[str] = set()
    for path in V.private_paths():
        names |= {path, Path(path).name}
    names |= set(V.PRIVATE_FILES.values()) | set(V.SITE_FILES)
    names |= {V.META_FILE, str(V.VAULT_DIR), budget.STOP_FILE}
    # What vault.publish writes beside the page.
    names |= {"lock.json", "data.enc"}
    # Change files arrive in control/ and are removed once applied.
    for paths in (triggers(workflow(WORKFLOWS / "watch.yml"))["push"].get("paths") or []):
        names.add(paths.split("/")[0])
    return names


def _named_paths(doc: str) -> set[str]:
    out = set()
    for span in re.findall(r"`([^`\s]+)`", doc):
        if "://" in span or "<" in span or span.startswith(("-", "$", "/")):
            continue
        if "/" in span or _FILE.search(span):
            out.add(span.rstrip("/"))
    return out


def _exists(path: str, runtime: set[str]) -> bool:
    if "*" in path:
        path = str(Path(path).parent)
    if path in runtime or any(path.startswith(f"{r}/") for r in runtime):
        return True
    return any((base / path).exists() for base in
               (ROOT, ROOT / "tests", ROOT / "autotrader", WORKFLOWS, ROOT / "docs"))


class TestEveryFileTheyPointAt:
    @pytest.mark.parametrize("doc", DOCS)
    def test_the_paths_named_exist(self, doc):
        runtime = _runtime_paths()
        missing = sorted(p for p in _named_paths(text(doc)) if not _exists(p, runtime))
        assert not missing, f"{doc} names paths that do not exist: {missing}"

    def test_the_modules_named_exist(self):
        named = set(re.findall(r"\*\*`(\w+)`\*\*", text("HOW-IT-WORKS.md")))
        assert named, "the module map has gone"
        missing = sorted(n for n in named if not (ROOT / "autotrader" / f"{n}.py").exists())
        assert not missing, missing

    def test_the_module_map_is_complete(self):
        named = set(re.findall(r"\*\*`(\w+)`\*\*", text("HOW-IT-WORKS.md")))
        modules = {p.stem for p in (ROOT / "autotrader").glob("*.py")} - {"__init__", "__main__"}
        unmapped = sorted(modules - named)
        assert not unmapped, f"HOW-IT-WORKS.md's module map leaves out: {unmapped}"

    def test_the_photo_directory_is_the_one_the_code_writes_to(self):
        from autotrader.thumbs import THUMB_DIR
        named = {p for doc in DOCS for p in _named_paths(text(doc)) if "thumbs" in p
                 and not p.startswith("thumbs")}
        assert named, "the documents stopped naming the photo directory"
        for path in named:
            assert Path(path) == THUMB_DIR, path


def _slug(heading: str) -> str:
    """The anchor GitHub gives a heading."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def _anchors(doc: str) -> set[str]:
    return {_slug(h) for h in re.findall(r"^#+\s+(.+?)\s*$", doc, re.M)}


class TestEveryLinkResolves:
    @pytest.mark.parametrize("doc", sorted(set(DOCS) | set(hand_written())))
    def test_relative_links_and_their_anchors(self, doc):
        body = re.sub(r"```.*?```", "", text(doc), flags=re.S)
        broken = []
        for target in re.findall(r"\]\(([^)\s]+)\)", body):
            if re.match(r"[a-z]+:", target):
                continue                              # https:, mailto:
            path, _, anchor = target.partition("#")
            if path and not (ROOT / path).exists():
                broken.append(target)
                continue
            if anchor:
                where = text(path) if path else text(doc)
                if anchor not in _anchors(where):
                    broken.append(target)
        assert not broken, f"{doc}: {broken}"


# ------------------------------------------------------------------ workflows

_ROW = re.compile(r"^\| `([\w-]+\.yml)` \| ([^|]+?) \|", re.M)


class TestEveryWorkflowTheyName:
    @pytest.mark.parametrize("doc", DOCS)
    def test_the_workflow_files_named_exist(self, doc):
        named = set(re.findall(r"`([\w-]+\.yml)`", text(doc)))
        missing = sorted(n for n in named if not (WORKFLOWS / n).exists())
        assert not missing, f"{doc}: {missing}"

    def test_the_table_uses_the_names_github_shows(self):
        rows = dict(_ROW.findall(text("HOW-IT-WORKS.md")))
        assert rows, "the workflow table has gone"
        wrong = {f: n for f, n in rows.items()
                 if (WORKFLOWS / f).exists() and workflow(WORKFLOWS / f)["name"] != n}
        assert not wrong, f"named differently from their name: fields: {wrong}"

    def test_the_table_has_every_workflow(self):
        rows = set(dict(_ROW.findall(text("HOW-IT-WORKS.md"))))
        assert rows == set(all_workflows()), (
            f"table {sorted(rows)} vs .github/workflows {sorted(all_workflows())}")

    def test_the_readme_names_the_check_as_github_shows_it(self):
        """Setup says which workflow to run; it has to be one in the list."""
        name = workflow(WORKFLOWS / "watch.yml")["name"]
        assert f"**{name}**" in text("README.md")

    def test_every_schedule_is_quoted(self):
        doc = text("HOW-IT-WORKS.md")
        for file, body in all_workflows().items():
            for entry in triggers(body).get("schedule") or []:
                assert entry["cron"] in doc, f"{file}: {entry['cron']!r} is not quoted"

    def test_every_secret_the_workflows_read_is_explained(self):
        readme, example = text("README.md"), text(".env.example")
        secrets = set()
        for path in WORKFLOWS.glob("*.yml"):
            secrets |= set(re.findall(r"secrets\.([A-Z][A-Z0-9_]+)",
                                      path.read_text(encoding="utf-8")))
        assert secrets, "no workflow reads a secret"
        for name in sorted(secrets):
            assert f"`{name}`" in readme, f"README.md does not explain {name}"
            assert re.search(rf"^{name}=", example, re.M), f".env.example lacks {name}"

    def test_every_variable_the_example_lists_is_one_the_code_reads(self):
        source = "\n".join(p.read_text(encoding="utf-8")
                           for p in (ROOT / "autotrader").glob("*.py"))
        listed = re.findall(r"^([A-Z][A-Z0-9_]+)=", text(".env.example"), re.M)
        assert listed
        unread = [n for n in listed if n not in source]
        assert not unread, f".env.example lists what nothing reads: {unread}"

    def test_every_workflow_that_alerts_can_reach_every_channel(self):
        """The watchdog and the weekly digest are alerts too. A channel the
        check can use and they cannot is one that never hears that the
        watch has gone quiet."""
        def read(name):
            return set(re.findall(r"secrets\.([A-Z][A-Z0-9_]+)",
                                  (WORKFLOWS / name).read_text(encoding="utf-8")))
        check = read("watch.yml") - {V.ENV_KEY, V.ENV_PREVIOUS}
        missing = sorted(check - read("events.yml"))
        assert not missing, f"events.yml cannot reach: {missing}"

    def test_which_channels_switch_themselves_on(self):
        from autotrader.config import CHANNEL_SECRETS, DEFAULTS
        channels = DEFAULTS["notifications"]["channels"]
        readme = flat("README.md")
        for name, spec in CHANNEL_SECRETS.items():
            if not spec["required"]:
                continue                  # set up in config, not by a secret
            told = f"set notifications.channels.{name}.enabled true" in readme
            auto = channels[name].get("enabled") == "auto"
            assert told != auto, f"{name}: the README and the default disagree"

    def test_changing_the_passphrase_works_as_described(self):
        bullet = flat("README.md").split("**Changing the passphrase.**")[1].split(" - **")[0]
        assert V.ENV_PREVIOUS in bullet
        if "**Check AutoTrader**" in bullet:
            # A check does it, so the check must be given the old one.
            body = (WORKFLOWS / "watch.yml").read_text(encoding="utf-8")
            assert f"secrets.{V.ENV_PREVIOUS}" in body, "watch.yml never reads it"
        else:
            steps = [bullet.index(f"`vault {s}") for s in ("pull", "open", "push --lease")]
            assert steps == sorted(steps), "pull, open, then push"
            assert "**Publish dashboard**" in bullet

    def test_nothing_it_says_points_at_a_missing_document(self):
        """An alert or a printed hint that sends someone to a document that is
        not there fails them at the moment they needed it."""
        data = {Path(p).name for p in V.private_paths()}
        sources = [*(ROOT / "autotrader").glob("*.py"), *WORKFLOWS.glob("*.yml"),
                   *(ROOT / "scripts").glob("*.sh"),
                   ROOT / "docs" / "index.html", ROOT / "docs" / "app.js"]
        missing = sorted(
            f"{path.relative_to(ROOT)}: {name}" for path in sources
            for name in set(re.findall(r"\b([A-Z][A-Z-]+\.md)\b",
                                       path.read_text(encoding="utf-8")))
            if name not in data and not (ROOT / name).exists())
        assert not missing, missing

    def test_the_kill_switch_is_the_one_the_check_reads(self):
        from autotrader.budget import STOP_FILE
        assert STOP_FILE in text("HOW-IT-WORKS.md")
        assert STOP_FILE in (WORKFLOWS / "watch.yml").read_text(encoding="utf-8")


# ------------------------------------------------------------------ numbers

class TestTheNumbersTheyQuote:
    """Every figure below was once true and then quietly was not."""

    def defaults(self):
        from autotrader.config import DEFAULTS
        return DEFAULTS

    def test_no_document_names_a_different_check_interval(self):
        every = int(self.defaults()["health"]["expected_interval_minutes"])
        bad = []
        for name in DOCS:
            doc = text(name)
            for match in re.finditer(
                    r"(?:every|one check every)\s+(?:\*\*)?(\d+|thirty|sixty|two|three)"
                    r"\s*(minutes?|hours?)", doc, re.I):
                before = " ".join(doc[max(0, match.start() - 24):match.start()]
                                  .lower().split())
                # A firing is not a check. The schedule fires more often than
                # a check is due, and a firing inside the floor stands down;
                # a document has to be able to say that.
                if before.endswith(("fire", "fires", "firing", "not", "rather than")):
                    continue
                n, unit = match.group(1).lower(), match.group(2).lower()
                n = {"thirty": 30, "sixty": 60, "two": 2, "three": 3}.get(n, n)
                minutes = int(n) * (60 if unit.startswith("hour") else 1)
                if minutes != every:
                    bad.append(f"{name}: '{match.group(0)}' (ships {every} min)")
        assert not bad, "; ".join(bad)

    def test_the_deduplication_floor(self):
        floor = int(self.defaults()["health"]["min_interval_minutes"])
        for name in DOCS:
            for said in re.findall(r"within (\d+) minutes", text(name)):
                assert int(said) == floor, f"{name} says {said}, ships {floor}"

    def test_no_document_names_a_different_silence_threshold(self):
        hours = float(self.defaults()["health"]["silent_after_hours"])
        words = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                 "seven": 7, "eight": 8, "twelve": 12}
        said = [(name, m) for name in DOCS for m in re.findall(
            r"no check has succeeded for\s+(\w+)\s+hours", text(name), re.I)]
        assert said, "the documents stopped saying when the watchdog speaks"
        for name, word in said:
            assert words.get(word.lower(), word) in (hours, str(int(hours))), (name, word)

    def test_the_failure_threshold(self):
        after = int(self.defaults()["health"]["alert_after_failures"])
        words = {"two": 2, "three": 3, "four": 4, "five": 5}
        for name in DOCS:
            for word in re.findall(r"fails?(?:ed)?\s+(\w+)\s+checks in a row",
                                   text(name), re.I):
                assert words.get(word.lower()) == after, (name, word)

    def test_the_passphrase_rules(self):
        readme = text("README.md")
        assert f"{V.MIN_PASSPHRASE} characters" in flat("README.md")
        for name in DOCS:
            assert f"{V.ITERATIONS:,} iterations" in flat(name), name

    def test_the_ntfy_topic_length(self):
        from autotrader import provision
        assert f"{provision._TOPIC_LENGTH} random characters" in flat("README.md")

    def test_the_drop_bar_and_the_new_car_priority(self):
        """The defaults, not anybody's own settings: a README that describes a
        customised bar or priority promises what no fresh copy ships with."""
        alerts = self.defaults()["notifications"]
        rows = text("README.md").splitlines()
        bar = next(r for r in rows if r.startswith("| `notifications.price_drop_alert_abs`"))
        assert f"`{alerts['price_drop_alert_abs']}`, the default" in bar, bar
        priority = alerts["channels"]["ntfy"]["priority_new"]
        row = next(r for r in rows
                   if r.startswith("| `notifications.channels.ntfy.priority_new`"))
        assert f"`{priority}` by default" in row, row
        assert f"sent at {priority} priority" in flat("README.md")

    def test_the_firings_a_day(self):
        """What a private copy is billed for, read off the schedule."""
        def count(field: str, span: int) -> int:
            if field == "*":
                return span
            if field.startswith("*/"):
                return -(-span // int(field[2:]))
            return len(field.split(","))
        per_day = sum(count(minute, 60) * count(hour, 24) for minute, hour, *_ in
                      (e["cron"].split() for e in
                       triggers(workflow(WORKFLOWS / "watch.yml"))["schedule"]))
        said = re.findall(r"(\d+) firings a day", flat("README.md"))
        assert said, "the README stopped saying what a private copy is billed for"
        assert all(int(n) == per_day for n in said), (said, per_day)

    def test_the_price_drop_floor(self):
        alerts = self.defaults()["notifications"]
        floor = f"{alerts['price_drop_min_pct']:g}% and ${alerts['price_drop_min_abs']:,}"
        assert floor in flat("README.md"), floor

    def test_which_alerts_are_on(self):
        on_off = self.defaults()["notifications"]["notify_on"]
        row = next(line for line in text("README.md").splitlines()
                   if line.startswith("| `notifications.notify_on.*`"))
        on, _, rest = row.partition("are on")
        off = rest.partition("are off")[0]
        said_on = set(re.findall(r"`(\w+)`", on.split("|")[2]))
        said_off = set(re.findall(r"`(\w+)`", off))
        assert said_on == {k for k, v in on_off.items() if v is True}
        assert said_off == {k for k, v in on_off.items() if v is False}

    def test_the_request_budget(self):
        from autotrader.config import Config
        budget = Config.defaults().get("scraping.request_budget")
        assert f"({budget} by default)" in flat("HOW-IT-WORKS.md")

    def test_the_photo_cap(self):
        from autotrader import thumbs
        assert f"at most {thumbs.MAX_PER_RUN} new photos" in flat("HOW-IT-WORKS.md")

    def test_the_run_history_depth(self):
        from autotrader.state import MAX_RUN_HISTORY
        assert f"last {MAX_RUN_HISTORY} run" in flat("HOW-IT-WORKS.md")

    def test_the_budget_guard(self):
        from autotrader import budget
        doc = flat("HOW-IT-WORKS.md")
        assert f"{budget.DEFAULT_STOP_AT:.0%}" in doc
        assert f"{budget.DEFAULT_ALLOWANCE:,}-minute" in doc

    def test_the_change_actions_are_the_real_ones(self):
        from autotrader import control
        para = text("HOW-IT-WORKS.md").split("Valid actions:")[1].split("\n\n")[0]
        assert set(re.findall(r"`([a-z-]+)`", para)) == set(control.ACTIONS)


# ------------------------------------------------------------------ the page

class TestThePageTheyDescribe:
    def test_the_views_are_the_pages_views(self):
        js = (ROOT / "docs" / "app.js").read_text(encoding="utf-8")
        block = re.search(r"const VIEWS = \[(.*?)\];", js, re.S).group(1)
        views = re.findall(r"label: '([^']+)'", block)
        section = text("README.md").split("## The dashboard")[1].split("\n## ")[0]
        listed = re.findall(r"^- \*\*(\w+)\*\*", section, re.M)
        assert listed == views, f"README lists {listed}, the page has {views}"

    def test_the_lock_screen_words_are_the_pages(self):
        page = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        assert "Keep this device unlocked" in flat("README.md")
        assert "Keep this device unlocked" in page
        assert 'aria-label="Lock"' in page
        if "ticked (the default)" in flat("README.md"):
            assert re.search(r'id="lock-keep"[^>]*\bchecked\b', page)

    def test_the_rules_the_searches_tab_changes(self):
        js = (ROOT / "docs" / "app.js").read_text(encoding="utf-8")
        editable = set(re.findall(r"(\w+):", re.search(r"const rule = \{([^}]*)\}", js).group(1)))
        said = flat("README.md").split("The Searches tab changes")[1].split(";")[0]
        assert set(re.findall(r"`(\w+)`", said)) == editable, (said, editable)

    def test_every_label_it_tells_you_to_press_is_on_the_page(self):
        """Bold words in running text are things to find on a screen: a
        workflow, GitHub's own button, or the page's. A heading on the page
        that reads differently is a step a stranger cannot follow."""
        page = "".join((ROOT / "docs" / f).read_text(encoding="utf-8")
                       for f in ("index.html", "app.js"))
        workflows = {workflow(p)["name"] for p in WORKFLOWS.glob("*.yml")}
        github = {"Commit changes", "Contents: Read and write"}
        body = re.sub(r"```.*?```", "", text("README.md"), flags=re.S)
        # A list item's or quote's own bold title is not a label.
        body = re.sub(r"(?m)^(\s*(?:[-*>]|\d+\.)\s+)\*\*[^*]+\*\*", r"\1", body)
        labels = {" ".join(b.split()) for b in re.findall(r"\*\*([^*]+)\*\*", body)}
        assert labels, "the README names nothing to press"
        missing = sorted(b for b in labels - workflows - github if b not in page)
        assert not missing, f"not on the page: {missing}"


# ------------------------------------------------------------------ alerts

class TestTheAlertsTheyList:
    """A message with no row is one you have to reason about from scratch,
    on a phone. So the table is checked against the code: a new alert cannot
    be added without this failing."""

    def subjects(self) -> set[str]:
        """Every subject the package can send, found by walking the syntax
        tree for calls to alert(), the "subject" key of dicts, and names
        called subject - not by grepping, so a near miss cannot count."""
        import ast

        def literal(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.JoinedStr) and node.values:
                head = node.values[0]
                if isinstance(head, ast.Constant) and isinstance(head.value, str):
                    return head.value
            return None

        found = set()
        for path in (ROOT / "autotrader").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                    if name == "alert" and len(node.args) >= 2 and literal(node.args[1]):
                        found.add(literal(node.args[1]))
                elif isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if isinstance(key, ast.Constant) and key.value == "subject" \
                                and literal(value):
                            found.add(literal(value))
                elif (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and getattr(node.targets[0], "id", "") == "subject"):
                    values = ((node.value.body, node.value.orelse)
                              if isinstance(node.value, ast.IfExp) else (node.value,))
                    found |= {literal(v) for v in values if literal(v)}
        return found

    def test_every_subject_has_a_row(self):
        doc = text("HOW-IT-WORKS.md").lower()
        subjects = self.subjects()
        assert len(subjects) >= 10, subjects
        missing = [s for s in sorted(subjects)
                   if len(fixed := s.split("{")[0].strip().rstrip(":").strip()) >= 12
                   and fixed.lower() not in doc]
        assert not missing, "sent and not explained: " + "; ".join(missing)

    def test_each_row_says_what_to_do(self):
        rows = [line for line in text("HOW-IT-WORKS.md").splitlines()
                if line.startswith("| **")]
        assert len(rows) >= len(self.subjects()) - 1
        for row in rows:
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            assert len(cells) >= 3 and all(cells), row

    def test_it_says_that_is_all_of_them(self):
        assert "these are all of them" in flat("HOW-IT-WORKS.md")


# ------------------------------------------------------------------ privacy

#: Shapes of the personal data this repository must never carry in its prose:
#: a generated ntfy topic, a topic URL, a postcode or the start of one, and an
#: email address. The owner's own account is read from the repository itself
#: (see ``_owner_patterns``) rather than written down here.
PERSONAL = [
    (r"autotrader-[a-z2-9]{20}\b", "a generated ntfy topic"),
    (r"ntfy\.sh/[\w-]{6,}", "an ntfy topic address"),
    (r"\b[ABCEGHJKLMNPRSTVXY]\d[A-Z](?:[ -]?\d[A-Z]\d)?\b", "a postcode"),
    (r"\b[\w.+-]+@[\w-]+\.[\w.]+\b", "an email address"),
]

#: Real makes and cities. In prose, one reads as somebody's own search, so
#: the examples use the cold start's generic make and at most one city.
MAKES = ("acura", "audi", "bmw", "buick", "cadillac", "chevrolet", "chrysler",
         "dodge", "fiat", "ford", "genesis", "gmc", "honda", "hyundai",
         "infiniti", "jaguar", "jeep", "kia", "lexus", "lincoln", "mazda",
         "mercedes", "mitsubishi", "nissan", "porsche", "subaru", "tesla",
         "toyota", "volkswagen", "volvo")
CITIES = ("toronto", "montreal", "vancouver", "calgary", "edmonton", "ottawa",
          "winnipeg", "hamilton", "halifax", "victoria", "saskatoon", "regina",
          "kelowna", "kitchener", "mississauga", "surrey", "burnaby", "laval")


def _this_repository() -> tuple[str, str] | None:
    """(owner, name) of the copy under test: from Actions, or from git."""
    slug = os.environ.get("GITHUB_REPOSITORY", "")
    if not slug:
        try:
            url = subprocess.run(["git", "-C", str(ROOT), "remote", "get-url", "origin"],
                                 capture_output=True, text=True).stdout.strip()
        except OSError:
            url = ""
        match = re.search(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url)
        slug = "/".join(match.groups()) if match else ""
    owner, _, name = slug.partition("/")
    return (owner, name) if owner and name else None


def _owner_patterns(owner: str, name: str) -> list[tuple[str, str]]:
    """The addresses that would point a reader at this particular copy."""
    o, n = re.escape(owner), re.escape(name)
    return [(rf"(?i)github\.com/{o}\b|\b{o}\.github\.io|\b{o}/{n}\b",
             "this copy's own account")]

#: Words that describe how this used to be rather than how it is.
HISTORY = [
    (r"\bv[12]\b", "a version"),
    (r"\bversion 1\b", "a version"),
    (r"\bused to\b", "history"),
    (r"\bno longer\b", "history"),
    (r"\bupgrading from\b", "history"),
    (r"\b20\d\d-\d\d-\d\d\b", "a date"),
]


def _prose() -> list[str]:
    return hand_written() + [".env.example"]


class TestNothingPersonalAndNoHistory:
    @pytest.mark.parametrize("doc", _prose())
    def test_no_personal_data(self, doc):
        body = text(doc)
        here = _this_repository()
        patterns = PERSONAL + (_owner_patterns(*here) if here else [])
        found = [f"{what}: {m.group(0)!r}" for pattern, what in patterns
                 for m in re.finditer(pattern, body)]
        assert not found, f"{doc}: {found}"

    def test_no_real_car_or_city_but_the_examples(self):
        example = re.search(r"autotrader\.ca/cars/(\w+)/",
                            (WORKFLOWS / "coldstart.yml").read_text(encoding="utf-8"))
        allowed = {example.group(1)} if example else set()
        words = {w for doc in _prose() for w in re.findall(r"[a-z]+", text(doc).lower())}
        makes = sorted(words & set(MAKES) - allowed)
        assert not makes, f"makes that read as someone's search: {makes}"
        cities = sorted(words & set(CITIES))
        assert len(cities) <= 1, f"more than one example city: {cities}"

    @pytest.mark.parametrize("doc", _prose())
    def test_written_as_it_is_now(self, doc):
        body = text(doc)
        found = [f"{what}: {m.group(0)!r}" for pattern, what in HISTORY
                 for m in re.finditer(pattern, body, re.I)]
        assert not found, f"{doc}: {found}"

    def test_the_patterns_would_catch_what_they_are_for(self):
        """A privacy check that matches nothing passes forever."""
        from autotrader.provision import generate_topic
        samples = [generate_topic(), "https://ntfy.sh/my-topic", "K1A 0B1",
                   "near K1A", "someone@example.com"]
        for sample in samples:
            assert any(re.search(p, sample) for p, _ in PERSONAL), sample
        mine = _owner_patterns("someone", "a-watch")
        for sample in ("github.com/someone/a-watch", "https://someone.github.io/a-watch/",
                       "--repo someone/a-watch"):
            assert any(re.search(p, sample) for p, _ in mine), sample
        clean = "AES-256-GCM, PBKDF2-SHA256, https://<you>.github.io/<repo>/ and a 2019 Example Coupe"
        assert not any(re.search(p, clean) for p, _ in PERSONAL + mine)


# ------------------------------------------------------------------ forking

class TestTheReadmeIsForSomeoneWithTheirOwnCopy:
    def test_it_says_to_fork_rather_than_use_this_one(self):
        readme = flat("README.md")
        assert re.search(r"\bfork\b", readme, re.I)
        assert "lock screen" in readme

    def test_it_explains_the_passphrase(self):
        readme = flat("README.md")
        for needed in (V.ENV_KEY, V.ENV_PREVIOUS, "Keep this device unlocked",
                       "**Lock**"):
            assert needed in readme, needed

    def test_it_says_to_start_a_vault_of_your_own(self):
        readme = text("README.md")
        assert f"`{V.VAULT_BRANCH}`" in readme and f"`{V.SITE_BRANCH}`" in readme
        assert re.search(rf"delete[^.]*`{V.VAULT_BRANCH}`", readme, re.I)

    def test_the_address_it_gives_is_the_one_alerts_link_to(self):
        """README tells you where your dashboard is; provision works out the
        same address for the alerts, from the same two names."""
        from autotrader.provision import find_dashboard_url
        url = find_dashboard_url({"GITHUB_REPOSITORY": "you/repo"})
        assert url == "https://you.github.io/repo"
        assert "https://<you>.github.io/<repo>/" in text("README.md")


# ------------------------------------------------------------------ behaviour

def test_hidden_cars_really_are_kept_and_explained(tmp_path):
    """The claim is about what the bot does, so the bot is what gets checked:
    a rule hides one of two cars, and both are still published."""
    from autotrader import filters
    from autotrader.config import Config
    from autotrader.dashboard import build_payload
    from autotrader.listing import Listing
    from autotrader.state import State

    assert any("kept, counted and explained" in flat(d) for d in DOCS)
    cfg = Config.defaults(tmp_path / "config.json")
    search = cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=25", "Civic")
    cfg.set("filters.max_price", 30000)
    state = State(path=tmp_path / "state.json")
    cars = [Listing(id="cheap", title="2019 Honda Civic", price=21000,
                    price_source="detail", search_id=search.id),
            Listing(id="dear", title="2023 Honda Civic Type R", price=58000,
                    price_source="detail", search_id=search.id)]
    kept, unpriced, dropped, _ = filters.apply(cars, cfg.rules_for(search)["filters"])
    for listing in kept + unpriced:
        state.record(listing)
    for listing, reason in dropped:
        state.record(listing, filtered=True, filter_reason=reason)
    payload = build_payload(cfg, state, {})
    hidden = [l for l in payload["listings"] if l.get("filtered")]
    assert [l["id"] for l in hidden] == ["dear"]
    assert all(l.get("filter_reason") for l in hidden)
    assert {l["id"] for l in payload["listings"]} == {"cheap", "dear"}
