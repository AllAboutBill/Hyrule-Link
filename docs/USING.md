# Using HyruleLink

## A co-op, start to finish

1. **Each player gets a seed.** Every player plays their own seed: roll one
   on [alttpr.com](https://alttpr.com/) (or with the desktop app's *Generate
   seed*) and load it in your emulator. The seeds do not have to match, and
   nobody sets one for the room. Only the inventory is shared.
2. **Open a room.** On the front page: your name, a room name if you want
   one, *Open a room*. You are the host.
3. **Send the link.** *Copy invite* in the top bar. Anyone with the link can
   *Join as a player* or *Just watch*. The 10-character code works too, in
   *Got a code?* on the front page.
4. **Link your game.** Run SNI (below). The Game row under the seed line finds
   your game by itself and says so.
5. **Play.** Load your save. What you find goes to the room by itself, items
   come and go in your game as they change hands, and lines about it appear
   on your HUD.
6. **Claim it back.** When somebody takes an item you found, *Claim* on its
   card brings it back.

Reopening the link in the same browser puts you back in as the same player,
holding what you held. "Your rooms on this browser" on the front page lists
the rooms this browser has a seat in.

## How the pool works

- **29 progression items** are shared: sword, shield, mail, gloves, bow,
  magic upgrade, boots, hookshot, fire and ice rod, the three medallions,
  lamp, hammer, bug net, book, both canes, cape, mirror, flippers, Moon Pearl,
  both boomerangs, mushroom, powder, shovel and flute. Not shared: ammo
  (rupees, bombs, arrows, hearts), bottles, and dungeon items (keys, maps,
  compasses).
- **One holder per item.** Finding one in your own world always makes it
  yours: it goes into your game and out of the previous holder's.
- **Claim** only what you have found yourself at least once. The card says
  *Find one to claim* until you have.
- **Tiers are per player.** You get back the best tier *you* have found. If
  Ana found the Master Sword and Bo the Gold Sword, a claim gives Ana a Master
  and Bo a Gold.
- **Cooldown.** After an item moves, nobody can claim it for the room's steal
  cooldown (5 s unless the host changes it); the card shows *Cooldown 3s*. A
  find is never held up by it.
- Losing Moon Pearl, Flippers or Gloves can strand you until you claim
  something back. That is the game.

The cards: gold outline, yours; solid, somebody else holds it; dashed, found
but nobody holds it; faded, nobody has found one yet.

## Linking your game

You need **SNI** or **QUsb2Snes** running on your PC. If you use an
auto-tracker you already have one. The page finds the game by itself; there
is nothing to type in.

**SNI is the one to use.** Writes through SNI into a real snes9x-nwa are
proven (EtherNet's HUD lines, through the same `snes.js` this page uses).
Writes through QUsb2Snes, and to an FXPak Pro, are not proven yet.

Run one of them, not both. They use the same port, so the second one to start
says the port is already in use. A tracker or bot that connects to the
emulator by itself is no problem: snes9x-nwa serves several at once.

| You play on | What to do |
|---|---|
| snes9x-nwa, bsnes-plus (NWA) | Nothing. SNI and QUsb2Snes find NWA emulators by themselves. |
| RetroArch | Settings, Network, Network Commands: on. Port 55355. |
| BizHawk, snes9x-rr | Lua console, load `Connector.lua` from SNI's `lua` folder. |
| FXPak Pro / SD2SNES | USB cable in, console on. |

No bridge? The Game row shows **Get SNI**: SNI's own GitHub releases page.
Unzip it anywhere, run `sni.exe`, leave it in the tray. A web page cannot open
an emulator's network port by itself, NWA included, so SNI is the one small
program in between.

If the browser asks whether the site may reach devices or apps on your local
network, allow it. Chrome, Edge and Firefox work. Safari does not let a web
page reach a program on the same computer.

| The Game row says | |
|---|---|
| *Emulator (NWA) through SNI* (or RetroArch, FXPak Pro, Emulator (Lua)) | Linked. *Save loaded. Items and messages go straight into your game*, or *Items are applied once a save file is loaded*. |
| *... is running, but it has no game yet.* | Load your ROM, or switch on the console. It links by itself. |
| *... is running, but it is not answering.* | Quit it from its tray icon and start it again. Do not start a second copy. |
| *No SNI or QUsb2Snes found on this PC.* | Start one, or *Get SNI*. |
| *Looking for SNI or QUsb2Snes on this PC.* | It is still looking. |
| *Game linking is off on this browser.* | Settings, *Link a game on this browser* is off. |

Not linked? You still see the board and can claim, but your finds do not
reach the room and nothing reaches your game. Once it links, the room sends
everything you hold.

### One game link per player

The page registers as your game link the first time it finds a game. One tab
per room links on each browser. A second tab of the same room says *Linked in
another tab of this browser* and takes over by itself when the first one
closes or stops linking. *Link this tab instead* moves the link to it now, and
the other tab waits. (This needs an https address or localhost; on a plain
http address every tab links.)

A second device or browser, or the desktop app's *Connect & Play*, for the
same player takes over from the page: the newest one wins, and the older one
stops getting items. Open the room on a second device only with *Settings,
Link a game on this browser* switched off there.

### What the page reads and writes

It reads the game module (`$7E0010`) every half second, and while a save is
loaded the inventory block `$7EF342`-`$7EF38E` in one read. It writes the
item bytes in that block (grants and revokes from the room), the run flag for
the boots (`$7EF379`), the bow's equip byte (`$7EF340`) and arrows
(`$7EF377`, 30 when a silver bow arrives and it reads 0), the 20 HUD cells of
the in-game line and the HUD update flag (`$7E0016`). It reads those 20 cells
before drawing over them, to put them back after. Nothing else is read or
written. What leaves your PC: each item it finds and at what level, whether
a game is linked, and whether each write took.

Items are only written while a save is loaded. At the title screen or file
select they wait and go in when you load. Loading a save, or reloading an
older one, makes the room send everything again: your game ends up holding
exactly what the board says you hold. Dying, the mirror and door transitions
do not count as leaving the game.

## In-game messages

Text on the bottom row of the HUD, in the game's own font, with no ROM patch.
It draws A-Z, 0-9, spaces and `:`, 20 characters at a time; longer lines
scroll across. They appear once a save is loaded.

Each move sends a line to the players it touches: `SWORD TAKEN FROM ANA` to
the finder, `SWORD SENT TO BO` to the player who lost it; claims, host moves,
Hot Potato passes and Chaos shuffles the same way. On the browser that is
your game link, the same line shows as a toast and in the activity log, and
the *Last line* row shows it the way your HUD draws it.

*Settings, In-game messages* off: no lines on your HUD. Items are still
applied.

## Modes and host controls

The host is whoever opened the room. The Host panel under the items is theirs
alone (*Settings, Host controls, Show* jumps to it).

| Mode | |
|---|---|
| **Normal** | Find, claim, steal back, with the steal cooldown. |
| **Hot Potato** | Each held item passes to the next player who has found it and has a game link up, every *Pass every* seconds (120 by default). Claiming is off. |
| **Chaos** | Every found item is dealt again at random among the players who found it and have a game link up, every *Shuffle every* seconds (120 by default). Claiming is off. |
| **Custom** | *Customize ruleset*: every rule by hand, starting from a preset. |

Anything but Normal shows a banner over the items with the rules in one line
and, when there is one, the next shuffle.

The Custom presets, beyond the three above:

| Preset | |
|---|---|
| **Cutthroat** | Steal anything somebody holds, found or not. 20 s cooldown per thief, at most 3 steals a minute, and 30 s before you can take back what was taken from you. |
| **Lease** | An item held for 300 s goes back to the pool, for anyone who found it to claim. |
| **Raid** | Take what somebody holds, found or not. One you have not found is a 120 s borrow; then it goes back to whoever held it. |
| **Siege** | An item held for 240 s cannot be stolen. |

The rest of the Host panel:

- **Room name**, *Rename*.
- **Players**: on every card a chip per player. Click marks it found or not
  found for them; shift-click makes them the holder, or clears it. Useful
  after a crash or a reset.
- **Remove** a player (a second click confirms). Their items go back to the
  pool and their seat stops working. The host cannot be removed.
- **Reset progression**: type RESET. Every item, find and claim is wiped;
  players and settings stay, and every linked game drops its shared items.
  It is for a fresh start on new seeds.

## Watching

*Watch* in "Live rooms" on the front page opens `room.html?watch=...`: the
board, the players and the activity log, with no Claim, no host controls and
no game link. A watch link carries the room's public handle, never its code.

## Your seat

Your seat lives in this browser. Another browser or device joining with the
same link is a new player. *Settings, Leave the room* (a second click
confirms) forgets the seat here; what you held stays in the room. A room
nobody uses for 14 days is removed.

## The desktop app

The Windows app (`Install.cmd` once, then `Play.cmd`) is the other way to be
in a room. Same server (it points at `https://hyrulelink.billogna.lol`), same
rooms, same board; a desktop player and browser players can share a room.
Use it when you want:

- a seed made for you: *Generate seed*, with sprite, palette shuffle and
  MSU-1 options;
- the emulator started for you: *Launch*;
- a link with no SNI: straight to snes9x-nwa or RetroArch (which also shows
  the lines on its on-screen display);
- your own server: *Start local server* for the LAN, and *Create a public
  internet link* for a free Cloudflare tunnel;
- *Connect Discord*: the same seat on any device, and admin in every room for
  the Discord server's mods;
- BillognaBot on the same PC: the app hands its lines to the bot, so the two
  do not fight over the HUD strip.

*Emulator help* in the app lists the emulators it links to. *Open spectator*
opens the room's watch page in the browser.

`run_agent.py` is the same agent without the window: `--setup` asks for the
server, your name and the room and writes `agent/config.json`; `--config`
picks another file, for several games on one PC.

The app is a game link like a browser is. Link each player's game through one
of them, not both.
