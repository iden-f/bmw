"""One word per idea, everywhere a person reads it.

HOW-IT-WORKS.md fixes the words. The value of fixing them is entirely in their
being the same in the digest, on the page and in the docs - a bot that says "hidden"
on the dashboard and "filtered out" in a Telegram message is two products
wearing one name, and the reader has to work out that they mean the same
thing.

These tests read the strings a person actually sees. Code identifiers are not
copy: `filter_reason` is a field name, `Change.RELISTED` is a constant, and
neither is ever rendered.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BANNED = {
    "scan":        "check",
    "poll":        "check",
    "vehicle":     "listing",
    "filtered out": "hidden",
}

# "sold" is not on that list, and the first version of this file put it there.
#
# The rule is not "never write the word". "A car can be listed and sold
# between checks" is a true sentence about the market and the right thing to
# say. The rule is that the bot must never *label a listing* sold, because it
# cannot know that - a listing coming down means the seller stopped
# advertising it. So the check below is on the labels, not on the prose.
STATE_WORDS = ("sold", "sells", "purchased")

# Strings that are addresses or parameters rather than anything anyone reads.
NOT_COPY = re.compile(r"https?://|\?\w+=|^\w+/\w+$|^[\w.-]+\.\w{2,4}$")


def page_copy() -> list[tuple[str, str]]:
    """Every literal string the dashboard renders, with where it came from."""
    out = []
    js = Path("docs/app.js").read_text(encoding="utf-8")
    # Strip // comments and block comments: they are for whoever reads the
    # code, and several of them discuss the banned words on purpose.
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    js = re.sub(r"^\s*//.*$", "", js, flags=re.M)
    for match in re.finditer(r"'((?:[^'\\\n]|\\.){4,})'|`((?:[^`\\]|\\.){4,})`",
                             js, re.S):
        out.append(("docs/app.js", match.group(1) or match.group(2)))

    html = Path("docs/index.html").read_text(encoding="utf-8")
    body = html.split("<body", 1)[-1] if "<body" in html else html
    body = re.sub(r"<style.*?</style>", "", body, flags=re.S)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    for text in re.findall(r">([^<>{}]{4,})<", body):
        out.append(("docs/index.html", text))
    return out


def message_copy() -> list[tuple[str, str]]:
    """Strings the notification channels put in front of a person."""
    out = []
    for name in ("autotrader/render.py", "autotrader/notifiers.py",
                 "autotrader/insight.py", "autotrader/control.py"):
        source = Path(name).read_text(encoding="utf-8")
        source = re.sub(r'"""[\s\S]*?"""', "", source)     # docstrings
        source = re.sub(r"^\s*#.*$", "", source, flags=re.M)
        for match in re.finditer(r'"((?:[^"\\\n]|\\.){6,})"|f"((?:[^"\\\n]|\\.){6,})"',
                                 source):
            out.append((name, match.group(1) or match.group(2)))
    return out


def _hits(banned, strings):
    return [(where, text) for where, text in strings
            if not NOT_COPY.search(text)
            and re.search(rf"\b{re.escape(banned)}\b", text, re.I)]


@pytest.mark.parametrize("banned,instead", sorted(BANNED.items()))
def test_the_page_does_not_use_it(banned, instead):
    hits = _hits(banned, page_copy())
    assert not hits, f"say {instead!r}: {hits[:3]}"


@pytest.mark.parametrize("banned,instead", sorted(BANNED.items()))
def test_the_messages_do_not_use_it(banned, instead):
    hits = _hits(banned, message_copy())
    assert not hits, f"say {instead!r}: {hits[:3]}"


def test_no_label_claims_a_car_was_sold():
    """The bot cannot know. A listing coming down is a seller who stopped
    advertising, which is not the same thing and matters to a buyer."""
    js = Path("docs/app.js").read_text(encoding="utf-8")
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    js = re.sub(r"^\s*//.*$", "", js, flags=re.M)

    labels = []
    block = re.search(r"const KIND = \{(.*?)\n\};", js, re.S).group(1)
    labels += re.findall(r"(?:label|group|rule):\s*'([^']+)'", block)
    labels += re.findall(r"el\('(?:span|b|button)', '(?:flag|chip|tag)[^']*', '([^']+)'", js)

    from autotrader.state import Change
    from autotrader.listing import Listing
    for kind in (v for k, v in vars(Change).items()
                 if k.isupper() and isinstance(v, str)):
        # Both shapes: a change with prices to compare and one without, since
        # several kinds word themselves differently for each.
        for prices in (({}, ), ({"old_price": 90000, "new_price": 86000}, )):
            change = Change(kind, Listing(id="x", url="u", price=86000),
                            **prices[0])
            labels.append(change.describe())

    for label in labels:
        for word in STATE_WORDS:
            assert not re.search(rf"\b{word}\b", label, re.I), \
                f"a label says {word!r}: {label!r}"


def test_the_documents_do_not_use_it_in_their_own_voice():
    """Quoted UI copy and the design rules themselves are exempt."""
    for name in ("README.md", "HOW-IT-WORKS.md"):
        text = Path(name).read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.lstrip().startswith(("|", ">")) or "`" in line:
                continue
            for banned in ("scan", "poll", "vehicle"):
                assert not re.search(rf"\b{banned}\b", line, re.I), \
                    f"{name}: {line.strip()[:80]}"


class TestOneNameForEachThing:
    def test_every_event_kind_the_bot_emits_has_page_copy(self):
        """A kind with no label renders as an empty chip."""
        insight = Path("autotrader/insight.py").read_text()
        emitted = set(re.findall(r'add\("(\w+)"', insight))
        block = re.search(r"const KIND = \{(.*?)\n\};",
                          Path("docs/app.js").read_text(), re.S).group(1)
        known = set(re.findall(r"^\s*(\w+):", block, re.M))
        assert emitted <= known, emitted - known

    def test_every_change_kind_the_runner_queues_has_digest_copy(self):
        """A kind with no line in render.py falls through to a bare price."""
        from autotrader.state import Change
        render = Path("autotrader/render.py").read_text()
        kinds = {v for k, v in vars(Change).items()
                 if k.isupper() and isinstance(v, str)}
        for kind in kinds:
            const = next(k for k, v in vars(Change).items() if v == kind)
            assert f"Change.{const}" in render, f"render.py never mentions {kind}"

    def test_the_control_actions_the_page_sends_all_exist(self):
        from autotrader import control
        js = Path("docs/app.js").read_text()
        js = re.sub(r"^\s*//.*$", "", js, flags=re.M)
        for action in re.findall(r"action: '([\w-]+)'", js):
            assert action in control.ACTIONS, action

    def test_sentence_case_on_the_page_headings(self):
        """No Title Case. A heading is a sentence, not a sign."""
        html = Path("docs/app.js").read_text()
        html = re.sub(r"^\s*//.*$", "", html, flags=re.M)
        for heading in re.findall(r"<h[12][^>]*>([^<${}]{6,})</h[12]>", html):
            words = [w for w in heading.split() if w.isalpha() and len(w) > 3]
            capped = [w for w in words[1:] if w[0].isupper()]
            assert len(capped) <= 1, f"Title Case: {heading!r}"


class TestOneIsNotPlural:
    """"1 photos" on a card, "2 day(s)" in a note. Small, and the kind of
    small that makes a page read like output rather than like writing."""

    def test_the_page_never_hardcodes_a_plural_after_a_count(self):
        js = Path("docs/app.js").read_text()
        js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
        js = re.sub(r"^\s*//.*$", "", js, flags=re.M)
        # A ${…} immediately followed by a space and a word ending in s, with
        # no conditional suffix anywhere in the same template piece.
        for match in re.finditer(r"\$\{[^{}]{1,60}\}\s(\w+s)\b(?!\s*\$\{)", js):
            word = match.group(1)
            if word in ("is", "was", "has", "as", "its", "this", "says",
                        "checks", "hours", "days", "minutes", "cars", "rules",
                        "searches", "listings", "photos", "half",
                        # A car with exactly one kilometre on it does not
                        # happen, and the reader of this string is a screen
                        # reader announcing an odometer.
                        "kilometres"):
                continue        # counted elsewhere, or not a count at all
            raise AssertionError(f"possible hardcoded plural: {match.group(0)!r}")

    def test_the_counts_that_can_be_one_are_conditional(self):
        js = Path("docs/app.js").read_text()
        for phrase in ("photo${", "day${", "search${"):
            assert phrase in js, f"{phrase} is not pluralised conditionally"

    def test_no_programmer_pluralisation_in_the_documents(self):
        """"2 day(s)" is a programmer talking to themselves in public."""
        for name in ("README.md", "HOW-IT-WORKS.md"):
            assert "(s)" not in Path(name).read_text(), name

    def test_none_in_the_message_strings_either(self):
        """Deliberately not applied to docs/app.js.

        The page's copy lives in backtick template literals that span code,
        so the string extractor at the top of this file cannot separate a
        sentence from the JavaScript around it - and a rule of "no (s)"
        flagged esc(s) and appendChild(s), which are calls. The page is
        covered by the conditional-plural test above instead, which checks the
        counts rather than the characters.
        """
        for where, text in message_copy():
            assert "(s)" not in text, (where, text[:90])

    def test_none_in_the_terminal_output_either(self):
        """A terminal is where "3 listing(s)" is most at home and least
        excusable - it is exactly the script's-output look this was meant to
        stop having."""
        import re as _re
        for name in ("autotrader/cli.py", "autotrader/runner.py"):
            source = Path(name).read_text()
            source = _re.sub(r'"""[\s\S]*?"""', "", source)
            source = _re.sub(r"^\s*#.*$", "", source, flags=_re.M)
            assert "(s)" not in source, name


class TestOneFormatterPerIdea:
    """Every number a person reads goes through one function.

    Written after "$413 /1000km" appeared beside "$29,000" on the same card -
    the same currency formatted two ways, one line apart - and after a
    "runner minutes" tile used a bare toLocaleString(), which is the browser's
    locale rather than this page's, so the same figure would render "3,000" in
    one tile and "3.000" in the next on a German phone.

    It is also a guard against a specific way of failing to fix things: the
    edit that was supposed to route per_1000km through money() silently
    matched nothing and was reported as done.
    """

    def app(self) -> str:
        return Path("docs/app.js").read_text(encoding="utf-8")

    def code_lines(self):
        """Lines of real code, with comments stripped."""
        out = []
        in_block = False
        for line in self.app().splitlines():
            stripped = line.strip()
            if in_block:
                if "*/" in stripped:
                    in_block = False
                continue
            if stripped.startswith("/*"):
                in_block = "*/" not in stripped
                continue
            if stripped.startswith("//"):
                continue
            out.append(line)
        return out

    def test_no_currency_is_formatted_by_hand(self):
        """`$${x}` in a template is money() not being used."""
        import re
        bad = [l.strip() for l in self.code_lines()
               if re.search(r"\$\$\{", l)]
        assert not bad, bad

    def test_every_locale_is_named(self):
        """A bare toLocaleString is the reader's locale, not the page's."""
        import re
        bad = [l.strip() for l in self.code_lines()
               if re.search(r"toLocale\w*\(\s*[\)\[]", l)]
        assert not bad, bad

    # The three functions allowed to group a number, plus the one tooltip that
    # formats a date rather than a figure.
    def test_only_three_functions_group_a_number(self):
        """A fourth way of writing a number is a fourth way of writing it
        differently. If this fails, either route the new call through money(),
        num() or signed(), or add it here on purpose.
        """
        calls = [l.strip() for l in self.code_lines() if "toLocaleString" in l]
        # money() and num() define the grouping; signed() reuses it; the
        # coverage strip's tooltip formats a DATE, not a figure.
        assert len(calls) == 5, calls
        assert sum(1 for c in calls if "cell.title" in c) == 1, calls

    def test_the_slot_word_is_derived_not_typed(self):
        """"half-hours" was typed into five strings while the schedule
        happened to be half-hourly, and stayed there when it stopped."""
        for line in self.code_lines():
            if "half-hour" in line:
                assert "slotWord" in line or "mins === 30" in line, line.strip()

    def test_python_says_it_the_same_way(self):
        """The alert and the page describe one schedule.

        Read from the AST rather than from the text, so a docstring saying
        "half-hours" - like this one - is not mistaken for a string somebody
        is sent.
        """
        import ast
        tree = ast.parse(Path("autotrader/events.py").read_text())
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docs.add(doc)
        # _slot_word is the one function allowed to know the word. That is
        # the whole point: it was in five places.
        allowed = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_slot_word":
                allowed = {n for n in ast.walk(node)}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and "half-hour" in node.value and node.value not in docs
                    and node not in allowed):
                raise AssertionError(
                    f"a literal 'half-hour' at line {node.lineno} - "
                    f"_slot_word is where that word lives")


class TestEveryTriggerTheBotCanRecordHasPageCopy:
    """The page turns an event name into a sentence. A trigger with no copy
    prints the raw GitHub event name at a reader - "workflow_dispatch" in the
    middle of an English sentence - which is the same class of leak as a
    placeholder title reaching the Feed."""

    def page(self):
        from pathlib import Path
        return (Path(__file__).resolve().parent.parent
                / "docs" / "app.js").read_text()

    def triggers_the_workflow_can_produce(self):
        import yaml
        from pathlib import Path
        doc = yaml.safe_load(
            (Path(__file__).resolve().parent.parent
             / ".github" / "workflows" / "watch.yml").read_text())
        on = doc[True] if True in doc else doc["on"]
        # Plus the two the bot invents: a local run, and a run recorded
        # before it started noting what started it.
        return set(on) | {"manual", "unattributed"}

    def test_each_one_has_a_phrase(self):
        import re
        source = self.page()
        block = source[source.index("const TRIGGER_WORDS"):]
        block = block[:block.index("};")]
        known = set(re.findall(r"^\s+(\w+):", block, re.M))
        missing = self.triggers_the_workflow_can_produce() - known
        assert not missing, f"no page copy for {sorted(missing)}"

    def test_a_named_outside_timer_does_not_print_its_event_name(self):
        """"repository_dispatch:cron-job.org" must reach the reader as the
        name, not as the event."""
        source = self.page()
        assert "startsWith('repository_dispatch:')" in source
        assert "an outside timer" in source

    def test_python_and_the_page_agree_on_what_counts_as_a_schedule(self):
        import re
        from autotrader.insight import SCHEDULE_TRIGGERS
        source = self.page()
        # The page filters these out when listing "the rest came from...".
        filtered = set(re.findall(r"k !== '(\w+)'", source))
        assert SCHEDULE_TRIGGERS <= filtered, (
            f"the page still lists {sorted(SCHEDULE_TRIGGERS - filtered)} as "
            f"something other than the schedule")


class TestThePythonSideHasOneVocabularyToo:
    """The page has been guarded against this since the "$1113 /1000km" card.

    The package had not been. It carried two copies of the plural helper with
    two different docstrings, a duration formatter only one module could
    reach, and a removal that read "gone" on Telegram, "REMOVED" in email and
    "Removed" in the digest - three self-consistent copies, which is exactly
    why no test noticed.
    """

    def package(self):
        return sorted(Path("autotrader").glob("*.py"))

    def code(self, path: Path) -> str:
        """Source with docstrings and comments blanked, lines preserved.

        The comments in this project discuss the words on purpose, at length,
        and several of them quote the wrong ones deliberately.
        """
        import tokenize
        lines = path.read_text(encoding="utf-8").splitlines()
        blank: set[int] = set()
        with open(path, "rb") as fh:
            previous = tokenize.INDENT
            for tok in tokenize.tokenize(fh.readline):
                docstring = tok.type == tokenize.STRING and previous in (
                    tokenize.INDENT, tokenize.NEWLINE, tokenize.NL,
                    tokenize.ENCODING, tokenize.DEDENT)
                if tok.type == tokenize.COMMENT or docstring:
                    blank.update(range(tok.start[0], tok.end[0] + 1))
                if tok.type not in (tokenize.NL, tokenize.COMMENT):
                    previous = tok.type
        return "\n".join("" if n + 1 in blank else line
                          for n, line in enumerate(lines))

    def test_every_dollar_figure_is_grouped(self):
        """`f"${x}"` beside `f"${y:,}"` is the same currency, two ways."""
        import re
        bad = []
        for path in self.package():
            for line in self.code(path).splitlines():
                for match in re.finditer(r"\$\{([^}]*)\}", line):
                    inner = match.group(1)
                    if ":," in inner or ":" not in inner and inner.endswith("_text"):
                        continue
                    if ":," not in inner:
                        bad.append(f"{path.name}: ${{{inner}}}")
        assert not bad, ("a dollar figure written without a thousands "
                         "separator: " + ", ".join(bad))

    def test_no_price_carries_cents(self):
        """Every price here is a whole-dollar asking price off a listing."""
        import re
        bad = []
        for path in self.package():
            for line in self.code(path).splitlines():
                if re.search(r"\$\{[^}]*:,\.\d", line):
                    bad.append(f"{path.name}: {line.strip()}")
        assert not bad, bad

    def test_the_plural_helper_is_defined_once(self):
        """It was defined twice, in runner.py and cli.py."""
        import re
        defined = [p.name for p in self.package()
                   if re.search(r"^def many\(|^def _many\(", self.code(p), re.M)]
        assert defined == ["words.py"], defined

    def test_the_duration_formatter_is_defined_once(self):
        import re
        defined = [p.name for p in self.package()
                   if re.search(r"^def span\(|^def _span\(", self.code(p), re.M)]
        assert defined == ["words.py"], defined

    def test_the_timestamp_parser_is_defined_once(self):
        """Three modules parsed a stored stamp with their own fromisoformat.

        Two of them returned whatever offset the string carried, so a stamp
        without one raised TypeError inside a comparison nobody expected could
        fail.
        """
        users = [p.name for p in self.package()
                 if "fromisoformat" in self.code(p)]
        assert users == ["clock.py"], (
            "parse a stored stamp with clock.parse, not by hand: " + str(users))


class TestACountAgreesWithItsVerb:
    """"1 problem need attention".

    The plural helper gets the noun right and knows nothing about the verb
    after it, so every place that follows a count with one has to agree by
    hand. This is the class, scanned for rather than the one instance.
    """

    VERBS = ("are", "have", "need", "were", "do", "say", "were", "were not")

    def package(self):
        return sorted(Path("autotrader").glob("*.py"))

    def test_no_count_is_followed_by_a_bare_plural_verb(self):
        import re
        bad = []
        pattern = re.compile(
            r"(?:_many|words\.many)\([^)]*\)\}\s+(" + "|".join(self.VERBS) + r")\b")
        for path in self.package():
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                match = pattern.search(line)
                if match:
                    bad.append(f"{path.name}:{n} '...{match.group(1)}'")
        assert not bad, (
            "a count followed by a verb that is plural whatever the count is: "
            + "; ".join(bad))

    def test_the_doctor_summary_agrees_at_one_and_at_two(self):
        import re
        source = Path("autotrader/cli.py").read_text(encoding="utf-8")
        line = re.search(r"problems == 1 else", source)
        assert line, "the doctor's summary no longer agrees its verb by hand"
        assert "'needs' if problems == 1 else 'need'" in source


class TestOneUnitPerInterval:
    """The page said "one expected every 120 minutes" in the stale alarm and
    "re-read about every 2 hours" in the welcome box. One interval, two
    units, four hundred pixels apart."""

    def code(self):
        return Path("docs/app.js").read_text(encoding="utf-8")

    def test_no_interval_is_printed_as_raw_minutes(self):
        import re
        bad = [line.strip() for line in self.code().splitlines()
               if re.search(r"\$\{(?:expected|cov\.expected_interval_minutes)"
                            r"[^}]*\}\s*minutes", line)]
        assert not bad, ("an interval printed in raw minutes rather than "
                         "through every(): " + "; ".join(bad))

    def test_no_duration_is_printed_as_raw_hours(self):
        import re
        bad = [line.strip() for line in self.code().splitlines()
               if re.search(r"Math\.round\([^)]*/\s*60\)\}\s*hours", line)]
        assert not bad, ("a duration printed by hand rather than through "
                         "hours(): " + "; ".join(bad))
