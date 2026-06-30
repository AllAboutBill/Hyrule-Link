"""
item_effects.py — authoritative ALttPR item add/remove/upgrade logic.

This is the single correct place for granting and removing items. It is
transport-agnostic: it works with anything exposing
    read_memory(address, size=1) -> bytes | None
    write_memory(address, data: bytes) -> bool
so it drives the baked-in EmuConnector (or any other tracker) unchanged.

Why this module exists
----------------------
The rebuilt sni_plugin replaced the project's tested item logic with flat
`write_memory(addr, [value])` calls. That broke three things:

  1. Boots — dashing needs BOTH the boots item ($7EF355) AND the run-ability
     flag ($7EF379 bit 0x04). A flat write set only the item, so Link "had"
     boots but couldn't dash.
  2. Shared-slot items — in ALttPR the boomerang byte ($7EF341) is a bitfield
     (bit0 = blue, bit1 = red, 0x03 = both); mushroom/powder ($7EF344) and
     shovel/flute ($7EF34C) work the same way. A flat write of one value
     clobbered the other half, and never set the Y-button selected-item index
     ($7EF33F), so the item often wasn't selectable.
  3. Silver arrows — setting the silver-bow byte without granting arrows did
     nothing useful.

Adding (per the configured behaviour) is *additive*: granting a red boomerang
keeps an existing blue (bitwise OR). Removing preserves the other half (bitwise
AND-NOT).

NOTE on selected-item index values: the 0x08/0x09/0x0A/0x0B/0x0D/0x0E codes
below are the ALttPR menu selection codes carried over from the project's
previously-working tracker. If a granted item ever isn't auto-equipped to Y,
these constants are the thing to verify against a live game.
"""

import logging

from shared.items import (
    BOW_FLAGS_ADDR, BOW_HAS_MASK, BOW_SILVER_MASK, BOW_EQUIP_ADDR, INV_TRACK_ADDR,
)

logger = logging.getLogger("ItemEffects")

# Shared-slot items (boomerangs, mushroom/powder, shovel/flute). Ownership is a
# bitfield in InventoryTracking ($7EF38C); the per-slot byte is an enum the menu
# cycles. Bits + enum values verified against z3randomizer newitems.asm /
# inventory.asm / itemdatatables.asm — do NOT OR the enum bytes (that corrupts
# them; see shared.items).
BOOM_BLUE_BIT = 0x80
BOOM_RED_BIT = 0x40
MUSHROOM_BITS = 0x28       # receive ORs 0x20|0x08; ownership/menu gate is 0x20
MUSHROOM_OWN = 0x20
POWDER_BIT = 0x10
SHOVEL_BIT = 0x04
FLUTE_ACTIVE_BIT = 0x01
FLUTE_INACTIVE_BIT = 0x02
FLUTE_ANY_BITS = 0x03

BOOM_EQUIP_ADDR = 0xF341    # enum: 1 = blue, 2 = red
POWDER_EQUIP_ADDR = 0xF344  # enum: 1 = mushroom, 2 = powder
FLUTE_EQUIP_ADDR = 0xF34C   # enum: 1 = shovel, 2 = inactive flute, 3 = active flute

# Boots + dash ability
BOOTS_ADDR = 0xF355
ABILITY_ADDR = 0xF379
RUN_ABILITY_MASK = 0x04

# Bow + arrows. Wood-vs-silver lives in the BowTracking byte ($7EF38E, imported as
# BOW_FLAGS_ADDR), NOT the equipped byte ($7EF340 / BOW_EQUIP_ADDR) — see set_bow()
# and shared.items for why writing $7EF340 alone never enables silver firing.
ARROWS_ADDR = 0xF377
DEFAULT_ARROWS = 30

# Progressive equipment: address -> max level. (The bow is NOT a plain counter —
# its wood/silver state is a bitfield in $7EF38E; see set_bow().)
PROGRESSIVE = {
    "sword": (0xF359, 4),   # 0 none .. 4 gold
    "shield": (0xF35A, 3),  # 0 none .. 3 mirror
    "armor": (0xF35B, 2),   # 0 green .. 2 red
    "gloves": (0xF354, 2),  # 0 none .. 2 titan
}


class ItemManager:
    def __init__(self, tracker, item_addresses=None):
        """
        tracker        : object with read_memory / write_memory
        item_addresses : the plugin's ITEM_ADDRESSES map {key: (addr, value)},
                         used for generic (non-special) items.
        """
        self.t = tracker
        self.item_addresses = item_addresses or {}

    # ── small helpers ───────────────────────────────────────────────────
    def _read(self, addr, tries=6):
        """Read one byte, retrying transient drops. NEVER fall back to 0.

        A dropped read returning 0 is catastrophic in a read-modify-write: it
        clobbers every other bit of a shared byte (ability flags $7EF379,
        InventoryTracking $7EF38C, BowTracking $7EF38E). NWA in particular drops
        the reply to a read issued right after a write, so we retry; if the byte
        is genuinely unreadable we raise and let the grant abort (and be retried)
        rather than corrupt the save."""
        for _ in range(tries):
            data = self.t.read_memory(addr, size=1)
            if data and len(data) >= 1:
                return int(data[0])
        raise IOError(f"[ItemEffects] could not read WRAM ${addr:04X}")

    def _track_set(self, bits):
        self.t.write_memory(INV_TRACK_ADDR,
                            bytes([(self._read(INV_TRACK_ADDR) | bits) & 0xFF]))

    def _track_clear(self, bits):
        self.t.write_memory(INV_TRACK_ADDR,
                            bytes([self._read(INV_TRACK_ADDR) & ~bits & 0xFF]))

    # ── shared-slot items: own via $7EF38C bits, equip via the enum slot ──
    # Granting sets the ownership bit AND equips the item (so it shows/works).
    # Revoking clears the bit AND, if that item was equipped, falls back to the
    # other item still owned in the slot (or empties it) — never OR-ing the enum.
    def _add_boomerang(self, blue=False, red=False):
        if blue:
            self._track_set(BOOM_BLUE_BIT)
            self.t.write_memory(BOOM_EQUIP_ADDR, bytes([0x01]))
        if red:
            self._track_set(BOOM_RED_BIT)
            self.t.write_memory(BOOM_EQUIP_ADDR, bytes([0x02]))
        return True

    def _remove_boomerang(self, blue=False, red=False):
        self._track_clear((BOOM_BLUE_BIT if blue else 0) | (BOOM_RED_BIT if red else 0))
        track = self._read(INV_TRACK_ADDR)
        equip = self._read(BOOM_EQUIP_ADDR)
        if blue and equip == 0x01:
            self.t.write_memory(BOOM_EQUIP_ADDR, bytes([0x02 if track & BOOM_RED_BIT else 0x00]))
        if red and equip == 0x02:
            self.t.write_memory(BOOM_EQUIP_ADDR, bytes([0x01 if track & BOOM_BLUE_BIT else 0x00]))
        return True

    def _add_mushroom_powder(self, mushroom=False, powder=False):
        if mushroom:
            self._track_set(MUSHROOM_BITS)
            self.t.write_memory(POWDER_EQUIP_ADDR, bytes([0x01]))
        if powder:
            self._track_set(POWDER_BIT)
            self.t.write_memory(POWDER_EQUIP_ADDR, bytes([0x02]))
        return True

    def _remove_mushroom_powder(self, mushroom=False, powder=False):
        self._track_clear((MUSHROOM_BITS if mushroom else 0) | (POWDER_BIT if powder else 0))
        track = self._read(INV_TRACK_ADDR)
        equip = self._read(POWDER_EQUIP_ADDR)
        if mushroom and equip == 0x01:
            self.t.write_memory(POWDER_EQUIP_ADDR, bytes([0x02 if track & POWDER_BIT else 0x00]))
        if powder and equip == 0x02:
            self.t.write_memory(POWDER_EQUIP_ADDR, bytes([0x01 if track & MUSHROOM_OWN else 0x00]))
        return True

    def _add_shovel_flute(self, shovel=False, flute=False):
        if shovel:
            self._track_set(SHOVEL_BIT)
            self.t.write_memory(FLUTE_EQUIP_ADDR, bytes([0x01]))
        if flute:
            self._track_set(FLUTE_ACTIVE_BIT)          # grant the working (active) flute
            self.t.write_memory(FLUTE_EQUIP_ADDR, bytes([0x03]))
        return True

    def _remove_shovel_flute(self, shovel=False, flute=False):
        self._track_clear((SHOVEL_BIT if shovel else 0) | (FLUTE_ANY_BITS if flute else 0))
        track = self._read(INV_TRACK_ADDR)
        equip = self._read(FLUTE_EQUIP_ADDR)
        if shovel and equip == 0x01:
            if track & FLUTE_ACTIVE_BIT:
                self.t.write_memory(FLUTE_EQUIP_ADDR, bytes([0x03]))
            elif track & FLUTE_INACTIVE_BIT:
                self.t.write_memory(FLUTE_EQUIP_ADDR, bytes([0x02]))
            else:
                self.t.write_memory(FLUTE_EQUIP_ADDR, bytes([0x00]))
        if flute and equip in (0x02, 0x03):
            self.t.write_memory(FLUTE_EQUIP_ADDR, bytes([0x01 if track & SHOVEL_BIT else 0x00]))
        return True

    # ── boots (+ run ability) ───────────────────────────────────────────
    def _add_boots(self):
        self.t.write_memory(BOOTS_ADDR, bytes([0x01]))
        ability = self._read(ABILITY_ADDR)
        return self.t.write_memory(ABILITY_ADDR, bytes([ability | RUN_ABILITY_MASK]))

    def _remove_boots(self):
        self.t.write_memory(BOOTS_ADDR, bytes([0x00]))
        ability = self._read(ABILITY_ADDR)
        return self.t.write_memory(ABILITY_ADDR, bytes([ability & ~RUN_ABILITY_MASK]))

    # ── bow (BowTracking byte $7EF38E is the wood/silver source of truth) ──
    def set_bow(self, level):
        """Set the bow to none (0), wood (1), or silver (2), authoritatively.

        ALTTPR reads wood-vs-silver from the BowTracking byte ($7EF38E): bit 0x80
        = a bow is present, 0x40 = silver upgrade, and silver arrows only fire when
        BOTH are set (z3randomizer gates on `BowTracking & 0xC0 == 0xC0`). The
        equipped byte ($7EF340) just picks which arrows fire, so we keep it in sync
        for the HUD but never rely on it. Returns the resulting BowTracking byte."""
        flags = self._read(BOW_FLAGS_ADDR)
        if level <= 0:
            flags &= ~(BOW_HAS_MASK | BOW_SILVER_MASK)
            equip = 0x00
        else:
            flags |= BOW_HAS_MASK
            if level >= 2:
                flags |= BOW_SILVER_MASK
                equip = 0x04          # silver bow + arrows (BowEquipment)
            else:
                flags &= ~BOW_SILVER_MASK
                equip = 0x01          # wood bow
        flags &= 0xFF
        self.t.write_memory(BOW_FLAGS_ADDR, bytes([flags]))
        self.t.write_memory(BOW_EQUIP_ADDR, bytes([equip]))
        if level >= 2 and self._read(ARROWS_ADDR) == 0:
            self.t.write_memory(ARROWS_ADDR, bytes([DEFAULT_ARROWS]))
        return self._read(BOW_FLAGS_ADDR)

    # ── public: add / remove ────────────────────────────────────────────
    SPECIAL = {
        "boomerang": ("_add_boomerang", {"blue": True}),
        "blue_boomerang": ("_add_boomerang", {"blue": True}),
        "red_boomerang": ("_add_boomerang", {"red": True}),
        "mushroom": ("_add_mushroom_powder", {"mushroom": True}),
        "powder": ("_add_mushroom_powder", {"powder": True}),
        "shovel": ("_add_shovel_flute", {"shovel": True}),
        "flute": ("_add_shovel_flute", {"flute": True}),
    }
    SPECIAL_REMOVE = {
        "boomerang": ("_remove_boomerang", {"blue": True}),
        "blue_boomerang": ("_remove_boomerang", {"blue": True}),
        "red_boomerang": ("_remove_boomerang", {"red": True}),
        "mushroom": ("_remove_mushroom_powder", {"mushroom": True}),
        "powder": ("_remove_mushroom_powder", {"powder": True}),
        "shovel": ("_remove_shovel_flute", {"shovel": True}),
        "flute": ("_remove_shovel_flute", {"flute": True}),
    }

    def add(self, item_key):
        """Grant an item, handling boots/shared-slots/silver-arrows correctly."""
        if item_key == "boots":
            return self._add_boots()
        if item_key in ("bow", "silver_arrows"):
            self.set_bow(2)
            return True
        if item_key in self.SPECIAL:
            method, kwargs = self.SPECIAL[item_key]
            return getattr(self, method)(**kwargs)
        # Generic item: flat write of its default value.
        if item_key in self.item_addresses:
            addr, value = self.item_addresses[item_key]
            return self.t.write_memory(addr, bytes([value]))
        logger.warning(f"[ItemEffects] Unknown item to add: {item_key}")
        return False

    def remove(self, item_key):
        """Remove an item, preserving the other half of shared slots."""
        if item_key == "boots":
            return self._remove_boots()
        if item_key in ("bow", "silver_arrows"):
            self.set_bow(0)
            return True
        if item_key in self.SPECIAL_REMOVE:
            method, kwargs = self.SPECIAL_REMOVE[item_key]
            return getattr(self, method)(**kwargs)
        if item_key in self.item_addresses:
            addr, _ = self.item_addresses[item_key]
            return self.t.write_memory(addr, bytes([0]))
        logger.warning(f"[ItemEffects] Unknown item to remove: {item_key}")
        return False

    # ── public: progressive upgrades ────────────────────────────────────
    def progressive(self, kind):
        """
        Bump an equipment tier by one, capped at its max. Returns the new level,
        or None if `kind` isn't progressive.
        """
        if kind not in PROGRESSIVE:
            logger.warning(f"[ItemEffects] Unknown progressive kind: {kind}")
            return None
        addr, cap = PROGRESSIVE[kind]
        cur = self._read(addr)
        new = min(cur + 1, cap)
        self.t.write_memory(addr, bytes([new]))
        logger.info(f"[ItemEffects] Progressive {kind}: {cur} -> {new}")
        return new

    # ── public: snapshot for timed-effect restoration ───────────────────
    def snapshot(self, item_key):
        """
        Capture what must be restored when a timed remove/add expires. Returns a
        dict {address: original_byte}. For boots this includes the ability flag.
        """
        snap = {}
        if item_key == "boots":
            snap[BOOTS_ADDR] = self._read(BOOTS_ADDR)
            snap[ABILITY_ADDR] = self._read(ABILITY_ADDR)
        elif item_key in ("boomerang", "blue_boomerang", "red_boomerang"):
            snap[INV_TRACK_ADDR] = self._read(INV_TRACK_ADDR)
            snap[BOOM_EQUIP_ADDR] = self._read(BOOM_EQUIP_ADDR)
        elif item_key in ("mushroom", "powder"):
            snap[INV_TRACK_ADDR] = self._read(INV_TRACK_ADDR)
            snap[POWDER_EQUIP_ADDR] = self._read(POWDER_EQUIP_ADDR)
        elif item_key in ("shovel", "flute"):
            snap[INV_TRACK_ADDR] = self._read(INV_TRACK_ADDR)
            snap[FLUTE_EQUIP_ADDR] = self._read(FLUTE_EQUIP_ADDR)
        elif item_key in ("bow", "silver_arrows"):
            snap[BOW_FLAGS_ADDR] = self._read(BOW_FLAGS_ADDR)
            snap[BOW_EQUIP_ADDR] = self._read(BOW_EQUIP_ADDR)
            snap[ARROWS_ADDR] = self._read(ARROWS_ADDR)
        elif item_key in self.item_addresses:
            addr, _ = self.item_addresses[item_key]
            snap[addr] = self._read(addr)
        return snap

    def restore(self, snapshot):
        """Write back a snapshot captured by snapshot()."""
        for addr, value in snapshot.items():
            self.t.write_memory(addr, bytes([value & 0xFF]))
