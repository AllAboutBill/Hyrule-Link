"""
agent.py — the local BombosSwap player agent.

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
import os
import threading
import time

import websocket  # websocket-client

from shared.items import ITEMS, BY_KEY, discovered_level
from shared import protocol as P
from .effects import Effects
from .sni.hud_text import HudText
from .sni.memory_constants import MEMORY_ADDRESSES, OUT_OF_GAME_MODES, PLAYABLE_MODES
from .sni.item_effects import ABILITY_ADDR, RUN_ABILITY_MASK

logger = logging.getLogger("HyruleAgent")

# BillognaBot's HUD text endpoint (see HyruleAgent._hud_show). Empty = never
# forward, always draw the strip ourselves.
BOT_HUD_URL = (os.environ.get("HYRULELINK_BOT_HUD", "http://127.0.0.1:5000/api/hud/say")
               if os.environ.get("HYRULELINK_BOT_HUD", "1") not in ("0", "", "off", "no")
               else "")
if BOT_HUD_URL and not BOT_HUD_URL.startswith("http"):
    BOT_HUD_URL = "http://127.0.0.1:5000/api/hud/say"
BOT_HUD_TIMEOUT_S = 1.5     # a bot mid-startup can hold the socket this long
BOT_HUD_RETRY_S = 30.0      # after a refused/failed call, draw locally this long

GAME_MODE_ADDR = MEMORY_ADDRESSES["game_mode"]  # 0x0010

# Items grouped by the WRAM address they live in (bitfields share an address).
_ITEMS_BY_ADDR = {}
for _it in ITEMS:
    _ITEMS_BY_ADDR.setdefault(_it.addr, []).append(_it)
_TRACKED_ADDRS = sorted(_ITEMS_BY_ADDR.keys())
_TRACKED_START = _TRACKED_ADDRS[0]
_TRACKED_SIZE = _TRACKED_ADDRS[-1] - _TRACKED_START + 1


class HyruleAgent:
    def __init__(self, transport, server_ws_url, room, user_id, player_token,
                 poll_interval=1.0, on_notify=None, hud_text=True, bot_hud_url=None):
        self.t = transport
        self.fx = Effects(transport)
        self.url = server_ws_url
        self.room = room
        self.user_id = int(user_id)
        self.token = player_token
        self.poll_interval = poll_interval
        # optional callback(text) so a host app (the desktop GUI) can surface
        # server notifications even when the transport has no OSD (Snes9x-NWA,
        # SNI/hardware — only RetroArch can draw on-screen messages itself).
        self.on_notify = on_notify
        # true in-game messages: rendered on the HUD strip via WRAM writes
        # (works on every transport, no ROM patch — see sni/hud_text.py)
        self.hud = HudText(transport) if hud_text else None
        # BillognaBot's /api/hud/say, or "" to always draw the strip ourselves
        # (see _hud_show). Callers pass BOT_HUD_URL; tests leave it off.
        self._bot_hud_url = bot_hud_url or ""
        # when the bot's endpoint last failed (0 = try it): _hud_show
        self._bot_hud_retry_at = 0.0

        self.ws = None
        self._ws_ready = threading.Event()
        self._stop = threading.Event()

        # echo-cancellation + change detection (shared between ws + poll threads)
        self._lock = threading.Lock()
        self._baseline = {}   # addr -> last raw byte we accept as "known"
        self._expected = {}   # addr -> raw byte we just wrote (suppress once)
        # Grants/revokes that arrived while no save was loaded (title screen /
        # file select). Writing then would land in a stale $7EF000 mirror and be
        # wiped by the next file load, so they wait here until gameplay resumes.
        self._pending = {}    # item key -> (level, enable)
        # True after MODE passes through an out-of-game module (file select,
        # save-quit, loading). Re-entering gameplay from there means the WRAM
        # mirror may be a different/re-loaded save: re-seed and resync.
        self._out_of_game = False
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
        logger.info("• %s", text)          # always visible in the console/app log
        if self.on_notify:
            try:
                self.on_notify(text)       # desktop-app toast (works on any transport)
            except Exception as e:
                logger.debug("notify callback failed: %s", e)
        if self.hud:
            self._hud_show(text)           # in-game HUD strip (drawn by the poll loop)
        show = getattr(self.t, "show_message", None)
        if show and self.t.connected:
            try:
                show(text)                 # emulator OSD (RetroArch only)
            except Exception as e:
                logger.debug("emulator notification failed: %s", e)

    # ----- in-game HUD strip: one writer at a time -------------------------
    # BillognaBot (the streaming bot) writes chat/bits/points/welcome lines to
    # the same 20-cell HUD strip. When it is running on this PC it owns the
    # strip, so our messages go through its queue (POST /api/hud/say) instead
    # of racing it cell-for-cell; when it is not running, or it says it can't
    # draw right now (emulator not connected there), we draw ourselves.
    # Disable with HYRULELINK_BOT_HUD=0 (or a blank URL).

    def _hud_show(self, text):
        if not self._bot_hud_url or self._bot_hud_retry_at > time.time():
            self.hud.show(text)
            return
        # Off the websocket thread: a bot that is starting up can hold the
        # connection for the full timeout and we must not stall grant/revoke.
        threading.Thread(target=self._hud_forward, args=(text,), daemon=True).start()

    def _hud_forward(self, text):
        import urllib.request
        body = json.dumps({"text": text, "source": "hyrulelink"}).encode("utf-8")
        req = urllib.request.Request(self._bot_hud_url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=BOT_HUD_TIMEOUT_S) as resp:
                reply = json.loads(resp.read().decode("utf-8") or "{}")
            if reply.get("ok") and reply.get("shown"):
                return                     # the bot drew it
            logger.debug("bot HUD declined (%s); drawing locally", reply)
        except Exception as e:
            # Not running (connection refused), or not ready: draw locally and
            # don't knock again for a while.
            self._bot_hud_retry_at = time.time() + BOT_HUD_RETRY_S
            logger.debug("bot HUD unreachable (%s); drawing locally", e)
        self.hud.show(text)

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
    def _apply(self, key, level, enable, tries=3):
        if key not in BY_KEY:
            return
        # Only write while a save is actually loaded. Outside a playable module
        # (title screen, file select, loading) the $7EF000 mirror isn't the live
        # save — a write there is wiped by the next file load, leaving the game
        # out of step with the ledger. Defer instead; the poll loop flushes the
        # queue the moment gameplay resumes. (An unreadable mode byte is treated
        # as a transient hiccup and handled by the retry loop below.)
        gm = self.t.read_memory(GAME_MODE_ADDR, size=1)
        if gm and int(gm[0]) not in PLAYABLE_MODES:
            with self._lock:
                self._pending[key] = (level, enable)
            logger.info("Deferred %s of %s — no save loaded (mode 0x%02X)",
                        "grant" if enable else "revoke", key, int(gm[0]))
            return
        with self._lock:
            self._pending.pop(key, None)   # this command supersedes any deferred one
        # Grants/revokes are idempotent (they set absolute state), so retry a few
        # times across a transient emulator hiccup rather than silently dropping
        # the item — a dropped write/read must not leave the player without an item
        # the ledger says they own.
        last = None
        for _ in range(tries):
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
                return
            except Exception as e:
                last = e
        logger.warning("apply %s failed after %d tries: %s", key, tries, last)
        self._send_applied(key, enable, False, str(last))

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
            # Passing through the title/file-select/loading modules means the
            # next playable state may be a different or re-loaded save.
            if gm and int(gm[0]) in OUT_OF_GAME_MODES:
                self._out_of_game = True
            return
        if self._out_of_game:
            self._out_of_game = False
            # A (re)loaded save reverts WRAM to what was last saved: items we
            # revoked reappear and items we granted vanish. Re-seed detection so
            # the reverted bytes aren't reported as fresh pickups, and have the
            # server re-push ownership so the game matches the ledger again.
            self._resync("Save (re)loaded")
        self._flush_pending()
        if self.hud:
            self.hud.tick()   # only while playable — the HUD buffer is live here

        # All inventory bytes occupy one small contiguous SRAM-mirror range.
        # Reading it once cuts RetroArch UDP traffic from ~23 round trips per
        # poll to one, which materially reduces dropped replies and UI flicker.
        snapshot = self.t.read_memory(_TRACKED_START, size=_TRACKED_SIZE)
        if not snapshot or len(snapshot) < _TRACKED_SIZE:
            return

        for addr in _TRACKED_ADDRS:
            raw = int(snapshot[addr - _TRACKED_START])
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

        ability = (int(snapshot[ABILITY_ADDR - _TRACKED_START])
                   if _TRACKED_START <= ABILITY_ADDR < _TRACKED_START + _TRACKED_SIZE else None)
        self._enforce_boots_ability(ability)

    def _enforce_boots_ability(self, ability_byte=None):
        """Keep the dash-ability flag ($7EF379 bit 0x04) in sync with ownership.

        Dashing is gated by this flag, not the boots inventory byte, and ALttP
        can both clear it on transitions (own boots but can't run) and leave it
        set after a lost revoke (run without boots). So every poll: set it if we
        own the boots, clear it if we don't. Writes only when it's actually
        wrong, so it's cheap and self-heals dropped grant/revoke writes."""
        if ability_byte is None:
            data = self.t.read_memory(ABILITY_ADDR, size=1)
            if not data:
                return
            ability_byte = int(data[0])
        have = bool(ability_byte & RUN_ABILITY_MASK)
        if self._boots_owned and not have:
            self.t.write_memory(ABILITY_ADDR, bytes([ability_byte | RUN_ABILITY_MASK]))
        elif not self._boots_owned and have:
            self.t.write_memory(ABILITY_ADDR, bytes([ability_byte & ~RUN_ABILITY_MASK]))

    def _flush_pending(self):
        """Apply grants/revokes that were deferred while no save was loaded."""
        with self._lock:
            pending = list(self._pending.items())
            self._pending.clear()
        for key, (level, enable) in pending:
            self._apply(key, level, enable)

    def _resync(self, reason, wipe_baseline=True):
        """Ask the server to re-push our ownership so the game is reconciled to
        the authoritative ledger. Used when the emulator comes back (crash/
        reload) and when a save is (re)loaded.

        `wipe_baseline` re-seeds pickup detection from current WRAM WITHOUT
        reporting its contents as fresh pickups — correct when the save may
        genuinely have changed underneath us (reload/restart), since a stale
        mirror shouldn't be reported as a find. But a plain transport blip (the
        emulator's command port hiccuped; the same save kept running the whole
        time) must NOT wipe it: the old baseline is still valid, and doing so
        would silently swallow any pickup that happened during the outage —
        the server's next ownership push then has no record of it and revokes
        an item the player legitimately holds."""
        logger.info("%s — %s.", reason,
                    "re-seeding state and requesting resync" if wipe_baseline
                    else "requesting resync")
        if wipe_baseline:
            with self._lock:
                self._baseline.clear()
                self._expected.clear()
            if self.hud:
                self.hud.reset()   # the game rebuilt the HUD; our snapshot is void
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
                    self._resync("Emulator reconnected", wipe_baseline=False)
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
