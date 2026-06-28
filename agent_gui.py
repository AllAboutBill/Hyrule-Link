#!/usr/bin/env python3
"""
HyruleLink — Player app (all-in-one, no login).

Type a name, Host or Join a room by code, and play — the game board lives right
inside this window (always connected), and one button links your emulator. No
accounts, no config files, no separate browser needed (the web page remains as
an optional spectator/second-screen view).

Run via "Play.cmd", or:  pythonw agent_gui.py
"""
import json
import os
import queue
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

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

SETTINGS = os.path.join(HERE, "agent", "gui_settings.json")
PUBLIC_SERVER = "https://hyrulelink.billogna.lol"   # default shared server
SNI_DIR = os.path.join(HERE, "tools", "sni")        # bundled SNI bridge (MIT)

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

# palette matched to billogna.lol (deep-space / nebula)
BG = "#070611"; PANEL = "#121a30"; PANEL2 = "#1a2340"; INK = "#e7ecff"; MUTED = "#9aa4c2"
GOLD = "#ffd700"; GREEN = "#34d399"; RED = "#ef4444"; BLUE = "#06b6d4"; LINE = "#2a3360"
ACCENT = "#6d7dff"; ACCENT2 = "#9a6bff"   # indigo / purple brand accents


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


def http_post(base, path, payload):
    req = urllib.request.Request(base.rstrip("/") + path,
                                 data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
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
        self.logo_font = "Orbitron" if "Orbitron" in tkfont.families() else "Segoe UI Semibold"
        self.title("HyruleLink")
        self.configure(bg=BG)
        self.geometry("900x700")
        self.minsize(680, 600)

        self.cfg = load_settings()
        self.base = self.cfg.get("server", PUBLIC_SERVER)
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

        self._build_chrome()
        self.show_start()
        threading.Thread(target=self._detect_loop, daemon=True).start()
        self._tick()
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
        head = tk.Frame(self, bg=BG); head.pack(fill="x", padx=16, pady=(12, 2))
        tk.Label(head, text="HyruleLink", fg=ACCENT, bg=BG,
                 font=(self.logo_font, 18, "bold")).pack(side="left")
        self.who = tk.Label(head, text="", fg=MUTED, bg=BG, font=("Segoe UI", 9))
        self.who.pack(side="right")
        # glowing accent line under the header (echoes the web's header glow)
        tk.Frame(self, bg=ACCENT, height=2).pack(fill="x", padx=16, pady=(0, 2))

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
        e.configure(highlightthickness=1, highlightbackground=LINE, highlightcolor=GOLD)
        return e

    def _button(self, parent, text, cmd, primary=False, small=False):
        return tk.Button(parent, text=text, command=cmd, relief="flat", cursor="hand2",
                         bg=ACCENT if primary else PANEL2, fg="#ffffff" if primary else INK,
                         activebackground=ACCENT2 if primary else LINE,
                         activeforeground="#ffffff" if primary else INK,
                         font=("Segoe UI Semibold", 9 if small else 11), bd=0,
                         padx=10 if small else 14, pady=4 if small else 8)

    def _label(self, parent, text, **kw):
        return tk.Label(parent, text=text, fg=kw.pop("fg", INK), bg=kw.pop("bg", BG),
                        font=kw.pop("font", ("Segoe UI", 10)), **kw)

    def _dot(self, canvas, color):
        canvas.delete("all"); canvas.create_oval(2, 2, 11, 11, fill=color, outline="")

    # ── step 1: name + host/join ────────────────────────────────────────────
    def show_start(self):
        self._clear_body(); self.who.config(text="")
        f = self.body
        self._label(f, "Start playing", fg=ACCENT, font=(self.logo_font, 14, "bold")).pack(anchor="w", pady=(6, 10))

        self._label(f, "Your name", fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w")
        self.e_name = self._entry(f, value=self.cfg.get("display", ""))
        self.e_name.pack(fill="x", pady=(0, 8), ipady=4)

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
                              {"name": "Co-op", "display_name": self.e_name.get().strip() or "Player"})
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
                              {"name": "Co-op", "display_name": self.e_name.get().strip() or "Player"})
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
                              {"display_name": self.e_name.get().strip() or "Player"})
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
                conn.send(json.dumps({"type": "hello", "role": "ui", "room": self.room["code"],
                                      "player_id": self.room["player_id"],
                                      "token": self.room["player_token"]}))
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
        self.who.config(text=f"{self.cfg.get('display','Player')} · room {self.room['code']}")
        f = self.body

        top = tk.Frame(f, bg=BG); top.pack(fill="x")
        tk.Label(top, text=f"Room {self.room['code']}", fg=GOLD, bg=BG,
                 font=(self.logo_font, 15, "bold")).pack(side="left")
        self._button(top, "Spectator view", lambda: webbrowser.open(f"{self.base}/"),
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

        # the board (scrollable grid)
        board_wrap = tk.Frame(f, bg=BG); board_wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(board_wrap, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(board_wrap, orient="vertical", command=self.canvas.yview)
        self.board = tk.Frame(self.canvas, bg=BG)
        self.board.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.board, anchor="nw")
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta/120), "units"))

        # activity (short)
        self.logbox = tk.Text(f, height=4, bg="#0a0f1a", fg=GREEN, relief="flat",
                              font=("Consolas", 9), highlightthickness=1, highlightbackground=LINE)
        self.logbox.pack(fill="x", pady=(6, 0)); self.logbox.configure(state="disabled")

        if self.state:
            self._render_board()

    def _build_host_bar(self):
        for w in self.host_bar.winfo_children():
            w.destroy()
        tk.Label(self.host_bar, text="★ Host:", fg=GOLD, bg=BG, font=("Segoe UI Semibold", 9)).pack(side="left")
        tk.Label(self.host_bar, text="cooldown", fg=MUTED, bg=BG, font=("Segoe UI", 9)).pack(side="left", padx=(8, 2))
        self.e_cd = self._entry(self.host_bar)
        self.e_cd.configure(width=4); self.e_cd.pack(side="left")
        self._button(self.host_bar, "set", self._set_cooldown, small=True).pack(side="left", padx=4)
        tk.Label(self.host_bar, text="· click a player below to remove",
                 fg=MUTED, bg=BG, font=("Segoe UI", 9)).pack(side="left", padx=8)

    def _is_host(self):
        return bool(self.state) and self.state.get("you") == self.state.get("host")

    def _set_cooldown(self):
        try:
            self._ui_send({"type": "admin_set_cooldown", "seconds": float(self.e_cd.get())})
        except ValueError:
            pass

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

    def _render_board(self):
        for w in self.board.winfo_children():
            w.destroy()
        self._render_players()
        if self._is_host():
            self.host_bar.pack(fill="x", pady=(0, 6), before=self.canvas.master)
            if not self.e_cd.get():
                self.e_cd.insert(0, str(int(self.state.get("cooldown_s", 5))))
        else:
            self.host_bar.pack_forget()

        ledger = self.state.get("ledger", {})
        cols = 3
        for i, cat in enumerate(self.room["items"]):
            e = ledger.get(cat["key"])
            self._card(self.board, cat, e).grid(row=i // cols, column=i % cols,
                                                 padx=5, pady=5, sticky="nsew")
        for c in range(cols):
            self.board.grid_columnconfigure(c, weight=1, minsize=200)

    def _card(self, parent, cat, e):
        you = self.state.get("you")
        border = LINE; sub = "undiscovered"; subcolor = MUTED; tier = ""
        mine = e and e.get("owner") == you
        if e:
            owner = e.get("owner_name") or "unowned"
            sub = f"held by {owner}"
            if e.get("owner"):
                border = GREEN if mine else BLUE
            if e.get("tier") and e["tier"] != "—":
                tier = "  " + e["tier"]
        card = tk.Frame(parent, bg=PANEL, highlightbackground=GREEN if mine else border,
                        highlightthickness=2 if mine else 1)
        tk.Label(card, text=cat["name"] + tier, fg=INK, bg=PANEL,
                 font=("Segoe UI Semibold", 10), anchor="w").pack(fill="x", padx=8, pady=(7, 0))
        tk.Label(card, text=sub, fg=subcolor, bg=PANEL, font=("Segoe UI", 8), anchor="w").pack(fill="x", padx=8)

        action = tk.Frame(card, bg=PANEL); action.pack(fill="x", padx=8, pady=6)
        if not e:
            tk.Label(action, text="—", fg=MUTED, bg=PANEL, font=("Segoe UI", 8)).pack(anchor="w")
        elif mine:
            tk.Label(action, text="✓ you hold this", fg=GREEN, bg=PANEL,
                     font=("Segoe UI Semibold", 9)).pack(anchor="w")
        elif you not in e.get("discovered", []):
            tk.Label(action, text="find one to claim", fg=GOLD, bg=PANEL,
                     font=("Segoe UI", 8)).pack(anchor="w")
        elif e.get("cooldown_remaining", 0) > 0.05:
            tk.Label(action, text=f"cooldown {e['cooldown_remaining']:.0f}s", fg=MUTED, bg=PANEL,
                     font=("Segoe UI", 8)).pack(anchor="w")
        else:
            self._button(action, "Claim", lambda k=cat["key"]: self._ui_send({"type": "claim", "item": k}),
                         small=True).pack(anchor="w")
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
        if hasattr(self, "btn_connect"):
            self.btn_connect.config(text="Connect & Play", bg=ACCENT, fg="#ffffff")
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
        if not hasattr(self, "logbox"):
            return
        self.logbox.configure(state="normal"); self.logbox.insert("end", line + "\n")
        self.logbox.see("end"); self.logbox.configure(state="disabled")

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
                self._log("⚠ " + msg.get("reason", ""))
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
        # live cooldown countdown between server pushes
        if self.state:
            for v in self.state.get("ledger", {}).values():
                if v.get("cooldown_remaining", 0) > 0:
                    v["cooldown_remaining"] = max(0, v["cooldown_remaining"] - 0.8)
        self.after(800, self._tick)

    def _on_close(self):
        self._stop_all.set(); self._ui_stop.set(); self._disconnect()
        for proc in (self.tunnel_proc, self.local_server, self.sni_proc):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
