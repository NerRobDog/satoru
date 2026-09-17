#!/usr/bin/env python3
"""Regenerate satoru's own default app icon.

Every .app satoru writes gets an icon now (see `install_bundle_icon` in
launcher/satoru.py): the game's own, when a pack names its exe, and
otherwise this mark — flat colours, one original geometric shape, nothing
traced from any game, brand or existing icon set.

Drawing is pure stdlib (struct + zlib for the PNG; no Pillow, no pip
installs, so it runs on the same macOS system python 3.9 the rest of the
project targets). Converting PNG -> .icns needs `sips`, which is why that
last step is not part of the runtime path: launcher/assets/satoru-default.icns
is committed, pre-built, and read as a plain file at install time.

    python3 tools/make-default-icon.py

Rewrites launcher/assets/satoru-default.icns in place. Needs macOS (`sips`).

Note: sips's icns writer rejects a 1024x1024 source (`Error 13`) but is
happy with 512x512, so this draws at 512 (with 2x2 supersampling for
antialiasing, i.e. computing at 1024 internally) rather than shipping the
full-resolution source.
"""
import struct
import subprocess
import sys
import tempfile
import zlib

SIZE = 512
SS = 2  # supersample factor for antialiased edges
BIG = SIZE * SS

# a rounded-square backdrop with a centered upward chevron-and-stem — read as
# "launch". Nothing here is copied from any game, brand or existing icon set.
BG = (52, 61, 82, 255)      # slate
FG = (137, 210, 214, 255)   # soft teal accent


def _rounded_rect_mask(x, y, size, radius):
    cx = cy = size / 2.0
    half = size / 2.0
    dx = abs(x - cx) - (half - radius)
    dy = abs(y - cy) - (half - radius)
    if dx <= 0 or dy <= 0:
        return abs(x - cx) <= half and abs(y - cy) <= half
    return (dx * dx + dy * dy) <= radius * radius


def _in_triangle(px, py, a, b, c):
    def sign(p1, p2, p3):
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])
    d1, d2, d3 = sign((px, py), a, b), sign((px, py), b, c), sign((px, py), c, a)
    has_neg = d1 < 0 or d2 < 0 or d3 < 0
    has_pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (has_neg and has_pos)


def _draw_rgba():
    s = BIG
    radius = s * 0.22
    cx = s / 2.0
    top_y, mid_y, bot_y = s * 0.28, s * 0.52, s * 0.74
    half_w, thick = s * 0.30, s * 0.14

    tri_outer = [(cx, top_y), (cx + half_w, mid_y), (cx - half_w, mid_y)]
    tri_inner = [(cx, top_y + thick), (cx + half_w - thick * 1.15, mid_y),
                 (cx - half_w + thick * 1.15, mid_y)]
    bar_left, bar_right = cx - thick * 0.55, cx + thick * 0.55
    bar_top, bar_bottom = mid_y - thick * 0.1, bot_y

    big_px = bytearray(s * s * 4)
    for by in range(s):
        for bx in range(s):
            idx = (by * s + bx) * 4
            if not _rounded_rect_mask(bx + 0.5, by + 0.5, s, radius):
                pixel = (0, 0, 0, 0)
            else:
                pixel = BG
                px, py = bx + 0.5, by + 0.5
                in_chevron = (_in_triangle(px, py, *tri_outer) and
                              not _in_triangle(px, py, *tri_inner))
                in_stem = (bar_left <= px <= bar_right and
                           bar_top <= py <= bar_bottom)
                if in_chevron or in_stem:
                    pixel = FG
            big_px[idx:idx + 4] = bytes(pixel)

    out = bytearray(SIZE * SIZE * 4)
    for y in range(SIZE):
        for x in range(SIZE):
            rs = gs = bs = as_ = 0
            for dy in range(SS):
                for dx in range(SS):
                    i = ((y * SS + dy) * s + (x * SS + dx)) * 4
                    rs += big_px[i]; gs += big_px[i + 1]
                    bs += big_px[i + 2]; as_ += big_px[i + 3]
            n = SS * SS
            o = (y * SIZE + x) * 4
            out[o:o + 4] = bytes((rs // n, gs // n, bs // n, as_ // n))
    return bytes(out)


def _write_png(path, rgba, size):
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)  # filter type 0 (none) for every scanline
        raw.extend(rgba[y * stride:(y + 1) * stride])
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw), 9)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", ihdr))
        fh.write(chunk(b"IDAT", idat))
        fh.write(chunk(b"IEND", b""))


def main():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dest = os.path.join(root, "launcher", "assets", "satoru-default.icns")
    rgba = _draw_rgba()
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        png_path = tmp.name
    try:
        _write_png(png_path, rgba, SIZE)
        code = subprocess.call(["sips", "-s", "format", "icns", png_path,
                                 "--out", dest])
    finally:
        os.remove(png_path)
    if code != 0:
        sys.exit("sips failed (%d) converting to %s" % (code, dest))
    print("wrote", dest)


if __name__ == "__main__":
    main()
