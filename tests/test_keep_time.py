"""The script the owner pastes to keep this bot on time.

GitHub's scheduler fills about 40% of this repository's two-hour slots, and it
drops whole windows rather than individual firings, so a second cron offset
buys very little. An outside timer starting the check is the fix, and the
handoff for setting one up is the thing most likely to be got wrong - so it
is a script in the repository rather than a curl in a README, and the
script's assumptions are asserted against the workflow here.

Every status-code branch is exercised with a stubbed curl. The README version
of this needed a table of HTTP codes that could not be verified from where it
was written: the sandbox's proxy intercepts api.github.com and rewrote the
replies (an invalid token came back 200), and docs.github.com was blocked. A
table of unverified codes reads exactly like a table of facts.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "keep-time.sh"
WATCH = ROOT / ".github" / "workflows" / "watch.yml"


def workflow():
    doc = yaml.safe_load(WATCH.read_text())
    return doc[True] if True in doc else doc["on"]


def run(*args, code="204", body='{"ok":1}', token="github_pat_x", **env):
    """Run the script with curl stubbed to return a canned status."""
    stub = ROOT / "tests" / "fixtures" / "fake-curl.sh"
    environ = dict(os.environ, KEEP_TIME_CURL=str(stub),
                   FAKE_CODE=code, FAKE_BODY=body, **env)
    if token:
        environ["GITHUB_TOKEN"] = token
    else:
        environ.pop("GITHUB_TOKEN", None)
        environ.pop("GH_TOKEN", None)
    return subprocess.run(["sh", str(SCRIPT), *args], capture_output=True,
                          text=True, env=environ, cwd=ROOT, timeout=30)


def sent(tmp_path, *args):
    """What the script handed curl: its arguments, and what came on its input."""
    record = tmp_path / "curl.txt"
    out = run(*args, FAKE_RECORD=str(record))
    assert out.returncode == 0, out.stdout + out.stderr
    argv, _, given = record.read_text().partition("--- input\n")
    return argv.splitlines(), given


class TestItCannotDriftFromTheWorkflow:
    """The script names a workflow, its inputs and a repository. If any stops
    matching the workflow, the owner's timer silently stops working and the
    dashboard says the schedule is keeping time when nothing is."""

    def test_it_starts_the_check_workflow(self, tmp_path):
        argv, _ = sent(tmp_path)
        url = [a for a in argv if a.startswith("https://")]
        assert len(url) == 1, argv
        assert url[0].endswith(f"/actions/workflows/{WATCH.name}/dispatches"), url

    def test_every_input_it_sends_is_one_the_workflow_takes(self, tmp_path):
        import json
        argv, _ = sent(tmp_path, "--from", "laptop")
        body = json.loads(argv[argv.index("-d") + 1])
        declared = workflow()["workflow_dispatch"]["inputs"]
        assert set(body["inputs"]) <= set(declared), (body, sorted(declared))
        assert body["ref"] == "main"
        assert body["inputs"]["from"] == "laptop"

    def test_it_is_deduplicated_like_the_schedule(self, tmp_path):
        """A check started without `automatic` never stands down, so an
        hourly timer would read the site every hour."""
        import json
        argv, _ = sent(tmp_path)
        body = json.loads(argv[argv.index("-d") + 1])
        assert body["inputs"]["automatic"] == "true"
        step = [s for s in yaml.safe_load(WATCH.read_text())["jobs"]["check"]["steps"]
                if s.get("id") == "bot"][0]
        assert "inputs.automatic" in step["env"]["AUTOTRADER_SCHEDULED"]

    def test_a_timer_that_asks_the_older_way_is_still_taken(self):
        """A cron service set up to send repository_dispatch of type `check`
        still starts a check, and still stands down like the schedule."""
        assert "check" in workflow()["repository_dispatch"]["types"]

    def test_no_repository_is_hard_coded(self):
        """A fork running it must ask for a check on itself, not on the
        repository it was forked from."""
        import re
        assert re.search(r'^REPO=""', SCRIPT.read_text(), re.M)

    def test_by_default_it_targets_this_clones_origin(self):
        url = subprocess.run(["git", "remote", "get-url", "origin"],
                             capture_output=True, text=True, cwd=ROOT).stdout.strip()
        if "github.com" not in url:
            pytest.skip("this clone's origin is not on GitHub")
        repo = url.split("github.com", 1)[1].lstrip(":/").removesuffix(".git")
        out = run()
        assert f"Asking {repo} for a check" in out.stdout, out.stdout + out.stderr

    def test_the_workflow_still_accepts_an_outside_timer_at_all(self):
        assert "workflow_dispatch" in workflow(), "the script has nothing to call"
        assert "from" in workflow()["workflow_dispatch"]["inputs"], (
            "an outside timer cannot say who it is")


class TestTheTokenItNeedsCannotReachThePassphrase:
    """Contents: write, which repository_dispatch needs, can push any code to
    main, and the next check runs it with the passphrase and every channel's
    secret. The script asks for a token that can only start a workflow, and
    keeps it off the command line, where `ps` shows it to every user."""

    def test_it_asks_for_actions_not_contents(self):
        out = run(token=None)
        assert "Actions to 'Read and write'" in out.stderr, out.stderr
        assert "Contents to" not in out.stderr, out.stderr

    def test_the_token_is_not_on_curls_command_line(self, tmp_path):
        argv, given = sent(tmp_path)
        assert not any("github_pat_x" in a for a in argv), argv
        assert "-H" in argv and argv[argv.index("-H") + 1] == "@-", argv
        assert given == "Authorization: Bearer github_pat_x\n", given

    def test_the_usage_does_not_put_it_on_one_either(self):
        usage = run("--help").stdout
        assert "--token" not in usage, usage
        assert "read -rs GITHUB_TOKEN" in usage, usage


class TestEveryAnswerGitHubCanGive:
    """Exercised, not tabulated."""

    @pytest.mark.parametrize("code,expect", [
        ("204", "OK"),
        ("401", "does not recognise the token"),
        ("403", "Actions: Read and write"),
        ("404", "cannot SEE"),
        ("422", "bug in this script"),
        ("415", "bug in this script"),
        ("000", "no HTTP response"),
        ("500", "not a code this script knows"),
        ("418", "not a code this script knows"),
    ])
    def test_it_explains_what_to_do(self, code, expect):
        out = run(code=code)
        assert expect in (out.stdout + out.stderr), (code, out.stdout, out.stderr)

    def test_only_success_exits_zero(self):
        assert run(code="204").returncode == 0
        for code in ("401", "403", "404", "422", "000", "500"):
            assert run(code=code).returncode == 1, code

    def test_an_unknown_code_still_prints_what_github_said(self):
        """The one thing a person can search for."""
        out = run(code="418", body='{"message":"teapot"}')
        assert "teapot" in out.stderr, out.stderr


class TestItRefusesToDoSomethingUseless:
    def test_no_token_explains_how_to_make_one(self):
        out = run(token=None)
        assert out.returncode == 1
        for needed in ("Fine-grained", "Actions", "Read and write"):
            assert needed in out.stderr, out.stderr

    def test_the_timer_name_reaching_the_dashboard_is_bounded(self):
        """It is printed on a page. Untrusted length and characters are not."""
        out = run("--from", "My Mac <script>alert(1)</script> " + "x" * 60)
        assert out.returncode == 0
        said = [l for l in out.stdout.splitlines() if "as " in l][0]
        name = said.split('"')[1]
        assert len(name) <= 24, name
        assert all(c.isalnum() or c in ".-" for c in name), name
        assert "<" not in name and ">" not in name

    def test_cron_mode_is_silent_on_success(self):
        """A cron entry that prints on every success mails the owner hourly."""
        assert run("--cron", code="204").stdout == ""

    def test_but_never_silent_on_failure(self):
        out = run("--cron", code="403")
        assert out.stderr.strip(), "a silent failure is how a timer dies unnoticed"


class TestTheDocumentationMatchesTheScript:
    """The README is where a fork's owner learns to run this. Whatever it
    shows has to be something the script takes, and the script - which reads
    GitHub's actual reply - is the only explanation of the answers."""

    DOCS = ("README.md", "HOW-IT-WORKS.md")

    def schedule(self):
        import re
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        section = re.split(r"^## Schedule\s*$", readme, flags=re.M)
        assert len(section) == 2, "the README's Schedule section has gone"
        return section[1].split("\n## ")[0]

    def test_the_readme_tells_the_reader_to_run_this_script(self):
        assert "scripts/keep-time.sh" in self.schedule()

    def test_the_flags_it_shows_are_ones_the_script_takes(self):
        import re
        accepted = set(re.findall(r"^\s+(--[\w-]+)\)", SCRIPT.read_text(), re.M))
        assert {"--repo", "--from", "--cron"} <= accepted, accepted
        shown = set(re.findall(r"(?<![\w-])(--[a-z][\w-]*)", self.schedule()))
        assert shown, "the README shows no way to run it"
        assert shown <= accepted, f"not flags of the script: {shown - accepted}"

    def test_a_fork_is_told_to_name_its_own_repository(self):
        """The script's default is the repository it was written in. A fork
        that runs it as shown must be asking for a check on itself."""
        assert "--repo <you>/<repo>" in self.schedule()

    def test_the_permission_it_names_is_the_one_the_script_asks_for(self):
        flat = " ".join(self.schedule().split())
        assert "with **Actions: Read and write** on your fork and nothing else" in flat
        body = SCRIPT.read_text()
        assert 'Actions set to "Read and write"' in " ".join(
            l.lstrip("# ") for l in body.splitlines())

    def test_the_readme_does_not_put_the_token_on_a_command_line(self):
        """A token typed into a command is in the shell's history."""
        import re
        for block in re.findall(r"```sh\n(.*?)```", self.schedule(), re.S):
            assert "github_pat_" not in block, block

    def test_the_hosted_timer_asks_the_way_the_script_does(self, tmp_path):
        import json
        import re
        section = self.schedule()
        url = re.search(r"\| URL \| `([^`]+)` \|", section).group(1)
        body = json.loads(re.search(r"\| Request body \| `([^`]+)` \|", section).group(1))
        argv, _ = sent(tmp_path, "--from", "cron-job")
        mine = [a for a in argv if a.startswith("https://")][0]
        assert url.split("/repos/", 1)[1].split("/", 2)[2] == \
            mine.split("/repos/", 1)[1].split("/", 2)[2], (url, mine)
        assert body == json.loads(argv[argv.index("-d") + 1]), body

    def test_it_does_not_also_carry_a_rival_curl_to_get_wrong(self):
        """Two ways to do it is two things to keep correct."""
        calls = sum((ROOT / d).read_text(encoding="utf-8").count("/dispatches")
                    for d in self.DOCS)
        assert calls <= 1, (
            f"{calls} copies of the dispatch call in the documents; "
            f"the script is the one that is tested")


class TestTheDocumentsPromiseNothingTheScriptCannotSay:
    """A document that lists status codes the script does not handle is worse
    than none: it is a thing the reader will believe. The documents may leave
    the codes to the script; they may not contradict it."""

    def codes_the_script_handles(self):
        import re
        body = SCRIPT.read_text(encoding="utf-8")
        # From THIS case to the esac that closes it. The script has an
        # earlier case for its own arguments, and searching from zero finds
        # that one's esac - before the start, so the slice comes back empty
        # and the check passes by having nothing to check.
        start = body.index('case "$CODE" in')
        block = body[start:body.index("esac", start)]
        out = set()
        for line in block.splitlines():
            match = re.match(r"\s{2}([\d|]+)\)", line)
            if match:
                out.update(match.group(1).split("|"))
        return out

    def test_the_script_handles_the_answers_that_matter(self):
        assert {"204", "401", "403", "404"} <= self.codes_the_script_handles()

    def test_no_row_for_a_code_the_script_would_fall_through_on(self):
        import re
        handled = self.codes_the_script_handles()
        for doc in TestTheDocumentationMatchesTheScript.DOCS:
            rows = re.findall(r"^\| \*\*(\d{3})\*\*", (ROOT / doc).read_text(), re.M)
            extra = [c for c in rows if c not in handled]
            assert not extra, f"{doc} explains codes the script does not: {extra}"


class TestTheStubItselfIsRight:
    """A test double that lies makes every test using it a lie.

    The canned body was written "${FAKE_BODY:-{}}", which POSIX sh parses as
    the expansion ${FAKE_BODY:-{ followed by a literal }. Every reply came
    back with one brace too many, and the first place it showed was a
    hand-run of the script printing `GitHub said: {"message":"..."}}` - which
    reads as a bug in the thing being handed over.
    """

    def curl(self, **env):
        import os
        import subprocess
        return subprocess.run(["sh", "tests/fixtures/fake-curl.sh"],
                              capture_output=True, text=True,
                              env={**os.environ, **env}).stdout

    def test_the_body_comes_back_exactly_as_given(self):
        body = '{"message":"Resource not accessible by personal access token"}'
        assert self.curl(FAKE_BODY=body, FAKE_CODE="403") == f"{body}\n403"

    def test_the_default_body_is_an_empty_object(self):
        assert self.curl() == "{}\n204"

    def test_the_script_quotes_it_back_unchanged(self):
        import os
        import subprocess
        body = '{"message":"nope"}'
        out = subprocess.run(
            ["sh", "scripts/keep-time.sh", "--from", "x"],
            capture_output=True, text=True,
            env={**os.environ, "FAKE_BODY": body, "FAKE_CODE": "403",
                 "KEEP_TIME_CURL": "tests/fixtures/fake-curl.sh",
                 "GITHUB_TOKEN": "github_pat_fake"})
        said = out.stdout + out.stderr
        assert f"GitHub said: {body}" in said, said
