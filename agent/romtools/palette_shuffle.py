"""palette_shuffle.py — overworld/dungeon tile palette shuffle for patched ALTTPR ROMs.

Thin wrapper around Maseya's z3pr (`pip install maseya-z3pr`) — the same
"Maseya-based palette shuffle" the alttpr.com website uses. z3pr groups colors
semantically (all grass colors move together, water together, etc. — 71
overworld groups, 31 dungeon groups, including the hardcoded/OAM color sites)
and blends each group in HCY space with heavily dampened chroma/luma changes,
so the game still looks normal — just recolored.

See ALTTPR-REFERENCE docs/10 §4 for the algorithm and offset provenance.

z3pr rewrites palette bytes but does not repair the SNES internal checksum;
we do that here as the final step (ALTTPR-REFERENCE docs/02 §2.3).
"""

import random

from maseya.z3pr.palette_randomizer import (
    build_offset_collections,
    generate_random_colors,
    randomize,
)

# SNES internal header (LoROM): complement @ 0x7FDC, checksum @ 0x7FDE.
CKSUM_COMPLEMENT = 0x7FDC
CKSUM_VALUE = 0x7FDE


def _fix_snes_checksum(rom):
    """Recompute the 16-bit SNES checksum + complement (power-of-two ROMs only;
    ALTTPR output is 2 MB). See ALTTPR-REFERENCE docs/02 §2.3."""
    size = len(rom)
    if size & (size - 1) != 0:
        raise ValueError(f"ROM size {size:#x} is not a power of two")
    rom[CKSUM_COMPLEMENT:CKSUM_COMPLEMENT + 2] = b"\xff\xff"
    rom[CKSUM_VALUE:CKSUM_VALUE + 2] = b"\x00\x00"
    checksum = sum(rom) & 0xFFFF
    rom[CKSUM_VALUE] = checksum & 0xFF
    rom[CKSUM_VALUE + 1] = (checksum >> 8) & 0xFF
    complement = checksum ^ 0xFFFF
    rom[CKSUM_COMPLEMENT] = complement & 0xFF
    rom[CKSUM_COMPLEMENT + 1] = (complement >> 8) & 0xFF


def shuffle_palettes(rom_path, overworld=True, dungeon=True, seed=None, log=print):
    """Shuffle the overworld and/or dungeon tile palettes of the ROM at `rom_path`,
    in place, and fix the SNES checksum. Returns the RNG seed used."""
    with open(rom_path, "rb") as f:
        rom = bytearray(f.read())
    if len(rom) % 1024 == 512:          # copier header (not expected from pyz3r)
        raise ValueError("ROM has a 512-byte copier header; refusing to shuffle")
    if len(rom) < 0x100000:
        raise ValueError("ROM is smaller than 1 MB; not a valid ALTTP image")

    if seed is None:
        seed = random.randrange(1 << 30)

    offset_collections = build_offset_collections({
        "randomize_overworld": overworld,
        "randomize_dungeon": dungeon,
    })
    randomize(rom, "maseya", offset_collections, generate_random_colors(seed))
    log(f"[palette] shuffled {len(offset_collections)} color groups "
        f"(overworld={overworld}, dungeon={dungeon}, seed={seed})")

    _fix_snes_checksum(rom)
    with open(rom_path, "wb") as f:
        f.write(rom)
    return seed
