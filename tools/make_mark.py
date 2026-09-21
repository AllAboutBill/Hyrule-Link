"""Build the HyruleLink mark from the hookshot item sprite.

    python tools/make_mark.py [path to a PNG] [--rows N]

The default source is web/items/hookshot.png, the icon the board shows. That
file is the game's 16 px tall sprite scaled by 3.5 with soft edges, so it is
not pixel art as it stands. This finds the sprite's own pixel grid again (the
art is --rows pixels tall, 16 by default), takes the colour most of each
cell's inside agrees on, and snaps every cell to the sprite's few real
colours. A clean sprite (16 px tall, or pixel-doubled) comes out unchanged.

Writes web/img/hookshot.svg (the hookshot alone, for the wordmark: a 16x16
box drawn at 32 px, so every sprite pixel lands on whole screen pixels) and
web/img/mark.svg (the same on a dark rounded tile, for the browser tab). The
SVG is one rect per run of same-coloured pixels with crispEdges, so it is the
sprite, pixel for pixel, at any size. Adapted from EtherNet's make_mark.py.
Needs Pillow; only run when the art changes - the two SVGs are committed.
"""
import argparse
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / "web" / "items" / "hookshot.png"
SAME = 24          # RGB distance under which two colours are the same colour
TILE = "#0E0E10"   # --bg in web/css/hyrulelink.css
TILE_N = 20        # the tab icon's tile: 2 px round a 16 px tall sprite (EtherNet's round
                   # medallion uses 22; the hookshot is thin, so it gets the room)


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
    snap = lambda c: None if c is None else min(palette, key=lambda p: dist(c, p))
    return [[snap(c) for c in row] for row in grid], palette


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
    args = ap.parse_args()
    grid, palette = sprite(Image.open(args.src).convert("RGBA"), args.rows)
    h, w = len(grid), len(grid[0])
    assert w <= 16 and h <= 16, (w, h)
    head = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
    img = ROOT / "web" / "img"
    img.mkdir(parents=True, exist_ok=True)
    (img / "hookshot.svg").write_text(
        head.format(n=16) + rects(grid, (16 - w) / 2, (16 - h) / 2) + "</svg>\n", encoding="utf-8", newline="\n")
    (img / "mark.svg").write_text(
        head.format(n=TILE_N) + f'<rect width="{TILE_N}" height="{TILE_N}" rx="4" fill="{TILE}" shape-rendering="auto"/>'
        + rects(grid, (TILE_N - w) / 2, (TILE_N - h) / 2) + "</svg>\n", encoding="utf-8", newline="\n")
    key = {c: "abcdefghijklmnop"[i] for i, c in enumerate(palette)}
    for row in grid:
        print("  " + "".join(key[c] if c else "." for c in row))
    print(f"wrote hookshot.svg + mark.svg; {w}x{h} px, {len(palette)} colours:",
          " ".join(f"{key[c]}=#{c[0]:02x}{c[1]:02x}{c[2]:02x}" for c in palette))


if __name__ == "__main__":
    main()
