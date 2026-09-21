"""sprites.py — alttpr.com player-sprite catalog, with on-disk preview cache.

The randomizer's sprite list lives at https://alttpr.com/sprites (JSON: name,
author, file=.zspr URL, preview=.png URL). pyz3r applies a sprite by name at
patch time, so all we need locally is the catalog for the picker plus preview
images for the dialog. Everything is cached under agent/cache/sprites so the
picker works offline after first use. stdlib-only (urllib) — no new deps.
"""
import json
import os
import re
import time
import urllib.parse
import urllib.request

SPRITES_API = "https://alttpr.com/sprites"
# Animated walk-cycle GIFs (community gallery; not every sprite has one).
GIF_GALLERY = "https://hyphen-ated.github.io/alttpr-sprite-gallery/spriteimgs/{name}.gif"
# Combined portrait sheet: 16x24 cells, row-major. Cell 0 is a "??" random
# placeholder; catalog()[i] is cell i+1 (the AlttprHelper trick — one download,
# no per-sprite fetches for list icons).
SPRITESHEET_URL = "https://alttpr-assets.s3.us-east-2.amazonaws.com/sprites.31.2.png"

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "cache", "sprites")
CATALOG_FILE = os.path.join(CACHE_DIR, "catalog.json")
CATALOG_TTL = 7 * 24 * 3600          # refresh the sprite list weekly
_UA = {"User-Agent": "HyruleLink (github.com/AllAboutBill)"}

RANDOM_SPRITE = "Random"             # picker sentinel — resolved at patch time


def _fetch(url, timeout=20):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _safe_name(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def catalog(refresh=False):
    """The sprite list as [{name, author, file, preview}, ...]. Served from the
    weekly disk cache; falls back to a stale cache when alttpr.com is down.
    Raises only if there is no cache at all AND the fetch fails."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    fresh = (os.path.exists(CATALOG_FILE)
             and time.time() - os.path.getmtime(CATALOG_FILE) < CATALOG_TTL)
    if fresh and not refresh:
        with open(CATALOG_FILE, encoding="utf-8") as f:
            return json.load(f)
    try:
        data = json.loads(_fetch(SPRITES_API))
        with open(CATALOG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return data
    except Exception:
        if os.path.exists(CATALOG_FILE):     # stale beats nothing
            with open(CATALOG_FILE, encoding="utf-8") as f:
                return json.load(f)
        raise


def sprite_names(refresh=False):
    return [s["name"] for s in catalog(refresh)]


def find(name):
    """Catalog entry for `name` (case-insensitive), or None."""
    want = name.strip().lower()
    for s in catalog():
        if s["name"].lower() == want:
            return s
    return None


def _cached_download(url, dest):
    """Download `url` to `dest` once; a zero-byte `.miss` marker remembers a
    failed fetch (e.g. sprites with no gallery GIF) so we don't re-try every
    time the picker lands on them. Returns the local path or None."""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    miss = dest + ".miss"
    if os.path.exists(miss) and time.time() - os.path.getmtime(miss) < CATALOG_TTL:
        return None
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        data = _fetch(url)
        with open(dest, "wb") as f:
            f.write(data)
        return dest
    except Exception:
        open(miss, "a").close()
        return None


def preview_png(sprite):
    """Local path of the sprite's 16x24 preview PNG (downloaded once), or None."""
    url = sprite.get("preview")
    if not url:
        return None
    return _cached_download(url, os.path.join(CACHE_DIR, _safe_name(sprite["name"]) + ".png"))


def preview_gif(name):
    """Local path of the sprite's animated walk GIF (community gallery; many
    sprites have one, not all), or None."""
    url = GIF_GALLERY.format(name=urllib.parse.quote(name))
    return _cached_download(url, os.path.join(CACHE_DIR, _safe_name(name) + ".gif"))


def spritesheet_png():
    """Local path of the combined portrait sheet (downloaded once), or None.
    Cell i sits at (16*(i%cols), 24*(i//cols)), cols = width//16; cell 0 is the
    "??" placeholder and catalog()[i] is cell i+1."""
    return _cached_download(SPRITESHEET_URL, os.path.join(CACHE_DIR, "spritesheet.png"))


def zspr_file(sprite):
    """Local path of the sprite's .zspr (downloaded once), or None. Used to feed
    the Zelda-follower reskin with the same sprite the player picked."""
    url = sprite.get("file")
    if not url or not url.lower().endswith(".zspr"):
        return None
    return _cached_download(url, os.path.join(CACHE_DIR, _safe_name(sprite["name"]) + ".zspr"))
