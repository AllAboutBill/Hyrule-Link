"""
agent.py — the local HyruleLink player agent.

Sits next to one player's emulator (EmuNWA/RetroArch) or FXPak/SD2SNES and:
  * polls WRAM to detect items the player picks up in their own world,
  * reports those pickups to the coordination server,
  * applies the server's grant/revoke commands by writing WRAM,
  * dials OUT to the server over WebSocket (NAT-friendly remote play).

Echo-cancellation: every write we make is recorded as an "expected" byte at that
address so our own poll loop doesn't mistake a grant/revoke for a fresh pickup.
"""

import json
import logging
import threading
import time

import websocket  # websocket-client

from shared.items import ITEMS, BY_KEY, discovered_level
from shared import protocol as P
from .effects import Effects
from .sni.memory_constants import MEMORY_ADDRESSES, PLAYABLE_MODES
from .sni.item_effects import ABILITY_ADDR, RUN_ABILITY_MASK

logger = logging.getLogger("HyruleAgent")

GAME_MODE_ADDR = MEMORY_ADDRESSES["game_mode"]  # 0x0010

# Items grouped by the WRAM address they live in (bitfields share an address).
_ITEMS_BY_ADDR = {}
for _it in ITEMS:
    _ITEMS_BY_ADDR.setdefault(_it.addr, []).append(_it)
_TRACKED_ADDRS = sorted(_ITEMS_BY_ADDR.keys())


class HyruleAgent:
    def __init__(self, transport, server_ws_url, room, user_id, player_token,
                 poll_interval=1.0):
        self.t = transport
        self.fx = Effects(transport)
        self.url = server_ws_url
        self.room = room
        self.user_id = int(user_id)
        self.token = player_token
        self.poll_interval = poll_interval

        self.ws = None
        self._ws_ready = threading.Event()
        self._stop = threading.Event()

        # echo-cancellation + change detection (shared between ws + poll threads)
        self._lock = threading.Lock()
        self._baseline = {}   # addr -> last raw byte we accept as "known"
        self._expected = {}   # addr -> raw byte we just wrote (suppress once)
        # Whether we currently own the boots. ALttP can clear the dash-ability
        # flag ($7EF379 bit 0x04) on screen transitions, so we re-assert it each
        # poll while owned — otherwise "have boots but can't run".
        self._boots_owned = False
        # Track emulator connectivity so we can re-sync after a crash/reload.
        self._emu_was_connected = False

    # ── WebSocket plumbing ─────────────────────────────────────────────────
    def _on_open(self, ws):
        ws.send(json.dumps({
            "type": P.HELLO, "role": P.ROLE_AGENT,
            "room": self.room, "player_id": self.user_id, "token": self.token,
        }))
        self._ws_ready.set()
        self._send_status()  # report current emulator connectivity
        logger.info("Connected to server; agent online.")

    def _send_status(self):
        """Tell the server whether our emulator is currently reachable."""
        if self.ws and self._ws_ready.is_set():
            try:
                self.ws.send(json.dumps({"type": P.STATUS, "emu": self.t.connected}))
            except Exception as e:
                logger.debug("status send failed: %s", e)

    def _on_message(self, ws, message):
        try:
            msg = json.loads(message)
        except Exception:
            return
        mtype = msg.get("type")
        if mtype == P.GRANT:
            self._apply(msg["item"], int(msg.get("level", 1)), enable=True)
        elif mtype == P.REVOKE:
            self._apply(msg["item"], 0, enable=False)
        elif mtype == P.NOTIFY:
            self._show_notification(msg.get("text", ""))
        elif mtype == P.REJECT:
            logger.info("Server rejected: %s", msg.get("reason"))

    def _show_notification(self, text):
        text = " ".join(str(text).split())[:120]
        if not text:
            return
        show = getattr(self.t, "show_message", None)
        if show and self.t.connected:
            try:
                if show(text):
                    logger.info("Emulator notification: %s", text)
            except Exception as e:
                logger.debug("emulator notification failed: %s", e)

    def _on_error(self, ws, err):
        logger.debug("ws error: %s", err)

    def _on_close(self, ws, *a):
        self._ws_ready.clear()
        logger.info("Server connection closed.")

    def _run_ws(self):
        while not self._stop.is_set():
            try:
                self.ws = websocket.WebSocketApp(
                    self.url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self.ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logger.debug("ws loop error: %s", e)
            if not self._stop.is_set():
                time.sleep(3)  # reconnect backoff

    # ── applying server commands ───────────────────────────────────────────
    def _apply(self, key, level, enable):
        if key not in BY_KEY:
            return
        if not self.t.connected:
            self.t.connect()
        try:
            if enable:
                raw = self.fx.enable(key, level)
                logger.info("Granted %s (lvl %s)", key, level)
            else:
                raw = self.fx.disable(key)
                logger.info("Revoked %s", key)
            if key == "boots":
                self._boots_owned = enable
            # Suppress our own write so the poller doesn't re-broadcast it.
            with self._lock:
                self._expected[BY_KEY[key].addr] = raw
            self._send_applied(key, enable, True)
        except Exception as e:
            logger.warning("apply %s failed: %s", key, e)
            self._send_applied(key, enable, False, str(e))

    def _send_applied(self, key, enable, ok, error=None):
        if not (self.ws and self._ws_ready.is_set()):
            return
        payload = {"type": P.APPLIED, "item": key,
                   "action": "grant" if enable else "revoke", "ok": bool(ok)}
        if error:
            payload["error"] = error[:200]
        try:
            self.ws.send(json.dumps(payload))
        except Exception as e:
            logger.debug("apply acknowledgement failed: %s", e)

    # ── pickup detection poll loop ─────────────────────────────────────────
    def _send_pickup(self, key, level):
        if self.ws and self._ws_ready.is_set():
            try:
                self.ws.send(json.dumps({"type": P.PICKUP, "item": key, "level": level}))
                logger.info("Reported pickup: %s lvl %s", key, level)
            except Exception as e:
                logger.debug("send pickup failed: %s", e)

    def _poll_once(self):
        # Only trust the SRAM mirror while actually in a playable game mode;
        # at file-select / transitions the bytes can be stale or zeroed.
        gm = self.t.read_memory(GAME_MODE_ADDR, size=1)
        if not gm or int(gm[0]) not in PLAYABLE_MODES:
            return

        for addr in _TRACKED_ADDRS:
            data = self.t.read_memory(addr, size=1)
            if not data:
                continue
            raw = int(data[0])
            with self._lock:
                old = self._baseline.get(addr)
                exp = self._expected.get(addr)
                if old is None:
                    self._baseline[addr] = raw      # first sight, seed baseline
                    continue
                if exp is not None and raw == exp:
                    self._baseline[addr] = raw       # our own write echoing back
                    del self._expected[addr]
                    continue
                if raw == old:
                    continue
                pickups = []
                for item in _ITEMS_BY_ADDR[addr]:
                    nl = discovered_level(item, raw)
                    ol = discovered_level(item, old)
                    if nl > ol:                      # 0->1, or a progressive bump
                        pickups.append((item.key, nl))
                self._baseline[addr] = raw
            for key, level in pickups:
                self._send_pickup(key, level)

        self._enforce_boots_ability()

    def _enforce_boots_ability(self):
        """Keep the dash-ability flag ($7EF379 bit 0x04) in sync with ownership.

        Dashing is gated by this flag, not the boots inventory byte, and ALttP
        can both clear it on transitions (own boots but can't run) and leave it
        set after a lost revoke (run without boots). So every poll: set it if we
        own the boots, clear it if we don't. Writes only when it's actually
        wrong, so it's cheap and self-heals dropped grant/revoke writes."""
        data = self.t.read_memory(ABILITY_ADDR, size=1)
        if not data:
            return
        have = bool(data[0] & RUN_ABILITY_MASK)
        if self._boots_owned and not have:
            self.t.write_memory(ABILITY_ADDR, bytes([data[0] | RUN_ABILITY_MASK]))
        elif not self._boots_owned and have:
            self.t.write_memory(ABILITY_ADDR, bytes([data[0] & ~RUN_ABILITY_MASK]))

    def _on_emu_reconnect(self):
        """Emulator came back (e.g. crash + save reload). Re-seed detection from
        the reloaded save WITHOUT reporting its contents as fresh pickups, and
        ask the server to re-push our ownership so the game is reconciled to the
        authoritative ledger."""
        logger.info("Emulator reconnected — re-seeding state and requesting resync.")
        with self._lock:
            self._baseline.clear()
            self._expected.clear()
        if self.ws and self._ws_ready.is_set():
            try:
                self.ws.send(json.dumps({"type": P.RESYNC}))
            except Exception as e:
                logger.debug("resync send failed: %s", e)

    def _run_poll(self):
        while not self._stop.is_set():
            try:
                if not self.t.connected:
                    self.t.connect()
                if self.t.connected and not self._emu_was_connected:
                    self._on_emu_reconnect()
                    self._send_status()
                elif not self.t.connected and self._emu_was_connected:
                    self._send_status()  # emulator just went offline
                self._emu_was_connected = self.t.connected
                self._poll_once()
            except Exception as e:
                logger.debug("poll error: %s", e)
            time.sleep(self.poll_interval)

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self):
        connected = self.t.connect()
        if not connected:
            logger.warning("No emulator/device yet — will keep retrying.")
        self._emu_was_connected = connected
        threading.Thread(target=self._run_ws, daemon=True).start()
        threading.Thread(target=self._run_poll, daemon=True).start()

    def stop(self):
        self._stop.set()
        try:
            if self.ws:
                self.ws.send(json.dumps({"type": P.BYE}))
                self.ws.close()
        except Exception:
            pass
        self.t.disconnect()
