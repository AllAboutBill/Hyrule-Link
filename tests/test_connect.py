"""QuakeCast cams (server/connect.py) against a fake QuakeCast.

The host opens a QuakeCast room for two to four players; each seat link goes
to that player's own ui sockets and nowhere else; the host closes it. The
fake is a stdlib HTTP server in a thread speaking the part of RaceConnect's
docs/CONTRACT.md this uses: POST /api/rooms, POST /api/seat/<token>
{close, force}, GET /api/seat/<token>. The HyruleLink side runs in process
through fastapi's TestClient over an in-memory database.
"""
import contextlib
import json
import logging
import shutil
import socket
import subprocess
import threading
import time
import unittest
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from server import db, connect
from shared import protocol as P

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")      # starlette's httpx deprecation notice
        from fastapi.testclient import TestClient
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
logging.getLogger("HyruleLink.connect").addHandler(logging.NullHandler())

SEATS = ("a", "b", "c", "d")
ROOT = Path(__file__).resolve().parents[1]


class _Fake(BaseHTTPRequestHandler):
    """A QuakeCast that answers the way RaceConnect's contract says."""
    mode = "ok"          # ok | busy | rate | error | two | junk: how POST /api/rooms answers
    close_status = 200
    seat_status = 200
    calls = []           # (method, path, headers, body)
    rooms = 0
    lock = threading.Lock()

    def _reply(self, status, doc):
        raw = json.dumps(doc).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _log(self, body=None):
        with _Fake.lock:
            _Fake.calls.append((self.command, self.path, dict(self.headers), body))

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
        self._log(body)
        if self.path == "/api/rooms":
            if _Fake.mode in ("busy", "rate"):
                return self._reply(429, {"ok": False, "error": _Fake.mode})
            if _Fake.mode == "error":
                return self._reply(500, {"ok": False})
            with _Fake.lock:
                _Fake.rooms += 1
                rid = "room%04d" % _Fake.rooms
            n = 2 if _Fake.mode == "two" else max(2, min(4, int(body.get("seats") or 2)))
            base = "http://127.0.0.1:%d" % self.server.server_address[1]
            seats = {s: {"token": "st_%s%s" % (rid, s), "url": "%s/seat/st_%s%s" % (base, rid, s)}
                     for s in SEATS[:n]}
            doc = {"ok": True, "room": {"id": rid, "name": body.get("name")},
                   "seats": seats, "view_key": "vk_" + rid}
            if _Fake.mode == "junk":
                del doc["room"]
            return self._reply(200, doc)
        if self.path.startswith("/api/seat/") and isinstance(body, dict) and body.get("close") is True:
            status = _Fake.close_status
            return self._reply(status, {"ok": status == 200, "closed": "x"} if status == 200
                               else {"ok": False, "error": "expired"})
        self._reply(404, {"ok": False, "error": "expired"})

    def do_GET(self):
        self._log()
        status = _Fake.seat_status if self.path.startswith("/api/seat/") else 404
        self._reply(status, {"ok": status == 200})

    def log_message(self, *args):
        pass


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


@unittest.skipIf(TestClient is None, "httpx not installed (pip install -r requirements-dev.txt)")
class CamsCase(unittest.TestCase):
    url = None           # the fake QuakeCast; None = the feature is off

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.fake = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        _Fake.mode, _Fake.close_status, _Fake.seat_status = "ok", 200, 200
        _Fake.calls, _Fake.rooms = [], 0
        self._api = mock.patch.object(connect, "API", self.fake if self.url is None else self.url)
        self._api.start()
        self._old_db = (db.DB_PATH, db._conn)
        db.DB_PATH, db._conn = ":memory:", None
        db.init()
        self._clear_hub()
        for limiter in (app_mod._CREATE_LIMIT, app_mod._JOIN_LIMIT, app_mod._LOOKUP_LIMIT,
                        app_mod._CAMS_LIMIT):
            limiter._hits.clear()
        self.client = TestClient(app_mod.app)
        self.client.__enter__()
        self.stack = contextlib.ExitStack()
        self.n = 0

    def tearDown(self):
        self.stack.close()
        self.client.__exit__(None, None, None)
        self._clear_hub()
        db._conn.close()
        db.DB_PATH, db._conn = self._old_db
        self._api.stop()

    @staticmethod
    def _clear_hub():
        h = app_mod.hub
        for d in (h.rooms, h.agents, h.uis, h.admin_uis, h.apply_failures, h.cams):
            d.clear()

    # -- helpers ---------------------------------------------------------------

    def room(self, *names):
        """The host (first name) and one seat per further name."""
        r = self.client.post("/api/rooms", json={"display_name": names[0], "name": "Friday co-op"})
        self.assertEqual(r.status_code, 200, r.text)
        seats = [r.json()]
        for name in names[1:]:
            j = self.client.post(f"/api/rooms/{seats[0]['code']}/join", json={"display_name": name})
            self.assertEqual(j.status_code, 200, j.text)
            seats.append(j.json())
        return seats

    def ui(self, seat, spectator=False):
        """An open ui socket past its first state, and what it said on hello."""
        ws = self.stack.enter_context(self.client.websocket_connect("/ws"))
        if spectator:
            ws.send_json({"type": P.HELLO, "role": P.ROLE_SPECTATOR, "watch": seat["pub_id"]})
        else:
            ws.send_json({"type": P.HELLO, "role": P.ROLE_UI, "room": seat["code"],
                          "player_id": seat["player_id"], "token": seat["player_token"]})
        first = ws.receive_json()
        self.assertEqual(first["type"], P.STATE)
        return ws, [first] + self.sync(ws)

    def sync(self, ws):
        """Everything the socket was sent so far: a claim of an item that does
        not exist is answered with a reject, after all that came before it."""
        self.n += 1
        ws.send_json({"type": P.CLAIM, "item": "probe%d" % self.n})
        got = []
        while True:
            m = ws.receive_json()
            if m.get("type") == P.REJECT:
                return got
            got.append(m)

    def open(self, host, ids, **extra):
        body = {"player_id": host["player_id"], "player_token": host["player_token"], "seats": ids}
        body.update(extra)
        return self.client.post(f"/api/rooms/{host['code']}/cams", json=body)

    def close(self, who):
        return self.client.post(f"/api/rooms/{who['code']}/cams/close",
                                json={"player_id": who["player_id"], "player_token": who["player_token"]})

    def posts(self, prefix):
        return [c for c in _Fake.calls if c[0] == "POST" and c[1].startswith(prefix)]

    @staticmethod
    def cams_msgs(msgs):
        return [m for m in msgs if m.get("type") == "cams"]

    def assert_no_secret(self, text):
        """No seat token, seat link or view key the fake handed out."""
        self.assertNotIn("st_room", text)
        self.assertNotIn("/seat/", text)
        self.assertNotIn("vk_room", text)


class CamsOffTests(CamsCase):
    url = ""

    def test_off_means_404_no_cams_key_and_no_message(self):
        ana, bo = self.room("Ana", "Bo")
        r = self.open(ana, [ana["player_id"], bo["player_id"]])
        self.assertEqual((r.status_code, r.json()), (404, {"detail": "not found"}))
        r = self.close(ana)
        self.assertEqual((r.status_code, r.json()), (404, {"detail": "not found"}))
        _, said = self.ui(ana)
        self.assertNotIn("cams", said[0])
        self.assertEqual(self.cams_msgs(said), [])
        self.assertEqual(_Fake.calls, [])


class CamsTests(CamsCase):
    def test_open_hands_each_seat_link_to_its_own_player_only(self):
        ana, bo, cy = self.room("Ana", "Bo", "Cy")
        socks = {name: self.ui(s)[0] for name, s in (("ana", ana), ("bo", bo), ("cy", cy))}
        socks["watch"] = self.ui(ana, spectator=True)[0]
        ids = [ana["player_id"], bo["player_id"]]

        r = self.open(ana, ids)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"ok": True, "cams": {"seats": ids, "error": ""}})
        self.assert_no_secret(r.text)       # not even the host's own link: that goes to the socket

        (method, path, headers, body), = _Fake.calls
        self.assertEqual((method, path), ("POST", "/api/rooms"))
        self.assertEqual(body, {"name": "Friday co-op", "seats": 2, "a": "Ana", "b": "Bo"})
        # QuakeCast is on loopback here, so the caller's address travels with it
        self.assertEqual(headers.get("X-Forwarded-For"), "testclient")

        said = {name: self.sync(ws) for name, ws in socks.items()}
        entry = app_mod.hub.cams[ana["code"]]
        self.assertEqual(self.cams_msgs(said["ana"]), [{"type": "cams", "url": entry["seats"]["a"]["url"]}])
        self.assertEqual(self.cams_msgs(said["bo"]), [{"type": "cams", "url": entry["seats"]["b"]["url"]}])
        self.assertTrue(entry["seats"]["a"]["url"].startswith(self.fake + "/seat/st_"))
        for name in ("cy", "watch"):
            self.assertEqual(self.cams_msgs(said[name]), [])
            self.assert_no_secret(json.dumps(said[name]))
        for name, msgs in said.items():
            states = [m for m in msgs if m["type"] == P.STATE]
            self.assertTrue(states, name)
            self.assertEqual(states[-1]["cams"], {"seats": ids, "error": ""})
            self.assertIn("Cams are open for Ana and Bo through QuakeCast",
                          [m.get("text") for m in msgs if m["type"] == P.EVENT])
            self.assert_no_secret(json.dumps(states))

    def test_a_later_hello_gets_its_own_link_and_nobody_else_s(self):
        ana, bo, cy = self.room("Ana", "Bo", "Cy")
        self.assertEqual(self.open(ana, [ana["player_id"], bo["player_id"]]).status_code, 200)
        entry = app_mod.hub.cams[ana["code"]]
        _, said = self.ui(bo)
        self.assertEqual(said[0]["cams"], {"seats": [ana["player_id"], bo["player_id"]], "error": ""})
        self.assertEqual(self.cams_msgs(said), [{"type": "cams", "url": entry["seats"]["b"]["url"]}])
        _, said = self.ui(cy)
        self.assertEqual(self.cams_msgs(said), [{"type": "cams", "url": None}])
        self.assert_no_secret(json.dumps(said))
        _, said = self.ui(ana, spectator=True)
        self.assertEqual(self.cams_msgs(said), [])
        self.assert_no_secret(json.dumps(said))

    def test_four_seats(self):
        seats = self.room("Ana", "Bo", "Cy", "Di")
        ids = [s["player_id"] for s in seats]
        r = self.open(seats[0], ids)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.posts("/api/rooms")[0][3],
                         {"name": "Friday co-op", "seats": 4, "a": "Ana", "b": "Bo", "c": "Cy", "d": "Di"})
        self.assertEqual(r.json()["cams"]["seats"], ids)
        for seat, s in zip(seats, SEATS):
            self.assertEqual(app_mod.hub.cams_url(seat["code"], seat["player_id"]),
                             app_mod.hub.cams[seat["code"]]["seats"][s]["url"])

    def test_close_ends_the_quakecast_room_and_takes_the_links_back(self):
        ana, bo = self.room("Ana", "Bo")
        a_ws, b_ws = self.ui(ana)[0], self.ui(bo)[0]
        self.assertEqual(self.open(ana, [ana["player_id"], bo["player_id"]]).status_code, 200)
        token_a = app_mod.hub.cams[ana["code"]]["seats"]["a"]["token"]
        self.sync(a_ws), self.sync(b_ws)

        r = self.close(ana)
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))
        (_, path, _, body), = self.posts("/api/seat/")
        self.assertEqual((path, body), ("/api/seat/" + token_a, {"close": True, "force": True}))
        self.assertNotIn(ana["code"], app_mod.hub.cams)
        for ws in (a_ws, b_ws):
            msgs = self.sync(ws)
            self.assertEqual(self.cams_msgs(msgs), [{"type": "cams", "url": None}])
            self.assertIsNone([m for m in msgs if m["type"] == P.STATE][-1]["cams"])
            self.assertIn("Cams were closed", [m.get("text") for m in msgs if m["type"] == P.EVENT])
        # nothing open: still fine, and QuakeCast is not bothered
        r = self.close(ana)
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))
        self.assertEqual(len(self.posts("/api/seat/")), 1)

    def test_only_the_host_opens_or_closes(self):
        ana, bo = self.room("Ana", "Bo")
        ids = [ana["player_id"], bo["player_id"]]
        for r in (self.open(bo, ids), self.close(bo)):
            self.assertEqual((r.status_code, r.json()), (403, {"detail": "host only"}))
        for r in (self.open(dict(ana, player_token="wrong"), ids), self.close(dict(ana, player_token="wrong")),
                  self.open(dict(ana, player_id="x"), ids)):
            self.assertEqual((r.status_code, r.json()), (403, {"detail": "bad room/player token"}))
        r = self.client.post("/api/rooms/NOPE/cams", json={"player_id": 1, "player_token": "x", "seats": ids})
        self.assertEqual((r.status_code, r.json()), (404, {"detail": "no such room"}))
        self.assertEqual(_Fake.calls, [])
        # a Discord admin may, as with the admin_* messages; they get no seat of their own
        with mock.patch.object(app_mod, "_session_from_request", return_value={"admin": True}):
            r = self.client.post(f"/api/rooms/{ana['code']}/cams", json={"seats": ids})
            self.assertEqual(r.status_code, 200, r.text)
            r = self.client.post(f"/api/rooms/{ana['code']}/cams/close", json={})
            self.assertEqual(r.status_code, 200, r.text)

    def test_the_pick_is_checked_before_quakecast_is_asked(self):
        seats = self.room("Ana", "Bo", "Cy", "Di", "Ed")
        a, b = seats[0]["player_id"], seats[1]["player_id"]
        for pick in ([a], [a, a], [a, 999], [s["player_id"] for s in seats], "12", None, [a, "x"]):
            with self.subTest(pick=pick):
                r = self.open(seats[0], pick)
                self.assertEqual((r.status_code, r.json()),
                                 (422, {"detail": "Pick two to four different players in the room."}))
        self.assertEqual(_Fake.calls, [])
        self.assertEqual(self.open(seats[0], [b, a]).json()["cams"]["seats"], [b, a])   # seat order is the pick's

    def test_quakecast_saying_no_is_said_in_words(self):
        ana, bo, cy = self.room("Ana", "Bo", "Cy")
        cases = (
            ("busy", "QuakeCast is full right now. It runs a handful of rooms at a time; try again in a bit."),
            ("rate", "QuakeCast says slow down. Try again in a few minutes."),
            ("error", "QuakeCast answered 500."),
            ("junk", "QuakeCast answered with something unexpected."),
            ("two", "This QuakeCast seats two players at a time. Pick two."),
        )
        for mode, words in cases:
            with self.subTest(mode=mode):
                _Fake.mode = mode
                r = self.open(ana, [ana["player_id"], bo["player_id"], cy["player_id"]])
                self.assertEqual((r.status_code, r.json()), (502, {"detail": words}))
                self.assertNotIn(ana["code"], app_mod.hub.cams)
        # the two-seat room an older QuakeCast made anyway is ended, not left in a slot
        (_, path, _, body), = self.posts("/api/seat/")
        self.assertEqual((path, body), ("/api/seat/st_room0002a", {"close": True, "force": True}))
        _, said = self.ui(ana)
        self.assertIsNone(said[0]["cams"])

    def test_quakecast_down_is_said_in_words_and_close_still_closes(self):
        ana, bo = self.room("Ana", "Bo")
        ids = [ana["player_id"], bo["player_id"]]
        with mock.patch.object(connect, "API", "http://127.0.0.1:%d" % _free_port()):
            r = self.open(ana, ids)
        self.assertEqual((r.status_code, r.json()), (502, {"detail": "QuakeCast is not answering."}))
        self.assertNotIn(ana["code"], app_mod.hub.cams)

        self.assertEqual(self.open(ana, ids).status_code, 200)
        with mock.patch.object(connect, "API", "http://127.0.0.1:%d" % _free_port()):
            r = self.close(ana)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["warning"].startswith(
            "Cams are closed here, but QuakeCast did not take it: QuakeCast is not answering."))
        self.assertNotIn(ana["code"], app_mod.hub.cams)       # never stuck open

        _Fake.close_status = 500
        self.assertEqual(self.open(ana, ids).status_code, 200)
        r = self.close(ana)
        self.assertIn("QuakeCast answered 500.", r.json()["warning"])
        self.assertNotIn(ana["code"], app_mod.hub.cams)

    def test_no_token_or_link_in_anything_but_the_seat_s_own_message(self):
        ana, bo, cy = self.room("Ana", "Bo", "Cy")
        self.assertEqual(self.open(ana, [ana["player_id"], bo["player_id"]]).status_code, 200)
        doc = app_mod.hub.serialize(ana["code"])
        self.assertEqual(doc["cams"], {"seats": [ana["player_id"], bo["player_id"]], "error": ""})
        self.assert_no_secret(json.dumps(doc))
        for path in (f"/api/rooms/{ana['code']}", "/api/rooms"):
            self.assert_no_secret(self.client.get(path).text)
        with mock.patch.object(operator, "KEY", "k" * 24):
            self.assert_no_secret(self.client.get("/api/operator/rooms",
                                                  headers={operator.HEADER: "k" * 24}).text)
        for seat in (ana, bo, cy):
            r = self.client.post(f"/api/rooms/{seat['code']}/resume",
                                 json={"player_id": seat["player_id"], "player_token": seat["player_token"]})
            self.assert_no_secret(r.text)

    def test_opening_again_replaces_and_ends_the_old_room(self):
        ana, bo, cy = self.room("Ana", "Bo", "Cy")
        b_ws, c_ws = self.ui(bo)[0], self.ui(cy)[0]
        self.assertEqual(self.open(ana, [ana["player_id"], bo["player_id"]]).status_code, 200)
        old_a = app_mod.hub.cams[ana["code"]]["seats"]["a"]["token"]
        self.sync(b_ws), self.sync(c_ws)
        self.assertEqual(self.open(ana, [ana["player_id"], cy["player_id"]]).status_code, 200)
        self.assertEqual(self.cams_msgs(self.sync(b_ws)), [{"type": "cams", "url": None}])
        self.assertEqual(self.cams_msgs(self.sync(c_ws)),
                         [{"type": "cams", "url": app_mod.hub.cams[ana["code"]]["seats"]["b"]["url"]}])
        self.assertTrue(_wait(lambda: [c[1] for c in self.posts("/api/seat/")] == ["/api/seat/" + old_a]))

    def test_deleting_the_room_ends_its_quakecast_room(self):
        ana, bo = self.room("Ana", "Bo")
        self.assertEqual(self.open(ana, [ana["player_id"], bo["player_id"]]).status_code, 200)
        token_a = app_mod.hub.cams[ana["code"]]["seats"]["a"]["token"]
        with mock.patch.object(operator, "KEY", "k" * 24):
            r = self.client.post(f"/api/operator/rooms/{ana['code']}/delete",
                                 headers={operator.HEADER: "k" * 24})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(ana["code"], app_mod.hub.cams)
        self.assertTrue(_wait(lambda: [c[1] for c in self.posts("/api/seat/")] == ["/api/seat/" + token_a]))

    def test_a_quakecast_room_that_ended_shows_as_ended(self):
        ana, bo = self.room("Ana", "Bo")
        a_ws = self.ui(ana)[0]
        self.assertEqual(self.open(ana, [ana["player_id"], bo["player_id"]]).status_code, 200)
        self.sync(a_ws)
        entry = app_mod.hub.cams[ana["code"]]
        # just opened: a hello does not ask QuakeCast again
        self.ui(bo)
        self.assertEqual([c for c in _Fake.calls if c[0] == "GET"], [])
        # a while later, and QuakeCast has let the room go (two idle hours)
        _Fake.seat_status = 404
        entry["checked"] = 0
        self.ui(bo)
        # the check runs beside the sockets: wait until it has said all it will
        self.assertTrue(_wait(lambda: entry["error"] == connect.ENDED and not connect._loose))
        (_, path, _, _), = [c for c in _Fake.calls if c[0] == "GET"]
        self.assertEqual(path, "/api/seat/" + entry["seats"]["a"]["token"])
        msgs = self.sync(a_ws)
        self.assertEqual(self.cams_msgs(msgs), [{"type": "cams", "url": None}])
        self.assertEqual([m for m in msgs if m["type"] == P.STATE][-1]["cams"],
                         {"seats": [ana["player_id"], bo["player_id"]], "error": "ended"})
        self.assertIn("The cam room ended on QuakeCast", [m.get("text") for m in msgs if m["type"] == P.EVENT])
        self.assertIsNone(app_mod.hub.cams_url(ana["code"], ana["player_id"]))
        # closing an ended room does not ask QuakeCast to close it
        self.assertEqual(self.close(ana).json(), {"ok": True})
        self.assertEqual(self.posts("/api/seat/"), [])

    def test_quakecast_not_answering_a_check_changes_nothing(self):
        ana, bo = self.room("Ana", "Bo")
        self.assertEqual(self.open(ana, [ana["player_id"], bo["player_id"]]).status_code, 200)
        entry = app_mod.hub.cams[ana["code"]]
        _Fake.seat_status = 502
        entry["checked"] = 0
        self.ui(bo)
        self.assertTrue(_wait(lambda: any(c[0] == "GET" for c in _Fake.calls) and not connect._loose))
        self.assertEqual(entry["error"], "")
        self.assertEqual(self.cams_msgs(self.ui(ana)[1])[0]["url"], entry["seats"]["a"]["url"])


_VIEWS = r"""
const C = require(process.argv[1]);
const players = [{id: 1, name: 'Ana'}, {id: 2, name: 'Bo'}, {id: 3, name: 'Cy'}];
const open = {seats: [1, 2], error: ''};
const st = (cams, you, extra) => Object.assign({cams, you, players, host: 1}, extra || {});
const out = {
  off: C.view({you: 1, players, host: 1}, true, true, null),
  watcher: C.view(st(open, null), false, false, 'https://q/seat/x'),
  none: C.view(st(null, 2), false, true, null),
  host: C.view(st(null, 1), true, true, null),
  alone: C.view(st(null, 1, {players: [players[0]]}), true, true, null),
  seated: C.view(st(open, 2), false, true, 'https://q/seat/b'),
  stale: C.view(st(open, 3), false, true, 'https://q/seat/b'),
  hostOpen: C.view(st(open, 1), true, true, 'https://q/seat/a'),
  gone: C.view(st({seats: [1, 9], error: ''}, 1), true, true, null),
  ended: C.view(st({seats: [1, 2], error: 'ended'}, 2), false, true, 'https://q/seat/b'),
  endedHost: C.view(st({seats: [1, 2], error: 'ended'}, 1), true, true, null),
  lists: [C.list(['Ana']), C.list(['Ana', 'Bo']), C.list(['Ana', 'Bo', 'Cy'])],
};
console.log(JSON.stringify(out));
"""


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class CamsPageTests(unittest.TestCase):
    """web/js/cams.js's view(): what the Cams row says to whom."""

    def test_the_row(self):
        done = subprocess.run([shutil.which("node"), "-e", _VIEWS, str(ROOT / "web" / "js" / "cams.js")],
                              capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        v = json.loads(done.stdout)
        for hidden in ("off", "watcher", "none"):
            self.assertEqual(v[hidden], {"show": False}, hidden)
        self.assertEqual(v["host"]["pick"], [{"id": 1, "name": "Ana"}, {"id": 2, "name": "Bo"},
                                             {"id": 3, "name": "Cy"}])
        self.assertEqual((v["host"]["open"], v["host"]["close"]), (None, False))
        self.assertEqual((v["alone"]["pick"], v["alone"]["small"]), (None, "Needs two players in the room."))
        self.assertEqual(v["seated"]["text"], "Cams are open for Ana and Bo through QuakeCast.")
        self.assertEqual((v["seated"]["open"], v["seated"]["close"], v["seated"]["lamp"]),
                         ("https://q/seat/b", False, "gold"))
        self.assertIsNone(v["stale"]["open"])          # a link for somebody without a seat is not shown
        self.assertEqual((v["hostOpen"]["open"], v["hostOpen"]["close"]), ("https://q/seat/a", True))
        self.assertEqual(v["gone"]["text"], "Cams are open for Ana and someone who left through QuakeCast.")
        self.assertEqual((v["ended"]["text"], v["ended"]["open"], v["ended"]["pick"]),
                         ("The cam room has ended.", None, None))
        self.assertEqual((v["endedHost"]["text"], len(v["endedHost"]["pick"])), ("The cam room has ended.", 3))
        self.assertEqual(v["lists"], ["Ana", "Ana and Bo", "Ana, Bo and Cy"])
        for key, row in v.items():
            if isinstance(row, dict) and row.get("show"):
                self.assertNotIn("!", row["text"] + row["small"], key)


class ClientAddressTests(unittest.TestCase):
    def test_the_address_goes_only_to_a_loopback_quakecast(self):
        self.assertTrue(connect._loopback("http://127.0.0.1:5043"))
        self.assertTrue(connect._loopback("http://localhost:5043/"))
        self.assertFalse(connect._loopback("https://www.billogna.lol/connect"))
        reply = (200, {"ok": True, "room": {"id": "r"}, "seats": {
            s: {"token": "st_" + s, "url": "u/" + s} for s in "ab"}})
        for api, sent in (("http://127.0.0.1:5043", {"X-Forwarded-For": "203.0.113.9"}),
                          ("https://www.billogna.lol/connect", {})):
            with self.subTest(api=api), mock.patch.object(connect, "API", api), \
                    mock.patch.object(connect, "_call", return_value=reply) as call:
                connect.create("Room", ["Ana", "Bo"], "203.0.113.9")
                self.assertEqual(call.call_args[0][3], sent)


if __name__ == "__main__":
    unittest.main()
