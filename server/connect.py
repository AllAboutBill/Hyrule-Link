"""
connect.py - QuakeCast cams for a co-op room.

QuakeCast (F:\\RaceConnect, https://www.billogna.lol/connect) puts two to four
players' cam, game window and item tracker into each other's OBS. It is its
own service with its own rooms; this file only uses its public HTTP API
(RaceConnect docs/CONTRACT.md), so neither project imports the other and
either keeps working with the other switched off. Copied in shape from
EtherNet's server/ethernet/connect.py, with urllib in a thread in place of
aiohttp (no new server dependency) and no clock: BombosSwap has no race.

What the host's one click does:

    POST <connect>/api/rooms {name, seats: n, a, b, c?, d?}
        -> a QuakeCast room and one seat link per player. A seat link is a
           credential: it goes to that player's own ui sockets and nowhere
           else, never into the state document.
    POST <connect>/api/seat/<seat a's token> {close: true, force: true}
        -> ends it. Only seat a (whoever made the room: this server) may
           close a QuakeCast room, and the host already decided, so force.

Things that will bite:

- HYRULELINK_CONNECT_URL empty (the default) means the feature is off: the
  routes answer 404, the state document has no `cams` key, and the page
  hides its row. On the droplet it is http://127.0.0.1:5043, QuakeCast's own
  port, not the public URL.
- QuakeCast budgets rooms per client address (6 an hour) and believes
  X-Forwarded-For only from loopback. So the caller's address is passed on
  only when the URL is loopback; otherwise every room would count against
  this server.
- The cams live in hub memory only (ledger.RoomHub.cams). A server restart
  forgets them; the QuakeCast room goes on until it ends by itself, two
  hours after the last player leaves its seat. The host can open new ones.
- Tokens and seat links are never logged.
"""

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("HyruleLink.connect")

API = (os.environ.get("HYRULELINK_CONNECT_URL", "") or "").strip().rstrip("/")
SEAT_IDS = ("a", "b", "c", "d")      # QuakeCast seats two, and up to four when asked
MIN_SEATS, MAX_SEATS = 2, len(SEAT_IDS)
TIMEOUT_S = 10
CHECK_EVERY_S = 60.0                 # at most one "is it still there?" per room
USER_AGENT = "BombosSwap (https://hyrulelink.billogna.lol)"
ENDED = "ended"                      # cams.error once QuakeCast says the room is gone

_loose = set()                       # background tasks, held until they finish


class ConnectError(Exception):
    """Something a person can read about why QuakeCast did not play along."""


def enabled() -> bool:
    return bool(API)


def _loopback(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


# -- QuakeCast's HTTP API (blocking: call through asyncio.to_thread) ------------

def _call(method: str, path: str, body=None, headers=None):
    """(status, parsed JSON or None). Raises ConnectError only when QuakeCast
    cannot be reached at all; an HTTP error status is returned, not raised."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(API + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, OSError, ValueError):
        raise ConnectError("QuakeCast is not answering.")
    try:
        return status, json.loads(raw.decode("utf-8") or "null")
    except ValueError:
        return status, None


def create(title: str, names, client_ip: str = "") -> dict:
    """`names`: two to four player names, in seat order.
    -> {"room_id", "seats": {"a": {"token", "url"}, "b": {...}, ...}}"""
    names = [str(n)[:24] for n in names][:MAX_SEATS]
    want = SEAT_IDS[:len(names)]
    ask = {"name": str(title or "BombosSwap")[:60], "seats": len(names)}
    ask.update(dict(zip(want, names)))
    headers = {}
    if client_ip and _loopback(API):
        headers["X-Forwarded-For"] = client_ip          # QuakeCast budgets rooms per address
    status, body = _call("POST", "/api/rooms", ask, headers)
    if status == 429:
        busy = isinstance(body, dict) and body.get("error") == "busy"
        raise ConnectError("QuakeCast is full right now. It runs a handful of rooms at a time; "
                           "try again in a bit." if busy else
                           "QuakeCast says slow down. Try again in a few minutes.")
    if status != 200 or not isinstance(body, dict) or not body.get("ok"):
        raise ConnectError(f"QuakeCast answered {status}.")
    got = body.get("seats") if isinstance(body.get("seats"), dict) else {}
    if any(s not in got for s in want):
        # an older QuakeCast ignores `seats` and makes two. Nobody will ever
        # hold a link to that room, so end it now rather than leave it in a slot.
        try:
            close_room(str(got["a"]["token"]))
        except (KeyError, TypeError, ConnectError):
            pass
        raise ConnectError("This QuakeCast seats two players at a time. Pick two.")
    try:
        seats = {s: {"token": str(got[s]["token"]), "url": str(got[s]["url"])} for s in want}
        return {"room_id": str(body["room"]["id"]), "seats": seats}
    except (KeyError, TypeError):
        raise ConnectError("QuakeCast answered with something unexpected.")


def close_room(token: str):
    """End a QuakeCast room this server made. A room already gone is fine."""
    status, _ = _call("POST", "/api/seat/" + urllib.parse.quote(token, safe=""),
                      {"close": True, "force": True})
    if status not in (200, 404, 410):
        raise ConnectError(f"QuakeCast answered {status}.")


def alive(token: str):
    """True while the seat exists, False once QuakeCast says it is gone, None
    when it cannot tell (not answering, or an odd answer). A read: QuakeCast
    does not count it as use, so it never keeps a room alive."""
    try:
        status, _ = _call("GET", "/api/seat/" + urllib.parse.quote(token, safe=""))
    except ConnectError:
        return None
    if status == 200:
        return True
    return False if status in (404, 410) else None


# -- the room side (async; `hub` is ledger.hub) ---------------------------------

def _names(names) -> str:
    names = list(names)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _background(fn, *args):
    """Run a blocking QuakeCast call in a thread, not waited for."""
    async def run():
        try:
            await asyncio.to_thread(fn, *args)
        except ConnectError as e:
            log.info("quakecast: %s", e)
        except Exception:
            log.exception("quakecast call failed")
    try:
        task = asyncio.get_running_loop().create_task(run())
    except RuntimeError:              # no loop (a plain script): nothing to do
        return
    _loose.add(task)
    task.add_done_callback(_loose.discard)


def end_in_background(entry):
    """Close the QuakeCast room behind `entry` (a hub.cams value), if it is
    still open there. Used when a BombosSwap room is deleted or replaced."""
    if not entry or entry.get("error") or not enabled():
        return
    try:
        token = entry["seats"]["a"]["token"]
    except (KeyError, TypeError):
        return
    _background(close_room, token)


async def _tell(hub, code, player_ids):
    """Each player's own seat link (or null) on each of their own ui sockets."""
    ids = set(player_ids)
    for ws, uid in list(hub.uis.get(code, {}).items()):
        if uid is not None and uid in ids:
            await hub._send(ws, {"type": "cams", "url": hub.cams_url(code, uid)})


async def hello(hub, code, ws, user_id):
    """A ui socket just said hello: its player's seat link (or null), and a
    look at whether the QuakeCast room is still there."""
    if not enabled() or user_id is None:
        return
    await hub._send(ws, {"type": "cams", "url": hub.cams_url(code, user_id)})
    c = hub.cams.get(code)
    if c and not c.get("error") and time.time() - c.get("checked", 0) >= CHECK_EVERY_S:
        c["checked"] = time.time()
        try:
            task = asyncio.get_running_loop().create_task(_check(hub, code, c))
        except RuntimeError:
            return
        _loose.add(task)
        task.add_done_callback(_loose.discard)


async def _check(hub, code, c):
    try:
        ok = await asyncio.to_thread(alive, c["seats"]["a"]["token"])
    except Exception:
        log.exception("quakecast check failed")
        return
    if ok is not False or hub.cams.get(code) is not c or c.get("error"):
        return
    c["error"] = ENDED
    log.info("room %s: its QuakeCast room has ended", code)
    await _tell(hub, code, [s["player"] for s in c["seats"].values()])
    await hub.broadcast_event(code, "The cam room ended on QuakeCast")
    await hub.broadcast_state(code)


async def open_cams(hub, code: str, player_ids, client_ip: str = ""):
    """Open a QuakeCast room for these players (two to four, in seat order).
    Raises ValueError for a bad pick and ConnectError for QuakeCast's no."""
    room = hub.rooms.get(code)
    if room is None:
        raise LookupError("no such room")
    try:
        ids = [int(i) for i in player_ids] if isinstance(player_ids, (list, tuple)) else []
    except (TypeError, ValueError):
        ids = []
    if not (MIN_SEATS <= len(ids) <= MAX_SEATS) or len(set(ids)) != len(ids) \
            or any(i not in room.names for i in ids):
        raise ValueError("Pick two to four different players in the room.")
    names = [room.names[i] for i in ids]
    info = await asyncio.to_thread(create, room.name, names, client_ip)
    if hub.rooms.get(code) is not room:            # deleted while QuakeCast answered
        end_in_background({"seats": info["seats"]})
        raise LookupError("no such room")
    old = hub.cams.get(code)
    hub.cams[code] = {
        "room_id": info["room_id"],
        "seats": {s: dict(info["seats"][s], player=pid) for s, pid in zip(SEAT_IDS, ids)},
        "error": "",
        "checked": time.time(),
    }
    end_in_background(old)
    log.info("room %s: QuakeCast room %s for %d players", code, info["room_id"], len(ids))
    before = [s["player"] for s in old["seats"].values()] if old else []
    await _tell(hub, code, set(ids) | set(before))
    await hub.broadcast_event(code, f"Cams are open for {_names(names)} through QuakeCast")
    await hub.broadcast_state(code)
    return hub.cams_doc(code)


async def close_cams(hub, code: str) -> str:
    """Forget the cams here and end the QuakeCast room. Returns "" or, when
    QuakeCast could not be told, words saying so; the cams are closed here
    either way, so a QuakeCast that is down can never leave them stuck open."""
    c = hub.cams.pop(code, None)
    if c is None:
        return ""
    await _tell(hub, code, [s["player"] for s in c["seats"].values()])
    await hub.broadcast_event(code, "Cams were closed")
    await hub.broadcast_state(code)
    if c.get("error"):
        return ""
    try:
        await asyncio.to_thread(close_room, c["seats"]["a"]["token"])
    except ConnectError as e:
        log.info("room %s: QuakeCast room not closed: %s", code, e)
        return (f"Cams are closed here, but QuakeCast did not take it: {e} Its room ends "
                "by itself two hours after the last player leaves their seat.")
    return ""
