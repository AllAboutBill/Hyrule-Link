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
from shared.rules import DEFAULT_RULES as RULE_DEFAULTS, PRESET_OVERRIDES as RULE_PRESETS
# Emulator discovery (which sources are open + auto-detect) — shares its probe
# ports/handshakes with the transport in agent.sni.emu_connector, so the two
# can't drift apart.
from agent.sni.discovery import scan_emulators, detect_emulator, SNI_BRIDGE_PORT

SETTINGS = os.path.join(HERE, "agent", "gui_settings.json")
PUBLIC_SERVER = "https://hyrulelink.billogna.lol"   # default shared server
SNI_DIR = os.path.join(HERE, "tools", "sni")        # installer-managed SNI bridge (MIT)
ITEMS_DIR = os.path.join(HERE, "web", "items")      # item sprite PNGs (shared w/ web)
ICON_PX = 38                                        # board item sprite size
CARD_MIN_PX = 145                                   # min item-card width → column count
CARD_H = 128                                        # stable grid rhythm

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


def _merge(base, overrides):
    """Deep-merge `overrides` onto a copy of `base` (nested dicts merged, not replaced)."""
    out = copy.deepcopy(base)
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


# ALTTPR generation presets (mirrors alttpr.com's preset dropdown / AlttprHelper).
# Each is an override on OPEN_SEED_SETTINGS; "Open" is the bare assured-sword template.
SEED_PRESET_OVERRIDES = {
    "Open": {},
    "Standard": {"mode": "standard", "weapons": "randomized"},
    "Fast Ganon": {"goal": "fast_ganon"},
    "All Dungeons": {"goal": "dungeons"},
    "Keysanity": {"dungeon_items": "full"},
    "Maps/Compasses/Keys": {"dungeon_items": "mcs"},
    "Hard": {"item": {"functionality": "hard", "pool": "hard"}},
    "Swordless": {"weapons": "swordless"},
}


def preset_settings(name):
    return _merge(OPEN_SEED_SETTINGS, SEED_PRESET_OVERRIDES.get(name, {}))


# Cosmetic / patch options passed to pyz3r create_patched_game (valid values per
# pyz3r.rom). MSU mode forces music off + resume on regardless of these.
DEFAULT_PATCH = {"heartspeed": "half", "heartcolor": "red", "menu_speed": "instant",
                 "quickswap": True, "music": True, "spritename": "Link",
                 "msu1_resume": False}
HEARTSPEEDS = ["off", "quarter", "half", "normal", "double"]
HEARTCOLORS = ["red", "blue", "green", "yellow"]
MENU_SPEEDS = ["instant", "fast", "normal", "slow"]

# palette — billogna.lol's "aurora" design language (see README "Web UI style").
# mint / violet / blue over near-black; no pink. Tk can't render the web's
# translucent "glass" or backdrop blur, so the rgba() card/border tokens are
# flattened to the solid colours they resolve to over the near-black background.
# Violet on neutral charcoal — surfaces are de-saturated (no blue cast) so the
# violet accent reads as a deliberate pop instead of a sea of blue. Blue is
# reserved for "owned by someone else", aqua for "you/online", gold for "owner".
BG = "#0b0a10"; PANEL = "#15131c"; PANEL2 = "#211e2a"; FIELD = "#100f16"
CARD = "#121118"; CARD_MINE = "#211a35"; CARD_OWNED = "#141a24"
INK = "#f4f1fa"; MUTED = "#aaa3b7"; DIM = "#625c6c"; EDGE_HI = "#3a3448"
GOLD = "#f2c75c"; GREEN = "#68dda8"; RED = "#ff7585"; BLUE = "#6bb4ff"; LINE = "#302b3c"
ACCENT = "#b7a0ff"                              # brand violet (headings, focus ring)
PRIMARY = "#7657ff"; PRIMARY_HI = "#8c73ff"     # primary button fill / hover (white text)
ACCENT2 = "#c08cff"                             # chaos / secondary accent


def _lighten(hexcol, amt=0.12):
    """Blend a hex colour toward white by `amt` — used for button hover states."""
    h = hexcol.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    f = lambda c: max(0, min(255, int(c + (255 - c) * amt)))
    return "#%02x%02x%02x" % (f(r), f(g), f(b))


# ── custom ruleset editor metadata ──────────────────────────────────────────
# (section title, [(key, kind, label, [enum choices])])
RULE_FIELDS = [
    ("Claiming & stealing", [
        ("claiming", "bool", "Allow manual claiming", None),
        ("require_found_to_claim", "bool", "Must have found it to claim", None),
        ("open_season_scope", "enum", "Open-season scope", ["owned", "any"]),
        ("steal_cooldown_s", "num", "Steal cooldown (s)", None),
        ("cooldown_scope", "enum", "Cooldown applies to", ["item", "thief", "victim", "none"]),
        ("steal_back_lock_s", "num", "Steal-back lock (s)", None),
        ("steal_budget_per_min", "num", "Max steals/min (0=∞)", None),
    ]),
    ("Holding / leases", [
        ("hold_limit_s", "num", "Hold limit (s, 0=off)", None),
        ("hold_expiry", "enum", "On expiry", ["next_finder", "release", "return_finder"]),
        ("tenure_lock_s", "num", "Unstealable after (s)", None),
        ("idle_release_s", "num", "Drop items when offline (s)", None),
    ]),
    ("Raid (unfound steals — when found-gate is off)", [
        ("borrow_s", "num", "Borrow duration (s, 0=permanent)", None),
        ("borrow_revert", "enum", "Revert to", ["prev_owner", "pool"]),
    ]),
    ("Stackable layers", [
        ("auto_shuffle_s", "num", "Auto-reshuffle every (s, 0=off)", None),
        ("shuffle_scope", "enum", "Reshuffle which", ["all", "unowned", "idle"]),
        ("shared_discovery", "bool", "Shared discovery", None),
    ]),
]


def summarize_rules(r):
    """Plain-English one-liner for a ruleset (mirror of the server's)."""
    p = []
    if r.get("claiming"):
        if r.get("require_found_to_claim"):
            p.append("claim found items")
        else:
            p.append("steal anything someone owns" if r.get("open_season_scope") == "owned"
                     else "claim anything")
        cd, scope = int(r.get("steal_cooldown_s", 0)), r.get("cooldown_scope")
        if cd and scope != "none":
            p.append(f"{cd}s {scope} cooldown")
        if r.get("steal_back_lock_s"):
            p.append(f"{int(r['steal_back_lock_s'])}s steal-back lock")
        if r.get("steal_budget_per_min"):
            p.append(f"max {int(r['steal_budget_per_min'])} steals/min")
    else:
        p.append("no manual claiming")
    if r.get("hold_limit_s"):
        ex = {"next_finder": "→ next finder", "release": "→ released",
              "return_finder": "→ first finder"}.get(r.get("hold_expiry"), "")
        p.append(f"hold {int(r['hold_limit_s'])}s {ex}".strip())
    if r.get("tenure_lock_s"):
        p.append(f"unstealable after {int(r['tenure_lock_s'])}s")
    if r.get("idle_release_s"):
        p.append(f"drop items when offline {int(r['idle_release_s'])}s")
    if not r.get("require_found_to_claim") and r.get("borrow_s"):
        rev = "to owner" if r.get("borrow_revert") == "prev_owner" else "to pool"
        p.append(f"borrows revert {rev} after {int(r['borrow_s'])}s")
    if r.get("auto_shuffle_s"):
        p.append(f"reshuffle {r.get('shuffle_scope')} every {int(r['auto_shuffle_s'])}s")
    if r.get("shared_discovery"):
        p.append("shared discovery")
    return " · ".join(p)
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
        self._configure_ttk()
        # Open wide and as tall as the screen allows, so the multi-column board
        # fits without scrolling (clamped so it still fits smaller displays).
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{min(1240, sw - 80)}x{min(920, sh - 80)}")
        self.minsize(720, 560)
        self._enable_dark_titlebar()  # dark native title bar to match the app
        self._board_cols = 0          # current item-grid column count (responsive)

        self.cfg = load_settings()
        self._migrate_settings()
        self.base = self.cfg.get("server", PUBLIC_SERVER)
        self.session = self.cfg.get("session")   # signed Discord session token (optional)
        self.me = None              # {name, avatar, admin} when logged in
        self.login_q = queue.Queue()  # device-login results from the poll thread
        self.local_server = None    # subprocess if hosting a server on this PC
        self.sni_proc = None        # installed SNI bridge, only if WE started it
        self.tunnel_proc = None     # cloudflared subprocess (optional public link)
        self.tunnel_url = None
        self.tunnel_q = queue.Queue()
        self._pub_url_entry = None  # dialog widget that shows the public link
        self.seedgen_q = queue.Queue()  # in-app seed generation status
        self.room = None            # join/create payload
        self._autolink_armed = False  # auto-connect once an emulator is detected
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
        self._ui_outbox = queue.Queue()   # actions queued while the socket is down

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

        self._build_chrome()
        self.show_start()
        self._check_session_async()      # validate a saved Discord login
        threading.Thread(target=self._detect_loop, daemon=True).start()
        self._tick()
        self._pump_loop()                # fast board refresh (server-state queue)
        self._animate_fields()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _detect_loop(self):
        while not self._stop_all.is_set():
            if self.room is not None and self.agent is None:
                try:
                    self._detected = detect_emulator()
                except Exception:
                    pass
                # Auto-link: once an emulator is actually detected, connect for the
                # player. Gated on `armed` so a manual Disconnect (which disarms)
                # isn't immediately undone, and on detection so we never nag.
                if (self._autolink_armed
                        and self.cfg.get("auto_connect", True)
                        and self._detected[2] != "not detected"):
                    self.after(0, self._auto_connect)
            self._stop_all.wait(1.5)

    def _auto_connect(self):
        # Runs on the UI thread; re-check everything (state may have changed between
        # the background detect and now).
        if (self._autolink_armed and self.agent is None and self.room is not None
                and self.cfg.get("auto_connect", True)
                and self._detected[2] != "not detected"):
            self._connect(auto=True)

    # ── chrome ──────────────────────────────────────────────────────────────
    def _configure_ttk(self):
        """Make native ttk controls belong to the same dark visual system."""
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("HL.TCombobox", fieldbackground=FIELD, background=PANEL2,
                        foreground=INK, arrowcolor=MUTED, bordercolor=LINE,
                        lightcolor=LINE, darkcolor=LINE, padding=(7, 5))
        style.map("HL.TCombobox", fieldbackground=[("readonly", FIELD)],
                  foreground=[("readonly", INK)], bordercolor=[("focus", ACCENT)])
        style.configure("HL.Vertical.TScrollbar", background=PANEL2, troughcolor=BG,
                        bordercolor=BG, arrowcolor=MUTED, lightcolor=PANEL2,
                        darkcolor=PANEL2, width=12)

    def _enable_dark_titlebar(self):
        """Windows 10/11: paint the native title bar dark so it matches the app
        instead of the default white. No-op (and harmless) elsewhere."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            value = ctypes.c_int(1)
            for attr in (20, 19):     # DWMWA_USE_IMMERSIVE_DARK_MODE: 20 (20H1+), 19 (older)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
            # nudge a repaint so the bar flips immediately rather than on first focus
            self.withdraw(); self.deiconify()
        except Exception:
            pass

    def _build_chrome(self):
        # Header is a live pixel-field banner — the closest Tk can get to the
        # site's nexus-bg.js field (widgets are opaque, so the field can only sit
        # behind chrome, not show *through* the cards like the web's glass does).
        HEAD_H = 62
        self.header = tk.Canvas(self, height=HEAD_H, bg=BG, highlightthickness=0)
        self.header.pack(fill="x")
        self._attach_pixel_field(self.header, cell=24)
        self.header.create_text(20, 23, anchor="w", text="HyruleLink",
                                fill=ACCENT, font=(self.logo_font, 17, "bold"), tags="fg")
        self.header.create_text(21, 44, anchor="w", text="SHARED INVENTORY CO-OP",
                                fill=MUTED, font=(self.mono_font, 7), tags="fg")
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

        self.body = tk.Frame(self, bg=BG); self.body.pack(fill="both", expand=True, padx=20, pady=10)

        bar = tk.Frame(self, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        bar.pack(fill="x", side="bottom")
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
        e = tk.Entry(parent, show=show, bg=FIELD, fg=INK, insertbackground=ACCENT,
                     relief="flat", font=("Segoe UI", 10))
        e.insert(0, value)
        e.configure(highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT,
                    disabledbackground=FIELD, disabledforeground=DIM)
        return e

    def _button(self, parent, text, cmd, primary=False, small=False):
        # primary CTAs are filled violet with white text; secondary buttons are a
        # raised slate chip with a hairline border. Both lift (lighten) on hover —
        # the hover reads the *current* bg, so it stays correct even when a button
        # is later recoloured (e.g. the Connect/Disconnect toggle).
        base = PRIMARY if primary else PANEL2
        b = tk.Button(parent, text=text, command=cmd, relief="flat", cursor="hand2",
                      bg=base, fg="#ffffff" if primary else INK,
                      activebackground=PRIMARY_HI if primary else LINE,
                      activeforeground="#ffffff" if primary else INK,
                      font=("Segoe UI Semibold", 9 if small else 10),
                      bd=0, highlightthickness=1,
                      highlightbackground=PRIMARY_HI if primary else LINE,
                      highlightcolor=PRIMARY_HI if primary else LINE,
                      padx=10 if small else 16, pady=4 if small else 8)

        def _enter(_):
            if not getattr(b, "_hovering", False):
                b._rest = b.cget("bg"); b._hovering = True
                b.configure(bg=_lighten(b._rest))

        def _leave(_):
            b._hovering = False
            b.configure(bg=getattr(b, "_rest", base))
        b.bind("<Enter>", _enter, add="+")
        b.bind("<Leave>", _leave, add="+")
        return b

    def _label(self, parent, text, **kw):
        return tk.Label(parent, text=text, fg=kw.pop("fg", INK), bg=kw.pop("bg", BG),
                        font=kw.pop("font", ("Segoe UI", 10)), **kw)

    def _panel(self, parent, **packopts):
        """A card surface with a hairline border and a faint lit top edge — a cheap
        'lit from above' bevel so panels read with depth instead of flat. Returns
        the inner content frame; the wrapper is packed for you (pass pack options)."""
        outer = tk.Frame(parent, bg=EDGE_HI)                 # 1px shows as the top edge
        outer.pack(**(packopts or {"fill": "x"}))
        inner = tk.Frame(outer, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        inner.pack(fill="both", expand=True, pady=(1, 0))    # reveal 1px of EDGE_HI on top
        return inner

    def _section_title(self, parent, text, fg=INK):
        """A section header with a small violet accent bar for hierarchy."""
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", anchor="w", padx=10, pady=(8, 3))
        tk.Frame(row, bg=ACCENT, width=3, height=14).pack(side="left", padx=(0, 7))
        tk.Label(row, text=text, fg=fg, bg=PANEL,
                 font=("Segoe UI Semibold", 10)).pack(side="left")
        return row

    def _eyebrow(self, parent, text, bg=BG):
        return tk.Label(parent, text=text.upper(), fg=ACCENT, bg=bg,
                        font=(self.mono_font, 8), anchor="w")

    def _field_label(self, parent, text, bg=PANEL):
        return tk.Label(parent, text=text, fg=MUTED, bg=bg,
                        font=("Segoe UI Semibold", 8), anchor="w")

    def _copy_text(self, value, notice="Copied"):
        self.clipboard_clear(); self.clipboard_append(value); self.update()
        if hasattr(self, "toast_lbl") and self.toast_lbl.winfo_exists():
            self.toast_lbl.config(text=notice, fg=GREEN)
            self.after(1800, lambda: self.toast_lbl.winfo_exists() and self.toast_lbl.config(text=""))

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
        box = self._panel(fr, fill="x")
        self._section_title(box, "Your rooms")
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
    _BG_RGB = (0x0d, 0x0d, 0x10)   # BG = #0d0d10

    def _blend_pixel(self, value, alpha):
        eff = (alpha / 255.0) * 0.32          # web: opacity .30 over the bg
        # faint violet tint (R/B up a touch, G down) rather than flat gray or a
        # blue cast — keeps the header on-brand without making it read as blue.
        tint = (value * 1.02, value * 0.94, value * 1.08)
        out = []
        for v, b in zip(tint, self._BG_RGB):
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
        self.unbind_all("<MouseWheel>")
        self._clear_body(); self._set_who("")
        f = self.body
        shell = tk.Frame(f, bg=BG); shell.pack(fill="both", expand=True)

        hero = tk.Frame(shell, bg=BG); hero.pack(fill="x", pady=(2, 16))
        self._eyebrow(hero, "Player app").pack(anchor="w")
        tk.Label(hero, text="Start a shared run", fg=INK, bg=BG,
                 font=(self.logo_font, 20, "bold")).pack(anchor="w", pady=(2, 3))
        tk.Label(hero, text="Join friends on the public hub, create a room, or host the whole "
                 "session from this PC.", fg=MUTED, bg=BG, font=("Segoe UI", 10),
                 anchor="w").pack(anchor="w")

        columns = tk.Frame(shell, bg=BG); columns.pack(fill="both", expand=True)
        columns.grid_columnconfigure(0, weight=3, uniform="start")
        columns.grid_columnconfigure(1, weight=2, uniform="start")
        columns.grid_rowconfigure(0, weight=1)
        left = tk.Frame(columns, bg=BG); left.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        right = tk.Frame(columns, bg=BG); right.grid(row=0, column=1, sticky="nsew", padx=(7, 0))

        play = self._panel(left, fill="x")
        self._section_title(play, "Join or create a room")
        form = tk.Frame(play, bg=PANEL); form.pack(fill="x", padx=14, pady=(2, 14))
        default_name = self.cfg.get("display") or (self.me.get("name") if self.me else "") or ""
        self._field_label(form, "PLAYER NAME").pack(fill="x", pady=(0, 4))
        self.e_name = self._entry(form, value=default_name)
        self.e_name.pack(fill="x", ipady=6, pady=(0, 12))

        self._field_label(form, "ROOM CODE").pack(fill="x", pady=(0, 4))
        coderow = tk.Frame(form, bg=PANEL); coderow.pack(fill="x")
        self.e_code = self._entry(coderow, value=self.cfg.get("room", ""))
        self.e_code.pack(side="left", fill="x", expand=True, ipady=6)
        self._button(coderow, "Join room", self._join, primary=True).pack(side="left", padx=(8, 0))
        self.e_code.bind("<Return>", lambda _e: self._join())

        sep = tk.Frame(form, bg=LINE, height=1); sep.pack(fill="x", pady=14)
        hostrow = tk.Frame(form, bg=PANEL); hostrow.pack(fill="x")
        tk.Label(hostrow, text="Starting the group?", fg=MUTED, bg=PANEL,
                 font=("Segoe UI", 9)).pack(side="left")
        self._button(hostrow, "Create a room", self._host).pack(side="right")

        server = tk.Frame(play, bg=FIELD, highlightbackground=LINE, highlightthickness=1)
        server.pack(fill="x", padx=14, pady=(0, 14))
        srvhead = tk.Frame(server, bg=FIELD); srvhead.pack(fill="x", padx=10, pady=(8, 3))
        self._field_label(srvhead, "SERVER", bg=FIELD).pack(side="left")
        self._button(srvhead, "Use public", self._use_public_server, small=True).pack(side="right")
        self.e_server = self._entry(server, value=self.cfg.get("server", PUBLIC_SERVER))
        self.e_server.pack(fill="x", padx=10, ipady=4)
        tk.Label(server, text="Keep the public server unless a host gave you another address.",
                 fg=MUTED, bg=FIELD, font=("Segoe UI", 8), anchor="w").pack(
                     fill="x", padx=10, pady=(4, 8))

        self.myrooms_frame = tk.Frame(left, bg=BG)
        if self.me:
            self.myrooms_frame.pack(fill="x", pady=(14, 0))
            self._load_my_rooms_async()

        account = self._panel(right, fill="x")
        self._section_title(account, "Account")
        ar = tk.Frame(account, bg=PANEL); ar.pack(fill="x", padx=14, pady=(2, 14))
        if self.me:
            tag = "  ·  moderator" if self.me.get("admin") else ""
            tk.Label(ar, text="●", fg=GREEN, bg=PANEL, font=("Segoe UI", 11)).pack(side="left")
            tk.Label(ar, text=f"{self.me.get('name', 'Discord user')}{tag}", fg=INK, bg=PANEL,
                     font=("Segoe UI Semibold", 10)).pack(side="left", padx=7)
            self._button(ar, "Log out", self._discord_logout, small=True).pack(side="right")
        else:
            info = tk.Frame(ar, bg=PANEL); info.pack(fill="x")
            tk.Label(info, text="Keep rooms across devices and unlock moderator tools.",
                     fg=MUTED, bg=PANEL, font=("Segoe UI", 9), wraplength=310,
                     justify="left").pack(anchor="w", pady=(0, 9))
            self._button(info, "Connect Discord", self._discord_login).pack(anchor="w")

        local = self._panel(right, fill="x", pady=(14, 0))
        self._section_title(local, "Host from this PC")
        tk.Label(local, text="Run a private hub here. Use the free tunnel when players are "
                 "outside your network.", fg=MUTED, bg=PANEL, font=("Segoe UI", 9),
                 wraplength=360, justify="left").pack(anchor="w", padx=14, pady=(2, 10))
        lrow = tk.Frame(local, bg=PANEL); lrow.pack(fill="x", padx=14)
        self._field_label(lrow, "PORT").pack(side="left", padx=(0, 7))
        self.e_port = self._entry(lrow, value=str(self.cfg.get("local_port", 5019)))
        self.e_port.configure(width=7); self.e_port.pack(side="left", ipady=4)
        self.tunnel_var = tk.BooleanVar(value=self.cfg.get("use_tunnel", False))
        tk.Checkbutton(local, text="Create a public internet link", variable=self.tunnel_var,
                       fg=INK, bg=PANEL, selectcolor=PANEL2, activebackground=PANEL,
                       activeforeground=INK, font=("Segoe UI", 9), anchor="w").pack(
                           anchor="w", padx=10, pady=(10, 10))
        self._button(local, "Start local server", self._host_local, primary=True).pack(
            fill="x", padx=14, pady=(0, 14))

        self.err = self._label(shell, "", fg=RED, font=("Segoe UI Semibold", 9))
        self.err.pack(fill="x", pady=(10, 0))
        self.toast_lbl = self._label(shell, "", fg=GREEN, font=("Segoe UI", 9))
        self.toast_lbl.pack(fill="x")

    def _use_public_server(self):
        """One-tap return to the shared public server (no retyping the URL)."""
        if hasattr(self, "e_server"):
            self.e_server.delete(0, "end"); self.e_server.insert(0, PUBLIC_SERVER)
        self.base = PUBLIC_SERVER
        self.cfg["server"] = PUBLIC_SERVER; save_settings(self.cfg)

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
        # Arm auto-link: once an emulator is detected we connect on our own, so the
        # player doesn't have to hit Connect. A manual Disconnect disarms it.
        self._autolink_armed = True
        # remember our identity in this room so a future launch rejoins as US.
        # Merge (don't replace) so a ROM pinned to this room survives the rejoin.
        rooms = self.cfg.setdefault("rooms", {})
        entry = rooms.setdefault(data["code"], {})
        entry.update(player_id=data["player_id"], player_token=data["player_token"])
        # if this room already has a ROM, make Launch use it again
        room_rom = entry.get("rom_path")
        if room_rom and os.path.exists(room_rom):
            self.cfg["rom_path"] = room_rom
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
                self._flush_outbox()             # send anything queued while down
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
        """Send an action over the board socket. If the socket is momentarily down
        (reconnecting), queue it and flush on reconnect instead of silently dropping
        it — otherwise a click during a hiccup just does nothing."""
        data = json.dumps(obj)
        with self.ui_lock:
            if self.ui_conn:
                try:
                    self.ui_conn.send(data)
                    return
                except Exception:
                    self.ui_conn = None          # broke mid-send; fall through to queue
        self._ui_outbox.put(data)

    def _flush_outbox(self):
        """Send anything queued while disconnected. Called right after (re)connect."""
        while True:
            try:
                data = self._ui_outbox.get_nowait()
            except queue.Empty:
                return
            with self.ui_lock:
                if not self.ui_conn:
                    self._ui_outbox.put(data)    # lost it again — requeue, retry next connect
                    return
                try:
                    self.ui_conn.send(data)
                except Exception:
                    self.ui_conn = None
                    self._ui_outbox.put(data)
                    return

    # ── step 2: room (board + emulator + controls) ──────────────────────────
    def show_room(self):
        self._clear_body()
        self._set_who(f"{self.cfg.get('display','Player')} · room {self.room['code']}")
        f = self.body

        top = tk.Frame(f, bg=BG); top.pack(fill="x", pady=(0, 10))
        titleblock = tk.Frame(top, bg=BG); titleblock.pack(side="left", fill="x", expand=True)
        self._eyebrow(titleblock, "Live room").pack(anchor="w")
        titleline = tk.Frame(titleblock, bg=BG); titleline.pack(fill="x", pady=(2, 0))
        self.room_title_lbl = tk.Label(titleline, text=self._room_title(), fg=INK, bg=BG,
                                       font=(self.logo_font, 17, "bold"))
        self.room_title_lbl.pack(side="left")
        codebox = tk.Frame(titleline, bg=PANEL2, highlightbackground=LINE, highlightthickness=1)
        codebox.pack(side="left", padx=10)
        tk.Label(codebox, text=self.room["code"], fg=ACCENT, bg=PANEL2,
                 font=(self.mono_font, 9)).pack(side="left", padx=(9, 4), pady=4)
        tk.Button(codebox, text="copy", command=lambda: self._copy_text(self.room["code"], "Room code copied"),
                  bg=PANEL2, fg=MUTED, activebackground=LINE, activeforeground=INK,
                  relief="flat", bd=0, cursor="hand2", font=("Segoe UI Semibold", 8),
                  padx=6, pady=1).pack(side="left", padx=(0, 3))
        # Rename button sits by the title; shown only to the host (in _render_board)
        self.rename_btn = self._button(titleline, "Rename", self._rename_room, small=True)

        actions = tk.Frame(top, bg=BG); actions.pack(side="right", anchor="n")
        self._button(actions, "Open spectator",
                     lambda: webbrowser.open(
                         f"{self.base}/?watch={self.room.get('pub_id') or self.room['code']}"),
                     small=True).pack(side="right")
        self._button(actions, "Leave room", self._leave, small=True).pack(side="right", padx=6)

        # compact play connection strip
        emu = self._panel(f, fill="x", pady=(0, 8))
        connect_actions = tk.Frame(emu, bg=PANEL); connect_actions.pack(side="right", padx=14, pady=12)
        self.btn_connect = self._button(connect_actions, "Connect & Play", self._toggle_connect, primary=True)
        self.btn_connect.pack(fill="x")
        main_tools = tk.Frame(connect_actions, bg=PANEL); main_tools.pack(fill="x", pady=(6, 0))
        self._button(main_tools, "Launch", self.launch_emulator, small=True).pack(side="left")
        self._button(main_tools, "Generate seed", self.generate_seed, small=True).pack(side="left", padx=5)

        connect_info = tk.Frame(emu, bg=PANEL); connect_info.pack(side="left", fill="both", expand=True,
                                                                  padx=14, pady=11)
        self._eyebrow(connect_info, "Play connection", bg=PANEL).pack(anchor="w")
        self.emu_summary_lbl = tk.Label(connect_info, text=self._emu_summary(), fg=INK, bg=PANEL,
                                        font=("Segoe UI Semibold", 11), anchor="w")
        self.emu_summary_lbl.pack(fill="x", pady=(2, 2))
        self.emu_line = tk.Label(connect_info, text="", fg=MUTED, bg=PANEL, font=("Segoe UI", 9),
                                 anchor="w")
        self.emu_line.pack(fill="x")

        # which emulator to link to (auto, or pin a specific open one — useful when
        # two emulators run on one PC, e.g. local two-player testing)
        bindrow = tk.Frame(connect_info, bg=PANEL); bindrow.pack(fill="x", pady=(7, 0))
        self._field_label(bindrow, "BIND TO").pack(side="left", padx=(0, 7))
        self.emu_pin_var = tk.StringVar(value="Auto-detect")
        self.emu_pin_combo = ttk.Combobox(bindrow, textvariable=self.emu_pin_var, state="readonly",
                                          style="HL.TCombobox", width=26, values=["Auto-detect"])
        self.emu_pin_combo.pack(side="left")
        self.emu_pin_combo.bind("<<ComboboxSelected>>", self._on_pick_emu)
        self._button(bindrow, "Refresh", self._refresh_emu_picker, small=True).pack(side="left", padx=6)
        self._emu_endpoints = []
        self._refresh_emu_picker()

        tools = tk.Frame(connect_info, bg=PANEL); tools.pack(fill="x", pady=(7, 0))
        self._button(tools, "Start SNI", self._use_sni, small=True).pack(side="left")
        self._button(tools, "Configure", self._configure_emulator, small=True).pack(side="left", padx=5)
        self._button(tools, "Emulator help", self._show_emu_help, small=True).pack(side="left")

        # player roster
        roster = tk.Frame(f, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        roster.pack(fill="x", pady=(0, 8))
        tk.Label(roster, text="PLAYERS", fg=MUTED, bg=PANEL,
                 font=(self.mono_font, 8)).pack(side="left", padx=(11, 8), pady=9)
        self.players_row = tk.Frame(roster, bg=PANEL); self.players_row.pack(side="left", fill="x", expand=True)

        # host controls (shown only to host)
        self.host_bar = tk.Frame(f, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        self._build_host_bar()

        # game-mode banner (shown to everyone when a shuffle mode is active)
        self.mode_banner = tk.Label(f, text="", bg=PANEL2, fg=GOLD, anchor="w",
                                    font=("Segoe UI Semibold", 10), padx=12, pady=8)

        self.board_head = tk.Frame(f, bg=BG); self.board_head.pack(fill="x", pady=(2, 6))
        tk.Label(self.board_head, text="Shared inventory", fg=INK, bg=BG,
                 font=(self.logo_font, 12, "bold")).pack(side="left")
        tk.Label(self.board_head, text=f"{len(ITEMS)} progression items", fg=MUTED, bg=BG,
                 font=("Segoe UI", 8)).pack(side="left", padx=10)
        tk.Label(self.board_head, text="VIOLET  yours     BLUE  another player     DIM  undiscovered",
                 fg=MUTED, bg=BG, font=(self.mono_font, 7)).pack(side="right")

        # the board (responsive grid: fills width, reflows columns to stay wide)
        self._board_sig = None       # fresh, empty board — force the first render
        self.board_wrap = tk.Frame(f, bg=BG); self.board_wrap.pack(fill="both", expand=True)
        board_wrap = self.board_wrap
        self.canvas = tk.Canvas(board_wrap, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(board_wrap, orient="vertical", command=self.canvas.yview,
                           style="HL.Vertical.TScrollbar")
        self.board = tk.Frame(self.canvas, bg=BG)
        self.board.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._board_window = self.canvas.create_window((0, 0), window=self.board, anchor="nw")
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta/120), "units"))

        # compact activity console
        activity = tk.Frame(f, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        activity.pack(fill="x", pady=(8, 0))
        tk.Label(activity, text="ACTIVITY", fg=MUTED, bg=PANEL,
                 font=(self.mono_font, 8)).pack(side="left", padx=10)
        self.logbox = tk.Text(activity, height=2, bg=PANEL, fg=GREEN, relief="flat",
                              font=(self.mono_font, 8), highlightthickness=0, padx=5, pady=5)
        self.logbox.pack(side="left", fill="x", expand=True)
        self.logbox.configure(state="disabled")
        self.toast_lbl = tk.Label(activity, text="", fg=GREEN, bg=PANEL,
                                  font=("Segoe UI", 8))
        self.toast_lbl.pack(side="right", padx=10)

        if self.state:
            self._render_board()

    def _build_host_bar(self):
        for w in self.host_bar.winfo_children():
            w.destroy()
        # collapsible header
        hdr = tk.Frame(self.host_bar, bg=PANEL); hdr.pack(fill="x", padx=8, pady=5)
        self._host_toggle = self._button(hdr, "", self._toggle_host_controls, small=True)
        self._host_toggle.pack(side="left")
        tk.Label(hdr, text="Right-click an item → give it to a player",
                 fg=MUTED, bg=PANEL, font=("Segoe UI", 8)).pack(side="right", padx=6)

        # the controls themselves live in a frame we can hide
        self.host_ctrls = tk.Frame(self.host_bar, bg=PANEL)
        c = self.host_ctrls
        # mode selector first; its single timing parameter sits right after it.
        self._field_label(c, "GAME MODE").pack(side="left", padx=(2, 6))
        self.mode_var = tk.StringVar(value="normal")
        cb = ttk.Combobox(c, textvariable=self.mode_var, state="readonly", width=11,
                          values=["normal", "hot_potato", "chaos", "custom"],
                          style="HL.TCombobox")
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._on_mode_pick())
        # steal cooldown — Normal only (you can claim/steal there)
        self.cd_group = tk.Frame(c, bg=BG)
        self.cd_group.configure(bg=PANEL)
        tk.Label(self.cd_group, text="steal cooldown", fg=MUTED, bg=PANEL,
                 font=("Segoe UI", 9)).pack(side="left", padx=(8, 2))
        self.e_cd = self._entry(self.cd_group); self.e_cd.configure(width=4); self.e_cd.pack(side="left")
        tk.Label(self.cd_group, text="s", fg=MUTED, bg=PANEL, font=("Segoe UI", 9)).pack(side="left", padx=(2, 0))
        self.cd_group.pack(side="left")
        # shuffle interval — hot potato / chaos only
        self.shuffle_group = tk.Frame(c, bg=PANEL)
        tk.Label(self.shuffle_group, text="shuffle every", fg=MUTED, bg=PANEL,
                 font=("Segoe UI", 9)).pack(side="left", padx=(8, 2))
        self.e_shuffle = self._entry(self.shuffle_group); self.e_shuffle.configure(width=4)
        self.e_shuffle.pack(side="left")
        tk.Label(self.shuffle_group, text="s", fg=MUTED, bg=PANEL, font=("Segoe UI", 9)).pack(side="left", padx=(2, 0))
        self.shuffle_group.pack(side="left")
        self._apply_btn = self._button(c, "Apply", self._apply_host, small=True)
        self._apply_btn.pack(side="left", padx=8)
        self._custom_btn = self._button(c, "Customize ruleset…", self._open_rules, small=True)
        # packed/unpacked by _sync_mode_fields
        self._sync_mode_fields()
        self._apply_host_collapse()

    def _sync_mode_fields(self):
        """Show only the control the chosen mode uses: cooldown (Normal), shuffle
        interval (shuffle presets), or the Customize button (Custom). The Apply
        button stays put so it never reflows past the trailing hint."""
        if not hasattr(self, "cd_group"):
            return
        m = self.mode_var.get()
        self.cd_group.pack_forget()
        self.shuffle_group.pack_forget()
        self._custom_btn.pack_forget()
        if m == "custom":
            self._custom_btn.pack(side="left", before=self._apply_btn)
        elif m == "normal":
            self.cd_group.pack(side="left", before=self._apply_btn)
        else:
            self.shuffle_group.pack(side="left", before=self._apply_btn)

    def _on_mode_pick(self):
        self._sync_mode_fields()
        if self.mode_var.get() == "custom":
            self._open_rules()

    # ── custom ruleset editor ────────────────────────────────────────────────
    def _open_rules(self):
        win = getattr(self, "_rules_win", None)
        if win is not None and win.winfo_exists():
            win.lift(); return
        win = tk.Toplevel(self); win.title("Custom ruleset"); win.configure(bg=BG)
        win.transient(self); win.geometry("720x760"); win.minsize(660, 700)
        self._rules_win = win
        win.protocol("WM_DELETE_WINDOW", lambda: (setattr(self, "_rules_win", None), win.destroy()))

        head = tk.Frame(win, bg=BG); head.pack(fill="x", padx=18, pady=(14, 10))
        self._eyebrow(head, "Host tools").pack(anchor="w")
        tk.Label(head, text="Custom ruleset", fg=INK, bg=BG,
                 font=(self.logo_font, 16, "bold")).pack(anchor="w", pady=(2, 2))
        tk.Label(head, text="Compose how claiming, leases, raids, and shuffles interact.",
                 fg=MUTED, bg=BG, font=("Segoe UI", 9)).pack(anchor="w")

        presets = tk.Frame(win, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        presets.pack(fill="x", padx=18, pady=(0, 10))
        self._field_label(presets, "START FROM A PRESET").grid(
            row=0, column=0, columnspan=4, sticky="w", padx=10, pady=(8, 5))
        for name in ("normal", "hot_potato", "chaos", "cutthroat", "lease", "raid", "siege"):
            idx = ("normal", "hot_potato", "chaos", "cutthroat", "lease", "raid", "siege").index(name)
            label = name.replace("_", " ").title()
            self._button(presets, label,
                         lambda n=name: self._fill_rules({**RULE_DEFAULTS, **RULE_PRESETS[n]}),
                         small=True).grid(row=1 + idx // 4, column=idx % 4, sticky="ew",
                                          padx=5, pady=(0, 7))
        for col in range(4):
            presets.grid_columnconfigure(col, weight=1, uniform="presets")

        self._rules_summary_lbl = tk.Label(
            win, text="", fg=INK, bg=PANEL, font=("Segoe UI", 9), wraplength=560,
            justify="left", anchor="w", highlightbackground=LINE, highlightthickness=1, padx=8, pady=6)
        self._rules_summary_lbl.pack(fill="x", padx=18, pady=(0, 10))

        body = tk.Frame(win, bg=BG); body.pack(fill="both", expand=True, padx=18)
        self._rule_vars = {}
        for title, fields in RULE_FIELDS:
            sec = tk.Frame(body, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
            sec.pack(fill="x", pady=(0, 7))
            tk.Label(sec, text=title, fg=ACCENT, bg=PANEL,
                     font=("Segoe UI Semibold", 9)).pack(anchor="w", padx=8, pady=(6, 2))
            for key, kind, label, choices in fields:
                row = tk.Frame(sec, bg=PANEL); row.pack(fill="x", padx=10, pady=2)
                if kind == "bool":
                    var = tk.BooleanVar()
                    tk.Checkbutton(row, text=label, variable=var, fg=INK, bg=PANEL, selectcolor=PANEL2,
                                   activebackground=PANEL, activeforeground=INK, font=("Segoe UI", 9),
                                   command=self._refresh_rules).pack(side="left")
                elif kind == "enum":
                    tk.Label(row, text=label, fg=MUTED, bg=PANEL, font=("Segoe UI", 9),
                             width=24, anchor="w").pack(side="left")
                    var = tk.StringVar()
                    cbx = ttk.Combobox(row, textvariable=var, state="readonly", width=16,
                                       values=choices, style="HL.TCombobox")
                    cbx.pack(side="left", padx=6)
                    cbx.bind("<<ComboboxSelected>>", lambda e: self._refresh_rules())
                else:                                       # numeric
                    tk.Label(row, text=label, fg=MUTED, bg=PANEL, font=("Segoe UI", 9),
                             width=24, anchor="w").pack(side="left")
                    var = tk.StringVar()
                    ent = self._entry(row); ent.configure(width=6, textvariable=var); ent.pack(side="left", padx=6)
                    var.trace_add("write", lambda *a: self._refresh_rules())
                self._rule_vars[key] = (kind, var)

        af = tk.Frame(win, bg=BG); af.pack(fill="x", padx=18, pady=12)
        self._button(af, "Apply ruleset", self._apply_rules, primary=True).pack(side="right")
        self._button(af, "Cancel",
                     lambda: (setattr(self, "_rules_win", None), win.destroy())).pack(side="right", padx=6)

        self._fill_rules(self.state.get("rules") or dict(RULE_DEFAULTS))

    def _fill_rules(self, r):
        r = {**RULE_DEFAULTS, **(r or {})}
        for key, (kind, var) in self._rule_vars.items():
            v = r.get(key, RULE_DEFAULTS.get(key))
            if kind == "bool":
                var.set(bool(v))
            elif kind == "enum":
                var.set(str(v))
            else:
                try:
                    var.set(str(int(float(v))))
                except (TypeError, ValueError):
                    var.set("0")
        self._refresh_rules()

    def _read_rules(self):
        out = {}
        for key, (kind, var) in self._rule_vars.items():
            if kind == "bool":
                out[key] = bool(var.get())
            elif kind == "enum":
                out[key] = var.get()
            else:
                try:
                    out[key] = float(var.get() or 0)
                except ValueError:
                    out[key] = 0
        return out

    def _refresh_rules(self):
        lbl = getattr(self, "_rules_summary_lbl", None)
        if lbl is not None and lbl.winfo_exists():
            lbl.config(text=summarize_rules(self._read_rules()) or "—")

    def _apply_rules(self):
        self._ui_send({"type": "admin_set_rules", "rules": self._read_rules()})
        win = getattr(self, "_rules_win", None)
        if win is not None and win.winfo_exists():
            win.destroy()
        self._rules_win = None

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
            self._host_toggle.config(text="▸  Host settings")
        else:
            self.host_ctrls.pack(fill="x", padx=10, pady=(2, 10))
            self._host_toggle.config(text="▾  Host settings")

    def _room_title(self):
        name = (self.state or {}).get("name") or self.room.get("name") or "Co-op"
        return name

    def _rename_room(self):
        from tkinter import simpledialog
        cur = (self.state or {}).get("name") or self.room.get("name", "")
        new = simpledialog.askstring("Rename room", "New room name:",
                                     initialvalue=cur, parent=self)
        if new and new.strip():
            self._ui_send({"type": "admin_set_name", "name": new.strip()})

    def _is_host(self):
        return bool(self.state) and self.state.get("you") == self.state.get("host")

    def _apply_host(self):
        """Apply the mode + its one timing field, sending only what actually
        changed — re-sending admin_set_mode for the same mode would reset the
        shuffle timer and spam an event, so we guard against no-op re-applies."""
        st = self.state or {}
        mode = self.mode_var.get()
        if mode == "custom":            # custom is driven by the rules popup, not here
            self._open_rules()
            return
        try:
            shuffle = float(self.e_shuffle.get() or 120)
        except ValueError:
            shuffle = 120.0
        try:
            cooldown = float(self.e_cd.get() or 0)
        except ValueError:
            cooldown = 0.0
        mode_changed = mode != st.get("mode", "normal")
        shuffle_changed = mode != "normal" and round(shuffle) != round(st.get("shuffle_s", 120))
        if mode_changed or shuffle_changed:
            self._ui_send({"type": "admin_set_mode", "mode": mode, "seconds": shuffle})
        if mode == "normal" and round(cooldown) != round(st.get("cooldown_s", 0)):
            self._ui_send({"type": "admin_set_cooldown", "seconds": cooldown})

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
        summary = self.state.get("rules_summary", "")
        rem = self.state.get("shuffle_remaining", 0)
        nxt = f" · next reshuffle in {self._clock(rem)}" if rem > 0 else ""
        icon, color = {"chaos": ("🌀", ACCENT2), "hot_potato": ("🔥", GOLD),
                       "custom": ("🎛️", ACCENT)}.get(mode, ("🎛️", ACCENT))
        label = {"chaos": "Chaos", "hot_potato": "Hot Potato", "custom": "Custom"}.get(mode, "Custom")
        self.mode_banner.config(text=f"{icon} {label} — {summary}{nxt}",
                                fg=color, highlightbackground=color)
        self.mode_banner.pack(fill="x", pady=(0, 7), before=self.board_head)

    # ── board rendering ─────────────────────────────────────────────────────
    def _render_players(self):
        for w in self.players_row.winfo_children():
            w.destroy()
        host = self._is_host()
        for p in self.state.get("players", []):
            color = GREEN if (p.get("agent") and p.get("emu")) else (
                GOLD if p.get("agent") else DIM)
            chip = tk.Frame(self.players_row, bg=PANEL2, highlightbackground=LINE,
                            highlightthickness=1)
            chip.pack(side="left", padx=(0, 7), pady=5)
            c = tk.Canvas(chip, width=12, height=12, bg=PANEL2, highlightthickness=0)
            c.pack(side="left", padx=(7, 2))
            self._dot(c, color)
            av = self._avatar_image(p.get("avatar"), 18)
            if av is not None:
                al = tk.Label(chip, image=av, bg=PANEL2); al.image = av
                al.pack(side="left", padx=(3, 3))
            is_room_host = p["id"] == self.state.get("host")
            is_you = p["id"] == self.state.get("you")
            suffix = "  YOU" if is_you else ("  HOST" if is_room_host else "")
            lbl = tk.Label(chip, text=f"{p['name']}{suffix}", fg=INK, bg=PANEL2,
                           font=("Segoe UI Semibold", 8))
            lbl.pack(side="left", padx=(3, 7), pady=5)
            if host and p["id"] != self.state.get("host"):
                remove = tk.Button(chip, text="×", relief="flat", bd=0, cursor="hand2",
                                   bg=PANEL2, fg=MUTED, activebackground=RED,
                                   activeforeground="#ffffff", font=("Segoe UI", 10), padx=5,
                                   command=lambda pid=p["id"], nm=p["name"]: self._remove_player(pid, nm))
                remove.pack(side="right", padx=(0, 2))

    def _remove_player(self, pid, name):
        if messagebox.askyesno("HyruleLink", f"Remove {name} from the room?"):
            self._ui_send({"type": "admin_remove_player", "player_id": pid})

    # ── host: per-item found/owner management (right-click an item) ───────────
    def _bind_recursive(self, widget, sequence, func, add=None):
        widget.bind(sequence, func, add=add)
        for ch in widget.winfo_children():
            self._bind_recursive(ch, sequence, func, add=add)

    def _item_menu(self, event, key, name):
        """Host-only right-click menu: give the item to a player (or take it away)
        in ONE click. "Give to X" sends admin_set_owner, which the server applies
        in-game immediately and auto-marks discovered — so there's no separate
        "found" step. The per-player discovery toggles live in a "Found by…"
        submenu for the rare discovery-gated case (fixing state after a disconnect)."""
        if not self._is_host():
            return
        it = (self.state or {}).get("ledger", {}).get(key, {})
        owner = it.get("owner")
        discovered = set(it.get("discovered", []))
        players = (self.state or {}).get("players", [])
        host_id = (self.state or {}).get("host")
        cur_level = it.get("level")
        defn = BY_KEY.get(key)
        # Multi-tier items (sword/shield/mail/gloves/magic/bow) let the host pick
        # the tier, so they can restore e.g. a Gold Sword after a reset/disconnect
        # wiped the ledger's memory of what the player had found.
        tiered = bool(defn and defn.cap > 1)

        def menu():
            return tk.Menu(self, tearoff=0, bg=PANEL, fg=INK, bd=0,
                           activebackground=PRIMARY, activeforeground="#ffffff",
                           font=("Segoe UI", 10))

        m = menu()
        m.add_command(label=name, state="disabled")
        m.add_separator()
        if not players:
            m.add_command(label="No players in the room yet", state="disabled")
        for p in players:
            pid = p["id"]
            star = "★ " if pid == host_id else ""
            if tiered:
                tsub = menu()
                for lvl in range(defn.present, defn.cap + 1):
                    tname = (defn.tiers[lvl] if 0 <= lvl < len(defn.tiers)
                             else f"level {lvl}")
                    mark = "● " if (pid == owner and cur_level == lvl) else "    "
                    tsub.add_command(
                        label=f"{mark}{tname}",
                        command=lambda _pid=pid, _lvl=lvl: self._ui_send(
                            {"type": "admin_set_owner", "item": key,
                             "player_id": _pid, "level": _lvl}))
                m.add_cascade(label=f"Give to {star}{p['name']}", menu=tsub)
            else:
                held = "  ●" if pid == owner else ""
                m.add_command(
                    label=f"Give to {star}{p['name']}{held}",
                    state=("disabled" if pid == owner else "normal"),
                    command=lambda _pid=pid: self._ui_send(
                        {"type": "admin_set_owner", "item": key, "player_id": _pid}))
        m.add_separator()
        m.add_command(
            label="Take away (nobody holds it)",
            state=("normal" if owner is not None else "disabled"),
            command=lambda: self._ui_send(
                {"type": "admin_set_owner", "item": key, "player_id": None}))
        if players:
            # "Found by…" sets the discovered tier WITHOUT granting it (no item
            # appears in their game). For a progressive item the found tier is the
            # tier they'd get when they claim it, so multi-tier items expose the
            # tier list here too; single-tier items stay a plain found/un-found toggle.
            disc_levels = it.get("discovered_levels", {})
            sub = menu()
            for p in players:
                pid = p["id"]
                has = pid in discovered
                if tiered:
                    fsub = menu()
                    cur = disc_levels.get(str(pid))
                    fsub.add_command(
                        label=("● " if not has else "    ") + "(not found)",
                        command=lambda _pid=pid: self._ui_send(
                            {"type": "admin_set_discovered", "item": key,
                             "player_id": _pid, "found": False}))
                    fsub.add_separator()
                    for lvl in range(defn.present, defn.cap + 1):
                        tname = (defn.tiers[lvl] if 0 <= lvl < len(defn.tiers)
                                 else f"level {lvl}")
                        mark = "● " if (has and cur == lvl) else "    "
                        fsub.add_command(
                            label=f"{mark}{tname}",
                            command=lambda _pid=pid, _lvl=lvl: self._ui_send(
                                {"type": "admin_set_discovered", "item": key,
                                 "player_id": _pid, "found": True, "level": _lvl}))
                    sub.add_cascade(label=f"{'✓ ' if has else '    '}{p['name']}", menu=fsub)
                else:
                    sub.add_command(
                        label=f"{'✓ ' if has else '    '}{p['name']}",
                        command=lambda _pid=pid, _was=has: self._ui_send(
                            {"type": "admin_set_discovered", "item": key,
                             "player_id": _pid, "found": not _was}))
            m.add_cascade(label="Found by…", menu=sub)
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _board_signature(self):
        """A cheap fingerprint of everything _render_board draws. Lets us skip the
        full destroy/rebuild when a state push didn't actually change the board —
        which is what made the app look like it was constantly reloading."""
        s = self.state or {}
        you = s.get("you")
        mode = s.get("mode", "normal")
        parts = [you, mode, self._is_host(), self._board_columns(),
                 self._room_title(), self._avatar_version,
                 s.get("claiming"), s.get("rules_summary")]   # rules affect the cards
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
                    round(e.get("hold_remaining", 0) or 0) if e.get("hold_remaining") is not None else None,
                    round(e.get("borrow_remaining", 0) or 0) if e.get("borrow_remaining") is not None else None,
                    e.get("locked"),
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
            self.host_bar.pack(fill="x", pady=(0, 8), before=self.board_head)
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
                                                 padx=4, pady=4, sticky="nsew")
        # equal-width columns that stretch to fill the canvas (wider, fewer rows)
        for c in range(cols):
            self.board.grid_columnconfigure(c, weight=1, minsize=CARD_MIN_PX, uniform="items")
        for c in range(cols, 12):                 # clear any columns from a wider layout
            self.board.grid_columnconfigure(c, weight=0, minsize=0, uniform="")

    def _board_columns(self):
        """How many item columns fit the current board width — favouring a wide,
        short grid. Falls back to a sensible count before the canvas is sized."""
        w = self.canvas.winfo_width() if hasattr(self, "canvas") else 0
        if w <= 1:
            w = 1040                              # pre-layout default (window width-ish)
        return max(4, min(8, w // CARD_MIN_PX))

    def _on_canvas_resize(self, event):
        # keep the grid as wide as the viewport, and reflow columns when the count
        # changes so the board stays wider-than-tall as the window resizes.
        self.canvas.itemconfig(self._board_window, width=event.width)
        if self.state and self._board_columns() != self._board_cols:
            self._render_board()

    def _card(self, parent, cat, e):
        you = self.state.get("you")
        mine = e and e.get("owner") == you
        owned = bool(e and e.get("owner"))
        bg = CARD_MINE if mine else (CARD_OWNED if owned else CARD)
        border = ACCENT if mine else (BLUE if owned else LINE)
        card = tk.Frame(parent, bg=bg, height=CARD_H, highlightbackground=border,
                        highlightthickness=2 if mine else 1)
        card.pack_propagate(False)
        tk.Frame(card, bg=border if e else LINE, height=2).pack(fill="x")

        level = e.get("level", 0) if e else 0
        icon = self._item_icon(item_image(cat["key"], level), dim=not e)
        if icon is not None:
            il = tk.Label(card, image=icon, bg=bg)
            il.image = icon
            il.pack(pady=(6, 1))
        tk.Label(card, text=cat["name"], fg=INK if e else MUTED, bg=bg,
                 font=("Segoe UI Semibold", 9), wraplength=CARD_MIN_PX - 12,
                 justify="center").pack(fill="x", padx=6)

        owner_av = self._avatar_image(self._player_avatar_url(e.get("owner")), 14) if e else None
        meta = tk.Frame(card, bg=bg); meta.pack(fill="x", padx=5, pady=(1, 0))
        inner = tk.Frame(meta, bg=bg); inner.pack()
        if owner_av is not None:
            al = tk.Label(inner, image=owner_av, bg=bg); al.image = owner_av
            al.pack(side="left", padx=(0, 3))
        if not e:
            meta_text = "Not discovered"
        else:
            owner = e.get("owner_name") or "Unowned"
            tier = e.get("tier") if e.get("tier") and e.get("tier") != "—" else ""
            meta_text = f"{tier} · {owner}" if tier else owner
        tk.Label(inner, text=meta_text, fg=MUTED, bg=bg, font=("Segoe UI", 8)).pack(side="left")

        mode = self.state.get("mode", "normal")
        claiming = self.state.get("claiming", mode == "normal")
        rules = self.state.get("rules", {})
        need_found = rules.get("require_found_to_claim", True)
        shared_found = bool(e and rules.get("shared_discovery") and e.get("discovered"))
        found_for_claim = bool(e and (you in e.get("discovered", []) or shared_found))
        open_scope = rules.get("open_season_scope", "owned")
        action = tk.Frame(card, bg=bg); action.pack(side="bottom", fill="x", padx=7, pady=(2, 7))
        action_text, action_color, button_label = "", MUTED, None
        if not e:
            if claiming and not need_found and open_scope == "any":
                button_label = "Claim"
            else:
                action_text = "—"
        elif not claiming:
            if mine:
                action_text, action_color = "✓  YOURS", GREEN
            elif not e.get("owner"):
                action_text = "—"
        elif mine:
            action_text, action_color = "✓  YOURS", GREEN
        elif e.get("locked"):
            action_text, action_color = "🔒  SECURED", GOLD
        elif ((need_found and not found_for_claim)
              or (not need_found and open_scope == "owned"
                  and not e.get("owner") and not found_for_claim)):
            action_text, action_color = "FIND ONE TO CLAIM", GOLD
        elif e.get("cooldown_remaining", 0) > 0.05:
            action_text = f"COOLDOWN  {e['cooldown_remaining']:.0f}s"
        else:
            button_label = "Steal" if (not need_found and e.get("owner") and not mine) else "Claim"

        if e and e.get("owner") and e.get("hold_remaining") is not None:
            action_text, action_color = f"⏱  {self._clock(e['hold_remaining'])}", GOLD
            button_label = None
        if e and e.get("borrow_remaining") is not None:
            action_text, action_color = f"⏳  {self._clock(e['borrow_remaining'])}", ACCENT2
            button_label = None

        if button_label:
            self._button(action, button_label,
                         lambda k=cat["key"]: self._ui_send({"type": "claim", "item": k}),
                         primary=True, small=True).pack(fill="x")
        else:
            tk.Label(action, text=action_text or " ", fg=action_color, bg=bg,
                     font=(self.mono_font, 7)).pack(fill="x")

        norm = border
        hover = _lighten(norm, 0.32)

        def _hov_on(_):
            card._hov = True
            card.configure(highlightbackground=hover)

        def _hov_off(_):
            card._hov = False
            card.after(40, lambda: card.winfo_exists() and not getattr(card, "_hov", False)
                       and card.configure(highlightbackground=norm))
        self._bind_recursive(card, "<Enter>", _hov_on, add="+")
        self._bind_recursive(card, "<Leave>", _hov_off, add="+")
        # host: right-click anywhere on a card → one-click give/take menu
        if self._is_host():
            card.configure(cursor="hand2")
            self._bind_recursive(
                card, "<Button-3>",
                lambda ev, k=cat["key"], n=cat["name"]: self._item_menu(ev, k, n))
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

    # ── installed SNI bridge (snes9x-rr / BizHawk / real hardware) ──────────
    def _sni_exe(self):
        import shutil
        bundled = os.path.join(SNI_DIR, "sni.exe")
        if os.path.exists(bundled):
            return bundled
        return shutil.which("sni") or (r"C:\SNI\sni.exe" if os.path.exists(r"C:\SNI\sni.exe") else None)

    def _bridge_running(self):
        """Is a QUsb2Snes/SNI bridge already listening on the default port?"""
        try:
            socket.create_connection(("127.0.0.1", SNI_BRIDGE_PORT), timeout=0.4).close()
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
            messagebox.showwarning("HyruleLink", "Couldn't find the installed SNI. Run Install.cmd, "
                                   "or run SNI/QUsb2Snes manually, then press Connect & Play.")
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

    # ── emulator picker (see what's open, pin one) ──────────────────────────
    def _refresh_emu_picker(self):
        """Re-scan open emulators on a worker thread; repopulate the Bind-to list."""
        def work():
            found = scan_emulators()
            self.after(0, lambda: self._populate_emu_picker(found))
        threading.Thread(target=work, daemon=True).start()

    def _populate_emu_picker(self, found):
        if not hasattr(self, "emu_pin_combo") or not self.emu_pin_combo.winfo_exists():
            return
        self._emu_endpoints = found
        labels = ["Auto-detect"] + [f["label"] for f in found]
        pin = self.cfg.get("emu_pin", "auto")
        sel = "Auto-detect"
        if isinstance(pin, dict):
            sel = next((f["label"] for f in found if f["bind"] == {k: v for k, v in pin.items()
                                                                   if k != "label"}), None)
            if sel is None:                       # pinned source isn't open right now
                sel = pin.get("label", "Auto-detect")
                if sel not in labels:
                    labels.append(sel + "  (offline)")
                    sel = sel + "  (offline)"
        self.emu_pin_combo.config(values=labels)
        self.emu_pin_var.set(sel)

    def _on_pick_emu(self, _e=None):
        label = self.emu_pin_var.get()
        if label == "Auto-detect":
            self.cfg["emu_pin"] = "auto"
        else:
            for f in getattr(self, "_emu_endpoints", []):
                if f["label"] == label:
                    self.cfg["emu_pin"] = {**f["bind"], "label": label}
                    break
        save_settings(self.cfg)
        self._log(f"Emulator bind: {label}")

    # ── emulator agent link ─────────────────────────────────────────────────
    def _toggle_connect(self):
        self._disconnect() if self.agent else self._connect()

    def _resolve_transport(self, auto=False):
        """Build (transport, label) from the pinned choice, or auto-detect.

        Returns (None, None) if the user declined to start a source when nothing
        was detected (auto mode only). When `auto` is set (an automatic link
        attempt), a "not detected" result returns quietly instead of prompting —
        the detect loop will try again when an emulator appears."""
        from agent.sni.emu_connector import EmuConnector
        from agent.sni.qusb2snes_tracker import QUsb2SnesTracker
        pin = self.cfg.get("emu_pin", "auto")
        if isinstance(pin, dict):
            if pin.get("transport") == "hardware":
                return QUsb2SnesTracker(), pin.get("label", "SNI bridge")
            return (EmuConnector(source=pin.get("source"), port=pin.get("port")),
                    pin.get("label") or pin.get("source") or "emulator")
        # auto
        transport, source, label = detect_emulator()
        if label == "not detected":
            if auto:
                return None, None
            if messagebox.askyesno("HyruleLink",
                "No emulator detected.\n\nStart the SNI bridge now? It's needed for snes9x-rr, "
                "BizHawk, or real hardware.\n\n(snes9x-nwa and RetroArch connect directly — if "
                "you're using one of those, click No and just make sure it's running.)"):
                self._use_sni()
                transport = "hardware"; label = "SNI bridge"
            else:
                return None, None
        if transport == "hardware":
            return QUsb2SnesTracker(), label
        return EmuConnector(source=source), label

    def _connect(self, auto=False):
        from agent.agent import HyruleAgent
        transport, label = self._resolve_transport(auto=auto)
        if transport is None:
            return
        self.transport = transport
        h = QueueLogHandler(self.log_q); h.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger().addHandler(h); logging.getLogger().setLevel(logging.INFO)
        self.agent = HyruleAgent(self.transport, self._ws_url(), self.room["code"],
                                 self.room["player_id"], self.room["player_token"], poll_interval=0.5)
        self.agent.start()
        self.btn_connect.config(text="Disconnect", bg=PANEL2, fg=INK)
        self._log(f"Linking emulator via {label}…")
        threading.Thread(target=self._greet_emu, daemon=True).start()

    def _greet_emu(self):
        """Once the emulator link is live, flash a one-time OSD so the player sees
        notifications work (RetroArch only; a no-op on transports without an OSD)."""
        for _ in range(40):
            if self.transport is None or self._stop_all.is_set():
                return
            if getattr(self.transport, "connected", False):
                try:
                    self.transport.show_message("HyruleLink linked — items now sync")
                except Exception:
                    pass
                return
            time.sleep(0.5)

    def _disconnect(self):
        self._autolink_armed = False   # a manual disconnect should stay disconnected
        try:
            if self.agent:
                self.agent.stop()
        except Exception:
            pass
        self.agent = None; self.transport = None
        if getattr(self, "btn_connect", None) is not None and self.btn_connect.winfo_exists():
            self.btn_connect.config(text="Connect & Play", bg=PRIMARY, fg="#ffffff")
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
    def _migrate_settings(self):
        """Bring older gui_settings.json up to the current schema (in place)."""
        c = self.cfg
        if "emu_paths" not in c:                    # split the old single emu_path
            paths = {}
            old = c.get("emu_path")
            if old:
                kind = ("retroarch" if (c.get("emu_type") == "retroarch"
                        or "retroarch" in os.path.basename(old).lower()) else "snes9x")
                paths[kind] = old
                c.setdefault("launch_emu", kind)
            c["emu_paths"] = paths
        c.setdefault("patch", dict(DEFAULT_PATCH))
        c.setdefault("msu", {"enable": False, "pack_dir": ""})
        c.setdefault("seed_preset", "Open")
        c.setdefault("emu_pin", "auto")

    def _emu_path(self, kind):
        return (self.cfg.get("emu_paths") or {}).get(kind, "")

    def _launch_kind(self):
        """Which emulator the Launch button starts (explicit choice, else inferred)."""
        k = self.cfg.get("launch_emu")
        if k in ("snes9x", "retroarch"):
            return k
        paths = self.cfg.get("emu_paths") or {}
        if paths.get("retroarch"):
            return "retroarch"
        if paths.get("snes9x"):
            return "snes9x"
        return "retroarch"

    def _remember_room_rom(self, rom):
        """Pin a ROM to the current room so a later Join/Rejoin relaunches the same one."""
        if not rom or self.room is None:
            return
        entry = self.cfg.setdefault("rooms", {}).setdefault(self.room.get("code"), {})
        entry["rom_path"] = rom
        save_settings(self.cfg)

    def _guess_core(self, emu_path):
        if not emu_path:
            return ""
        cand = os.path.join(os.path.dirname(emu_path), "cores", "snes9x_libretro.dll")
        return cand if os.path.exists(cand) else ""

    def _ensure_ra_netcfg(self):
        # Appended over RetroArch's own config at launch (--appendconfig). Besides
        # opening the command port, force on-screen notifications ON: SHOW_MSG
        # (our "stolen from…" / "Items shuffled" OSD) is queued via
        # runloop_msg_queue_push and only renders when video_font_enable is set,
        # so a player who turned notifications off would never see them.
        want = ('network_cmd_enable = "true"\n'
                'network_cmd_port = "55355"\n'
                'video_font_enable = "true"\n'
                'menu_enable_widgets = "true"\n')   # card-style notification popups, not plain text
        path = os.path.join(HERE, "retroarch_net.cfg")
        try:
            current = ""
            if os.path.exists(path):
                with open(path) as f:
                    current = f.read()
            if "menu_enable_widgets" not in current:   # create, or upgrade an older file
                with open(path, "w") as f:
                    f.write(want)
        except Exception:
            pass
        return path

    def _emu_summary(self):
        kind = self._launch_kind()
        emu = self._emu_path(kind)
        rom = self.cfg.get("rom_path")
        head = f"{kind} — {os.path.basename(emu)}" if emu else "emulator not set up"
        return f"{head}  ·  {os.path.basename(rom)}" if rom else head

    def _refresh_emu_summary(self):
        if hasattr(self, "emu_summary_lbl") and self.emu_summary_lbl.winfo_exists():
            self.emu_summary_lbl.config(text=self._emu_summary())

    # ── path-row helper shared by the setup dialogs ─────────────────────────
    def _path_row(self, parent, label, value, kinds=None, directory=False):
        tk.Label(parent, text=label, fg=MUTED, bg=BG, font=("Segoe UI", 9)).pack(
            anchor="w", pady=(8, 0), padx=16)
        row = tk.Frame(parent, bg=BG); row.pack(fill="x", padx=16)
        e = self._entry(row, value=value or ""); e.pack(side="left", fill="x", expand=True, ipady=3)

        def browse():
            p = (filedialog.askdirectory(title=label) if directory
                 else filedialog.askopenfilename(title=label, filetypes=kinds or [("All", "*.*")]))
            if p:
                e.delete(0, "end"); e.insert(0, p)
        tk.Button(row, text="Browse…", command=browse, relief="flat", bg=PANEL, fg=INK,
                  bd=0, padx=10).pack(side="left", padx=(6, 0))
        return e

    # ── in-app seed generation (pyz3r → alttpr.com) ─────────────────────────
    def generate_seed(self):
        self._seed_dialog()

    def _seed_dialog(self):
        win = tk.Toplevel(self); win.title("Generate a seed"); win.configure(bg=BG)
        win.geometry("600x430"); win.transient(self); pad = {"padx": 16}
        tk.Label(win, text="Generate an ALTTPR seed", fg=GOLD, bg=BG,
                 font=("Segoe UI Semibold", 13)).pack(anchor="w", pady=(14, 4), **pad)
        tk.Label(win, text="Patched on alttpr.com (needs internet, ~10s) and set as your ROM.",
                 fg=MUTED, bg=BG, font=("Segoe UI", 9)).pack(anchor="w", **pad)

        tk.Label(win, text="Preset", fg=MUTED, bg=BG, font=("Segoe UI", 9)).pack(
            anchor="w", pady=(10, 0), **pad)
        preset_var = tk.StringVar(value=self.cfg.get("seed_preset", "Open"))
        ttk.Combobox(win, textvariable=preset_var, state="readonly", style="HL.TCombobox",
                     values=list(SEED_PRESET_OVERRIDES)).pack(anchor="w", **pad)

        e_base = self._path_row(win, "ALTTP JP 1.0 base ROM (.sfc/.smc)", self.cfg.get("base_rom", ""),
                                [("SNES ROM", "*.sfc *.smc"), ("All", "*.*")])

        msu = self.cfg.get("msu", {})
        msu_var = tk.BooleanVar(value=msu.get("enable", False))
        tk.Checkbutton(win, text="Use an MSU-1 music pack (turns off in-ROM music)",
                       variable=msu_var, fg=INK, bg=BG, selectcolor=PANEL2, activebackground=BG,
                       activeforeground=INK, font=("Segoe UI", 9), anchor="w").pack(
                           anchor="w", padx=12, pady=(10, 0))
        e_msu = self._path_row(win, "MSU pack folder (its *.pcm tracks are copied next to the seed)",
                               msu.get("pack_dir", ""), directory=True)

        err = tk.Label(win, text="", fg=RED, bg=BG, font=("Segoe UI", 9)); err.pack(anchor="w", pady=(6, 0), **pad)
        bar = tk.Frame(win, bg=BG); bar.pack(anchor="w", pady=12, **pad)

        def gen():
            base = e_base.get().strip()
            if not base or not os.path.exists(base):
                err.config(text="pick your ALTTP JP 1.0 base ROM first"); return
            self.cfg["seed_preset"] = preset_var.get()
            self.cfg["base_rom"] = base
            self.cfg["msu"] = {"enable": msu_var.get(), "pack_dir": e_msu.get().strip()}
            save_settings(self.cfg)
            win.destroy()
            self._log(f"Generating a {preset_var.get()} seed (needs internet, ~10s)…")
            threading.Thread(target=self._generate_seed_thread,
                             args=(base, preset_var.get()), daemon=True).start()
        self._button(bar, "Generate", gen, primary=True).pack(side="left")
        self._button(bar, "Patch / cosmetics…", self._patch_dialog).pack(side="left", padx=8)
        self._button(bar, "Cancel", win.destroy).pack(side="left")

    def _patch_dialog(self):
        win = tk.Toplevel(self); win.title("Patch / cosmetics"); win.configure(bg=BG)
        win.geometry("440x420"); win.transient(self); pad = {"padx": 16}
        tk.Label(win, text="Cosmetic / patch settings", fg=GOLD, bg=BG,
                 font=("Segoe UI Semibold", 13)).pack(anchor="w", pady=(14, 8), **pad)
        p = {**DEFAULT_PATCH, **self.cfg.get("patch", {})}

        def combo(label, var, values):
            tk.Label(win, text=label, fg=MUTED, bg=BG, font=("Segoe UI", 9)).pack(anchor="w", pady=(6, 0), **pad)
            ttk.Combobox(win, textvariable=var, state="readonly", style="HL.TCombobox",
                         values=values).pack(anchor="w", **pad)

        hs = tk.StringVar(value=p["heartspeed"]); combo("Low-health beep", hs, HEARTSPEEDS)
        hc = tk.StringVar(value=p["heartcolor"]); combo("Heart color", hc, HEARTCOLORS)
        ms = tk.StringVar(value=p["menu_speed"]); combo("Menu speed", ms, MENU_SPEEDS)

        tk.Label(win, text="Sprite name (e.g. Link)", fg=MUTED, bg=BG,
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(6, 0), **pad)
        e_sprite = self._entry(win, value=p.get("spritename", "Link"))
        e_sprite.pack(fill="x", ipady=3, **pad)

        qs = tk.BooleanVar(value=p["quickswap"])
        mu = tk.BooleanVar(value=p["music"])
        mr = tk.BooleanVar(value=p["msu1_resume"])
        for text, var in (("Quickswap items (L/R)", qs), ("In-ROM music", mu),
                          ("MSU-1 resume after reset", mr)):
            tk.Checkbutton(win, text=text, variable=var, fg=INK, bg=BG, selectcolor=PANEL2,
                           activebackground=BG, activeforeground=INK, font=("Segoe UI", 9),
                           anchor="w").pack(anchor="w", padx=12, pady=(6, 0))

        def save():
            self.cfg["patch"] = {"heartspeed": hs.get(), "heartcolor": hc.get(),
                                 "menu_speed": ms.get(), "quickswap": qs.get(), "music": mu.get(),
                                 "msu1_resume": mr.get(), "spritename": e_sprite.get().strip() or "Link"}
            save_settings(self.cfg)
            win.destroy()
        bar = tk.Frame(win, bg=BG); bar.pack(anchor="w", pady=12, **pad)
        self._button(bar, "Save", save, primary=True).pack(side="left")
        self._button(bar, "Cancel", win.destroy).pack(side="left", padx=8)

    def _apply_msu(self, rom_path, pack_dir):
        """Copy an MSU-1 pack's *.pcm tracks next to the seed, renamed to match it,
        and drop the `<seed>.msu` marker so emulators pick up the soundtrack."""
        import glob, re, shutil
        base = os.path.splitext(rom_path)[0]
        pcms = glob.glob(os.path.join(pack_dir, "*.pcm"))
        if not pcms:
            raise RuntimeError("no .pcm tracks found in the MSU pack folder")
        open(base + ".msu", "a").close()
        copied = 0
        for src in pcms:
            m = re.search(r"-(\d+)\.pcm$", os.path.basename(src))
            if not m:
                continue
            shutil.copyfile(src, f"{base}-{int(m.group(1))}.pcm")
            copied += 1
        if not copied:
            raise RuntimeError("MSU tracks aren't named '<name>-<n>.pcm'")

    def _generate_seed_thread(self, base, preset_name):
        import asyncio
        import pyz3r
        out_dir = os.path.join(HERE, "seeds")
        os.makedirs(out_dir, exist_ok=True)
        patch = {**DEFAULT_PATCH, **self.cfg.get("patch", {})}
        msu = self.cfg.get("msu", {})
        music = patch.get("music", True)
        msu1_resume = patch.get("msu1_resume", False)
        if msu.get("enable"):                     # MSU pack drives the audio
            music = False
            msu1_resume = True

        async def make():
            seed = await pyz3r.ALTTPR.generate(settings=preset_settings(preset_name),
                                               endpoint="/api/randomizer")
            hash_id = getattr(seed, "hash", None) or seed.url.rstrip("/").split("/")[-1]
            out = os.path.join(out_dir, f"{hash_id}.sfc")
            await seed.create_patched_game(
                input_filename=base, output_filename=out,
                heartspeed=patch["heartspeed"], heartcolor=patch["heartcolor"],
                spritename=patch.get("spritename", "Link"), music=music,
                quickswap=patch.get("quickswap", True), menu_speed=patch["menu_speed"],
                msu1_resume=msu1_resume)
            return out, seed.url
        try:
            out, url = asyncio.run(make())
            if msu.get("enable") and msu.get("pack_dir"):
                try:
                    self._apply_msu(out, msu["pack_dir"])
                except Exception as e:
                    self.seedgen_q.put(("msuwarn", str(e), ""))
            self.cfg["rom_path"] = out
            self._remember_room_rom(out)
            save_settings(self.cfg)
            self.seedgen_q.put(("done", out, url))
        except Exception as e:
            self.seedgen_q.put(("error", str(e), ""))

    def launch_emulator(self):
        kind = self._launch_kind()
        emu = self._emu_path(kind); rom = self.cfg.get("rom_path")
        if not emu or not os.path.exists(emu):
            self._configure_emulator(); return
        try:
            if kind == "retroarch":
                core = self.cfg.get("core_path") or self._guess_core(emu)
                if not core or not os.path.exists(core):
                    messagebox.showwarning("HyruleLink", "Pick the snes9x RetroArch core in Configure.")
                    self._configure_emulator(); return
                args = [emu, "-L", core] + ([rom] if rom else []) + ["--appendconfig", self._ensure_ra_netcfg()]
            else:
                args = [emu] + ([rom] if rom else [])
            subprocess.Popen(args)
            self._log(f"Launching {kind} (network-ready)…")
            self._remember_room_rom(rom)
        except Exception as ex:
            messagebox.showerror("HyruleLink", f"Couldn't launch emulator:\n{ex}")

    def _configure_emulator(self):
        win = tk.Toplevel(self); win.title("Emulator setup"); win.configure(bg=BG)
        win.geometry("600x470"); win.transient(self); pad = {"padx": 16}
        tk.Label(win, text="Your emulators", fg=GOLD, bg=BG,
                 font=("Segoe UI Semibold", 13)).pack(anchor="w", pady=(14, 2), **pad)
        tk.Label(win, text="Set both if you like — pick which one the Launch button starts.",
                 fg=MUTED, bg=BG, font=("Segoe UI", 9)).pack(anchor="w", **pad)

        tk.Label(win, text="Launch with", fg=MUTED, bg=BG, font=("Segoe UI", 9)).pack(
            anchor="w", pady=(10, 0), **pad)
        launch_var = tk.StringVar(value=self._launch_kind())
        ttk.Combobox(win, textvariable=launch_var, state="readonly", style="HL.TCombobox",
                     values=["retroarch", "snes9x"]).pack(anchor="w", **pad)

        paths = self.cfg.get("emu_paths") or {}
        e_ra = self._path_row(win, "RetroArch program (retroarch.exe)", paths.get("retroarch", ""),
                              [("Programs", "*.exe"), ("All", "*.*")])
        e_core = self._path_row(win, "RetroArch core (snes9x_libretro.dll)", self.cfg.get("core_path", ""),
                                [("Core", "*.dll"), ("All", "*.*")])
        e_s9 = self._path_row(win, "snes9x program (snes9x.exe)", paths.get("snes9x", ""),
                              [("Programs", "*.exe"), ("All", "*.*")])
        e_rom = self._path_row(win, "Current seed (.sfc)", self.cfg.get("rom_path", ""),
                               [("SNES ROM", "*.sfc *.smc"), ("All", "*.*")])
        err = tk.Label(win, text="", fg=RED, bg=BG, font=("Segoe UI", 9)); err.pack(anchor="w", pady=(6, 0), **pad)

        def save():
            ra, s9 = e_ra.get().strip(), e_s9.get().strip()
            for label, p in (("RetroArch", ra), ("snes9x", s9)):
                if p and not os.path.exists(p):
                    err.config(text=f"{label} path not found"); return
            self.cfg["emu_paths"] = {"retroarch": ra, "snes9x": s9}
            self.cfg["launch_emu"] = launch_var.get()
            self.cfg["core_path"] = e_core.get().strip()
            self.cfg["rom_path"] = e_rom.get().strip()
            if launch_var.get() == "retroarch" and ra and not self.cfg["core_path"]:
                self.cfg["core_path"] = self._guess_core(ra)
            self._remember_room_rom(self.cfg["rom_path"])
            save_settings(self.cfg)
            self._refresh_emu_summary()
            win.destroy()

        bar = tk.Frame(win, bg=BG); bar.pack(anchor="w", pady=12, **pad)
        self._button(bar, "Save", save, primary=True).pack(side="left")
        self._button(bar, "Seed & patch…", self._seed_dialog).pack(side="left", padx=8)
        self._button(bar, "Cancel", win.destroy).pack(side="left")

    # ── fast state pump (board feels instant) ───────────────────────────────
    def _pump_state(self):
        """Drain the server-state queue and repaint the board. Runs on a fast
        loop so a click (claim / host give) shows up within ~90ms, instead of
        waiting up to one 800ms _tick."""
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
                self._log(msg.get("text", ""))
            elif t == "reject":
                reason = msg.get("reason", "")
                self._log("⚠ " + reason)
                if self.room is not None and ("room not found" in reason.lower()
                                              or "room closed" in reason.lower()):
                    messagebox.showinfo("HyruleLink", "This room was closed by an admin.")
                    self._leave()
                    break
        if new_state and self.room is not None and hasattr(self, "board"):
            self._render_board()

    def _pump_loop(self):
        try:
            self._pump_state()
        except Exception:
            pass
        self.after(90, self._pump_loop)   # stops when the window is destroyed, like _tick

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
                self._log(f"✓ Seed ready: {os.path.basename(a)} — click Launch.")
                self._refresh_emu_summary()
                messagebox.showinfo("HyruleLink",
                    f"Seed generated and set as your ROM:\n{a}\n\nClick “Launch”.")
            elif kind == "msuwarn":
                self._log("⚠ MSU pack not applied: " + a)
            else:
                self._log("Seed generation failed: " + a)
                messagebox.showerror("HyruleLink", "Couldn't generate a seed:\n" + a)
        # State/board updates are pumped on a much faster loop (_pump_state) so the
        # board reflects a click within ~90ms instead of up to one 800ms tick.

        # status dots
        if self.room is not None:
            srv_ok = self.ui_conn is not None
            self._dot(self.dot_srv, GREEN if srv_ok else GOLD)
            self.lbl_srv.config(text="server: " + ("connected" if srv_ok else "connecting…"))
            if self.agent is None:
                t, s, label = self._detected
                ok = label != "not detected"
                self._dot(self.dot_emu, GREEN if ok else DIM)
                self.lbl_emu.config(text="emulator: " + (label if ok else "not linked"))
                if hasattr(self, "emu_line"):
                    self.emu_line.config(
                        text=("Ready to link · " + label) if ok
                        else "No emulator detected. Start one or use Launch; Emulator help covers other setups.",
                        fg=GREEN if ok else MUTED)
            else:
                emu_ok = getattr(self.transport, "connected", False)
                self._dot(self.dot_emu, GREEN if emu_ok else GOLD)
                self.lbl_emu.config(text="emulator: " + ("linked" if emu_ok else "waiting…"))
                if hasattr(self, "emu_line"):
                    self.emu_line.config(text="Live sync is active — pickups and claims update instantly."
                                         if emu_ok else "Waiting for the emulator — load your seed to finish linking.",
                                         fg=GREEN if emu_ok else GOLD)
        else:
            self._dot(self.dot_emu, DIM); self._dot(self.dot_srv, DIM)
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
