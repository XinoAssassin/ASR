"""Write the app icons (PNG + PNG-in-ICO) with the standard library only.

Usage: python make_icons.py OUT_DIR
"""

import struct
import sys
import zlib
from pathlib import Path

BG = (32, 128, 240)
FG = (255, 255, 255)


def _pixel(x: int, y: int, size: int) -> tuple[int, int, int, int]:
    # Rounded square background with a white "wave" of five bars (a speech level meter).
    r = size * 0.18
    cx = min(max(x, r), size - 1 - r)
    cy = min(max(y, r), size - 1 - r)
    if (x - cx) ** 2 + (y - cy) ** 2 > r * r:
        return (0, 0, 0, 0)
    bars = (0.30, 0.55, 0.80, 0.55, 0.30)
    bar_w = size / 11
    for i, h in enumerate(bars):
        left = size * 0.5 + (i - 2) * bar_w * 1.8 - bar_w / 2
        if left <= x < left + bar_w and abs(y - size / 2) <= h * size / 2 * 0.8:
            return (*FG, 255)
    return (*BG, 255)


def png(size: int) -> bytes:
    rows = b"".join(
        b"\x00" + b"".join(bytes(_pixel(x, y, size)) for x in range(size)) for y in range(size)
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


def ico(sizes: list[int]) -> bytes:
    images = [png(s) for s in sizes]
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for s, data in zip(sizes, images, strict=True):
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return out + b"".join(images)


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "icon.ico").write_bytes(ico([16, 32, 48, 256]))
    for s, name in ((32, "32x32.png"), (128, "128x128.png"), (256, "128x128@2x.png"), (512, "icon.png")):
        (out / name).write_bytes(png(s))


if __name__ == "__main__":
    main()
