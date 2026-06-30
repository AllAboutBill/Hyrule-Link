import json
import unittest

from agent.agent import GAME_MODE_ADDR, HyruleAgent, _TRACKED_SIZE, _TRACKED_START
from agent.effects import Effects
from agent.sni.emu_connector import EmuConnector, READ_FAILURE_LIMIT, _RetroArchClient
from agent.sni.item_effects import (
    ARROWS_ADDR, DEFAULT_ARROWS,
    BOOM_EQUIP_ADDR, POWDER_EQUIP_ADDR, FLUTE_EQUIP_ADDR,
    BOOM_BLUE_BIT, BOOM_RED_BIT, MUSHROOM_OWN, POWDER_BIT,
    SHOVEL_BIT, FLUTE_ACTIVE_BIT, FLUTE_ANY_BITS,
)
from shared.items import (
    BY_KEY, discovered_level, INV_TRACK_ADDR,
    BOW_FLAGS_ADDR, BOW_EQUIP_ADDR, BOW_HAS_MASK, BOW_SILVER_MASK,
)


class MemoryTransport:
    def __init__(self, writable=True):
        self.connected = True
        self.writable = writable
        self.memory = {}
        self.notifications = []

    def connect(self):
        self.connected = True
        return True

    def disconnect(self):
        self.connected = False

    def read_memory(self, address, size=1, domain="WRAM"):
        return bytes(self.memory.get(address + offset, 0) for offset in range(size))

    def write_memory(self, address, data, domain="WRAM"):
        if not self.writable:
            return False
        self.memory[address] = data[0]
        return True

    def show_message(self, text):
        self.notifications.append(text)
        return True


class FlakyReadTransport(MemoryTransport):
    """Drops the first `drops` reads (returns None), then reads normally."""

    def __init__(self, drops=0):
        super().__init__()
        self._drops = drops

    def read_memory(self, address, size=1, domain="WRAM"):
        if self._drops > 0:
            self._drops -= 1
            return None
        return super().read_memory(address, size, domain)


class Socket:
    def __init__(self):
        self.messages = []

    def send(self, payload):
        self.messages.append(json.loads(payload))


class DatagramSocket:
    def __init__(self):
        self.sent = []

    def sendto(self, payload, address):
        self.sent.append((payload, address))


class ReplySocket(DatagramSocket):
    def __init__(self, replies):
        super().__init__()
        self.replies = list(replies)

    def recvfrom(self, _size):
        return self.replies.pop(0), ("127.0.0.1", 55355)


class SequencedClient:
    def __init__(self, reads):
        self.reads = list(reads)

    def read_memory(self, address, size):
        return self.reads.pop(0) if self.reads else None

    def close(self):
        pass


class AgentApplyTests(unittest.TestCase):
    def agent(self, transport):
        agent = HyruleAgent(transport, "ws://test", "ROOM", 1, "token")
        agent.ws = Socket()
        agent._ws_ready.set()
        return agent

    def test_successful_write_is_acknowledged(self):
        agent = self.agent(MemoryTransport())
        agent._apply("sword", 2, True)
        self.assertTrue(agent.ws.messages[-1]["ok"])
        self.assertEqual(agent.ws.messages[-1]["action"], "grant")

    def test_failed_write_is_acknowledged(self):
        agent = self.agent(MemoryTransport(writable=False))
        agent._apply("sword", 2, True)
        self.assertFalse(agent.ws.messages[-1]["ok"])
        self.assertIn("verification failed", agent.ws.messages[-1]["error"])

    def test_grant_wood_bow_sets_tracking_not_silver(self):
        transport = MemoryTransport()
        agent = self.agent(transport)
        agent._apply("bow", 1, True)
        self.assertTrue(agent.ws.messages[-1]["ok"])
        flags = transport.memory[BOW_FLAGS_ADDR]
        self.assertTrue(flags & BOW_HAS_MASK)          # has a bow
        self.assertFalse(flags & BOW_SILVER_MASK)      # but NOT silver
        self.assertEqual(transport.memory[BOW_EQUIP_ADDR], 0x01)
        self.assertEqual(discovered_level(BY_KEY["bow"], flags), 1)

    def test_grant_silver_bow_sets_silver_bit_and_arrows(self):
        transport = MemoryTransport()
        agent = self.agent(transport)
        agent._apply("bow", 2, True)
        self.assertTrue(agent.ws.messages[-1]["ok"])
        flags = transport.memory[BOW_FLAGS_ADDR]
        # z3randomizer gates silver firing on BowTracking & 0xC0 == 0xC0
        self.assertEqual(flags & (BOW_HAS_MASK | BOW_SILVER_MASK),
                         BOW_HAS_MASK | BOW_SILVER_MASK)
        self.assertEqual(transport.memory[ARROWS_ADDR], DEFAULT_ARROWS)
        self.assertEqual(discovered_level(BY_KEY["bow"], flags), 2)

    def test_revoke_bow_clears_tracking(self):
        transport = MemoryTransport()
        agent = self.agent(transport)
        agent._apply("bow", 2, True)
        agent._apply("bow", 0, False)
        self.assertTrue(agent.ws.messages[-1]["ok"])
        flags = transport.memory[BOW_FLAGS_ADDR]
        self.assertFalse(flags & (BOW_HAS_MASK | BOW_SILVER_MASK))
        self.assertEqual(discovered_level(BY_KEY["bow"], flags), 0)

    def test_silver_bow_pickup_detected_from_tracking_byte(self):
        transport = MemoryTransport()
        transport.memory[GAME_MODE_ADDR] = 0x07
        agent = self.agent(transport)
        agent._poll_once()                                   # seed baseline (no bow)
        transport.memory[BOW_FLAGS_ADDR] = BOW_HAS_MASK | BOW_SILVER_MASK
        agent._poll_once()
        bow_pickups = [m for m in agent.ws.messages
                       if m.get("item") == "bow" and "level" in m]
        self.assertEqual(bow_pickups[-1]["level"], 2)

    def test_shared_slot_grants_do_not_clobber_each_other(self):
        t = MemoryTransport()
        fx = Effects(t)
        # shovel then flute: both owned in $7EF38C, shovel survives, flute active
        fx.enable("shovel", 1)
        fx.enable("flute", 1)
        track = t.memory[INV_TRACK_ADDR]
        self.assertTrue(track & SHOVEL_BIT)
        self.assertTrue(track & FLUTE_ACTIVE_BIT)
        self.assertEqual(t.memory[FLUTE_EQUIP_ADDR], 0x03)
        self.assertEqual(discovered_level(BY_KEY["shovel"], track), 1)
        self.assertEqual(discovered_level(BY_KEY["flute"], track), 1)
        # powder then mushroom: both owned, powder survives
        fx.enable("powder", 1)
        fx.enable("mushroom", 1)
        track = t.memory[INV_TRACK_ADDR]
        self.assertTrue(track & POWDER_BIT)
        self.assertTrue(track & MUSHROOM_OWN)
        self.assertEqual(discovered_level(BY_KEY["powder"], track), 1)
        self.assertEqual(discovered_level(BY_KEY["mushroom"], track), 1)
        # both boomerangs owned (blue=0x80, red=0x40)
        fx.enable("blue_boomerang", 1)
        fx.enable("red_boomerang", 1)
        track = t.memory[INV_TRACK_ADDR]
        self.assertTrue(track & BOOM_BLUE_BIT)
        self.assertTrue(track & BOOM_RED_BIT)
        self.assertEqual(t.memory[BOOM_EQUIP_ADDR], 0x02)  # red equipped last

    def test_revoke_flute_keeps_shovel_and_falls_back_equip(self):
        t = MemoryTransport()
        fx = Effects(t)
        fx.enable("shovel", 1)
        fx.enable("flute", 1)                 # flute equipped (enum 3)
        fx.disable("flute")
        track = t.memory[INV_TRACK_ADDR]
        self.assertFalse(track & FLUTE_ANY_BITS)   # flute gone
        self.assertTrue(track & SHOVEL_BIT)        # shovel kept
        self.assertEqual(t.memory[FLUTE_EQUIP_ADDR], 0x01)  # equip fell back to shovel
        self.assertEqual(discovered_level(BY_KEY["flute"], track), 0)
        self.assertEqual(discovered_level(BY_KEY["shovel"], track), 1)

    def test_revoke_powder_keeps_mushroom_and_falls_back_equip(self):
        t = MemoryTransport()
        fx = Effects(t)
        fx.enable("mushroom", 1)
        fx.enable("powder", 1)                # powder equipped (enum 2)
        fx.disable("powder")
        track = t.memory[INV_TRACK_ADDR]
        self.assertFalse(track & POWDER_BIT)
        self.assertTrue(track & MUSHROOM_OWN)
        self.assertEqual(t.memory[POWDER_EQUIP_ADDR], 0x01)  # fell back to mushroom
        self.assertEqual(discovered_level(BY_KEY["powder"], track), 0)
        self.assertEqual(discovered_level(BY_KEY["mushroom"], track), 1)

    def test_dropped_read_aborts_grant_without_clobbering(self):
        # Boots is a read-modify-write on the ability byte; a persistently
        # failing read must NOT write a 0-derived value over the other bits.
        t = FlakyReadTransport(drops=1000)
        t.memory[0xF379] = 0x68          # read+talk+pull abilities
        fx = Effects(t)
        with self.assertRaises(IOError):
            fx.enable("boots", 1)
        # The ability byte must be untouched (no 0x04 clobber).
        self.assertEqual(t.memory[0xF379], 0x68)

    def test_transient_dropped_reads_are_retried_not_clobbered(self):
        t = FlakyReadTransport(drops=3)
        t.memory[0xF379] = 0x68
        fx = Effects(t)
        fx.enable("boots", 1)            # retries absorb the drops
        self.assertEqual(t.memory[0xF355], 0x01)        # boots granted
        self.assertEqual(t.memory[0xF379], 0x68 | 0x04)  # dash added, rest kept

    def test_server_notification_reaches_transport(self):
        transport = MemoryTransport()
        agent = self.agent(transport)
        agent._on_message(None, json.dumps({"type": "notify", "text": " Sword  stolen\nfrom Zelda "}))
        self.assertEqual(transport.notifications, ["Sword stolen from Zelda"])

    def test_retroarch_notification_uses_show_msg(self):
        client = _RetroArchClient()
        client._sock = DatagramSocket()
        self.assertTrue(client.show_message("Sword stolen from Zelda"))
        self.assertEqual(client._sock.sent,
                         [(b"SHOW_MSG Sword stolen from Zelda\n", ("127.0.0.1", 55355))])

    def test_inventory_poll_uses_one_contiguous_read(self):
        transport = MemoryTransport()
        transport.memory[GAME_MODE_ADDR] = 0x07
        agent = self.agent(transport)
        calls = []
        original = transport.read_memory

        def recording_read(address, size=1, domain="WRAM"):
            calls.append((address, size))
            return original(address, size, domain)

        transport.read_memory = recording_read
        agent._poll_once()
        self.assertIn((_TRACKED_START, _TRACKED_SIZE), calls)
        inventory_calls = [call for call in calls
                           if _TRACKED_START <= call[0] < _TRACKED_START + _TRACKED_SIZE]
        self.assertEqual(inventory_calls, [(_TRACKED_START, _TRACKED_SIZE)])

    def test_retroarch_discards_late_reply_for_another_address(self):
        client = _RetroArchClient()
        client._sock = ReplySocket([
            b"READ_CORE_RAM F341 AA BB", b"READ_CORE_RAM F340 01 02",
        ])
        self.assertEqual(client.read_memory(0xF340, 2), b"\x01\x02")
        self.assertEqual(len(client._sock.sent), 2)

    def test_transient_read_miss_does_not_disconnect(self):
        connector = EmuConnector(source="retroarch")
        client = SequencedClient([None, b"\x07"])
        connector._sources = [("RetroArch", client)]
        connector._active = "RetroArch"
        connector.connected = True
        self.assertIsNone(connector.read_memory(0x10, 1))
        self.assertTrue(connector.connected)
        self.assertEqual(connector.read_memory(0x10, 1), b"\x07")
        self.assertTrue(connector.connected)

    def test_repeated_read_misses_disconnect(self):
        connector = EmuConnector(source="retroarch")
        client = SequencedClient([None] * READ_FAILURE_LIMIT)
        connector._sources = [("RetroArch", client)]
        connector._active = "RetroArch"
        connector.connected = True
        for _ in range(READ_FAILURE_LIMIT):
            connector.read_memory(0x10, 1)
        self.assertFalse(connector.connected)


if __name__ == "__main__":
    unittest.main()
