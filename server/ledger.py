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

import json
import logging
import math
import random
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple

from shared.items import ITEMS, BY_KEY, tier_label, item_image
from shared import protocol as P
from shared.rules import DEFAULT_RULES, PRESET_OVERRIDES
from server import db

logger = logging.getLogger("HyruleLink.ledger")

# Game modes. "normal" = claim/find as usual. The shuffle modes auto-rotate
# ownership and disable manual claiming.
MODE_NORMAL = "normal"
MODE_HOT_POTATO = "hot_potato"   # each held item passes to the next finder after N s
MODE_CHAOS = "chaos"             # ALL found items randomly reassigned every N s
MODE_CUSTOM = "custom"           # host-composed ruleset (see DEFAULT_RULES)
MODES = (MODE_NORMAL, MODE_HOT_POTATO, MODE_CHAOS, MODE_CUSTOM)
MODE_LABELS = {MODE_NORMAL: "Normal", MODE_HOT_POTATO: "Hot Potato",
               MODE_CHAOS: "Chaos", MODE_CUSTOM: "Custom"}


# ── custom rules ───────────────────────────────────────────────────────────
# The whole game is one rule engine; the named modes are just presets. A room
# always carries a full effective ruleset (room.rules); resolve_claim and the
# 1-second tick read it. See the design spec for what each knob does.
_RULE_BOOLS = ("claiming", "require_found_to_claim", "shared_discovery")
_RULE_ENUMS = {
    "open_season_scope": ("owned", "any"),
    "cooldown_scope":    ("item", "thief", "victim", "none"),
    "hold_expiry":       ("next_finder", "release", "return_finder"),
    "borrow_revert":     ("prev_owner", "pool"),
    "shuffle_scope":     ("all", "unowned", "idle"),
}
_RULE_NUMS = {   # field -> (min, max)
    "steal_cooldown_s":     (0.0, 3600.0),
    "steal_back_lock_s":    (0.0, 3600.0),
    "steal_budget_per_min": (0, 120),
    "hold_limit_s":         (0.0, 86400.0),
    "tenure_lock_s":        (0.0, 86400.0),
    "idle_release_s":       (0.0, 86400.0),
    "borrow_s":             (0.0, 86400.0),
    "auto_shuffle_s":       (0.0, 86400.0),
}


def clamp_rules(raw: dict) -> dict:
    """Validate + clamp a client-supplied ruleset to the DEFAULT_RULES shape.
    Never trust the client: unknown keys are dropped, values are coerced/bounded."""
    out = dict(DEFAULT_RULES)
    raw = raw or {}
    for k in _RULE_BOOLS:
        if k in raw:
            out[k] = bool(raw[k])
    for k, (lo, hi) in _RULE_NUMS.items():
        if k in raw:
            try:
                v = int(raw[k]) if k == "steal_budget_per_min" else float(raw[k])
                if not math.isfinite(v):
                    continue
                out[k] = max(lo, min(hi, v))
            except (TypeError, ValueError):
                pass
    for k, choices in _RULE_ENUMS.items():
        if raw.get(k) in choices:
            out[k] = raw[k]
    return out


def preset_rules(mode: str, seconds=None) -> dict:
    """The full ruleset a named preset resolves to (interval from `seconds`)."""
    r = dict(DEFAULT_RULES)
    try:
        raw_s = float(seconds) if seconds else None
        s = max(5.0, min(86400.0, raw_s)) if raw_s is not None and math.isfinite(raw_s) else None
    except (TypeError, ValueError):
        s = None
    if mode == MODE_HOT_POTATO:
        r.update(PRESET_OVERRIDES[MODE_HOT_POTATO])
        r["hold_limit_s"] = s or 120.0
    elif mode == MODE_CHAOS:
        r.update(PRESET_OVERRIDES[MODE_CHAOS])
        r["auto_shuffle_s"] = s or 120.0
    return r


def summarize_rules(r: dict) -> str:
    """One-line plain-English summary of a ruleset (for the mode banner)."""
    p = []
    if r.get("claiming"):
        if r.get("require_found_to_claim"):
            p.append("claim found items")
        else:
            p.append("steal anything someone owns" if r.get("open_season_scope") == "owned"
                     else "claim anything")
        cd, scope = r.get("steal_cooldown_s", 0), r.get("cooldown_scope")
        if cd and scope != "none":
            p.append(f"{int(cd)}s {scope} cooldown")
        if r.get("steal_back_lock_s"):
            p.append(f"{int(r['steal_back_lock_s'])}s steal-back lock")
        if r.get("steal_budget_per_min"):
            p.append(f"max {int(r['steal_budget_per_min'])} steals/min")
    else:
        p.append("no manual claiming")
    if r.get("hold_limit_s"):
        ex = {"next_finder": "→ next finder", "release": "→ released",
              "return_finder": "→ first finder"}.get(r.get("hold_expiry"), "")
        p.append(f"hold {int(r['hold_limit_s'])}s {ex}".strip())
    if r.get("tenure_lock_s"):
        p.append(f"unstealable after {int(r['tenure_lock_s'])}s")
    if r.get("idle_release_s"):
        p.append(f"drop items when offline {int(r['idle_release_s'])}s")
    if not r.get("require_found_to_claim") and r.get("borrow_s"):
        rev = "to owner" if r.get("borrow_revert") == "prev_owner" else "to pool"
        p.append(f"borrows revert {rev} after {int(r['borrow_s'])}s")
    if r.get("auto_shuffle_s"):
        p.append(f"reshuffle {r.get('shuffle_scope')} every {int(r['auto_shuffle_s'])}s")
    if r.get("shared_discovery"):
        p.append("shared discovery")
    return " · ".join(p)


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
    # "raid" borrow: held via an unfound steal, reverts when the lease ends.
    borrowed: bool = False
    borrow_until: float = 0.0
    borrow_prev: Optional[int] = None     # who to hand it back to on revert


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
    rules: Dict = field(default_factory=lambda: dict(DEFAULT_RULES))  # effective ruleset
    items: Dict[str, ItemState] = field(default_factory=dict)
    names: Dict[int, str] = field(default_factory=dict)    # user_id -> display name
    avatars: Dict[int, str] = field(default_factory=dict)  # user_id -> Discord avatar url
    # connectivity per player: user_id -> {"agent": bool, "emu": bool}
    status: Dict[int, dict] = field(default_factory=dict)
    # transient game state (not persisted — all seconds-scale):
    thief_cd: Dict[int, float] = field(default_factory=dict)      # uid -> can-steal-again time
    victim_shield: Dict[int, float] = field(default_factory=dict) # uid -> shielded-until time
    steal_log: Dict[int, list] = field(default_factory=dict)      # uid -> recent steal timestamps
    last_lost: Dict = field(default_factory=dict)                 # (uid, key) -> when they lost it
    offline_since: Dict[int, float] = field(default_factory=dict) # uid -> when their agent dropped


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
    try:
        level = int(level)
    except (TypeError, ValueError):
        return Effects(reject=f"invalid level for {item.name}")
    if level < item.present:
        return Effects(reject=f"invalid level for {item.name}")
    level = min(level, item.cap)
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
    it.cooldown_until = now + room.rules.get("steal_cooldown_s", room.cooldown_s)
    it.held_since = now           # (re)start hold timers for this item
    it.borrowed = False           # a real find makes it legitimately yours

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


def _has_found(room: RoomState, it: ItemState, user_id: int) -> bool:
    """Has this player 'found' the item for claim purposes (honours shared discovery)."""
    if user_id in it.discovered:
        return True
    return bool(room.rules.get("shared_discovery") and it.discovered)


def resolve_claim(room: RoomState, user_id: int, key: str) -> Effects:
    item = BY_KEY.get(key)
    if item is None:
        return Effects(reject=f"unknown item {key}")
    R = room.rules
    if not R.get("claiming"):
        return Effects(reject="This mode moves items automatically — no claiming.")
    it = room.items.setdefault(key, ItemState())
    name = room.names.get(user_id, f"player {user_id}")
    now = time.time()
    found = _has_found(room, it, user_id)

    # ── gates (first failure wins) ────────────────────────────────────────
    if R.get("require_found_to_claim"):
        if not found:
            return Effects(reject=f"You haven't found {item.name} yet — go find one first.")
    elif R.get("open_season_scope") == "owned" and it.owner is None and not found:
        return Effects(reject=f"Nobody holds {item.name} yet — someone has to find one first.")
    if it.owner == user_id:
        return Effects(reject=f"You already hold {item.name}.")
    tl = R.get("tenure_lock_s", 0)
    if it.owner is not None and tl and (now - it.held_since) >= tl:
        return Effects(reject=f"{room.names.get(it.owner, 'Someone')} has secured "
                              f"{item.name} — it can't be stolen.")
    sbl = R.get("steal_back_lock_s", 0)
    if sbl and (now - room.last_lost.get((user_id, key), -1e9)) < sbl:
        wait = sbl - (now - room.last_lost[(user_id, key)])
        return Effects(reject=f"You just lost {item.name} — wait {wait:.0f}s to grab it back.")
    bud = R.get("steal_budget_per_min", 0)
    if bud:
        recent = [t for t in room.steal_log.get(user_id, []) if now - t < 60]
        room.steal_log[user_id] = recent
        if len(recent) >= bud:
            return Effects(reject=f"Steal limit reached ({bud}/min) — cool it for a sec.")
    scope, cd = R.get("cooldown_scope", "item"), R.get("steal_cooldown_s", 0)
    if scope == "item" and now < it.cooldown_until:
        return Effects(reject=f"{item.name} is on cooldown ({it.cooldown_until - now:.0f}s).")
    if scope == "thief" and now < room.thief_cd.get(user_id, 0):
        return Effects(reject=f"You're on cooldown ({room.thief_cd[user_id] - now:.0f}s).")
    if scope == "victim" and it.owner is not None and now < room.victim_shield.get(it.owner, 0):
        return Effects(reject=f"{room.names.get(it.owner, 'They')} are shielded right now.")

    # ── grant ─────────────────────────────────────────────────────────────
    prev = it.owner
    it.owner = user_id
    it.level = it.discovered.get(user_id, item.present)   # your own tier
    it.held_since = now
    if not found and R.get("borrow_s"):                   # temporary "raid" borrow
        it.borrowed, it.borrow_until, it.borrow_prev = True, now + R["borrow_s"], prev
    else:
        it.borrowed, it.borrow_until, it.borrow_prev = False, 0.0, None
        if not found:                                     # permanent unfound steal → keep it sanely
            it.discovered.setdefault(user_id, item.present)
    # arm the cooldown for the chosen scope + the budget / steal-back trackers
    if scope == "item":
        it.cooldown_until = now + cd
    elif scope == "thief":
        room.thief_cd[user_id] = now + cd
    elif scope == "victim" and prev is not None:
        room.victim_shield[prev] = now + cd
    if bud:
        room.steal_log.setdefault(user_id, []).append(now)
    if prev is not None:
        room.last_lost[(prev, key)] = now

    eff = Effects(changed=True)
    eff.grants.append((user_id, key, it.level))
    if prev is not None:
        eff.revokes.append((prev, key))
        verb = "borrowed" if it.borrowed else ("stole" if not found else "reclaimed")
        eff.event = f"{name} {verb} {item.name} from {room.names.get(prev, 'someone')}"
    else:
        eff.event = f"{name} claimed {item.name}"
    return eff


def ownership_commands(room: RoomState, user_id: int) -> List[dict]:
    """Build an exhaustive reconciliation set for one player's game."""
    commands = []
    for item in ITEMS:
        it = room.items.get(item.key)
        if it and it.owner == user_id and it.level > 0:
            commands.append({"type": P.GRANT, "item": item.key, "level": it.level})
        else:
            commands.append({"type": P.REVOKE, "item": item.key})
    return commands


# ── live hub: connections + persistence + dispatch ─────────────────────────
class RoomHub:
    def __init__(self):
        self.rooms: Dict[str, RoomState] = {}
        self.agents: Dict[str, Dict[int, object]] = {}   # code -> {user_id: ws}
        self.uis: Dict[str, Dict[object, int]] = {}      # code -> {ws: user_id|None}
        self.admin_uis: Dict[str, set] = {}              # code -> {ws} granted global-admin
        self.apply_failures: Dict[str, set] = {}         # code -> {(user_id, item)}

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
        # effective ruleset: stored custom JSON, else derived from the preset mode
        raw_rules = row["rules"] if "rules" in keys and row["rules"] else None
        if raw_rules:
            try:
                room.rules = clamp_rules(json.loads(raw_rules))
            except Exception:
                room.rules = preset_rules(room.mode, room.shuffle_s)
        else:
            room.rules = preset_rules(room.mode, room.shuffle_s)
        if room.mode != MODE_CUSTOM:        # legacy cooldown column drives presets
            room.rules["steal_cooldown_s"] = room.cooldown_s
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
            room.offline_since.pop(user_id, None)        # back online → reset idle timer

    def unregister_agent(self, code, user_id, ws):
        if self.agents.get(code, {}).get(user_id) is ws:
            del self.agents[code][user_id]
        room = self.rooms.get(code)
        if room is not None:
            room.status[user_id] = {"agent": False, "emu": False}
            room.offline_since[user_id] = time.time()    # start the idle-release clock

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

    async def _notify_transfers(self, code: str, grants, revokes, summary=None):
        """Send concise, player-specific emulator notifications after item moves."""
        room = self.rooms.get(code)
        if room is None:
            return
        if summary:
            for ws in self.agents.get(code, {}).values():
                await self._send(ws, {"type": P.NOTIFY, "text": summary})
            return
        keys = {key for _, key, *_ in grants} | {key for _, key in revokes}
        if len(keys) > 1:
            for ws in self.agents.get(code, {}).values():
                await self._send(ws, {"type": P.NOTIFY, "text": "Items updated"})
            return
        for key in keys:
            item = BY_KEY.get(key)
            if item is None:
                continue
            new_owner = next((uid for uid, ikey, _ in grants if ikey == key), None)
            old_owners = [uid for uid, ikey in revokes if ikey == key and uid != new_owner]
            new_name = room.names.get(new_owner, "another player") if new_owner is not None else None
            old_name = room.names.get(old_owners[0], "another player") if old_owners else None
            if new_owner is not None and old_name:
                ws = self.agents.get(code, {}).get(new_owner)
                if ws:
                    await self._send(ws, {"type": P.NOTIFY,
                                          "text": f"{item.name} stolen from {old_name}"})
            for old_owner in old_owners:
                ws = self.agents.get(code, {}).get(old_owner)
                if ws:
                    text = (f"{item.name} stolen by {new_name}"
                            if new_name else f"{item.name} released")
                    await self._send(ws, {"type": P.NOTIFY, "text": text})

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
        await self._notify_transfers(code, eff.grants, eff.revokes)
        if eff.changed:
            self._persist(room, key)
        if eff.event:
            await self.broadcast_event(code, eff.event)
        await self.broadcast_state(code)

    # -- state serialization for UIs ----------------------------------------
    def serialize(self, code: str) -> dict:
        room = self.rooms[code]
        R = room.rules
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
                # per-item cooldown only gates when the scope IS the item
                "cooldown_remaining": (max(0.0, it.cooldown_until - now)
                                       if R.get("cooldown_scope") == "item" else 0.0),
            }
            if owned and R.get("hold_limit_s"):
                entry["hold_remaining"] = max(0.0, it.held_since + R["hold_limit_s"] - now)
            if owned and R.get("tenure_lock_s"):
                entry["locked"] = (now - it.held_since) >= R["tenure_lock_s"]
                entry["tenure_remaining"] = max(0.0, it.held_since + R["tenure_lock_s"] - now)
            if it.borrowed:
                entry["borrow_remaining"] = max(0.0, it.borrow_until - now)
            ledger[key] = entry
        return {
            "type": P.STATE,
            "room": room.pub_id,      # public handle only — never leak the join code
            "name": room.name,
            "cooldown_s": R.get("steal_cooldown_s", room.cooldown_s),
            "host": room.host,
            "mode": room.mode,
            "rules": R,
            "rule_defaults": DEFAULT_RULES,
            "rule_presets": PRESET_OVERRIDES,
            "claiming": bool(R.get("claiming")),
            "rules_summary": summarize_rules(R),
            "shuffle_s": room.shuffle_s,
            "shuffle_remaining": (max(0.0, room.last_shuffle + R["auto_shuffle_s"] - now)
                                  if R.get("auto_shuffle_s") else 0.0),
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

    async def report_applied(self, code: str, user_id: int, msg: dict):
        """Record agent write failures and surface each failure once to the room."""
        key = msg.get("item")
        marker = (user_id, key)
        failures = self.apply_failures.setdefault(code, set())
        if msg.get("ok") is True:
            failures.discard(marker)
            return
        if marker in failures:
            return
        failures.add(marker)
        room = self.rooms.get(code)
        if room and key in BY_KEY:
            name = room.names.get(user_id, f"player {user_id}")
            logger.warning("agent write failed room=%s player=%s item=%s: %s",
                           code, user_id, key, msg.get("error", "unknown error"))
            await self.broadcast_event(
                code, f"⚠ {name}'s game could not apply {BY_KEY[key].name}; reconnecting will retry")

    # -- host admin actions (caller must verify user == room.host) -----------
    async def admin_set_cooldown(self, code: str, seconds: float):
        room = self.rooms[code]
        try:
            value = float(seconds)
        except (TypeError, ValueError):
            return
        if not math.isfinite(value):
            return
        room.cooldown_s = max(0.0, min(3600.0, value))
        room.rules["steal_cooldown_s"] = room.cooldown_s
        db.update_cooldown(code, room.cooldown_s)
        db.update_rules(code, json.dumps(room.rules))
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

    async def admin_set_owner(self, code: str, player_id, key: str, level=None):
        """Force an item's owner (player_id) or clear it (player_id None).

        `level` (optional) sets an explicit tier for a multi-tier item — the host
        uses it to restore e.g. a Gold Sword after a reset wiped the ledger's
        memory of what the player had found. Without it, the player gets back
        whatever tier they'd previously discovered."""
        room = self.rooms[code]
        item = BY_KEY.get(key)
        if not item or (player_id is not None and player_id not in room.names):
            return
        it = room.items.setdefault(key, ItemState())
        prev = it.owner
        if player_id is not None:
            if level is not None:
                it.discovered[player_id] = max(item.present, min(int(level), item.cap))
            else:
                it.discovered.setdefault(player_id, item.present)  # owning implies discovered
            it.owner = player_id
            it.level = it.discovered[player_id]   # grant the chosen / their own tier
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
        name = str(name or "").strip()[:60]
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
        room.mode = mode if mode in (MODE_NORMAL, MODE_HOT_POTATO, MODE_CHAOS) else MODE_NORMAL
        if seconds:
            try:
                value = float(seconds)
                if math.isfinite(value):
                    room.shuffle_s = max(5.0, min(86400.0, value))
            except (TypeError, ValueError):
                pass
        # a preset is just a ruleset; rebuild it (keeping the host's steal cooldown)
        room.rules = preset_rules(room.mode, room.shuffle_s)
        room.rules["steal_cooldown_s"] = room.cooldown_s
        now = time.time()
        room.last_shuffle = now
        for it in room.items.values():        # everyone keeps their item a full round first
            it.held_since = now
        db.update_mode(code, room.mode, room.shuffle_s)
        db.update_rules(code, json.dumps(room.rules))
        if room.mode == MODE_NORMAL:
            await self.broadcast_event(code, "Host set mode to Normal — claiming is back on.")
        else:
            await self.broadcast_event(
                code, f"Host started {MODE_LABELS[room.mode]} — items shuffle every "
                      f"{int(room.shuffle_s)}s. Claiming is off.")
        await self.broadcast_state(code)

    async def admin_set_rules(self, code: str, raw: dict):
        """Host-composed Custom ruleset. Validates + clamps, then the engine runs
        entirely off room.rules (presets are just named points in the same space)."""
        room = self.rooms.get(code)
        if room is None:
            return
        room.rules = clamp_rules(raw)
        room.mode = MODE_CUSTOM
        room.cooldown_s = room.rules["steal_cooldown_s"]   # mirror for legacy displays
        now = time.time()
        room.last_shuffle = now
        for it in room.items.values():
            it.held_since = now
        db.update_mode(code, room.mode, room.shuffle_s)
        db.update_cooldown(code, room.cooldown_s)
        db.update_rules(code, json.dumps(room.rules))
        await self.broadcast_event(code, "Host set a Custom ruleset — " + summarize_rules(room.rules))
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

    def _release(self, room: RoomState, key: str, now: float, grants, revokes):
        """Drop an item to unowned (claimable by anyone who can)."""
        it = room.items[key]
        prev = it.owner
        if prev is None:
            return
        it.owner = None
        it.held_since = now
        revokes.append((prev, key))
        self._persist(room, key)

    async def tick_shuffles(self):
        """Driven once a second by the server: applies every time-based rule
        (hold limits, tenure, idle release, borrow reverts, auto-shuffle)."""
        now = time.time()
        for code in list(self.rooms.keys()):
            room = self.rooms.get(code)
            if room is None:
                continue
            try:
                await self._tick_room(code, room, now)
            except Exception:
                logger.exception("time-based rules failed for room %s", code)

    async def _tick_room(self, code, room, now):
        R = room.rules
        if not (R.get("hold_limit_s") or R.get("idle_release_s") or R.get("auto_shuffle_s")
                or any(it.borrowed for it in room.items.values())):
            return                                    # no time-based rules → nothing to do
        grants, revokes, events = [], [], []
        notification = None

        # 1. borrow leases expire → revert to previous owner, else the pool
        for key, it in room.items.items():
            if it.borrowed and now >= it.borrow_until:
                it.borrowed, it.borrow_until = False, 0.0
                target = it.borrow_prev if R.get("borrow_revert") == "prev_owner" else None
                it.borrow_prev = None
                if target is not None and target in room.names:
                    self._reassign(room, key, target, now, grants, revokes)
                    events.append(f"↩ {BY_KEY[key].name} returned to {room.names.get(target)}")
                else:
                    self._release(room, key, now, grants, revokes)
                    events.append(f"↩ {BY_KEY[key].name} borrow ended")

        # 2. idle release → an offline owner drops their items back to the pool
        idle = R.get("idle_release_s", 0)
        if idle:
            for key, it in room.items.items():
                if it.owner is None:
                    continue
                off = room.offline_since.get(it.owner)
                if off and (now - off) >= idle:
                    nm = room.names.get(it.owner, "Someone")
                    self._release(room, key, now, grants, revokes)
                    events.append(f"💤 {nm} dropped {BY_KEY[key].name} (offline)")

        # 3. hold limit → pass to next finder / release / return to first finder
        hl = R.get("hold_limit_s", 0)
        if hl:
            tl = R.get("tenure_lock_s", 0)
            expiry = R.get("hold_expiry", "next_finder")
            for key, it in room.items.items():
                if not it.discovered:
                    continue
                if tl and it.owner is not None and (now - it.held_since) >= tl:
                    continue                          # secured items don't auto-move
                if expiry == "next_finder":
                    avail = self._available_finders(room, it)
                    if not avail:
                        continue
                    if it.owner is None:              # pull a found-but-unheld item in
                        new = avail[0]
                    else:
                        if now - it.held_since < hl:
                            continue                  # still their turn
                        if it.owner in avail:
                            if len(avail) == 1:       # only the holder is online → keep
                                it.held_since = now
                                continue
                            new = avail[(avail.index(it.owner) + 1) % len(avail)]
                        else:
                            new = avail[0]            # holder offline → hand it on
                    if new == it.owner:
                        it.held_since = now
                        continue
                    self._reassign(room, key, new, now, grants, revokes)
                    events.append(f"🔥 {BY_KEY[key].name} → {room.names.get(new, 'someone')}")
                else:
                    if it.owner is None or (now - it.held_since) < hl:
                        continue
                    if expiry == "release":
                        self._release(room, key, now, grants, revokes)
                        events.append(f"⌛ {BY_KEY[key].name} released — anyone can claim it")
                    else:                             # return_finder
                        finders = sorted(it.discovered)
                        target = finders[0] if finders else None
                        if target is not None and target != it.owner:
                            self._reassign(room, key, target, now, grants, revokes)
                            events.append(f"↩ {BY_KEY[key].name} → {room.names.get(target, 'someone')}")
                        else:
                            it.held_since = now

        # 4. auto-shuffle → periodic random reassignment (stacks on claiming)
        asf = R.get("auto_shuffle_s", 0)
        if asf and (now - room.last_shuffle) >= asf:
            room.last_shuffle = now
            scope = R.get("shuffle_scope", "all")
            moved = 0
            for key, it in room.items.items():
                if not it.discovered:
                    continue
                if scope == "unowned" and it.owner is not None:
                    continue
                if scope == "idle" and it.owner is not None and (now - it.held_since) < asf:
                    continue
                avail = self._available_finders(room, it)
                if not avail:
                    continue
                new = random.choice(avail)
                if new == it.owner:
                    continue
                self._reassign(room, key, new, now, grants, revokes)
                moved += 1
            if moved:
                events.append("🌀 Chaos shuffle! Items moved.")
                notification = "Items shuffled"

        if grants or revokes:
            await self._send_commands(code, grants, revokes, notification)
            for ev in events:
                await self.broadcast_event(code, ev)
            await self.broadcast_state(code)

    async def _send_commands(self, code, grants, revokes, notification=None):
        db.touch_room(code)
        for (uid, key, level) in grants:
            ws = self.agents.get(code, {}).get(uid)
            if ws:
                await self._send(ws, {"type": P.GRANT, "item": key, "level": level})
        for (uid, key) in revokes:
            ws = self.agents.get(code, {}).get(uid)
            if ws:
                await self._send(ws, {"type": P.REVOKE, "item": key})
        await self._notify_transfers(code, grants, revokes, notification)

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
        self.apply_failures.pop(code, None)


hub = RoomHub()
