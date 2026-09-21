"""Build the BombosSwap mark from the Bombos medallion sprite.

    python tools/make_mark.py [path to a PNG] [--rows N] [--out DIR] [--check REF]

The default source is web/items/bombos.png, the icon the board shows. That
file is the game's 16 px sprite scaled by 3.5 with soft edges, so it is not
pixel art as it stands. This finds the sprite's own pixel grid again (the art
is --rows pixels tall, 16 by default), takes the colour most of each cell's
inside agrees on, and snaps every cell to the sprite's few real colours. A
clean sprite (16 px tall, or pixel-doubled) comes out unchanged.

The three colours found are then put back on the game's medallion palette
(MEDALLION below), which the soft resample shifted by a few steps: the same
three values as EtherNet's ether.svg and QuakeCast's quake.png, so the three
medallion marks match. A source whose colours are not near that palette keeps
its own.

--check REF compares the result, pixel for pixel, with a clean sprite (16x16,
or pixel-doubled 32x32), such as F:/RaceConnect/web/img/items/bombos10.png,
and exits 1 on any difference.

Writes web/img/bombos.svg (the medallion alone, for the wordmark: a 16x16 box
drawn at 32 px, so every sprite pixel lands on whole screen pixels) and
web/img/bombos-mark.svg (the same on a dark rounded tile, for the browser tab), or
the same two files in --out. The SVG is one rect per run of same-coloured
pixels with crispEdges, so it is the sprite, pixel for pixel, at any size.
Adapted from EtherNet's make_mark.py. Needs Pillow; only run when the art
changes - the two SVGs are committed, and tests/test_intro.py checks that
running this again gives them byte for byte.
"""
import argparse
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / "web" / "items" / "bombos.png"
SAME = 24          # RGB distance under which two colours are the same colour
# the game's medallion palette: light gold, dark gold, outline
MEDALLION = ((0xF8, 0xD5, 0x35), (0xB9, 0x8D, 0x23), (0x23, 0x28, 0x23))
TILE = "#0E0E10"   # --bg in web/css/hyrulelink.css
TILE_N = 22        # the tab icon's tile: 3 px round a 16 px sprite, as EtherNet's round medallion


def dist(a, b):
    return sum((p - q) ** 2 for p, q in zip(a[:3], b[:3])) ** .5


def cell_colour(im, left, top, right, bottom, inset):
    """The colour most of the cell's inside agrees on, or None if clear."""
    px = [im.getpixel((x, y))
          for y in range(int(top), int(bottom) + 1) if top + inset <= y + .5 <= bottom - inset
          for x in range(int(left), int(right) + 1) if left + inset <= x + .5 <= right - inset
          if x < im.width and y < im.height]
    if not px:
        return None
    solid = [p for p in px if p[3] >= 128]
    if len(solid) * 2 <= len(px):
        return None
    return max(solid, key=lambda c: sum(dist(c, o) < SAME for o in solid))[:3]


def sprite(im, rows):
    """The source reduced to its own pixels: a list of rows of RGB or None."""
    x0, y0, x1, y1 = im.getchannel("A").point(lambda a: 255 if a >= 128 else 0).getbbox()
    scale = (y1 - y0) / rows
    cols = max(1, round((x1 - x0) / scale))
    sx, sy = (x1 - x0) / cols, (y1 - y0) / rows
    inset = .75 if min(sx, sy) >= 2.5 else 0
    grid = [[cell_colour(im, x0 + i * sx, y0 + j * sy, x0 + (i + 1) * sx, y0 + (j + 1) * sy, inset)
             for i in range(cols)] for j in range(rows)]
    # one palette for the whole sprite: near-identical colours become the
    # most common of them, so the soft resample does not leak shades
    counts = {}
    for row in grid:
        for c in row:
            if c is not None:
                counts[c] = counts.get(c, 0) + 1
    palette = []
    for c in sorted(counts, key=lambda c: -counts[c]):
        if not any(dist(c, p) < SAME for p in palette):
            palette.append(c)
    # back onto the medallion palette, when every colour has its own match there
    home = [min(MEDALLION, key=lambda m: dist(c, m)) for c in palette]
    if len(set(home)) == len(home) and all(dist(c, m) < SAME for c, m in zip(palette, home)):
        found = palette
        palette = home
        snap = lambda c: None if c is None else home[found.index(min(found, key=lambda p: dist(c, p)))]
    else:
        snap = lambda c: None if c is None else min(palette, key=lambda p: dist(c, p))
    return [[snap(c) for c in row] for row in grid], palette


def load_clean(path):
    """A clean sprite as rows of RGB or None (a pixel-doubled one is halved)."""
    im = Image.open(path).convert("RGBA")
    if im.size == (32, 32):
        im = im.resize((16, 16), Image.NEAREST)
    return [[im.getpixel((x, y))[:3] if im.getpixel((x, y))[3] >= 128 else None
             for x in range(im.width)] for y in range(im.height)]


def rects(grid, dx=0, dy=0):
    out = []
    for y, row in enumerate(grid):
        x = 0
        while x < len(row):
            c = row[x]
            if c is None:
                x += 1
                continue
            run = 1
            while x + run < len(row) and row[x + run] == c:
                run += 1
            out.append(f'<rect x="{x + dx:g}" y="{y + dy:g}" width="{run}" height="1" fill="#{c[0]:02x}{c[1]:02x}{c[2]:02x}"/>')
            x += run
    return "".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("src", nargs="?", default=str(DEFAULT))
    ap.add_argument("--rows", type=int, default=16, help="the sprite's height in game pixels (16)")
    ap.add_argument("--out", default=str(ROOT / "web" / "img"), help="where the two SVGs go (web/img)")
    ap.add_argument("--check", metavar="REF", help="a clean sprite to compare the result with")
    args = ap.parse_args()
    grid, palette = sprite(Image.open(args.src).convert("RGBA"), args.rows)
    h, w = len(grid), len(grid[0])
    assert w <= 16 and h <= 16, (w, h)
    head = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "bombos.svg").write_text(
        head.format(n=16) + rects(grid, (16 - w) / 2, (16 - h) / 2) + "</svg>\n", encoding="utf-8", newline="\n")
    (out / "bombos-mark.svg").write_text(
        head.format(n=TILE_N) + f'<rect width="{TILE_N}" height="{TILE_N}" rx="4" fill="{TILE}" shape-rendering="auto"/>'
        + rects(grid, (TILE_N - w) / 2, (TILE_N - h) / 2) + "</svg>\n", encoding="utf-8", newline="\n")
    key = {c: "abcdefghijklmnop"[i] for i, c in enumerate(palette)}
    for row in grid:
        print("  " + "".join(key[c] if c else "." for c in row))
    print(f"wrote bombos.svg + bombos-mark.svg; {w}x{h} px, {len(palette)} colours:",
          " ".join(f"{key[c]}=#{c[0]:02x}{c[1]:02x}{c[2]:02x}" for c in palette))
    if args.check:
        ref = load_clean(args.check)
        off = [(x, y) for y in range(max(h, len(ref))) for x in range(max(w, len(ref[0])))
               if (grid[y][x] if y < h and x < w else None) != (ref[y][x] if y < len(ref) and x < len(ref[0]) else None)]
        if off:
            print(f"{len(off)} pixels differ from {args.check}:", " ".join(f"{x},{y}" for x, y in off[:24]))
            sys.exit(1)
        print(f"the same as {args.check}, pixel for pixel")


if __name__ == "__main__":
    main()
