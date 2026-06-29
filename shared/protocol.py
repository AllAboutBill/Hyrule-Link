"""
protocol.py — WebSocket message contract between the HyruleLink server, the
player agents, and the browser UIs. Plain JSON dicts with a `type` field.

Transport model: agents and UIs both *dial out* to the server (works behind
NAT / for remote play). The server is the single source of truth for ownership.

────────────────────────────────────────────────────────────────────────────
agent  -> server
  hello     {type, role:"agent", room, player_id, token, platform}
  pickup    {type, item, level}      # player found `item` (raw level) in-world
  applied   {type, item, action, ok, error?}  # verified grant/revoke result
  bye       {type}

ui     -> server
  hello     {type, role:"ui", room, player_id, token}
  claim     {type, item}             # request ownership of a discovered item

server -> agent
  grant     {type, item, level}      # enable item at this level in your game
  revoke    {type, item}             # disable item in your game
  reject    {type, reason}

server -> ui  (and broadcast on any change)
  state     {type, room, you, players:[...], ledger:{key:{...}}, cooldown_s}
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

# server -> agent
GRANT = "grant"
REVOKE = "revoke"

# server -> ui
STATE = "state"
EVENT = "event"
REJECT = "reject"

ROLE_AGENT = "agent"
ROLE_UI = "ui"
ROLE_SPECTATOR = "spectator"   # read-only watcher: no player, no token, no claims
