"""
emu_connector.py — direct, baked-in SNES emulator connector.

Talks straight to the emulator with NO QUsb2Snes / SNI app in between, exactly
like AlttprHelper does:

  * EmuNetworkAccess (NWA) over TCP  -> Snes9x-emunwa and other NWA emulators
  * RetroArch network commands over UDP -> any RetroArch SNES core

Both sources are auto-scanned; whichever responds is used (the last working one
is preferred so we don't re-probe a dead source every call). Unlike AlttprHelper
(read-only) this also supports writes and reset, because the bot's effect system
writes WRAM.

Drop-in replacement for QUsb2SnesTracker's transport interface:
    connect() -> bool
    connected (bool attribute)
    disconnect()
    read_memory(address, size=1, domain='WRAM') -> bytes | None
    write_memory(address, data: bytes, domain='WRAM') -> bool
    reset_console() -> bool

Addressing: `address` is a WRAM offset (== SNES address - $7E0000), e.g. 0xF340.
That is exactly what both EmuNWA (CORE_READ WRAM;<off>;<size>) and RetroArch
(READ_CORE_RAM <off> <size>) expect, so no translation is required.

NOTE: direct EmuNWA/RetroArch only reach *emulators*. Real hardware (FXPak/
SD2SNES) is not supported by this connector — that requires the usb2snes/SNI app.
"""

import logging
import socket
import struct
import threading

logger = logging.getLogger("EmuConnector")

# ── EmuNWA (TCP) ───────────────────────────────────────────────────────────
NWA_HOST = "127.0.0.1"
# Ports to scan, in order. 48879 (0xBEEF) is what Snes9x-emunwa binds; 65400+ is
# the protocol draft's default range used by other NWA emulators.
NWA_PORTS = [48879] + list(range(65400, 65411))
NWA_TIMEOUT = 0.8  # seconds

# ── RetroArch (UDP) ─────────────────────────────────────────────────────────
RA_HOST = "127.0.0.1"
RA_PORT = 55355
RA_TIMEOUT = 0.8  # seconds
RA_READ_RETRIES = 2
READ_FAILURE_LIMIT = 3


class _EmuNWAClient:
    """
    EmuNetworkAccess TCP client (read + write + reset).

    Offsets/sizes are sent as DECIMAL — Snes9x-emunwa 1.6x rejects 0x-hex with
    "0 size is invalid"; decimal is accepted by every NWA build. WRAM offset ==
    SNES address - $7E0000.
    """

    def __init__(self, host=NWA_HOST, port=None):
        self.host = host
        self.port = port  # None => auto-scan on first connect
        self._sock = None

    # -- connection ----------------------------------------------------------
    def connect(self):
        ports = [self.port] if self.port else NWA_PORTS
        for p in ports:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(NWA_TIMEOUT)
                s.connect((self.host, p))
                # Confirm it actually speaks NWA before committing to this port.
                s.sendall(b"EMULATOR_INFO\n")
                reply = self._recv_reply(s)
                if isinstance(reply, dict) and "name" in reply:
                    self.port = p
                    self._sock = s
                    return True
                s.close()
            except OSError:
                continue
        return False

    def _ensure_sock(self):
        if self._sock is None:
            return self.connect()
        return True

    # -- low-level framing ---------------------------------------------------
    def _recv_n(self, sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("socket closed mid-reply")
            buf += chunk
        return buf

    def _recv_reply(self, sock):
        """
        Read one reply: bytes for a binary reply, dict for an ASCII/hash reply
        (errors come back as {'error': ..., 'reason': ...}).
        """
        first = self._recv_n(sock, 1)
        if first == b"\x00":
            size = struct.unpack(">I", self._recv_n(sock, 4))[0]
            return self._recv_n(sock, size)
        # ASCII: already consumed one byte (should be '\n'). Read until '\n\n',
        # tolerating a short read (empty success replies are just '\n').
        data = first
        while not data.endswith(b"\n\n"):
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
        out = {}
        for line in data.decode(errors="replace").split("\n"):
            if ":" in line:
                k, _, v = line.partition(":")
                out[k.strip()] = v.strip()
        return out

    def _drain(self):
        """
        Discard any bytes left in the socket buffer (e.g. a write ack we chose
        not to wait for) so the next read's framing stays aligned.
        """
        if self._sock is None:
            return
        self._sock.setblocking(False)
        try:
            while True:
                if not self._sock.recv(4096):
                    break
        except (BlockingIOError, OSError):
            pass
        finally:
            try:
                self._sock.settimeout(NWA_TIMEOUT)
            except OSError:
                pass

    # -- public API ----------------------------------------------------------
    def read_memory(self, address, size):
        try:
            if not self._ensure_sock():
                return None
            self._drain()
            cmd = f"CORE_READ WRAM;{address};{size}\n".encode()
            self._sock.sendall(cmd)
            reply = self._recv_reply(self._sock)
            if isinstance(reply, (bytes, bytearray)) and len(reply) >= size:
                return bytes(reply[:size])
            # dict => error / no game loaded; drop socket so we re-probe next time.
            self.close()
            return None
        except OSError:
            self.close()
            return None

    def write_memory(self, address, data):
        """
        Fire-and-forget write (matches the old QUsb2Snes behaviour, which did not
        wait for a write ack). Any ack is drained before the next read.
        """
        try:
            if not self._ensure_sock():
                return False
            size = len(data)
            cmd = f"bCORE_WRITE WRAM;{address};{size}\n".encode()
            self._sock.sendall(cmd)
            blob = b"\x00" + struct.pack(">I", size) + bytes(data)
            self._sock.sendall(blob)
            return True
        except OSError:
            self.close()
            return False

    def reset_console(self):
        try:
            if not self._ensure_sock():
                return False
            self._drain()
            self._sock.sendall(b"EMULATION_RESET\n")
            reply = self._recv_reply(self._sock)
            return not (isinstance(reply, dict) and "error" in reply)
        except OSError:
            self.close()
            return False

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


class _RetroArchClient:
    """RetroArch UDP network-command client (read + write + reset)."""

    def __init__(self, host=RA_HOST, port=RA_PORT):
        self.addr = (host, port)
        self._sock = None

    def _ensure_sock(self):
        if self._sock is None:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(RA_TIMEOUT)
            self._sock = s
        return True

    def connect(self):
        """Probe RetroArch with VERSION (answers even with no game loaded)."""
        try:
            self._ensure_sock()
            self._sock.sendto(b"VERSION\n", self.addr)
            self._sock.recvfrom(4096)
            return True
        except OSError:
            self.close()
            return False

    def read_memory(self, address, size):
        self._ensure_sock()
        cmd = f"READ_CORE_RAM {address:X} {size}\n".encode()
        for _ in range(RA_READ_RETRIES):
            try:
                self._sock.sendto(cmd, self.addr)
                data, _ = self._sock.recvfrom(65536)
                parts = data.decode(errors="replace").split()
                # Ignore a late UDP reply for an earlier request instead of
                # interpreting its bytes as the current address.
                if len(parts) < 3 or parts[0] != "READ_CORE_RAM":
                    continue
                response_addr = int(parts[1], 16)
                if response_addr != address or parts[2] == "-1":
                    continue
                payload = bytes(int(b, 16) for b in parts[2:])
                return payload[:size] if len(payload) >= size else None
            except (OSError, ValueError):
                continue
        return None

    def write_memory(self, address, data):
        try:
            self._ensure_sock()
            payload = " ".join(f"{b:02X}" for b in data)
            cmd = f"WRITE_CORE_RAM {address:X} {payload}\n"
            self._sock.sendto(cmd.encode(), self.addr)
            return True
        except OSError:
            return False

    def reset_console(self):
        try:
            self._ensure_sock()
            self._sock.sendto(b"RESET\n", self.addr)
            return True
        except OSError:
            return False

    def show_message(self, text):
        """Display a short message through RetroArch's built-in OSD."""
        try:
            self._ensure_sock()
            clean = " ".join(str(text).split())[:120]
            if not clean:
                return False
            self._sock.sendto(f"SHOW_MSG {clean}\n".encode("utf-8", "replace"), self.addr)
            return True
        except OSError:
            return False

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


class EmuConnector:
    """
    Direct emulator connector exposing the QUsb2SnesTracker transport interface.

    Tries EmuNWA first, then RetroArch; remembers whichever source last worked so
    a dead source isn't re-probed on every call. Thread-safe (the effect manager
    and GUI may both poke it).
    """

    # host/port/debug kept for drop-in compatibility with QUsb2SnesTracker; the
    # connector auto-scans localhost so they're advisory only.
    #
    # source pins which emulator transport to use ("nwa" or "retroarch"); None
    # auto-scans both. Pinning matters when two emulators run on one PC (e.g. a
    # local two-player test): otherwise both agents would prefer NWA and hit the
    # same emulator. In normal use (one emulator per player's PC) leave it None.
    def __init__(self, host="localhost", port=None, debug=False, source=None):
        self.debug = debug
        self.connected = False
        self._lock = threading.RLock()
        pin = {"nwa": "Snes9x-NWA", "snes9x": "Snes9x-NWA",
               "retroarch": "RetroArch", "ra": "RetroArch"}.get(
            (source or "").lower())
        # When NWA is pinned to a specific port, bind the client to it instead of
        # auto-scanning — lets two snes9x-nwa instances on one PC be told apart.
        nwa_client = (_EmuNWAClient(port=port) if pin == "Snes9x-NWA" and port
                      else _EmuNWAClient())
        all_sources = [
            ("Snes9x-NWA", nwa_client),
            ("RetroArch", _RetroArchClient()),
        ]
        self._sources = [s for s in all_sources if pin is None or s[0] == pin]
        self._active = None  # label of the source that last responded
        self._read_failures = 0

    # -- connection ----------------------------------------------------------
    def connect(self):
        with self._lock:
            for name, client in self._sources:
                try:
                    if client.connect():
                        self._active = name
                        self.connected = True
                        self._read_failures = 0
                        logger.info(f"[EmuConnector] Connected via {name}")
                        return True
                except Exception as e:
                    if self.debug:
                        logger.debug(f"[EmuConnector] {name} connect failed: {e}")
            self.connected = False
            self._active = None
            self._read_failures = 0
            return False

    def disconnect(self):
        with self._lock:
            for _, client in self._sources:
                try:
                    client.close()
                except Exception:
                    pass
            self.connected = False
            self._active = None
            self._read_failures = 0

    def _ordered(self):
        """Sources with the last-working one first."""
        if self._active:
            return sorted(self._sources, key=lambda t: t[0] != self._active)
        return list(self._sources)

    def _active_client(self):
        """The client we last confirmed alive (via connect/read), or None."""
        if not self._active:
            return None
        for name, client in self._sources:
            if name == self._active:
                return client
        return None

    # -- memory --------------------------------------------------------------
    def read_memory(self, address, size=1, domain="WRAM"):
        with self._lock:
            # A single missed local UDP reply is not a disconnect. Keep using
            # the active source until several consecutive reads fail; any good
            # read immediately restores confidence.
            active_name = self._active
            active_client = self._active_client()
            if active_client is not None:
                data = active_client.read_memory(address, size)
                if data is not None:
                    self.connected = True
                    self._read_failures = 0
                    return data
                self._read_failures += 1
                if self._read_failures < READ_FAILURE_LIMIT:
                    if self.debug:
                        logger.debug("[EmuConnector] transient read miss %s/%s via %s",
                                     self._read_failures, READ_FAILURE_LIMIT, active_name)
                    return None

            for name, client in self._sources:
                if name == active_name:
                    continue
                data = client.read_memory(address, size)
                if data is not None:
                    self._active = name
                    self.connected = True
                    self._read_failures = 0
                    return data
            self._active = None
            self.connected = False
            self._read_failures = 0
            return None

    def write_memory(self, address, data, domain="WRAM"):
        # Only write to the source we've confirmed alive. RetroArch is UDP, so a
        # blind sendto always "succeeds" even with nothing listening; gating on
        # the active source avoids reporting phantom writes.
        with self._lock:
            client = self._active_client()
            if client is None:
                return False
            return client.write_memory(address, data)

    def reset_console(self):
        with self._lock:
            client = self._active_client()
            if client is None:
                return False
            return client.reset_console()

    def show_message(self, text):
        """Use an emulator-native OSD when the active transport supports one."""
        with self._lock:
            client = self._active_client()
            show = getattr(client, "show_message", None) if client is not None else None
            return bool(show and show(text))
