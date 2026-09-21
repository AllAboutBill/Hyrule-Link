"""The server's HTTP routes and /ws hellos, in process.

fastapi.testclient over an in-memory database. Covers the routes the desktop
app depends on (docs/BROWSER_PLAN.md 4.2: new fields only, never a rename or a
removal) and the ones added for the browser client (5.3): room lookup,
/api/health, the operator gate, root static serving and the response headers.
"""
import json
import logging
import os
import unittest
import warnings
from types import SimpleNamespace
from unittest import mock

from server import db
from shared import protocol as P
from shared.items import ITEMS

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")      # starlette's httpx deprecation notice
        from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
except ImportError:                           # httpx is in requirements-dev.txt
    TestClient = None

# server.app opens the database when it is imported: point it at memory first
# so a test run never creates or touches server/hyrulelink.db.
_saved_db = (db.DB_PATH, db._conn)
db.DB_PATH, db._conn = ":memory:", None
from server import app as app_mod  # noqa: E402
from server import operator  # noqa: E402
if db._conn is not None and db._conn is not _saved_db[1]:
    db._conn.close()
db.DB_PATH, db._conn = _saved_db
# wrong-key warnings are expected here; assertLogs still sees them
logging.getLogger("HyruleLink.operator").addHandler(logging.NullHandler())

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
KEY = "test-operator-key-0123456789"

# What the desktop app reads from these replies (agent_gui.py). Supersets are
# fine; a missing key is a broken desktop app.
ROOM_REPLY_KEYS = {"code", "pub_id", "name", "cooldown_s", "host", "player_id",
                   "player_token", "players", "items"}
STATE_KEYS = {"type", "room", "name", "cooldown_s", "host", "mode", "rules", "rule_defaults",
              "rule_presets", "claiming", "rules_summary", "shuffle_s", "shuffle_remaining",
              "players", "ledger", "you", "admin", "spectator"}


def _clear_hub():
    for d in (app_mod.hub.rooms, app_mod.hub.agents, app_mod.hub.uis,
              app_mod.hub.admin_uis, app_mod.hub.apply_failures):
        d.clear()


@unittest.skipIf(TestClient is None, "httpx not installed (pip install -r requirements-dev.txt)")
class ServerCase(unittest.TestCase):
    def setUp(self):
        self._old_db = (db.DB_PATH, db._conn)
        db.DB_PATH, db._conn = ":memory:", None
        db.init()
        _clear_hub()
        for limiter in (app_mod._CREATE_LIMIT, app_mod._JOIN_LIMIT,
                        app_mod._DEVICE_LIMIT, app_mod._LOOKUP_LIMIT):
            limiter._hits.clear()
        operator.reset_misses()
        self.client = TestClient(app_mod.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        _clear_hub()
        operator.reset_misses()
        db._conn.close()
        db.DB_PATH, db._conn = self._old_db

    def create(self, display="Ana", **extra):
        body = {"display_name": display, "cooldown_s": 0}
        body.update(extra)
        r = self.client.post("/api/rooms", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def join(self, code, name="Bo"):
        r = self.client.post(f"/api/rooms/{code}/join", json={"display_name": name})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def hello(self, ws, seat, role="ui", **extra):
        msg = {"type": P.HELLO, "role": role, "room": seat["code"],
               "player_id": seat["player_id"], "token": seat["player_token"]}
        msg.update(extra)
        ws.send_json(msg)

    def commands(self, ws):
        """One grant/revoke per catalog item, as sent on hello and on resync."""
        out = [ws.receive_json() for _ in ITEMS]
        for m in out:
            self.assertIn(m["type"], (P.GRANT, P.REVOKE), m)
        self.assertEqual([m["item"] for m in out], [it.key for it in ITEMS])
        return out


class RoomRoutesTests(ServerCase):
    def test_create_join_resume_keep_the_desktop_shape(self):
        a = self.create("Ana", name="Test room")
        self.assertLessEqual(ROOM_REPLY_KEYS, set(a))
        self.assertEqual(a["name"], "Test room")
        self.assertEqual(a["host"], a["player_id"])
        self.assertEqual(len(a["items"]), len(ITEMS))
        self.assertEqual(set(a["items"][0]), {"key", "name", "image"})

        b = self.join(a["code"])
        self.assertLessEqual(ROOM_REPLY_KEYS, set(b))
        self.assertNotEqual(b["player_id"], a["player_id"])
        self.assertEqual([p["name"] for p in b["players"]], ["Ana", "Bo"])

        r = self.client.post(f"/api/rooms/{a['code'].lower()}/resume",
                             json={"player_id": a["player_id"], "player_token": a["player_token"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertLessEqual(ROOM_REPLY_KEYS, set(r.json()))
        self.assertEqual((r.json()["player_id"], r.json()["player_token"]),
                         (a["player_id"], a["player_token"]))

    def test_unknown_room_and_bad_seat_are_404(self):
        a = self.create()
        r = self.client.post("/api/rooms/NOPE/join", json={"display_name": "Bo"})
        self.assertEqual((r.status_code, r.json()), (404, {"detail": "no such room"}))
        r = self.client.post("/api/rooms/NOPE/resume", json={"player_id": 1, "player_token": "x"})
        self.assertEqual((r.status_code, r.json()), (404, {"detail": "no such room"}))
        r = self.client.post(f"/api/rooms/{a['code']}/resume",
                             json={"player_id": a["player_id"], "player_token": "wrong"})
        self.assertEqual((r.status_code, r.json()), (404, {"detail": "player not found in room"}))

    def test_room_lookup(self):
        a = self.create("Ana", name="Lookup")
        self.join(a["code"])
        r = self.client.get(f"/api/rooms/{a['code'].lower()}")
        self.assertEqual(r.status_code, 200, r.text)
        doc = r.json()
        self.assertEqual(set(doc), {"code", "pub_id", "name", "host", "mode", "cooldown_s",
                                    "players", "live"})
        self.assertEqual(doc["code"], a["code"])
        self.assertEqual(doc["pub_id"], a["pub_id"])
        self.assertEqual(doc["name"], "Lookup")
        self.assertEqual(doc["host"], a["player_id"])
        self.assertEqual(doc["mode"], "normal")
        self.assertEqual(doc["cooldown_s"], 0)
        self.assertEqual([p["name"] for p in doc["players"]], ["Ana", "Bo"])
        self.assertEqual(set(doc["players"][0]), {"id", "name"})
        self.assertEqual(doc["live"], {"uis": 0, "agents": 0})
        self.assertNotIn(a["player_token"], r.text)

    def test_room_lookup_unknown_is_404(self):
        r = self.client.get("/api/rooms/NOPE")
        self.assertEqual((r.status_code, r.json()), (404, {"detail": "no such room"}))

    def test_room_lookup_counts_live_sockets(self):
        a = self.create()
        with self.client.websocket_connect("/ws") as ws:
            self.hello(ws, a)
            ws.receive_json()
            live = self.client.get(f"/api/rooms/{a['code']}").json()["live"]
            self.assertEqual(live, {"uis": 1, "agents": 0})

    def test_public_list_is_unchanged_and_hides_codes(self):
        a = self.create()
        r = self.client.get("/api/rooms")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(r.json()), {"rooms", "login_enabled"})
        self.assertEqual(r.json()["rooms"][0]["pub_id"], a["pub_id"])
        self.assertNotIn(a["code"], r.text)


class HealthTests(ServerCase):
    def test_api_health(self):
        with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
            version = f.read().strip()
        self.create()
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        doc = r.json()
        self.assertEqual(set(doc), {"ok", "version", "rooms", "ts"})
        self.assertIs(doc["ok"], True)
        self.assertEqual(doc["version"], version)
        self.assertIsInstance(doc["rooms"], int)
        self.assertIsInstance(doc["ts"], float)

    def test_old_health_is_unchanged(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(r.json()), {"ok", "ts"})


class OperatorTests(ServerCase):
    def op(self, method, path, key=KEY):
        headers = {operator.HEADER: key} if key is not None else {}
        return self.client.request(method, path, headers=headers)

    def test_no_key_file_means_no_routes(self):
        with mock.patch.object(operator, "KEY", ""):
            for method, path in (("GET", "/api/operator/rooms"),
                                 ("POST", "/api/operator/rooms/ABC/delete")):
                r = self.op(method, path)
                self.assertEqual((r.status_code, r.json()), (404, {"detail": "not found"}))

    def test_wrong_or_missing_key_is_403(self):
        with mock.patch.object(operator, "KEY", KEY):
            for key in ("nope", "", None, KEY + "x"):
                r = self.op("GET", "/api/operator/rooms", key=key)
                self.assertEqual((r.status_code, r.json()), (403, {"detail": "wrong key"}))
            # a query string is never read
            r = self.client.get("/api/operator/rooms", params={"key": KEY})
            self.assertEqual(r.status_code, 403)

    def test_right_key_lists_every_room(self):
        old = self.create("Ana", name="Old")
        bo = self.join(old["code"], "Bo")
        new = self.create("Cy", name="New")
        db._q("UPDATE rooms SET last_active=? WHERE code=?", (1000.0, old["code"]))
        with mock.patch.object(operator, "KEY", KEY), \
                self.client.websocket_connect("/ws") as ws:
            self.hello(ws, new)
            ws.receive_json()
            r = self.op("GET", "/api/operator/rooms")
        self.assertEqual(r.status_code, 200, r.text)
        doc = r.json()
        self.assertEqual(set(doc), {"ok", "now", "ttl_days", "rooms"})
        self.assertIs(doc["ok"], True)
        self.assertEqual(doc["ttl_days"], app_mod.ROOM_TTL_DAYS)
        self.assertEqual([x["code"] for x in doc["rooms"]], [new["code"], old["code"]])
        first, second = doc["rooms"]
        self.assertEqual(set(first), {"code", "pub_id", "name", "mode", "created_at",
                                      "last_active", "in_use", "players", "uis"})
        self.assertEqual((first["name"], first["mode"], first["in_use"], first["uis"]),
                         ("New", "normal", True, 1))
        self.assertEqual((second["in_use"], second["uis"]), (False, 0))
        self.assertEqual(second["players"], [
            {"id": old["player_id"], "name": "Ana", "host": True, "agent": False, "emu": False},
            {"id": bo["player_id"], "name": "Bo", "host": False, "agent": False, "emu": False},
        ])
        for seat in (old, new):
            self.assertNotIn(seat["player_token"], r.text)

    def test_agent_and_emu_show_on_the_operator_list(self):
        a = self.create()
        with mock.patch.object(operator, "KEY", KEY), \
                self.client.websocket_connect("/ws") as ws:
            self.hello(ws, a, role=P.ROLE_AGENT)
            self.commands(ws)
            ws.send_json({"type": P.STATUS, "emu": True})
            ws.send_json({"type": P.RESYNC})
            self.commands(ws)                  # status was handled before this
            doc = self.op("GET", "/api/operator/rooms").json()
            ws.send_json({"type": P.BYE})
        person = doc["rooms"][0]["players"][0]
        self.assertEqual((person["agent"], person["emu"]), (True, True))
        self.assertIs(doc["rooms"][0]["in_use"], True)

    def test_twenty_wrong_keys_lock_the_address_for_the_hour(self):
        with mock.patch.object(operator, "KEY", KEY):
            for _ in range(operator.MISSES_PER_HOUR):
                self.assertEqual(self.op("GET", "/api/operator/rooms", key="guess").status_code, 403)
            r = self.op("GET", "/api/operator/rooms", key="guess")
            self.assertEqual(r.status_code, 429)
            self.assertEqual(self.op("GET", "/api/operator/rooms").status_code, 429)   # even the right key

    def test_miss_budget_is_per_address_and_expires(self):
        def request(ip, key):
            return SimpleNamespace(client=SimpleNamespace(host=ip),
                                   headers={operator.HEADER.lower(): key, operator.HEADER: key})
        with mock.patch.object(operator, "KEY", KEY):
            for i in range(operator.MISSES_PER_HOUR):
                with self.assertRaises(app_mod.HTTPException) as e:
                    operator.check(request("203.0.113.1", "guess"), now=100.0 + i)
                self.assertEqual(e.exception.status_code, 403)
            with self.assertRaises(app_mod.HTTPException) as e:
                operator.check(request("203.0.113.1", KEY), now=200.0)
            self.assertEqual(e.exception.status_code, 429)
            self.assertIsNone(operator.check(request("203.0.113.2", KEY), now=200.0))
            # an hour after the first miss the oldest ones fall out of the window
            self.assertIsNone(operator.check(request("203.0.113.1", KEY), now=100.0 + 3600 + 1))

    def test_the_key_is_never_logged(self):
        with mock.patch.object(operator, "KEY", KEY), \
                self.assertLogs("HyruleLink.operator", "WARNING") as logs:
            self.op("GET", "/api/operator/rooms", key="guess-" + KEY[:6])
        text = "\n".join(logs.output)
        self.assertIn("wrong key", text)
        self.assertNotIn(KEY, text)
        self.assertNotIn("guess-", text)

    def test_delete_closes_and_removes_the_room(self):
        a = self.create()
        with mock.patch.object(operator, "KEY", KEY):
            with self.client.websocket_connect("/ws") as ws:
                self.hello(ws, a)
                ws.receive_json()
                r = self.op("POST", f"/api/operator/rooms/{a['code'].lower()}/delete")
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.json(), {"ok": True, "deleted": a["code"]})
                self.assertEqual(ws.receive_json(), {"type": "reject", "reason": "room closed"})
                with self.assertRaises(WebSocketDisconnect):
                    ws.receive_json()
            self.assertIsNone(db.get_room(a["code"]))
            self.assertNotIn(a["code"], app_mod.hub.rooms)
            self.assertEqual(self.client.get(f"/api/rooms/{a['code']}").status_code, 404)
            r = self.op("POST", f"/api/operator/rooms/{a['code']}/delete")
            self.assertEqual((r.status_code, r.json()), (404, {"detail": "no such room"}))


class StaticTests(ServerCase):
    def _file(self, *parts):
        with open(os.path.join(WEB, *parts), "rb") as f:
            return f.read()

    def test_root_serves_index_html(self):
        for params in ({}, {"watch": "abc"}, {"room": "ABC"}):
            r = self.client.get("/", params=params)
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.headers["content-type"].startswith("text/html"))
            self.assertEqual(r.content, self._file("index.html"))
            self.assertEqual(r.headers["cache-control"], "no-cache")

    def test_web_is_served_at_the_root_and_under_static(self):
        png = self._file("items", "sword-1.png")
        for path in ("/static/items/sword-1.png", "/items/sword-1.png"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertEqual(r.headers["content-type"], "image/png")
            self.assertEqual(r.headers["cache-control"], "public, max-age=604800")
            self.assertEqual(r.content, png)
        self.assertEqual(self.client.get("/index.html").content, self._file("index.html"))

    def test_items_js_when_present(self):
        if not os.path.exists(os.path.join(WEB, "js", "items.js")):
            self.skipTest("web/js/items.js not built yet")
        r = self.client.get("/js/items.js")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/javascript"))
        self.assertEqual(r.headers["cache-control"], "no-cache")

    def test_headers(self):
        for path in ("/", "/api/health", "/items/sword-1.png", "/api/rooms/NOPE"):
            r = self.client.get(path)
            self.assertEqual(r.headers["x-content-type-options"], "nosniff", path)
            self.assertEqual(r.headers["referrer-policy"], "no-referrer", path)
            self.assertNotIn("x-frame-options", r.headers, path)
        self.assertEqual(self.client.get("/api/health").headers["cache-control"], "no-cache")
        # operator.html may not exist yet; the header goes by path either way
        for path in ("/operator.html", "/static/operator.html"):
            self.assertEqual(self.client.get(path).headers["x-frame-options"], "DENY")

    def test_a_missing_sprite_is_not_cached(self):
        r = self.client.get("/items/no-such-item.png")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.headers["cache-control"], "no-cache")

    def test_routes_are_not_shadowed_by_the_root_mount(self):
        self.assertEqual(self.client.get("/health").json()["ok"], True)
        self.assertEqual(self.client.get("/api/health").json()["ok"], True)
        self.assertEqual(self.client.get("/api/me").json()["logged_in"], False)
        self.assertEqual(self.client.get("/auth/device/poll", params={"pair": "x"}).json(),
                         {"status": "expired"})
        self.assertEqual(self.client.get("/api/my-rooms").json(), {"rooms": []})
        self.assertEqual(self.client.get("/api/operator/rooms").status_code, 404)
        self.assertEqual(self.client.get("/api/nope").status_code, 404)

    def test_a_websocket_to_another_path_is_refused(self):
        for path in ("/nope", "/static/ws", "/items/ws"):
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect(path) as ws:
                    ws.receive_json()


class WebSocketTests(ServerCase):
    def test_agent_gets_every_item_then_status_is_accepted(self):
        a = self.create()
        with self.client.websocket_connect("/ws") as ws:
            self.hello(ws, a, role=P.ROLE_AGENT, platform="test")
            first = self.commands(ws)
            self.assertEqual({m["type"] for m in first}, {P.REVOKE})   # a new room owns nothing
            ws.send_json({"type": P.STATUS, "emu": True})
            ws.send_json({"type": P.RESYNC})
            self.commands(ws)                  # in order, so status was handled first
            self.assertEqual(app_mod.hub.rooms[a["code"]].status[a["player_id"]],
                             {"agent": True, "emu": True})
            with self.client.websocket_connect("/ws") as ui:
                self.hello(ui, a)
                me = [p for p in ui.receive_json()["players"] if p["id"] == a["player_id"]]
                self.assertEqual((me[0]["agent"], me[0]["emu"]), (True, True))
            ws.send_json({"type": P.BYE})

    def test_ui_gets_state_with_items_and_you(self):
        a = self.create()
        with self.client.websocket_connect("/ws") as ws:
            self.hello(ws, a, session="not-a-session")
            msg = ws.receive_json()
        self.assertEqual(msg["type"], P.STATE)
        self.assertLessEqual(STATE_KEYS | {"items"}, set(msg))
        self.assertEqual(msg["you"], a["player_id"])
        self.assertEqual((msg["admin"], msg["spectator"]), (False, False))
        self.assertEqual(msg["room"], a["pub_id"])
        self.assertEqual([it["key"] for it in msg["items"]], [it.key for it in ITEMS])
        self.assertEqual(set(msg["items"][0]), {"key", "name", "image"})
        self.assertNotIn(a["code"], json.dumps(msg))

    def test_spectator_watches_by_public_handle(self):
        a = self.create()
        with self.client.websocket_connect("/ws") as ws:
            ws.send_json({"type": P.HELLO, "role": P.ROLE_SPECTATOR, "watch": a["pub_id"]})
            msg = ws.receive_json()
        self.assertEqual(msg["type"], P.STATE)
        self.assertEqual((msg["you"], msg["spectator"]), (None, True))
        self.assertEqual(len(msg["items"]), len(ITEMS))

    def test_bad_token_is_rejected(self):
        a = self.create()
        for role in (P.ROLE_UI, P.ROLE_AGENT):
            with self.client.websocket_connect("/ws") as ws:
                self.hello(ws, dict(a, player_token="wrong"), role=role)
                self.assertEqual(ws.receive_json(),
                                 {"type": P.REJECT, "reason": "bad room/player token"})
                with self.assertRaises(WebSocketDisconnect):
                    ws.receive_json()

    def test_unknown_room_is_rejected(self):
        with self.client.websocket_connect("/ws") as ws:
            ws.send_json({"type": P.HELLO, "role": P.ROLE_UI, "room": "NOPE",
                          "player_id": 1, "token": "x"})
            self.assertEqual(ws.receive_json(), {"type": P.REJECT, "reason": "room not found"})


if __name__ == "__main__":
    unittest.main()
