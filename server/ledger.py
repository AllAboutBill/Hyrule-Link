"""
ledger.py — authoritative ownership rules + the live connection hub.

The server is the single source of truth: agents report physical pickups and
UIs request claims; this module decides who owns each item and emits the
grant/revoke commands that agents apply to their games.

Rules (locked with the user):
  * Pool = progression items only (see shared.items).
  * Progressive tier is PER PLAYER: each player gets back the highest tier THEY
    have personally found. If A found Master and B found Gold, claiming the
    sword gives A a Master and B a Gold — A only reaches Gold once A finds it.
  * You can only CLAIM an item you have personally discovered (found in-world)
    at least once. Physical pickups always win (you literally found one).
  * Last action wins, with a per-room cooldown to stop two clickers ripping an
    item back and forth.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple

from shared.items import BY_KEY, tier_label, item_image
from shared import protocol as P
from server import db


# ── pure state ────────────────────────────────────────────────────────────
@dataclass
class ItemState:
    owner: Optional[int] = None          # user_id or None (unowned)
    level: int = 0                        # tier currently granted to the owner
    cooldown_until: float = 0.0
    # user_id -> the highest tier THAT player has personally found. Membership
    # (the keys) is "has discovered it"; the value is that player's own tier.
    discovered: Dict[int, int] = field(default_factory=dict)


@dataclass
class RoomState:
    code: str
    name: str = "Co-op"
    pub_id: str = ""        # public watch handle (safe to broadcast; code is not)
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

    # Track THIS player's own best tier (monotonic). Finding never lowers it.
    prev_personal = it.discovered.get(user_id, 0)
    personal = max(prev_personal, level)
    it.discovered[user_id] = personal
    upgraded = personal > prev_personal
    prev = it.owner

    # Physical pickup always takes the token; the finder holds it at THEIR tier.
    it.owner = user_id
    it.level = personal
    it.cooldown_until = now + room.cooldown_s

    eff = Effects(changed=True)
    eff.grants.append((user_id, key, personal))
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
    # Claiming gives YOU back your own tier, not whatever the last holder had.
    it.level = it.discovered.get(user_id, item.present)
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
        self.uis: Dict[str, Dict[object, int]] = {}      # code -> {ws: user_id|None}
        self.admin_uis: Dict[str, set] = {}              # code -> {ws} granted global-admin

    # -- room loading --------------------------------------------------------
    def get_room(self, code: str) -> Optional[RoomState]:
        if code in self.rooms:
            return self.rooms[code]
        row = db.get_room(code)
        if not row:
            return None
        room = RoomState(code=code, name=row["name"], pub_id=(row["pub_id"] or ""),
                         cooldown_s=float(row["cooldown_s"]),
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
            st = room.items.setdefault(d["item_key"], ItemState())
            st.discovered[d["player_id"]] = d["level"]
        # The current owner provably found at least the tier they're holding, so
        # make sure their personal record reflects it (covers legacy rows that
        # predate per-player levels and default to 1).
        for it in room.items.values():
            if it.owner is not None:
                it.discovered[it.owner] = max(it.discovered.get(it.owner, 0), it.level)
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

    def register_ui(self, code, user_id, ws, is_admin=False):
        self.uis.setdefault(code, {})[ws] = user_id
        if is_admin:
            self.admin_uis.setdefault(code, set()).add(ws)

    def unregister_ui(self, code, ws):
        self.uis.get(code, {}).pop(ws, None)
        self.admin_uis.get(code, set()).discard(ws)

    def is_admin_ui(self, code, ws) -> bool:
        return ws in self.admin_uis.get(code, set())

    # -- persistence ---------------------------------------------------------
    def _persist(self, room: RoomState, key: str):
        it = room.items[key]
        db.upsert_ledger(room.code, key, it.owner, it.level, it.cooldown_until)
        for uid, lvl in it.discovered.items():
            db.add_discovered(room.code, key, uid, lvl)

    # -- dispatch ------------------------------------------------------------
    async def _send(self, ws, payload):
        try:
            await ws.send_json(payload)
        except Exception:
            pass

    async def dispatch(self, code: str, eff: Effects, key: str):
        room = self.rooms.get(code)
        if room is None:        # room was deleted out from under an in-flight action
            return
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
                "image": item_image(key, it.level),
                "discovered": sorted(it.discovered),
                "cooldown_remaining": max(0.0, it.cooldown_until - now),
            }
        return {
            "type": P.STATE,
            "room": room.pub_id,      # public handle only — never leak the join code
            "name": room.name,
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
        admins = self.admin_uis.get(code, set())
        base = self.serialize(code)
        for ws, uid in list(self.uis.get(code, {}).items()):
            payload = dict(base)
            payload["you"] = uid
            payload["admin"] = ws in admins        # global admin → host-like controls
            payload["spectator"] = uid is None and ws not in admins
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
            it.discovered.pop(player_id, None)
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
            # Host-marked discovery counts as the base ("present") tier; an actual
            # in-world pickup later raises it to that player's real tier.
            it.discovered.setdefault(player_id, item.present)
        else:
            it.discovered.pop(player_id, None)
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
            it.discovered.setdefault(player_id, item.present)  # owning implies discovered
            it.owner = player_id
            it.level = it.discovered[player_id]   # grant their own tier
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

    async def drop_room(self, code: str):
        """Tear a room down: close every live connection and forget it in memory
        (the DB row is removed separately). Used by the global-admin delete."""
        for ws in list(self.agents.get(code, {}).values()):
            try:
                await ws.close()
            except Exception:
                pass
        for ws in list(self.uis.get(code, {}).keys()):
            try:
                await ws.close()
            except Exception:
                pass
        self.rooms.pop(code, None)
        self.agents.pop(code, None)
        self.uis.pop(code, None)
        self.admin_uis.pop(code, None)


hub = RoomHub()
