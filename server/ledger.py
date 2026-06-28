"""
ledger.py — authoritative ownership rules + the live connection hub.

The server is the single source of truth: agents report physical pickups and
UIs request claims; this module decides who owns each item and emits the
grant/revoke commands that agents apply to their games.

Rules (locked with the user):
  * Pool = progression items only (see shared.items).
  * A token carries the MAX level ever found, so claiming "the sword" gives you
    whatever tier is highest.
  * You can only CLAIM an item you have personally discovered (found in-world)
    at least once. Physical pickups always win (you literally found one).
  * Last action wins, with a per-room cooldown to stop two clickers ripping an
    item back and forth.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, List, Tuple

from shared.items import BY_KEY, tier_label
from shared import protocol as P
from server import db


# ── pure state ────────────────────────────────────────────────────────────
@dataclass
class ItemState:
    owner: Optional[int] = None          # user_id or None (unowned)
    level: int = 0                        # max tier discovered
    cooldown_until: float = 0.0
    discovered: Set[int] = field(default_factory=set)


@dataclass
class RoomState:
    code: str
    cooldown_s: float = 5.0
    host: int = 0  # host_user_id (room creator / admin)
    items: Dict[str, ItemState] = field(default_factory=dict)
    names: Dict[int, str] = field(default_factory=dict)  # user_id -> display name
    # connectivity per player: user_id -> {"agent": bool, "emu": bool}
    status: Dict[int, dict] = field(default_factory=dict)


@dataclass
class Effects:
    """Result of resolving a pickup/claim: commands + log line, or a rejection."""
    grants: List[Tuple[int, str, int]] = field(default_factory=list)   # (user, key, level)
    revokes: List[Tuple[int, str]] = field(default_factory=list)       # (user, key)
    event: Optional[str] = None
    reject: Optional[str] = None
    changed: bool = False


# ── rule resolution (pure, mutates RoomState) ──────────────────────────────
def resolve_pickup(room: RoomState, user_id: int, key: str, level: int) -> Effects:
    item = BY_KEY.get(key)
    if item is None:
        return Effects(reject=f"unknown item {key}")
    now = time.time()
    it = room.items.setdefault(key, ItemState())
    first_seen = user_id not in it.discovered
    it.discovered.add(user_id)

    new_level = max(it.level, level)
    upgraded = new_level > it.level
    prev = it.owner

    it.owner = user_id
    it.level = new_level
    it.cooldown_until = now + room.cooldown_s

    eff = Effects(changed=True)
    # Ensure the finder holds the (possibly upgraded-to-max) level.
    eff.grants.append((user_id, key, new_level))
    name = room.names.get(user_id, f"player {user_id}")
    if prev is not None and prev != user_id:
        eff.revokes.append((prev, key))
        eff.event = f"{name} grabbed {item.name} from {room.names.get(prev, 'someone')}"
    elif prev == user_id:
        eff.event = f"{name} upgraded {item.name}" if upgraded else None
    else:
        eff.event = f"{name} found {item.name}" + ("" if first_seen else " again")
    return eff


def resolve_claim(room: RoomState, user_id: int, key: str) -> Effects:
    item = BY_KEY.get(key)
    if item is None:
        return Effects(reject=f"unknown item {key}")
    it = room.items.get(key)
    name = room.names.get(user_id, f"player {user_id}")
    if it is None or user_id not in it.discovered:
        return Effects(reject=f"You haven't found {item.name} yet — go find one first.")
    now = time.time()
    if now < it.cooldown_until:
        wait = it.cooldown_until - now
        return Effects(reject=f"{item.name} is on cooldown ({wait:.0f}s).")
    if it.owner == user_id:
        return Effects(reject=f"You already hold {item.name}.")

    prev = it.owner
    it.owner = user_id
    it.cooldown_until = now + room.cooldown_s

    eff = Effects(changed=True)
    eff.grants.append((user_id, key, it.level))
    if prev is not None:
        eff.revokes.append((prev, key))
        eff.event = f"{name} reclaimed {item.name} from {room.names.get(prev, 'someone')}"
    else:
        eff.event = f"{name} claimed {item.name}"
    return eff


# ── live hub: connections + persistence + dispatch ─────────────────────────
class RoomHub:
    def __init__(self):
        self.rooms: Dict[str, RoomState] = {}
        self.agents: Dict[str, Dict[int, object]] = {}   # code -> {user_id: ws}
        self.uis: Dict[str, Dict[object, int]] = {}      # code -> {ws: user_id}

    # -- room loading --------------------------------------------------------
    def get_room(self, code: str) -> Optional[RoomState]:
        if code in self.rooms:
            return self.rooms[code]
        row = db.get_room(code)
        if not row:
            return None
        room = RoomState(code=code, cooldown_s=float(row["cooldown_s"]),
                         host=int(row["host_player_id"] or 0))
        for p in db.room_players(code):
            room.names[p["id"]] = p["display_name"]
        ledger_rows, disc_rows = db.load_ledger(code)
        for r in ledger_rows:
            room.items[r["item_key"]] = ItemState(
                owner=r["owner_player_id"],
                level=r["level"],
                cooldown_until=r["cooldown_until"],
            )
        for d in disc_rows:
            room.items.setdefault(d["item_key"], ItemState()).discovered.add(d["player_id"])
        self.rooms[code] = room
        self.agents.setdefault(code, {})
        self.uis.setdefault(code, {})
        return room

    def refresh_names(self, code: str):
        room = self.rooms.get(code)
        if room:
            for p in db.room_players(code):
                room.names[p["id"]] = p["display_name"]

    # -- connection registries ----------------------------------------------
    def register_agent(self, code, user_id, ws):
        self.agents.setdefault(code, {})[user_id] = ws
        room = self.rooms.get(code)
        if room is not None:
            room.status.setdefault(user_id, {"agent": False, "emu": False})["agent"] = True

    def unregister_agent(self, code, user_id, ws):
        if self.agents.get(code, {}).get(user_id) is ws:
            del self.agents[code][user_id]
        room = self.rooms.get(code)
        if room is not None:
            room.status[user_id] = {"agent": False, "emu": False}

    def set_emu_status(self, code, user_id, emu):
        room = self.rooms.get(code)
        if room is not None:
            room.status.setdefault(user_id, {"agent": True, "emu": False})["emu"] = bool(emu)

    def register_ui(self, code, user_id, ws):
        self.uis.setdefault(code, {})[ws] = user_id

    def unregister_ui(self, code, ws):
        self.uis.get(code, {}).pop(ws, None)

    # -- persistence ---------------------------------------------------------
    def _persist(self, room: RoomState, key: str):
        it = room.items[key]
        db.upsert_ledger(room.code, key, it.owner, it.level, it.cooldown_until)
        for uid in it.discovered:
            db.add_discovered(room.code, key, uid)

    # -- dispatch ------------------------------------------------------------
    async def _send(self, ws, payload):
        try:
            await ws.send_json(payload)
        except Exception:
            pass

    async def dispatch(self, code: str, eff: Effects, key: str):
        room = self.rooms[code]
        db.touch_room(code)   # item activity keeps the room from being pruned
        for (uid, ikey, level) in eff.grants:
            ws = self.agents.get(code, {}).get(uid)
            if ws:
                await self._send(ws, {"type": P.GRANT, "item": ikey, "level": level})
        for (uid, ikey) in eff.revokes:
            ws = self.agents.get(code, {}).get(uid)
            if ws:
                await self._send(ws, {"type": P.REVOKE, "item": ikey})
        if eff.changed:
            self._persist(room, key)
        if eff.event:
            await self.broadcast_event(code, eff.event)
        await self.broadcast_state(code)

    # -- state serialization for UIs ----------------------------------------
    def serialize(self, code: str) -> dict:
        room = self.rooms[code]
        now = time.time()
        ledger = {}
        for key, it in room.items.items():
            item = BY_KEY.get(key)
            if not item:
                continue
            ledger[key] = {
                "name": item.name,
                "owner": it.owner,
                "owner_name": room.names.get(it.owner) if it.owner else None,
                "level": it.level,
                "tier": tier_label(item, it.level),
                "discovered": sorted(it.discovered),
                "cooldown_remaining": max(0.0, it.cooldown_until - now),
            }
        return {
            "type": P.STATE,
            "room": code,
            "cooldown_s": room.cooldown_s,
            "host": room.host,
            "players": [
                {"id": uid, "name": nm,
                 "agent": room.status.get(uid, {}).get("agent", False),
                 "emu": room.status.get(uid, {}).get("emu", False)}
                for uid, nm in room.names.items()
            ],
            "ledger": ledger,
        }

    async def broadcast_state(self, code: str):
        if code not in self.rooms:
            return
        for ws, uid in list(self.uis.get(code, {}).items()):
            payload = self.serialize(code)
            payload["you"] = uid
            await self._send(ws, payload)

    async def broadcast_event(self, code: str, text: str):
        payload = {"type": P.EVENT, "text": text, "ts": time.time()}
        for ws in list(self.uis.get(code, {}).keys()):
            await self._send(ws, payload)

    # -- host admin actions (caller must verify user == room.host) -----------
    async def admin_set_cooldown(self, code: str, seconds: float):
        room = self.rooms[code]
        room.cooldown_s = max(0.0, float(seconds))
        db.update_cooldown(code, room.cooldown_s)
        await self.broadcast_event(code, f"Host set steal cooldown to {room.cooldown_s:.0f}s")
        await self.broadcast_state(code)

    async def admin_remove_player(self, code: str, player_id: int):
        room = self.rooms[code]
        if player_id == room.host or player_id not in room.names:
            return  # never remove the host (or a stranger)
        name = room.names.get(player_id, f"player {player_id}")
        # release their owned items and tell their agent to drop them
        for key, it in room.items.items():
            if it.owner == player_id:
                it.owner = None
                ws = self.agents.get(code, {}).get(player_id)
                if ws:
                    await self._send(ws, {"type": P.REVOKE, "item": key})
            it.discovered.discard(player_id)
            self._persist(room, key)
        db.remove_player(code, player_id)
        room.names.pop(player_id, None)
        # close their live connections
        aws = self.agents.get(code, {}).pop(player_id, None)
        if aws:
            try:
                await aws.close()
            except Exception:
                pass
        for ws, uid in list(self.uis.get(code, {}).items()):
            if uid == player_id:
                self.uis[code].pop(ws, None)
                try:
                    await ws.close()
                except Exception:
                    pass
        await self.broadcast_event(code, f"Host removed {name}")
        await self.broadcast_state(code)

    async def admin_set_discovered(self, code: str, player_id: int, key: str, found: bool):
        room = self.rooms[code]
        item = BY_KEY.get(key)
        if not item or player_id not in room.names:
            return
        it = room.items.setdefault(key, ItemState())
        if found:
            it.discovered.add(player_id)
        else:
            it.discovered.discard(player_id)
            db.remove_discovered(code, key, player_id)
            if it.owner == player_id:        # can't own what you haven't found
                it.owner = None
                ws = self.agents.get(code, {}).get(player_id)
                if ws:
                    await self._send(ws, {"type": P.REVOKE, "item": key})
        self._persist(room, key)
        verb = "found" if found else "un-found"
        await self.broadcast_event(
            code, f"Host marked {item.name} {verb} for {room.names.get(player_id)}")
        await self.broadcast_state(code)

    async def admin_set_owner(self, code: str, player_id, key: str):
        """Force an item's owner (player_id) or clear it (player_id None)."""
        room = self.rooms[code]
        item = BY_KEY.get(key)
        if not item:
            return
        it = room.items.setdefault(key, ItemState())
        prev = it.owner
        if player_id is not None:
            it.discovered.add(player_id)  # owning implies discovered
            it.owner = player_id
            if it.level == 0:
                it.level = item.present
            ws = self.agents.get(code, {}).get(player_id)
            if ws:
                await self._send(ws, {"type": P.GRANT, "item": key, "level": it.level})
        else:
            it.owner = None
        if prev is not None and prev != player_id:
            ws = self.agents.get(code, {}).get(prev)
            if ws:
                await self._send(ws, {"type": P.REVOKE, "item": key})
        self._persist(room, key)
        await self.broadcast_state(code)


hub = RoomHub()
