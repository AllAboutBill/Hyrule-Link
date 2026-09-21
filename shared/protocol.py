"""
protocol.py — WebSocket message contract between the BombosSwap server, the
player agents, and the browser UIs. Plain JSON dicts with a `type` field.

Transport model: agents and UIs both *dial out* to the server (works behind
NAT / for remote play). The server is the single source of truth for ownership.
One socket path, /ws; the first message is `hello`. An agent is either the
desktop app's Python agent or the browser page's (web/js/agent.js); the server
cannot tell them apart. One agent per player: a newer hello replaces the older.

────────────────────────────────────────────────────────────────────────────
agent  -> server
  hello     {type, role:"agent", room, player_id, token, platform?}
            # server answers with one grant/revoke for EVERY catalog item
  pickup    {type, item, level}      # player found `item` (raw level) in-world
  resync    {type}                   # re-push the full grant/revoke set
  status    {type, emu:bool}         # game link up/down (on open + every change)
  applied   {type, item, action, ok, error?}  # verified grant/revoke result
  bye       {type}

ui     -> server
  hello     {type, role:"ui", room, player_id, token, session?}
            # session: the desktop app's Discord token (browser: hl_session cookie)
  hello     {type, role:"spectator", watch:<pub_id>}   # no player, no claims
  claim     {type, item}             # request ownership of a discovered item
  admin_*   see the ADMIN_* constants below (room host or a Discord admin)

server -> agent
  grant     {type, item, level}      # enable item at this level in your game
  revoke    {type, item}             # disable item in your game
  notify    {type, text}             # on-screen line (HUD strip / emulator OSD)
  reject    {type, reason}

server -> ui  (and broadcast on any change)
  state     {type, room:<pub_id>, name, cooldown_s, host, mode, rules,
             rule_defaults, rule_presets, claiming, rules_summary, shuffle_s,
             shuffle_remaining, players:[{id, name, avatar, agent, emu}],
             ledger:{key:{...}}, you, admin, spectator}
            # the FIRST state after hello also carries items:[{key, name, image}]
            # `room` is the public handle; the join code is never sent
  event     {type, text, ts}
  reject    {type, reason}
────────────────────────────────────────────────────────────────────────────
"""

# agent -> server
HELLO = "hello"
PICKUP = "pickup"
APPLIED = "applied"
RESYNC = "resync"   # agent asks server to re-push ownership (e.g. emulator came back)
STATUS = "status"   # agent reports emulator connectivity {emu: bool}
BYE = "bye"

# ui -> server
CLAIM = "claim"

# host-only admin actions (ui -> server); server validates user == room host
ADMIN_SET_COOLDOWN = "admin_set_cooldown"     # {seconds}
ADMIN_REMOVE_PLAYER = "admin_remove_player"   # {player_id}
ADMIN_SET_DISCOVERED = "admin_set_discovered" # {player_id, item, found}
ADMIN_SET_OWNER = "admin_set_owner"           # {player_id|null, item}
ADMIN_SET_MODE = "admin_set_mode"             # {mode: normal|hot_potato|chaos, seconds}
ADMIN_SET_RULES = "admin_set_rules"           # {rules:{...}}  custom ruleset (mode=custom)
ADMIN_SET_NAME = "admin_set_name"             # {name}  rename the room
ADMIN_RESET_ROOM = "admin_reset_room"         # {} wipe all progression, keep players/settings

# server -> agent
GRANT = "grant"
REVOKE = "revoke"
NOTIFY = "notify"

# server -> ui
STATE = "state"
EVENT = "event"
REJECT = "reject"

ROLE_AGENT = "agent"
ROLE_UI = "ui"
ROLE_SPECTATOR = "spectator"   # read-only watcher: no player, no token, no claims
