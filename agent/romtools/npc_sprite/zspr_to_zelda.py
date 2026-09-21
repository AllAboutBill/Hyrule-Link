"""Reskin Zelda the follower from a Link ZSPR (e.g. max.zspr).

Pulls Link's Stand poses out of a .zspr, lays them into the follower's tile
layout, reduces 4bpp->3bpp (7 colors), rewrites OBJ palette 4 to Link's colors,
and injects into a ROM.

Follower draw model (DECODED from the girly_follower .do_chars path,
bank_09 ~$09AAE6; see project memory). Zelda *glides* — ONE pose per direction,
no leg animation. Each direction draws a fixed 16x16 head (OAM tile $20) above a
fixed 16x16 body (OAM tile $22), the body 10px below the head. The pixels behind
$20/$22 are re-copied every NMI from the follower 4bpp buffer at offset
2*FLWHO / 2*FLWBO, where FLWHO/FLWBO are per-direction GFX offsets from .props:
    dir    head FLWHO   body FLWBO   flip
    up       $20          $C0        none
    down     $00          $A0        none
    left     $40          $60        none (this is the NATIVE side art)
    right    $40          $60        h-flip
The buffer is built ONLY from sheet 0x64 (high), char-row 0 = its first two
tile-rows (32 tiles). FLWHO/$10 = the 8x8 tile-column of each 16x16 char:
    head: down=col0, up=col2, side=col4
    body: side=col6, down=col10, up=col12
(each 16x16 char = 8x8 tiles {c, c+1, c+16, c+17}). Sheet 0x65 and the rest of
0x64 are dead for the follower; we mirror the art into 0x65 too, for safety.

The follower is repointed to static OBJ sprite palette 3 (free in the standard
escape — nothing else uses it), so Max's colors no longer recolor other sprites.

Usage:
    python zspr_to_zelda.py <rom> <sprite.zspr> <out_rom> [--mail green|blue|red] [--pal 1-4]
    python zspr_to_zelda.py <rom> <sprite.zspr> --preview      # just render, no ROM
"""
import sys, os, re, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PIL import Image
import lc_lz2, sheets, tiles, inject

ZELDA_LOW, ZELDA_HIGH = 0x65, 0x64
SP_LOW, SP_HIGH, SP_BANK = sheets.SPRITE_LOW, sheets.SPRITE_HIGH, sheets.SPRITE_BANK


def _frozen():
    return getattr(sys, "frozen", False)


def animations_json_path():
    """Locate animations.json: bundled at the EXE root when frozen, otherwise next
    to this module (it is shipped alongside the converter in this app)."""
    if _frozen():
        return os.path.join(sys._MEIPASS, "animations.json")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "animations.json")


def preview_png_path():
    """A writable location for the preview image (the bundle dir is read-only)."""
    d = tempfile.gettempdir() if _frozen() else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "out")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "zspr_zelda_preview.png")


# back-compat for dev diagnostic scripts (the manifests directory, dev only)
MANIFESTS = os.path.dirname(animations_json_path()) if not _frozen() else ""

ROW_NAMES = [chr(c) for c in range(ord("A"), ord("Z") + 1)] + ["AA", "AB"]
GRID_RE = re.compile(r"^(A?[A-Z])([0-7])$")
MAIL_OFFSET = {"green": 0, "blue": 1, "red": 2}

# 8x8 tile-column of each 16x16 char in sheet 0x64 char-row 0.
# MEASURED in-game with a per-column color test (colmap_test.py), NOT derived:
#   head: down=col0, up=col2, side=col4
#   body: down=col10, up=col12, side=col8
# The SIDE follower has a TWO-FRAME walk: the engine alternates the side body
# between col8 and col6 while moving. col8 is the standing/frame-A body; col6 is
# frame-B. (up/down are single-frame, so they never drop legs.) Filling only col8
# made the col6 frame blank -> legs vanished every other left/right step.
HEAD_COL = {"down": 0, "up": 2, "side": 4}
BODY_COL = {"side": 8, "down": 10, "up": 12}
SIDE_WALK_COL = 6        # side body, walk frame B (alternates with col8)
SIDE_WALK_FRAME = 4      # Link Walk/left frame sampled for the side's frame-B body
# follower direction -> Link manifest direction; 'side' = Link 'left' (native, no flip)
DIR_ANIM = {"up": "up", "down": "down", "side": "left"}
# vertical gap (px) between the head char and the body char (body Y = head Y + 10)
BODY_DROP = 10


def conv555(w):
    r = (w & 0x1F) << 3; g = ((w >> 5) & 0x1F) << 3; b = ((w >> 10) & 0x1F) << 3
    return (r | r >> 5, g | g >> 5, b | b >> 5)


def load_zspr(path):
    d = open(path, "rb").read()
    assert d[:4] == b"ZSPR", "not a ZSPR file"
    soff = int.from_bytes(d[9:13], "little")
    slen = int.from_bytes(d[13:15], "little")
    poff = int.from_bytes(d[15:19], "little")
    sheet = d[soff:soff + slen]
    pal = d[poff:poff + 0x78]  # 4 mails * 15 colors * 2 bytes
    return sheet, pal


def mail_palette(pal_bytes, mail):
    base = MAIL_OFFSET[mail] * 0x0F  # 15 colors per mail
    words = [int.from_bytes(pal_bytes[i:i + 2], "little") for i in range(0, len(pal_bytes), 2)]
    rgb = [(0, 0, 0)] + [conv555(words[base + i]) for i in range(15)]  # idx0 transparent
    return rgb  # 16 entries, index 0 transparent


def named_tile(sheet, name):
    """Return a 16x16 grid of 4bpp values for a Link sheet tile like 'A1'."""
    m = GRID_RE.match(name)
    if not m:
        return None
    rowidx = ROW_NAMES.index(m.group(1))
    col = int(m.group(2))
    base = 0x400 * rowidx + 0x40 * col
    img = [[0] * 16 for _ in range(16)]
    for off, (ox, oy) in [(0x00, (0, 0)), (0x20, (8, 0)), (0x200, (0, 8)), (0x220, (8, 8))]:
        raw = sheet[base + off:base + off + 0x20]
        t = tiles.decode_tile_4bpp(raw)
        for i, v in enumerate(t):
            img[oy + i // 8][ox + i % 8] = v
    return img


def assemble_pose(sheet, anim, animation, direction, frame=0):
    """Composite a Link pose, return its head (top 16x16) and body (16x16 starting
    BODY_DROP px below the head) as 4bpp index grids."""
    frames = anim[animation][direction]
    pose = frames[min(frame, len(frames) - 1)]
    PAD = 8
    W, H = 16 + 2 * PAD, 24 + 2 * PAD
    canvas = [[0] * W for _ in range(H)]
    for t in pose["tiles"]:
        tile = named_tile(sheet, t["image"])
        if tile is None:
            continue  # skip shields, shadows, equipment
        px, py = t["pos"]
        flip = t.get("flip", "")
        for y in range(16):
            for x in range(16):
                v = tile[y][16 - 1 - x if "h" in flip else x]
                if v == 0:
                    continue
                cx, cy = PAD + px + x, PAD + py + y
                if 0 <= cx < W and 0 <= cy < H:
                    canvas[cy][cx] = v
    # Horizontal window: keep the whole pose in frame. Side-facing Link poses sit
    # a couple px right of the front/back poses and would otherwise clip at the
    # face. Pick a 16-wide window over the pose's content; when content is already
    # a full 16 wide (front/back) this is exactly PAD..PAD+16, leaving them as-is.
    cols = [c for c in range(W) if any(canvas[r][c] for r in range(H))]
    if cols:
        w = max(cols) - min(cols) + 1
        x0 = min(cols) - max(0, 16 - w) // 2
        x0 = max(0, min(W - 16, x0))
    else:
        x0 = PAD
    head = [row[x0:x0 + 16] for row in canvas[PAD:PAD + 16]]
    body = [row[x0:x0 + 16] for row in canvas[PAD + BODY_DROP:PAD + BODY_DROP + 16]]
    return head, body


def build_cells(sheet, anim):
    """Return {(role, dir): 16x16 4bpp grid} for the follower chars: a head and
    a (standing) body per direction, plus a second side body for the side's
    two-frame walk (the engine alternates side body col8<->col6 while moving)."""
    cells = {}
    for fdir, link_dir in DIR_ANIM.items():
        head, body = assemble_pose(sheet, anim, "Stand", link_dir)
        cells[("head", fdir)] = head
        cells[("body", fdir)] = body
    # side walk frame B: a Link Walk/left pose so left/right legs animate instead
    # of dropping. Heads don't animate (same col4), so only a body is needed.
    _, side_walk_body = assemble_pose(sheet, anim, "Walk", "left", SIDE_WALK_FRAME)
    cells[("body_walk", "side")] = side_walk_body
    return cells


def quantize_palette(cells, link_pal):
    """Collect used 4bpp indices across cells, map to <=7 palette-4 slots."""
    used = sorted({v for grid in cells.values() for row in grid for v in row if v})
    # map each used Link index -> a slot 1..7
    if len(used) <= 7:
        idx_map = {0: 0}
        pal = [(0, 0, 0)]
        for slot, li in enumerate(used, start=1):
            idx_map[li] = slot
            pal.append(link_pal[li])
        while len(pal) < 8:
            pal.append((0, 0, 0))
        return idx_map, pal
    # >7: greedily merge nearest colors until 7 remain
    reps = [(li, link_pal[li]) for li in used]
    def dist(a, b): return sum((a[i] - b[i]) ** 2 for i in range(3))
    while len(reps) > 7:
        best = None
        for i in range(len(reps)):
            for j in range(i + 1, len(reps)):
                d = dist(reps[i][1], reps[j][1])
                if best is None or d < best[0]:
                    best = (d, i, j)
        _, i, j = best
        reps[i] = (reps[i][0], reps[i][1])  # keep i's color, fold j into it
        merged_into = reps[i][0]
        folded = reps.pop(j)[0]
        quantize_palette._fold = getattr(quantize_palette, "_fold", {})
        quantize_palette._fold[folded] = merged_into
    keep = {li: slot for slot, (li, _) in enumerate(reps, start=1)}
    fold = getattr(quantize_palette, "_fold", {})
    idx_map = {0: 0}
    for li in used:
        target = li
        while target in fold:
            target = fold[target]
        idx_map[li] = keep[target]
    pal = [(0, 0, 0)] + [c for _, c in reps]
    while len(pal) < 8:
        pal.append((0, 0, 0))
    return idx_map, pal


def cells_to_sheets(cells, idx_map):
    """Lay the 6 chars into sheet 0x64 char-row 0 (tile rows 0-1) at the engine's
    tile-columns, then return identical 0x600 3bpp sheets for 0x65 (low) and
    0x64 (high). Only 0x64 rows 0-1 are read by the follower engine; 0x65 is a
    safety mirror. All other tiles are left blank."""
    # 64 tiles of 8x8 (each an 8x8 grid of 3bpp values), 16-wide x 4-tall
    sheet = [[[0] * 8 for _ in range(8)] for _ in range(64)]

    def put(tilecol, grid16):
        # 16x16 char at row0/col=tilecol -> 8x8 tiles {c, c+1, c+16, c+17}
        for (sy, sx, dtile) in [(0, 0, tilecol), (0, 8, tilecol + 1),
                                (8, 0, tilecol + 16), (8, 8, tilecol + 17)]:
            t = sheet[dtile]
            for y in range(8):
                for x in range(8):
                    t[y][x] = idx_map.get(grid16[sy + y][sx + x], 0)

    for fdir in ("up", "down", "side"):
        put(HEAD_COL[fdir], cells[("head", fdir)])
        put(BODY_COL[fdir], cells[("body", fdir)])
    put(SIDE_WALK_COL, cells[("body_walk", "side")])  # side walk frame B (col6)

    data = bytearray()
    for t in sheet:
        data += inject.encode_tile_3bpp([t[y][x] for y in range(8) for x in range(8)])
    packed = bytes(data)
    return packed, packed  # (0x65 low, 0x64 high) — identical mirror


def render_preview(cells, idx_map, pal, out_png):
    """Render the follower as the engine draws it: head over body (10px drop),
    for each on-screen direction. 'right' = h-flipped 'side', as the engine flips."""
    cellw, cellh = 18, 16 + BODY_DROP + 2
    shown = [("up", "up", False), ("down", "down", False),
             ("left", "side", False), ("right", "side", True)]
    canvas = Image.new("RGBA", (cellw * len(shown), cellh), (40, 40, 50, 255))
    px = canvas.load()

    def blit(grid, ox, oy, hflip):
        for y in range(16):
            for x in range(16):
                v = idx_map.get(grid[y][15 - x if hflip else x], 0)
                if v:
                    px[ox + x, oy + y] = pal[v] + (255,)

    for i, (label, fdir, hflip) in enumerate(shown):
        ox = i * cellw + 1
        blit(cells[("head", fdir)], ox, 0, hflip)
        blit(cells[("body", fdir)], ox, BODY_DROP, hflip)
    canvas = canvas.resize((canvas.width * 8, canvas.height * 8), Image.NEAREST)
    canvas.save(out_png)
    print(f"preview -> {out_png}  (cols: up / down / left / right)")


# --- Sprite palette ROM layout (verified from PaletteLoad_SpriteMain / bank_1B) ---
# Static OBJ sprite palettes 1-4 are loaded from one contiguous 60-color block,
# 15 colors each, light world @ file 0xDD218, dark world @ +0x78. The 3bpp
# follower uses colors 1-7 of its palette ('left'/low-8 expansion).
SPRITE_PAL_BASE = 0xDD218          # PaletteData_sprite_00, light world
SPRITE_PAL_WORLD_STRIDE = 0x78     # +0x78 -> dark world block
# .palette table byte for follower 01 (Zelda); value = OBJ palette index used.
FOLLOWER_PAL_FILE_OFF = 0x4A8E6

# DEPRECATED no-op: the moving follower's legs come from the .do_chars body
# reading the sheet (the side has a 2-frame walk, col8<->col6), NOT a separate
# path — so this "glide" branch patch does nothing useful. Kept off by default.
GIRLY_GLIDE_OPERAND_OFF = 0x4AA55

# Cell + sanctuary STANDING Zelda is sprite 0x76 (SpriteDraw_Maiden). It draws the
# SAME follower buffer graphics (SpritePrep_Zelda calls LoadFollowerGraphics), but
# takes its palette from its OAM property, SpriteData_OAMProp[0x76] @ file 0x6B3CF
# (vanilla $49 = palette 4). Repoint its palette bits (1-3) to the follower palette
# so the cell/sanctuary Zelda shows Max's colors too.
SPRITE76_OAMPROP_OFF = 0x6B3CF
# Back-compat: pal 4 colors-1 == SPRITE_PAL_BASE + (4-1)*30 == 0xDD272.
PAL4_FILE_OFF = 0xDD272


def sprite_pal_offset(pal, world=0):
    """File offset of color 1 of static OBJ sprite palette `pal` (1..4)."""
    return SPRITE_PAL_BASE + world * SPRITE_PAL_WORLD_STRIDE + (pal - 1) * 30


def write_follower_palette(rom, pal_colors, pal_index):
    """Write Max's 7 colors into static OBJ palette `pal_index` (1..4), both world
    blocks, point follower 01 at that palette, and repoint the cell/sanctuary
    standing Zelda (sprite 0x76) to the same palette. pal_colors[1..7] are RGB."""
    for world in (0, 1):
        off = sprite_pal_offset(pal_index, world)
        for i in range(7):  # colors 1..7 (skip transparent slot 0)
            c = pal_colors[i + 1]
            w = (c[0] >> 3) | ((c[1] >> 3) << 5) | ((c[2] >> 3) << 10)
            rom[off + i * 2] = w & 0xFF
            rom[off + i * 2 + 1] = (w >> 8) & 0xFF
    rom[FOLLOWER_PAL_FILE_OFF] = pal_index & 0xFF
    # sprite 0x76 (cell/sanctuary Zelda): set its OAM palette bits (1-3) to pal_index
    rom[SPRITE76_OAMPROP_OFF] = (rom[SPRITE76_OAMPROP_OFF] & ~0x0E) | ((pal_index & 7) << 1)


def convert(rom_path, zspr_path, out_rom=None, mail="green", write_pal=True,
            follower_pal=3, glide=False, log=print):
    """Reskin the Zelda follower from a ZSPR. Renders a preview always; injects
    into the ROM and writes out_rom when out_rom is given (preview-only if None).
    `follower_pal` (1..4) is the static OBJ sprite palette to give the follower;
    3 is free in the standard escape (nothing else uses it), so the default keeps
    Max's colors to himself. `write_pal=False` leaves all palette data untouched.
    `log` is a callable for progress lines. Returns a dict with results.
    Raises ValueError with a friendly message on a recoverable problem."""
    sheet, pal_bytes = load_zspr(zspr_path)
    link_pal = mail_palette(pal_bytes, mail)
    with open(animations_json_path()) as f:
        anim = json.load(f)

    cells = build_cells(sheet, anim)
    idx_map, pal4 = quantize_palette(cells, link_pal)
    ncolors = len(set(idx_map.values()) - {0})
    log(f"Link {mail} mail -> {ncolors} colors used")

    preview = preview_png_path()
    render_preview(cells, idx_map, pal4, preview)
    result = {"preview": preview, "colors": ncolors, "pal4": pal4[1:], "out_rom": None}
    if out_rom is None:
        return result

    sheet_low, sheet_high = cells_to_sheets(cells, idx_map)
    rom = bytearray(open(rom_path, "rb").read())

    # Write each recompressed sheet IN PLACE at its existing pointer location.
    # No repointing, no ROM expansion -> size unchanged, no bank-mirror issues,
    # works identically on vanilla JP and the (already-expanded) rando ROM.
    for index, data in ((ZELDA_LOW, sheet_low), (ZELDA_HIGH, sheet_high)):
        info = sheets.resolve_sheet(rom, index, "sprite")
        _, orig_clen = lc_lz2.decompress(rom, info["file"])
        comp = lc_lz2.compress(data)
        if len(comp) > orig_clen:
            raise ValueError(
                f"sheet {index:#04x}: recompressed {len(comp)} B > slot {orig_clen} B; "
                f"won't fit in place. Use a simpler / fewer-color source sprite.")
        rom[info["file"]:info["file"] + len(comp)] = comp
        log(f"  sheet {index:#04x}: {len(comp)} B in place @ {info['file']:#x} (slot {orig_clen} B)")

    if write_pal:
        write_follower_palette(rom, pal4, follower_pal)
        log(f"  follower + cell/sanctuary Zelda (spr 0x76) repointed to OBJ palette "
            f"{follower_pal}; colors written @ {sprite_pal_offset(follower_pal):#x} (+dark world)")
    else:
        log("  palettes left unchanged")

    if glide:
        rom[GIRLY_GLIDE_OPERAND_OFF] = 0x00
        log(f"  glide patch applied @ {GIRLY_GLIDE_OPERAND_OFF:#x} "
            f"(follower uses standing render while moving)")

    open(out_rom, "wb").write(rom)
    log(f"wrote {out_rom} ({len(rom)} bytes)")
    result["out_rom"] = out_rom
    return result


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        print(__doc__); return
    rom_path, zspr_path = args[0], args[1]
    mail = args[args.index("--mail") + 1] if "--mail" in args else "green"
    pal = int(args[args.index("--pal") + 1]) if "--pal" in args else 3
    glide = "--glide" in args  # deprecated no-op; the side walk uses .do_chars body
    preview_only = "--preview" in args
    out_rom = None if preview_only else (
        args[2] if len(args) > 2 and not args[2].startswith("--") else None)
    convert(rom_path, zspr_path, out_rom, mail, follower_pal=pal, glide=glide)


if __name__ == "__main__":
    main()
