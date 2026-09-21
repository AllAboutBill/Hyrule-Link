"""
operator.py - the operator gate and the operator's view of every room.

The operator page (web/operator.html) is the web global-admin: it lists every
room and can delete one. It is behind a key file, not a Discord login.

- The key is read once, at import, from HYRULELINK_OPERATOR_KEY_FILE (default
  server/operator.key, git-ignored). A relative path is taken from the repo
  root. No file, or an empty one, means the operator routes do not exist (404).
- The key comes only from the X-Operator-Key header, never a query string
  (nginx logs URLs). It is compared with secrets.compare_digest and never
  logged.
- A wrong key is a 403 and counts against the caller's address
  (rate_limit.client_key). MISSES_PER_HOUR wrong keys inside an hour and that
  address gets 429 for the rest of the hour, right key included.
"""

import logging
import os
import secrets
import threading
import time

from fastapi import HTTPException, Request

from server import db
from server.rate_limit import client_key

log = logging.getLogger("HyruleLink.operator")

HEADER = "X-Operator-Key"
MISSES_PER_HOUR = 20
MISS_WINDOW_S = 3600.0

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY_FILE = os.path.join(
    _ROOT, os.environ.get("HYRULELINK_OPERATOR_KEY_FILE", os.path.join("server", "operator.key")))


def load_key(path: str) -> str:
    """The key in `path`, stripped; "" when the file is missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


KEY = load_key(KEY_FILE)

_misses = {}                 # client address -> [monotonic time of each wrong key]
_misses_lock = threading.Lock()


def _recent(times, now):
    return [t for t in times if now - t < MISS_WINDOW_S]


def check(request: Request, now: float = None):
    """Raise the HTTPException to answer with, or return None to let the
    request through."""
    if not KEY:
        raise HTTPException(404, "not found")
    ip = client_key(request)
    now = time.monotonic() if now is None else float(now)
    with _misses_lock:
        misses = _recent(_misses.get(ip, ()), now)
        if len(misses) >= MISSES_PER_HOUR:
            _misses[ip] = misses
            raise HTTPException(429, "too many wrong keys; try again in an hour")
        given = request.headers.get(HEADER, "")
        if not given or not secrets.compare_digest(given.encode("utf-8"), KEY.encode("utf-8")):
            misses.append(now)
            _misses[ip] = misses
            if len(_misses) > 4096:          # forget addresses whose hour is over
                for k in [k for k, v in _misses.items() if not _recent(v, now)]:
                    del _misses[k]
            log.warning("operator: wrong key from %s (%d this hour)", ip, len(misses))
            raise HTTPException(403, "wrong key")
        _misses.pop(ip, None)
    return None


def reset_misses():
    """Forget every counted miss (tests)."""
    with _misses_lock:
        _misses.clear()


def rooms_doc(hub, ttl_days: float) -> dict:
    """Every room, most recently active first: names, times, who is connected.
    Never a player token. `code` is included: the operator may open or delete
    any room."""
    # db.py has no "every room with its code" query and is not edited in this
    # build, so read through its locked query helper.
    rows = db._q("SELECT code, pub_id, name, mode, host_player_id, created_at, last_active "
                 "FROM rooms ORDER BY last_active DESC").fetchall()
    roster = {}
    for p in db._q("SELECT id, room_code, display_name FROM players ORDER BY joined_at").fetchall():
        roster.setdefault(p["room_code"], []).append(p)
    rooms = []
    for r in rows:
        code = r["code"]
        agents = hub.agents.get(code, {})
        uis = hub.uis.get(code, {})
        live = hub.rooms.get(code)
        status = live.status if live is not None else {}
        players = []
        for p in roster.get(code, []):
            pid = p["id"]
            agent = pid in agents
            players.append({"id": pid, "name": p["display_name"],
                            "host": pid == r["host_player_id"],
                            "agent": agent,
                            "emu": agent and bool(status.get(pid, {}).get("emu"))})
        rooms.append({"code": code, "pub_id": r["pub_id"], "name": r["name"],
                      "mode": r["mode"], "created_at": r["created_at"],
                      "last_active": r["last_active"],
                      "in_use": bool(agents or uis),
                      "players": players, "uis": len(uis)})
    return {"ok": True, "now": time.time(), "ttl_days": ttl_days, "rooms": rooms}
