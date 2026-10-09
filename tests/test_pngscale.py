"""pngscale shrinks what Chromium writes and passes through what it cannot read."""

from __future__ import annotations

import struct
import sys
import unittest
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sketchgen import pngscale  # noqa: E402


def png(width, height, colour=6, depth=8, filters=(0,), interlace=0):
    bpp = 4 if colour == 6 else 3
    rows = bytearray()
    for y in range(height):
        kind = filters[y % len(filters)]
        line = bytes(((x * 7 + y * 13) & 255) for x in range(width * bpp))
        rows.append(kind)
        if kind == 0:
            rows += line
        elif kind == 1:
            rows += bytes(((line[i] - (line[i - bpp] if i >= bpp else 0)) & 255) for i in range(len(line)))
        elif kind == 2:
            prev = png.prev if y else bytes(len(line))
            rows += bytes(((line[i] - prev[i]) & 255) for i in range(len(line)))
        png.prev = line
    ihdr = struct.pack(">IIBBBBB", width, height, depth, colour, 0, 0, interlace)
    return (b"\x89PNG\r\n\x1a\n" + pngscale._chunk(b"IHDR", ihdr)
            + pngscale._chunk(b"IDAT", zlib.compress(bytes(rows))) + pngscale._chunk(b"IEND", b""))


class PngScaleTests(unittest.TestCase):
    def test_a_strip_shrinks_by_the_smallest_integer_factor_that_fits(self):
        data = png(5132, 90)
        small = pngscale.downscale(data, 1280)
        self.assertEqual(pngscale.dimensions(small), (1026, 18))
        self.assertLess(len(small), len(data))

    def test_an_image_that_fits_is_returned_as_is(self):
        data = png(640, 480)
        self.assertIs(pngscale.downscale(data, 1280), data)
        self.assertIs(pngscale.downscale(data, 0), data)

    def test_every_filter_type_decodes_to_the_same_pixels(self):
        plain = png(64, 16, filters=(0,))
        filtered = png(64, 16, filters=(1, 2, 0))
        self.assertEqual(pngscale.downscale(plain, 32), pngscale.downscale(filtered, 32))

    def test_rgb_works_too(self):
        self.assertEqual(pngscale.dimensions(pngscale.downscale(png(300, 100, colour=2), 100)), (100, 33))

    def test_what_it_cannot_read_passes_through(self):
        sixteen = png(300, 100, depth=16)
        self.assertIs(pngscale.downscale(sixteen, 100), sixteen)
        interlaced = png(300, 100, interlace=1)
        self.assertIs(pngscale.downscale(interlaced, 100), interlaced)
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 40
        self.assertIs(pngscale.downscale(jpeg, 100), jpeg)
        self.assertIsNone(pngscale.dimensions(jpeg))

    def test_the_box_filter_averages(self):
        # 2x2 of (0,0,0,255) and (255,255,255,255) checkered → one grey pixel.
        rows = bytes([0, 0, 0, 0, 255, 255, 255, 255, 255, 0, 255, 255, 255, 255, 0, 0, 0, 255])
        ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0)
        data = (b"\x89PNG\r\n\x1a\n" + pngscale._chunk(b"IHDR", ihdr)
                + pngscale._chunk(b"IDAT", zlib.compress(rows)) + pngscale._chunk(b"IEND", b""))
        small = pngscale.downscale(data, 1)
        self.assertEqual(pngscale.dimensions(small), (1, 1))
        idat = b"".join(body for kind, body in pngscale._chunks(small) if kind == b"IDAT")
        self.assertEqual(zlib.decompress(idat), bytes([0, 127, 127, 127, 255]))


if __name__ == "__main__":
    unittest.main()
