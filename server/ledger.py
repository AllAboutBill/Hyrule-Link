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

import random
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple

from shared.items import BY_KEY, tier_label, item_image
from shared import protocol as P
from server import db

# Game modes. "normal" = claim/find as usual. The shuffle modes auto-rotate
# ownership and disable manual claiming.
MODE_NORMAL = "normal"
MODE_HOT_POTATO = "hot_potato"   # each held item passes to the next finder after N s
MODE_CHAOS = "chaos"             # ALL found items randomly reassigned every N s
MODES = (MODE_NORMAL, MODE_HOT_POTATO, MODE_CHAOS)
MODE_LABELS = {MODE_NORMAL: "Normal", MODE_HOT_POTATO: "Hot Potato", MODE_CHAOS: "Chaos"}


# ── pure state ────────────────────────────────────────────────────────────
@dataclass
class ItemState:
    owner: Optional[int] = None          # user_id or None (unowned)
    level: int = 0                        # tier currently granted to the owner
    cooldown_until: float = 0.0
    held_since: float = 0.0               # when the current owner got it (hot potato)
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
    mode: str = MODE_NORMAL
    shuffle_s: float = 120.0   # rotation interval for the shuffle modes (seconds)
    last_shuffle: float = 0.0  # last chaos reshuffle time
    items: Dict[str, ItemState] = field(default_factory=dict)
    names: Dict[int, str] = field(default_factory=dict)    # user_id -> display name
    avatars: Dict[int, str] = field(default_factory=dict)  # user_id -> Discord avatar url
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
    it.held_since = now           # (re)start the hot-potato timer for this item

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
    if room.mode != MODE_NORMAL:
        return Effects(reject=f"{MODE_LABELS.get(room.mode, 'This')} mode moves items "
                              f"automatically — no claiming.")
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
    it.held_since = now

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
        keys = row.keys()
        room = RoomState(code=code, name=row["name"], pub_id=(row["pub_id"] or ""),
                         cooldown_s=float(row["cooldown_s"]),
                         host=int(row["host_player_id"] or 0),
                         mode=(row["mode"] if "mode" in keys and row["mode"] else MODE_NORMAL),
                         shuffle_s=float(row["shuffle_s"]) if "shuffle_s" in keys and row["shuffle_s"] else 120.0,
                         last_shuffle=time.time())
        for p in db.room_players(code):
            room.names[p["id"]] = p["display_name"]
            room.avatars[p["id"]] = p["avatar"]
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
        now = time.time()
        for it in room.items.values():
            if it.owner is not None:
                it.discovered[it.owner] = max(it.discovered.get(it.owner, 0), it.level)
            it.held_since = now      # restart hot-potato timers from load
        self.rooms[code] = room
        self.agents.setdefault(code, {})
        self.uis.setdefault(code, {})
        return room

    def refresh_names(self, code: str):
        room = self.rooms.get(code)
        if room:
            for p in db.room_players(code):
                room.names[p["id"]] = p["display_name"]
                room.avatars[p["id"]] = p["avatar"]

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
            # If nobody owns it and nobody has it found (e.g. a host un-found it for
            # every player), the token is effectively undiscovered again — emit no
            # entry so the UI shows it dimmed instead of a stale "lit up" card.
            if it.owner is None and not it.discovered:
                continue
            # An unowned token has no "current" tier (each finder has their own),
            # so don't surface the last holder's level/tier until someone claims.
            owned = it.owner is not None
            level = it.level if owned else 0
            entry = {
                "name": item.name,
                "owner": it.owner,
                "owner_name": room.names.get(it.owner) if it.owner else None,
                "level": level,
                "tier": tier_label(item, level) if owned else "—",
                "image": item_image(key, level),
                "discovered": sorted(it.discovered),
                "cooldown_remaining": max(0.0, it.cooldown_until - now),
            }
            if room.mode == MODE_HOT_POTATO and owned:
                entry["hold_remaining"] = max(0.0, it.held_since + room.shuffle_s - now)
            ledger[key] = entry
        return {
            "type": P.STATE,
            "room": room.pub_id,      # public handle only — never leak the join code
            "name": room.name,
            "cooldown_s": room.cooldown_s,
            "host": room.host,
            "mode": room.mode,
            "shuffle_s": room.shuffle_s,
            "shuffle_remaining": (max(0.0, room.last_shuffle + room.shuffle_s - now)
                                  if room.mode == MODE_CHAOS else 0.0),
            "players": [
                {"id": uid, "name": nm, "avatar": room.avatars.get(uid),
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
            it.held_since = time.time()
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

    async def admin_set_name(self, code: str, name: str):
        room = self.rooms.get(code)
        name = (name or "").strip()[:60]
        if room is None or not name:
            return
        room.name = name
        db.update_name(code, name)
        await self.broadcast_event(code, f"Room renamed to “{name}”")
        await self.broadcast_state(code)

    # ── game modes: auto-shuffle engine ──────────────────────────────────────
    async def admin_set_mode(self, code: str, mode: str, seconds=None):
        room = self.rooms.get(code)
        if room is None:
            return
        room.mode = mode if mode in MODES else MODE_NORMAL
        if seconds:
            try:
                room.shuffle_s = max(5.0, float(seconds))
            except (TypeError, ValueError):
                pass
        now = time.time()
        room.last_shuffle = now
        for it in room.items.values():        # everyone keeps their item a full round first
            it.held_since = now
        db.update_mode(code, room.mode, room.shuffle_s)
        if room.mode == MODE_NORMAL:
            await self.broadcast_event(code, "Host set mode to Normal — claiming is back on.")
        else:
            await self.broadcast_event(
                code, f"Host started {MODE_LABELS[room.mode]} — items shuffle every "
                      f"{int(room.shuffle_s)}s. Claiming is off.")
        await self.broadcast_state(code)

    def _available_finders(self, room: RoomState, it: ItemState) -> List[int]:
        """Discoverers who can actually receive the item right now (agent online),
        in stable id order so hot-potato rotation is a predictable round-robin."""
        return [uid for uid in sorted(it.discovered)
                if uid in room.names and room.status.get(uid, {}).get("agent")]

    def _reassign(self, room: RoomState, key: str, new_owner: int, now: float, grants, revokes):
        it = room.items[key]
        prev = it.owner
        it.owner = new_owner
        it.level = it.discovered.get(new_owner, BY_KEY[key].present)
        it.held_since = now
        grants.append((new_owner, key, it.level))
        if prev is not None and prev != new_owner:
            revokes.append((prev, key))
        self._persist(room, key)

    async def tick_shuffles(self):
        """Driven once a second by the server; rotates items in any room whose
        mode is active and whose timer is due."""
        now = time.time()
        for code in list(self.rooms.keys()):
            room = self.rooms.get(code)
            if room is None or room.mode == MODE_NORMAL:
                continue
            try:
                if room.mode == MODE_HOT_POTATO:
                    await self._tick_hot_potato(code, room, now)
                elif room.mode == MODE_CHAOS:
                    await self._tick_chaos(code, room, now)
            except Exception:
                pass

    async def _tick_hot_potato(self, code, room, now):
        grants, revokes, events = [], [], []
        for key, it in room.items.items():
            if not it.discovered:
                continue
            avail = self._available_finders(room, it)
            if not avail:
                continue
            if it.owner is None:                      # pull a found-but-unheld item in
                new = avail[0]
            else:
                if now - it.held_since < room.shuffle_s:
                    continue                          # still their turn
                if it.owner in avail:
                    if len(avail) == 1:               # only the holder is online → keep
                        it.held_since = now
                        continue
                    new = avail[(avail.index(it.owner) + 1) % len(avail)]
                else:
                    new = avail[0]                    # holder went offline → hand it on
            if new == it.owner:
                it.held_since = now
                continue
            self._reassign(room, key, new, now, grants, revokes)
            events.append(f"🔥 {BY_KEY[key].name} → {room.names.get(new, 'someone')}")
        if grants or revokes:
            await self._send_commands(code, grants, revokes)
            for ev in events:
                await self.broadcast_event(code, ev)
            await self.broadcast_state(code)

    async def _tick_chaos(self, code, room, now):
        if now - room.last_shuffle < room.shuffle_s:
            return
        room.last_shuffle = now
        grants, revokes = [], []
        for key, it in room.items.items():
            avail = self._available_finders(room, it)
            if not avail:
                continue
            new = random.choice(avail)
            if new == it.owner:
                continue
            self._reassign(room, key, new, now, grants, revokes)
        if grants or revokes:
            await self._send_commands(code, grants, revokes)
            await self.broadcast_event(code, "🌀 Chaos shuffle! Everything moved.")
            await self.broadcast_state(code)

    async def _send_commands(self, code, grants, revokes):
        db.touch_room(code)
        for (uid, key, level) in grants:
            ws = self.agents.get(code, {}).get(uid)
            if ws:
                await self._send(ws, {"type": P.GRANT, "item": key, "level": level})
        for (uid, key) in revokes:
            ws = self.agents.get(code, {}).get(uid)
            if ws:
                await self._send(ws, {"type": P.REVOKE, "item": key})

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
