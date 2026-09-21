"""Resolve ALTTP (JP 1.0) sprite/background gfx sheet addresses from the ROM's
own pointer arrays, and decompress them.

Pointer arrays (from spannerisms/jpdasm, GFXSheetPointers @ $00CFC0):
  background: bank @ $00CFC0, high @ $00D09F, low @ $00D17E   (115 entries, GFX_00..GFX_72)
  sprite:     bank @ $00D033, high @ $00D112, low @ $00D1F1   (108 entries, GFX_73..GFX_DE)

There are three arrays, each covering all 223 sheets with the 115 background
entries first and the 108 sprite entries after -- so each sub-array start is
derivable, and they check each other:
  bank  $00CFC0            sprite bank = +115 = $00D033
  high  $00CFC0 + 223      = $00D09F,  sprite high = +115 = $00D112
  low   $00D09F + 223      = $00D17E,  sprite low  = +115 = $00D1F1

We read the arrays from the ROM directly (so it works on vanilla JP, rando, etc.)
rather than trusting a hard-coded data address. Sheet index is the array index;
sprite sheet 0x00 == GFX_73.

Each decompressed sheet is 0x600 bytes (a 3bpp sheet = 0x30 8x8 tiles).
A few sprite sheets (the ones living at $10F000+ right after Link) are stored
*uncompressed*; resolve_sheet() flags those so callers can raw-read them.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import lc_lz2

SHEET_SIZE = 0x600

# SNES addresses of the pointer sub-arrays -> file offsets (LoROM bank 0)
SPRITE_BANK = 0x5033   # $00D033
SPRITE_HIGH = 0x5112   # $00D112
SPRITE_LOW  = 0x51F1   # $00D1F1
BG_BANK     = 0x4FC0   # $00CFC0
BG_HIGH     = 0x509F   # $00D09F  (was 0x50A1 -- two entries off, fixed 2026-07-31)
BG_LOW      = 0x517E   # $00D17E  (was 0x5180 -- ditto)
# The old values shifted every BG lookup by two sheets, which mostly returns the
# WRONG SHEET rather than failing: on the JP 1.0 ROM only 14 of 115 decoded to the
# same bytes either way, and 10 failed outright. Sprite sheets were unaffected,
# which is why this survived -- nothing here had exercised the BG arrays.

NUM_SPRITE_SHEETS = 108
NUM_BG_SHEETS = 115

# Sprite sheet indices 0x00..0x0B are stored UNCOMPRESSED (raw 3bpp, 0x600 each)
# in banks 0x10-0x11; indices 0x0C..0x6B are LC_LZ2-compressed (banks 0x14-0x18).
# Verified against spannerisms/jpdasm (incbin vs db on GFX_73..GFX_DE).
NUM_RAW_SPRITE_SHEETS = 12


def lorom_to_file(snes):
    return ((snes & 0x7F0000) >> 1) | (snes & 0x7FFF)


def sprite_sheet_snes_addr(rom, index):
    """SNES long address of sprite sheet `index` (0..107), read from the ROM arrays."""
    lo = rom[SPRITE_LOW + index]
    hi = rom[SPRITE_HIGH + index]
    bank = rom[SPRITE_BANK + index]
    return lo | (hi << 8) | (bank << 16)


def bg_sheet_snes_addr(rom, index):
    lo = rom[BG_LOW + index]
    hi = rom[BG_HIGH + index]
    bank = rom[BG_BANK + index]
    return lo | (hi << 8) | (bank << 16)


def resolve_sheet(rom, index, kind="sprite"):
    """Return dict: {index, kind, snes, file, raw(bool)}."""
    snes = sprite_sheet_snes_addr(rom, index) if kind == "sprite" else bg_sheet_snes_addr(rom, index)
    return {
        "index": index,
        "kind": kind,
        "snes": snes,
        "file": lorom_to_file(snes),
        "raw": kind == "sprite" and index < NUM_RAW_SPRITE_SHEETS,
    }


def read_sheet(rom, index, kind="sprite"):
    """Return (decompressed_600_bytes, info_dict)."""
    info = resolve_sheet(rom, index, kind)
    if info["raw"]:
        data = bytes(rom[info["file"]:info["file"] + SHEET_SIZE])
    else:
        data, _ = lc_lz2.decompress(rom, info["file"])
    return data, info


if __name__ == "__main__":
    rom = open(sys.argv[1] if len(sys.argv) > 1 else
               os.path.join(os.path.dirname(__file__), "..", "assets", "zelda.sfc"), "rb").read()
    from collections import Counter
    raw_ok = comp_ok = errors = roundtrip_ok = 0
    size_hist = Counter()
    for i in range(NUM_SPRITE_SHEETS):
        info = resolve_sheet(rom, i, "sprite")
        try:
            if info["raw"]:
                data = bytes(rom[info["file"]:info["file"] + SHEET_SIZE])
                assert len(data) == SHEET_SIZE
                raw_ok += 1
            else:
                data, clen = lc_lz2.decompress(rom, info["file"])
                comp_ok += 1
                # prove codec roundtrips real data: re-encode, re-decode, compare
                re_dec, _ = lc_lz2.decompress(lc_lz2.compress(data), 0)
                if re_dec == data:
                    roundtrip_ok += 1
                else:
                    print(f"  roundtrip MISMATCH sprite {i:#x}")
            size_hist[len(data)] += 1
        except Exception as e:
            errors += 1
            print(f"  ERROR sprite {i:#04x} GFX_{0x73+i:02X} @ {info['file']:#07x}: {e}")
    print(f"\nsprite sheets: {NUM_SPRITE_SHEETS} total")
    print(f"  raw read OK:        {raw_ok}/{NUM_RAW_SPRITE_SHEETS}")
    print(f"  compressed decoded: {comp_ok}/{NUM_SPRITE_SHEETS - NUM_RAW_SPRITE_SHEETS}")
    print(f"  codec roundtrip OK: {roundtrip_ok}/{comp_ok}")
    print(f"  hard errors:        {errors}")
    print(f"  decompressed sizes: {dict(sorted(size_hist.items()))}")
