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
- The shared token carries the **highest tier found** — claim "the sword" and
  you get whatever the best version anyone has discovered is (e.g. Gold).

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

## Shared pool

Progression items only: sword, shield, mail, gloves, bow, boots, hookshot, fire/
ice rod, bombos/ether/quake, lamp, hammer, bug net, book, somaria, byrna, cape,
mirror, flippers, moon pearl, blue/red boomerang, mushroom, powder, shovel,
flute, magic upgrade, bottle. **Excluded:** ammo (rupees/bombs/arrows/hearts)
and per-dungeon items (keys/maps/compasses).

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

> The web page (`http://server:5019/`) still works as an optional **spectator /
> second-screen** view — join it with the same name + room code.

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
- Bottles are a single shared token (one empty bottle) in v1.
- Detection only fires while in a playable game mode, so file-select/transition
  bytes never produce phantom pickups.
- The agent echo-cancels its own writes, so applying a grant/revoke never
  re-broadcasts as a pickup.
