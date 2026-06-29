import json
import unittest

from agent.agent import HyruleAgent
from agent.sni.emu_connector import _RetroArchClient


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
        return bytes([self.memory.get(address, 0)])

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


if __name__ == "__main__":
    unittest.main()
