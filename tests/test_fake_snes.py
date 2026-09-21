"""tools/fake_snes.py: the stand-in bridge the browser client and the co-op
harness run against.

Its item pokes have to look to the page exactly like a pickup in the real
game (the agent only ever sees the bytes), and a loss has to leave the bytes
the agent's own revoke would. The pure tests pin both against agent/effects.py
and agent/sni/item_effects.py; the socket tests drive the usb2snes side the way
web/js/snes.js does and the control API the way the harness does.
"""
import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import unittest
import urllib.request
from pathlib import Path

import aiohttp
from websockets.asyncio.client import connect

from agent.effects import Effects
from agent.sni import item_effects as ie
from shared.items import (
    BOW_EQUIP_ADDR, BOW_FLAGS_ADDR, BY_KEY, INV_TRACK_ADDR, ITEMS, discovered_level,
)

ROOT = Path(__file__).resolve().parent.parent
FAKE_PATH = ROOT / "tools" / "fake_snes.py"


def _load():
    spec = importlib.util.spec_from_file_location("fake_snes", FAKE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


fake = _load()
W = fake.WRAM_BASE
TRACKED_START = min(it.addr for it in ITEMS)
TRACKED_SIZE = max(it.addr for it in ITEMS) - TRACKED_START + 1


class GameTransport:
    """agent.effects' transport interface over a FakeGame's WRAM."""

    def __init__(self, game):
        self.g = game

    def read_memory(self, address, size=1, domain="WRAM"):
        return bytes(self.g.mem[address:address + size])

    def write_memory(self, address, data, domain="WRAM"):
        self.g.mem[address:address + len(data)] = bytes(data)
        return True


def _levels(item):
    return range(0, fake.max_level(item) + 1)


def _diff(a, b, skip=()):
    """Addresses where two WRAM images differ, as hex strings."""
    return [f"{i:04X}:{a[i]:02X}!={b[i]:02X}"
            for i in range(len(a)) if a[i] != b[i] and i not in skip]


class PokeTests(unittest.TestCase):
    def test_starts_at_file_select_with_a_blank_strip(self):
        g = fake.FakeGame()
        s = g.state()
        self.assertEqual(s["module"], 1)
        self.assertEqual(s["strip"], " " * 20)
        self.assertEqual(s["items"], {it.key: 0 for it in ITEMS})
        self.assertEqual((s["ability"], s["arrows"]), (0, 0))

    def test_every_level_of_every_item_reads_back_alone(self):
        for item in ITEMS:
            for level in _levels(item):
                g = fake.FakeGame()
                g.set_item(item.key, level)
                want = {it.key: 0 for it in ITEMS}
                want[item.key] = level
                self.assertEqual(g.items(), want, f"{item.key} {level}")

    def test_a_pickup_writes_what_the_agents_grant_writes(self):
        # The same bytes, so a poke on one fake and a grant from the page on the
        # other look alike. The one difference: the agent tops up arrows for a
        # silver bow, a pickup does not.
        for item in ITEMS:
            for level in _levels(item):
                if not level:
                    continue
                game, agent = fake.FakeGame(), fake.FakeGame()
                game.set_item(item.key, level)
                Effects(GameTransport(agent)).enable(item.key, level)
                self.assertEqual(_diff(game.mem, agent.mem, skip={ie.ARROWS_ADDR}), [],
                                 f"{item.key} {level}")
                self.assertEqual(game.mem[ie.ARROWS_ADDR], 0, item.key)

    def test_level_0_leaves_what_the_agents_revoke_leaves(self):
        for item in ITEMS:
            for level in _levels(item):
                if not level:
                    continue
                game, agent = fake.FakeGame(), fake.FakeGame()
                game.set_item(item.key, level)
                game.set_item(item.key, 0)
                fx = Effects(GameTransport(agent))
                fx.enable(item.key, level)
                fx.disable(item.key)
                self.assertEqual(_diff(game.mem, agent.mem, skip={ie.ARROWS_ADDR}), [],
                                 f"{item.key} {level}")
                self.assertEqual(game.items()[item.key], 0)

    def test_shared_slots_match_the_agent_step_by_step(self):
        runs = [
            [("shovel", 1), ("flute", 1), ("flute", 0), ("shovel", 0)],
            [("flute", 1), ("shovel", 1), ("shovel", 0), ("flute", 0)],
            [("mushroom", 1), ("powder", 1), ("powder", 0), ("mushroom", 0)],
            [("powder", 1), ("mushroom", 1), ("mushroom", 0)],
            [("blue_boomerang", 1), ("red_boomerang", 1), ("red_boomerang", 0)],
            [("red_boomerang", 1), ("blue_boomerang", 1), ("blue_boomerang", 0),
             ("red_boomerang", 0)],
            [("blue_boomerang", 1), ("shovel", 1), ("powder", 1), ("blue_boomerang", 0)],
        ]
        for run in runs:
            game, agent = fake.FakeGame(), fake.FakeGame()
            fx = Effects(GameTransport(agent))
            for key, level in run:
                game.set_item(key, level)
                if level:
                    fx.enable(key, level)
                else:
                    fx.disable(key)
                self.assertEqual(_diff(game.mem, agent.mem), [], f"{run} at {key} {level}")

    def test_shared_slot_pickup_leaves_the_siblings_alone(self):
        g = fake.FakeGame()
        g.set_item("shovel", 1)
        g.set_item("flute", 1)
        self.assertEqual(g.mem[INV_TRACK_ADDR] & 0x05, 0x05)
        self.assertEqual(g.mem[fake.FLUTE_SLOT], 3)          # enum, never OR-ed
        g.set_item("flute", 0)
        self.assertEqual(g.items()["shovel"], 1)
        self.assertEqual(g.mem[fake.FLUTE_SLOT], 1)          # falls back to the shovel

    def test_inactive_flute(self):
        g = fake.FakeGame()
        g.set_item("flute", 1, inactive=True)
        self.assertEqual(g.mem[INV_TRACK_ADDR] & 0x03, 0x02)
        self.assertEqual(g.mem[fake.FLUTE_SLOT], 2)
        self.assertEqual(g.items()["flute"], 1)
        g.set_item("shovel", 1)
        g.set_item("shovel", 0)
        self.assertEqual(g.mem[fake.FLUTE_SLOT], 2)          # back to the inactive flute
        g.set_item("flute", 0)
        self.assertEqual((g.mem[INV_TRACK_ADDR], g.mem[fake.FLUTE_SLOT]), (0, 0))

    def test_bow(self):
        g = fake.FakeGame()
        g.mem[BOW_FLAGS_ADDR] = 0x01                          # a bit the bow does not own
        g.set_item("bow", 2)
        self.assertEqual(g.mem[BOW_FLAGS_ADDR], 0xC1)
        self.assertEqual(g.mem[BOW_EQUIP_ADDR], 4)
        self.assertEqual(g.mem[ie.ARROWS_ADDR], 0)
        g.set_item("bow", 1)
        self.assertEqual(g.mem[BOW_FLAGS_ADDR], 0x81)
        self.assertEqual(g.mem[BOW_EQUIP_ADDR], 1)
        g.set_item("bow", 0)
        self.assertEqual((g.mem[BOW_FLAGS_ADDR], g.mem[BOW_EQUIP_ADDR]), (0x01, 0))

    def test_boots_keep_the_other_ability_bits(self):
        g = fake.FakeGame()
        g.mem[ie.ABILITY_ADDR] = 0x68
        g.set_item("boots", 1)
        self.assertEqual((g.mem[ie.BOOTS_ADDR], g.mem[ie.ABILITY_ADDR]), (1, 0x6C))
        g.set_item("boots", 0)
        self.assertEqual((g.mem[ie.BOOTS_ADDR], g.mem[ie.ABILITY_ADDR]), (0, 0x68))

    def test_simple_and_progressive(self):
        g = fake.FakeGame()
        g.set_item("mirror", 1)
        self.assertEqual(g.mem[BY_KEY["mirror"].addr], 2)     # 1 is the broken scroll icon
        g.set_item("sword", 3)
        self.assertEqual(g.mem[0xF359], 3)
        g.set_item("sword", 1)                                # exact level, not a max
        self.assertEqual(g.mem[0xF359], 1)

    def test_constants_agree_with_the_agent(self):
        self.assertEqual(fake.BOOTS_ADDR, ie.BOOTS_ADDR)
        self.assertEqual(fake.ABILITY_ADDR, ie.ABILITY_ADDR)
        self.assertEqual(fake.RUN_ABILITY_MASK, ie.RUN_ABILITY_MASK)
        self.assertEqual(fake.ARROWS_ADDR, ie.ARROWS_ADDR)
        self.assertEqual(fake.BOOM_SLOT, ie.BOOM_EQUIP_ADDR)
        self.assertEqual(fake.POWDER_SLOT, ie.POWDER_EQUIP_ADDR)
        self.assertEqual(fake.FLUTE_SLOT, ie.FLUTE_EQUIP_ADDR)
        self.assertEqual(fake.PICKUP["mushroom"][0], ie.MUSHROOM_BITS)
        self.assertEqual(fake.FLUTE_INACTIVE[0], ie.FLUTE_INACTIVE_BIT)
        self.assertEqual(fake.LOSS_BITS["flute"], ie.FLUTE_ANY_BITS)
        bitfields = {it.key for it in ITEMS if it.kind == "bitfield"}
        self.assertEqual(set(fake.PICKUP), bitfields)
        for key in bitfields:
            self.assertTrue(fake.PICKUP[key][0] & BY_KEY[key].mask, key)

    def test_bad_pokes(self):
        g = fake.FakeGame()
        with self.assertRaises(KeyError):
            g.set_item("nope", 1)
        for key, level in (("sword", 5), ("bow", 3), ("lamp", 2), ("boots", -1)):
            with self.assertRaises(ValueError):
                g.set_item(key, level)

    def test_addresses(self):
        for text in ("F379", "f379", "0xF379", "$7EF379", "7EF379", "F5F379"):
            self.assertEqual(fake.parse_addr(text), 0xF379, text)
        self.assertEqual(fake.parse_addr("16"), 0x16)
        for text in ("20000", "E00000", "", "zz"):
            with self.assertRaises(ValueError):
                fake.parse_addr(text)

    def test_reads_outside_wram_are_zero(self):
        g = fake.FakeGame()
        self.assertEqual(g.read(0xE00000, 4), bytes(4))
        self.assertEqual(g.read(W + fake.WRAM_SIZE - 1, 3), bytes(3))
        g.write(0xE00000, b"\x01")                            # ignored, no error

    def test_strip_decodes_what_hud_js_draws(self):
        g = fake.FakeGame()
        words = [0x255D + ord(c) - 65 if c.isalpha() else
                 0x2490 + int(c) if c.isdigit() else
                 0x2806 if c == ":" else 0x247F for c in "SWORD SENT TO BO 1:2"]
        g.write(W + fake.STRIP, b"".join(w.to_bytes(2, "little") for w in words))
        self.assertEqual(g.strip_text(), "SWORD SENT TO BO 1:2")


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    """The real listeners on ephemeral ports: usb2snes over websockets, the
    control API over HTTP."""

    async def asyncSetUp(self):
        self.game = fake.FakeGame()
        self.runners, ws_port, ctl_port = await fake.start(self.game, "127.0.0.1", 0)
        self.ws_url = f"ws://127.0.0.1:{ws_port}/"
        self.ctl = f"http://127.0.0.1:{ctl_port}"
        self.http = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.http.close()
        for runner in self.runners:
            await runner.cleanup()

    async def post(self, path, status=200):
        async with self.http.post(self.ctl + path) as r:
            self.assertEqual(r.status, status, path)
            return await r.json()

    async def get(self, path, status=200):
        async with self.http.get(self.ctl + path) as r:
            self.assertEqual(r.status, status, path)
            return await r.json()

    @staticmethod
    async def send(ws, op, *operands):
        await ws.send(json.dumps({"Opcode": op, "Space": "SNES", "Operands": list(operands)}))

    async def ask(self, ws, op, *operands):
        await self.send(ws, op, *operands)
        return await asyncio.wait_for(ws.recv(), 5)

    async def read(self, ws, addr, size):
        return await self.ask(ws, "GetAddress", f"{W + addr:x}", f"{size:x}")

    async def write(self, ws, addr, data):
        await self.send(ws, "PutAddress", f"{W + addr:x}", f"{len(data):x}")
        await ws.send(bytes(data))

    async def test_handshake_like_snes_js(self):
        async with connect(self.ws_url) as ws:
            self.assertIn('"SNI-fake"', await self.ask(ws, "AppVersion"))
            await self.send(ws, "Name", "HyruleLink")                 # no reply
            self.assertIn('"emunwa://fake-alttp:48879"', await self.ask(ws, "DeviceList"))
            await self.send(ws, "Attach", "emunwa://fake-alttp:48879")  # no reply
            info = await self.ask(ws, "Info")                         # the next reply is Info's
            self.assertIn('"alttpr.sfc"', info)

    async def test_poked_sword_reads_back_at_f5f359(self):
        state = await self.post("/item/sword/2")
        self.assertEqual(state["items"]["sword"], 2)
        async with connect(self.ws_url) as ws:
            self.assertEqual(await self.ask(ws, "GetAddress", "f5f359", "1"), b"\x02")

    async def test_the_agents_tracked_range_in_one_read(self):
        await self.post("/item/bow/2")
        await self.post("/item/flute/1")
        async with connect(self.ws_url) as ws:
            snap = await self.read(ws, TRACKED_START, TRACKED_SIZE)
        self.assertEqual(len(snap), TRACKED_SIZE)
        self.assertEqual(discovered_level(BY_KEY["bow"], snap[BOW_FLAGS_ADDR - TRACKED_START]), 2)
        self.assertEqual(discovered_level(BY_KEY["flute"], snap[INV_TRACK_ADDR - TRACKED_START]), 1)

    async def test_several_pairs_in_one_getaddress(self):
        await self.post("/module/7")
        await self.post("/item/lamp/1")
        async with connect(self.ws_url) as ws:
            got = await self.ask(ws, "GetAddress", f"{W + 0x10:x}", "1", f"{W + 0xF34A:x}", "1")
        self.assertEqual(got, b"\x07\x01")

    async def test_putaddress_lands(self):
        async with connect(self.ws_url) as ws:
            await self.write(ws, 0xF34A, b"\x01")
            await self.write(ws, 0xF359, b"\x03")
            self.assertEqual(await self.read(ws, 0xF359, 1), b"\x03")   # both writes done
        self.assertEqual((await self.get("/mem/F34A"))["bytes"], [1])
        items = (await self.get("/"))["items"]
        self.assertEqual((items["lamp"], items["sword"]), (1, 3))

    async def test_a_hud_flag_write_is_consumed(self):
        text = "HELLO"
        words = [0x255D + ord(c) - 65 for c in text]
        async with connect(self.ws_url) as ws:
            await self.write(ws, fake.STRIP, b"".join(w.to_bytes(2, "little") for w in words))
            await self.write(ws, 0x16, b"\x01")
            self.assertEqual(await self.read(ws, 0x16, 1), b"\x00")
        self.assertTrue((await self.get("/"))["strip"].startswith("HELLO"))

    async def test_bow_poke(self):
        await self.post("/item/bow/2")
        self.assertEqual((await self.get("/mem/F38E"))["bytes"][0] & 0xC0, 0xC0)
        self.assertEqual((await self.get("/mem/F340"))["bytes"], [4])
        await self.post("/item/bow/1")
        self.assertEqual((await self.get("/mem/F38E"))["bytes"][0] & 0xC0, 0x80)
        self.assertEqual((await self.get("/mem/F340"))["bytes"], [1])

    async def test_boots_poke_sets_the_run_flag(self):
        state = await self.post("/item/boots/1")
        self.assertEqual(state["ability"] & 0x04, 0x04)
        self.assertEqual(state["items"]["boots"], 1)
        self.assertEqual((await self.get("/mem/F355"))["bytes"], [1])
        state = await self.post("/item/boots/0")
        self.assertEqual((state["ability"] & 0x04, state["items"]["boots"]), (0, 0))

    async def test_inactive_flute_query(self):
        await self.post("/item/flute/1?inactive=1")
        self.assertEqual((await self.get("/mem/F38C"))["bytes"], [0x02])
        self.assertEqual((await self.get("/mem/F34C"))["bytes"], [2])

    async def test_module(self):
        self.assertEqual((await self.post("/module/7"))["module"], 7)
        async with connect(self.ws_url) as ws:
            self.assertEqual(await self.read(ws, 0x10, 1), b"\x07")
        self.assertEqual((await self.post("/module/0x19"))["module"], 0x19)
        await self.post("/module/zz", 400)
        await self.post("/module/256", 400)

    async def test_mem_routes(self):
        self.assertEqual(await self.post("/mem/F379/68"), {"bytes": [0x68]})
        self.assertEqual(await self.get("/mem/F378?n=3"), {"bytes": [0, 0x68, 0]})
        self.assertEqual(await self.get("/mem/7EF379"), {"bytes": [0x68]})
        self.assertEqual((await self.get("/"))["ability"], 0x68)
        await self.post("/mem/F379/100", 400)
        await self.post("/mem/30000/00", 400)
        await self.get("/mem/zz", 400)
        await self.get("/mem/F379?n=0", 400)

    async def test_bad_item_pokes(self):
        await self.post("/item/nope/1", 404)
        await self.post("/item/sword/5", 400)
        await self.post("/item/sword/x", 400)

    async def test_state_shape(self):
        state = await self.get("/")
        self.assertEqual(set(state), {"module", "strip", "items", "ability", "arrows"})
        self.assertEqual(list(state["items"]), [it.key for it in ITEMS])

    async def test_reset_zeroes(self):
        await self.post("/module/7")
        for key, level in (("sword", 4), ("bow", 2), ("boots", 1), ("shovel", 1)):
            await self.post(f"/item/{key}/{level}")
        await self.post("/mem/F377/1E")
        async with connect(self.ws_url) as ws:
            await self.write(ws, fake.STRIP, (0x255D).to_bytes(2, "little"))
            await self.read(ws, 0x10, 1)
        state = await self.post("/reset")
        self.assertEqual(state, {"module": 1, "strip": " " * 20, "ability": 0, "arrows": 0,
                                 "items": {it.key: 0 for it in ITEMS}})
        async with connect(self.ws_url) as ws:
            self.assertEqual(await self.read(ws, TRACKED_START, TRACKED_SIZE),
                             bytes(TRACKED_SIZE))


class CliTests(unittest.TestCase):
    def test_runs_from_the_repo_root_without_pythonpath(self):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        proc = subprocess.Popen(
            [sys.executable, os.path.join("tools", "fake_snes.py"), "--port", "0"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            lines = []
            reader = threading.Thread(target=lambda: lines.append(proc.stdout.readline()),
                                      daemon=True)
            reader.start()
            reader.join(20)
            self.assertTrue(lines and lines[0], "no banner from the fake")
            m = re.search(r"control on http://localhost:(\d+)", lines[0])
            self.assertIsNotNone(m, lines[0])
            with urllib.request.urlopen(f"http://127.0.0.1:{m.group(1)}/", timeout=5) as r:
                self.assertIn(b'"module": 1', r.read())
        finally:
            proc.kill()
            proc.wait(10)
            proc.stdout.close()
            proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
