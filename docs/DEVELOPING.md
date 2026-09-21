# Developing HyruleLink

Commands are for Git Bash, from the repo root. `Install.cmd` makes `.venv`;
everything runs from it.

## Local run

```bash
.venv/Scripts/python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.venv/Scripts/python.exe run_server.py --port 5019          # http://localhost:5019/
```

`run_server.py` flags: `--host` (default `0.0.0.0`, the LAN; `127.0.0.1` keeps
it on this PC), `--port` (5019), `--open` (a browser tab), `--reload`.
`requirements-dev.txt` is test-only: `httpx` for FastAPI's TestClient,
`aiohttp` for the fake game. The droplet installs `requirements.txt` only.

The server reads a git-ignored `.env` at the repo root (a real environment
variable wins). `.env.example` has all of it:

| Variable | |
|---|---|
| `HYRULELINK_DB` | the sqlite file, default `server/hyrulelink.db` |
| `HYRULELINK_ROOM_TTL_DAYS` | a room idle this long is deleted (14); checked at start and twice a day |
| `HYRULELINK_OPERATOR_KEY_FILE` | the operator key, default `server/operator.key`; relative paths from the repo root |
| `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `DISCORD_REDIRECT_URI`, `DISCORD_GUILD_ID`, `DISCORD_MOD_ROLE_IDS`, `DISCORD_ADMIN_USER_IDS`, `SESSION_SECRET` | Discord login. Only the desktop app has a button for it. Unset, it is off. |
| `HYRULELINK_BOT_HUD` | the desktop agent's BillognaBot hand-off URL; `0` turns it off |

The operator page, locally:

```bash
.venv/Scripts/python.exe -c "import secrets; print(secrets.token_urlsafe(24))" > server/operator.key
```

then restart the server (the key is read once, at start) and open
http://localhost:5019/operator.html. With no key file the operator routes
answer 404.

Deploying: read [deploy/README.md](../deploy/README.md) first.
`bash deploy/deploy.sh` ships `origin/master` to the droplet, `--check` looks
and changes nothing, `--rollback` goes back one deploy.

## A game without a game

```bash
.venv/Scripts/python.exe tools/fake_snes.py            # usb2snes on ws://localhost:23074, control on :23075
curl -s localhost:23075/                               # module, HUD strip, every item's level, run flag, arrows
curl -s -X POST localhost:23075/module/7               # a dungeon: a save is loaded
curl -s -X POST localhost:23075/item/sword/2           # find the Master Sword, the way the game does
curl -s -X POST localhost:23075/item/sword/0           # lose it again
curl -s localhost:23075/mem/F379                       # one byte: {"bytes": [...]}
curl -s -X POST localhost:23075/mem/F379/00            # clear the run flag behind the page's back
curl -s -X POST localhost:23075/module/1               # back to the file select
curl -s -X POST localhost:23075/reset                  # zeroed memory, file select
```

Open http://localhost:5019/, open a room, and the Game row says *Emulator
(NWA) through SNI*. The full control API is in
[PROTOCOL.md](PROTOCOL.md#the-fake-game).

Do not run it beside the real SNI or QUsb2Snes: they want the same port. A
second browser profile on the same PC finds the same fake, so a second player
there must switch *Link a game on this browser* off; two players on one PC
means the node harness below, which gives each its own fake.

Linking a page to a REAL game from a dev session writes to it: the agent
socket's hello pushes a grant or revoke for all 29 items as soon as a save is
loaded. Use a throwaway save, and check nobody is mid-run on that emulator.

## Tests

```bash
.venv/Scripts/python.exe -m unittest discover -s tests         # everything: server, agents, fake, node units, the two-player round
node tests/web/units.js                                        # just the JS units: hud, snes, items, effects, agent
node tests/test_claim_policy.js
.venv/Scripts/python.exe -m unittest tests.test_coop_e2e -v    # the two-player round alone
.venv/Scripts/python.exe tools/gen_items_js.py --check         # web/js/items.js against shared/items.py
```

After changing `shared/items.py`, write `web/js/items.js` again with
`.venv/Scripts/python.exe tools/gen_items_js.py`.

`tests/test_coop_e2e.py` starts two fakes (23174 and 23274, control one port
up) and the server on 5919 with a temp database, then runs
`tools/coop_check.js`: two players, the page's own game modules under node, no
browser. It needs node 22+ and skips without it. By hand, each in its own
terminal:

```bash
.venv/Scripts/python.exe tools/fake_snes.py --port 23174
.venv/Scripts/python.exe tools/fake_snes.py --port 23274
.venv/Scripts/python.exe run_server.py --host 127.0.0.1 --port 5919
node tools/coop_check.js --server http://127.0.0.1:5919 --a ws://localhost:23174 --b ws://localhost:23274
```

It prints each step and `PASS`. `--verbose` adds the agents' own log lines.
Against `run_server.py` it leaves a room behind in `server/hyrulelink.db`.

CI (`.github/workflows/tests.yml`, Windows, Python 3.12, node 22) installs
both requirements files and runs the suite, the node units, the claim policy
test and `compileall`.

## Hard constraints

- **FastAPI and uvicorn stay.** No new server dependency; test-only ones go in
  `requirements-dev.txt`.
- **Python 3.10** syntax and stdlib in `server/`, `shared/`, `run_server.py`
  and `tools/fake_snes.py`: the droplet is Ubuntu 22.04. `tests/test_py310.py`
  enforces it.
- **Relative URLs everywhere in `web/`.** No leading slash in `href`, `src`,
  `action`, `fetch` or `new URL`, and no `url(/` in CSS; the websocket is
  `new URL('ws', location.href)`. The site runs at
  `https://www.billogna.lol/hyrulelink/` (nginx strips the prefix) as well as
  at `https://hyrulelink.billogna.lol`. `tests/test_web_js.py` enforces it.
- **The desktop app's routes are frozen.** New fields and new routes only;
  never rename, remove or reshape: `GET api/me` (header `X-HL-Session`),
  `POST auth/device/start`, `GET auth/device/poll?pair=`, `GET api/my-rooms`,
  `POST api/rooms/{code}/rejoin`, `POST api/rooms` (`display_name`, `name`,
  `cooldown_s`), `POST api/rooms/{code}/resume`, `POST api/rooms/{code}/join`,
  `GET health`, `/ws` as `ui` (with an optional `session`) and as `agent`, and
  `{base}/?watch={pub_id}`. It reads `players` and `ledger` from the state
  document and `code`, `pub_id`, `player_id`, `player_token` and `items` from
  the create, join and resume replies. `tests/test_server_api.py` holds the
  shapes.
- **Only the player's own client writes to a game.** The server never does:
  it sends `grant`, `revoke` and `notify` to that player's agent socket.
- **Item writes are never optional while a game is linked.** The one switch
  is for HUD lines: `game.js` makes `say` a no-op and takes the line down.
  `snes.js` has no writes switch; do not add one.
- **Shared bytes are read-modify-write** (`$7EF379`, `$7EF38C`, `$7EF38E`):
  every read inside one retries 6 times and never falls back to 0. A 0 would
  clobber the other bits.
- **Every player plays their own seed.** Only the inventory is shared. No
  same-seed sync, no host-sets-seed, no race clock, no seed generation in the
  browser.
- **`shared/items.py` is the catalog.** `web/js/items.js` is generated from it
  and `agent/sni/memory_constants.py` by `tools/gen_items_js.py`;
  `tests/test_items_js.py` fails on drift. Never edit `items.js` by hand.
- **The two agents are interchangeable** on `/ws`: same messages, same write
  sequences. `tests/test_items_js.py` replays every grant and revoke through
  both and compares the reads and writes.
- **On-page wording is plain and short.** No tutorial voice, no exclamation
  marks, no emoji. Server event strings are data and are shown as they come.

## Traps (each of these cost time once)

### The game link

- **usb2snes**: `Attach` never answers (use `Info` as the confirmation);
  `GetAddress` answers in binary frames that may be split; one request at a
  time per socket; a reply that lands after its timeout would be read as the
  next request's answer, so a timeout closes the socket and starts clean.
- **A page behind a game is throttled.** `setInterval` drops to about once a
  second in a background tab, once a minute later on. The game loop in
  `snes.js` runs off a Worker, which is not clamped. Never move game or HUD
  work onto a plain timer. The page's own countdowns do run on one, so
  `room.js` redraws them on `visibilitychange`.
- **One bridge per PC.** SNI and QUsb2Snes both want 23074 (usb2snes) and
  65398 (Lua); the second to start reports the port in use. Trackers and bots
  with their own NWA connection are no clash.
- **A connect timeout that also retries doubles itself.** In `snes.js` the
  retry comes from `onclose`, once per socket; the 2.5 s timer only closes.
  EtherNet found the other way flooding QUsb2Snes until it stopped scanning.
- **A jammed bridge is not a missing one.** Two rounds in a row where a
  bridge takes the connection and does not answer are `stuck`: "running, but
  it is not answering". Saying "not found" sends people to start a second
  bridge, which fails with "port in use".
- **node's WebSocket fires `close` twice** for a socket closed while it is
  still connecting; Chrome fires it once. `snes.js` acts on the first, and the
  units fake both.
- **The back/forward cache keeps sockets open.** A page left for another one
  stayed in the cache with its room and bridge sockets up, and the room went
  on showing that player's game as linked. `pagehide` now closes the sockets
  and the bridge; a `pageshow` from the cache reloads.

### The agent

- **One contiguous read.** Every item address sits in `$7EF342`-`$7EF38E`
  (77 bytes), read once per poll; `$7EF379` (the run flag) is read from the
  same snapshot. Several items share `$7EF38C`.
- **First sight seeds, silently.** The first byte seen at an address is the
  baseline, not a find. A byte equal to `expected` is our own write echoing
  back: accepted, not reported. Otherwise each item on that byte is reported
  when its level went up.
- **The save gate.** Only playable modules (hex `06 07 08 09 0A 0B 0E`) are
  polled. An out-of-game module (`00`-`05`, `14`, `17`, `1B`: title, file
  select and its screens, loading, attract, save and quit, the where-to-start
  prompt) sets a flag; the next playable poll wipes the baseline and
  `expected` and sends `resync`, because a (re)loaded save reverts memory and
  those bytes are not finds. Death (`12`), door spotlights, the mirror and a
  boss victory are not out-of-game and cause no resync. `snes.js` resets the
  HUD strip on the same edge.
- **A link blip is not a reload.** When the bridge comes back on the same
  save, `resync` keeps the baseline, so a find made during the blip is still
  reported.
- **Grants wait for a save.** A grant or revoke reads the module first;
  outside a playable module, or when that read fails, it waits in `pending`
  (the newest per item wins) and the next playable poll applies it before it
  takes the snapshot. Otherwise it is written and read back, up to 3 tries,
  and `applied` reports how it went.
- **Grant and poll never interleave.** Everything that touches the game in
  `agent.js` runs through one promise chain.
- **Writes, per kind.** Progressive: the exact level, never +1. Simple: `give`
  (the Mirror is 2; 1 is a broken icon). Boots: `$7EF355` and bit 0x04 of
  `$7EF379`. Bow: `$7EF38E` bits 0x80 (has) and 0x40 (silver) are the truth;
  `$7EF340` is 1 wood, 4 silver, 0 none. Shared slots: ownership bits in
  `$7EF38C`, the slot bytes `$7EF341`, `$7EF344`, `$7EF34C` are enums kept in
  step. Never OR an enum: shovel|flute is 3, the active flute, and the shovel
  is gone.
- **The run flag is enforced every poll**, written only when wrong. On the
  poll that sees a boots find, the agent clears the flag the game just set
  (the boots are not the player's yet), and the server's grant sets it again
  a moment later. The Python agent does the same. A test that wants the flag
  waits for the grant.
- **The HUD strip resets on the out-of-game to playable edge**, before the
  agent's tick: a line queued before it is dropped. After `POST /module/7`
  from the file select, wait one poll (600 ms) before expecting strip text.
- **A find while the room is unreachable waits in `outbox`** and goes out
  right after the next hello. The hello's ownership push arrives first and
  revokes it for a moment; the find's grant puts it back.
- **Two agents for one player.** The newest hello wins and the older socket
  stops getting commands. It is not closed, and when it goes, the room marks
  the player as unlinked although the newer one is still registered, until
  that one says hello again (see
  [PROTOCOL.md](PROTOCOL.md#one-agent-per-player)). The page opens its agent
  socket only after it finds a game, and only with *Link a game on this
  browser* on.
- **29 items, not 31.** An early count was wrong. A hello gets
  `len(shared.items.ITEMS)` commands; use that, or `HLItems.ITEMS.length`.

### The server and the pages

- **`web/` is mounted at `/`, last.** A route added after the mount is never
  reached. `StaticFiles` does not take websockets, so `_WebFiles` closes one
  to an unknown path. `/static` stays mounted for old links.
- **Windows maps `.js` to `text/plain`** through the registry, and with
  `nosniff` a browser then refuses the script. `server/app.py` registers the
  types itself.
- **Images are cached for a week** (png, svg, woff2, ttf, ico). A changed
  image gets a new file name.
- **The `/shared/` alias** (billogna.lol's old aurora chrome) exists only in
  the subdomain's nginx block. Nothing may use it; a test checks.
- **`request.base_url` assumes the subdomain.** `auth/device/start` builds its
  `login_url` from it, and `auth/callback` redirects to `/`. Under
  `/hyrulelink/` both would point at billogna.lol itself. Only the desktop app
  uses them, and it uses the subdomain.
- **`you` is null for a watcher, and `owner` is null for an unheld item**, so
  `owner === you` is true for both. The old page showed watchers every unheld
  item as theirs. Compare with `you != null` first.
- **A `<dialog>`'s `close` event is held back in a hidden tab.** Cleanup that
  must run when the rules dialog closes is called directly from every way the
  page closes it, and from the event only for Escape.
- **The shuffle and hold clocks re-arm without a state.** The server restarts
  its timer even when a shuffle moves nothing, and then sends nothing. The
  page re-arms its own countdown the same way.
- **The operator key is read once**, at start. Create the file, then restart.

### The repo and this PC

- **CRLF worktrees.** Files are LF in git, but `core.autocrlf=true` checks
  them out CRLF. Git normalizes on commit; tests that compare text strip
  `\r`. A tool that writes LF (`tools/gen_items_js.py`) leaves the file
  showing as modified with an empty diff. `deploy/deploy.sh` must stay LF on
  disk (`.gitattributes`).
- **The harness needs node 22+** for the global WebSocket and fetch. The
  units fake the WebSocket and do not.
- **A fake on 23074 is found by everything on this PC**: every page with
  linking on, and AlttprHelper's auto-tracker, which attaches and reads (only
  reads). Tests use 23174 and 23274 and never 23074.

## Where things came from

| Piece | Source |
|---|---|
| usb2snes client, Worker ticker (`web/js/snes.js`) | EtherNet's `web/js/snes.js`, which took them from RaceConnect's `web/js/publish.js`. Changed here: the bridge URLs, `onTick`, no writes switch. |
| HUD strip writer (`web/js/hud.js`) | EtherNet's `web/js/hud.js`, verbatim but for the header comment; a port of this repo's `agent/sni/hud_text.py` by way of the Twitch bot (`F:\TwitchBot\BillognaBot\sni\hud_text.py`), plus the `:` glyph |
| HUD text cleaner (`Hud.clean`) | the Twitch bot's `ingame_text_plugin.hud_clean`, by way of EtherNet |
| browser agent and item writes (`agent.js`, `effects.js`) | this repo's `agent/agent.py`, `agent/effects.py`, `agent/sni/item_effects.py`, ported line for line |
| fake game (`tools/fake_snes.py`) | EtherNet's `tools/fake_snes.py`, plus the item-poke control API |
| two-player harness (`tools/coop_check.js`) | the shape of EtherNet's `tools/hud_check.js` |
| look, fonts, front door, operator page and gate | EtherNet's `ethernet.css` tokens and components, its bundled fonts (StreamStudio's), `index.html`, `operator.html` and its server's operator key rules |
| the mark (`web/img/hookshot.svg`, `mark.svg`) | the Hookshot item sprite, turned into a pixel-exact SVG by `tools/make_mark.py` (adapted from EtherNet's) |
| RAM addresses, game modes | [ALTTPR-REFERENCE](https://github.com/AllAboutBill/ALTTPR-REFERENCE), z3randomizer, and the tools the agent was built from (TwitchBot SNI, AlttprHelper, ALTTPRFollowerInjector) |
| item sprites (`web/items/`) | the ALTTPR community tracker |

Copied, never imported. None of those projects needs to be present. How the
browser client was built, and why each decision went the way it did:
[BROWSER_PLAN.md](BROWSER_PLAN.md).
