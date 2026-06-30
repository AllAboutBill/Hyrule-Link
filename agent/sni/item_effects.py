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
    BOW_FLAGS_ADDR, BOW_HAS_MASK, BOW_SILVER_MASK, BOW_EQUIP_ADDR,
)

logger = logging.getLogger("ItemEffects")

# Bitfield slot addresses (ALttPR)
BOOMERANG_ADDR = 0xF341   # bit0 = blue, bit1 = red
MUSHROOM_ADDR = 0xF344    # bit0 = mushroom, bit1 = powder
SHOVEL_FLUTE_ADDR = 0xF34C  # 1 = shovel, 2 = flute, 3 = flute (activated)

# Boots + dash ability
BOOTS_ADDR = 0xF355
ABILITY_ADDR = 0xF379
RUN_ABILITY_MASK = 0x04

# Bow + arrows. Wood-vs-silver lives in the BowTracking byte ($7EF38E, imported as
# BOW_FLAGS_ADDR), NOT the equipped byte ($7EF340 / BOW_EQUIP_ADDR) — see set_bow()
# and shared.items for why writing $7EF340 alone never enables silver firing.
ARROWS_ADDR = 0xF377
DEFAULT_ARROWS = 30

# Y-button selected-item index
SELECTED_INDEX_ADDR = 0xF33F

# Progressive equipment: address -> max level. (The bow is NOT a plain counter —
# its wood/silver state is a bitfield in $7EF38E; see set_bow().)
PROGRESSIVE = {
    "sword": (0xF359, 4),   # 0 none .. 4 gold
    "shield": (0xF35A, 3),  # 0 none .. 3 mirror
    "armor": (0xF35B, 2),   # 0 green .. 2 red
    "gloves": (0xF354, 2),  # 0 none .. 2 titan
}

# Menu selection codes for the Y-selectable shared-slot items (ALttPR).
SEL_BLUE_BOOM = 0x08
SEL_RED_BOOM = 0x09
SEL_MUSHROOM = 0x0A
SEL_POWDER = 0x0B
SEL_SHOVEL = 0x0D
SEL_FLUTE = 0x0E


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
    def _read(self, addr):
        data = self.t.read_memory(addr, size=1)
        return int(data[0]) if data and len(data) >= 1 else 0

    def _set_slot(self, addr, value, selected_index=None):
        ok = self.t.write_memory(addr, bytes([value & 0xFF]))
        if ok and selected_index is not None:
            self.t.write_memory(SELECTED_INDEX_ADDR, bytes([selected_index]))
        return ok

    # ── shared bitfield slots ───────────────────────────────────────────
    def _add_boomerang(self, blue=False, red=False):
        cur = self._read(BOOMERANG_ADDR)
        new = cur | (0x01 if blue else 0) | (0x02 if red else 0)
        sel = SEL_BLUE_BOOM if (blue or new & 0x01) else SEL_RED_BOOM
        return self._set_slot(BOOMERANG_ADDR, new, sel)

    def _remove_boomerang(self, blue=False, red=False):
        cur = self._read(BOOMERANG_ADDR)
        new = cur & ~((0x01 if blue else 0) | (0x02 if red else 0))
        sel = SEL_BLUE_BOOM if new & 0x01 else (SEL_RED_BOOM if new & 0x02 else None)
        return self._set_slot(BOOMERANG_ADDR, new, sel)

    def _add_mushroom_powder(self, mushroom=False, powder=False):
        cur = self._read(MUSHROOM_ADDR)
        new = cur | (0x01 if mushroom else 0) | (0x02 if powder else 0)
        sel = SEL_MUSHROOM if (mushroom or new & 0x01) else SEL_POWDER
        return self._set_slot(MUSHROOM_ADDR, new, sel)

    def _remove_mushroom_powder(self, mushroom=False, powder=False):
        cur = self._read(MUSHROOM_ADDR)
        new = cur & ~((0x01 if mushroom else 0) | (0x02 if powder else 0))
        sel = SEL_MUSHROOM if new & 0x01 else (SEL_POWDER if new & 0x02 else None)
        return self._set_slot(MUSHROOM_ADDR, new, sel)

    def _add_shovel_flute(self, shovel=False, flute=False):
        cur = self._read(SHOVEL_FLUTE_ADDR)
        new = cur
        if shovel:
            new |= 0x01
        if flute:
            new |= 0x02          # flute present (inactive); 0x03 = activated
        sel = SEL_FLUTE if (flute or new & 0x02) else SEL_SHOVEL
        return self._set_slot(SHOVEL_FLUTE_ADDR, new, sel)

    def _remove_shovel_flute(self, shovel=False, flute=False):
        cur = self._read(SHOVEL_FLUTE_ADDR)
        new = cur
        if shovel:
            new &= ~0x01
        if flute:
            new &= ~0x03          # clear flute + activated bit
        sel = SEL_SHOVEL if new & 0x01 else (SEL_FLUTE if new & 0x02 else None)
        return self._set_slot(SHOVEL_FLUTE_ADDR, new, sel)

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
            snap[BOOMERANG_ADDR] = self._read(BOOMERANG_ADDR)
        elif item_key in ("mushroom", "powder"):
            snap[MUSHROOM_ADDR] = self._read(MUSHROOM_ADDR)
        elif item_key in ("shovel", "flute"):
            snap[SHOVEL_FLUTE_ADDR] = self._read(SHOVEL_FLUTE_ADDR)
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
