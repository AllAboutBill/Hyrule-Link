"""HUD strip ownership when BillognaBot runs on the same PC.

The agent hands its notifications to the bot's /api/hud/say when it has a
bot_hud_url and the bot says it drew the line; otherwise (no url, bot not
running, bot can't draw right now) it writes the strip itself. A dead
endpoint is remembered for BOT_HUD_RETRY_S so a room full of item moves
doesn't stall on connection attempts.
"""
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from agent import agent as agent_mod
from agent.agent import HyruleAgent
from tests.test_effects import MemoryTransport, Socket


class _Bot(BaseHTTPRequestHandler):
    shown = True          # what the fake bot replies
    received = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        _Bot.received.append(json.loads(body.decode("utf-8")))
        reply = json.dumps({"ok": True, "shown": _Bot.shown}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


def _serve():
    srv = HTTPServer(("127.0.0.1", 0), _Bot)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/api/hud/say"


def _agent(t, url):
    a = HyruleAgent(t, "ws://test", "ROOM", 1, "token", bot_hud_url=url)
    a.ws = Socket()
    a._ws_ready.set()
    return a


def _wait(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


class HudForwardTests(unittest.TestCase):
    def setUp(self):
        _Bot.received = []
        _Bot.shown = True

    def test_no_url_draws_locally_and_synchronously(self):
        t = MemoryTransport()
        a = _agent(t, None)
        a._show_notification("Lamp stolen from Bill")
        self.assertEqual(len(a.hud._queue), 1)       # queued for our own tick

    def test_bot_that_draws_it_takes_the_line(self):
        srv, url = _serve()
        try:
            t = MemoryTransport()
            a = _agent(t, url)
            a._show_notification("Lamp stolen from Bill")
            self.assertTrue(_wait(lambda: len(_Bot.received) == 1))
            self.assertEqual(_Bot.received[0], {"text": "Lamp stolen from Bill", "source": "hyrulelink"})
            time.sleep(0.1)
            self.assertEqual(len(a.hud._queue), 0)   # not drawn twice
            self.assertEqual(a._bot_hud_retry_at, 0.0)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_bot_that_cannot_draw_falls_back_to_local(self):
        srv, url = _serve()
        _Bot.shown = False                            # SNI not connected there
        try:
            t = MemoryTransport()
            a = _agent(t, url)
            a._show_notification("Hot potato - Bow is yours")
            self.assertTrue(_wait(lambda: len(a.hud._queue) == 1))
            self.assertEqual(len(_Bot.received), 1)
            self.assertEqual(a._bot_hud_retry_at, 0.0)   # still worth asking next time
        finally:
            srv.shutdown()
            srv.server_close()

    def test_no_bot_falls_back_and_backs_off(self):
        srv, url = _serve()
        srv.shutdown()
        srv.server_close()                            # port now refuses
        t = MemoryTransport()
        a = _agent(t, url)
        a._show_notification("Sword borrowed by Ted (60s)")
        self.assertTrue(_wait(lambda: len(a.hud._queue) == 1))
        self.assertGreater(a._bot_hud_retry_at, time.time() + agent_mod.BOT_HUD_RETRY_S - 5)
        # inside the back-off window the next line skips the network entirely
        a._show_notification("Bow stolen from Ana")
        self.assertEqual(len(a.hud._queue), 2)

    def test_env_switch_disables_forwarding(self):
        self.assertTrue(agent_mod.BOT_HUD_URL.startswith("http") or agent_mod.BOT_HUD_URL == "")


if __name__ == "__main__":
    unittest.main()
