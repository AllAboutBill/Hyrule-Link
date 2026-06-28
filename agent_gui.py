#!/usr/bin/env python3
"""
HyruleLink — Player app (all-in-one, no login).

Type a name, Host or Join a room by code, and play — the game board lives right
inside this window (always connected), and one button links your emulator. No
accounts, no config files, no separate browser needed (the web page remains as
an optional spectator/second-screen view).

Run via "Play.cmd", or:  pythonw agent_gui.py
"""
import copy
import json
import os
import queue
import random
import socket
import subprocess
import sys
import threading
import time
import logging
import urllib.request
import urllib.error
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import websocket  # websocket-client

try:                                  # optional: item sprites + Discord avatars
    from PIL import Image, ImageTk, ImageOps, ImageDraw
except Exception:                     # app still runs without icons
    Image = ImageTk = ImageOps = ImageDraw = None

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from shared.items import item_image, ITEMS, BY_KEY   # local catalog (don't trust server's list)

SETTINGS = os.path.join(HERE, "agent", "gui_settings.json")
PUBLIC_SERVER = "https://hyrulelink.billogna.lol"   # default shared server
SNI_DIR = os.path.join(HERE, "tools", "sni")        # bundled SNI bridge (MIT)
ITEMS_DIR = os.path.join(HERE, "web", "items")      # item sprite PNGs (shared w/ web)
ICON_PX = 30                                        # board item sprite size
CARD_MIN_PX = 132                                   # min item-card width → column count

# Open-mode ALTTPR settings for in-app seed generation (assured sword = quick start).
OPEN_SEED_SETTINGS = {
    "glitches": "none", "item_placement": "advanced", "dungeon_items": "standard",
    "accessibility": "items", "goal": "ganon", "crystals": {"ganon": "7", "tower": "7"},
    "mode": "open", "hints": "off", "weapons": "assured",
    "item": {"pool": "normal", "functionality": "normal"},
    "tournament": False, "spoilers": "on", "lang": "en",
    "enemizer": {"boss_shuffle": "none", "enemy_shuffle": "none",
                 "enemy_damage": "default", "enemy_health": "default"},
    "entrances": "none",
}

# palette — billogna.lol's "aurora" design language (see README "Web UI style").
# mint / violet / blue over near-black; no pink. Tk can't render the web's
# translucent "glass" or backdrop blur, so the rgba() card/border tokens are
# flattened to the solid colours they resolve to over the near-black background.
BG = "#070709"; PANEL = "#15131d"; PANEL2 = "#211f2d"; INK = "#e8e6f0"; MUTED = "#8f8da3"
GOLD = "#ffd700"; GREEN = "#b3ffc8"; RED = "#ff6b7a"; BLUE = "#5eadff"; LINE = "#2a2838"
ACCENT = "#b3ffc8"; ACCENT2 = "#8a6bff"   # mint (primary) / violet (secondary) accents
# Note: GREEN==ACCENT (mint) and BLUE map the web's --green/--accent and --cyan
# states; "mine" reads mint, "owned by another" reads blue, "owner/host" stays gold.


def _register_bundled_fonts():
    """Best-effort (Windows): register bundled .ttf fonts so Tk can use them."""
    fdir = os.path.join(HERE, "tools", "fonts")
    if not os.path.isdir(fdir):
        return
    try:
        import ctypes
        for f in os.listdir(fdir):
            if f.lower().endswith(".ttf"):
                ctypes.windll.gdi32.AddFontResourceExW(os.path.join(fdir, f), 0x10, 0)
    except Exception:
        pass


def load_settings():
    try:
        with open(SETTINGS) as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(d):
    try:
        os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
        with open(SETTINGS, "w") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass


def http_post(base, path, payload, headers=None):
    req = urllib.request.Request(base.rstrip("/") + path,
                                 data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("detail", f"error {e.code}")
        except Exception:
            detail = f"error {e.code}"
        return None, detail
    except Exception as e:
        return None, f"can't reach server: {e}"


def http_get(base, path, headers=None):
    req = urllib.request.Request(base.rstrip("/") + path)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode()), None
    except Exception as e:
        return None, str(e)


def detect_emulator():
    """Find whatever SNES memory source is reachable locally.

    Returns (transport, emu_source, label):
      transport "emu"      -> direct EmuConnector (emu_source "nwa"/"retroarch")
      transport "hardware" -> QUsb2Snes/SNI bridge (snes9x-rr, BizHawk, FXPak…)

    Priority:
      1. A QUsb2Snes/SNI bridge that ALREADY has a device attached — join it, so
         we coexist with trackers / crowd-control sharing that bridge instead of
         opening a competing direct connection.
      2. Otherwise pick a direct source: snes9x-nwa preferred, then RetroArch.
      3. A bridge that's running but has no device yet (just a hint).
    """
    # 1. existing bridge that's actively managing a device -> join it
    bridge_hint = None
    for port in (23074, 8080):   # 23074 = usb2snes/SNI default; 8080 legacy
        try:
            w = websocket.create_connection(f"ws://127.0.0.1:{port}", timeout=0.8)
            w.send(json.dumps({"Opcode": "DeviceList", "Space": "SNES"}))
            devices = json.loads(w.recv()).get("Results", [])
            w.close()
            if devices:
                return ("hardware", None, f"QUsb2Snes/SNI — {devices[0]}")
            bridge_hint = "QUsb2Snes/SNI — attach your emulator to it"
            break
        except Exception:
            continue
    # 2. direct sources (only chosen when no shared bridge owns a device);
    #    snes9x-nwa preferred over RetroArch.
    try:
        s = socket.create_connection(("127.0.0.1", 48879), timeout=0.4)
        s.sendall(b"EMULATOR_INFO\n"); s.recv(64); s.close()
        return ("emu", "nwa", "snes9x (EmuNetworkAccess)")
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(0.5)
        s.sendto(b"VERSION\n", ("127.0.0.1", 55355)); s.recvfrom(64); s.close()
        return ("emu", "retroarch", "RetroArch")
    except OSError:
        pass
    # 3. bridge running but idle
    if bridge_hint:
        return ("hardware", None, bridge_hint)
    return ("emu", None, "not detected")


class QueueLogHandler(logging.Handler):
    def __init__(self, q):
        super().__init__(level=logging.INFO)
        self.q = q

    def emit(self, record):
        try:
            self.q.put_nowait(self.format(record))
        except Exception:
            pass


class App(tk.Tk):
    def __init__(self):
        _register_bundled_fonts()
        super().__init__()
        import tkinter.font as tkfont
        fams = tkfont.families()
        # Headings use the web's display face (Unbounded); fall back to the bundled
        # Orbitron, then a system semibold. Body/log prefer DM Mono like the site,
        # falling back to Consolas (always present on Windows). Drop Unbounded.ttf /
        # DMMono.ttf into tools/fonts to match the website exactly.
        self.logo_font = next((f for f in ("Unbounded", "Orbitron") if f in fams),
                              "Segoe UI Semibold")
        self.mono_font = "DM Mono" if "DM Mono" in fams else "Consolas"
        self.title("HyruleLink")
        self.configure(bg=BG)
        # Open wide and as tall as the screen allows, so the multi-column board
        # fits without scrolling (clamped so it still fits smaller displays).
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{min(1180, sw - 80)}x{min(900, sh - 80)}")
        self.minsize(720, 560)
        self._board_cols = 0          # current item-grid column count (responsive)

        self.cfg = load_settings()
        self.base = self.cfg.get("server", PUBLIC_SERVER)
        self.session = self.cfg.get("session")   # signed Discord session token (optional)
        self.me = None              # {name, avatar, admin} when logged in
        self.login_q = queue.Queue()  # device-login results from the poll thread
        self.local_server = None    # subprocess if hosting a server on this PC
        self.sni_proc = None        # bundled SNI bridge, only if WE started it
        self.tunnel_proc = None     # cloudflared subprocess (optional public link)
        self.tunnel_url = None
        self.tunnel_q = queue.Queue()
        self._pub_url_entry = None  # dialog widget that shows the public link
        self.seedgen_q = queue.Queue()  # in-app seed generation status
        self.room = None            # join/create payload
        self.agent = None           # emulator link (HyruleAgent)
        self.transport = None
        self.state = None           # latest ledger state from the ui socket
        self._host_collapsed = bool(self.cfg.get("host_collapsed", False))
        self.log_q = queue.Queue()
        self.state_q = queue.Queue()

        # ui socket (board) — separate from the emulator agent
        self.ui_conn = None
        self.ui_lock = threading.Lock()
        self._ui_stop = threading.Event()

        # emulator detection runs on a background thread (its probes block ~1s);
        # the UI thread only ever reads this cached result, so it never stalls.
        self._detected = ("emu", None, "not detected")
        self._stop_all = threading.Event()

        # animated "aurora" pixel-field backgrounds (faked nexus-bg.js); each is a
        # Canvas registered here and pumped by _animate_fields on the UI thread.
        self._fields = []
        self._closing = False
        self._icon_cache = {}       # (filename, dim) -> PhotoImage (kept alive here)
        self._avatar_cache = {}     # (url, size) -> PhotoImage | None (loading) | False (failed)
        self._avatar_q = queue.Queue()   # worker threads -> UI thread (PIL images)
        self._avatar_version = 0    # bumped when an avatar finishes loading (board sig)
        self._board_sig = None      # signature of the last board render (skip no-op rebuilds)
        self._manage_win = None     # host's per-item found/owner popup (if open)

        self._build_chrome()
        self.show_start()
        self._check_session_async()      # validate a saved Discord login
        threading.Thread(target=self._detect_loop, daemon=True).start()
        self._tick()
        self._animate_fields()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _detect_loop(self):
        while not self._stop_all.is_set():
            if self.room is not None and self.agent is None:
                try:
                    self._detected = detect_emulator()
                except Exception:
                    pass
            self._stop_all.wait(1.5)

    # ── chrome ──────────────────────────────────────────────────────────────
    def _build_chrome(self):
        # Header is a live pixel-field banner — the closest Tk can get to the
        # site's nexus-bg.js field (widgets are opaque, so the field can only sit
        # behind chrome, not show *through* the cards like the web's glass does).
        HEAD_H = 64
        self.header = tk.Canvas(self, height=HEAD_H, bg=BG, highlightthickness=0)
        self.header.pack(fill="x")
        self._attach_pixel_field(self.header, cell=24)
        self.header.create_text(18, HEAD_H // 2 - 1, anchor="w", text="HyruleLink",
                                fill=ACCENT, font=(self.logo_font, 18, "bold"), tags="fg")
        self._who_id = self.header.create_text(0, HEAD_H // 2 - 1, anchor="e", text="",
                                               fill=MUTED, font=("Segoe UI", 9), tags="fg")
        # mint underline echoing the web header's glow
        self._head_rule = self.header.create_rectangle(0, HEAD_H - 2, 0, HEAD_H,
                                                        fill=ACCENT, outline="", tags="fg")

        def _layout_header(e):
            self.header.coords(self._who_id, e.width - 18, HEAD_H // 2 - 1)
            self.header.coords(self._head_rule, 0, HEAD_H - 2, e.width, HEAD_H)
            self.header.tag_raise("fg")
        self.header.bind("<Configure>", _layout_header, add="+")

        self.body = tk.Frame(self, bg=BG); self.body.pack(fill="both", expand=True, padx=16, pady=4)

        bar = tk.Frame(self, bg=PANEL); bar.pack(fill="x", side="bottom")
        self.dot_emu = tk.Canvas(bar, width=12, height=12, bg=PANEL, highlightthickness=0)
        self.dot_emu.pack(side="left", padx=(12, 4), pady=8)
        self.lbl_emu = tk.Label(bar, text="emulator: —", fg=MUTED, bg=PANEL, font=("Segoe UI", 9))
        self.lbl_emu.pack(side="left")
        self.dot_srv = tk.Canvas(bar, width=12, height=12, bg=PANEL, highlightthickness=0)
        self.dot_srv.pack(side="left", padx=(16, 4))
        self.lbl_srv = tk.Label(bar, text="server: —", fg=MUTED, bg=PANEL, font=("Segoe UI", 9))
        self.lbl_srv.pack(side="left")

    def _clear_body(self):
        for w in self.body.winfo_children():
            w.destroy()

    def _entry(self, parent, show=None, value=""):
        e = tk.Entry(parent, show=show, bg=PANEL, fg=INK, insertbackground=INK,
                     relief="flat", font=("Segoe UI", 11))
        e.insert(0, value)
        e.configure(highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT)
        return e

    def _button(self, parent, text, cmd, primary=False, small=False):
        # primary CTAs are filled mint with near-black ink (the web's "lean mint"
        # CTAs); on hover they brighten rather than switch hue. Mint is too light
        # for white text, so primary foreground is the near-black bg colour.
        return tk.Button(parent, text=text, command=cmd, relief="flat", cursor="hand2",
                         bg=ACCENT if primary else PANEL2, fg=BG if primary else INK,
                         activebackground="#c9ffd8" if primary else LINE,
                         activeforeground=BG if primary else INK,
                         font=("Segoe UI Semibold", 9 if small else 11), bd=0,
                         padx=10 if small else 14, pady=4 if small else 8)

    def _label(self, parent, text, **kw):
        return tk.Label(parent, text=text, fg=kw.pop("fg", INK), bg=kw.pop("bg", BG),
                        font=kw.pop("font", ("Segoe UI", 10)), **kw)

    def _dot(self, canvas, color):
        canvas.delete("all"); canvas.create_oval(2, 2, 11, 11, fill=color, outline="")

    # ── Discord login (browser pairing flow) ─────────────────────────────────
    def _auth_headers(self):
        return {"X-HL-Session": self.session} if self.session else {}

    def _check_session_async(self):
        """Validate the stored session against the current server (background)."""
        if not self.session:
            return
        base = self.base
        def work():
            data, _ = http_get(base, "/api/me", self._auth_headers())
            self.login_q.put({"status": "me", "me": data if (data and data.get("logged_in")) else None})
        threading.Thread(target=work, daemon=True).start()

    def _discord_login(self):
        base = (self.e_server.get().strip().rstrip("/") if hasattr(self, "e_server") else self.base)
        self.base = base
        data, err = http_post(base, "/auth/device/start", {})
        if err or not data or "login_url" not in data:
            if hasattr(self, "err"):
                self.err.config(text=err or "Discord login isn't enabled on this server")
            return
        webbrowser.open(data["login_url"])
        if hasattr(self, "err"):
            self.err.config(text="Finish login in your browser, then come back…")
        threading.Thread(target=self._poll_login, args=(base, data["pair"]), daemon=True).start()

    def _poll_login(self, base, pair):
        for _ in range(90):                       # ~3 minutes
            if self._stop_all.wait(2):
                return
            data, _ = http_get(base, f"/auth/device/poll?pair={pair}", {})
            if not data:
                continue
            if data.get("status") in ("ok", "expired"):
                self.login_q.put(data)
                return

    def _discord_logout(self):
        self.session = None
        self.me = None
        self.cfg.pop("session", None)
        save_settings(self.cfg)
        self.show_start()

    def _load_my_rooms_async(self):
        base = self.base
        def work():
            data, _ = http_get(base, "/api/my-rooms", self._auth_headers())
            rooms = (data or {}).get("rooms", [])
            self.after(0, lambda: self._render_my_rooms(rooms))
        threading.Thread(target=work, daemon=True).start()

    def _render_my_rooms(self, rooms):
        fr = getattr(self, "myrooms_frame", None)
        if fr is None or not fr.winfo_exists() or not rooms:
            return
        for w in fr.winfo_children():
            w.destroy()
        box = tk.Frame(fr, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        box.pack(fill="x")
        tk.Label(box, text="Your rooms", fg=INK, bg=PANEL,
                 font=("Segoe UI Semibold", 10)).pack(anchor="w", padx=10, pady=(8, 2))
        for r in rooms[:8]:
            row = tk.Frame(box, bg=PANEL); row.pack(fill="x", padx=10, pady=2)
            star = "★ " if r.get("is_host") else ""
            tk.Label(row, text=f"{star}{r.get('name', 'Co-op')}", fg=INK, bg=PANEL,
                     font=("Segoe UI", 9)).pack(side="left")
            self._button(row, "Rejoin", lambda c=r["code"]: self._rejoin(c), small=True).pack(side="right")
        tk.Frame(box, bg=PANEL, height=6).pack()

    def _rejoin(self, code):
        data, err = http_post(self.base, f"/api/rooms/{code}/rejoin", {},
                              headers=self._auth_headers())
        if err:
            if hasattr(self, "err"):
                self.err.config(text=err)
            return
        self._enter_room(data)

    def _item_icon(self, filename, dim=False):
        """Load + cache an item sprite scaled to ICON_PX (grayscaled/faded when
        `dim`). Returns a Tk image or None if Pillow/the file is unavailable."""
        if Image is None or not filename:
            return None
        ckey = (filename, dim)
        if ckey in self._icon_cache:
            return self._icon_cache[ckey]
        path = os.path.join(ITEMS_DIR, filename)
        try:
            img = Image.open(path).convert("RGBA")
        except Exception:
            self._icon_cache[ckey] = None
            return None
        img.thumbnail((ICON_PX, ICON_PX), Image.LANCZOS)   # keep aspect, fit box
        if dim:                                            # grayscale + faded
            gray = ImageOps.grayscale(img)
            alpha = img.split()[3].point(lambda v: int(v * 0.5))
            img = Image.merge("RGBA", (gray, gray, gray, alpha))
        photo = ImageTk.PhotoImage(img)
        self._icon_cache[ckey] = photo
        return photo

    # ── Discord avatars (downloaded async, round-masked, cached) ──────────────
    # Worker threads only download + PIL-process; the PhotoImage is created on the
    # UI thread in _tick (Tk isn't thread-safe), matching the rest of the app.
    def _avatar_image(self, url, size=18):
        """Round avatar PhotoImage for `url`, or None until it's fetched."""
        if Image is None or not url:
            return None
        key = (url, size)
        if key in self._avatar_cache:          # PhotoImage (done) / None (loading) / False (failed)
            v = self._avatar_cache[key]
            return v if v not in (None, False) else None
        self._avatar_cache[key] = None         # mark in-flight (no duplicate fetch)
        threading.Thread(target=self._fetch_avatar, args=(url, size, key), daemon=True).start()
        return None

    def _fetch_avatar(self, url, size, key):
        import io
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                raw = r.read()
            img = Image.open(io.BytesIO(raw)).convert("RGBA").resize((size, size), Image.LANCZOS)
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
            img.putalpha(mask)
            self._avatar_q.put((key, img))     # hand the PIL image to the UI thread
        except Exception:
            self._avatar_q.put((key, None))

    def _player_avatar_url(self, player_id):
        for p in (self.state or {}).get("players", []):
            if p.get("id") == player_id:
                return p.get("avatar")
        return None

    # ── faked "aurora" pixel-field background (port of web/nexus-bg.js) ───────
    # The site layers a chunky grayscale block grid at opacity .30 / soft-light
    # over the near-black bg. Tk canvases have no per-item alpha, so we bake the
    # alpha+opacity down into an opaque colour composited over BG and draw flat
    # rectangles — visually the same faint steel field, just not see-through.
    _BG_RGB = (0x07, 0x07, 0x09)   # BG = #070709

    def _blend_pixel(self, value, alpha):
        eff = (alpha / 255.0) * 0.30          # web: opacity .30 over the bg
        out = []
        for v, b in zip((value, value, value), self._BG_RGB):
            out.append(max(0, min(255, int(b + (v - b) * eff))))
        return "#%02x%02x%02x" % tuple(out)

    def _new_pixel(self):
        """A block's (value, alpha) using nexus-bg.js's three brightness tiers."""
        base = random.random()
        if base < 0.45:
            return 20 + random.random() * 40, 120     # mostly-dark
        if base < 0.85:
            return 80 + random.random() * 70, 140      # mid steel
        return 180 + random.random() * 40, 160         # occasional bright

    def _attach_pixel_field(self, canvas, cell=24):
        """Fill `canvas` with a chunky pixel grid that rebuilds on resize.
        Registers it for the shared drift animation in _animate_fields."""
        field = {"cell": cell, "cells": [], "cols": 0, "rows": 0}
        canvas._field = field

        def rebuild(_=None):
            w, h = canvas.winfo_width(), canvas.winfo_height()
            if w <= 1 or h <= 1:
                return
            cols, rows = w // cell + 1, h // cell + 1
            if cols == field["cols"] and rows == field["rows"]:
                return
            canvas.delete("pxf")
            field["cols"], field["rows"], field["cells"] = cols, rows, []
            for r in range(rows):
                for c in range(cols):
                    value, alpha = self._new_pixel()
                    rid = canvas.create_rectangle(
                        c * cell, r * cell, (c + 1) * cell, (r + 1) * cell,
                        fill=self._blend_pixel(value, alpha), outline="", tags="pxf")
                    field["cells"].append({"id": rid, "value": value,
                                           "alpha": alpha, "target": None})
            canvas.tag_lower("pxf")     # keep the field behind foreground items
            canvas.tag_raise("fg")

        canvas.bind("<Configure>", rebuild, add="+")
        self._fields.append(canvas)
        rebuild()

    def _animate_fields(self):
        """Slowly drift every registered field — only blocks mid-transition are
        recoloured, so this stays cheap even with a few hundred cells."""
        if self._closing:
            return
        for canvas in self._fields:
            field = getattr(canvas, "_field", None)
            if not field:
                continue
            for p in field["cells"]:
                if p["target"] is None:
                    if random.random() < 0.02:       # retarget a few each tick
                        p["target"], p["alpha"] = self._new_pixel()
                    continue
                p["value"] += (p["target"] - p["value"]) * 0.08
                if abs(p["target"] - p["value"]) < 1.5:
                    p["value"], p["target"] = p["target"], None
                canvas.itemconfig(p["id"], fill=self._blend_pixel(p["value"], p["alpha"]))
        self.after(140, self._animate_fields)

    def _set_who(self, text):
        self.header.itemconfig(self._who_id, text=text)

    # ── step 1: name + host/join ────────────────────────────────────────────
    def show_start(self):
        self._clear_body(); self._set_who("")
        f = self.body
        self._label(f, "Start playing", fg=ACCENT, font=(self.logo_font, 14, "bold")).pack(anchor="w", pady=(6, 10))

        # Discord login row
        drow = tk.Frame(f, bg=BG); drow.pack(fill="x", pady=(0, 8))
        if self.me:
            tag = "  ★ mod" if self.me.get("admin") else ""
            self._label(drow, f"✓ Discord: {self.me.get('name', '')}{tag}", fg=GREEN,
                        font=("Segoe UI Semibold", 10)).pack(side="left")
            self._button(drow, "Logout", self._discord_logout, small=True).pack(side="left", padx=8)
        else:
            self._button(drow, "Login with Discord", self._discord_login, small=True).pack(side="left")
            self._label(drow, "optional — keeps your rooms across devices + mod powers",
                        fg=MUTED, font=("Segoe UI", 8)).pack(side="left", padx=8)

        self._label(f, "Your name", fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w")
        default_name = self.cfg.get("display") or (self.me.get("name") if self.me else "") or ""
        self.e_name = self._entry(f, value=default_name)
        self.e_name.pack(fill="x", pady=(0, 8), ipady=4)

        # Your rooms (logged-in) — filled asynchronously
        self.myrooms_frame = tk.Frame(f, bg=BG)
        if self.me:
            self.myrooms_frame.pack(fill="x", pady=(0, 4))
            self._load_my_rooms_async()

        self._label(f, "Server", fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w")
        self.e_server = self._entry(f, value=self.cfg.get("server", PUBLIC_SERVER))
        self.e_server.pack(fill="x", pady=(0, 2), ipady=4)
        self._label(f, "Default is the public server — leave it to play with anyone. Change it "
                    "only to join someone's local server.", fg=MUTED, font=("Segoe UI", 8),
                    wraplength=560, justify="left").pack(anchor="w", pady=(0, 12))

        # Join / host on the server in the box above
        join = tk.Frame(f, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        join.pack(fill="x", pady=6)
        tk.Label(join, text="Play on this server", fg=INK, bg=PANEL,
                 font=("Segoe UI Semibold", 10)).pack(anchor="w", padx=10, pady=(8, 4))
        coderow = tk.Frame(join, bg=PANEL); coderow.pack(fill="x", padx=10, pady=(0, 8))
        self.e_code = self._entry(coderow, value=self.cfg.get("room", ""))
        self.e_code.pack(side="left", fill="x", expand=True, ipady=3)
        self._button(coderow, "Join", self._join, primary=True).pack(side="left", padx=(6, 0))
        self._button(join, "Host a new room", self._host).pack(anchor="w", padx=10, pady=(0, 10))

        # Host a server on THIS PC (no website needed)
        local = tk.Frame(f, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        local.pack(fill="x", pady=10)
        tk.Label(local, text="Host a server on THIS PC", fg=INK, bg=PANEL,
                 font=("Segoe UI Semibold", 10)).pack(anchor="w", padx=10, pady=(8, 2))
        tk.Label(local, text="Runs the server right here, so nobody needs the public website. "
                 "Best for players on the same network.", fg=MUTED, bg=PANEL,
                 font=("Segoe UI", 8), wraplength=560, justify="left").pack(anchor="w", padx=10)
        lrow = tk.Frame(local, bg=PANEL); lrow.pack(anchor="w", padx=10, pady=(6, 4))
        tk.Label(lrow, text="Port", fg=MUTED, bg=PANEL, font=("Segoe UI", 9)).pack(side="left")
        self.e_port = self._entry(lrow, value=str(self.cfg.get("local_port", 5019)))
        self.e_port.configure(width=6); self.e_port.pack(side="left", padx=(4, 8))
        self._button(lrow, "Start local server & host", self._host_local).pack(side="left")
        self.tunnel_var = tk.BooleanVar(value=self.cfg.get("use_tunnel", False))
        tk.Checkbutton(local, text="Also make it reachable over the internet (free tunnel — "
                       "no port forwarding)", variable=self.tunnel_var, fg=INK, bg=PANEL,
                       selectcolor=PANEL2, activebackground=PANEL, activeforeground=INK,
                       font=("Segoe UI", 9), anchor="w").pack(anchor="w", padx=8, pady=(0, 10))

        self.err = self._label(f, "", fg=RED, font=("Segoe UI", 9)); self.err.pack(anchor="w", pady=8)

    def _host(self):
        self.base = self.e_server.get().strip().rstrip("/")
        data, err = http_post(self.base, "/api/rooms",
                              {"display_name": self.e_name.get().strip() or "Player"},
                              headers=self._auth_headers())   # server auto-names the room
        if err:
            self.err.config(text=err); return
        self._enter_room(data)

    # ── host a local server on this PC ──────────────────────────────────────
    def _lan_ip(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80)); return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            s.close()

    def _start_local_server(self, port):
        if self.local_server and self.local_server.poll() is None:
            return True  # already running
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            # IMPORTANT: redirect stdout/stderr to DEVNULL. The app runs under
            # pythonw (no console), so a child that inherits those handles would
            # crash the instant run_server.py / uvicorn writes a log line.
            self.local_server = subprocess.Popen(
                [sys.executable, os.path.join(HERE, "run_server.py"),
                 "--host", "0.0.0.0", "--port", str(port)],
                cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags)
        except Exception:
            return False
        for _ in range(30):  # wait up to ~15s for it to come up
            if self.local_server.poll() is not None:
                return False  # it exited/crashed already
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                return True
            except Exception:
                time.sleep(0.5)
        return False

    def _host_local(self):
        self.err.config(text="")
        try:
            port = int(self.e_port.get())
        except ValueError:
            self.err.config(text="port must be a number (e.g. 5019)"); return
        self.err.config(text="starting local server…"); self.update_idletasks()
        if not self._start_local_server(port):
            self.err.config(text=f"couldn't start the server on port {port} (is it already in use?)")
            return
        self.cfg["local_port"] = port; save_settings(self.cfg)
        self.base = f"http://localhost:{port}"
        self.e_server.delete(0, "end"); self.e_server.insert(0, self.base)
        data, err = http_post(self.base, "/api/rooms",
                              {"display_name": self.e_name.get().strip() or "Player"},
                              headers=self._auth_headers())
        if err:
            self.err.config(text=err); return
        use_tunnel = self.tunnel_var.get()
        self.cfg["use_tunnel"] = use_tunnel; save_settings(self.cfg)
        self._enter_room(data)
        self._show_local_info(port, use_tunnel)
        if use_tunnel:
            threading.Thread(target=self._run_tunnel, args=(port,), daemon=True).start()

    def _show_local_info(self, port, tunnel=False):
        lan = f"http://{self._lan_ip()}:{port}"
        win = tk.Toplevel(self); win.title("Local server running"); win.configure(bg=BG)
        win.geometry("560x420"); win.transient(self)
        tk.Label(win, text="✓ Your local server is running", fg=GREEN, bg=BG,
                 font=("Segoe UI Semibold", 13)).pack(anchor="w", padx=16, pady=(14, 8))

        def url_row(caption, value, copy_get):
            tk.Label(win, text=caption, fg=INK, bg=BG, font=("Segoe UI", 10)).pack(anchor="w", padx=16, pady=(6, 0))
            row = tk.Frame(win, bg=BG); row.pack(anchor="w", padx=16, pady=(2, 0))
            e = self._entry(row, value=value); e.configure(width=34); e.pack(side="left", ipady=3)
            self._button(row, "Copy", lambda: (self.clipboard_clear(), self.clipboard_append(copy_get())),
                         small=True).pack(side="left", padx=6)
            return e

        url_row("Same network — players enter this Server URL:", lan, lambda: lan)
        if tunnel:
            self._pub_url_entry = url_row(
                "Any network (internet) — players enter this:",
                "preparing… (first run downloads the tunnel, ~20s)",
                lambda: self.tunnel_url or "")

        tk.Label(win, text=f"…then everyone joins with room code  {self.room['code']}.",
                 fg=GOLD, bg=BG, font=("Segoe UI Semibold", 10)).pack(anchor="w", padx=16, pady=(10, 0))
        notes = ("• If Windows asks, click Allow (Private network).\n"
                 f"• Same-network only: make sure port {port} isn't firewall-blocked.\n"
                 "• The server (and tunnel) run while this app is open; closing it stops them.")
        if not tunnel:
            notes += ("\n• Players on another network can't reach this directly — tick the "
                      "internet tunnel box, or use the public server.")
        tk.Label(win, text=notes, fg=MUTED, bg=BG, font=("Segoe UI", 9),
                 justify="left").pack(anchor="w", padx=16, pady=(10, 0))
        self._button(win, "Got it", win.destroy, primary=True).pack(anchor="w", padx=16, pady=12)

    # ── internet tunnel (cloudflared quick tunnel; no account/forwarding) ───
    def _cloudflared_path(self):
        import shutil
        local = os.path.join(HERE, "tools", "cloudflared.exe")
        return local if os.path.exists(local) else shutil.which("cloudflared")

    def _download_cloudflared(self):
        dest_dir = os.path.join(HERE, "tools"); os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "cloudflared.exe")
        url = ("https://github.com/cloudflare/cloudflared/releases/latest/download/"
               "cloudflared-windows-amd64.exe")
        try:
            urllib.request.urlretrieve(url, dest)
            return dest if os.path.exists(dest) else None
        except Exception:
            return None

    def _run_tunnel(self, port):
        import re
        cf = self._cloudflared_path()
        if not cf:
            self.tunnel_q.put(("status", "downloading tunnel (one-time, ~50MB)…"))
            cf = self._download_cloudflared()
            if not cf:
                self.tunnel_q.put(("error", "couldn't download tunnel (check internet)")); return
        self.tunnel_q.put(("status", "starting tunnel…"))
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.tunnel_proc = subprocess.Popen(
                [cf, "tunnel", "--url", f"http://localhost:{port}"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                bufsize=1, creationflags=flags)
        except Exception as e:
            self.tunnel_q.put(("error", str(e))); return
        pat = re.compile(r"https://[-a-z0-9]+\.trycloudflare\.com")
        found = False
        for line in self.tunnel_proc.stdout:        # keep draining so it doesn't block
            if not found:
                m = pat.search(line)
                if m:
                    found = True
                    self.tunnel_q.put(("url", m.group(0)))

    def _join(self):
        self.base = self.e_server.get().strip().rstrip("/")
        code = self.e_code.get().strip().upper()
        if not code:
            self.err.config(text="enter a room code"); return
        # Resume as the same player (keeping our items) if we've joined before.
        saved = self.cfg.get("rooms", {}).get(code)
        if saved and saved.get("player_token"):
            data, err = http_post(self.base, f"/api/rooms/{code}/resume",
                                  {"player_id": saved["player_id"],
                                   "player_token": saved["player_token"]})
            if not err:
                self._enter_room(data); return  # resumed
            # else (server reset / unknown player) fall through to a fresh join
        data, err = http_post(self.base, f"/api/rooms/{code}/join",
                              {"display_name": self.e_name.get().strip() or "Player"},
                              headers=self._auth_headers())
        if err:
            self.err.config(text=err); return
        self._enter_room(data)

    def _enter_room(self, data):
        self.room = data
        # remember our identity in this room so a future launch rejoins as US
        rooms = self.cfg.setdefault("rooms", {})
        rooms[data["code"]] = {"player_id": data["player_id"],
                               "player_token": data["player_token"]}
        self.cfg.update(server=self.base, display=self.e_name.get().strip(), room=data["code"])
        save_settings(self.cfg)
        self._start_ui_socket()
        self.show_room()

    # ── ui socket (the board) ───────────────────────────────────────────────
    def _ws_url(self):
        return self.base.replace("http://", "ws://").replace("https://", "wss://") + "/ws"

    def _start_ui_socket(self):
        self._ui_stop.clear()
        threading.Thread(target=self._ui_loop, daemon=True).start()

    def _ui_loop(self):
        while not self._ui_stop.is_set():
            try:
                conn = websocket.create_connection(self._ws_url(), timeout=6)
                hello = {"type": "hello", "role": "ui", "room": self.room["code"],
                         "player_id": self.room["player_id"], "token": self.room["player_token"]}
                if self.session:
                    hello["session"] = self.session     # carries Discord identity/admin
                conn.send(json.dumps(hello))
                with self.ui_lock:
                    self.ui_conn = conn
                while not self._ui_stop.is_set():
                    msg = json.loads(conn.recv())
                    self.state_q.put(msg)
            except Exception:
                with self.ui_lock:
                    self.ui_conn = None
                if self._ui_stop.is_set():
                    break
                self.state_q.put({"type": "_disconnected"})
                threading.Event().wait(2.5)

    def _ui_send(self, obj):
        with self.ui_lock:
            if self.ui_conn:
                try:
                    self.ui_conn.send(json.dumps(obj))
                except Exception:
                    pass

    # ── step 2: room (board + emulator + controls) ──────────────────────────
    def show_room(self):
        self._clear_body()
        self._set_who(f"{self.cfg.get('display','Player')} · room {self.room['code']}")
        f = self.body

        top = tk.Frame(f, bg=BG); top.pack(fill="x")
        self.room_title_lbl = tk.Label(top, text=self._room_title(), fg=GOLD, bg=BG,
                                        font=(self.logo_font, 15, "bold"))
        self.room_title_lbl.pack(side="left")
        # Rename button sits by the title; shown only to the host (in _render_board)
        self.rename_btn = self._button(top, "✎ Rename", self._rename_room, small=True)
        self._button(top, "Spectator view",
                     lambda: webbrowser.open(
                         f"{self.base}/?watch={self.room.get('pub_id') or self.room['code']}"),
                     small=True).pack(side="right")
        self._button(top, "Leave", self._leave, small=True).pack(side="right", padx=6)

        # emulator + connect row
        emu = tk.Frame(f, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        emu.pack(fill="x", pady=(8, 6))
        self.emu_summary_lbl = tk.Label(emu, text="Emulator: " + self._emu_summary(),
                                        fg=INK, bg=PANEL, font=("Segoe UI", 10))
        self.emu_summary_lbl.pack(anchor="w", padx=10, pady=(8, 2))
        erow = tk.Frame(emu, bg=PANEL); erow.pack(anchor="w", padx=10, pady=(0, 4))
        self.btn_connect = self._button(erow, "Connect & Play", self._toggle_connect, primary=True)
        self.btn_connect.pack(side="left")
        self._button(erow, "Launch emulator", self.launch_emulator).pack(side="left", padx=6)
        self._button(erow, "Generate seed", self.generate_seed).pack(side="left")
        erow2 = tk.Frame(emu, bg=PANEL); erow2.pack(anchor="w", padx=10, pady=(0, 8))
        self._button(erow2, "Start SNI", self._use_sni, small=True).pack(side="left", padx=(0, 6))
        self._button(erow2, "Configure…", self._configure_emulator, small=True).pack(side="left")
        self._button(erow2, "Which emulators?", self._show_emu_help, small=True).pack(side="left", padx=6)
        self.emu_line = tk.Label(emu, text="", fg=MUTED, bg=PANEL, font=("Segoe UI", 9))
        self.emu_line.pack(anchor="w", padx=10, pady=(0, 8))

        # players row
        self.players_row = tk.Frame(f, bg=BG); self.players_row.pack(fill="x", pady=(2, 6))

        # host controls (shown only to host)
        self.host_bar = tk.Frame(f, bg=BG)
        self._build_host_bar()

        # game-mode banner (shown to everyone when a shuffle mode is active)
        self.mode_banner = tk.Label(f, text="", bg=PANEL, fg=GOLD, anchor="w",
                                    font=(self.logo_font, 11, "bold"), padx=10, pady=6)

        # the board (responsive grid: fills width, reflows columns to stay wide)
        self._board_sig = None       # fresh, empty board — force the first render
        self.board_wrap = tk.Frame(f, bg=BG); self.board_wrap.pack(fill="both", expand=True)
        board_wrap = self.board_wrap
        self.canvas = tk.Canvas(board_wrap, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(board_wrap, orient="vertical", command=self.canvas.yview)
        self.board = tk.Frame(self.canvas, bg=BG)
        self.board.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._board_window = self.canvas.create_window((0, 0), window=self.board, anchor="nw")
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta/120), "units"))

        # activity (short)
        self.logbox = tk.Text(f, height=3, bg="#0b0b10", fg=GREEN, relief="flat",
                              font=(self.mono_font, 9), highlightthickness=1, highlightbackground=LINE)
        self.logbox.pack(fill="x", pady=(6, 0)); self.logbox.configure(state="disabled")

        if self.state:
            self._render_board()

    def _build_host_bar(self):
        for w in self.host_bar.winfo_children():
            w.destroy()
        # collapsible header
        hdr = tk.Frame(self.host_bar, bg=BG); hdr.pack(fill="x")
        self._host_toggle = self._button(hdr, "", self._toggle_host_controls, small=True)
        self._host_toggle.pack(side="left")

        # the controls themselves live in a frame we can hide
        self.host_ctrls = tk.Frame(self.host_bar, bg=BG)
        c = self.host_ctrls
        # steal cooldown — only meaningful in Normal (you can claim/steal there)
        self.cd_group = tk.Frame(c, bg=BG)
        tk.Label(self.cd_group, text="cooldown", fg=MUTED, bg=BG,
                 font=("Segoe UI", 9)).pack(side="left", padx=(2, 2))
        self.e_cd = self._entry(self.cd_group); self.e_cd.configure(width=4); self.e_cd.pack(side="left")
        self._button(self.cd_group, "set", self._set_cooldown, small=True).pack(side="left", padx=4)
        self.cd_group.pack(side="left")
        self._mode_label = tk.Label(c, text="· mode", fg=MUTED, bg=BG, font=("Segoe UI", 9))
        self._mode_label.pack(side="left", padx=(10, 2))
        self.mode_var = tk.StringVar(value="normal")
        cb = ttk.Combobox(c, textvariable=self.mode_var, state="readonly", width=11,
                          values=["normal", "hot_potato", "chaos"])
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._sync_mode_fields())
        # shuffle interval — only for the shuffle modes (hot potato / chaos)
        self.shuffle_group = tk.Frame(c, bg=BG)
        tk.Label(self.shuffle_group, text="every", fg=MUTED, bg=BG,
                 font=("Segoe UI", 9)).pack(side="left", padx=(6, 2))
        self.e_shuffle = self._entry(self.shuffle_group); self.e_shuffle.configure(width=4)
        self.e_shuffle.pack(side="left")
        tk.Label(self.shuffle_group, text="s", fg=MUTED, bg=BG, font=("Segoe UI", 9)).pack(side="left")
        self.shuffle_group.pack(side="left")
        self._go_btn = self._button(c, "go", self._set_mode, small=True)
        self._go_btn.pack(side="left", padx=4)
        tk.Label(c, text="· click player=remove · right-click item=manage",
                 fg=MUTED, bg=BG, font=("Segoe UI", 9)).pack(side="left", padx=8)
        self._sync_mode_fields()
        self._apply_host_collapse()

    def _sync_mode_fields(self):
        """Show 'steal cooldown' only in Normal and the 'shuffle every…' fields
        only in the shuffle modes — they don't both apply at once."""
        if not hasattr(self, "cd_group"):
            return
        if self.mode_var.get() == "normal":
            self.shuffle_group.pack_forget()
            self.cd_group.pack(side="left", before=self._mode_label)
        else:
            self.cd_group.pack_forget()
            self.shuffle_group.pack(side="left", before=self._go_btn)

    def _toggle_host_controls(self):
        self._host_collapsed = not self._host_collapsed
        self.cfg["host_collapsed"] = self._host_collapsed
        save_settings(self.cfg)
        self._apply_host_collapse()

    def _apply_host_collapse(self):
        if not hasattr(self, "host_ctrls"):
            return
        if self._host_collapsed:
            self.host_ctrls.pack_forget()
            self._host_toggle.config(text="▸ ★ Host controls")
        else:
            self.host_ctrls.pack(fill="x", pady=(3, 0))
            self._host_toggle.config(text="▾ ★ Host controls")

    def _room_title(self):
        name = (self.state or {}).get("name") or self.room.get("name") or "Co-op"
        return f"{name}  ·  {self.room['code']}"

    def _rename_room(self):
        from tkinter import simpledialog
        cur = (self.state or {}).get("name") or self.room.get("name", "")
        new = simpledialog.askstring("Rename room", "New room name:",
                                     initialvalue=cur, parent=self)
        if new and new.strip():
            self._ui_send({"type": "admin_set_name", "name": new.strip()})

    def _is_host(self):
        return bool(self.state) and self.state.get("you") == self.state.get("host")

    def _set_cooldown(self):
        try:
            self._ui_send({"type": "admin_set_cooldown", "seconds": float(self.e_cd.get())})
        except ValueError:
            pass

    def _set_mode(self):
        try:
            secs = float(self.e_shuffle.get() or 120)
        except ValueError:
            secs = 120
        self._ui_send({"type": "admin_set_mode", "mode": self.mode_var.get(), "seconds": secs})

    @staticmethod
    def _clock(s):
        s = max(0, int(round(s)))
        return f"{s // 60}:{s % 60:02d}"

    def _update_mode_banner(self):
        if not hasattr(self, "mode_banner"):
            return
        mode = (self.state or {}).get("mode", "normal")
        if not self.state or mode == "normal":
            self.mode_banner.pack_forget()
            return
        every = self._clock(self.state.get("shuffle_s", 120))
        if mode == "chaos":
            rem = self._clock(self.state.get("shuffle_remaining", 0))
            self.mode_banner.config(
                text=f"🌀 Chaos — everything reshuffles every {every} · next in {rem}",
                fg=ACCENT2, highlightbackground=ACCENT2)
        else:
            self.mode_banner.config(
                text=f"🔥 Hot Potato — each item passes to the next online finder every {every}. No claiming.",
                fg=GOLD, highlightbackground=GOLD)
        self.mode_banner.pack(fill="x", pady=(0, 6), before=self.board_wrap)

    # ── board rendering ─────────────────────────────────────────────────────
    def _render_players(self):
        for w in self.players_row.winfo_children():
            w.destroy()
        host = self._is_host()
        for p in self.state.get("players", []):
            color = GREEN if (p.get("agent") and p.get("emu")) else (
                GOLD if p.get("agent") else "#555")
            chip = tk.Frame(self.players_row, bg=BG); chip.pack(side="left", padx=(0, 10))
            c = tk.Canvas(chip, width=12, height=12, bg=BG, highlightthickness=0); c.pack(side="left")
            self._dot(c, color)
            av = self._avatar_image(p.get("avatar"), 18)
            if av is not None:
                al = tk.Label(chip, image=av, bg=BG); al.image = av
                al.pack(side="left", padx=(3, 3))
            star = "★ " if p["id"] == self.state.get("host") else ""
            you = " (you)" if p["id"] == self.state.get("you") else ""
            lbl = tk.Label(chip, text=f"{star}{p['name']}{you}", fg=INK, bg=BG, font=("Segoe UI", 9))
            lbl.pack(side="left")
            if host and p["id"] != self.state.get("host"):
                lbl.configure(cursor="hand2")
                lbl.bind("<Button-1>", lambda e, pid=p["id"], nm=p["name"]: self._remove_player(pid, nm))

    def _remove_player(self, pid, name):
        if messagebox.askyesno("HyruleLink", f"Remove {name} from the room?"):
            self._ui_send({"type": "admin_remove_player", "player_id": pid})

    # ── host: per-item found/owner management (right-click an item) ───────────
    def _bind_recursive(self, widget, sequence, func):
        widget.bind(sequence, func)
        for ch in widget.winfo_children():
            self._bind_recursive(ch, sequence, func)

    def _manage_item(self, key, name):
        """Host-only popup to fix who has 'found' an item and who owns it — mirrors
        the web's per-item chips for when the app is the only screen open."""
        if not self._is_host():
            return
        win = getattr(self, "_manage_win", None)
        if win is not None and win.winfo_exists():
            win.destroy()
        win = tk.Toplevel(self); win.title(f"Manage — {name}"); win.configure(bg=BG)
        win.transient(self); win.geometry("380x430")
        self._manage_win = win; self._manage_key = key
        tk.Label(win, text=name, fg=GOLD, bg=BG,
                 font=(self.logo_font, 13, "bold")).pack(anchor="w", padx=14, pady=(12, 2))
        tk.Label(win, text="Found = they've discovered it (so they can Claim it). "
                 "Owner = who holds it right now. Use this to fix a player's state after "
                 "a disconnect.", fg=MUTED, bg=BG, font=("Segoe UI", 8),
                 wraplength=350, justify="left").pack(anchor="w", padx=14, pady=(0, 8))
        self._manage_body = tk.Frame(win, bg=BG)
        self._manage_body.pack(fill="both", expand=True, padx=8)
        self._button(win, "Close", win.destroy, primary=True).pack(anchor="e", padx=14, pady=10)
        win.protocol("WM_DELETE_WINDOW", lambda: (setattr(self, "_manage_win", None), win.destroy()))
        self._refresh_manage()

    def _refresh_manage(self):
        win = getattr(self, "_manage_win", None)
        if win is None or not win.winfo_exists():
            self._manage_win = None
            return
        body = self._manage_body
        for w in body.winfo_children():
            w.destroy()
        key = self._manage_key
        it = (self.state or {}).get("ledger", {}).get(key, {})
        discovered = set(it.get("discovered", []))
        owner = it.get("owner")
        players = (self.state or {}).get("players", [])
        if not players:
            tk.Label(body, text="No players in the room yet.", fg=MUTED, bg=BG,
                     font=("Segoe UI", 9)).pack(anchor="w", padx=8, pady=6)
        for p in players:
            row = tk.Frame(body, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
            row.pack(fill="x", padx=6, pady=3)
            var = tk.BooleanVar(value=p["id"] in discovered)
            tk.Checkbutton(
                row, text="found", variable=var,
                command=lambda pid=p["id"], v=var: self._ui_send(
                    {"type": "admin_set_discovered", "player_id": pid, "item": key, "found": v.get()}),
                fg=INK, bg=PANEL, selectcolor=PANEL2, activebackground=PANEL,
                activeforeground=INK, font=("Segoe UI", 9)).pack(side="left", padx=(6, 4), pady=4)
            star = "★ " if p["id"] == self.state.get("host") else ""
            tk.Label(row, text=f"{star}{p['name']}", fg=INK, bg=PANEL,
                     font=("Segoe UI", 10)).pack(side="left")
            is_owner = (owner == p["id"])
            self._button(
                row, "● owner" if is_owner else "make owner",
                lambda pid=p["id"], cur=is_owner: self._ui_send(
                    {"type": "admin_set_owner", "item": key,
                     "player_id": (None if cur else pid)}),
                small=True).pack(side="right", padx=6)

    def _board_signature(self):
        """A cheap fingerprint of everything _render_board draws. Lets us skip the
        full destroy/rebuild when a state push didn't actually change the board —
        which is what made the app look like it was constantly reloading."""
        s = self.state or {}
        you = s.get("you")
        mode = s.get("mode", "normal")
        parts = [you, mode, self._is_host(), self._board_columns(),
                 self._room_title(), self._avatar_version]
        ledger = s.get("ledger", {})
        for it in ITEMS:
            e = ledger.get(it.key)
            if not e:
                parts.append((it.key, None))
            else:
                parts.append((
                    it.key, e.get("owner"), e.get("owner_name"), e.get("tier"),
                    e.get("level"), you in e.get("discovered", []),
                    round(e.get("cooldown_remaining", 0) or 0),
                    round(e.get("hold_remaining", 0) or 0) if mode == "hot_potato" else None,
                ))
        for p in s.get("players", []):
            parts.append((p.get("id"), p.get("name"), p.get("agent"),
                          p.get("emu"), p.get("avatar"), s.get("host")))
        return tuple(parts)

    def _render_board(self, force=False):
        sig = self._board_signature()
        if not force and sig == self._board_sig:
            return                       # nothing on the board changed — no rebuild
        self._board_sig = sig
        for w in self.board.winfo_children():
            w.destroy()
        if hasattr(self, "room_title_lbl"):
            self.room_title_lbl.config(text=self._room_title())   # reflect renames
        if hasattr(self, "rename_btn"):                           # host-only rename
            if self._is_host() and not self.rename_btn.winfo_ismapped():
                self.rename_btn.pack(side="left", padx=(8, 0))
            elif not self._is_host():
                self.rename_btn.pack_forget()
        self._render_players()
        if self._is_host():
            self.host_bar.pack(fill="x", pady=(0, 6), before=self.board_wrap)
            if not self.e_cd.get():
                self.e_cd.insert(0, str(int(self.state.get("cooldown_s", 5))))
            self.mode_var.set(self.state.get("mode", "normal"))
            self._sync_mode_fields()      # show only the fields the mode uses
            if not self.e_shuffle.get():
                self.e_shuffle.insert(0, str(int(self.state.get("shuffle_s", 120))))
        else:
            self.host_bar.pack_forget()
        self._update_mode_banner()

        ledger = self.state.get("ledger", {})
        cols = self._board_columns()
        self._board_cols = cols
        # Render from the LOCAL catalog so a stale server (e.g. one still pooling
        # Bottle) can never inject items we've removed. Order/names are canonical.
        for i, it in enumerate(ITEMS):
            cat = {"key": it.key, "name": it.name}
            e = ledger.get(it.key)
            self._card(self.board, cat, e).grid(row=i // cols, column=i % cols,
                                                 padx=4, pady=3, sticky="nsew")
        # equal-width columns that stretch to fill the canvas (wider, fewer rows)
        for c in range(cols):
            self.board.grid_columnconfigure(c, weight=1, minsize=CARD_MIN_PX, uniform="items")
        for c in range(cols, 12):                 # clear any columns from a wider layout
            self.board.grid_columnconfigure(c, weight=0, minsize=0, uniform="")
        self._refresh_manage()                    # keep an open host popup in sync

    def _board_columns(self):
        """How many item columns fit the current board width — favouring a wide,
        short grid. Falls back to a sensible count before the canvas is sized."""
        w = self.canvas.winfo_width() if hasattr(self, "canvas") else 0
        if w <= 1:
            w = 1040                              # pre-layout default (window width-ish)
        return max(4, min(10, w // CARD_MIN_PX))

    def _on_canvas_resize(self, event):
        # keep the grid as wide as the viewport, and reflow columns when the count
        # changes so the board stays wider-than-tall as the window resizes.
        self.canvas.itemconfig(self._board_window, width=event.width)
        if self.state and self._board_columns() != self._board_cols:
            self._render_board()

    def _card(self, parent, cat, e):
        you = self.state.get("you")
        border = LINE
        mine = e and e.get("owner") == you
        if e:
            owner = e.get("owner_name") or "unowned"
            sub = f"held by {owner}"
            if e.get("owner"):
                border = GREEN if mine else BLUE
            if e.get("tier") and e["tier"] != "—":   # icon shows tier too; label it briefly
                sub = f"{e['tier']} · {sub}"
        else:
            sub = "undiscovered"
        card = tk.Frame(parent, bg=PANEL, highlightbackground=GREEN if mine else border,
                        highlightthickness=2 if mine else 1)
        # Vertical tile: centered sprite, name + status below. Giving the name the
        # full card width keeps it on one line even in a dense, narrow grid.
        # Resolve the sprite locally from key+level so icons work against ANY
        # server (the public host may not send an `image` field).
        level = e.get("level", 0) if e else 0
        icon = self._item_icon(item_image(cat["key"], level), dim=not e)
        if icon is not None:
            il = tk.Label(card, image=icon, bg=PANEL)
            il.image = icon                      # extra ref guard (cache also holds it)
            il.pack(pady=(4, 1))
        tk.Label(card, text=cat["name"], fg=INK, bg=PANEL, font=("Segoe UI Semibold", 9),
                 wraplength=CARD_MIN_PX, justify="center").pack(fill="x", padx=3)
        # sub line: owner avatar (if any) + "held by …" / "undiscovered"
        owner_av = self._avatar_image(self._player_avatar_url(e.get("owner")), 16) if e else None
        subf = tk.Frame(card, bg=PANEL); subf.pack(fill="x", padx=3)
        inner = tk.Frame(subf, bg=PANEL); inner.pack()
        if owner_av is not None:
            al = tk.Label(inner, image=owner_av, bg=PANEL); al.image = owner_av
            al.pack(side="left", padx=(0, 3))
        tk.Label(inner, text=sub, fg=MUTED, bg=PANEL, font=("Segoe UI", 8)).pack(side="left")

        mode = self.state.get("mode", "normal")
        action = tk.Frame(card, bg=PANEL); action.pack(pady=(1, 4))
        if not e:
            tk.Label(action, text="—", fg=MUTED, bg=PANEL, font=("Segoe UI", 8)).pack()
        elif mode != "normal":
            # shuffle modes: no claiming — show ownership + hot-potato hold timer
            if mine:
                tk.Label(action, text="✓ yours", fg=GREEN, bg=PANEL,
                         font=("Segoe UI Semibold", 9)).pack()
            if mode == "hot_potato" and e.get("owner") and e.get("hold_remaining") is not None:
                tk.Label(action, text=f"⏱ {self._clock(e['hold_remaining'])}", fg=GOLD, bg=PANEL,
                         font=("Segoe UI", 8)).pack()
            elif not mine:
                tk.Label(action, text="—", fg=MUTED, bg=PANEL, font=("Segoe UI", 8)).pack()
        elif mine:
            tk.Label(action, text="✓ you hold this", fg=GREEN, bg=PANEL,
                     font=("Segoe UI Semibold", 9)).pack()
        elif you not in e.get("discovered", []):
            tk.Label(action, text="find one to claim", fg=GOLD, bg=PANEL,
                     font=("Segoe UI", 8)).pack()
        elif e.get("cooldown_remaining", 0) > 0.05:
            tk.Label(action, text=f"cooldown {e['cooldown_remaining']:.0f}s", fg=MUTED, bg=PANEL,
                     font=("Segoe UI", 8)).pack()
        else:
            self._button(action, "Claim", lambda k=cat["key"]: self._ui_send({"type": "claim", "item": k}),
                         small=True).pack()
        # host: right-click anywhere on a card to manage found/owner per player
        if self._is_host():
            card.configure(cursor="hand2")
            self._bind_recursive(
                card, "<Button-3>",
                lambda ev, k=cat["key"], n=cat["name"]: self._manage_item(k, n))
        return card

    def _show_emu_help(self):
        win = tk.Toplevel(self); win.title("Which emulators work?"); win.configure(bg=BG)
        win.geometry("600x420"); win.transient(self)
        tk.Label(win, text="Supported emulators (auto-detected)", fg=GOLD, bg=BG,
                 font=("Segoe UI Semibold", 13)).pack(anchor="w", padx=16, pady=(14, 8))
        body = (
            "HyruleLink reads your game's memory live. Any of these work — just have it\n"
            "running with your ALTTPR seed loaded, then press Connect & Play.\n\n"
            "EASIEST (nothing extra to install):\n"
            "  • snes9x-nwa  (the EmuNetworkAccess build of snes9x) — works out of the box.\n"
            "  • RetroArch  (any SNES core) — this app turns on the needed network setting.\n\n"
            "EVERYTHING ELSE — run QUsb2Snes or SNI, attach your emulator/console to it,\n"
            "and the app auto-detects it. That covers:\n"
            "  • snes9x-rr (rerecording) — load the QUsb2Snes Lua connector.\n"
            "  • BizHawk — enable its QUsb2Snes/usb2snes connector.\n"
            "  • Real hardware: SD2SNES / FXPak Pro.\n\n"
            "NOT supported: plain mainline snes9x with no network build and no Lua — it has\n"
            "no way to share memory. Use snes9x-nwa or RetroArch instead.\n\n"
            "Tip: SNI is the same tool Archipelago uses; if you already have it, you're set.")
        tk.Label(win, text=body, fg=INK, bg=BG, font=("Segoe UI", 9),
                 justify="left").pack(anchor="w", padx=16)
        self._button(win, "Got it", win.destroy, primary=True).pack(anchor="w", padx=16, pady=12)

    # ── bundled SNI bridge (snes9x-rr / BizHawk / real hardware) ────────────
    def _sni_exe(self):
        import shutil
        bundled = os.path.join(SNI_DIR, "sni.exe")
        if os.path.exists(bundled):
            return bundled
        return shutil.which("sni") or (r"C:\SNI\sni.exe" if os.path.exists(r"C:\SNI\sni.exe") else None)

    def _bridge_running(self):
        """Is a QUsb2Snes/SNI bridge already listening on 23074?"""
        try:
            socket.create_connection(("127.0.0.1", 23074), timeout=0.4).close()
            return True
        except OSError:
            return False

    def _start_sni(self):
        """'already' / 'started' / 'missing'. Never starts a second bridge."""
        if self._bridge_running():
            return "already"
        if self.sni_proc and self.sni_proc.poll() is None:
            return "started"
        exe = self._sni_exe()
        if not exe:
            return "missing"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.sni_proc = subprocess.Popen(
                [exe], cwd=os.path.dirname(exe),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        except Exception:
            return "missing"
        return "started"

    def _use_sni(self):
        status = self._start_sni()
        if status == "missing":
            messagebox.showwarning("HyruleLink", "Couldn't find the bundled SNI. Install/run "
                                   "SNI or QUsb2Snes manually, then press Connect & Play.")
            return False
        self._show_connector_help(status == "already")
        return True

    def _show_connector_help(self, already):
        lua = os.path.join(SNI_DIR, "lua", "Connector.lua")
        win = tk.Toplevel(self); win.title("SNI bridge"); win.configure(bg=BG)
        win.geometry("600x340"); win.transient(self)
        head = "✓ A bridge is already running" if already else "✓ SNI bridge started"
        tk.Label(win, text=head, fg=GREEN, bg=BG,
                 font=("Segoe UI Semibold", 13)).pack(anchor="w", padx=16, pady=(14, 8))
        tk.Label(win, text=(
            "Real hardware (SD2SNES / FXPak Pro): nothing else to do — it's detected\n"
            "automatically. Press Connect & Play.\n\n"
            "snes9x-rr or BizHawk: load this Lua connector inside the emulator\n"
            "(its Lua console / script window), then press Connect & Play:"),
            fg=INK, bg=BG, font=("Segoe UI", 10), justify="left").pack(anchor="w", padx=16)
        row = tk.Frame(win, bg=BG); row.pack(anchor="w", padx=16, pady=(6, 0))
        e = self._entry(row, value=lua); e.configure(width=42); e.pack(side="left", ipady=3)
        self._button(row, "Open folder",
                     lambda: (os.path.exists(lua) and os.startfile(os.path.dirname(lua))),
                     small=True).pack(side="left", padx=6)
        tk.Label(win, text="(snes9x-nwa and RetroArch connect directly — they don't need SNI.)",
                 fg=MUTED, bg=BG, font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(10, 0))
        self._button(win, "Got it", win.destroy, primary=True).pack(anchor="w", padx=16, pady=12)

    # ── emulator agent link ─────────────────────────────────────────────────
    def _toggle_connect(self):
        self._disconnect() if self.agent else self._connect()

    def _connect(self):
        from agent.agent import HyruleAgent
        transport, source, label = detect_emulator()
        if label == "not detected":
            if messagebox.askyesno("HyruleLink",
                "No emulator detected.\n\nStart the SNI bridge now? It's needed for snes9x-rr, "
                "BizHawk, or real hardware.\n\n(snes9x-nwa and RetroArch connect directly — if "
                "you're using one of those, click No and just make sure it's running.)"):
                self._use_sni()
                transport, label = "hardware", "SNI bridge"
        if transport == "hardware":
            from agent.sni.qusb2snes_tracker import QUsb2SnesTracker
            self.transport = QUsb2SnesTracker()
        else:
            from agent.sni.emu_connector import EmuConnector
            self.transport = EmuConnector(source=source)
        h = QueueLogHandler(self.log_q); h.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger().addHandler(h); logging.getLogger().setLevel(logging.INFO)
        self.agent = HyruleAgent(self.transport, self._ws_url(), self.room["code"],
                                 self.room["player_id"], self.room["player_token"], poll_interval=0.5)
        self.agent.start()
        self.btn_connect.config(text="Disconnect", bg=PANEL2, fg=INK)
        self._log(f"Linking emulator via {label}…")

    def _disconnect(self):
        try:
            if self.agent:
                self.agent.stop()
        except Exception:
            pass
        self.agent = None; self.transport = None
        if getattr(self, "btn_connect", None) is not None and self.btn_connect.winfo_exists():
            self.btn_connect.config(text="Connect & Play", bg=ACCENT, fg=BG)
        self._log("Emulator unlinked.")

    def _leave(self):
        self._disconnect()
        self._ui_stop.set()
        with self.ui_lock:
            try:
                if self.ui_conn:
                    self.ui_conn.close()
            except Exception:
                pass
            self.ui_conn = None
        self.room = None; self.state = None
        self.show_start()

    def _log(self, line):
        box = getattr(self, "logbox", None)
        if box is None or not box.winfo_exists():   # not on the room screen (or it's gone)
            return
        box.configure(state="normal"); box.insert("end", line + "\n")
        box.see("end"); box.configure(state="disabled")

    # ── in-app emulator launch ──────────────────────────────────────────────
    def _emu_type(self, path):
        choice = self.cfg.get("emu_type", "auto")
        if choice in ("snes9x", "retroarch"):
            return choice
        return "retroarch" if "retroarch" in os.path.basename(path or "").lower() else "snes9x"

    def _guess_core(self, emu_path):
        if not emu_path:
            return ""
        cand = os.path.join(os.path.dirname(emu_path), "cores", "snes9x_libretro.dll")
        return cand if os.path.exists(cand) else ""

    def _ensure_ra_netcfg(self):
        path = os.path.join(HERE, "retroarch_net.cfg")
        try:
            if not os.path.exists(path):
                with open(path, "w") as f:
                    f.write('network_cmd_enable = "true"\nnetwork_cmd_port = "55355"\n')
        except Exception:
            pass
        return path

    def _emu_summary(self):
        emu = self.cfg.get("emu_path")
        return f"{self._emu_type(emu)} — {os.path.basename(emu)}" if emu else "not set up"

    # ── in-app seed generation (pyz3r → alttpr.com) ─────────────────────────
    def generate_seed(self):
        base = self.cfg.get("base_rom")
        if not base or not os.path.exists(base):
            p = filedialog.askopenfilename(
                title="Select your A Link to the Past JP 1.0 base ROM (.sfc)",
                filetypes=[("SNES ROM", "*.sfc *.smc"), ("All", "*.*")])
            if not p:
                return
            self.cfg["base_rom"] = p; save_settings(self.cfg); base = p
        self._log("Generating an Open seed (needs internet, ~10s)…")
        threading.Thread(target=self._generate_seed_thread, args=(base,), daemon=True).start()

    def _generate_seed_thread(self, base):
        import asyncio
        import pyz3r
        out_dir = os.path.join(HERE, "seeds")
        os.makedirs(out_dir, exist_ok=True)

        async def make():
            seed = await pyz3r.ALTTPR.generate(settings=OPEN_SEED_SETTINGS, endpoint="/api/randomizer")
            hash_id = getattr(seed, "hash", None) or seed.url.rstrip("/").split("/")[-1]
            out = os.path.join(out_dir, f"{hash_id}.sfc")
            await seed.create_patched_game(
                input_filename=base, output_filename=out,
                heartspeed="half", heartcolor="red", spritename="Link",
                music=True, quickswap=True, menu_speed="instant", msu1_resume=False)
            return out, seed.url
        try:
            out, url = asyncio.run(make())
            self.cfg["rom_path"] = out; save_settings(self.cfg)
            self.seedgen_q.put(("done", out, url))
        except Exception as e:
            self.seedgen_q.put(("error", str(e), ""))

    def launch_emulator(self):
        emu = self.cfg.get("emu_path"); rom = self.cfg.get("rom_path")
        if not emu or not os.path.exists(emu):
            self._configure_emulator(); return
        etype = self._emu_type(emu)
        try:
            if etype == "retroarch":
                core = self.cfg.get("core_path") or self._guess_core(emu)
                if not core or not os.path.exists(core):
                    messagebox.showwarning("HyruleLink", "Pick the snes9x RetroArch core in Configure.")
                    self._configure_emulator(); return
                args = [emu, "-L", core] + ([rom] if rom else []) + ["--appendconfig", self._ensure_ra_netcfg()]
            else:
                args = [emu] + ([rom] if rom else [])
            subprocess.Popen(args)
            self._log(f"Launching {etype} (network-ready)…")
        except Exception as ex:
            messagebox.showerror("HyruleLink", f"Couldn't launch emulator:\n{ex}")

    def _configure_emulator(self):
        win = tk.Toplevel(self); win.title("Emulator setup"); win.configure(bg=BG)
        win.geometry("580x440"); win.transient(self); pad = {"padx": 16}
        tk.Label(win, text="Your emulator", fg=GOLD, bg=BG,
                 font=("Segoe UI Semibold", 13)).pack(anchor="w", pady=(14, 6), **pad)
        tk.Label(win, text="Type", fg=MUTED, bg=BG, font=("Segoe UI", 9)).pack(anchor="w", **pad)
        type_var = tk.StringVar(value=self.cfg.get("emu_type", "auto"))
        ttk.Combobox(win, textvariable=type_var, state="readonly",
                     values=["auto", "snes9x", "retroarch"]).pack(anchor="w", **pad)

        def path_row(label, key, kinds):
            tk.Label(win, text=label, fg=MUTED, bg=BG, font=("Segoe UI", 9)).pack(anchor="w", pady=(8, 0), **pad)
            row = tk.Frame(win, bg=BG); row.pack(fill="x", **pad)
            e = self._entry(row, value=self.cfg.get(key, "")); e.pack(side="left", fill="x", expand=True, ipady=3)
            def browse():
                p = filedialog.askopenfilename(title=label, filetypes=kinds)
                if p:
                    e.delete(0, "end"); e.insert(0, p)
            tk.Button(row, text="Browse…", command=browse, relief="flat", bg=PANEL, fg=INK,
                      bd=0, padx=10).pack(side="left", padx=(6, 0))
            return e

        e_emu = path_row("Emulator program (.exe)", "emu_path", [("Programs", "*.exe"), ("All", "*.*")])
        e_rom = path_row("Your ALTTPR seed (.sfc)", "rom_path", [("SNES ROM", "*.sfc *.smc"), ("All", "*.*")])
        e_base = path_row("ALTTP JP 1.0 base ROM — only needed for “Generate seed”",
                          "base_rom", [("SNES ROM", "*.sfc *.smc"), ("All", "*.*")])
        e_core = path_row("RetroArch core (snes9x_libretro.dll) — RetroArch only",
                          "core_path", [("Core", "*.dll"), ("All", "*.*")])
        err = tk.Label(win, text="", fg=RED, bg=BG, font=("Segoe UI", 9)); err.pack(anchor="w", **pad)

        def save():
            emu = e_emu.get().strip()
            if emu and not os.path.exists(emu):
                err.config(text="emulator path not found"); return
            self.cfg.update(emu_type=type_var.get(), emu_path=emu,
                            rom_path=e_rom.get().strip(), base_rom=e_base.get().strip(),
                            core_path=e_core.get().strip())
            if self._emu_type(emu) == "retroarch" and not self.cfg["core_path"]:
                self.cfg["core_path"] = self._guess_core(emu)
            save_settings(self.cfg)
            if hasattr(self, "emu_summary_lbl"):
                self.emu_summary_lbl.config(text="Emulator: " + self._emu_summary())
            win.destroy()

        bar = tk.Frame(win, bg=BG); bar.pack(anchor="w", pady=12, **pad)
        self._button(bar, "Save", save, primary=True).pack(side="left")
        self._button(bar, "Cancel", win.destroy).pack(side="left", padx=8)

    # ── periodic refresh ────────────────────────────────────────────────────
    def _tick(self):
        # Discord login results from the background poll/validate threads
        while True:
            try:
                msg = self.login_q.get_nowait()
            except queue.Empty:
                break
            st = msg.get("status")
            if st == "ok":
                self.session = msg["token"]
                self.cfg["session"] = self.session
                save_settings(self.cfg)
                self.me = {"name": msg.get("name"), "avatar": msg.get("avatar"),
                           "admin": msg.get("admin")}
                if self.room is None:
                    self.show_start()
            elif st == "me":
                self.me = msg.get("me")
                if self.me is None:           # stored session was invalid/expired
                    self.session = None
                    self.cfg.pop("session", None); save_settings(self.cfg)
                if self.room is None:
                    self.show_start()
            elif st == "expired" and self.room is None and hasattr(self, "err"):
                self.err.config(text="Login timed out — click Login with Discord again.")
        # avatar downloads finished by worker threads -> make PhotoImages here
        av_loaded = False
        while True:
            try:
                key, pil = self._avatar_q.get_nowait()
            except queue.Empty:
                break
            if pil is None:
                self._avatar_cache[key] = False           # failed — don't retry
            else:
                self._avatar_cache[key] = ImageTk.PhotoImage(pil)
                av_loaded = True
        if av_loaded:
            self._avatar_version += 1     # invalidate the board signature
            if self.room is not None and self.state and hasattr(self, "board"):
                self._render_board()
        while True:
            try:
                self._log(self.log_q.get_nowait())
            except queue.Empty:
                break
        # tunnel status -> the public-link field in the local-host dialog
        while True:
            try:
                kind, val = self.tunnel_q.get_nowait()
            except queue.Empty:
                break
            if kind == "url":
                self.tunnel_url = val
                self._log("Public link ready: " + val)
            text = ("error: " + val) if kind == "error" else val
            if self._pub_url_entry is not None:
                try:
                    self._pub_url_entry.delete(0, "end")
                    self._pub_url_entry.insert(0, text)
                except Exception:
                    pass
        # in-app seed generation results
        while True:
            try:
                kind, a, _b = self.seedgen_q.get_nowait()
            except queue.Empty:
                break
            if kind == "done":
                self._log(f"✓ Seed ready: {os.path.basename(a)} — click Launch emulator.")
                if hasattr(self, "emu_summary_lbl"):
                    self.emu_summary_lbl.config(text="Emulator: " + self._emu_summary())
                messagebox.showinfo("HyruleLink",
                    f"Open seed generated and set as your ROM:\n{a}\n\nClick “Launch emulator”.")
            else:
                self._log("Seed generation failed: " + a)
                messagebox.showerror("HyruleLink", "Couldn't generate a seed:\n" + a)
        new_state = False
        while True:
            try:
                msg = self.state_q.get_nowait()
            except queue.Empty:
                break
            t = msg.get("type")
            if t == "state":
                self.state = msg; new_state = True
            elif t == "event":
                self._log(msg.get("text", ""));
            elif t == "reject":
                reason = msg.get("reason", "")
                self._log("⚠ " + reason)
                if self.room is not None and ("room not found" in reason.lower()
                                              or "room closed" in reason.lower()):
                    messagebox.showinfo("HyruleLink", "This room was closed by an admin.")
                    self._leave()
                    break          # stop draining; rest of _tick handles room=None
        if new_state and self.room is not None and hasattr(self, "board"):
            self._render_board()

        # status dots
        if self.room is not None:
            srv_ok = self.ui_conn is not None
            self._dot(self.dot_srv, GREEN if srv_ok else GOLD)
            self.lbl_srv.config(text="server: " + ("connected" if srv_ok else "connecting…"))
            if self.agent is None:
                t, s, label = self._detected
                ok = label != "not detected"
                self._dot(self.dot_emu, GREEN if ok else "#555")
                self.lbl_emu.config(text="emulator: " + (label if ok else "not linked"))
                if hasattr(self, "emu_line"):
                    self.emu_line.config(
                        text=("✓ " + label + " — press Connect & Play") if ok
                        else "No emulator found — start snes9x-nwa / RetroArch / QUsb2Snes/SNI "
                             "(see “Which emulators?”), or click Launch emulator",
                        fg=GREEN if ok else MUTED)
            else:
                emu_ok = getattr(self.transport, "connected", False)
                self._dot(self.dot_emu, GREEN if emu_ok else GOLD)
                self.lbl_emu.config(text="emulator: " + ("linked" if emu_ok else "waiting…"))
                if hasattr(self, "emu_line"):
                    self.emu_line.config(text="● Linked — your pickups + claims are live"
                                         if emu_ok else "Linking… (load your seed if you haven't)",
                                         fg=GREEN if emu_ok else GOLD)
        else:
            self._dot(self.dot_emu, "#555"); self._dot(self.dot_srv, "#555")
            self.lbl_emu.config(text="emulator: —"); self.lbl_srv.config(text="server: —")
        # live countdowns between server pushes (cooldown, hot-potato hold, chaos timer)
        if self.state:
            for v in self.state.get("ledger", {}).values():
                if v.get("cooldown_remaining", 0) > 0:
                    v["cooldown_remaining"] = max(0, v["cooldown_remaining"] - 0.8)
                if v.get("hold_remaining", 0) > 0:
                    v["hold_remaining"] = max(0, v["hold_remaining"] - 0.8)
            if self.state.get("mode") == "chaos" and self.state.get("shuffle_remaining", 0) > 0:
                self.state["shuffle_remaining"] = max(0, self.state["shuffle_remaining"] - 0.8)
                self._update_mode_banner()
        self.after(800, self._tick)

    def _on_close(self):
        self._closing = True            # stops the pixel-field animation loop
        try:                            # never let cleanup errors block the close
            self._stop_all.set(); self._ui_stop.set(); self._disconnect()
        except Exception:
            pass
        for proc in (self.tunnel_proc, self.local_server, self.sni_proc):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
