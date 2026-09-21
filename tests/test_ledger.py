import asyncio
import math
import time
import unittest
from unittest import mock

from server import db
from server.ledger import (
    RoomHub, RoomState, clamp_rules, ownership_commands, preset_rules, resolve_claim,
    resolve_pickup,
)
from shared import protocol as P
from shared.items import ITEMS


class FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send_json(self, payload):
        self.messages.append(payload)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self._old_path, self._old_conn = db.DB_PATH, db._conn
        db.DB_PATH, db._conn = ":memory:", None
        db.init()

    def tearDown(self):
        db._conn.close()
        db.DB_PATH, db._conn = self._old_path, self._old_conn

    def room(self):
        room = RoomState("TEST")
        room.names = {1: "A", 2: "B"}
        room.rules["steal_cooldown_s"] = 0
        return room

    def test_replaced_agent_closing_keeps_the_live_link(self):
        hub = RoomHub()
        room = self.room()
        hub.rooms[room.code] = room
        old, new = object(), object()
        hub.register_agent(room.code, 1, old)
        hub.set_emu_status(room.code, 1, True)
        hub.register_agent(room.code, 1, new)        # a second browser / the desktop app
        hub.unregister_agent(room.code, 1, old)      # the replaced socket goes away
        self.assertTrue(hub.is_current_agent(room.code, 1, new))
        self.assertEqual(room.status[1], {"agent": True, "emu": True})
        self.assertNotIn(1, room.offline_since)
        hub.unregister_agent(room.code, 1, new)      # the live one closing still counts
        self.assertEqual(room.status[1], {"agent": False, "emu": False})
        self.assertIn(1, room.offline_since)

    def test_shared_discovery_allows_another_player_to_claim(self):
        room = self.room()
        resolve_pickup(room, 1, "sword", 2)
        room.rules["shared_discovery"] = True
        effect = resolve_claim(room, 2, "sword")
        self.assertIsNone(effect.reject)
        self.assertEqual(room.items["sword"].owner, 2)

    def test_claim_any_allows_a_never_discovered_item(self):
        room = self.room()
        room.rules.update(require_found_to_claim=False, open_season_scope="any")
        effect = resolve_claim(room, 1, "hookshot")
        self.assertIsNone(effect.reject)
        self.assertEqual(room.items["hookshot"].owner, 1)

    def test_pickup_level_is_bounded_and_invalid_zero_is_rejected(self):
        room = self.room()
        self.assertIsNotNone(resolve_pickup(room, 1, "sword", 0).reject)
        effect = resolve_pickup(room, 1, "sword", 255)
        self.assertIsNone(effect.reject)
        self.assertEqual(room.items["sword"].level, 4)

    def test_non_finite_rules_fall_back_to_defaults(self):
        rules = clamp_rules({"steal_cooldown_s": math.nan})
        self.assertEqual(rules["steal_cooldown_s"], 5.0)
        self.assertEqual(preset_rules("chaos", math.nan)["auto_shuffle_s"], 120)

    def test_non_finite_host_cooldown_is_ignored(self):
        hub = RoomHub()
        room = self.room()
        hub.rooms[room.code] = room
        asyncio.run(hub.admin_set_cooldown(room.code, math.inf))
        self.assertEqual(room.cooldown_s, 5.0)

    def test_resync_is_exhaustive_and_revokes_unowned_items(self):
        room = self.room()
        resolve_pickup(room, 1, "sword", 2)
        room.items["hookshot"] = room.items["sword"].__class__()
        commands = ownership_commands(room, 1)
        self.assertEqual(len(commands), len(ITEMS))
        by_item = {command["item"]: command for command in commands}
        self.assertEqual(by_item["sword"]["type"], P.GRANT)
        self.assertEqual(by_item["hookshot"]["type"], P.REVOKE)
        self.assertEqual(by_item["boots"]["type"], P.REVOKE)

    def test_admin_cannot_assign_an_item_to_a_non_player(self):
        hub = RoomHub()
        room = self.room()
        hub.rooms[room.code] = room
        asyncio.run(hub.admin_set_owner(room.code, 999, "sword"))
        self.assertNotIn("sword", room.items)

    def test_admin_set_owner_with_level_restores_a_progressive_tier(self):
        hub = RoomHub()
        room = self.room()
        hub.rooms[room.code] = room
        agent = FakeWebSocket()
        hub.agents[room.code] = {1: agent}
        # Player 1 has discovered nothing; host hands them a Gold Sword (tier 4).
        asyncio.run(hub.admin_set_owner(room.code, 1, "sword", level=4))
        it = room.items["sword"]
        self.assertEqual(it.owner, 1)
        self.assertEqual(it.level, 4)
        self.assertEqual(it.discovered[1], 4)          # they now "own" that tier
        grants = [m for m in agent.messages
                  if m.get("type") == P.GRANT and m.get("item") == "sword"]
        self.assertEqual(grants[-1]["level"], 4)

    def test_admin_set_discovered_with_level_marks_tier_without_granting(self):
        hub = RoomHub()
        room = self.room()
        hub.rooms[room.code] = room
        agent = FakeWebSocket()
        hub.agents[room.code] = {1: agent}
        # Mark player 1 as having FOUND a Gold Sword (tier 4) — but don't give it.
        asyncio.run(hub.admin_set_discovered(room.code, 1, "sword", True, level=4))
        it = room.items["sword"]
        self.assertEqual(it.discovered[1], 4)   # found at gold
        self.assertIsNone(it.owner)             # nobody owns it
        self.assertEqual([m for m in agent.messages if m.get("type") == P.GRANT], [])
        # …and if they later claim it, they get gold (their found tier).
        eff = resolve_claim(room, 1, "sword")
        self.assertIsNone(eff.reject)
        self.assertEqual(room.items["sword"].level, 4)

    def test_admin_set_owner_clamps_level_to_item_cap(self):
        hub = RoomHub()
        room = self.room()
        hub.rooms[room.code] = room
        asyncio.run(hub.admin_set_owner(room.code, 1, "gloves", level=99))
        self.assertEqual(room.items["gloves"].level, 2)   # Titan's Mitt is the cap

    def test_transfer_notifications_are_targeted(self):
        hub = RoomHub()
        room = self.room()
        hub.rooms[room.code] = room
        old_ws, new_ws = FakeWebSocket(), FakeWebSocket()
        hub.agents[room.code] = {1: old_ws, 2: new_ws}
        asyncio.run(hub._notify_transfers(
            room.code, [(2, "sword", 1)], [(1, "sword")]))
        self.assertEqual(old_ws.messages[-1]["text"], "Sword stolen by B")
        self.assertEqual(new_ws.messages[-1]["text"], "Sword stolen from A")

    def test_shuffle_notification_is_broadcast_once(self):
        hub = RoomHub()
        room = self.room()
        hub.rooms[room.code] = room
        first, second = FakeWebSocket(), FakeWebSocket()
        hub.agents[room.code] = {1: first, 2: second}
        asyncio.run(hub._notify_transfers(room.code, [], [], "Items shuffled"))
        self.assertEqual(first.messages, [{"type": P.NOTIFY, "text": "Items shuffled"}])
        self.assertEqual(second.messages, [{"type": P.NOTIFY, "text": "Items shuffled"}])

    def test_admin_reset_room_wipes_progression_but_keeps_players(self):
        hub = RoomHub()
        room = self.room()
        hub.rooms[room.code] = room
        a1, a2 = FakeWebSocket(), FakeWebSocket()
        hub.agents[room.code] = {1: a1, 2: a2}
        # build some state: finds, a steal cooldown, and persisted rows
        resolve_pickup(room, 1, "sword", 2)
        resolve_pickup(room, 2, "lamp", 1)
        room.thief_cd[1] = time.time() + 99
        hub._persist(room, "sword")
        hub._persist(room, "lamp")
        self.assertTrue(db.load_ledger(room.code)[0])

        asyncio.run(hub.admin_reset_room(room.code))

        self.assertEqual(room.items, {})                    # in-memory ledger gone
        self.assertEqual(room.thief_cd, {})                 # transient timers gone
        ledger_rows, disc_rows = db.load_ledger(room.code)
        self.assertEqual((list(ledger_rows), list(disc_rows)), ([], []))
        self.assertEqual(room.names, {1: "A", 2: "B"})      # players survive
        for agent in (a1, a2):                              # exhaustive revoke + notify
            revoked = {m["item"] for m in agent.messages if m.get("type") == P.REVOKE}
            self.assertEqual(revoked, {item.key for item in ITEMS})
            self.assertIn("Room reset - fresh start",
                          [m.get("text") for m in agent.messages if m.get("type") == P.NOTIFY])


class NotificationTests(unittest.TestCase):
    """Every transfer path must produce correctly-worded per-player messages."""

    def setUp(self):
        self._old_path, self._old_conn = db.DB_PATH, db._conn
        db.DB_PATH, db._conn = ":memory:", None
        db.init()

    def tearDown(self):
        db._conn.close()
        db.DB_PATH, db._conn = self._old_path, self._old_conn

    def hub_room(self):
        hub = RoomHub()
        room = RoomState("TEST")
        room.names = {1: "A", 2: "B"}
        room.rules["steal_cooldown_s"] = 0
        hub.rooms[room.code] = room
        a1, a2 = FakeWebSocket(), FakeWebSocket()
        hub.agents[room.code] = {1: a1, 2: a2}
        return hub, room, a1, a2

    @staticmethod
    def notifies(ws):
        return [m["text"] for m in ws.messages if m.get("type") == P.NOTIFY]

    def test_pickup_grab_notifies_both_sides(self):
        _, room, _, _ = self.hub_room()
        resolve_pickup(room, 1, "lamp", 1)
        eff = resolve_pickup(room, 2, "lamp", 1)
        self.assertIn((2, "Lamp taken from A"), eff.notifies)
        self.assertIn((1, "Lamp sent to B"), eff.notifies)

    def test_reclaim_notifies_with_the_right_verb(self):
        hub, room, a1, a2 = self.hub_room()
        resolve_pickup(room, 1, "lamp", 1)
        resolve_pickup(room, 2, "lamp", 1)          # B holds it now
        eff = resolve_claim(room, 1, "lamp")        # A reclaims (found it before)
        asyncio.run(hub.dispatch(room.code, eff, "lamp"))
        self.assertIn("Lamp reclaimed from B", self.notifies(a1))
        self.assertIn("Lamp reclaimed by A", self.notifies(a2))

    def test_pool_claim_notifies_the_claimer(self):
        hub, room, a1, _ = self.hub_room()
        resolve_pickup(room, 1, "lamp", 1)
        room.items["lamp"].owner = None             # released back to the pool
        eff = resolve_claim(room, 1, "lamp")
        asyncio.run(hub.dispatch(room.code, eff, "lamp"))
        self.assertIn("Lamp claimed", self.notifies(a1))

    def test_borrow_notifies_both_sides_with_the_lease(self):
        _, room, _, _ = self.hub_room()
        room.rules.update(require_found_to_claim=False, open_season_scope="owned",
                          borrow_s=60.0)
        resolve_pickup(room, 1, "lamp", 1)
        eff = resolve_claim(room, 2, "lamp")        # B never found one — a raid borrow
        self.assertIn((2, "Lamp borrowed from A (60s)"), eff.notifies)
        self.assertIn((1, "Lamp borrowed by B (60s)"), eff.notifies)

    def test_hot_potato_pass_notifies_both_sides(self):
        hub, room, a1, a2 = self.hub_room()
        room.rules["hold_limit_s"] = 10.0
        room.status = {1: {"agent": True}, 2: {"agent": True}}
        resolve_pickup(room, 1, "lamp", 1)
        resolve_pickup(room, 2, "lamp", 1)
        resolve_claim(room, 1, "lamp")              # A holds it again
        room.items["lamp"].held_since = time.time() - 999
        asyncio.run(hub._tick_room(room.code, room, time.time()))
        self.assertIn("Lamp passed to B", self.notifies(a1))
        self.assertIn("Hot potato - Lamp is yours", self.notifies(a2))

    def test_chaos_shuffle_sends_per_player_summaries(self):
        hub, room, a1, a2 = self.hub_room()
        room.rules["auto_shuffle_s"] = 10.0
        room.last_shuffle = time.time() - 999
        room.status = {1: {"agent": True}, 2: {"agent": True}}
        resolve_pickup(room, 1, "lamp", 1)
        resolve_pickup(room, 2, "lamp", 1)          # both found it; B holds it
        with mock.patch("server.ledger.random.choice", lambda seq: seq[0]):  # → player 1
            asyncio.run(hub._tick_room(room.code, room, time.time()))
        self.assertIn("Shuffle: got Lamp", self.notifies(a1))
        self.assertIn("Shuffle: lost Lamp", self.notifies(a2))

    def test_host_give_and_take_notify_the_players(self):
        hub, room, a1, a2 = self.hub_room()
        asyncio.run(hub.admin_set_owner(room.code, 1, "sword", level=4))
        self.assertIn("Host gave you Sword (Gold)", self.notifies(a1))
        asyncio.run(hub.admin_set_owner(room.code, 2, "sword"))
        self.assertIn("Host moved Sword to B", self.notifies(a1))
        asyncio.run(hub.admin_set_owner(room.code, None, "sword"))
        self.assertIn("Host took Sword", self.notifies(a2))


if __name__ == "__main__":
    unittest.main()
