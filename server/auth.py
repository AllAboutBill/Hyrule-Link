"""
auth.py — Discord OAuth2 login + signed session cookies.

Admin rights come from your Discord server: the guild **owner** and anyone with a
configured **mod role** become HyruleLink global admins (delete/kick/manage any
room). There are no passwords or shared keys — just "Login with Discord".

Config (env / .env), reusing your existing Discord application is fine:
    DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET
    DISCORD_REDIRECT_URI      e.g. https://hyrulelink.billogna.lol/auth/callback
    DISCORD_GUILD_ID          your server id
    DISCORD_MOD_ROLE_IDS      comma-separated role ids that count as admin
    DISCORD_ADMIN_USER_IDS    (optional) comma-separated user ids always admin
    SESSION_SECRET            random string used to sign the session cookie
"""

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

DISCORD_API = "https://discord.com/api"
# Discord's CDN (Cloudflare) blocks the default Python-urllib agent with error
# 1010 — every API call MUST send a real User-Agent.
USER_AGENT = "HyruleLink (https://hyrulelink.billogna.lol, 1.0)"
SCOPES = "identify guilds guilds.members.read"
SESSION_COOKIE = "hl_session"
SESSION_TTL = 7 * 86400          # 1 week
STATE_TTL = 600                  # 10 min for the OAuth round-trip

DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", "")
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")
MOD_ROLE_IDS = {r for r in os.environ.get("DISCORD_MOD_ROLE_IDS", "").split(",") if r.strip()}
ADMIN_USER_IDS = {u for u in os.environ.get("DISCORD_ADMIN_USER_IDS", "").split(",") if u.strip()}
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
TOKENS_ENABLED = len(SESSION_SECRET) >= 32

# Login is only offered when fully configured.
LOGIN_ENABLED = bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET
                     and DISCORD_REDIRECT_URI and TOKENS_ENABLED)


# ── signed tokens (cookies + oauth state) ───────────────────────────────────
def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(data: dict) -> str:
    if not TOKENS_ENABLED:
        raise RuntimeError("SESSION_SECRET must contain at least 32 characters")
    payload = _b64e(json.dumps(data, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def unsign(token: str):
    # An empty HMAC key is public knowledge, so accepting tokens without a
    # configured secret would let anyone mint an admin session.
    if not TOKENS_ENABLED:
        return None
    try:
        payload, sig = token.split(".", 1)
        expected = _b64e(hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(_b64d(payload))
        if float(data.get("exp", 0)) < time.time():
            return None
        return data
    except Exception:
        return None


def new_state(pair: str = "") -> str:
    d = {"n": _b64e(os.urandom(9)), "exp": time.time() + STATE_TTL}
    if pair:
        d["pair"] = pair
    return sign(d)


def new_pairing_id() -> str:
    return _b64e(os.urandom(18))


def session_from_cookies(cookies: dict):
    tok = cookies.get(SESSION_COOKIE)
    return unsign(tok) if tok else None


def session_from_token(token: str):
    """For the desktop app, which sends the signed session as a header / WS field
    instead of a browser cookie."""
    return unsign(token) if token else None


def authorize_url(state: str) -> str:
    q = urllib.parse.urlencode({
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "prompt": "none",
    })
    return f"{DISCORD_API}/oauth2/authorize?{q}"


# ── Discord API (blocking; call via asyncio.to_thread) ──────────────────────
def _post_form(url: str, data: dict) -> dict:
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(data).encode(), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"discord {url} -> {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")


def _get(url: str, token: str):
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"discord {url} -> {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")


def fetch_profile(code: str) -> dict:
    """Exchange the OAuth code and decide if this user is a HyruleLink admin
    (guild owner or holds a configured mod role)."""
    tok = _post_form(f"{DISCORD_API}/oauth2/token", {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI,
    })
    access = tok["access_token"]
    me = _get(f"{DISCORD_API}/users/@me", access)
    uid = str(me["id"])
    is_admin = uid in ADMIN_USER_IDS

    if DISCORD_GUILD_ID and not is_admin:
        try:  # owner?
            for g in _get(f"{DISCORD_API}/users/@me/guilds", access):
                if str(g.get("id")) == DISCORD_GUILD_ID and g.get("owner"):
                    is_admin = True
        except Exception:
            pass
    if DISCORD_GUILD_ID and not is_admin:
        try:  # mod role?
            member = _get(f"{DISCORD_API}/users/@me/guilds/{DISCORD_GUILD_ID}/member", access)
            if set(str(r) for r in member.get("roles", [])) & MOD_ROLE_IDS:
                is_admin = True
        except Exception:
            pass

    name = me.get("global_name") or me.get("username") or "discord user"
    avatar = (f"https://cdn.discordapp.com/avatars/{uid}/{me['avatar']}.png?size=64"
              if me.get("avatar") else None)
    return {"id": uid, "name": name, "avatar": avatar, "is_admin": is_admin}
