"""
db.py — tiny stdlib-sqlite3 persistence for HyruleLink.

No accounts: a "player" is just a display name + a per-room secret token, created
on join. The room CODE is the shared secret that gates entry; the player TOKEN
authenticates that player's agent + UI over the WebSocket. This is plenty for a
private friends co-op and keeps the whole thing password-free.
"""

import os
import sqlite3
import secrets
import threading
import time

ROOM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I
ROOM_CODE_LENGTH = 10                                      # ~50 bits

DB_PATH = os.environ.get(
    "HYRULELINK_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "hyrulelink.db"),
)

_lock = threading.RLock()
_conn: sqlite3.Connection = None


def _connect():
    global _conn
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    code            TEXT PRIMARY KEY,   -- private join secret (never listed publicly)
    name            TEXT NOT NULL,
    pub_id          TEXT,               -- public watch handle (safe to list)
    host_player_id  INTEGER,
    cooldown_s      REAL NOT NULL DEFAULT 5,
    mode            TEXT NOT NULL DEFAULT 'normal',  -- normal | hot_potato | chaos | custom
    shuffle_s       REAL NOT NULL DEFAULT 120,       -- shuffle-mode interval (seconds)
    rules           TEXT,                            -- custom ruleset (JSON); null = derive from mode
    created_at      REAL NOT NULL,
    last_active     REAL
);
-- a player is just a name + token scoped to one room (no account / password).
-- discord_id (optional) links the player to a logged-in Discord user so they can
-- find + rejoin their rooms from any device.
CREATE TABLE IF NOT EXISTS players (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    room_code     TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    player_token  TEXT NOT NULL,
    discord_id    TEXT,
    avatar        TEXT,
    joined_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    room_code        TEXT NOT NULL,
    item_key         TEXT NOT NULL,
    owner_player_id  INTEGER,
    level            INTEGER NOT NULL DEFAULT 0,
    cooldown_until   REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (room_code, item_key)
);
CREATE TABLE IF NOT EXISTS discovered (
    room_code  TEXT NOT NULL,
    item_key   TEXT NOT NULL,
    player_id  INTEGER NOT NULL,
    level      INTEGER NOT NULL DEFAULT 1,   -- this player's own best tier found
    PRIMARY KEY (room_code, item_key, player_id)
);
"""


def init():
    with _lock:
        if _conn is None:
            _connect()
        _conn.executescript(SCHEMA)
        # migrate older DBs: add last_active and backfill from created_at
        cols = [r[1] for r in _conn.execute("PRAGMA table_info(rooms)").fetchall()]
        if "last_active" not in cols:
            _conn.execute("ALTER TABLE rooms ADD COLUMN last_active REAL")
        _conn.execute("UPDATE rooms SET last_active = COALESCE(last_active, created_at)")
        # migrate older DBs: public watch handle (back-fill a random id per room)
        if "pub_id" not in cols:
            _conn.execute("ALTER TABLE rooms ADD COLUMN pub_id TEXT")
        for row in _conn.execute("SELECT code FROM rooms WHERE pub_id IS NULL").fetchall():
            _conn.execute("UPDATE rooms SET pub_id=? WHERE code=?",
                          (secrets.token_urlsafe(9), row[0]))
        # migrate older DBs: game-mode columns
        if "mode" not in cols:
            _conn.execute("ALTER TABLE rooms ADD COLUMN mode TEXT NOT NULL DEFAULT 'normal'")
        if "shuffle_s" not in cols:
            _conn.execute("ALTER TABLE rooms ADD COLUMN shuffle_s REAL NOT NULL DEFAULT 120")
        if "rules" not in cols:
            _conn.execute("ALTER TABLE rooms ADD COLUMN rules TEXT")
        # migrate older DBs: link players to a Discord user
        pcols = [r[1] for r in _conn.execute("PRAGMA table_info(players)").fetchall()]
        if "discord_id" not in pcols:
            _conn.execute("ALTER TABLE players ADD COLUMN discord_id TEXT")
        if "avatar" not in pcols:
            _conn.execute("ALTER TABLE players ADD COLUMN avatar TEXT")
        # migrate older DBs: per-player discovered tier (defaults legacy rows to 1)
        disc_cols = [r[1] for r in _conn.execute("PRAGMA table_info(discovered)").fetchall()]
        if "level" not in disc_cols:
            _conn.execute("ALTER TABLE discovered ADD COLUMN level INTEGER NOT NULL DEFAULT 1")
        _conn.commit()


def _q(sql, params=()):
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur


# ── rooms ────────────────────────────────────────────────────────────────
def create_room(name: str, cooldown_s: float = 5.0) -> str:
    # This code is the room's join credential. Ten base32-like characters keep
    # it typeable while making online guessing impractical. Retry the vanishingly
    # unlikely collision instead of surfacing a database error.
    for _ in range(8):
        code = "".join(secrets.choice(ROOM_CODE_ALPHABET) for _ in range(ROOM_CODE_LENGTH))
        pub_id = secrets.token_urlsafe(9)       # public watch handle (~12 chars)
        now = time.time()
        try:
            _q("INSERT INTO rooms(code, name, pub_id, host_player_id, cooldown_s, created_at, last_active) "
               "VALUES (?,?,?,?,?,?,?)", (code, name, pub_id, None, cooldown_s, now, now))
            return code
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("could not allocate a unique room code")


def get_room(code: str):
    return _q("SELECT * FROM rooms WHERE code=?", (code,)).fetchone()


def get_room_by_pub(pub_id: str):
    return _q("SELECT * FROM rooms WHERE pub_id=?", (pub_id,)).fetchone()


def list_rooms():
    """Public 'live rooms' list — exposes only the public watch handle (`pub_id`)
    and never the private join `code`, plus the player roster (names + avatars)."""
    rows = _q("SELECT code, pub_id, name, created_at, last_active "
              "FROM rooms WHERE pub_id IS NOT NULL ORDER BY last_active DESC").fetchall()
    roster = {}
    for p in _q("SELECT room_code, display_name, avatar FROM players "
                "ORDER BY joined_at").fetchall():
        roster.setdefault(p["room_code"], []).append(
            {"name": p["display_name"], "avatar": p["avatar"]})
    out = []
    for r in rows:
        pl = roster.get(r["code"], [])
        out.append({"pub_id": r["pub_id"], "name": r["name"], "last_active": r["last_active"],
                    "players": len(pl), "player_list": pl})
    return out


def update_name(code: str, name: str):
    _q("UPDATE rooms SET name=? WHERE code=?", (name, code))


def delete_room(code: str):
    """Delete one room and everything scoped to it."""
    with _lock:
        for tbl in ("players", "ledger", "discovered"):
            _conn.execute(f"DELETE FROM {tbl} WHERE room_code=?", (code,))
        _conn.execute("DELETE FROM rooms WHERE code=?", (code,))
        _conn.commit()


def touch_room(code: str):
    """Mark a room active now (gates auto-expiry)."""
    _q("UPDATE rooms SET last_active=? WHERE code=?", (time.time(), code))


def prune_rooms(max_age_seconds: float):
    """Delete rooms idle longer than max_age (and their players/ledger/discovered).
    Returns the list of pruned room codes."""
    cutoff = time.time() - max_age_seconds
    with _lock:
        codes = [r["code"] for r in _conn.execute(
            "SELECT code FROM rooms WHERE last_active < ?", (cutoff,)).fetchall()]
        for code in codes:
            _conn.execute("DELETE FROM players WHERE room_code=?", (code,))
            _conn.execute("DELETE FROM ledger WHERE room_code=?", (code,))
            _conn.execute("DELETE FROM discovered WHERE room_code=?", (code,))
            _conn.execute("DELETE FROM rooms WHERE code=?", (code,))
        _conn.commit()
        return codes


def set_host(code: str, player_id: int):
    _q("UPDATE rooms SET host_player_id=? WHERE code=?", (player_id, code))


def update_cooldown(code: str, seconds: float):
    _q("UPDATE rooms SET cooldown_s=? WHERE code=?", (seconds, code))


def update_mode(code: str, mode: str, shuffle_s: float):
    _q("UPDATE rooms SET mode=?, shuffle_s=? WHERE code=?", (mode, shuffle_s, code))


def update_rules(code: str, rules_json):
    _q("UPDATE rooms SET rules=? WHERE code=?", (rules_json, code))


# ── players ──────────────────────────────────────────────────────────────
def add_player(room_code: str, display_name: str, discord_id: str = None, avatar: str = None):
    token = secrets.token_urlsafe(18)
    cur = _q("INSERT INTO players(room_code, display_name, player_token, discord_id, avatar, joined_at) "
             "VALUES (?,?,?,?,?,?)", (room_code, display_name, token, discord_id, avatar, time.time()))
    return cur.lastrowid, token


def set_player_identity(player_id: int, display_name: str, avatar: str):
    """Refresh a Discord-linked player's name + avatar when they rejoin."""
    _q("UPDATE players SET display_name=?, avatar=? WHERE id=?", (display_name, avatar, player_id))


def get_player(player_id: int):
    return _q("SELECT * FROM players WHERE id=?", (player_id,)).fetchone()


def get_player_by_discord(room_code: str, discord_id: str):
    """The player a logged-in Discord user already has in this room (for dedup +
    rejoin), most-recent first if somehow duplicated."""
    return _q("SELECT * FROM players WHERE room_code=? AND discord_id=? ORDER BY id DESC",
              (room_code, discord_id)).fetchone()


def rooms_for_discord(discord_id: str):
    """Rooms a Discord user has a player in — their personal 'rejoin' list."""
    rows = _q(
        "SELECT r.code, r.pub_id, r.name, r.last_active, p.id AS player_id, "
        "  (p.id = r.host_player_id) AS is_host "
        "FROM players p JOIN rooms r ON r.code = p.room_code "
        "WHERE p.discord_id = ? ORDER BY r.last_active DESC", (discord_id,)).fetchall()
    return [dict(r) for r in rows]


def player_by_token(room_code: str, player_id: int, player_token: str):
    return _q("SELECT * FROM players WHERE room_code=? AND id=? AND player_token=?",
              (room_code, player_id, player_token)).fetchone()


def room_players(room_code: str):
    return _q("SELECT * FROM players WHERE room_code=? ORDER BY joined_at",
              (room_code,)).fetchall()


def remove_player(room_code: str, player_id: int):
    _q("DELETE FROM players WHERE room_code=? AND id=?", (room_code, player_id))
    _q("DELETE FROM discovered WHERE room_code=? AND player_id=?", (room_code, player_id))
    _q("UPDATE ledger SET owner_player_id=NULL WHERE room_code=? AND owner_player_id=?",
       (room_code, player_id))


# ── ledger persistence ───────────────────────────────────────────────────
def load_ledger(room_code: str):
    rows = _q("SELECT * FROM ledger WHERE room_code=?", (room_code,)).fetchall()
    disc = _q("SELECT * FROM discovered WHERE room_code=?", (room_code,)).fetchall()
    return rows, disc


def upsert_ledger(room_code, item_key, owner_player_id, level, cooldown_until):
    _q("INSERT INTO ledger(room_code, item_key, owner_player_id, level, cooldown_until) "
       "VALUES (?,?,?,?,?) "
       "ON CONFLICT(room_code, item_key) DO UPDATE SET "
       "owner_player_id=excluded.owner_player_id, level=excluded.level, "
       "cooldown_until=excluded.cooldown_until",
       (room_code, item_key, owner_player_id, level, cooldown_until))


def add_discovered(room_code, item_key, player_id, level=1):
    _q("INSERT INTO discovered(room_code, item_key, player_id, level) VALUES (?,?,?,?) "
       "ON CONFLICT(room_code, item_key, player_id) DO UPDATE SET level=excluded.level",
       (room_code, item_key, player_id, level))


def remove_discovered(room_code, item_key, player_id):
    _q("DELETE FROM discovered WHERE room_code=? AND item_key=? AND player_id=?",
       (room_code, item_key, player_id))
