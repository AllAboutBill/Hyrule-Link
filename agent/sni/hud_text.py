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
docs/08-graphics-tiles-and-gfx.md §8, and verified live in-game (2026-07-05,
Snes9x-NWA, ALTTPR dungeon).

Placement (measured on a live ALTTPR game by stamping test glyphs and watching
what the game rewrote): the item box owns cols 0-7; counters own rows 0-1
center; hearts can own rows 1-2 cols 20+; the game continuously re-stamps
cols 25-26 of rows 3-4 (dungeon key/floor area). Row 4 cols 5-24 is the widest
strip the game never touches — that is the default. Messages longer than the
strip marquee-scroll.

Robustness rules (learned live):
  * NWA drops the reply to a read issued right after a write — reads retry,
    and a failed read/write means "try again next tick", never lost state.
  * The game may rewrite SOME cells under the strip (Hud_Rebuild, hearts).
    Those per-cell changes are merged into the restore snapshot so expiry
    puts back what the game now wants there, not what we saw at draw time.
  * Draw/tick only while a save is loaded (caller gates on PLAYABLE_MODES);
    reset() drops all state after a file load rebuilt the HUD.
"""

import threading
import time
from collections import deque

HUD_BUFFER_ADDR = 0xC700     # $7EC700 hud_tile_indices_buffer (WRAM offset)
HUD_FLAG_ADDR = 0x0016       # $7E0016 flag_update_hud_in_nmi
HUD_COLS = 32
HUD_ROWS = 5                 # buffer rows 0-4 (165 words = 5*32 + 5 spill)

# Glyphs verified against the REAL ALTTPR in-game tile layout (NOT zelda3's
# enhancement font, which mislead an earlier version): z3randomizer repoints
# GFX sheet 0xDC to its own HUD sheet (data/c2807_v4.gfx), loaded at BG3 char
# indices 0x100-0x17F during gameplay. That sheet contains a full uppercase
# font at tiles 0x5D-0x76 ('A'-'Z', followed by digits 0-8) — the map screen's
# HC/L1/AT/D1-D7/GT labels sit right before it. Digits use the classic HUD
# digit tiles 0x90-0x99 (z3randomizer timer.asm renders the challenge timer
# with `!ADD.w #$2490`), and 0x247F is the blank the timer clears with.
# Confirmed live: text written with these words rendered pixel-perfect on a
# real seed (and the earlier wrong font's garbage decoded exactly to this
# layout: "LAMP" -> tiles [D7][HC][GT][C] = 0x5B/0x50/0x5C/0x5F + 0x100).
SPACE_WORD = 0x247F          # blank tile the rando's own timer uses
LETTER_BASE = 0x255D         # 'A' (char 0x15D = HUD sheet 0xDC tile 0x5D)
DIGIT_BASE = 0x2490          # '0' (char 0x90, classic HUD digits)

DEFAULT_ROW = 4              # bottom HUD row — measured free of game writes
DEFAULT_COL = 5              # cols 0-4 belong to the item box frame
DEFAULT_WIDTH = 20           # cols 5-24; cols 25-26 are game-owned (keys/floor)
DEFAULT_SECONDS = 4.0        # static display time (scroll time is added)
SCROLL_HOLD_S = 1.0          # show the head of a long message before scrolling
SCROLL_STEP_S = 0.35         # one 1-char shift per this interval
MAX_QUEUE = 6


def char_word(ch: str) -> int:
    """Tilemap word for one character (uppercase A-Z / 0-9 / blank)."""
    if "A" <= ch <= "Z":
        return LETTER_BASE + ord(ch) - ord("A")
    if "0" <= ch <= "9":
        return DIGIT_BASE + ord(ch) - ord("0")
    return SPACE_WORD    # the resident HUD font has no punctuation


def encode_words(text: str) -> list:
    """Per-character little-endian word bytes for `text` (cleaned, uppercased)."""
    text = " ".join(str(text).split()).upper()
    return [char_word(c).to_bytes(2, "little") for c in text]


def encode_text(text: str, width: int) -> bytes:
    """Static strip: `text` centred and padded to `width`, as LE word bytes."""
    words = encode_words(text)[:width]
    pad = max(0, width - len(words)) // 2
    space = SPACE_WORD.to_bytes(2, "little")
    return b"".join([space] * pad + words +
                    [space] * (width - len(words) - pad))


class HudText:
    """Message strip on the in-game HUD, driven from the agent's poll loop.

    show() queues a message from any thread; tick() (poll thread, only while a
    save is loaded) draws it — statically if it fits, marquee-scrolled if it
    doesn't — re-asserts it over the game's own HUD refreshes, and restores
    the original cells when it expires.
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
        self._msg = None          # word-bytes list of the active message
        self._offset = 0          # marquee scroll position (chars)
        self._next_shift = 0.0
        self._until = 0.0
        self._last_written = None  # strip bytes we last put on screen
        self._saved = None         # strip bytes to restore (per-cell merged)

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
        self._msg = None
        self._last_written = None
        self._saved = None
        self._until = 0.0

    def tick(self, now=None):
        """Advance the strip. Call ONLY while the game is in a playable mode."""
        now = time.time() if now is None else now
        if self._msg is not None:
            self._merge_game_writes()
            if now >= self._until:
                if not self._restore():
                    return                   # write dropped — retry next tick
            else:
                self._advance(now)
                return
        if self._msg is None:
            with self._lock:
                text = self._queue[0] if self._queue else None
            if text is not None and self._start(text, now):
                with self._lock:
                    if self._queue:
                        self._queue.popleft()   # consumed once actually drawn

    # -- internals ----------------------------------------------------------
    def _read_strip(self, tries=4):
        size = self.width * 2
        for _ in range(tries):
            data = self.t.read_memory(self.addr, size=size)
            if data and len(data) >= size:
                return bytes(data[:size])
        return None

    def _flag(self):
        self.t.write_memory(HUD_FLAG_ADDR, b"\x01")

    def _start(self, text: str, now: float) -> bool:
        saved = self._read_strip()
        if saved is None:
            return False                    # transport hiccup — retry next tick
        msg = encode_words(text)
        if len(msg) <= self.width:
            strip = encode_text(text, self.width)
            scroll_time = 0.0
        else:
            strip = b"".join(msg[:self.width])
            scroll_time = SCROLL_HOLD_S + (len(msg) - self.width) * SCROLL_STEP_S
        if not self.t.write_memory(self.addr, strip):
            return False
        self._flag()
        self._saved = saved
        self._msg = msg
        self._offset = 0
        self._next_shift = now + SCROLL_HOLD_S
        self._last_written = strip
        self._until = now + self.seconds + scroll_time
        return True

    def _merge_game_writes(self):
        """Fold cells the GAME changed under us into the restore snapshot —
        per cell, so our own letters never poison what gets restored."""
        cur = self._read_strip(tries=1)
        if cur is None or cur == self._last_written:
            return
        saved = bytearray(self._saved)
        for i in range(0, len(cur), 2):
            if cur[i:i + 2] != self._last_written[i:i + 2]:
                saved[i:i + 2] = cur[i:i + 2]   # the game wants this cell
        self._saved = bytes(saved)

    def _advance(self, now: float):
        """Scroll if due, and re-assert the strip over game HUD refreshes."""
        if len(self._msg) > self.width and now >= self._next_shift \
                and self._offset < len(self._msg) - self.width:
            self._offset += 1
            self._next_shift = now + SCROLL_STEP_S
        window = (encode_text_from_words(self._msg, self.width)
                  if len(self._msg) <= self.width else
                  b"".join(self._msg[self._offset:self._offset + self.width]))
        if self.t.write_memory(self.addr, window):
            self._flag()
            self._last_written = window

    def _restore(self) -> bool:
        """Put the (merged) original cells back; keep state and retry if the
        write is dropped — a lost restore leaves the message stuck on screen."""
        if self._saved is not None:
            if not self.t.write_memory(self.addr, self._saved):
                return False
            self._flag()
        self._msg = None
        self._last_written = None
        self._saved = None
        return True


def encode_text_from_words(words: list, width: int) -> bytes:
    """Centre a fitting word list into a width-sized strip (static messages)."""
    pad = max(0, width - len(words)) // 2
    space = SPACE_WORD.to_bytes(2, "little")
    return b"".join([space] * pad + list(words) +
                    [space] * (width - len(words) - pad))
