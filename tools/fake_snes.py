"""A stand-in for SNI / QUsb2Snes with a fake ALTTPR behind it, for working on
the browser client and the co-op harness without a game running.

    python tools\\fake_snes.py                    # ws://localhost:23074, control :23075
    python tools\\fake_snes.py --port 23174       # a second fake: ws :23174, control :23175
    curl -s localhost:23075/                      # module, HUD strip, item levels
    curl -s -X POST localhost:23075/module/7      # walk into a dungeon (playable)
    curl -s -X POST localhost:23075/item/sword/2  # find the Master Sword
    curl -s -X POST localhost:23075/item/sword/0  # lose it again
    curl -s localhost:23075/mem/F379              # {"bytes": [4]}
    curl -s "localhost:23075/mem/F38C?n=3"        # three bytes from $7EF38C
    curl -s -X POST localhost:23075/mem/F379/00   # clear the run flag behind the page's back
    curl -s -X POST localhost:23075/reset         # zeroed WRAM, file select
    curl -s -X POST localhost:23075/module/1      # back to the file select

Run it from the repo root; it imports `shared.items` for the catalog.

Do not run it beside the real SNI or QUsb2Snes: they want the same port, and
a page that finds this one instead of the real bridge writes to nothing.

The bridge side speaks enough usb2snes for web/js/snes.js: DeviceList,
AppVersion, Info, Name, Attach (no reply), GetAddress (binary reply) and
PutAddress (binary follows). Addresses are in the usb2snes WRAM space
(0xF50000 = $7E0000). A write that covers $7E0016 is consumed at once, the way
the NMI takes the HUD flag. Memory starts zeroed, with the HUD buffer filled
with the blank tile and the module at 0x01 (file select).

The control side pokes the game the way the game itself does on a pickup,
so the page sees an ordinary find:

    progressive   the byte = level                         (sword, shield, mail, gloves, magic)
    simple        the byte = `give` (Magic Mirror is 2)
    boots         $7EF355 = 1 and $7EF379 |= 0x04 (the run flag)
    bow           $7EF38E |= 0x80, with 0x40 set at level 2 and cleared at level 1;
                  $7EF340 = 1 wood / 4 silver. Arrows are not touched.
    shared slot   $7EF38C |= the item's bits (blue 0x80, red 0x40, mushroom 0x28,
                  powder 0x10, shovel 0x04, flute 0x01 active or 0x02 with
                  ?inactive=1) and the enum slot ($7EF341 / $7EF344 / $7EF34C)
                  set to that item

Level 0 takes the item away the way agent/sni/item_effects.py revokes it:
the byte back to 0, the run flag and bow bits cleared, the shared-slot bits
cleared and the enum slot falling back to the other item still owned there.
"""
import argparse
import asyncio
import json
import os
import sys

from aiohttp import WSMsgType, web

# `python tools\fake_snes.py` puts tools/ on sys.path, not the repo root.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from shared.items import (  # noqa: E402
    BOW_EQUIP_ADDR, BOW_FLAGS_ADDR, BOW_HAS_MASK, BOW_SILVER_MASK, BY_KEY,
    INV_TRACK_ADDR, ITEMS, discovered_level,
)

WRAM_BASE = 0xF50000        # usb2snes address of $7E0000
WRAM_SIZE = 0x20000         # $7E0000-$7FFFFF
MODULE_ADDR = 0x0010        # $7E0010 game module
FLAG_ADDR = 0x0016          # $7E0016 flag_update_hud_in_nmi
HUD = 0xC700                # $7EC700 hud_tile_indices_buffer
HUD_WORDS = 32 * 5
STRIP = HUD + (4 * 32 + 5) * 2   # row 4, col 5: where hud.js draws
STRIP_WIDTH = 20
SPACE = 0x247F
LETTER_A = 0x255D
DIGIT_0 = 0x2490
COLON = 0x2806

BOOTS_ADDR = 0xF355
ABILITY_ADDR = 0xF379
RUN_ABILITY_MASK = 0x04
ARROWS_ADDR = 0xF377

BOOM_SLOT = 0xF341          # enum: 1 blue, 2 red
POWDER_SLOT = 0xF344        # enum: 1 mushroom, 2 powder
FLUTE_SLOT = 0xF34C         # enum: 1 shovel, 2 inactive flute, 3 active flute

# What z3randomizer's receive code does for the shared-slot items
# (newitems.asm ORs $7EF38C, itemdatatables.asm sets the slot enum):
# key -> (bits set in $7EF38C, enum slot, enum value).
PICKUP = {
    "blue_boomerang": (0x80, BOOM_SLOT, 1),
    "red_boomerang": (0x40, BOOM_SLOT, 2),
    "mushroom": (0x28, POWDER_SLOT, 1),     # 0x20 owned + 0x08 "had one"
    "powder": (0x10, POWDER_SLOT, 2),
    "shovel": (0x04, FLUTE_SLOT, 1),
    "flute": (0x01, FLUTE_SLOT, 3),         # the active flute
}
FLUTE_INACTIVE = (0x02, FLUTE_SLOT, 2)      # the flute before the bird is freed
# Bits a loss clears where they differ from the pickup bits (item_effects).
LOSS_BITS = {"flute": 0x03}
# Per slot, in fallback order: (key, ownership bit, enum). When the equipped
# item goes, the slot falls back to the first other item still owned.
SLOTS = {
    BOOM_SLOT: [("blue_boomerang", 0x80, 1), ("red_boomerang", 0x40, 2)],
    POWDER_SLOT: [("mushroom", 0x20, 1), ("powder", 0x10, 2)],
    FLUTE_SLOT: [("shovel", 0x04, 1), ("flute", 0x01, 3), ("flute", 0x02, 2)],
}


def max_level(item) -> int:
    if item.kind == "progressive":
        return item.cap
    if item.kind == "bow":
        return 2
    return 1


def parse_addr(text: str) -> int:
    """A WRAM offset from hex: `F379`, `0xF379`, `$7EF379` or `F5F379`."""
    s = text.strip().lower()
    for prefix in ("0x", "$"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    addr = int(s, 16)
    if addr >= WRAM_BASE:
        addr -= WRAM_BASE
    elif 0x7E0000 <= addr < 0x800000:
        addr -= 0x7E0000
    if not 0 <= addr < WRAM_SIZE:
        raise ValueError(f"{text} is outside WRAM")
    return addr


class FakeGame:
    """128 KB of WRAM and the pokes a real pickup makes."""

    def __init__(self):
        self.mem = bytearray(WRAM_SIZE)
        self.reset()

    def reset(self):
        self.mem[:] = bytes(WRAM_SIZE)
        blank = SPACE.to_bytes(2, "little")
        for i in range(HUD_WORDS):
            self.mem[HUD + i * 2:HUD + i * 2 + 2] = blank
        self.mem[MODULE_ADDR] = 0x01          # file select

    # -- usb2snes side -------------------------------------------------------
    def read(self, addr: int, size: int) -> bytes:
        """Bytes at a usb2snes address; anything outside WRAM reads as 0."""
        out = bytearray(size)
        off = addr - WRAM_BASE
        lo, hi = max(off, 0), min(off + size, WRAM_SIZE)
        if lo < hi:
            out[lo - off:hi - off] = self.mem[lo:hi]
        return bytes(out)

    def write(self, addr: int, data: bytes):
        off = addr - WRAM_BASE
        lo, hi = max(off, 0), min(off + len(data), WRAM_SIZE)
        if lo < hi:
            self.mem[lo:hi] = data[lo - off:hi - off]
        if lo <= FLAG_ADDR < hi:
            self.mem[FLAG_ADDR] = 0            # the NMI took it

    # -- what the page sees ----------------------------------------------------
    def strip_text(self) -> str:
        out = ""
        for i in range(STRIP_WIDTH):
            w = int.from_bytes(self.mem[STRIP + i * 2:STRIP + i * 2 + 2], "little")
            if LETTER_A <= w < LETTER_A + 26:
                out += chr(65 + w - LETTER_A)
            elif DIGIT_0 <= w < DIGIT_0 + 10:
                out += str(w - DIGIT_0)
            elif w == COLON:
                out += ":"              # the randomizer clock's separator tile
            else:
                out += " " if w == SPACE else "?"
        return out

    def items(self) -> dict:
        return {it.key: discovered_level(it, self.mem[it.addr]) for it in ITEMS}

    def state(self) -> dict:
        return {
            "module": self.mem[MODULE_ADDR],
            "strip": self.strip_text(),
            "items": self.items(),
            "ability": self.mem[ABILITY_ADDR],
            "arrows": self.mem[ARROWS_ADDR],
        }

    # -- the game finding (or losing) an item ------------------------------------
    def set_item(self, key: str, level: int, inactive: bool = False):
        """Poke `key` to `level` the way the game does on a pickup.

        Raises KeyError for an unknown item and ValueError for a level the
        item cannot have. `inactive` only matters for the flute."""
        item = BY_KEY[key]
        if not 0 <= level <= max_level(item):
            raise ValueError(f"{key} has levels 0-{max_level(item)}, not {level}")
        m = self.mem
        if item.kind == "progressive":
            m[item.addr] = level
        elif item.kind == "simple":
            m[item.addr] = item.give if level else 0
        elif item.kind == "boots":
            m[BOOTS_ADDR] = 1 if level else 0
            if level:
                m[ABILITY_ADDR] |= RUN_ABILITY_MASK
            else:
                m[ABILITY_ADDR] &= ~RUN_ABILITY_MASK & 0xFF
        elif item.kind == "bow":
            flags = m[BOW_FLAGS_ADDR]
            if level == 0:
                flags &= ~(BOW_HAS_MASK | BOW_SILVER_MASK)
                equip = 0
            elif level == 1:
                flags = (flags | BOW_HAS_MASK) & ~BOW_SILVER_MASK
                equip = 1
            else:
                flags |= BOW_HAS_MASK | BOW_SILVER_MASK
                equip = 4
            m[BOW_FLAGS_ADDR] = flags & 0xFF
            m[BOW_EQUIP_ADDR] = equip
        elif item.kind == "bitfield":
            bits, slot, enum = FLUTE_INACTIVE if key == "flute" and inactive else PICKUP[key]
            if level:
                m[INV_TRACK_ADDR] |= bits
                m[slot] = enum
            else:
                m[INV_TRACK_ADDR] &= ~LOSS_BITS.get(key, bits) & 0xFF
                track = m[INV_TRACK_ADDR]
                mine = {e for k, _, e in SLOTS[slot] if k == key}
                if m[slot] in mine:
                    m[slot] = next((e for k, b, e in SLOTS[slot] if k != key and track & b), 0)
        else:
            raise ValueError(f"{key}: unknown kind {item.kind}")


# -- usb2snes websocket ------------------------------------------------------------

def bridge_app(game: FakeGame) -> web.Application:
    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        puts, buf = [], bytearray()     # a PutAddress waiting for its bytes
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                if not puts:
                    continue
                buf += msg.data
                if len(buf) >= sum(size for _, size in puts):
                    pos = 0
                    for addr, size in puts:
                        game.write(addr, bytes(buf[pos:pos + size]))
                        pos += size
                    puts, buf = [], bytearray()
                continue
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                req = json.loads(msg.data)
                op, args = req.get("Opcode"), req.get("Operands") or []
            except (ValueError, AttributeError):
                continue
            if op == "DeviceList":
                await ws.send_str(json.dumps({"Results": ["emunwa://fake-alttp:48879"]}))
            elif op == "AppVersion":
                await ws.send_str(json.dumps({"Results": ["SNI-fake"]}))
            elif op == "Info":
                await ws.send_str(json.dumps({"Results": ["1.0", "fake", "alttpr.sfc"]}))
            elif op in ("GetAddress", "PutAddress"):
                # address/size pairs in hex; several pairs are one request
                try:
                    pairs = [(int(args[i], 16), int(args[i + 1], 16))
                             for i in range(0, len(args) - 1, 2)]
                except (ValueError, TypeError):
                    continue
                if not pairs:
                    continue
                if op == "GetAddress":
                    await ws.send_bytes(b"".join(game.read(a, n) for a, n in pairs))
                else:
                    puts, buf = pairs, bytearray()
            # Name, Attach and anything else: no reply, as the real bridges do
        return ws

    app = web.Application()
    app.router.add_get("/", handler)
    return app


# -- control API -------------------------------------------------------------------

def control_app(game: FakeGame) -> web.Application:
    def bad(status, text):
        return web.json_response({"error": text}, status=status)

    async def state(request):
        return web.json_response(game.state())

    async def set_module(request):
        n = request.match_info["n"]
        try:
            value = int(n, 16) if n.lower().startswith("0x") else int(n)
        except ValueError:
            return bad(400, f"not a module number: {n}")
        if not 0 <= value <= 0xFF:
            return bad(400, f"module {n} does not fit in a byte")
        game.mem[MODULE_ADDR] = value
        return web.json_response(game.state())

    async def set_item(request):
        key = request.match_info["key"]
        if key not in BY_KEY:
            return bad(404, f"no such item: {key}")
        try:
            level = int(request.match_info["level"])
            inactive = request.query.get("inactive", "") in ("1", "true", "yes")
            game.set_item(key, level, inactive)
        except ValueError as e:
            return bad(400, str(e))
        return web.json_response(game.state())

    async def get_mem(request):
        try:
            addr = parse_addr(request.match_info["addr"])
            n = int(request.query.get("n", "1"))
        except ValueError as e:
            return bad(400, str(e))
        if not 1 <= n <= 0x1000:
            return bad(400, "n must be 1-4096")
        return web.json_response({"bytes": list(game.read(WRAM_BASE + addr, n))})

    async def put_mem(request):
        try:
            addr = parse_addr(request.match_info["addr"])
            value = int(request.match_info["value"], 16)
        except ValueError as e:
            return bad(400, str(e))
        if not 0 <= value <= 0xFF:
            return bad(400, "value must be one byte, 00-FF")
        game.mem[addr] = value
        return web.json_response({"bytes": [game.mem[addr]]})

    async def reset(request):
        game.reset()
        return web.json_response(game.state())

    app = web.Application()
    app.router.add_get("/", state)
    app.router.add_post("/module/{n}", set_module)
    app.router.add_post("/item/{key}/{level}", set_item)
    app.router.add_get("/mem/{addr}", get_mem)
    app.router.add_post("/mem/{addr}/{value}", put_mem)
    app.router.add_post("/reset", reset)
    return app


def _port(runner: web.AppRunner) -> int:
    return runner.addresses[0][1]


async def start(game: FakeGame, host: str = "127.0.0.1", port: int = 23074,
                control_port=None):
    """Start the bridge and the control API; returns (runners, ws_port, control_port).

    Port 0 picks a free port; the control port defaults to port + 1 (or a free
    one when port is 0)."""
    if control_port is None:
        control_port = port + 1 if port else 0
    runners = []
    try:
        for app, p in ((bridge_app(game), port), (control_app(game), control_port)):
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            runners.append(runner)
            await web.TCPSite(runner, host, p).start()
    except BaseException:
        for runner in runners:
            await runner.cleanup()
        raise
    return runners, _port(runners[0]), _port(runners[1])


async def serve(host: str, port: int):
    runners, ws_port, ctl_port = await start(FakeGame(), host, port)
    print(f"fake usb2snes on ws://localhost:{ws_port}, control on http://localhost:{ctl_port}",
          flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        for runner in runners:
            await runner.cleanup()


def main():
    ap = argparse.ArgumentParser(description="Fake SNI/QUsb2Snes bridge with a fake ALTTPR.")
    ap.add_argument("--port", type=int, default=23074,
                    help="usb2snes websocket port (control API on port + 1)")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    try:
        asyncio.run(serve(args.host, args.port))
    except KeyboardInterrupt:
        pass
    except OSError as e:
        print(f"cannot listen on {args.port} or {args.port + 1}: {e}\n"
              "Is SNI or QUsb2Snes running? Do not run the fake beside them.",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
