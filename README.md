# HyruleLink

**Archipelago-style _shared-inventory_ co-op for A Link to the Past Randomizer.**

Every player generates and plays their **own** seed. But the *progression
inventory is a shared pool*: each item type (sword, bow, hookshot, …) can be
held by **only one player at a time**.

- Player A finds a sword → A has it; nobody else can.
- B and C keep playing. When **B finds a sword** in their own world, ownership
  moves to B and **A's sword is disabled live**.
- A wants it back → clicks **Claim** in the web app; A's sword re-enables and
  B's disables.
- You can only **Claim** an item you have **personally found** at least once.
- Progressive tiers are **per player**: you get back the best version **you**
  have personally found. If A found a Master Sword and B found a Gold Sword,
  claiming "the sword" gives **A a Master** and **B a Gold** — A only reaches
  Gold once A finds Tempered/Gold themselves.

It works by reading/writing SNES WRAM live (the SRAM mirror at `$7EF000`),
exactly like the reference tools it was built from (TwitchBot SNI, AlttprHelper,
ALTTPRFollowerInjector).

## Pieces

```
shared/   item catalog (progression pool) + WebSocket protocol
server/   FastAPI hub: accounts, rooms, authoritative ledger, web UI  (port 5019)
agent/    local app next to each emulator: polls pickups, applies grant/revoke
web/      browser UI: room grid + Claim buttons + activity log
```

The **server** is the single source of truth. **Agents and browsers dial OUT**
to it over one WebSocket, so it can live on a public host (your droplet) and
every remote player just needs the URL — no port-forwarding.

## Web UI style

The `web/` view shares **billogna.lol's "aurora" design language** (2026-06-28
restyle) so the spectator page and the main site feel like one product.

- **Palette:** mint `#b3ffc8`, violet `#8a6bff`, blue `#5eadff` over near-black
  `#070709`. No pink — accents are mint/violet/blue only.
- **Type:** [Unbounded](https://fonts.google.com/specimen/Unbounded) for
  headings, [DM Mono](https://fonts.google.com/specimen/DM+Mono) for body.
- **Background layers** (all `aria-hidden`, behind the content): three drifting
  gradient blobs → a faint **pixel field** canvas (`nexus-bg.js`) → an SVG
  **noise** grain overlay.
- **Pixel-hover gimmick:** interactive elements fill with a subtle grayscale /
  steel-blue **pixel shimmer** on hover/focus, ported from billogna.lol.
  Borderless text (logo, header links) gets a soft radial edge-fade instead of a
  hard rectangle.
- **Cards & buttons** are translucent "glass" (`backdrop-filter: blur`) so the
  pixel field shows through; the item `#grid` buttons are excluded from the
  hover effect (too many, recreated often).

Style-only files in `web/` (no game logic):

```
style.css        aurora palette, blobs, glass cards, .hl-pixel-* helpers
nexus-bg.js      animated pixel-field background canvas (#nexus-pixels)
pixel-canvas.js  Ryan Mulligan's <pixel-canvas> web component
pixel-hover.js   injects <pixel-canvas> behind buttons / links / logo on hover
items/           item sprites (ALTTPR tracker art) shown in the board
```

Mirror billogna.lol if you re-theme: keep these in sync with the copies under
that site's `shared/` so both stay on the same palette and component versions.

**Item sprites** (`web/items/<key>.png`, plus `<key>-1..N.png` for progressive
tiers) come from the ALTTPR community tracker and are shared by both UIs: the web
grid loads them from `/static/items/`, and the desktop app scales/dims them with
Pillow. `shared.items.item_image(key, level)` is the single source of truth for
which sprite a given item/tier uses, so the two views never drift.

## Shared pool

Progression items only: sword, shield, mail, gloves, bow, boots, hookshot, fire/
ice rod, bombos/ether/quake, lamp, hammer, bug net, book, somaria, byrna, cape,
mirror, flippers, moon pearl, blue/red boomerang, mushroom, powder, shovel,
flute, magic upgrade. **Excluded:** ammo (rupees/bombs/arrows/hearts), bottles
(they're consumable-like, not progression), and per-dungeon items
(keys/maps/compasses).

> ⚠ Disabling movement items (Moon Pearl, Flippers, Gloves) can briefly strand a
> robbed player until they Claim something back. That's the intended tension —
> and why the Claim button exists (the in-world chest is one-time).

## Quick start — no terminal, no login (Windows)

**First time, once:** double-click **`Install.cmd`** (sets up Python + deps).

**The app already points at the public server** (`https://hyrulelink.billogna.lol`)
— most groups need nothing else. Don't want to use the website? In the app, use
**Host a server on THIS PC**:
- It launches a local server and shows the `http://<your-ip>:port` URL for
  **same-network** players.
- Tick **"make it reachable over the internet (free tunnel)"** and it also spins
  up a **Cloudflare quick tunnel** — you get a public `https://…trycloudflare.com`
  link to share with players on **any** network, **no port forwarding, no
  account** (first use downloads `cloudflared` once).

Or run **`Start Server.cmd`** for a standalone local server.

**Everyone plays from one app — double-click `Play.cmd`:**
1. Type your **name**.
2. **Join** with a code, **Host a new room** (on the server shown), or
   **Host a server on THIS PC** — you get a room code to share.
3. The **game board appears right in the app**. Need a seed? Click **Generate
   seed** (makes an Open seed from your own JP 1.0 base ROM — no AlttprHelper).
   Then **Launch emulator** (auto-configured network-ready) or start your own,
   and **Connect & Play**.

That's the whole flow — **no accounts, no passwords, no config files, no commands**.
The board, Claim buttons, player list, connection health, and host controls are
all inside the app window. The room code is the only thing you share. **Re-joining
the same room** reconnects you as the *same* player (your items are kept), not a
duplicate.

> The web page (`http://server:5019/`) is the **spectator / second-screen** view.
> Its home page lists **live rooms by name** — click **Watch** to spectate any
> room read-only (no account, no player created). Rooms are **public to watch but
> private to play**: the join **code is never shown in the list**, so only people
> you give the code to can join as a player (paste it in *Join with a code*).
> `…/?watch=<public-id>` deep-links straight into watching (the app's *Spectator
> view* button uses this; players still share the secret code out-of-band).

### Host & admin controls

- **Host** (whoever created the room) gets controls in both the app and the web:
  set the steal **cooldown**, **remove** a player, and **right-click any item**
  (app) / use the **player chips** (web) to fix who has *found* an item or who
  *owns* it — handy after a disconnect.
- **Game modes** (host-set, shown to everyone with a banner; claiming is off):
  - 🔥 **Hot Potato** — each item you're holding auto-passes to the next *online*
    player who's found it after a timer (round-robin). You can't keep anything.
  - 🌀 **Chaos** — every found item is randomly reassigned among its online
    finders on a shared timer.
  - **Normal** — the usual find/claim/steal game.
- **Global admin via Discord.** Click **Login with Discord** on the web page;
  your server's **owner and mod roles** become HyruleLink admins — **delete any
  room** and **manage / kick** in *any* room (not just ones you host). Configure
  it with the `DISCORD_*` + `SESSION_SECRET` env vars (see `.env.example`); you can
  reuse an existing Discord app by adding `…/auth/callback` to its OAuth redirects.
  With it unconfigured, global admin is simply unavailable (hosts still run their
  own rooms).

> **Supported emulators (all auto-detected):**
> - **snes9x-nwa** (EmuNetworkAccess build) — direct, nothing extra.
> - **RetroArch** (any SNES core) — the app enables the network setting for you.
> - **snes9x-rr, BizHawk, real hardware (SD2SNES/FXPak), and more** — via the
>   **SNI bridge, which is bundled**. Click **Start SNI** in the app (or just say
>   Yes when Connect & Play offers it). Real hardware then connects with nothing
>   else to do; for snes9x-rr/BizHawk the app points you at the `Connector.lua`
>   to load in the emulator. If you already run SNI/QUsb2Snes, it uses that one.
> - *Not* supported: plain mainline snes9x with no network build / no Lua — it
>   exposes no memory. Use snes9x-nwa or RetroArch.
>
> In the app, **"Which emulators?"** explains all of this.

Bundled third-party tools (in `tools/`): **SNI** (MIT, `tools/sni/`) and
**cloudflared** (Apache-2.0) — both auto-launched only on demand.

## Advanced / manual (any OS, terminal)

```bash
pip install -r requirements.txt
python run_server.py --port 5019 --open     # server + open browser
python run_agent.py --setup                 # player: login/join, writes config
python run_agent.py                          # player: connect + play
```

Server environment variables (the server auto-loads a git-ignored `.env`; see
`.env.example`):

```
DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET   Discord OAuth app (global admin login)
DISCORD_REDIRECT_URI    e.g. https://hyrulelink.billogna.lol/auth/callback
DISCORD_GUILD_ID        your server; owner + mod roles below get admin
DISCORD_MOD_ROLE_IDS    comma-separated role ids that count as admin
SESSION_SECRET          random string signing the login cookie
HYRULELINK_ROOM_TTL_DAYS  auto-delete rooms idle this long (default 14)
HYRULELINK_DB           sqlite path (default server/hyrulelink.db)
```

Or run a specific config (several agents on one PC):
`python run_agent.py --config agent/config_a.json`.

Load your seed in the emulator and play normally. Pickups are detected and
reported automatically; grants/revokes are written into your game live.

## Config (`agent/config.json`)

```json
{
  "server_http": "http://YOUR_SERVER:5019",
  "server_ws":   "ws://YOUR_SERVER:5019/ws",
  "room": "ABC123",
  "user_id": 1,
  "player_token": "…",
  "transport": "emu",
  "poll_interval": 1.0
}
```

## Notes & limits (v1)

- Only one client may attach to a QUsb2Snes device at a time — close
  EmoTracker/LiveSplit if the agent can't attach.
- Detection only fires while in a playable game mode, so file-select/transition
  bytes never produce phantom pickups.
- The agent echo-cancels its own writes, so applying a grant/revoke never
  re-broadcasts as a pickup.
