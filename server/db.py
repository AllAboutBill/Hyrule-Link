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
    code            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    host_player_id  INTEGER,
    cooldown_s      REAL NOT NULL DEFAULT 5,
    created_at      REAL NOT NULL,
    last_active     REAL
);
-- a player is just a name + token scoped to one room (no account / password).
CREATE TABLE IF NOT EXISTS players (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    room_code     TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    player_token  TEXT NOT NULL,
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
        _conn.commit()


def _q(sql, params=()):
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur


# ── rooms ────────────────────────────────────────────────────────────────
def create_room(name: str, cooldown_s: float = 5.0) -> str:
    code = secrets.token_hex(3).upper()  # 6-char join code
    now = time.time()
    _q("INSERT INTO rooms(code, name, host_player_id, cooldown_s, created_at, last_active) "
       "VALUES (?,?,?,?,?,?)", (code, name, None, cooldown_s, now, now))
    return code


def get_room(code: str):
    return _q("SELECT * FROM rooms WHERE code=?", (code,)).fetchone()


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


# ── players ──────────────────────────────────────────────────────────────
def add_player(room_code: str, display_name: str):
    token = secrets.token_urlsafe(18)
    cur = _q("INSERT INTO players(room_code, display_name, player_token, joined_at) "
             "VALUES (?,?,?,?)", (room_code, display_name, token, time.time()))
    return cur.lastrowid, token


def get_player(player_id: int):
    return _q("SELECT * FROM players WHERE id=?", (player_id,)).fetchone()


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


def add_discovered(room_code, item_key, player_id):
    _q("INSERT OR IGNORE INTO discovered(room_code, item_key, player_id) VALUES (?,?,?)",
       (room_code, item_key, player_id))


def remove_discovered(room_code, item_key, player_id):
    _q("DELETE FROM discovered WHERE room_code=? AND item_key=? AND player_id=?",
       (room_code, item_key, player_id))
