# BombosSwap - notes for Claude

Read `README.md` first, then `docs/DEVELOPING.md` ("Hard constraints" and
"Traps") before changing anything. Interfaces are in `docs/PROTOCOL.md`.
`docs/BROWSER_PLAN.md` is the record of how the browser client was built;
leave it as it is.

BombosSwap was HyruleLink until 2026-09-21. The product name changed: every
string a person reads, the docs and the error messages. Code names did not:
the repo folder, packages, modules, classes, loggers, `hyrulelink.service`,
`/opt/hyrulelink`, `HYRULELINK_*`, `server/hyrulelink.db`, the `hyrulelink.*`
localStorage keys and the `hl_*` import, the Web Lock `hyrulelink.link.<CODE>`,
`X-HL-Session` and `hl_session`, `web/css/hyrulelink.css`, the GitHub repo and
the subdomain `hyrulelink.billogna.lol` (every installed desktop app points
at it). Do not rename those; it breaks installs, seats or the server.

Two clients, one server. The browser page (`web/`) and the desktop app
(`agent_gui.py` + `agent/`) both link a game as `role: "agent"` on `/ws`, and
the server cannot tell them apart. The desktop app keeps working: its routes
are frozen.

Standalone on purpose. It copied code from EtherNet (`snes.js`, `hud.js`,
`fake_snes.py`, the look, the operator page), which carries pieces of the
Twitch bot (the HUD writer and text cleaner) and RaceConnect (the usb2snes
client). It imports nothing from any of them. Never edit `F:\EtherNet` or
`F:\RaceConnect` from here.

## Easy to get wrong

- **Every player plays their own seed.** Only the inventory is shared. Never
  build same-seed sync, a host-set seed or a race clock.
- **BombosSwap reads inventory bytes and writes items.** That is why it is not
  an EtherNet mode. Item writes are never optional while a game is linked;
  only the HUD lines can be switched off.
- **The server never writes to a game.** It sends `grant`, `revoke` and
  `notify` to the player's own agent, which writes.
- **The desktop app's routes are frozen** (the list is in DEVELOPING). New
  fields and new routes only.
- **FastAPI stays, and Python 3.10** syntax in `server/` and `shared/` (the
  droplet is 22.04). No new server dependency. A test enforces the syntax.
- **No leading-slash URLs** in `web/`: it runs under `/bombosswap/` too. A test
  enforces it.
- **`shared/items.py` is the catalog; `web/js/items.js` is generated** by
  `tools/gen_items_js.py`. 29 items; never hardcode the count.
- **Shared bytes are read-modify-write** (`$7EF379`, `$7EF38C`, `$7EF38E`):
  reads retry and never fall back to 0.
- **`web/` is mounted at `/`, last**, in `server/app.py`. A route added after
  it is never reached.
- **Never write to a real game from a dev session.** Use `tools/fake_snes.py`,
  and never port 23074 in tests: the user's tracker and any open page find it.
- On-page wording is plain and short. No tutorial voice, no exclamation marks,
  no emoji. No page shows the old name; a test checks.

## Working here

```bash
.venv/Scripts/python.exe -m unittest discover -s tests   # everything, node units and the two-player round included
node tests/web/units.js                                  # just the JS units
```

Port 5019. Remote `origin` on GitHub; CI runs the suite on Windows. To go
live at https://www.billogna.lol/bombosswap/ and https://hyrulelink.billogna.lol:
`bash deploy/deploy.sh`, `--check`, `--rollback`; read `deploy/README.md`
first. The droplet is shared with every other site: never touch nginx beyond
BombosSwap's own blocks (`/bombosswap/` and the `/hyrulelink/` redirect), and
never another service. Line endings are LF in git; `core.autocrlf` checks
worktrees out CRLF, and `deploy/deploy.sh` must stay LF on disk. UTF-8
without a BOM.
