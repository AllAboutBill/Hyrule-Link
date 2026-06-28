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
import urllib.parse
import urllib.request

DISCORD_API = "https://discord.com/api"
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

# Login is only offered when fully configured.
LOGIN_ENABLED = bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET
                     and DISCORD_REDIRECT_URI and SESSION_SECRET)


# ── signed tokens (cookies + oauth state) ───────────────────────────────────
def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(data: dict) -> str:
    payload = _b64e(json.dumps(data, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def unsign(token: str):
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


def new_state() -> str:
    return sign({"n": _b64e(os.urandom(9)), "exp": time.time() + STATE_TTL})


def session_from_cookies(cookies: dict):
    tok = cookies.get(SESSION_COOKIE)
    return unsign(tok) if tok else None


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
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read())


def _get(url: str, token: str):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read())


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
