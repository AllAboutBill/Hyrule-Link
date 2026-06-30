import asyncio
import math
import unittest

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


if __name__ == "__main__":
    unittest.main()
