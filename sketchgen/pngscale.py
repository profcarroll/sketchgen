"""Shrink a PNG with the standard library, for a judge on a small box.

The gate's strip is four frames side by side, 5132×900 on the Flip, and Gemma
4's image encoder prices an image by its pixels: two strips did not finish on
the handheld's CPU in ten minutes, and on the Adreno (2026-10-09, patched
Turnip, encoder on Vulkan0) the attention matmuls still fall back to the CPU.
A judge that cannot answer is no judge, so the shim can shrink what it
forwards (:mod:`sketchgen.llamashim`, ``--max-image-px``), and this is the
shrinking: a box filter by an integer factor, which keeps thin lines visible
where nearest-neighbour would drop them, on 8-bit RGB and RGBA PNGs with any
of the five scanline filters, non-interlaced, which is what Chromium writes.

Pure Python over bytes is slow — about a second per megapixel on an A77 —
but it is seconds against minutes, and the strip on disk is untouched: what
the judge was shown is a smaller copy, and the shim says so in its log line.
Anything this cannot read (16-bit, palette, interlaced) is passed through as
it came, never rejected.
"""

from __future__ import annotations

import struct
import zlib

__all__ = ["downscale", "dimensions"]

_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunks(data: bytes):
    pos = 8
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        yield kind, body
        pos += 12 + length


def dimensions(data: bytes) -> tuple[int, int] | None:
    """``(width, height)`` of a PNG, or None for anything else."""
    if not data.startswith(_SIGNATURE) or len(data) < 24:
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> bytearray:
    stride = width * bpp
    out = bytearray(stride * height)
    prev = bytearray(stride)
    pos = 0
    for row in range(height):
        kind = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if kind == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif kind == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        elif kind != 0:
            raise ValueError(f"unknown PNG filter {kind}")
        out[row * stride:(row + 1) * stride] = line
        prev = line
    return out


def downscale(data: bytes, max_px: int) -> bytes:
    """The PNG shrunk by the smallest integer factor that puts its longer side
    at or under ``max_px``; the bytes unchanged when it already fits or cannot
    be read here."""
    dims = dimensions(data)
    if dims is None:
        return data
    width, height = dims
    factor = -(-max(width, height) // max_px) if max_px > 0 else 1
    if factor <= 1:
        return data
    ihdr = None
    idat = bytearray()
    for kind, body in _chunks(data):
        if kind == b"IHDR":
            ihdr = body
        elif kind == b"IDAT":
            idat += body
    if ihdr is None:
        return data
    depth, colour, _, _, interlace = struct.unpack(">BBBBB", ihdr[8:13])
    if depth != 8 or colour not in (2, 6) or interlace:
        return data
    bpp = 3 if colour == 2 else 4
    try:
        pixels = _unfilter(zlib.decompress(bytes(idat)), width, height, bpp)
    except (zlib.error, ValueError, IndexError):
        return data
    new_w, new_h = width // factor, height // factor
    if new_w < 1 or new_h < 1:
        return data
    stride = width * bpp
    area = factor * factor
    out = bytearray()
    for y in range(new_h):
        row = bytearray(1 + new_w * bpp)  # filter byte 0, then the pixels
        base_rows = [(y * factor + dy) * stride for dy in range(factor)]
        for x in range(new_w):
            x0 = x * factor * bpp
            for c in range(bpp):
                total = 0
                for r in base_rows:
                    start = r + x0 + c
                    total += sum(pixels[start:start + factor * bpp:bpp])
                row[1 + x * bpp + c] = total // area
        out += row
    new_ihdr = struct.pack(">IIBBBBB", new_w, new_h, 8, colour, 0, 0, 0)
    return (_SIGNATURE + _chunk(b"IHDR", new_ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(out), 6)) + _chunk(b"IEND", b""))


def _chunk(kind: bytes, body: bytes) -> bytes:
    return (struct.pack(">I", len(body)) + kind + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))
