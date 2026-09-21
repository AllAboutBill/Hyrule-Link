"""The Bombos mark and the Bombos fire, once per open.

web/img/bombos.svg (the logo) and web/img/mark.svg (the tab icon) are drawn
by tools/make_mark.py from web/items/bombos.png, and running it again gives
the same files byte for byte. web/js/intro.js parses, keeps its rules (once a
tab session, never for reduced motion, never on a front page that is only
forwarding a link) and is loaded by index.html and room.html only, never
operator.html. Its stylesheet is one block at the end of hyrulelink.css, and
the ember colours live in that block only. The node parts skip without node;
the make_mark run skips without Pillow.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import PIL  # noqa: F401  (make_mark.py needs it)
except ImportError:
    PIL = None

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
WEB = ROOT / "web"
IMG = WEB / "img"
INTRO = WEB / "js" / "intro.js"
CSS = WEB / "css" / "hyrulelink.css"
TOOL = ROOT / "tools" / "make_mark.py"

SVG = "{http://www.w3.org/2000/svg}"
MEDALLION = {"#f8d535", "#b98d23", "#232823"}     # the game's medallion palette
EMBERS = ("#E07A30", "#A8391F")                   # the ember orange and red: the intro block only
BLOCK = "Bombos, once per open (js/intro.js"


def _sprite(path, box):
    """The SVG's pixels as {(x, y): colour}, checking the file on the way."""
    root = ET.parse(path).getroot()
    assert root.tag == SVG + "svg", root.tag
    assert root.get("viewBox") == f"0 0 {box} {box}", root.get("viewBox")
    assert root.get("shape-rendering") == "crispEdges"
    px = {}
    for r in root.iter(SVG + "rect"):
        if r.get("x") is None:                   # the tab icon's tile
            continue
        x, y, w, h = (int(r.get(k)) for k in ("x", "y", "width", "height"))
        assert h == 1 and w >= 1, (x, y, w, h)
        for i in range(x, x + w):
            assert (i, y) not in px, f"{path.name}: two rects on {i},{y}"
            assert 0 <= i < box and 0 <= y < box, (i, y)
            px[(i, y)] = r.get("fill").lower()
    return px


class MarkTests(unittest.TestCase):
    def test_the_logo_is_the_bombos_sprite_in_the_medallion_palette(self):
        px = _sprite(IMG / "bombos.svg", 16)
        self.assertEqual(set(px.values()), MEDALLION)
        xs = [x for x, _ in px]
        ys = [y for _, y in px]
        self.assertEqual((min(xs), max(xs), min(ys), max(ys)), (0, 15, 0, 15))   # a full 16x16 round

    def test_the_tab_icon_is_the_same_pixels_on_a_tile(self):
        logo = _sprite(IMG / "bombos.svg", 16)
        tile = _sprite(IMG / "mark.svg", 22)
        self.assertEqual(tile, {(x + 3, y + 3): c for (x, y), c in logo.items()})
        root = ET.parse(IMG / "mark.svg").getroot()
        first = next(root.iter(SVG + "rect"))
        self.assertEqual((first.get("width"), first.get("height"), first.get("fill")), ("22", "22", "#0E0E10"))

    @unittest.skipIf(PIL is None, "Pillow is not installed")
    def test_make_mark_draws_the_committed_files_again(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            for out in (a, b):
                done = subprocess.run([sys.executable, str(TOOL), "--out", out], capture_output=True,
                                      text=True, encoding="utf-8", errors="replace", timeout=120, cwd=str(ROOT))
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            for name in ("bombos.svg", "mark.svg"):
                with self.subTest(name):
                    first = (Path(a) / name).read_bytes()
                    self.assertEqual(first, (Path(b) / name).read_bytes())
                    # a Windows checkout may have turned the committed file's LF into CRLF
                    committed = (IMG / name).read_bytes().replace(b"\r\n", b"\n")
                    self.assertEqual(first, committed, f"web/img/{name} is not what tools/make_mark.py draws")


class IntroSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = INTRO.read_text(encoding="utf-8")

    def test_it_keeps_its_rules(self):
        self.assertIn("sessionStorage", self.js)
        self.assertIn("'hyrulelink.intro'", self.js)
        self.assertIn("prefers-reduced-motion: reduce", self.js)
        self.assertIn("pointer-events", CSS.read_text(encoding="utf-8").split(BLOCK, 1)[1])

    def test_it_loads_nothing(self):
        hit = re.search(r"https?://|fetch\(|XMLHttpRequest|new Image\b|\.src\s*=|url\(|import\(", self.js)
        self.assertIsNone(hit, hit and hit.group(0))

    def test_only_the_front_and_room_pages_load_it(self):
        self.assertNotIn("intro.js", (WEB / "operator.html").read_text(encoding="utf-8"))
        for page in sorted(WEB.glob("*.html")):
            text = page.read_text(encoding="utf-8")
            if "js/intro.js" not in text:
                continue
            with self.subTest(page.name):
                self.assertIn(page.name, ("index.html", "room.html"))
                scripts = re.findall(r"<script\b[^>]*\bsrc=[\"']([^\"']+)", text)
                self.assertEqual(scripts[-1], "js/intro.js", "intro.js goes last, after the page's own scripts")

    def test_its_styles_are_one_block_at_the_end(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertEqual(css.count(BLOCK), 1)
        head, block = css.split(BLOCK, 1)
        hit = re.search(r"\.bombos-|@keyframes bombos", head)
        self.assertIsNone(hit, "a Bombos rule outside the intro block")
        self.assertNotIn("/* ---", block, "the intro block is the last section")
        for name in ("--fire-core", "--fire-gold", "--fire-ember", "--fire-red"):
            self.assertIn(name, block)
            self.assertIn(name, self.js)
        for ember in EMBERS + ("224, 122, 48", "168, 57, 31"):
            with self.subTest(ember):
                self.assertNotIn(ember.lower(), head.lower())
                self.assertIn(ember.lower(), block.lower())


# Runs web/js/intro.js against a stand-in page and reports whether it would
# play (it waits for DOMContentLoaded) and what it left in sessionStorage.
_RULES_SCRIPT = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync('web/js/intro.js', 'utf8');
function open(o) {
  const mem = new Map(Object.entries(o.store || {}));
  let plays = false;
  const storage = o.noStorage
    ? { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } }
    : { getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => { mem.set(k, String(v)); } };
  const page = {
    location: { pathname: o.path, search: o.search || '' },
    document: {
      readyState: 'loading', hidden: false,
      body: { classList: { contains: (c) => (o.body || []).indexOf(c) >= 0 } },
      addEventListener: (type) => { if (type === 'DOMContentLoaded') plays = true; },
    },
    sessionStorage: storage,
    matchMedia: (q) => ({ matches: !!o.reduced && /prefers-reduced-motion: reduce/.test(q) }),
    URLSearchParams: URLSearchParams,
  };
  page.window = page;
  vm.runInNewContext(src, page);
  return { plays: plays, key: mem.has('hyrulelink.intro') ? mem.get('hyrulelink.intro') : null };
}
console.log(JSON.stringify({
  front: open({ path: '/' }),
  frontFile: open({ path: '/hyrulelink/index.html' }),
  again: open({ path: '/', store: { 'hyrulelink.intro': '1' } }),
  reduced: open({ path: '/', reduced: true }),
  noStorage: open({ path: '/', noStorage: true }),
  invite: open({ path: '/', search: '?room=ABCDEFGH23' }),
  watch: open({ path: '/hyrulelink/', search: '?watch=Xy_z-12AB' }),
  emptyInvite: open({ path: '/', search: '?room=' }),
  room: open({ path: '/room.html', search: '?room=ABCDEFGH23', body: ['room'] }),
  roomWatch: open({ path: '/hyrulelink/room.html', search: '?watch=Xy_z-12AB', body: ['room'] }),
}));
"""


@unittest.skipIf(NODE is None, "node is not installed")
class IntroNodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        done = subprocess.run([NODE, "-"], input=_RULES_SCRIPT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60, cwd=str(ROOT))
        if done.returncode != 0:
            raise AssertionError(done.stdout + done.stderr)
        cls.out = json.loads(done.stdout.strip().splitlines()[-1])

    def test_it_parses(self):
        done = subprocess.run([NODE, "--check", str(INTRO)], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60, cwd=str(ROOT))
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_it_plays_on_the_first_page_of_a_tab_session(self):
        for case in ("front", "frontFile", "emptyInvite", "room", "roomWatch"):
            with self.subTest(case):
                self.assertEqual(self.out[case], {"plays": True, "key": "1"})

    def test_once_per_open(self):
        self.assertEqual(self.out["again"], {"plays": False, "key": "1"})

    def test_no_storage_no_show(self):
        self.assertFalse(self.out["noStorage"]["plays"])

    def test_never_for_reduced_motion(self):
        self.assertFalse(self.out["reduced"]["plays"])

    def test_a_front_page_that_is_only_forwarding_keeps_the_showing(self):
        for case in ("invite", "watch"):
            with self.subTest(case):
                self.assertEqual(self.out[case], {"plays": False, "key": None})


if __name__ == "__main__":
    unittest.main()
