#!/usr/bin/env python3
"""
main.py — HyruleLink agent entry point.

First run:  python -m agent.main --setup     (logs in, joins a room, writes config)
Then:       python -m agent.main              (connects emulator + server, runs)

Config (agent/config.json):
  {
    "server_http": "http://localhost:5019",
    "server_ws":   "ws://localhost:5019/ws",
    "room": "ABC123",
    "user_id": 1,
    "player_token": "...",
    "transport": "emu",        # "emu" (Snes9x-NWA/RetroArch) or "hardware" (FXPak/SD2SNES via QUsb2Snes)
    "poll_interval": 1.0
  }
"""

import argparse
import json
import logging
import os
import sys
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

CONFIG_PATH = os.path.join(HERE, "config.json")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("agent")


def _post(url, payload, token=None):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise SystemExit(f"server error {e.code}: {body}")


def setup():
    print("── HyruleLink agent setup ── (most players should just use Play.cmd)")
    http = input("Server URL [http://localhost:5019]: ").strip() or "http://localhost:5019"
    http = http.rstrip("/")
    display = input("Your display name: ").strip() or "Player"
    action = input("(c)reate a new room or (j)oin existing? [j]: ").strip().lower() or "j"
    if action.startswith("c"):
        name = input("Room name [Co-op]: ").strip() or "Co-op"
        room = _post(http + "/api/rooms", {"name": name, "display_name": display})
        print(f"Created room {room['code']} — share this code with the others.")
    else:
        code = input("Room code: ").strip().upper()
        room = _post(http + f"/api/rooms/{code}/join", {"display_name": display})
        print(f"Joined room {room['code']}.")

    transport = input("Transport (emu/hardware) [emu]: ").strip().lower() or "emu"
    ws = http.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
    cfg = {
        "server_http": http,
        "server_ws": ws,
        "room": room["code"],
        "player_id": room["player_id"],
        "player_token": room["player_token"],
        "transport": transport,
        "poll_interval": 1.0,
    }
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"Wrote {CONFIG_PATH}. Now run: python run_agent.py")


def build_transport(cfg):
    kind = cfg.get("transport", "emu")
    if kind == "hardware":
        from agent.sni.qusb2snes_tracker import QUsb2SnesTracker
        logger.info("Transport: QUsb2Snes (FXPak/SD2SNES or emulator via QUsb2Snes).")
        return QUsb2SnesTracker()
    from agent.sni.emu_connector import EmuConnector
    src = cfg.get("emu_source")  # "nwa" | "retroarch" | None (auto-scan both)
    logger.info("Transport: direct emulator (%s).", src or "auto NWA/RetroArch")
    return EmuConnector(source=src)


def run(config_path=None):
    config_path = config_path or CONFIG_PATH
    if not os.path.exists(config_path):
        raise SystemExit(f"No config at {config_path} — run:  python -m agent.main --setup")
    with open(config_path) as f:
        cfg = json.load(f)

    from agent.agent import HyruleAgent
    transport = build_transport(cfg)
    agent = HyruleAgent(
        transport=transport,
        server_ws_url=cfg["server_ws"],
        room=cfg["room"],
        user_id=cfg.get("player_id", cfg.get("user_id")),
        player_token=cfg["player_token"],
        poll_interval=float(cfg.get("poll_interval", 1.0)),
    )
    logger.info("Room %s as user %s. Start your emulator + load your seed.", cfg["room"], cfg["user_id"])
    agent.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down.")
        agent.stop()


def main():
    ap = argparse.ArgumentParser(description="HyruleLink player agent")
    ap.add_argument("--setup", action="store_true", help="interactive login/join + write config.json")
    ap.add_argument("--config", help="path to a config.json (default: agent/config.json)")
    args = ap.parse_args()
    if args.setup:
        setup()
    else:
        run(args.config)


if __name__ == "__main__":
    main()
