"""qr.py — a QR code of one short ASCII URL, drawn in Python (spec qr.md §2).

One job: a string in, a matrix of modules out, and an SVG document around it.
It does not know what a gallery is, it imports nothing but the stdlib, and it
is deterministic — no clock, no randomness, no dictionary iteration that is not
already ordered. Two runs of the same string give the same bytes, which is what
lets ``publish_index`` tell a re-render that changed nothing from one that did.

Why this file exists at all, rather than ``pip install qrcode``: the pipeline is
stdlib-only by design (``requirements.txt`` is Playwright alone, because the
gate is the referee), and the alternative to a local encoder is an ``<img
src="https://api.qrserver.com/…?data=…">`` — one request per published entry,
carrying the entry's identity, to a machine neither the gallery nor the viewer
controls, on a page whose whole claim is that it fetches nothing but its own
files. One encoder in one language is also one thing to test.

Byte mode, error-correction level M, versions 1 to 6, and nothing else:

* **Byte mode** because a URL is not numeric and not alphanumeric (it has
  lowercase letters, a colon and a slash). UTF-8 is what a scanner assumes for
  byte mode with no ECI block, and this writes no ECI block.
* **Level M** because the failure mode for a code on a wall is angular size and
  not damage. Q would put today's URL in version 5 and H in version 6 — more
  modules in the same tile, so smaller modules and a worse scan from the back
  of the room, buying redundancy against damage a backlit white tile cannot
  suffer. L is worse the other way: its version boundary falls at 53 bytes,
  inside the range of entry-page URLs, so the codes would change size as the
  slideshow advanced.
* **Stopping at 6** because version 7 is where a version-information block
  starts, and nothing here needs one. A payload over 106 bytes raises
  :class:`TooLong` rather than silently growing the table.

Python 3.12, stdlib only.
"""

from __future__ import annotations

__all__ = ["TooLong", "encode", "svg", "version_for"]


# ---------------------------------------------------------------------------
# The tables (spec §2.2)
# ---------------------------------------------------------------------------

#: Level M, versions 1–6: ``version -> (total codewords, EC per block, blocks)``.
#: Data codewords are ``total - ec_per_block * blocks``, and in every one of
#: these six rows they divide evenly among the blocks — the standard's "short
#: block / long block" split does not arise below version 7 at this level, so
#: nothing in this file implements it.
_TABLE = {
    #        total  ec/block  blocks
    1: (26, 10, 1),
    2: (44, 16, 1),
    3: (70, 26, 1),
    4: (100, 18, 2),
    5: (134, 24, 2),
    6: (172, 16, 4),
}

#: The bits left over after the codewords, written as zeroes (spec §2.2).
#: Version 1 has none; versions 2–6 have seven each.
_REMAINDER_BITS = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7}

#: The one alignment-pattern centre each version has, on both axes. The
#: standard gives versions 2–6 the coordinate pair ``{6, N}``; three of the
#: four combinations land on a finder, which leaves exactly one pattern, at
#: ``(N, N)``. Version 1 has none at all.
_ALIGNMENT_CENTRE = {1: None, 2: 18, 3: 22, 4: 26, 5: 30, 6: 34}

#: Level M's two format bits (spec §2.4). L is ``01``, M ``00``, Q ``11``,
#: H ``10`` — the indicator is not the ordering, which is why this is written
#: down rather than counted.
_LEVEL_BITS = 0b00

#: BCH(15,5) generator and the mask the standard XORs the format bits with, so
#: that an all-zero format is not an all-light block a scanner could mistake
#: for quiet zone.
_FORMAT_GENERATOR = 0x537
_FORMAT_MASK = 0b101010000010010

#: Byte mode's indicator, and its count field: 8 bits for every version 1–9.
_MODE_BYTE = 0b0100
_COUNT_BITS = 8

#: The two pad bytes, alternating, that fill a version's data capacity.
_PAD = (0xEC, 0x11)


class TooLong(ValueError):
    """The text does not fit in version 6 at level M."""


# ---------------------------------------------------------------------------
# GF(256), built once at import
# ---------------------------------------------------------------------------

#: The QR field: primitive polynomial x^8+x^4+x^3+x^2+1, generator element 2.
_PRIMITIVE = 0x11D

_EXP = [0] * 512
_LOG = [0] * 256


def _build_field() -> None:
    value = 1
    for power in range(255):
        _EXP[power] = value
        _LOG[value] = power
        value <<= 1
        if value & 0x100:
            value ^= _PRIMITIVE
    # A second lap of the table so that a product of two logs, which can reach
    # 508, indexes it without a modulo on every multiply.
    for power in range(255, 512):
        _EXP[power] = _EXP[power - 255]


_build_field()


def _mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _generator_poly(degree: int) -> list[int]:
    """The product of ``(x - 2^i)`` for ``i`` in ``0..degree-1``.

    Coefficients highest power first, monic, so the leading 1 is implied by
    the division below rather than carried around.
    """
    poly = [1]
    for power in range(degree):
        # Multiply by (x - 2^power); in this field subtraction is XOR.
        root = _EXP[power]
        nxt = poly + [0]
        for at in range(len(poly)):
            nxt[at + 1] ^= _mul(poly[at], root)
        poly = nxt
    return poly


def _remainder(data: list[int], degree: int) -> list[int]:
    """The EC codewords for one block: ``data`` shifted by ``degree``, mod g."""
    gen = _generator_poly(degree)
    rem = [0] * degree
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        for at in range(degree):
            rem[at] ^= _mul(gen[at + 1], factor)
    return rem


# ---------------------------------------------------------------------------
# The data (spec §2.1)
# ---------------------------------------------------------------------------


def _capacity(version: int) -> int:
    """How many data *bytes* a version holds at level M."""
    total, ec_per_block, blocks = _TABLE[version]
    data_codewords = total - ec_per_block * blocks
    # Four bits of mode and eight of count come off the top; whatever is left
    # over after the last whole byte cannot hold another one.
    return (data_codewords * 8 - 4 - _COUNT_BITS) // 8


def version_for(length: int) -> int:
    """The smallest version 1–6 whose level-M byte capacity holds ``length``."""
    for version in sorted(_TABLE):
        if length <= _capacity(version):
            return version
    raise TooLong(
        f"the URL is {length} bytes; version 6 at level M holds "
        f"{_capacity(max(_TABLE))}. Extend the table in qr.py to version 7 "
        "(which needs the version-information block) or shorten gallery_url."
    )


def _data_codewords(payload: bytes, version: int) -> list[int]:
    """Mode, count, the bytes, a terminator and the padding, as codewords."""
    total, ec_per_block, blocks = _TABLE[version]
    want = total - ec_per_block * blocks

    bits: list[int] = []

    def put(value: int, width: int) -> None:
        for shift in range(width - 1, -1, -1):
            bits.append((value >> shift) & 1)

    put(_MODE_BYTE, 4)
    put(len(payload), _COUNT_BITS)
    for byte in payload:
        put(byte, 8)

    # Up to four zeroes, stopping at the capacity: a terminator that would run
    # off the end is simply shorter.
    bits.extend([0] * min(4, want * 8 - len(bits)))
    # Then to a byte boundary, also with zeroes.
    bits.extend([0] * (-len(bits) % 8))

    codewords = [
        int("".join(str(bit) for bit in bits[at:at + 8]), 2)
        for at in range(0, len(bits), 8)
    ]
    # And the alternating pad bytes, to the version's data capacity exactly.
    at = 0
    while len(codewords) < want:
        codewords.append(_PAD[at % 2])
        at += 1
    return codewords


def _interleave(data: list[int], version: int) -> list[int]:
    """Data codeword *i* of every block, then *i+1*; then the same over EC.

    Every block in versions 1–6 at level M is the same length as its fellows
    (see ``_TABLE``), so this is a transpose and not the ragged walk the
    standard describes for the layouts that mix short and long blocks.
    """
    _total, ec_per_block, blocks = _TABLE[version]
    per_block = len(data) // blocks
    chunks = [data[at * per_block:(at + 1) * per_block] for at in range(blocks)]
    ec = [_remainder(chunk, ec_per_block) for chunk in chunks]

    out: list[int] = []
    for at in range(per_block):
        out.extend(chunk[at] for chunk in chunks)
    for at in range(ec_per_block):
        out.extend(block[at] for block in ec)
    return out


# ---------------------------------------------------------------------------
# The matrix (spec §2.3)
# ---------------------------------------------------------------------------


class _Grid:
    """A matrix under construction: the modules, and which of them are fixed.

    ``fixed`` is the reservation the standard calls function patterns — the
    finders, the timing lines, the alignment pattern, the dark module and both
    format-information areas. Data is written into everything else, and the
    mask is applied to everything else, which is the same set.
    """

    def __init__(self, version: int) -> None:
        self.version = version
        self.size = 17 + 4 * version
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.fixed = [[False] * self.size for _ in range(self.size)]

    def set(self, row: int, col: int, dark: bool, *, fix: bool = True) -> None:
        if 0 <= row < self.size and 0 <= col < self.size:
            self.modules[row][col] = dark
            if fix:
                self.fixed[row][col] = True


def _draw_finder(grid: _Grid, row: int, col: int) -> None:
    """One 7×7 finder, from its centre, and its light separator around it.

    Drawn as the 9×9 block centred on the pattern and clipped by
    ``_Grid.set``, so the two sides of the separator that fall outside the
    matrix cost no special case. The pattern is rings by Chebyshev radius from
    the centre: 0 and 1 are the dark 3×3 core, 2 is the light ring, 3 is the
    dark outer ring, and 4 is the separator.
    """
    for dy in range(-4, 5):
        for dx in range(-4, 5):
            radius = max(abs(dy), abs(dx))
            grid.set(row + dy, col + dx, radius in (0, 1, 3))


def _draw_alignment(grid: _Grid, centre: int) -> None:
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            grid.set(centre + dy, centre + dx, max(abs(dy), abs(dx)) != 1)


def _draw_function_patterns(grid: _Grid) -> None:
    size = grid.size

    # 1. Timing: row 6 and column 6, alternating, dark on the even indices.
    #    Drawn the whole way across and then partly overwritten by the finders
    #    below, which is the standard's own order: the timing line is only a
    #    timing line between the finders, and the eight modules at each end of
    #    it belong to the pattern that sits there.
    for at in range(size):
        dark = at % 2 == 0
        grid.set(6, at, dark)
        grid.set(at, 6, dark)

    # 2. The three finders, with their separators.
    _draw_finder(grid, 3, 3)
    _draw_finder(grid, 3, size - 4)
    _draw_finder(grid, size - 4, 3)

    # 3. The alignment pattern, at the single centre this version names. Below
    #    version 7 there is at most one, and it never collides with a finder.
    centre = _ALIGNMENT_CENTRE[grid.version]
    if centre is not None:
        _draw_alignment(grid, centre)

    # 4. The dark module, always.
    grid.set(4 * grid.version + 9, 8, True)

    # 5. Both format-information areas, reserved with zeroes: the real bits go
    #    in after the mask is chosen, but the data placement has to know these
    #    modules are spoken for before it starts.
    _draw_format(grid, 0)


def _format_bits(mask: int) -> int:
    """The 15 bits both format copies carry: level, mask, BCH and the XOR."""
    data = (_LEVEL_BITS << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * _FORMAT_GENERATOR)
    return ((data << 10) | rem) ^ _FORMAT_MASK


def _draw_format(grid: _Grid, mask: int) -> None:
    """Write both copies. Bit 0 is the least significant of the fifteen.

    The layout is the standard's and is not derivable from anything: the first
    copy wraps the top-left finder, the second is split between the row under
    the top-right finder and the column beside the bottom-left one.
    """
    bits = _format_bits(mask)
    size = grid.size

    def bit(at: int) -> bool:
        return bool((bits >> at) & 1)

    # First copy, around the top-left finder.
    for at in range(6):
        grid.set(at, 8, bit(at))
    grid.set(7, 8, bit(6))
    grid.set(8, 8, bit(7))
    grid.set(8, 7, bit(8))
    for at in range(9, 15):
        grid.set(8, 14 - at, bit(at))

    # Second copy: along row 8 from the right edge inward, then down column 8
    # from just under the bottom-left finder. It stops one short of the dark
    # module, which is why that is not overwritten here.
    for at in range(8):
        grid.set(8, size - 1 - at, bit(at))
    for at in range(8, 15):
        grid.set(size - 15 + at, 8, bit(at))


def _place_data(grid: _Grid, codewords: list[int]) -> None:
    """The zigzag: two columns at a time from the bottom-right, skipping 6.

    Column 6 is the vertical timing line and is not a data column at all —
    stepping over it rather than through it is what keeps every column pair
    two wide.
    """
    size = grid.size
    bits = [(byte >> shift) & 1 for byte in codewords for shift in range(7, -1, -1)]
    # The remainder bits, written as zeroes to fill the last modules.
    bits.extend([0] * _REMAINDER_BITS[grid.version])
    at = 0
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        upward = ((right + 1) & 2) == 0
        for step in range(size):
            row = size - 1 - step if upward else step
            for col in (right, right - 1):
                if grid.fixed[row][col]:
                    continue
                grid.modules[row][col] = at < len(bits) and bits[at] == 1
                at += 1
        right -= 2


# ---------------------------------------------------------------------------
# Mask and penalty (spec §2.4)
# ---------------------------------------------------------------------------

#: The eight mask conditions, in their numbered order. A module is inverted
#: where its condition is true, and only where it is not a function module.
_MASKS = (
    lambda row, col: (row + col) % 2 == 0,
    lambda row, col: row % 2 == 0,
    lambda row, col: col % 3 == 0,
    lambda row, col: (row + col) % 3 == 0,
    lambda row, col: (row // 2 + col // 3) % 2 == 0,
    lambda row, col: (row * col) % 2 + (row * col) % 3 == 0,
    lambda row, col: ((row * col) % 2 + (row * col) % 3) % 2 == 0,
    lambda row, col: ((row + col) % 2 + (row * col) % 3) % 2 == 0,
)

#: The finder-like run the N3 rule hunts for, dark-first and light-first, with
#: the four light modules on one side. Written as the eleven modules they are
#: rather than as a ratio, because a ratio has to be measured and eleven
#: booleans can simply be compared.
_N3_DARK_FIRST = (1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0)
_N3_LIGHT_FIRST = (0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1)


def _penalty(modules: list[list[bool]]) -> int:
    size = len(modules)
    score = 0

    lines: list[list[int]] = []
    for row in range(size):
        lines.append([1 if modules[row][col] else 0 for col in range(size)])
    for col in range(size):
        lines.append([1 if modules[row][col] else 0 for row in range(size)])

    # N1: every run of five or more same-colour modules, in a row or a column.
    for line in lines:
        run = 1
        for at in range(1, size):
            if line[at] == line[at - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)

    # N3: the finder-like pattern, in either direction, in rows and columns.
    for line in lines:
        for at in range(size - 10):
            window = tuple(line[at:at + 11])
            if window in (_N3_DARK_FIRST, _N3_LIGHT_FIRST):
                score += 40

    # N2: every 2×2 block of one colour, counted at every overlap.
    for row in range(size - 1):
        for col in range(size - 1):
            first = modules[row][col]
            if (
                modules[row][col + 1] == first
                and modules[row + 1][col] == first
                and modules[row + 1][col + 1] == first
            ):
                score += 3

    # N4: ten for each five percent the dark proportion departs from half —
    # the standard's "50 ± (5 × k)%", so a symbol at exactly 55% dark scores
    # one step and not none. In integers, to keep the score exact.
    dark = sum(1 for row in modules for module in row if module)
    total = size * size
    score += (abs(dark * 100 - total * 50) // total // 5) * 10
    return score


# ---------------------------------------------------------------------------
# The public functions
# ---------------------------------------------------------------------------


def _masked(grid: _Grid, mask: int) -> list[list[bool]]:
    """A copy of the grid with one mask applied and that mask's format bits.

    The format modules are part of what a scanner reads, so they are part of
    what gets scored: a candidate without them would be penalised for a
    neighbourhood the finished code does not have.
    """
    candidate = [row[:] for row in grid.modules]
    condition = _MASKS[mask]
    for row in range(grid.size):
        for col in range(grid.size):
            if not grid.fixed[row][col] and condition(row, col):
                candidate[row][col] = not candidate[row][col]
    written = _Grid(grid.version)
    written.modules = candidate
    written.fixed = grid.fixed
    _draw_format(written, mask)
    return candidate


def _encode(text: str) -> tuple[list[list[bool]], int, int]:
    """:func:`encode`, and the version and mask it chose.

    The two extra numbers are what the tests check the code's own format
    information against; nothing in the gallery needs them.
    """
    payload = text.encode("utf-8")
    version = version_for(len(payload))
    codewords = _interleave(_data_codewords(payload, version), version)

    grid = _Grid(version)
    _draw_function_patterns(grid)
    _place_data(grid, codewords)

    # All eight masks scored, lowest penalty wins, ties to the lowest number,
    # so the choice is total and the file is reproducible.
    best: list[list[bool]] | None = None
    best_score = 0
    best_mask = 0
    for mask in range(8):
        candidate = _masked(grid, mask)
        score = _penalty(candidate)
        if best is None or score < best_score:
            best, best_score, best_mask = candidate, score, mask
    assert best is not None  # eight masks always produce one
    return best, version, best_mask


def encode(text: str) -> list[list[bool]]:
    """The modules, row-major, ``True`` for dark, no quiet zone."""
    return _encode(text)[0]


def svg(text: str, quiet: int = 4) -> str:
    """``encode()`` as an SVG document (spec §3).

    One user unit per module, a white background rectangle and one ``<path>``
    of horizontal runs. Not one ``<rect>`` per module: a version-4 code is
    1,089 modules and a rect apiece is a 20 KB file the browser lays out on
    every entry page in the gallery.

    The white rectangle is not decoration. The kiosk's ground is black, a
    transparent code on black is an inverted code, scanners differ on whether
    they will read one, and a projector's contrast makes the gamble worse. The
    two colours are literal rather than theme tokens for the same reason: this
    file is read by a camera, not by a stylesheet.
    """
    modules = encode(text)
    side = len(modules) + 2 * quiet

    runs: list[str] = []
    for row, line in enumerate(modules):
        col = 0
        while col < len(line):
            if not line[col]:
                col += 1
                continue
            start = col
            while col < len(line) and line[col]:
                col += 1
            width = col - start
            runs.append(f"M{start + quiet} {row + quiet}h{width}v1h-{width}z")

    return (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {side} {side}" width="{side}" height="{side}" '
        'shape-rendering="crispEdges" role="img" aria-hidden="true">'
        f'<rect width="{side}" height="{side}" fill="#fff"/>'
        f'<path fill="#000" d="{"".join(runs)}"/>'
        "</svg>\n"
    )
