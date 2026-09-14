"""
VFP: Draws the Terminal Ludik icon — a white L and two price levels on black — into assets/ludik.ico.
Changes when: the icon design changes.
Anti-goal:
1. Image libraries — a few rectangles in 32-bit BMP icon frames need nothing but the standard library.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZES = (16, 32, 48, 256)
OUT = Path(__file__).resolve().parents[2] / "assets" / "ludik.ico"

# Rectangles in fractions of the side: (left, top, right, bottom).
BORDER = 0.04
SHAPES = (
    (0.20, 0.18, 0.34, 0.82),  # L stem
    (0.20, 0.68, 0.52, 0.82),  # L foot
    (0.48, 0.18, 0.66, 0.28),  # higher price on one exchange
    (0.64, 0.40, 0.82, 0.50),  # lower price on the other: the gap between them
)
WHITE = (255, 255, 255, 255)
BLACK = (0, 0, 0, 255)
GREY = (90, 90, 90, 255)


def pixels(size: int) -> list[list[tuple[int, int, int, int]]]:
    rows = []
    border = max(1, round(size * BORDER))
    for y in range(size):
        row = []
        for x in range(size):
            color = BLACK
            if x < border or y < border or x >= size - border or y >= size - border:
                color = GREY
            for left, top, right, bottom in SHAPES:
                if round(left * size) <= x < round(right * size) and round(top * size) <= y < round(bottom * size):
                    color = WHITE
            row.append(color)
        rows.append(row)
    return rows


def bmp_frame(size: int) -> bytes:
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    body = bytearray()
    for row in reversed(pixels(size)):  # bitmaps are stored bottom-up
        for red, green, blue, alpha in row:
            body += bytes((blue, green, red, alpha))
    mask_row = ((size + 31) // 32) * 4
    body += bytes(mask_row * size)  # fully opaque; alpha carries transparency
    return header + bytes(body)


def png_frame(size: int) -> bytes:
    """Windows reads PNG-compressed frames; the 256 px frame would be ~260 KB as a bitmap."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"".join(bytes(pixel) for pixel in row) for row in pixels(size))
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def main() -> None:
    frames = [png_frame(size) if size >= 256 else bmp_frame(size) for size in SIZES]
    offset = 6 + 16 * len(frames)
    directory = struct.pack("<HHH", 0, 1, len(frames))
    for size, frame in zip(SIZES, frames):
        side = 0 if size >= 256 else size  # 0 means 256 in the icon directory
        directory += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(frame), offset)
        offset += len(frame)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(directory + b"".join(frames))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
