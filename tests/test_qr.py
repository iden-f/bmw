"""The QR code is either scannable or it is a grey square nobody can use.

The whole risk of owning a QR encoder instead of installing one is that a
wrong code looks exactly like a right one. Nothing in the module can tell you
it is wrong, and neither can a person looking at the page.

So these vectors come from three independent things and none of this module:
python-qrcode, a widely used encoder, drew every symbol under every mask;
segno's implementation of the specification's penalty rules chose the mask;
and zxing-cpp, a real decoder, read the text back out of a rendering of every
one of them. All three need installing, so none runs here - the fixtures they
produced do, and a change that alters a single module fails by name.

Four real bugs were caught that way, all of which produced codes that looked
entirely plausible: the format bits were written with rows and columns
swapped, two alignment patterns were dropped in every code from version 7 up,
the mask-penalty rule that counts finder-lookalikes scored some lines twice
and others not at all, and the always-dark module was left light - which every
scanner forgives and the specification does not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autotrader import qr

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "qr-vectors.json").read_text())


def _rows(code: qr.QRCode) -> list[str]:
    return ["".join("#" if v else "." for v in row) for row in code.modules]


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_every_module_matches_a_code_a_real_decoder_read(vector):
    code = qr.encode(vector["text"], level=vector["level"], mask=vector["mask"])
    assert code.version == vector["version"]
    assert code.level == vector["level"]
    assert code.mask == vector["chosen_mask"]
    assert code.size == vector["size"]
    assert _rows(code) == vector["rows"]


def test_the_same_payload_always_gives_the_same_code():
    """No randomness anywhere: a republished page must not rewrite the QR."""
    payload = "https://ntfy.sh/autotrader-exampletopic23456789"
    assert _rows(qr.encode(payload)) == _rows(qr.encode(payload))


def test_the_skeleton_checks_itself():
    """Every version leaves exactly the modules the specification says it does.

    This is the assertion that caught the missing alignment patterns. It is
    cheap and it runs on every encode, because a function pattern in the wrong
    place shifts every data bit after it and produces a perfect-looking code
    that scans as nothing.
    """
    for version in range(1, qr.MAX_VERSION + 1):
        _, fixed = qr._skeleton(version)
        free = sum(1 for row in fixed for cell in row if not cell)
        assert free == qr._CODEWORDS[version] * 8 + qr._REMAINDER_BITS[version], version


def test_the_always_dark_module_is_dark():
    """ISO/IEC 18004 puts one dark module beside the bottom-left finder in
    every symbol. Scanners do not need it, which is how it went missing
    without a single code failing to scan."""
    for version in range(1, qr.MAX_VERSION + 1):
        code = qr.encode("x", min_version=version)
        assert code.version == version
        assert code.modules[4 * version + 9][8], version


def test_the_format_bits_are_the_ones_in_the_specification():
    """Table C.1 of ISO/IEC 18004, spot-checked."""
    assert format(qr._format_bits("L", 0), "015b") == "111011111000100"
    assert format(qr._format_bits("M", 0), "015b") == "101010000010010"
    assert format(qr._format_bits("Q", 0), "015b") == "011010101011111"
    assert format(qr._format_bits("H", 0), "015b") == "001011010001001"
    assert format(qr._format_bits("L", 1), "015b") == "111001011110011"


def test_the_version_bits_are_the_ones_in_the_specification():
    assert format(qr._version_bits(7), "018b") == "000111110010010100"
    assert format(qr._version_bits(10), "018b") == "001010010011010011"


def test_a_payload_that_will_not_fit_says_so():
    with pytest.raises(qr.QRError) as caught:
        qr.encode("x" * 5000)
    assert "will not fit" in str(caught.value)
    assert "Shorten the payload" in str(caught.value)


def test_nothing_to_encode_is_refused_rather_than_drawn():
    with pytest.raises(qr.QRError):
        qr.encode("")


def test_an_unknown_error_level_is_refused_by_name():
    with pytest.raises(qr.QRError) as caught:
        qr.encode("hello", level="Z")
    assert "L, M, Q, H" in str(caught.value)


def test_the_topic_url_fits_comfortably():
    """The one payload that actually matters, with room to spare.

    A longer topic must not silently push the code to a version a phone
    camera struggles with across a desk. Measured on a topic the bot really
    generates, so a change to their length is a change to this test.
    """
    from autotrader.provision import generate_topic
    code = qr.encode(f"https://ntfy.sh/{generate_topic()}", level="M")
    assert code.version <= 5
    assert code.size <= 37


class TestTheSvg:
    def test_it_is_one_path_and_needs_nothing_external(self):
        svg = qr.encode("https://ntfy.sh/topic").to_svg()
        assert svg.count("<path") == 1
        assert "http://www.w3.org/2000/svg" in svg
        # Nothing fetched, nothing scripted, nothing that can fail offline.
        for forbidden in ("<script", "<image", "xlink:href", "url("):
            assert forbidden not in svg

    def test_it_names_itself_when_given_a_label(self):
        svg = qr.encode("x").to_svg(label="Subscribe to alerts")
        assert 'role="img"' in svg
        assert "Subscribe to alerts" in svg

    def test_it_hides_itself_from_a_screen_reader_when_unlabelled(self):
        """A code a camera reads is furniture to a screen reader unless the
        page has given it words of its own."""
        assert 'aria-hidden="true"' in qr.encode("x").to_svg()

    def test_the_quiet_zone_is_part_of_the_picture(self):
        """Four modules of margin, because a code drawn flush to its edge is
        a code a scanner cannot find."""
        code = qr.encode("x")
        svg = code.to_svg(quiet_zone=4)
        assert f"0 0 {code.size + 8} {code.size + 8}" in svg

    def test_a_negative_quiet_zone_is_refused(self):
        with pytest.raises(qr.QRError):
            qr.encode("x").to_svg(quiet_zone=-1)

    def test_it_draws_in_the_colour_the_page_gives_it(self):
        assert 'fill="currentColor"' in qr.encode("x").to_svg(dark="currentColor")
