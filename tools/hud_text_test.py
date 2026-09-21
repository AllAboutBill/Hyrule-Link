"""
hud_text_test.py — write a test message onto the in-game HUD, live.

Run with your emulator open and a save loaded:

    python -m tools.hud_text_test "HELLO FROM BOMBOSSWAP"
    python -m tools.hud_text_test --row 4 --col 1 --width 30 "LAMP STOLEN FROM BILL"

The message shows for ~6 seconds, then the original HUD cells are restored.
Use it to confirm (or adjust) the strip placement on your setup — the defaults
target HUD row 4 (the bottom, blank row of the strip). Works over RetroArch
and Snes9x-NWA directly; for SNI/hardware run the desktop app instead.
"""

import argparse
import sys
import time

sys.path.insert(0, ".")

from agent.sni.emu_connector import EmuConnector
from agent.sni.hud_text import HudText
from agent.sni.memory_constants import MEMORY_ADDRESSES, PLAYABLE_MODES


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("text", nargs="?", default="HELLO FROM BOMBOSSWAP")
    ap.add_argument("--row", type=int, default=4)
    ap.add_argument("--col", type=int, default=5)
    ap.add_argument("--width", type=int, default=20)
    ap.add_argument("--seconds", type=float, default=6.0)
    args = ap.parse_args()

    t = EmuConnector()
    if not t.connect():
        sys.exit("No emulator found (RetroArch UDP 55355 / Snes9x-NWA). "
                 "Start one with a game loaded and try again.")
    print(f"Connected via {t._active}")

    gm = t.read_memory(MEMORY_ADDRESSES["game_mode"], size=1)
    if not gm or int(gm[0]) not in PLAYABLE_MODES:
        sys.exit(f"Game mode 0x{int(gm[0]) if gm else 0:02X} is not playable — "
                 "load a save and stand in the world first.")

    hud = HudText(t, row=args.row, col=args.col, width=args.width,
                  seconds=args.seconds)
    hud.show(args.text)
    print(f"Drawing {args.text!r} at row {args.row}, cols "
          f"{args.col}-{args.col + hud.width - 1} for {args.seconds:.0f}s…")
    end = time.time() + args.seconds + 1.0
    while time.time() < end:
        hud.tick()
        time.sleep(0.25)
    print("Restored. If the text didn't appear (or covered HUD art), "
          "try --row/--col/--width and tell the agent the winning values.")


if __name__ == "__main__":
    main()
