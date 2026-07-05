"""
hud_text.py — true in-game on-screen messages, no ROM patch required.

ALTTP keeps its HUD as a BG3 tilemap buffer in WRAM at $7EC700
(hud_tile_indices_buffer: 165 little-endian words, 32 words per row, rows 0-4
of the top screen strip). Every NMI the game checks $7E0016
(flag_update_hud_in_nmi) and, when nonzero, DMAs the whole buffer to VRAM and
clears the flag. The HUD's 2bpp tile space contains a RESIDENT uppercase font
the game itself uses to draw "LIFE": zelda3 hud.c

    #define L(x) (x == ' ' ? 0x24f5 : 0x2550 + x - 'A')

i.e. word 0x2550+n for 'A'+n, digits 0x2490+n (base 0x2400 | tile 0x90+n),
blank/space 0x24F5 — all with BG priority (0x2000) + palette 1 (0x0400).

So writing letter words into unused HUD cells and setting the $16 flag renders
text in-game over ANY transport that can write WRAM (RetroArch, Snes9x-NWA,
QUsb2Snes/SNI incl. real hardware) — the exact mechanism the game uses, just
driven from outside.

Sources: snesrev/zelda3 src/hud.c (L() macro, Hud_Rebuild, HUDXY(x,y)=x+y*32),
src/nmi.c (NMI_DoUpdates: `if (flag_update_hud_in_nmi) memcpy(vram, buf, 165*2)`),
src/variables.h ($7E0016 / $7EC700); cross-checked with ALTTPR-REFERENCE
docs/08-graphics-tiles-and-gfx.md §8 ($7EC700 hud_tile_indices_buffer).

Caveats:
  * The game's own Hud_Rebuild (item change, menu exit) rewrites the whole
    buffer, erasing the text — tick() detects that and redraws, so a message
    survives; it just blinks for one frame.
  * Draw only while a save is loaded (caller gates on PLAYABLE_MODES); the
    buffer is rebuilt on every file load, so stale state must be dropped when
    gameplay was left (reset()).
  * The strip must sit on cells the HUD doesn't own. Rows 0-3 hold the item
    box / magic bar (cols 0-7), counters (center) and LIFE/hearts (cols 20+);
    row 4 is the strip's bottom edge and is blank in vanilla/ALTTPR, so the
    default is row 4, cols 1-30.
"""

import threading
import time
from collections import deque

HUD_BUFFER_ADDR = 0xC700     # $7EC700 hud_tile_indices_buffer (WRAM offset)
HUD_FLAG_ADDR = 0x0016       # $7E0016 flag_update_hud_in_nmi
HUD_COLS = 32
HUD_ROWS = 5                 # buffer rows 0-4 (165 words = 5*32 + 5 spill)

SPACE_WORD = 0x24F5          # the game's own HUD space glyph
LETTER_BASE = 0x2550         # 'A' (tile 0x150, priority + palette 1)
DIGIT_BASE = 0x2490          # '0' (tile 0x90)

DEFAULT_ROW = 4
DEFAULT_COL = 1
DEFAULT_WIDTH = 30
DEFAULT_SECONDS = 4.0
MAX_QUEUE = 6


def char_word(ch: str) -> int:
    """Tilemap word for one character (uppercase A-Z / 0-9 / blank)."""
    if "A" <= ch <= "Z":
        return LETTER_BASE + ord(ch) - ord("A")
    if "0" <= ch <= "9":
        return DIGIT_BASE + ord(ch) - ord("0")
    return SPACE_WORD    # the resident HUD font has no punctuation


def encode_text(text: str, width: int) -> bytes:
    """Little-endian tilemap words for `text`, centred and padded to `width`."""
    text = " ".join(str(text).split()).upper()[:width]
    pad = max(0, width - len(text)) // 2
    padded = " " * pad + text + " " * (width - len(text) - pad)
    out = bytearray()
    for ch in padded:
        word = char_word(ch)
        out += bytes((word & 0xFF, word >> 8))
    return bytes(out)


class HudText:
    """Message strip on the in-game HUD, driven from the agent's poll loop.

    show() queues a message from any thread; tick() (poll thread, only while a
    save is loaded) draws it, re-asserts it if the game's own HUD refresh wiped
    it, and restores the original cells when it expires.
    """

    def __init__(self, transport, row=DEFAULT_ROW, col=DEFAULT_COL,
                 width=DEFAULT_WIDTH, seconds=DEFAULT_SECONDS):
        self.t = transport
        row = max(0, min(HUD_ROWS - 1, int(row)))
        col = max(0, min(HUD_COLS - 1, int(col)))
        self.width = max(1, min(HUD_COLS - col, int(width)))
        self.addr = HUD_BUFFER_ADDR + (row * HUD_COLS + col) * 2
        self.seconds = float(seconds)
        self._lock = threading.Lock()
        self._queue = deque()
        self._current = None      # encoded strip bytes currently on screen
        self._until = 0.0
        self._saved = None        # original strip bytes to restore

    # -- public ------------------------------------------------------------
    def show(self, text: str):
        """Queue a message (thread-safe). Drawn by the next tick()s."""
        with self._lock:
            if self._queue and self._queue[-1] == text:
                return                       # drop immediate duplicates
            if len(self._queue) >= MAX_QUEUE:
                self._queue.popleft()        # oldest news is the least useful
            self._queue.append(text)

    def reset(self):
        """Forget everything without touching memory — the game rebuilt the
        HUD itself (file load / emulator reconnect), so our snapshot is void."""
        with self._lock:
            self._queue.clear()
        self._current = None
        self._saved = None
        self._until = 0.0

    def tick(self, now=None):
        """Advance the strip. Call ONLY while the game is in a playable mode."""
        now = time.time() if now is None else now
        if self._current is not None:
            if now >= self._until:
                self._restore()
            else:
                self._reassert()
        if self._current is None:
            with self._lock:
                text = self._queue.popleft() if self._queue else None
            if text is not None:
                self._draw(encode_text(text, self.width), now)

    # -- internals ----------------------------------------------------------
    def _flag(self):
        self.t.write_memory(HUD_FLAG_ADDR, b"\x01")

    def _draw(self, strip: bytes, now: float):
        saved = self.t.read_memory(self.addr, size=len(strip))
        if not saved or len(saved) < len(strip):
            return                          # transport hiccup — retry next tick
        self._saved = bytes(saved)
        if self.t.write_memory(self.addr, strip):
            self._flag()
            self._current = strip
            self._until = now + self.seconds

    def _reassert(self):
        """If the game's Hud_Rebuild wiped the strip, put the message back."""
        cur = self.t.read_memory(self.addr, size=len(self._current))
        if cur and bytes(cur) != self._current:
            # the game redrew these cells; what's under us now is its fresh
            # content, so THAT becomes what we restore afterwards
            self._saved = bytes(cur)
            if self.t.write_memory(self.addr, self._current):
                self._flag()

    def _restore(self):
        if self._saved is not None and self.t.write_memory(self.addr, self._saved):
            self._flag()
        self._current = None
        self._saved = None
