import json
import unittest

from agent.agent import GAME_MODE_ADDR, HyruleAgent, _TRACKED_SIZE, _TRACKED_START
from agent.sni.emu_connector import EmuConnector, READ_FAILURE_LIMIT, _RetroArchClient


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
