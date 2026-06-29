"""
discovery.py — enumerate the SNES memory sources reachable on localhost.

This is the *discovery* half of the emulator link: which emulators/bridges are
open right now, and which one to bind to. The *transport* half (actually
reading/writing WRAM) lives in emu_connector.py and qusb2snes_tracker.py.

Both halves must agree on the same ports and probe handshakes, so the probe
constants are imported from emu_connector here rather than re-typed — that's the
single source of truth. (Before this module existed the GUI kept its own copy
and the NWA port ranges had already drifted apart.)

Two entry points:
  scan_emulators()  -> [{"label", "bind"}, ...]   every source found, for a picker
  detect_emulator() -> (transport, source, label) one best source, for auto mode

A `bind` is exactly what the GUI's transport builder needs:
  {"transport": "emu", "source": "nwa"|"retroarch", "port": <int|None>}  or
  {"transport": "hardware"}   (a QUsb2Snes/SNI bridge — FXPak/SD2SNES, snes9x-rr…)
"""

import json
import socket

from .emu_connector import NWA_HOST, NWA_PORTS, RA_HOST, RA_PORT

# QUsb2Snes/SNI WebSocket bridge. 23074 is the usb2snes/SNI default; 8080 is the
# legacy QUsb2Snes port (also what QUsb2Snes uses to reach emulators via EmuNWA).
SNI_BRIDGE_PORTS = (23074, 8080)
SNI_BRIDGE_PORT = SNI_BRIDGE_PORTS[0]   # the one we probe / start by default

# Probe timeouts (seconds). Closed localhost ports refuse instantly, so these
# only bite when something is listening but slow to answer.
_NWA_TIMEOUT = 0.4
_RA_TIMEOUT = 0.5
_BRIDGE_TIMEOUT = 0.6


def _probe_nwa(port, timeout=_NWA_TIMEOUT):
    """Return the EMULATOR_INFO `name:` field if an NWA emulator answers on
    `port`, else None."""
    try:
        s = socket.create_connection((NWA_HOST, port), timeout=timeout)
    except OSError:
        return None
    try:
        s.sendall(b"EMULATOR_INFO\n")
        reply = s.recv(256).decode(errors="replace")
    except OSError:
        return None
    finally:
        s.close()
    return next((ln.split(":", 1)[1].strip() for ln in reply.splitlines()
                 if ln.lower().startswith("name:")), "snes9x-nwa")


def _probe_retroarch(timeout=_RA_TIMEOUT):
    """Return RetroArch's VERSION string if it answers, else None. VERSION is
    answered even with no game loaded."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(b"VERSION\n", (RA_HOST, RA_PORT))
        return s.recvfrom(128)[0].decode(errors="replace").strip()
    except OSError:
        return None
    finally:
        s.close()


def _probe_bridge(port, timeout=_BRIDGE_TIMEOUT):
    """Ask a QUsb2Snes/SNI bridge for its device list.

    Returns the device-name list (possibly empty if the bridge is up but no
    emulator/hardware is attached yet), or None if nothing is listening.
    """
    # Imported lazily so this module stays importable without websocket-client
    # (the bridge probe simply reports "nothing there" when the dep is absent).
    try:
        import websocket  # websocket-client
    except ImportError:
        return None
    try:
        w = websocket.create_connection(f"ws://127.0.0.1:{port}", timeout=timeout)
    except Exception:
        return None
    try:
        w.send(json.dumps({"Opcode": "DeviceList", "Space": "SNES"}))
        return json.loads(w.recv()).get("Results", [])
    except Exception:
        return None
    finally:
        try:
            w.close()
        except Exception:
            pass


def scan_emulators():
    """Enumerate every SNES memory source reachable on localhost right now.

    Returns a list of {"label", "bind"}. Lets the player see which emulators are
    open and pin one — handy when two run on one PC (e.g. two snes9x-nwa
    instances on different ports for local testing).
    """
    found = []
    for p in NWA_PORTS:                      # snes9x-nwa / other EmuNWA emulators
        name = _probe_nwa(p, timeout=0.25)
        if name is not None:
            found.append({"label": f"{name} @{p}",
                          "bind": {"transport": "emu", "source": "nwa", "port": p}})
    ver = _probe_retroarch(timeout=0.4)      # RetroArch (UDP network commands)
    if ver is not None:
        found.append({"label": f"RetroArch {ver}".strip(),
                      "bind": {"transport": "emu", "source": "retroarch", "port": None}})
    for port in SNI_BRIDGE_PORTS:            # QUsb2Snes/SNI bridge (hardware + others)
        devices = _probe_bridge(port)
        if devices is not None:
            found.append({"label": f"SNI bridge — {devices[0]}" if devices
                          else "SNI bridge (no device yet)",
                          "bind": {"transport": "hardware"}})
            break
    return found


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
    for port in SNI_BRIDGE_PORTS:
        devices = _probe_bridge(port, timeout=0.8)
        if devices is None:
            continue
        if devices:
            return ("hardware", None, f"QUsb2Snes/SNI — {devices[0]}")
        bridge_hint = "QUsb2Snes/SNI — attach your emulator to it"
        break
    # 2. direct sources (only chosen when no shared bridge owns a device);
    #    snes9x-nwa preferred over RetroArch.
    if _probe_nwa(NWA_PORTS[0]) is not None:
        return ("emu", "nwa", "snes9x (EmuNetworkAccess)")
    if _probe_retroarch() is not None:
        return ("emu", "retroarch", "RetroArch")
    # 3. bridge running but idle
    if bridge_hint:
        return ("hardware", None, bridge_hint)
    return ("emu", None, "not detected")
