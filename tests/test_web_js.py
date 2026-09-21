"""The browser's JavaScript: every file parses, the DOM-free units pass under
node, and nothing uses a leading-slash URL (the site is served under a path
prefix as well as at a root). Skips the node parts when node is not installed.
"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
JS_DIR = ROOT / "web" / "js"
UNITS = ROOT / "tests" / "web" / "units.js"

# href="/x", src='/x', action="/x", fetch("/x"), new URL("/x"): all of them
# escape a path prefix. Protocol-relative "//host" is left alone.
LEADING_SLASH = re.compile(
    r"""(?:href|src|action)=["']/(?!/)|fetch\(\s*["']/|new URL\(\s*["']/""")

# The game core must not touch the page or open sockets of its own:
# snes.js is the only file that builds a WebSocket.
CORE = ("items.js", "effects.js", "agent.js", "hud.js")


def _run(*args, timeout=120):
    return subprocess.run([NODE, *map(str, args)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, cwd=str(ROOT))


@unittest.skipIf(NODE is None, "node is not installed")
class NodeTests(unittest.TestCase):
    def test_every_file_parses(self):
        files = sorted(JS_DIR.glob("*.js"))
        self.assertTrue(files, "no web/js/*.js")
        for path in files:
            with self.subTest(path.name):
                done = _run("--check", path)
                self.assertEqual(done.returncode, 0, done.stderr)

    def test_units(self):
        done = _run(UNITS)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        tally = re.search(r"^(\d+) / (\d+) passed$", done.stdout, re.M)
        self.assertIsNotNone(tally, done.stdout)
        self.assertEqual(tally.group(1), tally.group(2), done.stdout)
        self.assertGreaterEqual(int(tally.group(1)), 30)


class SourceTests(unittest.TestCase):
    def test_no_leading_slash_urls(self):
        for path in sorted(JS_DIR.glob("*.js")):
            with self.subTest(path.name):
                hit = LEADING_SLASH.search(path.read_text(encoding="utf-8"))
                self.assertIsNone(hit, hit and hit.group(0))

    def test_the_game_core_has_no_dom_and_no_socket(self):
        for name in CORE:
            path = JS_DIR / name
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(name):
                self.assertNotRegex(text, r"\bdocument\.|new WebSocket\b|\blocalStorage\b")


if __name__ == "__main__":
    unittest.main()
