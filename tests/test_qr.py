"""Unit tests for sketchgen.qr — the encoder, checked by decoding it back.

Run:  python3 -m unittest discover -s tests -v

There is no QR library in this repository to check the encoder against and
there is not going to be one (qr.md §1.2), so the check is the one a phone
makes: a **decoder**, written here from the standard, that takes the matrix
apart again. It reads the format information and checks its BCH code, rebuilds
the function-module map from the version alone, unmasks, walks the zigzag,
de-interleaves the blocks, computes every block's Reed–Solomon syndromes, and
then strips the mode, the count, the terminator and the padding to recover the
string that went in.

It is written to lean on ``sketchgen.qr`` as little as it reasonably can — its
own field, its own generator polynomials, its own block table, its own
reservation map — because a decoder built out of the encoder's helpers would
agree with a wrong generator polynomial, a wrong block split or a wrong
interleave just as happily as with a right one.

``tests/test_gallery.py`` imports :func:`decode` from here to read the two
files ``render_index`` writes into every entry's directory.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sketchgen import gallery  # noqa: E402
from sketchgen import qr  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"

#: Level M, versions 1–6, written out again rather than imported: this is the
#: table the encoder is being checked against.
#: ``version -> (total codewords, EC codewords per block, blocks)``
BLOCKS = {
    1: (26, 10, 1),
    2: (44, 16, 1),
    3: (70, 26, 1),
    4: (100, 18, 2),
    5: (134, 24, 2),
    6: (172, 16, 4),
}

#: The bits after the last codeword, which the encoder writes as zeroes.
REMAINDER = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7}

#: The single alignment centre of each version, or None for version 1.
ALIGNMENT = {1: None, 2: 18, 3: 22, 4: 26, 5: 30, 6: 34}

#: The URL the two golden fixtures hold, and the entry they belong to.
GOLDEN_ENTRY = 222


# ---------------------------------------------------------------------------
# GF(256), the decoder's own
# ---------------------------------------------------------------------------


def _field() -> tuple[list[int], list[int]]:
    exp = [0] * 512
    log = [0] * 256
    value = 1
    for power in range(255):
        exp[power] = value
        log[value] = power
        value = (value << 1) ^ (0x11D if value & 0x80 else 0)
    for power in range(255, 512):
        exp[power] = exp[power - 255]
    return exp, log


EXP, LOG = _field()


def gf_mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else EXP[LOG[a] + LOG[b]]


def syndromes(codewords: list[int], count: int) -> list[int]:
    """The ``count`` syndromes of one block: the codeword polynomial at 2^i.

    A block with no errors gives zeroes, whatever the message was. This is the
    check that a wrong generator polynomial cannot survive: the encoder's EC
    bytes would be the remainder against the wrong divisor, and evaluating at
    the true roots would come out non-zero.
    """
    out = []
    for i in range(count):
        root = EXP[i]
        acc = 0
        for byte in codewords:
            acc = gf_mul(acc, root) ^ byte
        out.append(acc)
    return out


# ---------------------------------------------------------------------------
# The function-module map, from the version alone
# ---------------------------------------------------------------------------


def reserved(version: int) -> list[list[bool]]:
    """Every module a scanner knows the place of before it reads anything.

    The three finders and their separators, both timing lines, the one
    alignment pattern, the dark module and both format-information areas. Data
    is everything else, and so is what the mask applies to.
    """
    size = 17 + 4 * version
    fixed = [[False] * size for _ in range(size)]

    def block(top: int, left: int, height: int, width: int) -> None:
        for row in range(top, top + height):
            for col in range(left, left + width):
                if 0 <= row < size and 0 <= col < size:
                    fixed[row][col] = True

    # Finders with their separators: the 8×8 corner each one occupies.
    block(0, 0, 8, 8)
    block(0, size - 8, 8, 8)
    block(size - 8, 0, 8, 8)
    # Timing.
    for at in range(size):
        fixed[6][at] = True
        fixed[at][6] = True
    # Alignment.
    centre = ALIGNMENT[version]
    if centre is not None:
        block(centre - 2, centre - 2, 5, 5)
    # Format information: the column beside the top-left finder and the row
    # under it are inside the 8×8 blocks above; these are the second copy's
    # strip plus the dark module.
    for at in range(size - 8, size):
        fixed[at][8] = True
    for at in range(size - 8, size):
        fixed[8][at] = True
    for at in range(9):
        fixed[8][at] = True
        fixed[at][8] = True
    return fixed


MASKS = (
    lambda row, col: (row + col) % 2 == 0,
    lambda row, col: row % 2 == 0,
    lambda row, col: col % 3 == 0,
    lambda row, col: (row + col) % 3 == 0,
    lambda row, col: (row // 2 + col // 3) % 2 == 0,
    lambda row, col: (row * col) % 2 + (row * col) % 3 == 0,
    lambda row, col: ((row * col) % 2 + (row * col) % 3) % 2 == 0,
    lambda row, col: ((row + col) % 2 + (row * col) % 3) % 2 == 0,
)


# ---------------------------------------------------------------------------
# The decoder
# ---------------------------------------------------------------------------


class Undecodable(AssertionError):
    """The matrix is not a readable code, and the message says where it fails."""


def format_positions(size: int) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Where bits 0..14 of each format copy live, as ``(row, col)``.

    Written out as two lists rather than as loops: the layout is the
    standard's and is not derivable from anything, so a restatement of it is
    the only honest form of a second opinion.
    """
    first = (
        [(at, 8) for at in range(6)]
        + [(7, 8), (8, 8), (8, 7)]
        + [(8, 14 - at) for at in range(9, 15)]
    )
    second = (
        [(8, size - 1 - at) for at in range(8)]
        + [(size - 15 + at, 8) for at in range(8, 15)]
    )
    return first, second


def read_format(modules: list[list[bool]], places: list[tuple[int, int]]) -> int:
    bits = 0
    for at, (row, col) in enumerate(places):
        if modules[row][col]:
            bits |= 1 << at
    return bits


def check_format(raw: int) -> tuple[int, int]:
    """Unmask, check the BCH(15,5) code, and return ``(level, mask)``.

    The check is a division rather than a table: the fifteen bits are a
    codeword of the BCH code generated by 0x537, so the remainder of the whole
    word by that polynomial is zero for a format nobody corrupted.
    """
    bits = raw ^ 0b101010000010010
    rem = bits
    for shift in range(14, 9, -1):
        if rem & (1 << shift):
            rem ^= 0x537 << (shift - 10)
    if rem != 0:
        raise Undecodable(f"format information fails its BCH check: {bits:015b}")
    return (bits >> 13) & 0b11, (bits >> 10) & 0b111


def read_codewords(modules: list[list[bool]], version: int, mask: int) -> list[int]:
    """Unmask and walk the zigzag: two columns at a time, column 6 skipped."""
    size = 17 + 4 * version
    fixed = reserved(version)
    condition = MASKS[mask]
    bits: list[int] = []
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        upward = ((right + 1) & 2) == 0
        for step in range(size):
            row = size - 1 - step if upward else step
            for col in (right, right - 1):
                if fixed[row][col]:
                    continue
                dark = modules[row][col]
                if condition(row, col):
                    dark = not dark
                bits.append(1 if dark else 0)
        right -= 2

    total = BLOCKS[version][0]
    if len(bits) != total * 8 + REMAINDER[version]:
        raise Undecodable(
            f"version {version} holds {total} codewords, but the zigzag gave "
            f"{len(bits)} bits"
        )
    tail = bits[total * 8:]
    if any(tail):
        raise Undecodable("the remainder bits are not all zero")
    return [
        int("".join(str(bit) for bit in bits[at:at + 8]), 2)
        for at in range(0, total * 8, 8)
    ]


def deinterleave(codewords: list[int], version: int) -> list[list[int]]:
    """The blocks, each as its data codewords followed by its EC codewords."""
    total, ec_per_block, count = BLOCKS[version]
    per_block = (total - ec_per_block * count) // count
    data: list[list[int]] = [[] for _ in range(count)]
    ec: list[list[int]] = [[] for _ in range(count)]
    at = 0
    for _ in range(per_block):
        for block in data:
            block.append(codewords[at])
            at += 1
    for _ in range(ec_per_block):
        for block in ec:
            block.append(codewords[at])
            at += 1
    assert at == len(codewords)
    return [data[i] + ec[i] for i in range(count)]


def decode(text_or_matrix) -> str:
    """An SVG document or a matrix in, the string the code holds out.

    Raises :class:`Undecodable` with the first thing that is wrong, so a
    failure names the step of the standard it failed at rather than printing
    two strings that differ.
    """
    modules = (
        matrix_from_svg(text_or_matrix)
        if isinstance(text_or_matrix, str)
        else text_or_matrix
    )
    size = len(modules)
    if any(len(row) != size for row in modules):
        raise Undecodable("the matrix is not square")
    if size < 21 or (size - 17) % 4:
        raise Undecodable(f"{size} is not a QR size")
    version = (size - 17) // 4
    if version not in BLOCKS:
        raise Undecodable(f"version {version} is outside the table")

    first, second = format_positions(size)
    raw_first = read_format(modules, first)
    raw_second = read_format(modules, second)
    if raw_first != raw_second:
        raise Undecodable("the two format copies disagree")
    level, mask = check_format(raw_first)
    if level != 0b00:
        raise Undecodable(f"level is {level:02b}, not M")

    codewords = read_codewords(modules, version, mask)
    for at, block in enumerate(deinterleave(codewords, version)):
        bad = syndromes(block, BLOCKS[version][1])
        if any(bad):
            raise Undecodable(f"block {at} has non-zero syndromes: {bad}")

    total, ec_per_block, count = BLOCKS[version]
    per_block = (total - ec_per_block * count) // count
    data: list[int] = []
    for block in deinterleave(codewords, version):
        data.extend(block[:per_block])

    bits = "".join(f"{byte:08b}" for byte in data)
    if bits[:4] != "0100":
        raise Undecodable(f"mode is {bits[:4]}, not byte mode")
    length = int(bits[4:12], 2)
    payload = bits[12:12 + length * 8]
    if len(payload) != length * 8:
        raise Undecodable(f"the count says {length} bytes and the code holds fewer")
    body = bytes(
        int(payload[at:at + 8], 2) for at in range(0, len(payload), 8)
    )

    # The terminator, the pad to the byte boundary and the alternating pad
    # bytes, all checked rather than skipped: padding is where a length bug
    # hides, because the string comes out right either way.
    rest = bits[12 + length * 8:]
    terminator = rest[:min(4, len(rest))]
    if terminator.strip("0"):
        raise Undecodable(f"the terminator is {terminator}, not zeroes")
    rest = rest[len(terminator):]
    boundary = -(12 + length * 8 + len(terminator)) % 8
    if rest[:boundary].strip("0"):
        raise Undecodable("the pad to the byte boundary is not zeroes")
    rest = rest[boundary:]
    for at in range(0, len(rest), 8):
        want = "11101100" if (at // 8) % 2 == 0 else "00010001"
        if rest[at:at + 8] != want:
            raise Undecodable(
                f"pad byte {at // 8} is {rest[at:at + 8]}, not {want}"
            )
    return body.decode("utf-8")


def matrix_from_svg(document: str) -> list[list[bool]]:
    """The modules back out of the SVG, quiet zone removed.

    Parsed rather than trusted: this is what makes the round trip a test of
    the *file* the gallery publishes and not only of ``encode()``.
    """
    box = re.search(r'viewBox="0 0 (\d+) \1"', document)
    if not box:
        raise Undecodable("no square viewBox")
    side = int(box.group(1))
    path = re.search(r'<path fill="#000" d="([^"]*)"/>', document)
    if not path:
        raise Undecodable("no path of dark modules")
    grid = [[False] * side for _ in range(side)]
    for x, y, width in re.findall(r"M(\d+) (\d+)h(\d+)v1h-\3z", path.group(1)):
        for col in range(int(x), int(x) + int(width)):
            grid[int(y)][col] = True
    # The quiet zone is four modules, so what is left is the code itself.
    quiet = 4
    return [row[quiet:side - quiet] for row in grid[quiet:side - quiet]]


# ---------------------------------------------------------------------------
# Version selection (§7)
# ---------------------------------------------------------------------------


class VersionTests(unittest.TestCase):

    def test_the_boundaries_of_every_version_in_the_table(self):
        # 14, 26, 42, 62, 84, 106 is the level-M byte capacity of versions
        # 1-6, so each of these pairs straddles one boundary.
        self.assertEqual(1, qr.version_for(14))
        self.assertEqual(2, qr.version_for(15))
        self.assertEqual(2, qr.version_for(26))
        self.assertEqual(3, qr.version_for(27))
        self.assertEqual(3, qr.version_for(42))
        self.assertEqual(4, qr.version_for(43))
        self.assertEqual(4, qr.version_for(62))
        self.assertEqual(5, qr.version_for(63))
        self.assertEqual(5, qr.version_for(84))
        self.assertEqual(6, qr.version_for(85))
        self.assertEqual(6, qr.version_for(106))

    def test_every_entry_url_this_gallery_can_have_is_version_four(self):
        # The budget of §1.5, as a number rather than as a thing to notice on
        # a wall: today's URLs are 52-55 bytes and version 4 at level M holds
        # 62, so a longer gallery_url or a six-digit entry id fails here.
        for length in (52, 54, 55):
            self.assertEqual(4, qr.version_for(length), length)
        # And the same three with ?kiosk on them, which is the six bytes §1.8
        # says the budget affords.
        for length in (58, 60, 61):
            self.assertEqual(4, qr.version_for(length), length)

    def test_both_of_the_gallery_s_own_payloads_are_version_four(self):
        config = gallery.Config()
        url = config.entry_url(GOLDEN_ENTRY)
        self.assertEqual(4, qr._encode(url)[1])
        self.assertEqual(4, qr._encode(url + "?kiosk")[1])
        self.assertEqual(33, len(qr.encode(url)))
        self.assertEqual(33, len(qr.encode(url + "?kiosk")))

    def test_one_byte_over_version_six_refuses_and_names_the_number(self):
        with self.assertRaises(qr.TooLong) as caught:
            qr.version_for(107)
        message = str(caught.exception)
        self.assertIn("107 bytes", message)
        self.assertIn("106", message)
        self.assertIn("gallery_url", message)
        # It is a ValueError, so it propagates out of render_index the way
        # Unsafe does rather than needing a handler of its own.
        self.assertIsInstance(caught.exception, ValueError)

    def test_encode_refuses_a_payload_that_does_not_fit(self):
        with self.assertRaises(qr.TooLong):
            qr.encode("x" * 107)

    def test_a_multibyte_character_costs_its_bytes_and_not_one(self):
        # Byte mode counts bytes, so 53 two-byte characters fill version 6
        # exactly and 54 of them do not fit at all. Our URLs are ASCII; this
        # is here so that a non-ASCII gallery_url fails loudly.
        self.assertEqual(6, qr._encode("é" * 53)[1])
        with self.assertRaises(qr.TooLong):
            qr.encode("é" * 54)


# ---------------------------------------------------------------------------
# The published tables (§2.2, §2.4)
# ---------------------------------------------------------------------------


class PublishedTableTests(unittest.TestCase):
    """The two numbers in this file that come from outside it.

    Everything else here is a decoder the same hand wrote as the encoder, so
    a shared misreading of the standard would survive every round trip.
    These do not: the format-information words and the Reed-Solomon generator
    polynomials are printed in the standard itself (Tables C.1 and A.1), and
    an encoder that disagrees with them is wrong however well it decodes.
    """

    #: Level M, masks 0-7, from the standard's Table C.1.
    FORMAT_M = (
        "101010000010010",
        "101000100100101",
        "101111001111100",
        "101101101001011",
        "100010111111001",
        "100000011001110",
        "100111110010111",
        "100101010100000",
    )

    #: The generator polynomials for the five EC lengths versions 1-6 at
    #: level M use, as the exponents of alpha the standard prints them in.
    GENERATORS = {
        10: [0, 251, 67, 46, 61, 118, 70, 64, 94, 32, 45],
        16: [0, 120, 104, 107, 109, 102, 161, 76, 3, 91, 191, 147, 169, 182,
             194, 225, 120],
        18: [0, 215, 234, 158, 94, 184, 97, 118, 170, 79, 187, 152, 148, 252,
             179, 5, 98, 96, 153],
        24: [0, 229, 121, 135, 48, 211, 117, 251, 126, 159, 180, 169, 152, 192,
             226, 228, 218, 111, 0, 117, 232, 87, 96, 227, 21],
        26: [0, 173, 125, 158, 2, 103, 182, 118, 17, 145, 201, 111, 28, 165,
             53, 161, 21, 245, 142, 13, 102, 48, 227, 153, 145, 218, 70],
    }

    def test_the_format_words_are_the_standard_s(self):
        for mask, want in enumerate(self.FORMAT_M):
            self.assertEqual(want, format(qr._format_bits(mask), "015b"), mask)

    def test_the_generator_polynomials_are_the_standard_s(self):
        # In this file's own field, which is built from 0x11D independently of
        # the encoder's: two wrong fields would have to be wrong the same way.
        for degree, want in self.GENERATORS.items():
            got = qr._generator_poly(degree)
            self.assertEqual(degree + 1, len(got))
            self.assertEqual(want, [LOG[coefficient] for coefficient in got], degree)

    def test_the_table_s_own_arithmetic_holds(self):
        # Data plus EC is the total, every block is the same length as its
        # fellows, and the byte capacity is what is left after mode and count.
        for version, (total, ec_per_block, blocks) in BLOCKS.items():
            self.assertEqual((total, ec_per_block, blocks), qr._TABLE[version])
            data = total - ec_per_block * blocks
            self.assertEqual(0, data % blocks, f"v{version} blocks are uneven")
            self.assertEqual((data * 8 - 12) // 8, qr._capacity(version))
        self.assertEqual(
            [14, 26, 42, 62, 84, 106],
            [qr._capacity(version) for version in range(1, 7)],
        )

    def test_the_free_modules_are_exactly_the_codewords_the_table_promises(self):
        # The strongest structural check there is, and one neither the
        # encoder nor the decoder can fake: the number of modules that are
        # not function patterns must equal the version's total codewords
        # times eight, plus its remainder bits. A reservation map that is one
        # module out anywhere fails this for every string.
        for version in range(1, 7):
            size = 17 + 4 * version
            free = sum(
                1
                for row in range(size)
                for col in range(size)
                if not reserved(version)[row][col]
            )
            total = BLOCKS[version][0]
            self.assertEqual(total * 8 + REMAINDER[version], free, f"v{version}")

    def test_the_encoder_reserves_the_same_modules_this_file_does(self):
        # Two reservation maps written from different descriptions — the
        # encoder draws the patterns, this file blocks out the rectangles they
        # occupy — agreeing module for module.
        for version in range(1, 7):
            grid = qr._Grid(version)
            qr._draw_function_patterns(grid)
            self.assertEqual(reserved(version), grid.fixed, f"v{version}")


# ---------------------------------------------------------------------------
# Structure (§7)
# ---------------------------------------------------------------------------


class StructureTests(unittest.TestCase):

    def matrix(self, version: int) -> list[list[bool]]:
        # The shortest string that lands in each version, so every row of the
        # table is actually built.
        want = {1: 1, 2: 15, 3: 27, 4: 43, 5: 63, 6: 85}[version]
        modules = qr.encode("a" * want)
        self.assertEqual(17 + 4 * version, len(modules))
        return modules

    def test_the_matrix_is_square_and_the_size_the_version_says(self):
        for version in range(1, 7):
            modules = self.matrix(version)
            size = 17 + 4 * version
            self.assertEqual(size, len(modules))
            for row in modules:
                self.assertEqual(size, len(row))

    def test_three_finders_with_their_separators_and_no_fourth(self):
        for version in range(1, 7):
            modules = self.matrix(version)
            size = len(modules)
            corners = [(0, 0), (0, size - 7), (size - 7, 0)]
            for top, left in corners:
                for dy in range(7):
                    for dx in range(7):
                        radius = max(abs(dy - 3), abs(dx - 3))
                        self.assertEqual(
                            radius in (0, 1, 3),
                            modules[top + dy][left + dx],
                            f"v{version} finder at {(top, left)} module {(dy, dx)}",
                        )
            # The separator: the light L on the inner sides of each.
            for at in range(8):
                self.assertFalse(modules[7][at], f"v{version} top-left separator")
                self.assertFalse(modules[at][7], f"v{version} top-left separator")
                self.assertFalse(modules[7][size - 1 - at])
                self.assertFalse(modules[at][size - 8])
                self.assertFalse(modules[size - 8][at])
                self.assertFalse(modules[size - 1 - at][7])
            # And the bottom-right corner is data, not a fourth finder: a
            # 7x7 of the finder's shape there would be a different symbol.
            bottom_right = [
                modules[size - 7 + dy][size - 7 + dx]
                for dy in range(7)
                for dx in range(7)
            ]
            finder = [
                max(abs(dy - 3), abs(dx - 3)) in (0, 1, 3)
                for dy in range(7)
                for dx in range(7)
            ]
            self.assertNotEqual(finder, bottom_right, f"v{version}")

    def test_the_timing_lines_alternate_from_the_finder_edge_inward(self):
        for version in range(1, 7):
            modules = self.matrix(version)
            size = len(modules)
            for at in range(8, size - 8):
                self.assertEqual(at % 2 == 0, modules[6][at], f"v{version} row 6")
                self.assertEqual(at % 2 == 0, modules[at][6], f"v{version} col 6")

    def test_the_alignment_pattern_is_there_from_version_two_and_not_before(self):
        # Version 1 has none at all; 2-6 have exactly one, at (N, N).
        modules = self.matrix(1)
        self.assertIsNone(ALIGNMENT[1])
        for version in range(2, 7):
            modules = self.matrix(version)
            centre = ALIGNMENT[version]
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    self.assertEqual(
                        max(abs(dy), abs(dx)) != 1,
                        modules[centre + dy][centre + dx],
                        f"v{version} alignment module {(dy, dx)}",
                    )

    def test_the_dark_module_is_where_the_standard_puts_it(self):
        for version in range(1, 7):
            modules = self.matrix(version)
            self.assertTrue(
                modules[4 * version + 9][8], f"v{version} dark module"
            )

    def test_both_format_copies_say_level_m_and_the_mask_the_encoder_chose(self):
        for version in range(1, 7):
            want = {1: 1, 2: 15, 3: 27, 4: 43, 5: 63, 6: 85}[version]
            modules, got_version, mask = qr._encode("a" * want)
            self.assertEqual(version, got_version)
            size = len(modules)
            first, second = format_positions(size)
            raw_first = read_format(modules, first)
            raw_second = read_format(modules, second)
            self.assertEqual(raw_first, raw_second, f"v{version} copies differ")
            level, read_mask = check_format(raw_first)
            self.assertEqual(0b00, level, "level M is 00")
            self.assertEqual(mask, read_mask, f"v{version}")


# ---------------------------------------------------------------------------
# Error correction and the round trip (§7)
# ---------------------------------------------------------------------------


#: Every length §7 names: the two sides of the version-1/2 boundary, a
#: single byte, a real entry URL, and the top of the two-block and four-block
#: layouts.
ROUND_TRIP_LENGTHS = (1, 13, 14, 15, 54, 84, 106)


class RoundTripTests(unittest.TestCase):

    def sample(self, length: int) -> str:
        # Not one repeated character: a run of identical bytes exercises none
        # of the interleave, because every block would hold the same thing.
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789-._~:/?#[]@!$&'()*+,;="
        return "".join(alphabet[at % len(alphabet)] for at in range(length))

    def test_every_block_s_syndromes_are_zero(self):
        # Independent of the encoder's own field and generator: if the
        # generator polynomial, the block split or the interleave were wrong,
        # evaluating the block at the true roots would not give zero.
        for length in ROUND_TRIP_LENGTHS:
            text = self.sample(length)
            modules, version, mask = qr._encode(text)
            codewords = read_codewords(modules, version, mask)
            blocks = deinterleave(codewords, version)
            self.assertEqual(BLOCKS[version][2], len(blocks))
            for at, block in enumerate(blocks):
                self.assertEqual(
                    [0] * BLOCKS[version][1],
                    syndromes(block, BLOCKS[version][1]),
                    f"{length} bytes, version {version}, block {at}",
                )

    def test_the_decoder_recovers_the_exact_input_at_every_boundary(self):
        for length in ROUND_TRIP_LENGTHS:
            text = self.sample(length)
            self.assertEqual(text, decode(qr.encode(text)), f"{length} bytes")

    def test_the_round_trip_survives_the_svg_as_well_as_the_matrix(self):
        for length in ROUND_TRIP_LENGTHS:
            text = self.sample(length)
            self.assertEqual(text, decode(qr.svg(text)), f"{length} bytes")

    def test_the_two_real_payloads_decode_to_themselves(self):
        url = gallery.Config().entry_url(GOLDEN_ENTRY)
        self.assertEqual(url, decode(qr.svg(url)))
        self.assertEqual(url + "?kiosk", decode(qr.svg(url + "?kiosk")))

    def test_a_flipped_module_is_caught_by_the_syndromes(self):
        # The decoder is only worth something if it can fail: one module in
        # the data region inverted must show up as a non-zero syndrome, or
        # every assertion above is checking nothing.
        text = self.sample(54)
        modules = [row[:] for row in qr.encode(text)]
        modules[20][20] = not modules[20][20]
        with self.assertRaises(Undecodable):
            decode(modules)


# ---------------------------------------------------------------------------
# The mask choice (§7)
# ---------------------------------------------------------------------------


class MaskTests(unittest.TestCase):

    def penalty(self, modules: list[list[bool]]) -> int:
        """The four rules, restated from the standard.

        Restated and not imported, for the same reason the decoder is: a
        penalty function that is the encoder's own cannot disagree with it.
        """
        size = len(modules)
        lines = [list(row) for row in modules]
        lines += [[modules[row][col] for row in range(size)] for col in range(size)]

        n1 = 0
        for line in lines:
            run = 1
            for at in range(1, size):
                if line[at] == line[at - 1]:
                    run += 1
                    continue
                if run >= 5:
                    n1 += 3 + run - 5
                run = 1
            if run >= 5:
                n1 += 3 + run - 5

        n2 = 0
        for row in range(size - 1):
            for col in range(size - 1):
                square = {
                    modules[row][col],
                    modules[row][col + 1],
                    modules[row + 1][col],
                    modules[row + 1][col + 1],
                }
                if len(square) == 1:
                    n2 += 3

        n3 = 0
        dark_first = [True, False, True, True, True, False, True] + [False] * 4
        light_first = [False] * 4 + [True, False, True, True, True, False, True]
        for line in lines:
            for at in range(size - 10):
                window = line[at:at + 11]
                if window == dark_first or window == light_first:
                    n3 += 40

        dark = sum(1 for row in modules for module in row if module)
        n4 = (abs(dark * 100 - size * size * 50) // (size * size) // 5) * 10
        return n1 + n2 + n3 + n4

    def test_the_encoder_picks_the_lowest_penalty_lowest_index_on_a_tie(self):
        for text in (
            "a",
            gallery.Config().entry_url(GOLDEN_ENTRY),
            gallery.Config().entry_url(GOLDEN_ENTRY) + "?kiosk",
            "x" * 106,
        ):
            payload = text.encode("utf-8")
            version = qr.version_for(len(payload))
            grid = qr._Grid(version)
            qr._draw_function_patterns(grid)
            qr._place_data(
                grid, qr._interleave(qr._data_codewords(payload, version), version)
            )
            scores = [self.penalty(qr._masked(grid, mask)) for mask in range(8)]
            want = scores.index(min(scores))
            self.assertEqual(want, qr._encode(text)[2], f"{text[:20]!r}: {scores}")

    def test_the_mask_is_the_one_the_format_information_announces(self):
        # A code masked with one pattern and labelled with another is
        # unreadable, and nothing else in this file would notice.
        for text in ("a", gallery.Config().entry_url(GOLDEN_ENTRY) + "?kiosk"):
            modules, _version, mask = qr._encode(text)
            first, _second = format_positions(len(modules))
            self.assertEqual(mask, check_format(read_format(modules, first))[1])


# ---------------------------------------------------------------------------
# Determinism and the document (§7, §3)
# ---------------------------------------------------------------------------


class DocumentTests(unittest.TestCase):

    def test_two_calls_give_identical_bytes(self):
        url = gallery.Config().entry_url(GOLDEN_ENTRY)
        self.assertEqual(qr.svg(url), qr.svg(url))
        self.assertEqual(qr.svg(url + "?kiosk"), qr.svg(url + "?kiosk"))

    def test_the_two_golden_fixtures_match_byte_for_byte(self):
        # A change to the encoder can never be accidental: these two files are
        # in the repository and this is what they are for.
        config = gallery.Config()
        url = config.entry_url(GOLDEN_ENTRY)
        clean = (DATA / f"qr-entry-{GOLDEN_ENTRY}.svg").read_text(encoding="utf-8")
        kiosk = (DATA / f"qr-entry-{GOLDEN_ENTRY}-kiosk.svg").read_text(encoding="utf-8")
        self.assertEqual(clean, qr.svg(url))
        self.assertEqual(kiosk, qr.svg(url + "?kiosk"))
        # They differ, which is also the test that the param reaches the
        # payload rather than being printed beside it and nowhere else.
        self.assertNotEqual(clean, kiosk)
        self.assertEqual(url, decode(clean))
        self.assertEqual(url + "?kiosk", decode(kiosk))

    def test_the_document_is_the_one_shape_section_three_fixes(self):
        document = qr.svg(gallery.Config().entry_url(GOLDEN_ENTRY))
        # A version-4 code is 33 modules and the quiet zone is four a side.
        self.assertIn('viewBox="0 0 41 41"', document)
        self.assertIn('width="41" height="41"', document)
        self.assertEqual(1, document.count("<rect"))
        self.assertEqual(1, document.count("<path"))
        self.assertIn('<rect width="41" height="41" fill="#fff"/>', document)
        self.assertIn('fill="#000"', document)
        self.assertIn('shape-rendering="crispEdges"', document)
        self.assertIn('aria-hidden="true"', document)
        self.assertTrue(document.endswith("</svg>\n"))

    def test_the_document_carries_no_script_no_style_and_no_url_text(self):
        # The code is read by a camera. There is nothing in the file for a
        # browser to run and nothing for the guard to find: the URL is in the
        # modules and appears nowhere as text.
        for text in (
            gallery.Config().entry_url(GOLDEN_ENTRY),
            gallery.Config().entry_url(GOLDEN_ENTRY) + "?kiosk",
        ):
            document = qr.svg(text)
            self.assertNotIn("<script", document)
            self.assertNotIn("<style", document)
            self.assertNotIn("<!DOCTYPE", document)
            self.assertNotIn("<!--", document)
            self.assertNotIn("https://", document)
            # §7 asks for no ``http://`` either. The SVG namespace is the one
            # exception and cannot be anything else — §3's own template has
            # it — so the check is that it is the only one, which is the
            # thing that rule is actually about: nothing in this file points
            # anywhere, and the encoded URL appears nowhere as text.
            self.assertEqual(1, document.count("http://"))
            self.assertIn('xmlns="http://www.w3.org/2000/svg"', document)
            self.assertNotIn("profcarroll", document)
            self.assertNotIn("kiosk", document)

    def test_the_quiet_zone_is_four_modules_of_white_on_every_side(self):
        modules = qr.encode(gallery.Config().entry_url(GOLDEN_ENTRY))
        document = qr.svg(gallery.Config().entry_url(GOLDEN_ENTRY))
        # Parsed back out, the border is empty and the middle is the code.
        grid = matrix_from_svg(document)
        self.assertEqual(modules, grid)

    def test_a_wider_quiet_zone_is_the_same_code_in_a_bigger_box(self):
        url = gallery.Config().entry_url(GOLDEN_ENTRY)
        wide = qr.svg(url, quiet=8)
        self.assertIn('viewBox="0 0 49 49"', wide)
        self.assertEqual(qr.encode(url), _strip(wide, 8))


def _strip(document: str, quiet: int) -> list[list[bool]]:
    box = re.search(r'viewBox="0 0 (\d+) \1"', document)
    side = int(box.group(1))
    grid = [[False] * side for _ in range(side)]
    path = re.search(r'<path fill="#000" d="([^"]*)"/>', document)
    for x, y, width in re.findall(r"M(\d+) (\d+)h(\d+)v1h-\3z", path.group(1)):
        for col in range(int(x), int(x) + int(width)):
            grid[int(y)][col] = True
    return [row[quiet:side - quiet] for row in grid[quiet:side - quiet]]


if __name__ == "__main__":
    unittest.main()
