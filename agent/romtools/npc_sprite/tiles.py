"""SNES planar tile <-> image helpers for ALTTP gfx sheets.

A sheet is 64 8x8 tiles laid out 16 wide x 4 tall -> a 128x32 image, OR we lay
them 8x8 for a square 64x64 view. ALTTP sprite gfx are 3bpp (24 bytes/tile);
the larger sheets are 4bpp (32 bytes/tile).

3bpp tile layout (24 bytes): rows 0..7 each contribute bp0,bp1 (interleaved,
16 bytes) then bp2 (8 bytes appended).
4bpp tile layout (32 bytes): bp0,bp1 interleaved (16 bytes), then bp2,bp3
interleaved (16 bytes).
"""
from PIL import Image


def decode_tile_3bpp(tile):
    """24 bytes -> list of 64 palette indices (0..7), row-major."""
    px = [0] * 64
    for y in range(8):
        bp0 = tile[y * 2]
        bp1 = tile[y * 2 + 1]
        bp2 = tile[16 + y]
        for x in range(8):
            bit = 7 - x
            px[y * 8 + x] = ((bp0 >> bit) & 1) | (((bp1 >> bit) & 1) << 1) | (((bp2 >> bit) & 1) << 2)
    return px


def decode_tile_4bpp(tile):
    """32 bytes -> list of 64 palette indices (0..15), row-major."""
    px = [0] * 64
    for y in range(8):
        bp0 = tile[y * 2]
        bp1 = tile[y * 2 + 1]
        bp2 = tile[16 + y * 2]
        bp3 = tile[16 + y * 2 + 1]
        for x in range(8):
            bit = 7 - x
            px[y * 8 + x] = (((bp0 >> bit) & 1) << 0) | (((bp1 >> bit) & 1) << 1) | \
                            (((bp2 >> bit) & 1) << 2) | (((bp3 >> bit) & 1) << 3)
    return px


def sheet_to_image(data, palette, cols=16):
    """Decode a sheet (0x600 3bpp or 0x800 4bpp) to an indexed-color RGBA image.

    palette: list of (r,g,b) for the 8 or 16 indices (index 0 -> transparent).
    """
    if len(data) == 0x600:
        bpp, tsize, decode = 3, 24, decode_tile_3bpp
    elif len(data) == 0x800:
        bpp, tsize, decode = 4, 32, decode_tile_4bpp
    else:
        raise ValueError(f"unexpected sheet size {len(data):#x}")
    ntiles = len(data) // tsize
    rows = (ntiles + cols - 1) // cols
    img = Image.new("RGBA", (cols * 8, rows * 8), (0, 0, 0, 0))
    pixels = img.load()
    for t in range(ntiles):
        px = decode(data[t * tsize:(t + 1) * tsize])
        ox = (t % cols) * 8
        oy = (t // cols) * 8
        for i, idx in enumerate(px):
            if idx == 0:
                continue  # transparent
            r, g, b = palette[idx % len(palette)]
            pixels[ox + i % 8, oy + i // 8] = (r, g, b, 255)
    return img


# a generic high-contrast palette just for shape identification (not game-accurate)
IDENT_PALETTE_4 = [
    (0, 0, 0), (90, 90, 90), (140, 90, 70), (200, 150, 120),
    (240, 210, 170), (210, 60, 60), (240, 130, 60), (250, 220, 90),
    (90, 170, 90), (60, 220, 120), (60, 140, 220), (120, 90, 200),
    (230, 120, 200), (255, 255, 255), (170, 170, 170), (40, 40, 40),
]
