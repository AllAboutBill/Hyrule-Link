"""The browser's pages and JavaScript: every file parses, the DOM-free units
pass under node, nothing uses a leading-slash URL (the site is served under a
path prefix as well as at a root), nothing is loaded from the old billogna.lol
chrome or a font CDN, every page carries the billogna.lol bar, and the old
aurora UI is gone. Skips the node parts when node is not installed.

room.html is written by another package; its checks run once it exists.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
WEB = ROOT / "web"
JS_DIR = WEB / "js"
CSS_DIR = WEB / "css"
UNITS = ROOT / "tests" / "web" / "units.js"

# href="/x", src='/x', action="/x", fetch("/x"), new URL("/x"): all of them
# escape a path prefix. Protocol-relative "//host" is left alone.
LEADING_SLASH = re.compile(
    r"""(?:href|src|action)=["']/(?!/)|fetch\(\s*["']/|new URL\(\s*["']/""")
# the same in a stylesheet: url(/x), url("/x"), url('/x')
CSS_ROOT_URL = re.compile(r"""url\(\s*["']?/(?!/)""")

# The game core must not touch the page or open sockets of its own:
# snes.js is the only file that builds a WebSocket.
CORE = ("items.js", "effects.js", "agent.js", "hud.js")

# The pages people open, each with the billogna.lol bar on top.
PAGES = ("index.html", "room.html", "operator.html")
# Written by this package; room.html comes from the room package.
OWN_PAGES = ("index.html", "operator.html")

# The aurora UI: the one-page app and its billogna.lol chrome.
RETIRED = ("app.js", "style.css", "nexus-bg.js", "pixel-canvas.js", "pixel-hover.js")

# Anything a page loads from next to itself must be there.
LOCAL_ASSET = re.compile(r"""(?:src|href)=["']([^"'#?]+\.(?:css|js|svg|png|ico|woff2|ttf|jpg))["']""")

SITEBAR = re.compile(r"""<nav\b[^>]*\bclass=["']sitebar["'][^>]*>.*?</nav>""", re.S)


def _run(*args, timeout=120):
    return subprocess.run([NODE, *map(str, args)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, cwd=str(ROOT))


def _pages():
    return sorted(WEB.glob("*.html"))


def _scripts():
    return sorted(WEB.glob("*.js")) + sorted(JS_DIR.glob("*.js"))


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
        files = _pages() + _scripts()
        self.assertTrue(files)
        for path in files:
            with self.subTest(path.name):
                hit = LEADING_SLASH.search(path.read_text(encoding="utf-8"))
                self.assertIsNone(hit, hit and hit.group(0))

    def test_stylesheets_use_no_root_urls(self):
        files = sorted(CSS_DIR.glob("*.css"))
        self.assertTrue(files, "no web/css/*.css")
        for path in files:
            with self.subTest(path.name):
                hit = CSS_ROOT_URL.search(path.read_text(encoding="utf-8"))
                self.assertIsNone(hit, hit and hit.group(0))

    def test_pages_load_nothing_from_the_old_chrome_or_a_font_cdn(self):
        """/shared/ was an nginx alias that only the subdomain had, /static/
        breaks under a path prefix, and the fonts are bundled."""
        for path in _pages():
            text = path.read_text(encoding="utf-8")
            for needle in ("googleapis", "/shared/", "/static/"):
                with self.subTest(page=path.name, needle=needle):
                    self.assertNotIn(needle, text)

    def test_what_the_pages_load_is_there(self):
        for path in _pages():
            for ref in LOCAL_ASSET.findall(path.read_text(encoding="utf-8")):
                if re.match(r"(?:[a-z]+:|//)", ref):
                    continue
                with self.subTest(page=path.name, ref=ref):
                    self.assertTrue((WEB / ref).is_file(), f"{path.name} loads {ref}, which is not in web/")

    def test_the_billogna_bar_is_on_every_page(self):
        for name in PAGES:
            path = WEB / name
            if not path.exists() and name not in OWN_PAGES:
                continue            # room.html: checked once it lands
            with self.subTest(name):
                page = path.read_text(encoding="utf-8")
                bar = SITEBAR.search(page)
                self.assertIsNotNone(bar, f"{name} has no sitebar")
                for href in ("https://www.billogna.lol/", "https://www.billogna.lol/ethernet/",
                             "https://www.billogna.lol/connect/"):
                    self.assertIn(f'href="{href}"', bar.group(0))

    def test_the_bar_is_the_first_thing_on_the_front_and_operator_pages(self):
        for name in OWN_PAGES:
            page = (WEB / name).read_text(encoding="utf-8")
            with self.subTest(name):
                self.assertRegex(page, r"<body[^>]*>\s*<nav\b[^>]*\bclass=\"sitebar\"")
                bar = SITEBAR.search(page).group(0)
                self.assertNotIn("_blank", bar)       # the room page opens them in a new tab; these do not
        self.assertIn('aria-current="page"', SITEBAR.search((WEB / "index.html").read_text(encoding="utf-8")).group(0))

    def test_the_aurora_ui_is_gone(self):
        for name in RETIRED:
            with self.subTest(name):
                self.assertFalse((WEB / name).exists(), f"web/{name} should be gone")

    def test_the_front_page_forwards_links_before_it_draws(self):
        """home.js loads in <head>, so index.html?room= and ?watch= (the
        desktop app opens {base}/?watch=PUB) go on to room.html without the
        front page flashing up first."""
        page = (WEB / "index.html").read_text(encoding="utf-8")
        head = page[:page.index("</head>")]
        self.assertIn('<script src="js/home.js"></script>', head)
        self.assertIn("css/hyrulelink.css", head)
        self.assertIn('<script src="js/operator.js"></script>', (WEB / "operator.html").read_text(encoding="utf-8"))

    def test_the_game_core_has_no_dom_and_no_socket(self):
        for name in CORE:
            path = JS_DIR / name
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(name):
                self.assertNotRegex(text, r"\bdocument\.|new WebSocket\b|\blocalStorage\b")


# Drives the DOM-free half of web/js/home.js under node and prints what it saw.
_HOME_SCRIPT = r"""
const H = require('./web/js/home.js');
function mem(init) {
  const m = new Map(Object.entries(init || {}));
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => { m.set(k, String(v)); },
           dump: () => Object.fromEntries(m) };
}
const out = {};
out.redirect = ['?room=ABCDEFGH23', '?watch=Xy_z-12AB', 'watch=abc&x=1', '?room=abcdefgh23&x=1',
                '', '?room=', '?watch=%20', '?foo=1'].map(H.redirectFor);
out.code = [H.codeFrom('https://www.billogna.lol/hyrulelink/room.html?room=abcdefgh23'),
            H.codeFrom(' abcd-efgh 23 '), H.codeFrom('')];

// the old page's seats move over once, with the old name on them
const a = mem({ hl_name: 'Ana', hl_rooms: JSON.stringify({
  abcdefgh23: { player_id: 3, player_token: 'tok3' },
  'NOPE!': { player_id: 4, player_token: 'tok4' },
  QRSTUVWX45: { player_id: 0, player_token: 'tok0' },
  ZZZZZZZZ22: { player_id: 5 } }) });
const sa = H.makeStore(a);
H.importLegacy(sa);
out.imported = a.dump();
out.name = H.readName(sa);
// a seat forgotten after the import stays forgotten
H.forgetSeat(sa, 'ABCDEFGH23');
H.importLegacy(sa);
out.afterForget = H.readSeats(sa);

// the new keys win over the old ones
const b = mem({ 'hyrulelink.name': JSON.stringify('Bo'), hl_name: 'Ana',
                'hyrulelink.rooms': JSON.stringify({ KEEPKEEP22: { player_id: 1, player_token: 'k', t: 5 } }),
                hl_rooms: JSON.stringify({ OLDOLDOL22: { player_id: 2, player_token: 'o' } }) });
const sb = H.makeStore(b);
H.importLegacy(sb);
out.kept = { name: H.readName(sb), rooms: Object.keys(H.readSeats(sb)) };

// the old page's stand-in names are not names
const c = mem({ hl_name: 'Spectator' });
H.importLegacy(H.makeStore(c));
out.standIn = c.dump();

// a name stored as plain text still reads
out.plain = H.readName(H.makeStore(mem({ 'hyrulelink.name': 'Cy' })));

// storage that throws on every call (a locked-down private window)
const broken = { getItem() { throw new Error('no'); }, setItem() { throw new Error('no'); } };
const sx = H.makeStore(broken);
H.importLegacy(sx);
H.saveSeat(sx, 'ABCDEFGH23', { player_id: 1, player_token: 't' });
out.broken = { name: H.readName(sx), seats: H.readSeats(sx) };

// seats come back newest first; a seat without a token is not one
const d = mem();
const sd = H.makeStore(d);
H.saveSeat(sd, 'OLDER00022', { player_id: 1, player_token: 'a', name: 'Ana', title: 'One' }, 100);
H.saveSeat(sd, 'NEWER00022', { player_id: 2, player_token: 'b', name: 'Ana', title: 'Two' }, 200);
H.retitleSeat(sd, 'OLDER00022', 'One renamed');
const raw = H.readSeats(sd); raw.NOTOKEN022 = { player_id: 3, t: 300 };
d.setItem('hyrulelink.rooms', JSON.stringify(raw));
out.list = H.seatList(sd).map((s) => [s.code, s.seat.title]);
out.ago = [H.ago(10), H.ago(600), H.ago(7200), H.ago(3 * 86400)];
out.roster = [H.roster([{ name: 'Ana' }, { name: 'Bo' }]),
              H.roster([1, 2, 3, 4, 5, 6, 7].map((i) => ({ name: 'P' + i })))];
console.log(JSON.stringify(out));
"""


@unittest.skipIf(NODE is None, "node is not installed")
class HomeLogicTests(unittest.TestCase):
    """web/js/home.js: the ?room= / ?watch= forward and the seat store,
    including the one-time move of the old page's hl_name / hl_rooms."""

    @classmethod
    def setUpClass(cls):
        # on stdin, not -e: no command-line quoting to get wrong on Windows
        done = subprocess.run([NODE, "-"], input=_HOME_SCRIPT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60, cwd=str(ROOT))
        if done.returncode != 0:
            raise AssertionError(done.stdout + done.stderr)
        cls.out = json.loads(done.stdout.strip().splitlines()[-1])

    def test_room_and_watch_links_go_on_to_the_room_page_with_the_same_query(self):
        self.assertEqual(self.out["redirect"], [
            "room.html?room=ABCDEFGH23", "room.html?watch=Xy_z-12AB", "room.html?watch=abc&x=1",
            "room.html?room=abcdefgh23&x=1", None, None, None, None])

    def test_the_code_door_takes_a_code_or_a_whole_invite_link(self):
        self.assertEqual(self.out["code"], ["ABCDEFGH23", "ABCDEFGH23", ""])

    def test_old_seats_move_over_once(self):
        got = self.out["imported"]
        self.assertEqual(json.loads(got["hyrulelink.name"]), "Ana")
        self.assertEqual(json.loads(got["hyrulelink.rooms"]), {
            "ABCDEFGH23": {"player_id": 3, "player_token": "tok3", "name": "Ana", "title": "", "t": 0}})
        self.assertIn("hl_rooms", got)          # left for a rollback to the old page
        self.assertEqual(self.out["name"], "Ana")
        self.assertEqual(self.out["afterForget"], {})

    def test_the_new_keys_win(self):
        self.assertEqual(self.out["kept"], {"name": "Bo", "rooms": ["KEEPKEEP22"]})

    def test_stand_in_names_are_not_imported(self):
        self.assertNotIn("hyrulelink.name", self.out["standIn"])

    def test_a_plain_text_name_still_reads(self):
        self.assertEqual(self.out["plain"], "Cy")

    def test_storage_that_throws_is_survived(self):
        self.assertEqual(self.out["broken"], {"name": "", "seats": {}})

    def test_seats_list_newest_first(self):
        self.assertEqual(self.out["list"], [["NEWER00022", "Two"], ["OLDER00022", "One renamed"]])

    def test_times_and_rosters_read_plainly(self):
        self.assertEqual(self.out["ago"], ["active now", "10 min ago", "2 h ago", "3 d ago"])
        self.assertEqual(self.out["roster"], ["Ana, Bo", "P1, P2, P3, P4 and 3 more"])


if __name__ == "__main__":
    unittest.main()
