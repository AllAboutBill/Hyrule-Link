"""
items.py — the canonical ALTTPR progression-item catalog shared by the
HyruleLink server and the player agent.

Only *progression* items are in the shared, one-owner-at-a-time pool. Ammo
(rupees/bombs/arrows/hearts) and per-dungeon items (keys/maps/compasses) are
deliberately excluded — capping those across players would be miserable.

Each entry is a `Item`:

    key        stable identifier used in the protocol and ledger
    name       human label for the UI
    addr       WRAM offset (== SNES addr - $7E0000); the SRAM mirror lives here
    kind       how the byte is interpreted / written:
                 "simple"      0 = absent, 1 = present (single byte)
                 "progressive" 0..cap tiers; each owner gets back their own best
                               tier (the highest level THEY have found), not a
                               shared max
                 "bitfield"    one bit of a shared byte (boomerangs, etc.)
                 "boots"       like simple but also toggles the dash-ability flag
    mask       for "bitfield": which bit of `addr` this token owns
    cap        for "progressive": highest tier
    present    for "progressive": lowest tier that counts as "discovered"
               (armor/magic sit at 0 by default, so a token only exists at >=1)
    effect_key the key understood by sni.item_effects.ItemManager.add()/remove()
               for items that need its special handling (bitfields, boots,
               silver arrows). None => generic byte write via `addr`.

The agent's effects wrapper uses `kind`/`addr`/`effect_key` to enable (write the
token's level) and disable (zero it) an item without clobbering shared bytes.
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, List


@dataclass(frozen=True)
class Item:
    key: str
    name: str
    addr: int
    kind: str = "simple"
    mask: int = 0xFF
    cap: int = 1
    present: int = 1
    # byte value written to grant this item. Usually 1, but some slots need a
    # specific value (Magic Mirror = 2; 1 renders a broken icon — the "scroll"
    # state — and isn't usable).
    give: int = 1
    effect_key: Optional[str] = None
    # progressive tier labels for the UI, index 0..cap (optional)
    tiers: tuple = field(default_factory=tuple)


# WRAM offsets are the ALTTPR SRAM-mirror addresses (see memory_constants.py).
ITEMS: List[Item] = [
    # ── progressive equipment ────────────────────────────────────────────
    Item("sword",  "Sword",  0xF359, "progressive", cap=4, present=1,
         tiers=("none", "Fighter", "Master", "Tempered", "Gold")),
    Item("shield", "Shield", 0xF35A, "progressive", cap=3, present=1,
         tiers=("none", "Fighter", "Fire", "Mirror")),
    Item("armor",  "Mail",   0xF35B, "progressive", cap=2, present=1,
         tiers=("Green", "Blue", "Red")),
    Item("gloves", "Gloves", 0xF354, "progressive", cap=2, present=1,
         tiers=("none", "Power Glove", "Titan's Mitt")),
    Item("bow",    "Bow",    0xF340, "progressive", cap=2, present=1,
         effect_key="bow", tiers=("none", "Bow", "Silver Bow")),
    Item("magic",  "Magic Upgrade", 0xF37B, "progressive", cap=2, present=1,
         tiers=("none", "1/2 Magic", "1/4 Magic")),

    # ── boots (needs the run-ability flag too) ───────────────────────────
    Item("boots", "Pegasus Boots", 0xF355, "boots", effect_key="boots"),

    # ── simple single-byte items ─────────────────────────────────────────
    Item("hookshot",   "Hookshot",        0xF342, "simple"),
    Item("firerod",    "Fire Rod",        0xF345, "simple"),
    Item("icerod",     "Ice Rod",         0xF346, "simple"),
    Item("bombos",     "Bombos Medallion", 0xF347, "simple"),
    Item("ether",      "Ether Medallion",  0xF348, "simple"),
    Item("quake",      "Quake Medallion",  0xF349, "simple"),
    Item("lamp",       "Lamp",            0xF34A, "simple"),
    Item("hammer",     "Magic Hammer",    0xF34B, "simple"),
    Item("bug_net",    "Bug Net",         0xF34D, "simple"),
    Item("book",       "Book of Mudora",  0xF34E, "simple"),
    Item("somaria",    "Cane of Somaria", 0xF350, "simple"),
    Item("byrna",      "Cane of Byrna",   0xF351, "simple"),
    Item("cape",       "Magic Cape",      0xF352, "simple"),
    Item("mirror",     "Magic Mirror",    0xF353, "simple", give=2),
    Item("flippers",   "Flippers",        0xF356, "simple"),
    Item("moon_pearl", "Moon Pearl",      0xF357, "simple"),

    # ── shared-byte bitfield items ───────────────────────────────────────
    Item("blue_boomerang", "Blue Boomerang", 0xF341, "bitfield", mask=0x01,
         effect_key="blue_boomerang"),
    Item("red_boomerang",  "Red Boomerang",  0xF341, "bitfield", mask=0x02,
         effect_key="red_boomerang"),
    Item("mushroom", "Mushroom", 0xF344, "bitfield", mask=0x01,
         effect_key="mushroom"),
    Item("powder",   "Magic Powder", 0xF344, "bitfield", mask=0x02,
         effect_key="powder"),
    Item("shovel", "Shovel", 0xF34C, "bitfield", mask=0x01, effect_key="shovel"),
    Item("flute",  "Flute",  0xF34C, "bitfield", mask=0x02, effect_key="flute"),
    # Bottles are intentionally NOT pooled: they behave like consumables
    # (rupees/bombs/arrows), so sharing one bottle token was broken and pointless.
]

BY_KEY: Dict[str, Item] = {it.key: it for it in ITEMS}

# Convenience sets used by the agent/effects layer.
PROGRESSIVE_KEYS = {it.key for it in ITEMS if it.kind == "progressive"}
BITFIELD_KEYS = {it.key for it in ITEMS if it.kind == "bitfield"}

# Generic {key: (addr, value)} map for sni.item_effects.ItemManager — the simple
# byte-write items. Bitfields/boots/bow are handled specially by ItemManager via
# effect_key, so they are intentionally not listed here.
ITEM_ADDRESSES = {
    it.key: (it.addr, it.give)
    for it in ITEMS
    if it.kind == "simple"
}


def discovered_level(item: Item, raw_byte: int) -> int:
    """
    Translate a raw WRAM byte into this token's effective level.

    0 means "not present for this token". For bitfields that means the bit is
    clear; for progressive/simple it's the byte value (clamped to cap) as long
    as it meets the `present` threshold.
    """
    if item.kind == "bitfield":
        return 1 if (raw_byte & item.mask) else 0
    if item.kind == "boots":
        return 1 if raw_byte else 0
    if item.kind == "simple":
        return 1 if raw_byte else 0
    # progressive
    if raw_byte < item.present:
        return 0
    return min(raw_byte, item.cap)


def tier_label(item: Item, level: int) -> str:
    """Human label for a token level, used by the UI."""
    if item.tiers and 0 <= level < len(item.tiers):
        return item.tiers[level]
    return "owned" if level > 0 else "—"


# Item icons (sprites from the ALTTPR tracker, see web/items/). Items listed here
# have per-tier art `<key>-1.png` … `<key>-N.png`; every other item has a single
# `<key>.png`. Keep this in sync with the files in web/items/.
ITEM_IMAGE_TIERS = {"sword": 4, "shield": 3, "armor": 2, "gloves": 2, "bow": 2}


def item_image(key: str, level: int = 0) -> str:
    """Filename (within web/items/) for an item at a given level.

    Progressive items return their tier art (`sword-2.png` = Master); level 0 or a
    non-tiered item falls back to the item's base sprite. The returned name is
    used both by the web grid (/static/items/<name>) and the desktop app.
    """
    maxtier = ITEM_IMAGE_TIERS.get(key)
    if maxtier:
        n = min(max(level, 1), maxtier)   # clamp into 1..N; level 0 → base art
        return f"{key}-{n}.png"
    return f"{key}.png"
