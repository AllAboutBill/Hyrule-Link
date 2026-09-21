"""Inject custom graphics into ALTTP sprite gfx sheets (built for Zelda the
follower = sheets 0x64 + 0x65, but works for any sprite sheet index).

Workflow:
    # 1) dump the current art to an editable indexed PNG (128 tiles = both halves)
    python inject.py dump <rom> zelda_edit.png
    # 2) edit zelda_edit.png in any editor, keeping it <=8 colors / indexed
    # 3) inject it back, producing a new patched ROM
    python inject.py apply <rom> zelda_edit.png <out_rom>

The two sheets combine as one 16x8-tile sheet:
    rows 0-3 (tiles 0x00-0x3F) -> sheet 0x65 (low,  WRAM $7F4000)
    rows 4-7 (tiles 0x40-0x7F) -> sheet 0x64 (high, WRAM $7F4600)

Injection strategy: each edited 0x600-byte sheet is LC_LZ2-recompressed and
written to free space appended at the end of the ROM, then the sprite gfx
pointer arrays (low/high/bank @ 0x51F1/0x5112/0x5033) are repointed to it. This
sidesteps the original block sizes entirely, so it is safe on vanilla JP and on
the larger randomizer ROM alike.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from PIL import Image
import lc_lz2, sheets, tiles

ZELDA_LOW = 0x65   # rows 0-3
ZELDA_HIGH = 0x64  # rows 4-7

# pointer arrays (file offsets), see sheets.py
SP_LOW, SP_HIGH, SP_BANK = sheets.SPRITE_LOW, sheets.SPRITE_HIGH, sheets.SPRITE_BANK


def file_to_lorom(file_off):
    bank = file_off >> 15
    return (bank << 16) | 0x8000 | (file_off & 0x7FFF)


def encode_tile_3bpp(indices):
    """64 palette indices (0..7) -> 24 bytes (inverse of tiles.decode_tile_3bpp)."""
    out = bytearray(24)
    for y in range(8):
        bp0 = bp1 = bp2 = 0
        for x in range(8):
            v = indices[y * 8 + x] & 7
            bit = 7 - x
            bp0 |= (v & 1) << bit
            bp1 |= ((v >> 1) & 1) << bit
            bp2 |= ((v >> 2) & 1) << bit
        out[y * 2] = bp0
        out[y * 2 + 1] = bp1
        out[16 + y] = bp2
    return bytes(out)


def sheet_from_image_region(img, tile_row_start, rows=4, cols=16):
    """Encode a region of the editable image into a 0x600-byte 3bpp sheet."""
    px = img.load()
    data = bytearray()
    for tr in range(rows):
        for tc in range(cols):
            ox = tc * 8
            oy = (tile_row_start + tr) * 8
            indices = [px[ox + x, oy + y] for y in range(8) for x in range(8)]
            data += encode_tile_3bpp(indices)
    assert len(data) == 0x600, len(data)
    return bytes(data)


def dump_editable(rom, out_png, palette=None):
    """Write sheets 0x65 (rows 0-3) + 0x64 (rows 4-7) as one indexed 128x64 PNG."""
    low, _ = sheets.read_sheet(rom, ZELDA_LOW, "sprite")
    high, _ = sheets.read_sheet(rom, ZELDA_HIGH, "sprite")
    img = Image.new("P", (128, 64), 0)
    px = img.load()
    for half, data in ((0, low), (4, high)):
        for t in range(64):
            tile = tiles.decode_tile_3bpp(data[t * 24:(t + 1) * 24])
            ox = (t % 16) * 8
            oy = (half + t // 16) * 8
            for i, v in enumerate(tile):
                px[ox + i % 8, oy + i // 8] = v
    # Zelda's real in-game colors: OBJ palette 4, colors 0-7 (followers use the
    # "left"/low-8 3bpp->4bpp expansion). Index 0 is transparent.
    # Read from the $7EC300 palette buffer; SpriteMain fills palettes 1-4 and is
    # environment-independent, so these are static.
    pal = palette or [0, 0, 0,        # 0 transparent
                      255, 255, 255,  # 1 white
                      222, 99, 99,    # 2 pink/red (dress)
                      181, 99, 41,    # 3 brown (hair)
                      247, 165, 107,  # 4 skin
                      41, 41, 41,     # 5 dark outline
                      181, 148, 255,  # 6 lavender (robe)
                      82, 115, 206]   # 7 blue
    img.putpalette(pal + [0] * (768 - len(pal)))
    img.save(out_png)
    print(f"dumped editable sheet -> {out_png} (128x64, 8-color indexed)")
    print("  rows 0-3 = sheet 0x65, rows 4-7 = sheet 0x64; index 0 = transparent")


def apply_injection(rom_path, png, out_rom):
    rom = bytearray(open(rom_path, "rb").read())
    img = Image.open(png).convert("P")
    if img.size != (128, 64):
        raise SystemExit(f"PNG must be 128x64 (16x8 tiles); got {img.size}")

    sheet_low = sheet_from_image_region(img, 0)   # rows 0-3 -> 0x65
    sheet_high = sheet_from_image_region(img, 4)   # rows 4-7 -> 0x64

    # append recompressed sheets to end of ROM, aligned so neither crosses a
    # LoROM bank boundary ($x0000 .. $x7FFF window)
    def append_sheet(data):
        nonlocal rom
        comp = lc_lz2.compress(data)
        off = len(rom)
        if (off & 0x7FFF) + len(comp) > 0x8000:          # would cross bank wrap
            pad = 0x8000 - (off & 0x7FFF)
            rom += b"\xFF" * pad
            off = len(rom)
        rom += comp
        return off, len(comp)

    for index, sheet in ((ZELDA_LOW, sheet_low), (ZELDA_HIGH, sheet_high)):
        off, clen = append_sheet(sheet)
        snes = file_to_lorom(off)
        rom[SP_LOW + index] = snes & 0xFF
        rom[SP_HIGH + index] = (snes >> 8) & 0xFF
        rom[SP_BANK + index] = (snes >> 16) & 0xFF
        print(f"  sheet {index:#04x}: {clen} compressed bytes @ file {off:#08x} "
              f"(snes {snes:#08x}); repointed")

    open(out_rom, "wb").write(rom)
    print(f"wrote {out_rom} ({len(rom)} bytes)")

    # self-verify: re-read the sheets through the (modified) pointers and codec
    rom2 = bytes(rom)
    for index, original in ((ZELDA_LOW, sheet_low), (ZELDA_HIGH, sheet_high)):
        got, _ = sheets.read_sheet(rom2, index, "sprite")
        assert got == original, f"verify FAILED for sheet {index:#x}"
    print("  verify OK: both sheets decode back to the injected pixels")


def main():
    if len(sys.argv) < 4:
        print(__doc__); return
    cmd, rom_path = sys.argv[1], sys.argv[2]
    rom = open(rom_path, "rb").read()
    if cmd == "dump":
        dump_editable(rom, sys.argv[3])
    elif cmd == "apply":
        apply_injection(rom_path, sys.argv[3], sys.argv[4])
    else:
        print(f"unknown command {cmd!r}"); print(__doc__)


if __name__ == "__main__":
    main()
