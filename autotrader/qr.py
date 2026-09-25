"""A QR code encoder using only the standard library.

The dashboard shows the alert subscription address as a QR code, so a phone
can subscribe by pointing its camera instead of typing a long random topic.

The encoder is a couple of hundred lines of well-specified arithmetic, which
is cheaper to own than a dependency installed on every run.

A code nobody can scan looks exactly like one everybody can, so
``tests/test_qr.py`` compares the output module by module against reference
matrices from independent implementations (python-qrcode for the matrix,
segno's penalty rules for the mask, the zxing-cpp decoder to read it back),
frozen in ``tests/fixtures/qr-vectors.json`` so CI needs nothing installed.

Scope: byte mode, versions 1 to 10, all four error-correction levels, up to
271 bytes at level M. A longer payload raises rather than truncating.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- GF(256), the field Reed-Solomon works in -------------------------------
#
# Generated with the primitive polynomial x^8 + x^4 + x^3 + x^2 + 1 (0x11D),
# the one the QR specification names.

_EXP: list[int] = [0] * 512
_LOG: list[int] = [0] * 256


def _build_tables() -> None:
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_build_tables()


def _mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _generator(degree: int) -> list[int]:
    """The generator polynomial for `degree` error-correction codewords."""
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, coeff in enumerate(poly):
            nxt[j] ^= _mul(coeff, 1)
            nxt[j + 1] ^= _mul(coeff, _EXP[i])
        poly = nxt
    return poly


def _remainder(data: list[int], degree: int) -> list[int]:
    """The error-correction codewords for one block."""
    gen = _generator(degree)
    rest = list(data) + [0] * degree
    for i in range(len(data)):
        factor = rest[i]
        if factor == 0:
            continue
        for j, coeff in enumerate(gen):
            rest[i + j] ^= _mul(coeff, factor)
    return rest[len(data):]


# --- The specification's tables, versions 1 to 10 ---------------------------
#
# Per (version, level): total data codewords, error-correction codewords per
# block, block count in group 1, block count in group 2. Group 2's blocks hold
# one more data codeword than group 1's.

LEVELS = ("L", "M", "Q", "H")

_BLOCKS: dict[tuple[int, str], tuple[int, int, int, int]] = {
    (1, "L"): (19, 7, 1, 0),    (1, "M"): (16, 10, 1, 0),
    (1, "Q"): (13, 13, 1, 0),   (1, "H"): (9, 17, 1, 0),
    (2, "L"): (34, 10, 1, 0),   (2, "M"): (28, 16, 1, 0),
    (2, "Q"): (22, 22, 1, 0),   (2, "H"): (16, 28, 1, 0),
    (3, "L"): (55, 15, 1, 0),   (3, "M"): (44, 26, 1, 0),
    (3, "Q"): (34, 18, 2, 0),   (3, "H"): (26, 22, 2, 0),
    (4, "L"): (80, 20, 1, 0),   (4, "M"): (64, 18, 2, 0),
    (4, "Q"): (48, 26, 2, 0),   (4, "H"): (36, 16, 4, 0),
    (5, "L"): (108, 26, 1, 0),  (5, "M"): (86, 24, 2, 0),
    (5, "Q"): (62, 18, 2, 2),   (5, "H"): (46, 22, 2, 2),
    (6, "L"): (136, 18, 2, 0),  (6, "M"): (108, 16, 4, 0),
    (6, "Q"): (76, 24, 4, 0),   (6, "H"): (60, 28, 4, 0),
    (7, "L"): (156, 20, 2, 0),  (7, "M"): (124, 18, 4, 0),
    (7, "Q"): (88, 18, 2, 4),   (7, "H"): (66, 26, 4, 1),
    (8, "L"): (194, 24, 2, 0),  (8, "M"): (154, 22, 2, 2),
    (8, "Q"): (110, 22, 4, 2),  (8, "H"): (86, 26, 4, 2),
    (9, "L"): (232, 30, 2, 0),  (9, "M"): (182, 22, 3, 2),
    (9, "Q"): (132, 20, 4, 4),  (9, "H"): (100, 24, 4, 4),
    (10, "L"): (274, 18, 2, 2), (10, "M"): (216, 26, 4, 1),
    (10, "Q"): (154, 24, 6, 2), (10, "H"): (122, 28, 6, 2),
}

# Where the alignment patterns sit, per version. Version 1 has none.
_ALIGNMENT: dict[int, list[int]] = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46],
    10: [6, 28, 50],
}

MAX_VERSION = 10

# Total codewords (data plus error correction) and the leftover modules that
# carry no codeword, per version, from the specification's tables. Used only
# to check the skeleton against itself.
_CODEWORDS = {1: 26, 2: 44, 3: 70, 4: 100, 5: 134, 6: 172,
              7: 196, 8: 242, 9: 292, 10: 346}
_REMAINDER_BITS = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7,
                   7: 0, 8: 0, 9: 0, 10: 0}

# Format information, pre-computed: (level, mask) -> the 15 bits to place.
# BCH(15,5) with generator 0b10100110111, then XORed with 0b101010000010010.
_LEVEL_BITS = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}


def _format_bits(level: str, mask: int) -> int:
    value = (_LEVEL_BITS[level] << 3) | mask
    rest = value << 10
    for _ in range(5):
        if rest.bit_length() < 11:
            break
        rest ^= 0b10100110111 << (rest.bit_length() - 11)
    return ((value << 10) | rest) ^ 0b101010000010010


def _version_bits(version: int) -> int:
    """The 18-bit version block, only drawn from version 7 upwards."""
    rest = version << 12
    for _ in range(6):
        if rest.bit_length() < 13:
            break
        rest ^= 0b1111100100101 << (rest.bit_length() - 13)
    return (version << 12) | rest


class QRError(ValueError):
    """The payload will not fit, or the arguments make no sense."""


@dataclass(frozen=True)
class QRCode:
    """A finished code: `size` by `size` booleans, True where a module is dark."""

    version: int
    level: str
    mask: int
    modules: tuple[tuple[bool, ...], ...]

    @property
    def size(self) -> int:
        return len(self.modules)

    def __getitem__(self, rc: tuple[int, int]) -> bool:
        row, col = rc
        return self.modules[row][col]

    def rows(self) -> list[list[bool]]:
        return [list(row) for row in self.modules]

    def to_svg(self, *, quiet_zone: int = 4, dark: str = "#000000",
               light: str | None = None, label: str = "") -> str:
        """The code as an SVG string, one path, no external references.

        One path rather than one rect per module keeps the markup small.
        """
        if quiet_zone < 0:
            raise QRError("the quiet zone cannot be negative")
        n = self.size
        span = n + quiet_zone * 2
        parts: list[str] = []
        for r, row in enumerate(self.modules):
            c = 0
            while c < n:
                if not row[c]:
                    c += 1
                    continue
                run = 1
                while c + run < n and row[c + run]:
                    run += 1
                parts.append(f"M{c + quiet_zone} {r + quiet_zone}h{run}v1h-{run}z")
                c += run
        background = (f'<rect width="{span}" height="{span}" fill="{light}"/>'
                      if light else "")
        title = f"<title>{label}</title>" if label else ""
        role = ' role="img" aria-label="' + label + '"' if label else ' aria-hidden="true"'
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {span} {span}"'
            f'{role} shape-rendering="crispEdges">{title}{background}'
            f'<path fill="{dark}" d="{"".join(parts)}"/></svg>'
        )


def _capacity(version: int, level: str) -> int:
    """How many payload bytes fit, after mode and length overhead."""
    data_codewords = _BLOCKS[(version, level)][0]
    length_bits = 8 if version < 10 else 16
    return (data_codewords * 8 - 4 - length_bits) // 8


def _pick_version(length: int, level: str, minimum: int) -> int:
    for version in range(max(1, minimum), MAX_VERSION + 1):
        if length <= _capacity(version, level):
            return version
    raise QRError(
        f"{length} bytes will not fit in a version-{MAX_VERSION} code at level "
        f"{level} (the ceiling is {_capacity(MAX_VERSION, level)} bytes). "
        "Shorten the payload or use a lower error-correction level.")


def _bitstream(payload: bytes, version: int, level: str) -> list[int]:
    """Payload -> data codewords, padded to the version's capacity."""
    bits: list[int] = []

    def push(value: int, width: int) -> None:
        for i in range(width - 1, -1, -1):
            bits.append((value >> i) & 1)

    push(0b0100, 4)                                   # byte mode
    push(len(payload), 8 if version < 10 else 16)     # character count
    for byte in payload:
        push(byte, 8)

    total = _BLOCKS[(version, level)][0] * 8
    push(0, min(4, total - len(bits)))                # terminator
    while len(bits) % 8:
        bits.append(0)

    codewords = [int("".join(str(b) for b in bits[i:i + 8]), 2)
                 for i in range(0, len(bits), 8)]
    # The specification's pad bytes, alternating, until the block is full.
    for i in range(_BLOCKS[(version, level)][0] - len(codewords)):
        codewords.append(0xEC if i % 2 == 0 else 0x11)
    return codewords


def _interleave(codewords: list[int], version: int, level: str) -> list[int]:
    data_total, ec_per_block, group1, group2 = _BLOCKS[(version, level)]
    blocks = group1 + group2
    short = data_total // blocks
    if group2 and short * group1 + (short + 1) * group2 != data_total:
        raise QRError(f"block table is inconsistent for version {version}{level}")

    data_blocks: list[list[int]] = []
    at = 0
    for i in range(blocks):
        size = short + (1 if i >= group1 else 0)
        data_blocks.append(codewords[at:at + size])
        at += size
    ec_blocks = [_remainder(b, ec_per_block) for b in data_blocks]

    out: list[int] = []
    for i in range(max(len(b) for b in data_blocks)):
        for block in data_blocks:
            if i < len(block):
                out.append(block[i])
    for i in range(ec_per_block):
        for block in ec_blocks:
            out.append(block[i])
    return out


def _skeleton(version: int) -> tuple[list[list[int | None]], list[list[bool]]]:
    """An empty grid plus a map of which modules are function patterns."""
    n = version * 4 + 17
    grid: list[list[int | None]] = [[None] * n for _ in range(n)]
    fixed = [[False] * n for _ in range(n)]

    def put(r: int, c: int, value: int) -> None:
        grid[r][c] = value
        fixed[r][c] = True

    def finder(top: int, left: int) -> None:
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = top + r, left + c
                if not (0 <= rr < n and 0 <= cc < n):
                    continue
                inside = 0 <= r < 7 and 0 <= c < 7
                dark = inside and (r in (0, 6) or c in (0, 6)
                                   or (2 <= r <= 4 and 2 <= c <= 4))
                put(rr, cc, 1 if dark else 0)

    finder(0, 0)
    finder(0, n - 7)
    finder(n - 7, 0)

    for i in range(8, n - 8):                      # timing patterns
        put(6, i, 1 if i % 2 == 0 else 0)
        put(i, 6, 1 if i % 2 == 0 else 0)

    # Alignment patterns sit at every pairing of the version's centre
    # coordinates except the three that would land on a finder. The two
    # centred on a timing line, at (6, last) and (last, 6), do belong: the
    # timing run passes through them and agrees module for module.
    centres = _ALIGNMENT[version]
    if centres:
        first, last = centres[0], centres[-1]
        skip = {(first, first), (first, last), (last, first)}
        for r in centres:
            for c in centres:
                if (r, c) in skip:
                    continue
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        dark = max(abs(dr), abs(dc)) != 1
                        put(r + dr, c + dc, 1 if dark else 0)

    for i in range(9):                             # reserve the format area
        if not fixed[8][i]:
            put(8, i, 0)
        if not fixed[i][8]:
            put(i, 8, 0)
    for i in range(8):                             # eight along row 8...
        put(8, n - 1 - i, 0)
    for i in range(7):                             # ...and seven up column 8
        put(n - 1 - i, 8, 0)

    # The specification's always-dark module, just above the seven format
    # modules in column 8.
    put(n - 8, 8, 1)

    if version >= 7:                               # reserve the version area
        for i in range(6):
            for j in range(3):
                put(n - 11 + j, i, 0)
                put(i, n - 11 + j, 0)

    # Catches a mislaid function pattern. Every module outside one carries a
    # codeword bit or a remainder bit, so their count is fixed by the
    # specification; a skeleton one module out looks perfect and scans as
    # nothing.
    free = sum(1 for row in fixed for cell in row if not cell)
    expected = _CODEWORDS[version] * 8 + _REMAINDER_BITS[version]
    if free != expected:
        raise QRError(f"version {version} left {free} modules for data where "
                      f"the specification wants {expected} - a function "
                      f"pattern is in the wrong place")
    return grid, fixed


def _place(grid: list[list[int | None]], fixed: list[list[bool]],
           bits: list[int]) -> None:
    """Walk the zig-zag the specification describes, laying bits down."""
    n = len(grid)
    i = 0
    col = n - 1
    upward = True
    while col > 0:
        if col == 6:                               # skip the vertical timing line
            col -= 1
        rows = range(n - 1, -1, -1) if upward else range(n)
        for row in rows:
            for c in (col, col - 1):
                if fixed[row][c]:
                    continue
                grid[row][c] = bits[i] if i < len(bits) else 0
                i += 1
        upward = not upward
        col -= 2


def _masked(value: int, row: int, col: int, mask: int) -> int:
    if mask == 0:
        flip = (row + col) % 2 == 0
    elif mask == 1:
        flip = row % 2 == 0
    elif mask == 2:
        flip = col % 3 == 0
    elif mask == 3:
        flip = (row + col) % 3 == 0
    elif mask == 4:
        flip = (row // 2 + col // 3) % 2 == 0
    elif mask == 5:
        flip = (row * col) % 2 + (row * col) % 3 == 0
    elif mask == 6:
        flip = ((row * col) % 2 + (row * col) % 3) % 2 == 0
    elif mask == 7:
        flip = ((row + col) % 2 + (row * col) % 3) % 2 == 0
    else:
        raise QRError(f"there is no mask {mask}")
    return value ^ 1 if flip else value


def _draw_format(grid: list[list[int | None]], level: str, mask: int) -> None:
    """Write the 15 format bits into both of the places they live.

    Bit 0 is the least significant. Copy one runs down column 8 and then left
    along row 8; copy two runs left along row 8 from the right edge and then up
    column 8 from the bottom. With rows and columns swapped the code still
    looks plausible, but no scanner can tell which mask was applied.
    """
    n = len(grid)
    bits = _format_bits(level, mask)
    for i in range(15):
        bit = (bits >> i) & 1
        if i < 6:
            grid[i][8] = bit
        elif i == 6:
            grid[7][8] = bit
        elif i == 7:
            grid[8][8] = bit
        elif i == 8:
            grid[8][7] = bit
        else:
            grid[8][14 - i] = bit
        if i < 8:
            grid[8][n - 1 - i] = bit
        else:
            grid[n - 15 + i][8] = bit


def _draw_version(grid: list[list[int | None]], version: int) -> None:
    if version < 7:
        return
    n = len(grid)
    bits = _version_bits(version)
    for i in range(18):
        bit = (bits >> i) & 1
        grid[i // 3][n - 11 + i % 3] = bit
        grid[n - 11 + i % 3][i // 3] = bit


def _penalty(grid: list[list[int]]) -> int:
    """The specification's four penalty rules, used to choose a mask.

    Table 11 of ISO/IEC 18004. The mask with the lowest score wins, and the
    point of the exercise is a symbol a camera can actually find: large blocks
    of one colour, long runs, and anything resembling a fourth finder pattern
    all make a code harder to read.
    """
    n = len(grid)
    lines = [list(row) for row in grid] + [list(col) for col in zip(*grid)]
    score = 0

    # N1: every run of five or more of one colour, in any row or column.
    for line in lines:
        run = 1
        for i in range(1, n):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        if run >= 5:
            score += run - 2

    # N2: every two-by-two block of one colour.
    for r in range(n - 1):
        row, below = grid[r], grid[r + 1]
        for c in range(n - 1):
            if row[c] == row[c + 1] == below[c] == below[c + 1]:
                score += 3

    # N3: the 1:1:3:1:1 run that a scanner could mistake for a finder, with a
    # light area four modules wide on either side of it. "Light" means no dark
    # module in those four, and modules past the symbol's edge count as light
    # (the quiet zone is there), so a run at the edge always qualifies.
    core = [1, 0, 1, 1, 1, 0, 1]
    for line in lines:
        i = 0
        while True:
            try:
                i = next(j for j in range(i, n - 6) if line[j:j + 7] == core)
            except StopIteration:
                break
            before = line[max(i - 4, 0):i]
            after = line[i + 7:i + 11]
            if i in (0, n - 7) or not any(before) or not any(after):
                score += 40
                i += 7
            else:
                # Overlapping matches are possible: the last dark module of
                # one run can be the first of the next.
                i += 4

    # N4: how far the whole symbol is from half dark.
    dark = sum(sum(row) for row in grid)
    score += 10 * int(abs(dark * 100 / (n * n) - 50) / 5)
    return score


def encode(payload: str | bytes, *, level: str = "M",
           min_version: int = 1, mask: int | None = None) -> QRCode:
    """Encode `payload` as a QR code.

    `level` is the error-correction level: L, M, Q or H, in rising order of
    redundancy. M is the default because it survives a phone camera at an angle
    and still keeps the code small enough to scan from across a desk.
    """
    level = str(level).upper()
    if level not in LEVELS:
        raise QRError(f"error-correction level must be one of {', '.join(LEVELS)}")
    data = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    if not data:
        raise QRError("there is nothing to encode")
    version = _pick_version(len(data), level, min_version)

    bits: list[int] = []
    for codeword in _interleave(_bitstream(data, version, level), version, level):
        for i in range(7, -1, -1):
            bits.append((codeword >> i) & 1)

    grid, fixed = _skeleton(version)
    _place(grid, fixed, bits)

    best: tuple[int, int, list[list[int]]] | None = None
    candidates = range(8) if mask is None else [int(mask)]
    for candidate in candidates:
        trial = [[(value if fixed[r][c] else _masked(int(value), r, c, candidate))
                  for c, value in enumerate(row)] for r, row in enumerate(grid)]
        _draw_format(trial, level, candidate)
        _draw_version(trial, version)
        clean = [[int(v or 0) for v in row] for row in trial]
        score = _penalty(clean)
        if best is None or score < best[0]:
            best = (score, candidate, clean)

    assert best is not None
    _, chosen, final = best
    return QRCode(version=version, level=level, mask=chosen,
                  modules=tuple(tuple(bool(v) for v in row) for row in final))
