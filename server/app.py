"""
app.py — HyruleLink coordination server (FastAPI).

Serves the web UI, the account/room REST API, and the single /ws WebSocket that
both player agents and browsers dial out to. Run remotely (e.g. your droplet)
so every remote player can reach it.

    uvicorn server.app:app --host 0.0.0.0 --port 5019
"""

import asyncio
import logging
import math
import os
import time


def _load_dotenv():
    """Load KEY=VALUE lines from a repo-root `.env` into the environment (real
    env vars win). Keeps secrets like DISCORD_CLIENT_SECRET / SESSION_SECRET out
    of git and the command line. Must run before anything below reads os.environ."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_dotenv()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from server import db, auth, names
from server.rate_limit import RateLimiter, client_key
from server.ledger import hub, resolve_pickup, resolve_claim, ownership_commands
from shared import protocol as P
from shared.items import ITEMS, item_image

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

app = FastAPI(title="HyruleLink")
db.init()
logger = logging.getLogger("HyruleLink.server")

# Rooms idle longer than this are auto-deleted (with their players/ledger).
ROOM_TTL_DAYS = float(os.environ.get("HYRULELINK_ROOM_TTL_DAYS", "14"))
_CREATE_LIMIT = RateLimiter(10, 60)
_JOIN_LIMIT = RateLimiter(20, 60)
_DEVICE_LIMIT = RateLimiter(10, 60)


def _rate_limit(request: Request, limiter: RateLimiter, action: str):
    if not limiter.allow(f"{action}:{client_key(request)}"):
        raise HTTPException(429, "too many requests — wait a minute and try again")


def _display_name(value) -> str:
    return (str(value or "Player").strip() or "Player")[:40]


def _session_from_request(request: Request):
    """Session from the browser cookie OR the X-HL-Session header (desktop app)."""
    return (auth.session_from_cookies(request.cookies)
            or auth.session_from_token(request.headers.get("x-hl-session", "")))


def _session_from_ws(ws, hello: dict):
    """Session from the WS cookie (browser) OR the hello `session` field (app)."""
    return (auth.session_from_cookies(ws.cookies)
            or auth.session_from_token(hello.get("session", "")))


# pairing store for the desktop app's browser login: pair_id -> {token, exp}
_PAIRINGS = {}
_PAIR_TTL = 600


def _pairings_gc():
    now = time.time()
    for pid in [p for p, v in _PAIRINGS.items() if v["exp"] < now]:
        _PAIRINGS.pop(pid, None)


def _prune_rooms():
    codes = db.prune_rooms(ROOM_TTL_DAYS * 86400)
    for c in codes:                      # also drop from the live hub
        hub.rooms.pop(c, None)
        hub.agents.pop(c, None)
        hub.uis.pop(c, None)
        hub.admin_uis.pop(c, None)
        hub.apply_failures.pop(c, None)
    if codes:
        print(f"[cleanup] pruned {len(codes)} idle room(s): {', '.join(codes)}")


@app.on_event("startup")
async def _startup():
    _prune_rooms()
    asyncio.create_task(_prune_loop())
    asyncio.create_task(_shuffle_loop())


async def _shuffle_loop():
    while True:
        await asyncio.sleep(1)
        try:
            await hub.tick_shuffles()
        except Exception:
            logger.exception("time-based rule loop failed")


async def _prune_loop():
    while True:
        await asyncio.sleep(12 * 3600)   # twice a day
        try:
            _prune_rooms()
        except Exception:
            logger.exception("room prune failed")


# ── Discord login ───────────────────────────────────────────────────────────
@app.get("/auth/login")
def auth_login(pair: str = ""):
    if not auth.LOGIN_ENABLED:
        raise HTTPException(404, "discord login not configured")
    # `pair` (set by the desktop app) is carried through the signed OAuth state
    return RedirectResponse(auth.authorize_url(auth.new_state(pair=pair)))


@app.get("/auth/callback")
async def auth_callback(code: str = "", state: str = ""):
    if not auth.LOGIN_ENABLED:
        raise HTTPException(404)
    st = auth.unsign(state)
    if not code or not st:                          # CSRF / bad request
        return RedirectResponse("/?login=error")
    try:
        prof = await asyncio.to_thread(auth.fetch_profile, code)
    except Exception as e:
        print(f"[auth] discord login failed: {e}")
        return RedirectResponse("/?login=error")
    sess = auth.sign({"uid": prof["id"], "name": prof["name"], "avatar": prof["avatar"],
                      "admin": prof["is_admin"], "exp": time.time() + auth.SESSION_TTL})
    pair = st.get("pair")
    if pair:                                        # desktop app login — hand the
        _pairings_gc()                              # token to the polling app
        _PAIRINGS[pair] = {"token": sess, "exp": time.time() + _PAIR_TTL}
        return HTMLResponse("<!doctype html><meta charset=utf-8>"
            "<body style='font-family:system-ui;background:#070709;color:#e8e6f0;"
            "text-align:center;padding-top:18vh'><h2>✓ Logged in</h2>"
            "<p>You can close this tab and return to HyruleLink.</p></body>")
    resp = RedirectResponse("/")
    resp.set_cookie(auth.SESSION_COOKIE, sess, max_age=auth.SESSION_TTL,
                    httponly=True, secure=True, samesite="lax")
    return resp


@app.post("/auth/logout")
def auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


# desktop-app login: start a pairing, then poll for the session token
@app.post("/auth/device/start")
def device_start(request: Request):
    if not auth.LOGIN_ENABLED:
        raise HTTPException(404, "discord login not configured")
    _rate_limit(request, _DEVICE_LIMIT, "device-login")
    _pairings_gc()
    pair = auth.new_pairing_id()
    _PAIRINGS[pair] = {"token": None, "exp": time.time() + _PAIR_TTL}
    base = str(request.base_url).rstrip("/")
    return {"pair": pair, "login_url": f"{base}/auth/login?pair={pair}"}


@app.get("/auth/device/poll")
def device_poll(pair: str = ""):
    _pairings_gc()
    entry = _PAIRINGS.get(pair)
    if not entry:
        return {"status": "expired"}
    if not entry["token"]:
        return {"status": "pending"}
    token = _PAIRINGS.pop(pair)["token"]            # one-time
    sess = auth.unsign(token) or {}
    return {"status": "ok", "token": token, "name": sess.get("name"),
            "avatar": sess.get("avatar"), "admin": bool(sess.get("admin"))}


@app.get("/api/me")
def api_me(request: Request):
    sess = _session_from_request(request)
    if not sess:
        return {"logged_in": False, "login_enabled": auth.LOGIN_ENABLED}
    return {"logged_in": True, "login_enabled": auth.LOGIN_ENABLED,
            "name": sess.get("name"), "avatar": sess.get("avatar"),
            "admin": bool(sess.get("admin"))}


# ── room REST (no accounts — name + room code only) ─────────────────────────
@app.get("/api/rooms")
def list_rooms():
    """Public list of live rooms for the 'watch' picker on the home page."""
    return {"rooms": db.list_rooms(), "login_enabled": auth.LOGIN_ENABLED}


@app.post("/api/rooms/{handle}/delete")
async def delete_room(handle: str, request: Request):
    if not (_session_from_request(request) or {}).get("admin"):
        raise HTTPException(403, "admin only — log in with a mod Discord account")
    # admins act on the public handle (that's all the list exposes), but accept a
    # raw code too for convenience.
    row = db.get_room_by_pub(handle) or db.get_room(handle.upper())
    if row:
        code = row["code"]
        await hub.drop_room(code)    # close live connections + forget in memory
        db.delete_room(code)         # then remove persisted rows
    return {"ok": True}


def _identity(request: Request):
    """(discord_id, avatar) for the caller, or (None, None) if not logged in."""
    sess = _session_from_request(request)
    return (sess.get("uid"), sess.get("avatar")) if sess else (None, None)


@app.post("/api/rooms")
def create_room(request: Request, payload: dict = Body(...)):
    _rate_limit(request, _CREATE_LIMIT, "create-room")
    # silly auto-name unless the client supplied a real one (the host can rename)
    name = (str(payload.get("name") or "").strip() or names.random_room_name())[:60]
    try:
        cooldown = float(payload.get("cooldown_s", 5))
    except (TypeError, ValueError):
        raise HTTPException(422, "cooldown_s must be a number")
    if not math.isfinite(cooldown):
        raise HTTPException(422, "cooldown_s must be finite")
    cooldown = max(0.0, min(3600.0, cooldown))
    display = _display_name(payload.get("display_name"))
    discord_id, avatar = _identity(request)
    code = db.create_room(name, cooldown)
    player_id, token = db.add_player(code, display, discord_id, avatar)
    db.set_host(code, player_id)        # creator is the host/admin
    return _room_payload(code, player_id, token)


@app.post("/api/rooms/{code}/join")
def join_room(code: str, request: Request, payload: dict = Body(...)):
    _rate_limit(request, _JOIN_LIMIT, "join-room")
    code = code.upper()
    if not db.get_room(code):
        raise HTTPException(404, "no such room")
    display = _display_name(payload.get("display_name"))
    # a logged-in user who's already in this room rejoins their existing player
    # (keeps items) instead of piling up duplicates.
    discord_id, avatar = _identity(request)
    if discord_id:
        existing = db.get_player_by_discord(code, discord_id)
        if existing:
            db.set_player_identity(existing["id"], display, avatar)   # freshen name/avatar
            db.touch_room(code); hub.refresh_names(code)
            return _room_payload(code, existing["id"], existing["player_token"])
    player_id, token = db.add_player(code, display, discord_id, avatar)
    db.touch_room(code)
    hub.refresh_names(code)
    return _room_payload(code, player_id, token)


@app.get("/api/my-rooms")
def my_rooms(request: Request):
    """Rooms the logged-in Discord user has a player in (their rejoin list)."""
    discord_id, _ = _identity(request)
    if not discord_id:
        return {"rooms": []}
    return {"rooms": db.rooms_for_discord(discord_id)}


@app.post("/api/rooms/{code}/rejoin")
def rejoin_room(code: str, request: Request):
    """Re-enter a room as your existing Discord-linked player (any device)."""
    discord_id, avatar = _identity(request)
    if not discord_id:
        raise HTTPException(401, "log in with Discord first")
    code = code.upper()
    if not db.get_room(code):
        raise HTTPException(404, "no such room")
    p = db.get_player_by_discord(code, discord_id)
    if not p:
        raise HTTPException(404, "you're not in that room")
    db.set_player_identity(p["id"], p["display_name"], avatar)        # freshen avatar
    db.touch_room(code); hub.refresh_names(code)
    return _room_payload(code, p["id"], p["player_token"])


@app.post("/api/rooms/{code}/resume")
def resume_room(code: str, request: Request, payload: dict = Body(...)):
    """Re-enter a room as an EXISTING player (keeps owned items) instead of
    creating a duplicate. The client passes back its saved player_id+token."""
    _rate_limit(request, _JOIN_LIMIT, "resume-room")
    code = code.upper()
    if not db.get_room(code):
        raise HTTPException(404, "no such room")
    pid = int(payload.get("player_id", 0))
    token = payload.get("player_token", "")
    if not db.player_by_token(code, pid, token):
        raise HTTPException(404, "player not found in room")
    db.touch_room(code)
    hub.refresh_names(code)
    return _room_payload(code, pid, token)


def _room_payload(code, player_id, player_token):
    row = db.get_room(code)
    return {
        "code": code,
        "pub_id": row["pub_id"],
        "name": row["name"],
        "cooldown_s": row["cooldown_s"],
        "host": row["host_player_id"],
        "player_id": player_id,
        "player_token": player_token,
        "players": [
            {"id": p["id"], "name": p["display_name"]} for p in db.room_players(code)
        ],
        "items": [{"key": it.key, "name": it.name,
                   "image": item_image(it.key, it.present)} for it in ITEMS],
    }


# ── WebSocket: agents + UIs ────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    code = role = None
    user_id = None
    try:
        hello = await ws.receive_json()
        if hello.get("type") != P.HELLO:
            await ws.send_json({"type": P.REJECT, "reason": "expected hello"})
            await ws.close()
            return
        role = hello.get("role")
        # admin comes from the Discord session — browser cookie or app `session`
        sess = _session_from_ws(ws, hello)
        is_admin = bool(sess and sess.get("admin"))

        if role == P.ROLE_SPECTATOR:
            # watchers address the room by its PUBLIC handle, never the join code
            handle = hello.get("watch") or hello.get("room") or ""
            row = db.get_room_by_pub(handle) or db.get_room(handle.upper())
            code = row["code"] if row else None
            user_id = None
        else:
            code = (hello.get("room") or "").upper()

        room = hub.get_room(code) if code else None
        if room is None:
            await ws.send_json({"type": P.REJECT, "reason": "room not found"})
            await ws.close()
            return

        if role != P.ROLE_SPECTATOR:
            user_id = int(hello.get("player_id", 0))
            if not db.player_by_token(code, user_id, hello.get("token", "")):
                await ws.send_json({"type": P.REJECT, "reason": "bad room/player token"})
                await ws.close()
                return
        db.touch_room(code)
        hub.refresh_names(code)

        if role == P.ROLE_AGENT:
            await _serve_agent(ws, code, user_id)
        else:
            await _serve_ui(ws, code, user_id, is_admin)
    except WebSocketDisconnect:
        pass
    finally:
        if code is not None:
            if role == P.ROLE_AGENT and user_id is not None:
                hub.unregister_agent(code, user_id, ws)
                if code in hub.rooms:
                    await hub.broadcast_state(code)  # show agent offline
            elif role != P.ROLE_AGENT:
                hub.unregister_ui(code, ws)


async def _push_ownership(ws, code, user_id):
    """(Re)send this player's full ownership so their game matches the ledger —
    used on agent connect AND on a resync request (e.g. after an emulator crash
    + save reload)."""
    room = hub.rooms[code]
    # Send every catalog item. This is intentionally exhaustive: an item that
    # is unowned (or absent from the ledger) must be removed from a stale save.
    for command in ownership_commands(room, user_id):
        await ws.send_json(command)


async def _serve_agent(ws, code, user_id):
    hub.register_agent(code, user_id, ws)
    room = hub.rooms[code]
    await _push_ownership(ws, code, user_id)
    await hub.broadcast_state(code)  # show this player's agent as online
    while True:
        msg = await ws.receive_json()
        mtype = msg.get("type")
        if mtype == P.PICKUP:
            eff = resolve_pickup(room, user_id, msg.get("item"), msg.get("level", 1))
            if eff.reject:
                await ws.send_json({"type": P.REJECT, "reason": eff.reject})
            else:
                await hub.dispatch(code, eff, msg.get("item"))
        elif mtype == P.RESYNC:
            await _push_ownership(ws, code, user_id)
        elif mtype == P.STATUS:
            hub.set_emu_status(code, user_id, msg.get("emu", False))
            await hub.broadcast_state(code)
        elif mtype == P.APPLIED:
            await hub.report_applied(code, user_id, msg)
        elif mtype == P.BYE:
            break


async def _serve_ui(ws, code, user_id, is_admin=False):
    hub.register_ui(code, user_id, ws, is_admin=is_admin)
    payload = hub.serialize(code)
    payload["you"] = user_id
    payload["admin"] = is_admin
    payload["spectator"] = user_id is None and not is_admin
    # include the catalog so watchers can render the full grid without joining
    payload["items"] = [{"key": it.key, "name": it.name,
                         "image": item_image(it.key, it.present)} for it in ITEMS]
    await ws.send_json(payload)
    while True:
        msg = await ws.receive_json()
        mtype = msg.get("type")
        if code not in hub.rooms:
            await ws.send_json({"type": P.REJECT, "reason": "room closed"})
            return
        if mtype == P.CLAIM:
            if user_id is None:        # spectators/admins have no player to claim with
                await ws.send_json({"type": P.REJECT, "reason": "watch-only — join in the app to claim"})
                continue
            room = hub.rooms[code]
            eff = resolve_claim(room, user_id, msg.get("item"))
            if eff.reject:
                await ws.send_json({"type": P.REJECT, "reason": eff.reject})
            else:
                await hub.dispatch(code, eff, msg.get("item"))
        elif mtype in (P.ADMIN_SET_COOLDOWN, P.ADMIN_REMOVE_PLAYER, P.ADMIN_SET_DISCOVERED,
                       P.ADMIN_SET_OWNER, P.ADMIN_SET_MODE, P.ADMIN_SET_RULES, P.ADMIN_SET_NAME):
            if not (is_admin or user_id == hub.rooms[code].host):
                await ws.send_json({"type": P.REJECT, "reason": "host only"})
                continue
            if mtype == P.ADMIN_SET_COOLDOWN:
                await hub.admin_set_cooldown(code, msg.get("seconds", 5))
            elif mtype == P.ADMIN_SET_RULES:
                await hub.admin_set_rules(code, msg.get("rules") or {})
            elif mtype == P.ADMIN_REMOVE_PLAYER:
                await hub.admin_remove_player(code, int(msg.get("player_id")))
            elif mtype == P.ADMIN_SET_DISCOVERED:
                await hub.admin_set_discovered(
                    code, int(msg.get("player_id")), msg.get("item"), bool(msg.get("found")))
            elif mtype == P.ADMIN_SET_OWNER:
                pid = msg.get("player_id")
                await hub.admin_set_owner(
                    code, int(pid) if pid is not None else None, msg.get("item"))
            elif mtype == P.ADMIN_SET_MODE:
                await hub.admin_set_mode(code, msg.get("mode", "normal"), msg.get("seconds"))
            elif mtype == P.ADMIN_SET_NAME:
                await hub.admin_set_name(code, msg.get("name", ""))


# ── static UI ──────────────────────────────────────────────────────────────
@app.get("/")
def index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


@app.get("/health")
def health():
    return {"ok": True, "ts": time.time()}


if os.path.isdir(WEB_DIR):
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
