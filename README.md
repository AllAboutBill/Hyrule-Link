# HyruleLink

**https://www.billogna.lol/hyrulelink/** and **https://hyrulelink.billogna.lol**

Every player their own seed. One shared inventory.

Co-op for A Link to the Past Randomizer where the progression items are one
pool. Each player plays their own seed, from alttpr.com or the desktop app's
seed generator; only the inventory is shared. Each item has one holder at a
time: find it and it is yours, until a friend finds one in their own world and
it moves to them, out of your game. The board lets you claim it back. Your
game changes live.

No install beyond SNI, no accounts. The room link is the key.

HyruleLink reads inventory bytes and writes items. That is why it is its own
app and not an EtherNet mode: EtherNet's rule is that only the module byte
leaves a racer's PC.

## What it does

| | |
|---|---|
| **Shared pool** | 29 progression items (sword, bow, boots, hookshot, the rods, the medallions, Moon Pearl and the rest; no ammo, bottles or dungeon items). One holder per item. A find always takes it. You can claim only what you have found yourself, and you get it back at the best tier *you* found. |
| **Board** | The room page: every item with its holder and tier, Claim with its cooldown, the players with their game-link state, the activity log. Watchers see the same board without Claim. |
| **Game link** | The page talks to **SNI** or **QUsb2Snes** on the player's PC, which covers snes9x-nwa and other NWA emulators, RetroArch, BizHawk / snes9x-rr (Lua) and the FXPak Pro. It reports finds and writes grants and revokes, only while a save is loaded. |
| **In-game messages** | Lines on the HUD strip in the game's own font, with no ROM patch: `SWORD TAKEN FROM ANA`. One switch per browser. |
| **Modes** | Normal, Hot Potato, Chaos, and Custom: a rule editor with seven presets. The host picks. |
| **Host controls** | Rename, mode, cooldown, custom rules, mark an item found or set its holder for any player, remove a player, reset progression. |
| **Rooms** | "Your rooms on this browser" on the front page is the way back in; "Live rooms" lists the rooms anyone can watch. A room nobody uses for 14 days is removed. `operator.html` lists and deletes every room, behind a key. |
| **Desktop app** | The Windows app from before the browser client still works against the same server and rooms: seed generation, sprites, emulator launch, direct NWA and RetroArch links, a server of your own. |

## The documents

| Read this | When |
|---|---|
| [docs/USING.md](docs/USING.md) | You play or host. A co-op start to finish, linking a game, what the page reads and writes, modes, the desktop app. |
| [docs/DEVELOPING.md](docs/DEVELOPING.md) | You are changing the code. Local run, the fake game, tests, the hard constraints, the traps already paid for. |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | The exact interfaces: routes, websocket messages, the room document, the game memory. |
| [deploy/README.md](deploy/README.md) | It is on the droplet: what runs where, how to deploy, check and roll back. |
| [docs/BROWSER_PLAN.md](docs/BROWSER_PLAN.md) | The record of how the browser client was built, package by package. |

## How it moves

```
player's browser  -- /ws "ui" ------>  HyruleLink server
   |              -- /ws "agent" --->  rooms, the ledger, the rules
   |
   +--- ws://localhost:23074 ---> SNI / QUsb2Snes ---> emulator or FXPak Pro
                                  reads $7E0010 (the module) and $7EF342-$7EF38E (the inventory),
                                  writes item bytes, the boots run flag, the bow and arrow
                                  bytes, 20 HUD cells and one flag
```

The page keeps two sockets to the room: `ui` for the board, claims and host
controls, and `agent` once a game is linked on that PC. The desktop app's
agent uses the same `agent` role; the server cannot tell them apart. The
server decides who holds what and sends `grant`, `revoke` and `notify` to that
player's agent, and the player's own page (or app) writes them into the game
next to it. The server never touches a game. What leaves a player's PC: the
items it finds (key and level), whether a game is linked, and whether each
write took.

## Run it here

Windows, no terminal: `Install.cmd` once (makes `.venv`, installs
`requirements.txt`, fetches SNI), then `Start Server.cmd` for a server at
http://localhost:5019/ or `Play.cmd` for the desktop app.

By hand, from the repo root in Git Bash:

```bash
.venv/Scripts/python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.venv/Scripts/python.exe run_server.py --port 5019          # http://localhost:5019/
.venv/Scripts/python.exe -m unittest discover -s tests       # everything, node units included
.venv/Scripts/python.exe tools/fake_snes.py                  # a stand-in game, see DEVELOPING
```

## The repo

```
server/       app.py (FastAPI: REST, /ws, web/ at the root), ledger.py (who holds what, the rules,
              the connection hub), operator.py (the operator gate), db.py (sqlite), auth.py (Discord,
              desktop app only), rate_limit.py, names.py
shared/       items.py (the catalog), protocol.py (message names), rules.py (rule defaults, presets)
web/          index.html (front door), room.html (the room), operator.html
web/js/       items.js (generated), effects.js, agent.js (the browser agent), hud.js, snes.js,
              game.js, room.js, home.js, operator.js, claim-policy.js
agent/        the desktop app's agent: agent.py, effects.py, sni/ (links, item writes, HUD), romtools/
agent_gui.py  the desktop app (Tk)
tools/        fake_snes.py (a stand-in game), coop_check.js (a two-player round, no browser),
              gen_items_js.py (writes web/js/items.js), make_mark.py; for the desktop app:
              install_sni.ps1, sni/ (SNI's Lua connector), hud_text_test.py, fonts/
tests/        unittest suite; web/units.js (node units, run by the suite too)
deploy/       deploy.sh and what it installs, see deploy/README.md
docs/         USING, DEVELOPING, PROTOCOL, BROWSER_PLAN
```

## What is proven, and what is not

Proven 2026-09-21, on this PC:

- **The test suite.** Server routes, including the desktop app's frozen
  shapes; websocket hellos for all three roles; the operator gate; the ledger
  and rules; the Python agent's item writes; the fake bridge; Python 3.10
  syntax; `items.js` against the catalog; the page URL rules. Node units for
  `hud.js`, `snes.js`, `items.js`, `effects.js` and `agent.js`, and a replay
  of every grant and revoke through the Python and the JS item writers, which
  make the same reads and writes in the same order.
- **A two-player round with no browser** (`tests/test_coop_e2e.py` running
  `tools/coop_check.js`): two fake games, the real server, the page's own game
  modules under node. Seven steps, each within 3 s: (1) a find; (2) a steal by
  find, with a line on both HUD strips; (3) a claim back at the player's own
  tier; (4) the boots run flag leaving with the boots and put back when the
  game drops it; (5) silver bow at one player's tier and wood at the other's,
  with the equip byte and 30 arrows on the claim back; (6) a revoke that lands
  on the file select waiting for the save, then applied with a resync and no
  phantom pickup; (7) every pickup sent is a find the game made.
- **The room page in Chrome against `tools/fake_snes.py`** (the room
  package's acceptance run): the game row found the fake, finds poked into it
  reached the board at the right tier, and a claim by a second player put a
  line on the fake's HUD strip. Real clicks, focus rings and the clipboard
  were not part of it.
- **A walk through in real Chrome** against a local server and
  `tools/fake_snes.py`. Two players on two origins: Ana's page linked the fake,
  Bo's page had *Link a game on this browser* off and his game linked through
  a second fake. The seven steps above all passed in the browser, and so did
  every host control (the Hot Potato and Chaos banners and timers, cooldown,
  rename, the found and holder chips, remove, reset, the custom rules dialog),
  the spectator page, the operator page, the invite link, the watch
  redirects, 390 px layouts and keyboard-only use. With the room tab in the
  background for over a minute, finds and grants still landed within about
  half a second. The pass fixed six page bugs, among them a false "not
  answering" bridge message caused by Chrome holding back new sockets.
- **One game link per browser.** Two tabs of the same room in one Chrome
  profile: only one linked, *Link this tab instead* moved the link, and
  closing the linked tab let the other take over in about 130 ms, with finds
  still reaching the board.

**Live deploy: pending.**

Proven elsewhere and relied on: EtherNet's `snes.js` and `hud.js`, which
these are copies of, wrote HUD lines into a real snes9x-nwa through the real
SNI from a live HTTPS page (2026-09-20), and read through QUsb2Snes 0.7.35 for
18 minutes (2026-09-21).

Not proven yet: item writes from a browser into a real game; writes through
QUsb2Snes; an FXPak, where a WRAM write goes through the cartridge's command
hook; a real two-PC session. Safari does not let a web page reach a program
on the same computer, so it cannot link a game.

## Desktop app

`Play.cmd` (after `Install.cmd`) opens the Windows app. It still works and
still points at `https://hyrulelink.billogna.lol`. It joins the same rooms as
the browser, and adds what a web page cannot do:

- **Seeds:** *Generate seed* rolls a seed on alttpr.com from a preset, or
  patches one from its permalink, onto your own JP 1.0 base ROM, with a player
  sprite, palette shuffle, a follower sprite and MSU-1 packs.
- **Emulator:** *Launch* starts it, set up for a network link.
- **Links:** straight to snes9x-nwa (NWA) and RetroArch (network commands),
  or through SNI, which `Install.cmd` fetches and *Start SNI* starts.
  RetroArch also shows the lines on its on-screen display.
- **Your own server:** *Start local server* runs one on this PC for the LAN;
  *Create a public internet link* adds a free Cloudflare quick tunnel
  (cloudflared downloads on first use).
- **Discord login:** *Connect Discord* keeps your seat across devices and gives
  the Discord server's mods admin in every room.
- **BillognaBot:** when the streaming bot runs on the same PC, the agent hands
  its lines to the bot's HUD writer (`POST http://127.0.0.1:5000/api/hud/say`)
  instead of racing it on the same strip. `HYRULELINK_BOT_HUD=0` turns that off.

Either the app or a browser is your game link, not both: see
[docs/USING.md](docs/USING.md#one-game-link-per-player).

RAM addresses and game-mode gating follow
[ALTTPR-REFERENCE](https://github.com/AllAboutBill/ALTTPR-REFERENCE) and the
tools the agent was built from (TwitchBot SNI, AlttprHelper,
ALTTPRFollowerInjector). Item sprites in `web/items/` come from the ALTTPR
community tracker. The SNI (MIT) and cloudflared (Apache-2.0) programs are
downloaded from their upstream releases, not committed.
