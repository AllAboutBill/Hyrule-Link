"""
effects.py — translate ledger commands into correct WRAM writes.

Thin wrapper over the vendored sni.item_effects.ItemManager so we can enable an
item at an *exact* level (the ledger's max-found tier) and disable it cleanly,
without clobbering shared bytes. ItemManager already handles the nasty cases
(boots' run-ability flag, bitfield boomerang/mushroom/shovel halves, silver-bow
+ arrows); we only add exact-level setting for progressive items.

Every enable/disable returns the resulting raw byte at the item's address so the
poller can echo-cancel its own write (and not re-report it as a fresh pickup).
"""

from .sni.item_effects import ItemManager, ARROWS_ADDR, DEFAULT_ARROWS
from shared.items import BY_KEY, ITEM_ADDRESSES


class Effects:
    def __init__(self, transport):
        self.t = transport
        self.mgr = ItemManager(transport, item_addresses=ITEM_ADDRESSES)

    def _read(self, addr) -> int:
        d = self.t.read_memory(addr, size=1)
        return int(d[0]) if d and len(d) >= 1 else 0

    def enable(self, key: str, level: int) -> int:
        item = BY_KEY[key]
        if item.kind == "progressive":
            self.t.write_memory(item.addr, bytes([level & 0xFF]))
            if key == "bow" and level >= 2 and self._read(ARROWS_ADDR) == 0:
                self.t.write_memory(ARROWS_ADDR, bytes([DEFAULT_ARROWS]))
        elif item.kind == "boots":
            self.mgr._add_boots()
        elif item.kind == "bitfield":
            self.mgr.add(item.effect_key)
        elif item.kind == "bottle":
            self.t.write_memory(item.addr, bytes([0x02]))  # empty bottle
        else:  # simple single-byte
            self.mgr.add(key)
        return self._read(item.addr)

    def disable(self, key: str) -> int:
        item = BY_KEY[key]
        if item.kind == "progressive":
            self.t.write_memory(item.addr, bytes([0x00]))
        elif item.kind == "boots":
            self.mgr._remove_boots()
        elif item.kind == "bitfield":
            self.mgr.remove(item.effect_key)
        elif item.kind == "bottle":
            self.t.write_memory(item.addr, bytes([0x00]))
        else:
            self.mgr.remove(key)
        return self._read(item.addr)
