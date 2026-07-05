import unittest

from agent.agent import GAME_MODE_ADDR, HyruleAgent
from agent.sni.hud_text import (
    HUD_BUFFER_ADDR, HUD_FLAG_ADDR, HUD_COLS,
    LETTER_BASE, DIGIT_BASE, SPACE_WORD, SCROLL_HOLD_S, SCROLL_STEP_S,
    HudText, char_word, encode_text, encode_words,
)
from tests.test_effects import MemoryTransport, Socket


def words(t, addr, n):
    """Read back n little-endian words from the fake transport."""
    raw = t.read_memory(addr, size=n * 2)
    return [raw[i * 2] | (raw[i * 2 + 1] << 8) for i in range(n)]


class EncodingTests(unittest.TestCase):
    """Glyph mapping = the game's own hud.c `L(x)` macro (zelda3)."""

    def test_char_word_matches_the_games_font_layout(self):
        # letters: rando HUD sheet 0xDC font at chars 0x15D+ (verified live)
        self.assertEqual(char_word("A"), 0x255D)
        self.assertEqual(char_word("Z"), 0x255D + 25)
        # digits: classic HUD digit tiles (z3randomizer timer.asm, +$2490)
        self.assertEqual(char_word("0"), 0x2490)
        self.assertEqual(char_word("9"), 0x2499)
        self.assertEqual(char_word(" "), 0x247F)
        self.assertEqual(char_word("-"), 0x247F)   # no punctuation in the font

    def test_encode_uppercases_centres_and_pads(self):
        strip = encode_text("hi", 6)               # -> "  HI  "
        self.assertEqual(len(strip), 12)
        w = [strip[i * 2] | (strip[i * 2 + 1] << 8) for i in range(6)]
        self.assertEqual(w, [SPACE_WORD, SPACE_WORD,
                             LETTER_BASE + 7, LETTER_BASE + 8,
                             SPACE_WORD, SPACE_WORD])

    def test_encode_truncates_to_width(self):
        strip = encode_text("ABCDEFGH", 4)
        self.assertEqual(len(strip), 8)


class HudTextTests(unittest.TestCase):
    def hud(self, row=4, col=1, width=10, seconds=4.0):
        t = MemoryTransport()
        return t, HudText(t, row=row, col=col, width=width, seconds=seconds)

    def test_draw_writes_strip_and_sets_nmi_flag(self):
        t, hud = self.hud(width=21)
        hud.show("LAMP STOLEN FROM BILL")                     # fits exactly
        hud.tick(now=100.0)
        self.assertEqual(t.memory[HUD_FLAG_ADDR], 1)          # $7E0016 set
        expected = encode_text("LAMP STOLEN FROM BILL", 21)
        self.assertEqual(bytes(t.read_memory(hud.addr, size=42)), expected)
        # strip sits inside HUD row 4
        row4 = HUD_BUFFER_ADDR + 4 * HUD_COLS * 2
        self.assertTrue(row4 <= hud.addr < row4 + HUD_COLS * 2)

    def test_long_message_marquee_scrolls(self):
        t, hud = self.hud(width=4, seconds=4.0)
        hud.show("ABCDEF")                                    # 6 chars in a 4 strip
        hud.tick(now=100.0)
        self.assertEqual(bytes(t.read_memory(hud.addr, size=8)),
                         b"".join(encode_words("ABCD")))      # head first
        hud.tick(now=100.0 + SCROLL_HOLD_S)                   # first shift due
        self.assertEqual(bytes(t.read_memory(hud.addr, size=8)),
                         b"".join(encode_words("BCDE")))
        hud.tick(now=100.0 + SCROLL_HOLD_S + SCROLL_STEP_S)
        self.assertEqual(bytes(t.read_memory(hud.addr, size=8)),
                         b"".join(encode_words("CDEF")))      # tail reached
        hud.tick(now=100.0 + SCROLL_HOLD_S + 5 * SCROLL_STEP_S)
        self.assertEqual(bytes(t.read_memory(hud.addr, size=8)),
                         b"".join(encode_words("CDEF")))      # holds the tail

    def test_partial_game_write_is_merged_into_restore(self):
        t, hud = self.hud(width=4, seconds=2.0)
        t.write_memory(hud.addr, bytes([0x7F, 0x20] * 4))     # original blanks
        hud.show("ABCD")
        hud.tick(now=100.0)
        # the game re-stamps ONLY cell 2 (e.g. key counter area)
        t.write_memory(hud.addr + 4, bytes([0x7F, 0x00]))
        hud.tick(now=101.0)                                   # merge + reassert
        self.assertEqual(bytes(t.read_memory(hud.addr, size=8)),
                         b"".join(encode_words("ABCD")))      # message back
        hud.tick(now=103.0)                                   # expiry
        self.assertEqual(words(t, hud.addr, 4),
                         [0x207F, 0x207F, 0x007F, 0x207F])    # game's cell kept

    def test_expiry_restores_the_original_cells(self):
        t, hud = self.hud(width=4, seconds=2.0)
        t.write_memory(hud.addr, bytes([0x7F, 0x20] * 4))     # game blank tiles
        t.memory[HUD_FLAG_ADDR] = 0
        hud.show("HI")
        hud.tick(now=100.0)
        self.assertNotEqual(words(t, hud.addr, 4), [0x207F] * 4)
        hud.tick(now=103.0)                                   # past 100+2s
        self.assertEqual(words(t, hud.addr, 4), [0x207F] * 4)  # restored
        self.assertEqual(t.memory[HUD_FLAG_ADDR], 1)           # re-uploaded

    def test_game_hud_rebuild_is_detected_and_message_redrawn(self):
        t, hud = self.hud(width=4, seconds=10.0)
        hud.show("HI")
        hud.tick(now=100.0)
        wiped = bytes([0x7F, 0x20] * 4)
        t.write_memory(hud.addr, wiped)                       # Hud_Rebuild wiped us
        hud.tick(now=101.0)
        self.assertEqual(bytes(t.read_memory(hud.addr, size=8)),
                         encode_text("HI", 4))                # message back
        hud.tick(now=111.0)                                   # expiry
        self.assertEqual(bytes(t.read_memory(hud.addr, size=8)),
                         wiped)                               # restores the REBUILT cells

    def test_queued_messages_play_back_to_back(self):
        t, hud = self.hud(width=6, seconds=2.0)
        hud.show("ONE")
        hud.show("TWO")
        hud.tick(now=100.0)
        self.assertEqual(bytes(t.read_memory(hud.addr, size=12)), encode_text("ONE", 6))
        hud.tick(now=103.0)                                   # ONE expires, TWO draws
        self.assertEqual(bytes(t.read_memory(hud.addr, size=12)), encode_text("TWO", 6))

    def test_reset_forgets_state_without_writing(self):
        t, hud = self.hud(width=4)
        hud.show("HI")
        hud.tick(now=100.0)
        drawn = bytes(t.read_memory(hud.addr, size=8))
        hud.reset()                                           # file reload etc.
        hud.tick(now=200.0)
        self.assertEqual(bytes(t.read_memory(hud.addr, size=8)), drawn)  # untouched


class AgentHudIntegrationTests(unittest.TestCase):
    def test_notification_is_drawn_on_the_hud_by_the_poll_loop(self):
        t = MemoryTransport()
        t.memory[GAME_MODE_ADDR] = 0x07
        agent = HyruleAgent(t, "ws://test", "ROOM", 1, "token")
        agent.ws = Socket()
        agent._ws_ready.set()
        agent._show_notification("Lamp stolen from Bill")
        agent._poll_once()
        self.assertEqual(t.memory.get(HUD_FLAG_ADDR), 1)
        strip = bytes(t.read_memory(agent.hud.addr, size=agent.hud.width * 2))
        # 21 chars in a 20-wide strip -> marquee starts at the head
        head = b"".join(encode_words("Lamp stolen from Bill")[:agent.hud.width])
        self.assertEqual(strip, head)

    def test_hud_is_not_touched_outside_playable_modes(self):
        t = MemoryTransport()
        t.memory[GAME_MODE_ADDR] = 0x01                       # file select
        agent = HyruleAgent(t, "ws://test", "ROOM", 1, "token")
        agent.ws = Socket()
        agent._ws_ready.set()
        agent._show_notification("Lamp stolen from Bill")
        agent._poll_once()
        self.assertNotEqual(t.memory.get(HUD_FLAG_ADDR), 1)   # nothing drawn


if __name__ == "__main__":
    unittest.main()
