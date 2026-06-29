import asyncio
import math
import unittest

from server.ledger import (
    RoomHub, RoomState, clamp_rules, ownership_commands, preset_rules, resolve_claim,
    resolve_pickup,
)
from shared import protocol as P
from shared.items import ITEMS


class LedgerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
