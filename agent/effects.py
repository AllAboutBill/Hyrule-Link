"""
effects.py — translate ledger commands into correct WRAM writes.

Thin wrapper over the vendored sni.item_effects.ItemManager so we can enable an
item at an *exact* level (the owner's own found tier) and disable it cleanly,
without clobbering shared bytes. ItemManager already handles the nasty cases
(boots' run-ability flag, bitfield boomerang/mushroom/shovel halves, silver-bow
+ arrows); we only add exact-level setting for progressive items.

Every enable/disable returns the resulting raw byte at the item's address so the
poller can echo-cancel its own write (and not re-report it as a fresh pickup).
"""

from .sni.item_effects import ItemManager
from shared.items import BY_KEY, ITEM_ADDRESSES, BOW_HAS_MASK, BOW_SILVER_MASK


class Effects:
    def __init__(self, transport):
        self.t = transport
        self.mgr = ItemManager(transport, item_addresses=ITEM_ADDRESSES)

    def _read(self, addr) -> int:
        d = self.t.read_memory(addr, size=1)
        if not d or len(d) < 1:
            raise IOError(f"could not read WRAM ${addr:04X}")
        return int(d[0])

    @staticmethod
    def _matches(item, raw: int, level: int, enable: bool) -> bool:
        if item.kind == "bitfield":
            return bool(raw & item.mask) == enable
        if item.kind == "bow":
            has = bool(raw & BOW_HAS_MASK)
            if not enable:
                return not has
            return has and (bool(raw & BOW_SILVER_MASK) == (level >= 2))
        expected = (level if item.kind == "progressive" else item.give) if enable else 0
        return raw == expected

    def enable(self, key: str, level: int) -> int:
        item = BY_KEY[key]
        if item.kind == "bow":
            self.mgr.set_bow(level)   # writes BowTracking ($7EF38E) + equip + arrows
        elif item.kind == "progressive":
            self.t.write_memory(item.addr, bytes([level & 0xFF]))
        elif item.kind == "boots":
            self.mgr._add_boots()
        elif item.kind == "bitfield":
            self.mgr.add(item.effect_key)
        else:  # simple single-byte
            self.mgr.add(key)
        raw = self._read(item.addr)
        if not self._matches(item, raw, level, True):
            raise IOError(f"grant verification failed for {key}: read {raw}")
        return raw

    def disable(self, key: str) -> int:
        item = BY_KEY[key]
        if item.kind == "bow":
            self.mgr.set_bow(0)
        elif item.kind == "progressive":
            self.t.write_memory(item.addr, bytes([0x00]))
        elif item.kind == "boots":
            self.mgr._remove_boots()
        elif item.kind == "bitfield":
            self.mgr.remove(item.effect_key)
        else:
            self.mgr.remove(key)
        raw = self._read(item.addr)
        if not self._matches(item, raw, 0, False):
            raise IOError(f"revoke verification failed for {key}: read {raw}")
        return raw
