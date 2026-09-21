# HyruleLink: interfaces

A reference, not an introduction. Server times are Unix seconds. Paths are
relative to the site root: `https://www.billogna.lol/hyrulelink/` or
`https://hyrulelink.billogna.lol/` (one server behind both), or
`http://localhost:5019/` here. Message names are the constants in
`shared/protocol.py`.

## Identity

- **room code**: 10 characters from `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`
  (older rooms have 6). Any case works in routes and hellos. The code is the
  secret: whoever has it can join as a player.
- **pub_id**: 12 url-safe characters, case-sensitive. The public handle: watch
  links, the public room list, `room` in the state document. It never lets
  anyone play.
- **player**: `player_id` (an integer, public, in the state document) and
  `player_token` (24 url-safe characters, private). No accounts.
- **host**: the player who opened the room (`host` in the state document). It
  never changes. The host, or a Discord admin, may send `admin_*` messages.
- **Discord admin**: a session signed by the server after the desktop app's
  device login, sent as `session` in the ui hello or as the `hl_session`
  cookie. The web pages never send one.
- **operator key**: one key in a file on the server; sent only as the
  `X-Operator-Key` header.

The browser keeps everything in its localStorage (values are JSON):

| Key | |
|---|---|
| `hyrulelink.name` | the last name typed |
| `hyrulelink.rooms` | `{CODE: {player_id, player_token, name, title, t}}`, `t` in ms. The front page shows the newest 8 and looks each up; the server's own `{"detail": "no such room"}` drops one. |
| `hyrulelink.link` | `"on"` \| `"off"`: this browser links a game (default on) |
| `hyrulelink.hud` | `"on"` \| `"off"`: in-game lines (default on) |
| `hyrulelink.operator` | the operator key, plain text, until *Forget key* |
| `hl_name`, `hl_rooms` | the old page's. Copied into the new keys once, when those are missing; never read again, never deleted. |

| Page | |
|---|---|
| `index.html` | the front door. `?room=CODE` and `?watch=PUB` go on to `room.html` with the same query before anything is drawn (the desktop app opens `{base}/?watch=PUB`). |
| `room.html?room=CODE` | a player: resume the seat in storage, else the join cover |
| `room.html?watch=PUB` | a watcher: one spectator socket, no seat, no game link |
| `operator.html` | every room, behind the operator key |

## HTTP

"Frozen" routes are the ones the desktop app calls: their shape does not
change, ever. "New" came with the browser client.

| | | |
|---|---|---|
| `POST api/rooms` (frozen) | `{display_name, name?, cooldown_s?}` | the room payload. The caller is the host. An empty `name` gets a made-up one; `cooldown_s` is clamped to 0-3600, default 5. |
| `POST api/rooms/{code}/join` (frozen) | `{display_name}` | the room payload for a new player (a Discord-linked caller already in the room gets their seat back). 404 `no such room`. |
| `POST api/rooms/{code}/resume` (frozen) | `{player_id, player_token}` | the room payload for that seat. 404 `no such room` or `player not found in room`. |
| `GET api/rooms/{code}` (new) | | `{code, pub_id, name, host, mode, cooldown_s, players:[{id, name}], live:{uis, agents}}` or 404 `no such room`: what a page needs before it has a seat. `live` counts open sockets. Not counted as activity. |
| `GET api/rooms` | | `{rooms:[{pub_id, name, last_active, players, player_list:[{name, avatar}]}], login_enabled}`, most recently active first. Never a code. |
| `POST api/rooms/{code}/rejoin` (frozen) | Discord session | the room payload for your Discord-linked seat; 401 without a login, 404 when you are not in it |
| `GET api/my-rooms` (frozen) | Discord session | `{rooms:[...]}`, the rooms your Discord login has a seat in |
| `POST api/rooms/{handle}/delete` | Discord admin session | `{ok}`; `handle` is a pub_id or a code. 403 otherwise. |
| `GET api/me` (frozen) | header `X-HL-Session`, or the cookie | `{logged_in, login_enabled}` plus `{name, avatar, admin}` when logged in |
| `POST auth/device/start` (frozen) | | `{pair, login_url}`; 404 when Discord login is not configured |
| `GET auth/device/poll?pair=` (frozen) | | `{status:"pending"}`, `{status:"expired"}`, or once `{status:"ok", token, name, avatar, admin}` |
| `GET auth/login?pair=`, `GET auth/callback`, `POST auth/logout` | | the Discord OAuth round trip: the `hl_session` cookie, or the pairing the desktop app polls |
| `GET health` (frozen) | | `{ok, ts}` |
| `GET api/health` (new) | | `{ok, version, rooms, ts}`: `version` from the `VERSION` file, `rooms` = rooms loaded in memory |
| `GET api/operator/rooms` (new) | header `X-Operator-Key` | `{ok, now, ttl_days, rooms:[{code, pub_id, name, mode, created_at, last_active, in_use, players:[{id, name, host, agent, emu}], uis}]}`, most recently active first. `in_use` = any socket open. No token. |
| `POST api/operator/rooms/{code}/delete` (new) | header `X-Operator-Key` | `{ok, deleted: CODE}`; 404 unknown room. Closes every socket in it, then erases it. |
| `GET /`, `GET {file}` | | `web/`, the pages and their files. Also under `static/`, kept for old links. |

The room payload (create, join, resume, rejoin):
`{code, pub_id, name, cooldown_s, host, player_id, player_token, players:[{id, name}], items:[{key, name, image}]}`.

Errors are FastAPI's `{"detail": "text"}` with 401, 403, 404, 422 or 429.
Limits per address per minute: create 10, join 20, resume 20, lookup 120,
device login 10. The address is the peer, or the last `X-Forwarded-For` hop
when the peer is loopback (nginx).

The operator routes are 404 `not found` until the key file holds a key
(`HYRULELINK_OPERATOR_KEY_FILE`, default `server/operator.key`, read once at
start). A wrong key is 403 `wrong key`, and 20 wrong keys in an hour from one
address are 429 for the rest of that hour, right key included. The key is
only read from the header, never a query string (nginx logs URLs), is
compared with `secrets.compare_digest`, and is never logged.

Every response: `Cache-Control: public, max-age=604800` for `.png .svg
.woff2 .ttf .ico` that were found, `no-cache` for everything else;
`X-Content-Type-Options: nosniff`; `Referrer-Policy: no-referrer`;
`X-Frame-Options: DENY` on `operator.html`.

## Websocket `ws`

`/ws` at the root; a page builds it as `new URL('ws', location.href)` with
`ws:` or `wss:`. The first message is `hello`. Anything else gets
`reject "expected hello"`; a hello that fails gets a `reject` and the socket
is closed (1000). There are no custom close codes. Deleting a room sends each
of its sockets `reject "room closed"`, then closes it.

| hello | | |
|---|---|---|
| agent | `{type:"hello", role:"agent", room, player_id, token}` | a game link: the browser page's (`web/js/game.js`) or the desktop app's. The server cannot tell them apart. |
| ui | `{type:"hello", role:"ui", room, player_id, token, session?}` | a player's board |
| spectator | `{type:"hello", role:"spectator", watch}` | a watcher. `watch` is the pub_id; `room` is taken in its place, and a code works too. |

Hello rejects: `room not found`, `bad room/player token`. The room page takes
`room not found` or `room closed` as "the room is gone" (forget the seat, go
to the front page) and `bad room/player token` as "the seat is gone" (forget
it, show the join cover). `game.js` stops reconnecting on any of the three.

### The agent socket

Server to agent:

| type | |
|---|---|
| `grant` | `{item, level}` put this item in your game at this level |
| `revoke` | `{item}` take it out |
| `notify` | `{text}` a line for this player's screen: the HUD strip, and the page's toast and log |
| `reject` | `{reason}` a refused hello (then the close), or a refused pickup: `unknown item KEY`, `invalid level for Name` (the socket stays) |

On a good hello the server first sends one `grant` or `revoke` for every
catalog item, in catalog order (29 today; use `len(shared.items.ITEMS)`):
`grant` at the player's level for what they hold, `revoke` for everything else,
so a stale save loses what it should not have. Then the room gets a state.

Agent to server:

| type | |
|---|---|
| `pickup` | `{item, level}` a find in this player's own game. `level` is the level read from the byte, not a delta. |
| `resync` | `{}` send every item again (after a save load, or when the game link came back) |
| `status` | `{emu: bool}` whether a game is linked. Sent right after hello and on every attach and loss. |
| `applied` | `{item, action: "grant"\|"revoke", ok, error?}` the result of a write, read back and checked. `error` is at most 200 characters. A failure goes to the room's activity log once per player and item (a line starting with the warning sign); an `ok` for that item clears it. |
| `bye` | `{}` closing on purpose |

### One agent per player

`hub.agents[code][player_id]` holds one socket. A newer agent hello replaces
the older registration; the older socket is not closed and gets no more
commands, though a `pickup` it sends still counts. Its `status` is ignored,
and its close changes nothing. Only the registered socket closing records the
player as unlinked (`agent` and `emu` false) and starts their offline clock.
The page keeps out of this where it can. It opens its agent socket only after
it finds a game, only with *Link a game on this browser* on, and only in one
tab per room per browser: `HLGame.link` (`web/js/game.js`) runs the whole
linked lifetime (bridge, agent socket, HUD) inside the Web Lock
`hyrulelink.link.<CODE>` (`navigator.locks`). Another tab of the same room
waits in the lock's queue with no bridge and no socket, and links when the
holder lets go (link switched off, Leave, tab closed). *Link this tab instead*
requests the lock with `steal`; the robbed tab sends `bye`, closes its socket
and the bridge, and queues again. It never steals back. Without
`navigator.locks` (plain http off localhost, an old browser) every tab links,
as before. Other browsers, devices and the desktop app are not covered: there
the newest hello wins.

### The ui socket

Server to page:

| type | |
|---|---|
| `state` | the room document, on every change. The first one after hello also has `items: [{key, name, image}]`, the catalog in order. |
| `event` | `{text, ts}` a line for the activity log |
| `reject` | `{reason}` a refused claim, in words (`Sword is on cooldown (3s).`), `host only`, `room closed`, or a hello reject |

Page to server:

| type | who | |
|---|---|---|
| `claim` | players | `{item}`. A watcher gets `reject`. |
| `admin_set_name` | host | `{name}` up to 60 characters |
| `admin_set_mode` | host | `{mode: "normal"\|"hot_potato"\|"chaos", seconds?}` the preset, and its interval (5-86400 s) for the two timed ones. Anything else is `normal`. Resets the rules to the preset. |
| `admin_set_cooldown` | host | `{seconds}` 0-3600, the steal cooldown |
| `admin_set_rules` | host | `{rules: {...}}` any of the rules below, clamped; unknown keys dropped. The mode becomes `custom`. |
| `admin_set_discovered` | host | `{player_id, item, found, level?}` mark found or not found for a player, no grant. Not found also takes it from them if they hold it. |
| `admin_set_owner` | host | `{player_id \| null, item, level?}` make a player the holder (at their tier, or `level`), or clear the holder |
| `admin_remove_player` | host | `{player_id}` never the host. Their items go back to the pool, their finds are forgotten, their sockets close. |
| `admin_reset_room` | host | `{}` wipe every holder, find, cooldown and borrow; players, mode and rules stay. Every agent gets 29 revokes. |

"host" means the room's host or a Discord admin; anyone else gets
`reject "host only"`.

### The room document

```json
{ "type": "state", "room": "<pub_id>", "name": "Friday co-op",
  "host": 1, "mode": "normal | hot_potato | chaos | custom",
  "cooldown_s": 5, "shuffle_s": 120, "shuffle_remaining": 0,
  "claiming": true, "rules": {...}, "rules_summary": "claim found items · 5s item cooldown",
  "rule_defaults": {...}, "rule_presets": {"normal": {}, "hot_potato": {...}, "chaos": {...},
                   "cutthroat": {...}, "lease": {...}, "raid": {...}, "siege": {...}},
  "players": [{"id": 1, "name": "Ana", "avatar": null, "agent": true, "emu": true}],
  "ledger": {"sword": {"name": "Sword", "owner": 1, "owner_name": "Ana", "level": 2, "tier": "Master",
                       "image": "sword-2.png", "discovered": [1, 2], "discovered_levels": {"1": 2, "2": 1},
                       "cooldown_remaining": 3.2,
                       "hold_remaining": 0, "locked": false, "tenure_remaining": 0, "borrow_remaining": 0}},
  "you": 1, "admin": false, "spectator": false }
```

- `room` is the pub_id. The join code is never in the document.
- `ledger` has only items somebody has found or holds; an item missing from
  it has not been found. An unheld item has `owner: null`, `level: 0` and
  `tier: "—"`: each finder has their own tier.
- `cooldown_remaining` is non-zero only when the cooldown applies to the item.
  `hold_remaining` is there when a hold limit is on and the item is held;
  `locked` and `tenure_remaining` when a tenure lock is on; `borrow_remaining`
  when it is a borrow.
- `agent` = an agent socket is registered for the player, `emu` = it says a
  game is linked.
- `you` is the player id, `null` for a watcher. `admin` = Discord admin.
  `spectator` = no seat and not an admin.
- Times are seconds from now. The page counts them down itself; a shuffle or
  hold that restarts without moving anything sends no new state.

The rules (`shared/rules.py` has the defaults, `server/ledger.py` the clamps):

| Rule | Default | |
|---|---|---|
| `claiming` | true | the Claim button exists |
| `require_found_to_claim` | true | only what you found yourself |
| `open_season_scope` | `owned` | without the rule above: `owned` = only what somebody holds, `any` = anything |
| `steal_cooldown_s` | 5 | 0-3600 |
| `cooldown_scope` | `item` | `item`, `thief`, `victim` (a shield for whoever lost it), `none` |
| `steal_back_lock_s` | 0 | 0-3600; how long before you can take back what was taken from you |
| `steal_budget_per_min` | 0 | 0-120 steals a minute per player, 0 = no limit |
| `hold_limit_s` | 0 | 0-86400; then `hold_expiry` |
| `hold_expiry` | `next_finder` | `next_finder`, `release` (to the pool), `return_finder` (the first finder) |
| `tenure_lock_s` | 0 | 0-86400; held this long, it cannot be claimed away and a hold limit no longer moves it |
| `idle_release_s` | 0 | 0-86400; an owner whose agent is gone this long drops everything |
| `borrow_s` | 0 | 0-86400; with finding not required, a claim of an unfound item is a borrow for this long |
| `borrow_revert` | `prev_owner` | `prev_owner` or `pool` |
| `auto_shuffle_s` | 0 | 0-86400; reshuffle every so often |
| `shuffle_scope` | `all` | `all`, `unowned`, `idle` (held a full round) |
| `shared_discovery` | false | one find lets everyone claim |

Hot Potato and Chaos move items only among players whose agent is
registered. A physical find always takes the item, whatever the rules.

## QuakeCast cams

The host can open a QuakeCast room for two to four players: each gets a
private seat link that shares their cam, game window and tracker and gives
them the others' OBS sources. `server/connect.py` talks to QuakeCast's own
HTTP API (RaceConnect `docs/CONTRACT.md`). There is no clock. The feature is
off unless `HYRULELINK_CONNECT_URL` is set: then both routes answer 404
`not found`, the state document has no `cams` key, and no `cams` message is
sent.

| | | |
|---|---|---|
| `POST api/rooms/{code}/cams` | `{player_id, player_token, seats: [player ids]}` | `{ok, cams: {seats, error}}`. Two to four different players of the room, in seat order (`a`, `b`, `c`, `d`). Opening again replaces the cams and ends the old QuakeCast room. |
| `POST api/rooms/{code}/cams/close` | `{player_id, player_token}` | `{ok}`, or `{ok, warning}` when QuakeCast could not be told: the cams are closed here either way. |

Both are the host's, by their own seat, or a Discord admin's (the session
cookie or `X-HL-Session`): 403 `host only` or `bad room/player token`
otherwise, 404 `no such room`. A bad pick is 422 `Pick two to four different
players in the room.` QuakeCast's no is 502 with its reason in words:
`QuakeCast is not answering.`, `QuakeCast is full right now. ...`,
`QuakeCast says slow down. ...`, `QuakeCast answered 500.`. Ten calls a
minute per address.

The seat links are credentials. They are kept in server memory only, never
written to the database or a log, never in the state document or any
reply, and they reach nobody but their own player:

| ui socket, server to page | |
|---|---|
| `cams` | `{url}` this player's own seat link, or `null`. Sent after the first `state` of every player's hello, and to each seated player (and anyone who just lost a seat) when cams open, close or end. Never to a watcher. |

The state document gains `cams: null | {seats: [player ids], error}`: who has
a seat, never a link. `error` is `""`, or `"ended"` once QuakeCast says the
room is gone (it ends a room two hours after the last player leaves their
seat, or twelve after it opened). The server asks at most once a minute per
room, when a player's page says hello.

To QuakeCast, from the server: `POST api/rooms {name: <room name>, seats: n,
a, b, c?, d?: <player names>}`, with the caller's address as
`X-Forwarded-For` only when `HYRULELINK_CONNECT_URL` is loopback (QuakeCast
budgets rooms per address); `POST api/seat/<seat a's token> {close: true,
force: true}` to close (seat `a` made the room, so only it may); `GET
api/seat/<seat a's token>` to see whether it still exists. Deleting a
HyruleLink room (operator, Discord admin, the 14-day prune) closes its
QuakeCast room too. A server restart forgets the cams; the QuakeCast room then
ends by itself.

## The game memory contract

The page reaches the game through SNI or QUsb2Snes (usb2snes) at
`ws://localhost:23074`, then `ws://localhost:8080`. The client name is
`HyruleLink`. Opcodes used: `AppVersion`, `Name`, `DeviceList`, `Attach`,
`Info`, `GetAddress`, `PutAddress`; the first device listed is used. An
address is `0xF50000` + the WRAM offset (`$7E0000` = `0xF50000`). One request
at a time; 2.5 s to connect, 3 s per answer, and a timeout closes the socket.
Four failed module reads in a row close it too.

Every 500 ms, from a Worker: read `$7E0010` (the module); if it is playable,
the agent applies any grants and revokes that were waiting, reads `$7EF342`
for 77 bytes, checks the boots run flag, and then the HUD strip advances.

| Modules (hex) | |
|---|---|
| `06 07 08 09 0A 0B 0E` | playable: a save is loaded. Polled, written, lines drawn. |
| `00 01 02 03 04 05 14 17 1B` | out of game: title, file select, copy and erase, name entry, loading, attract, save and quit, the where-to-start prompt. The next playable poll re-seeds detection, sends `resync` and resets the HUD strip. |
| anything else | a transition (death `12`, door spotlights, the mirror, a boss victory): not polled, commands wait, no resync |

### The items

Addresses are `$7E` + the offset. What counts as found is the level read
from the byte; a `pickup` is sent when it goes up.

| Items | Address | Found when | Grant writes | Revoke writes |
|---|---|---|---|---|
| sword, shield, armor (Mail), gloves, magic | `$7EF359`, `$7EF35A`, `$7EF35B`, `$7EF354`, `$7EF37B` | byte 1 or more, level = byte up to the cap (4, 3, 2, 2, 2) | the byte = the level | 0 |
| hookshot `F342`, firerod `F345`, icerod `F346`, bombos `F347`, ether `F348`, quake `F349`, lamp `F34A`, hammer `F34B`, bug_net `F34D`, book `F34E`, somaria `F350`, byrna `F351`, cape `F352`, mirror `F353`, flippers `F356`, moon_pearl `F357` | `$7E` + that | byte not 0 | 1 (the mirror 2) | 0 |
| boots | `$7EF355` | byte not 0 | 1, and bit `0x04` of `$7EF379` | 0, and clears that bit |
| bow | `$7EF38E` | bit `0x80`; level 2 with `0x40` | sets `0x80` (and `0x40` for silver, cleared for wood); `$7EF340` = 1 wood or 4 silver; silver puts 30 in `$7EF377` when it reads 0 | clears both bits; `$7EF340` = 0 |
| blue_boomerang `0x80`, red_boomerang `0x40` | `$7EF38C` | the bit | sets the bit; `$7EF341` = 1 blue, 2 red | clears it; `$7EF341` falls back to the other if owned, else 0 |
| mushroom `0x20`, powder `0x10` | `$7EF38C` | the bit | sets `0x28` or `0x10`; `$7EF344` = 1 mushroom, 2 powder | clears them; `$7EF344` falls back |
| shovel `0x04`, flute `0x01` or `0x02` | `$7EF38C` | the bit (either flute bit) | sets `0x04`, or `0x01` (the active flute); `$7EF34C` = 1 shovel, 3 active flute | clears `0x04` or `0x03`; `$7EF34C` falls back (2 = inactive flute) |

`$7EF379`, `$7EF38C` and `$7EF38E` are read-modify-write: each read retries 6
times and never falls back to 0. Every grant and revoke reads the item's byte
back and fails when it is wrong; the agent tries 3 times, then sends
`applied` with `ok: false`. Our own write is remembered per address and not
reported when the next poll sees it.

The run flag is enforced every poll: set while the boots are this player's,
cleared while they are not, written only when wrong.

### The HUD strip

WRAM `$7EC700` is the HUD tilemap, 32 words a row. The strip is row 4,
columns 5-24: `$7EC80A`, 20 little-endian words. `A`-`Z` = `0x255D`+n,
`0`-`9` = `0x2490`+n, blank = `0x247F`, `:` = `0x2806`. After each write,
`$7E0016` = 1 makes the game copy the buffer to VRAM on its next NMI. The
cells under the line are saved first and put back when it ends, with any
cell the game changed meanwhile.

A line is cleaned to A-Z, 0-9, spaces and `:`. It is centred when it fits
and stays 4 s; a longer one shows its head for 1.5 s, then moves one cell
every 0.75 s, then stays 4 s. Six lines queue; a line equal to the last one
queued is dropped. Lines are drawn only in playable modules, and the strip is
reset (nothing written) on the out-of-game to playable edge and on every new
link.

## The fake game

`tools/fake_snes.py [--port N] [--host H]`: usb2snes on `ws://localhost:N`
(default 23074), control HTTP on N+1. Run from the repo root; it exits 1 when
the port is taken.

usb2snes: `DeviceList` answers `["emunwa://fake-alttp:48879"]`, `AppVersion`
`["SNI-fake"]`, `Info` `["1.0","fake","alttpr.sfc"]`; `Name` and `Attach` get
no answer; `GetAddress` answers one binary frame; `PutAddress` takes the
binary frames that follow. 128 KB of WRAM at `0xF50000`, zeroed, the HUD
buffer blank (`0x247F`), module `0x01`. A write that covers `$7E0016` is set
back to 0 at once, the way the NMI takes it.

| Control | |
|---|---|
| `GET /` | `{module, strip, items:{key: level}, ability, arrows}`: `strip` is the 20 cells as text (`?` for a tile that is not a letter, digit, blank or colon), `ability` is `$7EF379`, `arrows` `$7EF377` |
| `POST /module/{n}` | set the module; decimal, or hex with `0x` |
| `POST /item/{key}/{level}` | poke the item the way the game does on a find; level 0 takes it away. `?inactive=1` for the flute before the bird is freed. 404 unknown item, 400 bad level. Answers the state. |
| `GET /mem/{addr}?n=1` | `{bytes:[...]}`, up to 4096 |
| `POST /mem/{addr}/{value}` | one byte, hex |
| `POST /reset` | zeroed memory, file select |

`{addr}` is hex and takes `F379`, `0xF379`, `$7EF379`, `7EF379` or
`F5F379`. A find poke: progressive, the byte = level; simple, the byte =
`give`; boots, `$7EF355` = 1 and `$7EF379 |= 0x04`; bow, `$7EF38E |= 0x80`
(with `0x40` at level 2, cleared at 1) and `$7EF340` = 1 or 4, arrows left
alone; shared slots, their bits ORed into `$7EF38C` (mushroom `0x28`) and the
slot enum set to that item.
