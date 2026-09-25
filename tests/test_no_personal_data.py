"""Nothing public says what this copy watches, where, or whose it is.

The watch's own records are sealed in the vault. What is left in the open -
the code, the tests, the documents and the site the bot publishes - is read
by anyone, so it stays generic: a test that needs a car invents one, and the
published page shows cars only to someone holding the passphrase.

Three checks:

* Every tracked text file is searched for identities a real watch here has
  used: its searches' models, chassis codes and the site's codes for those
  models, the place tags of its links and the point they measure from, the
  place its distance rule measures from, an old postcode area, the old
  notification topic and the account. They are held as salted digests
  rather than written out, so this file does not become the list it guards
  against. A digest of a two-letter model code is
  no secret from anyone who tries; it keeps the words out of a casual read
  and out of a code search, and the real protection is that they appear
  nowhere else.
* Every listing-id-shaped UUID in a tracked text file is either visibly
  invented or copied from a captured page under tests/fixtures. A real car's
  id pasted into a test from the bot's own records is neither.
* The site is built exactly as the bot builds it - the vault's own publish
  over a copy of docs/ holding a fixture data.json - and every published file
  is searched for any car make, dollar figure, Canadian city or listing id.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote_plus

import pytest

from autotrader import vault as V

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
PHOTO = Path(__file__).resolve().parent / "fixtures" / "photo.webp"


# ============================================================ tracked files

#: Tracked paths the scan does not read, and why. Nothing else is skipped.
NOT_SCANNED = {
    "tests/fixtures/*.html": "captured pages; the parser tests need their real markup",
    "tests/fixtures/listings/*": "captured listing pages, kept byte for byte for the same reason",
    "config.json": "the bot's own data; it belongs in the vault",
    "state.json": "the bot's own data; it belongs in the vault",
    "docs/data.json": "the bot's own data; it belongs in the vault",
    "docs/events.json": "the bot's own data; it belongs in the vault",
    "docs/thumbs/*": "the bot's own photos; they belong in the vault",
    "EVENTS.md": "the bot's own ledger; it belongs in the vault",
    "NOTIFY.md": "the bot's own record; it belongs in the vault",
    "SOAK.md": "the bot's own record; it belongs in the vault",
    "validation-report.md": "the bot's own report; it belongs in the vault",
    "archives/*": "the bot's own archive; it belongs in the vault",
    "diagnostics/*": "the bot's own diagnostics; they belong in the vault",
}

SALT = b"autotrader/no-personal-data/"

#: salted SHA-256 of the lower-cased identity -> what it is. The label is
#: all a failure prints: printing the match would publish it in the CI log.
FORBIDDEN = {
    "0cd1a8195e4cb755ed51077ba691ac9b1ea6eb0081e07223df8dab707f214f17": "a watched model",
    "1019df15f9cde746d2d70987e8ef13ae8a53eb5360c811c89997d0d4d304d3b2": "a watched model",
    "f52dbec1ff433802c96337bec0ea90be968dd2ba4da86487c59618b92cebe49a": "a watched model",
    "957083522b704751a3de627b39f40aa631211c430302488ca0b4bd57522521b5": "a watched chassis code",
    "33b72e734d1f920de7ba8e69a6d2b14eb8c6c80a67d067500f57b526324aa9f7": "a watched chassis code",
    "02c29c1b408865d7220efceb6a4351790e3f6f41ef8bb1bcd44d81f386afa2ec": "a watched chassis code",
    "2ed88776610449cb67e1da5096ae3cba65ca296063c39677af8cb593a40f7bc7": "the city tag of a watched search link",
    "09c1df3f6219163d08f3cf07a344f67d12caf3042997739ba2ad477be89574cd": "the place typed into a watched search link",
    "efaef8bcfcbf2784e09f17ba34b05f9f2b5033e6665f5fe6b16eb4ad4937ecf8": "an old postcode area",
    "c1b381dfb0b190105f4dc0972104914844f467771265928c89a6a8d09c19f3b6": "the old notification topic",
    "e0405286e9b273cffa1e91e99a97e02d7fddb28e8c70137ebb6492223a92044f": "the owner's account",
    "eba0d16f1471c10502844209b6ebe7a2e4874d02138e2770ec0eb796b1e9d14d": "a watched model's site code",
    "4a2cac1a0abef2a890547b1f53a50e1aa0c729bb32977da4a06cbc0aaa90114a": "a watched model's site code",
    "b8e2fce12819efa81609a8d4c63be6626ace49bb2d865b947939ea1cd9164747": "a watched model's site code",
    "524b554400e692a80dbb7a06a271c3a1482e8764d2c6b94b3b5552904b930774": "the point a watched search link measures from",
    "2331cf19b8153d7f383b97510bf25b36f613ea32ab3f9688601229ef5ecb23b6": "the point a watched search link measures from",
    "071b3439cf5924b88969d4ca41f607d6b06aeb4123545f87923dccf776fb85cc": "the place a watched search measures from",
}


def digest(token: str) -> str:
    return hashlib.sha256(SALT + token.lower().encode("utf-8")).hexdigest()


# SVG path data is geometry: "M9 15h28" moves the pen; it names no car.
_SVG_PATH = re.compile(r"""\bd\s*=\s*(?:"[^"]*"|'[^']*')""")
# Nor is a CSS colour a chassis code.
_COLOUR = re.compile(r"#[0-9a-f]{3,8}\b")
_WORD = re.compile(r"[a-z0-9]+")
_JOINED = re.compile(r"[a-z0-9]+(?:[_-][a-z0-9]+)+")
_SETTING = re.compile(r"""([a-z_]+)"?\s*[=:]\s*"?([^&\s"'#;)}\]]+)""")
_SQUASHED_POSTCODE = re.compile(r"(?<![a-z0-9])([a-z]\d[a-z])\d[a-z]\d(?![a-z0-9])")
# A model with a trim fused onto it: "q7cs" is still a Q7.
_FUSED_TRIM = re.compile(r"([a-z]\d)[a-z]{2,4}")


def candidates(text: str) -> dict[str, int]:
    """Every form a forbidden identity could take in ``text``, with the
    first line it is on.

    A model on its own ("Q7") and with a one-letter suffix however it is
    joined ("Q7 Z", "Q7Z", "q7-z", "va_q7-z"), or with a trim fused onto it
    ("Q7CS"); a tagged or hyphenated name
    whole ("cit_exampleville", an ntfy topic, an account); a query parameter
    or setting as ``key=value`` ("zip=Exampleville", '"zip": "Exampleville"');
    and the area of a postcode written without its space.
    """
    found: dict[str, int] = {}
    for number, line in enumerate(text.lower().splitlines(), 1):
        line = _COLOUR.sub(" ", _SVG_PATH.sub(" ", line))
        words = _WORD.findall(line)
        tokens = set(words)
        tokens.update(a + b for a, b in zip(words, words[1:])
                      if len(a) <= 3 and len(b) == 1)
        tokens.update(_JOINED.findall(line))
        tokens.update(m.group(1) for w in words if (m := _FUSED_TRIM.fullmatch(w)))
        for key, value in _SETTING.findall(line):
            value = unquote_plus(value)
            first = re.split(r"[,\s]", value)[0]
            tokens.update({f"{key}={value}", f"{key}={first}"})
        tokens.update(_SQUASHED_POSTCODE.findall(line))
        for token in tokens:
            found.setdefault(token, number)
    return found


def leaks_in(text: str, forbidden: dict[str, str]) -> list[tuple[int, str]]:
    """(line, what) for each forbidden identity in ``text`` - never the word."""
    hits = ((line, forbidden.get(digest(token))) for token, line in candidates(text).items())
    return sorted((line, what) for line, what in hits if what)


def _excluded(path: str) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in NOT_SCANNED)


def _tracked() -> list[str]:
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                             capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout, so there is no list of what is published")
    return [p for p in out.decode("utf-8").split("\0") if p]


def _text(path: str) -> str | None:
    """The file as text, or None for a binary file or one not on disk."""
    try:
        blob = (ROOT / path).read_bytes()
    except OSError:
        return None
    if b"\0" in blob[:8192]:
        return None
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return None


def scanned() -> list[str]:
    return [p for p in _tracked() if not _excluded(p)]


_LISTING_ID = re.compile(
    r"(?i)(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"(?![0-9a-f])")
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def invented(listing_id: str) -> bool:
    """An id nobody could take for a real car's: a few digits, repeated.

    A random UUID uses a dozen or more of the sixteen hex digits; the ones
    tests make up ("00000000-0000-4000-8000-000000000001") use a handful.
    """
    return len(set(listing_id.replace("-", "").lower())) <= 6


def captured_ids() -> set[str]:
    """Every listing id on a captured page - public already, with the page."""
    import gzip
    pages = [p.read_text(encoding="utf-8", errors="replace")
             for p in FIXTURES.glob("*.html")]
    pages += [gzip.decompress(p.read_bytes()).decode("utf-8", errors="replace")
              for p in (FIXTURES / "listings").glob("*.html.gz")]
    return {m.lower() for page in pages for m in _LISTING_ID.findall(page)}


def stray_ids(text: str, captured: set[str]) -> list[int]:
    """Lines holding a listing id that is neither invented nor captured."""
    return sorted({number for number, line in enumerate(text.splitlines(), 1)
                   for m in _LISTING_ID.findall(line)
                   if not invented(m) and m.lower() not in captured})


class TestNoTrackedFileSaysWhatThisCopyWatches:

    def test_no_tracked_text_file_names_an_identity(self):
        found = []
        for path in scanned():
            text = _text(path)
            if text is not None:
                found += [f"{path}:{line}: {what}" for line, what in leaks_in(text, FORBIDDEN)]
        assert not found, ("tracked files name what a real watch here watches - "
                           "use an invented car and place instead:\n" + "\n".join(found))

    def test_every_listing_id_is_invented_or_from_a_captured_page(self):
        """A real car's id copied out of the bot's records says which cars
        this copy watched, as surely as the model does."""
        captured = captured_ids()
        found = []
        for path in scanned():
            text = _text(path)
            if text is not None:
                found += [f"{path}:{line}: a listing id from neither a captured "
                          f"page nor obviously invented" for line in stray_ids(text, captured)]
        assert not found, ("use an invented id, or one from a page under "
                           "tests/fixtures:\n" + "\n".join(found))

    def test_the_scan_reads_the_code_the_tests_and_the_documents(self):
        paths = set(scanned())
        for expected in ("README.md", "autotrader/urls.py", "tests/conftest.py",
                         "config.example.json", "tests/fixtures/qr-vectors.json"):
            assert expected in paths, expected

    @pytest.mark.parametrize("path", [
        "tests/test_no_personal_data.py", "tests/conftest.py", "autotrader/geo.py",
        "docs/app.js", "docs/index.html", "README.md", "config.example.json",
        "tests/fixtures/qr-vectors.json", "scripts/keep-time.sh",
        ".github/workflows/watch.yml",
    ])
    def test_the_exclusions_hold_only_data_and_captured_pages(self, path):
        assert not _excluded(path)

    def test_every_exclusion_says_why(self):
        assert all(reason.strip() for reason in NOT_SCANNED.values())

    def test_every_digest_is_a_digest_with_a_label(self):
        assert len(FORBIDDEN) == 17
        for key, what in FORBIDDEN.items():
            assert re.fullmatch(r"[0-9a-f]{64}", key) and what


class TestTheScanWouldCatchWhatItIsFor:
    """A privacy check that matches nothing passes forever.

    Invented identities of the same shapes go through the same code, each
    against its own digest.
    """

    @pytest.mark.parametrize("text,token", [
        ("Example Q7 Z, 2017 onwards", "q7z"),
        ("an Example Q7Z", "q7z"),
        ("/cars/example/q7/va_q7-z/reg_on/", "q7z"),
        ("Example Q7 2012-2016", "q7"),
        ("example-q7-2012-2016-e99-1a2b3c", "q7"),
        ("an Example Q7CS", "q7"),
        ("Example Q7 (E99)", "e99"),
        ("example-q7-2012-2016-e99-1a2b3c", "e99"),
        ("/cars/example/q7/reg_on/cit_exampleville/?zipr=500", "cit_exampleville"),
        ("?offer=U&zip=Exampleville&zipr=500", "zip=exampleville"),
        ("?zip=Exampleville%2C+ON&zipr=500", "zip=exampleville"),
        ('{"zip": "Exampleville"}', "zip=exampleville"),
        ('"near": "Exampleville, ON"', "near=exampleville"),
        ("?mcat=zz00gr000000&size=20", "zz00gr000000"),
        ("&lat=12.34567&lon=-98.76543", "lat=12.34567"),
        ('{"lon": -98.76543}', "lon=-98.76543"),
        ("within 150 km of K0Z 1A0", "k0z"),
        ("loc=K0Z1A0", "k0z"),
        ("https://ntfy.sh/autotrader-abcdefghjkmnpqrstuvw", "autotrader-abcdefghjkmnpqrstuvw"),
        ("https://some-one.github.io/watch/", "some-one"),
        ("git clone https://github.com/some-one/watch", "some-one"),
    ])
    def test_an_identity_is_found_however_it_is_written(self, text, token):
        stand_in = {digest(token): "a stand-in"}
        assert leaks_in(text, stand_in) == [(1, "a stand-in")], candidates(text)

    @pytest.mark.parametrize("text,token", [
        ('<path d="M9 15h28M6 15l2.4-7A3 3 0 0111.3 6h11.4"/>', "m9"),
        ("  <path d='M9 15h28'/>", "m9"),
        ("color: #e99;", "e99"),
        ("background: #e99f00", "e99"),
        ("0xe99 and e99f", "e99"),
        ("Example Q7 Z40i", "q7z"),
        ("Example Q70 Z", "q7z"),
        ("a q7u8 playlist", "q7"),
    ])
    def test_geometry_colours_and_other_models_are_not_identities(self, text, token):
        assert leaks_in(text, {digest(token): "a stand-in"}) == []

    def test_a_failure_names_the_file_and_line_but_not_the_word(self):
        """The CI log of a public repository is public too."""
        text = "fine\nExample Q7 Z\n"
        assert leaks_in(text, {digest("q7z"): "a stand-in"}) == [(2, "a stand-in")]

    def test_a_real_looking_listing_id_is_caught_and_a_made_up_one_is_not(self):
        import uuid
        real_looking = str(uuid.uuid5(uuid.NAMESPACE_URL, "a stand-in listing"))
        assert stray_ids(f'x = "{real_looking}"\n', set()) == [1]
        assert stray_ids(f'x = "{real_looking}"\n', {real_looking}) == []
        assert stray_ids('x = "00000000-0000-4000-8000-000000000001"', set()) == []
        assert stray_ids('x = "aaaaaaaa-1111-2222-3333-444444444444"', set()) == []

    def test_the_captured_pages_have_ids_to_allow(self):
        """Otherwise every id the parser tests name would read as stray."""
        assert len(captured_ids()) > 20

    def test_the_real_table_is_what_the_scan_consults(self):
        """No real identity is in this file's text, so no stand-in shares a
        digest with one - the stand-ins above test the code, not the table."""
        stand_ins = ("q7z", "q7", "e99", "cit_exampleville", "zip=exampleville", "k0z",
                     "near=exampleville", "zz00gr000000", "lat=12.34567",
                     "lon=-98.76543", "autotrader-abcdefghjkmnpqrstuvw", "some-one",
                     "m9")
        assert not {digest(t) for t in stand_ins} & set(FORBIDDEN)
        assert leaks_in(Path(__file__).read_text(encoding="utf-8"), FORBIDDEN) == []


# ========================================================= the published site

PHRASE = "a long enough test passphrase"

#: The makes a published file must never name.
MAKES = ("Acura", "Alfa Romeo", "Aston Martin", "Audi", "Bentley", "BMW", "Buick",
         "Cadillac", "Chevrolet", "Chrysler", "Dodge", "Ferrari", "Fiat", "Ford",
         "GMC", "Honda", "Hyundai", "Infiniti", "Jaguar", "Jeep", "Kia",
         "Lamborghini", "Land Rover", "Lexus", "Lincoln", "Maserati", "Mazda",
         "McLaren", "Mercedes", "Mitsubishi", "Nissan", "Polestar", "Porsche",
         "Rivian", "Subaru", "Tesla", "Toyota", "Volkswagen", "Volvo")

#: The cities a published file must never name.
CITIES = ("Toronto", "Montreal", "Montréal", "Vancouver", "Calgary", "Edmonton",
          "Ottawa", "Winnipeg", "Quebec City", "Hamilton", "Kitchener", "Halifax",
          "Victoria", "Saskatoon", "Regina", "Kelowna", "Mississauga", "Brampton",
          "Surrey", "Burnaby", "Richmond", "Laval", "Gatineau", "Markham", "Oshawa",
          "Windsor", "St. Catharines", "Barrie", "Sherbrooke", "Guelph", "Kingston",
          "Moncton", "Fredericton", "Charlottetown", "St. John's", "Whitehorse",
          "Yellowknife", "Iqaluit", "Abbotsford", "Coquitlam", "Nanaimo", "Kamloops",
          "Lethbridge", "Red Deer", "Sudbury", "Thunder Bay", "Oakville", "Burlington")


def _any_of(names) -> re.Pattern:
    return re.compile(r"(?i)(?<![a-z])(?:" + "|".join(re.escape(n) for n in names)
                      + r")(?![a-z])")


SITE_PATTERNS = {
    "a car make": _any_of(MAKES),
    "a Canadian city": _any_of(CITIES),
    "a dollar figure": re.compile(r"\$\s?\d[\d,]{2,}|\d{1,3}(?:[   ]\d{3})+\s?\$"),
    "a listing id": re.compile(
        r"(?i)(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
        r"(?![0-9a-f])"),
}

#: (published file, exact text) -> why that text is a deliberate, generic
#: example rather than anybody's data. Nothing on the page needs one today;
#: an entry that stops matching fails below, so the list cannot go stale.
ALLOWED: dict[tuple[str, str], str] = {}

#: The fixture's cars. Every value here is invented.
CARS = [
    # (id, city, province, price, hidden)
    ("00000000-0000-4000-8000-000000000001", "Toronto", "ON", 21000, False),
    ("00000000-0000-4000-8000-000000000002", "Ottawa", "ON", 26500, False),
    ("00000000-0000-4000-8000-000000000003", "Montreal", "QC", 48000, True),
]
SEARCH_NAME = "Honda Civic near Toronto"
SELLER = "Example Honda Dealer"


def site_findings(name: str, text: str) -> list[str]:
    found = []
    for what, pattern in SITE_PATTERNS.items():
        for match in pattern.finditer(text):
            if (name, match.group(0)) not in ALLOWED:
                found.append(f"{name}: {what}: {match.group(0)!r}")
    return found


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    """docs/ as a run leaves it - the page, a data.json, the ledger's
    events.json and a photo - published by the vault the way the bot does."""
    from autotrader.config import Config
    from autotrader.dashboard import build_payload
    from autotrader.listing import Listing
    from autotrader.state import State

    root = tmp_path_factory.mktemp("repo")
    with pytest.MonkeyPatch.context() as mp:
        # build_payload reads the photo index and the archive from the
        # working directory, which for the bot is its own checkout.
        mp.chdir(root)
        cfg = Config.defaults(root / "config.json")
        search = cfg.add_search(
            "https://www.autotrader.ca/cars/honda/civic/reg_on/cit_toronto/"
            "?zip=Toronto&zipr=500", SEARCH_NAME)
        cfg.set("filters.max_price", 40000)
        cfg.save()
        state = State(path=root / "state.json")
        for lid, city, province, price, hidden in CARS:
            state.record(Listing(
                id=lid, url=f"https://www.autotrader.ca/offers/honda-civic-{lid}",
                title="2021 Honda Civic Touring", year=2021, make="Honda",
                model="Civic", price=price, price_source="detail",
                search_id=search.id, location=city, province=province,
                seller=SELLER, mileage_km=42000),
                filtered=hidden,
                filter_reason=(f"price ${price:,} above maximum $40,000" if hidden else ""))
        state.record_run({"ok": True, "searches": 1, "listings": len(CARS)})
        payload = build_payload(cfg, state, {"GITHUB_REPOSITORY": "someone/watch"})

    first = CARS[0][0]
    payload["listings"][0]["thumb"] = f"thumbs/{first}.webp"
    payload["listings"][0]["thumbs"] = [f"thumbs/{first}.webp"]

    docs = root / "docs"
    shutil.copytree(DOCS, docs, ignore=shutil.ignore_patterns(
        "data.json", "events.json", "thumbs"))
    data = json.dumps(payload)
    (docs / "data.json").write_text(data, encoding="utf-8")
    (docs / "events.json").write_text(json.dumps(
        [{"title": "2021 Honda Civic Touring", "id": first, "city": "Toronto",
          "price": "$21,000"}]), encoding="utf-8")
    (docs / "thumbs").mkdir()
    shutil.copy(PHOTO, docs / "thumbs" / f"{first}.webp")

    vault = V.Vault.unlock(root, {V.ENV_KEY: PHRASE}, create=True)
    out = root / "site"
    vault.publish(docs, out)
    return {"out": out, "data": data, "vault": vault}


def _published(site) -> dict[str, bytes]:
    out = site["out"]
    return {p.relative_to(out).as_posix(): p.read_bytes()
            for p in sorted(out.rglob("*")) if p.is_file()}


class TestThePublishedSiteNamesNothing:

    def test_no_published_text_file_names_a_car_a_price_a_city_or_an_id(self, site):
        found = []
        for name, blob in _published(site).items():
            if b"\0" in blob[:8192]:
                continue
            try:
                text = blob.decode("utf-8")
            except UnicodeDecodeError:
                continue
            found += site_findings(name, text)
        assert not found, "\n".join(found)

    def test_no_published_file_holds_the_fixtures_plaintext(self, site):
        """Ciphertext is not text, so the patterns above cannot read it -
        but the fixture's own words and photo can be looked for, byte for
        byte, in every file including the sealed ones."""
        needles = {"Honda", "Civic", SEARCH_NAME, SELLER, "cit_toronto",
                   "Toronto", "Ottawa", "Montreal", "21,000", "26,500", "48,000",
                   "someone/watch", *(lid for lid, *_ in CARS)}
        photo = PHOTO.read_bytes()[:64]
        for name, blob in _published(site).items():
            for needle in needles:
                assert needle.encode("utf-8") not in blob, (name, needle)
            assert photo not in blob, name

    def test_no_published_name_is_a_listing_id(self, site):
        for name in _published(site):
            assert not SITE_PATTERNS["a listing id"].search(name), name

    def test_only_the_page_the_lock_and_ciphertext_are_published(self, site):
        """The ledger in docs/ holds plaintext too; it is left behind."""
        for name in _published(site):
            top = name.split("/")[0]
            assert (top in V.SITE_FILES or top.startswith("icon")
                    or top in {"data.enc", "lock.json"}
                    or (top == "thumbs" and name.endswith(".bin"))), name

    def test_the_patterns_find_all_four_in_the_plaintext_they_protect(self, site):
        """The fixture's data.json, before sealing, has every kind of thing
        the published files must not - so a pattern that matched nothing
        would fail here rather than pass above."""
        kinds = {line.split(": ")[1] for line in site_findings("data.json", site["data"])}
        assert kinds == set(SITE_PATTERNS), kinds

    def test_the_sealed_data_is_the_fixture(self, site):
        """And the ciphertext is the real payload, not an empty stand-in."""
        blob = (site["out"] / "data.enc").read_bytes()
        assert json.loads(V.unseal(site["vault"].key, blob, "data.json")) == \
            json.loads(site["data"])

    def test_every_allowance_is_still_needed(self, site):
        published = _published(site)
        for (name, text), reason in ALLOWED.items():
            assert reason.strip(), (name, text)
            assert text.encode("utf-8") in published.get(name, b""), (
                f"{name} no longer contains {text!r}; drop it from ALLOWED")
