"""
app.py — HyruleLink coordination server (FastAPI).

Serves the web UI, the account/room REST API, and the single /ws WebSocket that
both player agents and browsers dial out to. Run remotely (e.g. your droplet)
so every remote player can reach it.

    uvicorn server.app:app --host 0.0.0.0 --port 5019
"""

import asyncio
import os
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header, Body
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from server import db
from server.ledger import hub, resolve_pickup, resolve_claim
from shared import protocol as P
from shared.items import ITEMS

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

app = FastAPI(title="HyruleLink")
db.init()

# Rooms idle longer than this are auto-deleted (with their players/ledger).
ROOM_TTL_DAYS = float(os.environ.get("HYRULELINK_ROOM_TTL_DAYS", "14"))


def _prune_rooms():
    codes = db.prune_rooms(ROOM_TTL_DAYS * 86400)
    for c in codes:                      # also drop from the live hub
        hub.rooms.pop(c, None)
        hub.agents.pop(c, None)
        hub.uis.pop(c, None)
    if codes:
        print(f"[cleanup] pruned {len(codes)} idle room(s): {', '.join(codes)}")


@app.on_event("startup")
async def _startup():
    _prune_rooms()
    asyncio.create_task(_prune_loop())


async def _prune_loop():
    while True:
        await asyncio.sleep(12 * 3600)   # twice a day
        try:
            _prune_rooms()
        except Exception:
            pass


# ── room REST (no accounts — name + room code only) ─────────────────────────
@app.post("/api/rooms")
def create_room(payload: dict = Body(...)):
    name = (payload.get("name") or "Co-op").strip()
    cooldown = float(payload.get("cooldown_s", 5))
    display = (payload.get("display_name") or "Player").strip()
    code = db.create_room(name, cooldown)
    player_id, token = db.add_player(code, display)
    db.set_host(code, player_id)        # creator is the host/admin
    return _room_payload(code, player_id, token)


@app.post("/api/rooms/{code}/join")
def join_room(code: str, payload: dict = Body(...)):
    code = code.upper()
    if not db.get_room(code):
        raise HTTPException(404, "no such room")
    display = (payload.get("display_name") or "Player").strip()
    player_id, token = db.add_player(code, display)
    db.touch_room(code)
    hub.refresh_names(code)
    return _room_payload(code, player_id, token)


@app.post("/api/rooms/{code}/resume")
def resume_room(code: str, payload: dict = Body(...)):
    """Re-enter a room as an EXISTING player (keeps owned items) instead of
    creating a duplicate. The client passes back its saved player_id+token."""
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
        "name": row["name"],
        "cooldown_s": row["cooldown_s"],
        "host": row["host_player_id"],
        "player_id": player_id,
        "player_token": player_token,
        "players": [
            {"id": p["id"], "name": p["display_name"]} for p in db.room_players(code)
        ],
        "items": [{"key": it.key, "name": it.name} for it in ITEMS],
    }


# ── WebSocket: agents + UIs ────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    code = user_id = role = None
    try:
        hello = await ws.receive_json()
        if hello.get("type") != P.HELLO:
            await ws.send_json({"type": P.REJECT, "reason": "expected hello"})
            await ws.close()
            return
        code = (hello.get("room") or "").upper()
        user_id = int(hello.get("player_id", 0))
        token = hello.get("token", "")
        role = hello.get("role")

        if not db.player_by_token(code, user_id, token):
            await ws.send_json({"type": P.REJECT, "reason": "bad room/player token"})
            await ws.close()
            return
        room = hub.get_room(code)
        if room is None:
            await ws.send_json({"type": P.REJECT, "reason": "room not found"})
            await ws.close()
            return
        db.touch_room(code)
        hub.refresh_names(code)

        if role == P.ROLE_AGENT:
            await _serve_agent(ws, code, user_id)
        else:
            await _serve_ui(ws, code, user_id)
    except WebSocketDisconnect:
        pass
    finally:
        if code is not None and user_id is not None:
            if role == P.ROLE_AGENT:
                hub.unregister_agent(code, user_id, ws)
                if code in hub.rooms:
                    await hub.broadcast_state(code)  # show agent offline
            else:
                hub.unregister_ui(code, ws)


async def _push_ownership(ws, code, user_id):
    """(Re)send this player's full ownership so their game matches the ledger —
    used on agent connect AND on a resync request (e.g. after an emulator crash
    + save reload)."""
    room = hub.rooms[code]
    for key, it in room.items.items():
        if it.owner == user_id and it.level > 0:
            await ws.send_json({"type": P.GRANT, "item": key, "level": it.level})
        elif it.owner not in (None, user_id):
            await ws.send_json({"type": P.REVOKE, "item": key})


async def _serve_agent(ws, code, user_id):
    hub.register_agent(code, user_id, ws)
    room = hub.rooms[code]
    await _push_ownership(ws, code, user_id)
    await hub.broadcast_state(code)  # show this player's agent as online
    while True:
        msg = await ws.receive_json()
        mtype = msg.get("type")
        if mtype == P.PICKUP:
            eff = resolve_pickup(room, user_id, msg.get("item"), int(msg.get("level", 1)))
            if eff.reject:
                await ws.send_json({"type": P.REJECT, "reason": eff.reject})
            else:
                await hub.dispatch(code, eff, msg.get("item"))
        elif mtype == P.RESYNC:
            await _push_ownership(ws, code, user_id)
        elif mtype == P.STATUS:
            hub.set_emu_status(code, user_id, msg.get("emu", False))
            await hub.broadcast_state(code)
        elif mtype == P.BYE:
            break
        # APPLIED acks are informational; ignored for now.


async def _serve_ui(ws, code, user_id):
    hub.register_ui(code, user_id, ws)
    payload = hub.serialize(code)
    payload["you"] = user_id
    await ws.send_json(payload)
    while True:
        msg = await ws.receive_json()
        mtype = msg.get("type")
        if mtype == P.CLAIM:
            room = hub.rooms[code]
            eff = resolve_claim(room, user_id, msg.get("item"))
            if eff.reject:
                await ws.send_json({"type": P.REJECT, "reason": eff.reject})
            else:
                await hub.dispatch(code, eff, msg.get("item"))
        elif mtype in (P.ADMIN_SET_COOLDOWN, P.ADMIN_REMOVE_PLAYER,
                       P.ADMIN_SET_DISCOVERED, P.ADMIN_SET_OWNER):
            if user_id != hub.rooms[code].host:
                await ws.send_json({"type": P.REJECT, "reason": "host only"})
                continue
            if mtype == P.ADMIN_SET_COOLDOWN:
                await hub.admin_set_cooldown(code, msg.get("seconds", 5))
            elif mtype == P.ADMIN_REMOVE_PLAYER:
                await hub.admin_remove_player(code, int(msg.get("player_id")))
            elif mtype == P.ADMIN_SET_DISCOVERED:
                await hub.admin_set_discovered(
                    code, int(msg.get("player_id")), msg.get("item"), bool(msg.get("found")))
            elif mtype == P.ADMIN_SET_OWNER:
                pid = msg.get("player_id")
                await hub.admin_set_owner(
                    code, int(pid) if pid is not None else None, msg.get("item"))


# ── static UI ──────────────────────────────────────────────────────────────
@app.get("/")
def index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


@app.get("/health")
def health():
    return {"ok": True, "ts": time.time()}


if os.path.isdir(WEB_DIR):
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
