"""deploy/nginx_install.py on a stand-in for billogna-sites.conf, with nginx
and systemctl stubbed: the three starting states (nothing, the old
/hyrulelink/ proxy, the new block), the refusals, and the backup going back
when `nginx -t` or the reload fails. Never touches a real nginx file."""
import contextlib
import glob
import importlib.util
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("nginx_install", ROOT / "deploy" / "nginx_install.py")
ni = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ni)

OK = [sys.executable, "-c", "print('stub ok')"]
FAIL = [sys.executable, "-c", "import sys; print('stub failed'); sys.exit(1)"]

# What the installer put in before the rename: the live state on 2026-09-21.
OLD_BLOCK = """\
    # ---- HyruleLink co-op (F:\\HyruleLink, hyrulelink.service, 127.0.0.1:5019; also hyrulelink.billogna.lol) ----
    location = /hyrulelink { return 301 /hyrulelink/; }
    location ^~ /hyrulelink/ {
        proxy_pass http://127.0.0.1:5019/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $race_connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
        client_max_body_size 16k;
    }

"""

CONF = """\
map $http_upgrade $race_connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    server_name billogna.lol www.billogna.lol;
    location ~* \\.(png|css|js)$ { expires 7d; }

    # ---- EtherNet race rooms (F:\\EtherNet, ethernet.service, 127.0.0.1:5028) ----
    location = /ethernet { return 301 /ethernet/; }
    location ^~ /ethernet/ {
        proxy_pass http://127.0.0.1:5028/;
    }

%s    listen [::]:443 ssl ipv6only=on; # managed by Certbot
    listen 443 ssl; # managed by Certbot
}

server {
    server_name classroom.billogna.lol;
    listen [::]:443 ssl; # managed by Certbot
}
"""
NOTHING, OLD = CONF % "", CONF % OLD_BLOCK


def _body(text, loc):
    """The directives inside `location ^~ <loc> { ... }`."""
    return re.search(r"location \^~ %s \{\n(.*?)\n\s*\}" % re.escape(loc), text, re.S).group(1)


class NginxInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.test, self.reload = ni.NGINX_TEST, ni.NGINX_RELOAD
        ni.NGINX_TEST, ni.NGINX_RELOAD = OK, OK
        self.addCleanup(setattr, ni, "NGINX_TEST", self.test)
        self.addCleanup(setattr, ni, "NGINX_RELOAD", self.reload)

    def conf(self, text):
        path = os.path.join(self.tmp.name, "billogna-sites.conf")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return path

    def run_it(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ni.main(list(args))
        return rc, out.getvalue()

    @staticmethod
    def read(path):
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()

    def test_from_the_old_proxy_to_the_new_block(self):
        path = self.conf(OLD)
        rc, out = self.run_it(path)
        self.assertEqual(rc, 0, out)
        self.assertIn("replaced the old /hyrulelink/ proxy", out)
        new = self.read(path)
        self.assertEqual(new, CONF % ni.BLOCK)
        self.assertEqual(_body(new, "/bombosswap/"), _body(OLD, "/hyrulelink/"))   # the same directives
        self.assertIn("location = /bombosswap { return 301 /bombosswap/; }", new)
        self.assertIn("location = /hyrulelink { return 301 /bombosswap/; }", new)
        self.assertIn("location ^~ /hyrulelink/ { rewrite ^/hyrulelink/(.*)$ /bombosswap/$1 permanent; }", new)
        self.assertEqual(new.count("proxy_pass http://127.0.0.1:5019/;"), 1)      # /hyrulelink/ proxies no more
        self.assertNotIn("HyruleLink co-op (", new)
        backups = glob.glob(path + ".bak.*")
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.read(backups[0]), OLD)

    def test_a_fresh_install_ends_the_same(self):
        path = self.conf(NOTHING)
        rc, out = self.run_it(path)
        self.assertEqual(rc, 0, out)
        self.assertIn("inserted above line", out)
        self.assertEqual(self.read(path), CONF % ni.BLOCK)

    def test_again_changes_nothing(self):
        for start in (NOTHING, OLD):
            with self.subTest(start is OLD and "old" or "nothing"):
                path = self.conf(start)
                for old in glob.glob(path + ".bak.*"):
                    os.remove(old)
                self.assertEqual(self.run_it(path)[0], 0)
                done = self.read(path)
                rc, out = self.run_it(path)
                self.assertEqual((rc, self.read(path)), (0, done))
                self.assertIn("already installed", out)
                self.assertEqual(len(glob.glob(path + ".bak.*")), 1)

    def test_a_dry_run_shows_the_diff_and_writes_nothing(self):
        path = self.conf(OLD)
        rc, out = self.run_it("--dry-run", path)
        self.assertEqual(rc, 0, out)
        self.assertIn("-    location ^~ /hyrulelink/ {", out)
        self.assertIn("+    location ^~ /bombosswap/ {", out)
        self.assertIn("nothing written", out)
        self.assertEqual(self.read(path), OLD)
        self.assertEqual(glob.glob(path + ".bak.*"), [])

    def test_a_failed_test_or_reload_puts_the_file_back(self):
        for which in ("NGINX_TEST", "NGINX_RELOAD"):
            with self.subTest(which):
                setattr(ni, which, FAIL)
                path = self.conf(OLD)
                rc, out = self.run_it(path)
                setattr(ni, which, OK)
                self.assertEqual(rc, 1, out)
                self.assertIn("restored the backup", out)
                self.assertEqual(self.read(path), OLD)

    def test_it_refuses_and_changes_nothing(self):
        cases = {
            "no map": OLD.replace("$race_connection_upgrade {", "$other_upgrade {"),
            "anchor outside the www block": NOTHING.replace(
                "    listen [::]:443 ssl ipv6only=on; # managed by Certbot\n", "").replace(
                "    listen [::]:443 ssl; # managed by Certbot", ni.ANCHOR),
            "anchor twice": NOTHING.replace("    listen 443 ssl; # managed by Certbot",
                                            ni.ANCHOR + "\n    listen 443 ssl; # managed by Certbot"),
            "an old block edited by hand": OLD.replace("client_max_body_size 16k;",
                                                       "client_max_body_size 16k;\n        if ($x) { return 403; }"),
            "a hand-made /hyrulelink location": NOTHING.replace(
                "    listen [::]:443", "    location /hyrulelink/ { proxy_pass http://127.0.0.1:5019/; }\n"
                "    listen [::]:443"),
            "both blocks": CONF % (ni.BLOCK + OLD_BLOCK),
        }
        for name, text in cases.items():
            with self.subTest(name):
                path = self.conf(text)
                rc, out = self.run_it(path)
                self.assertEqual(rc, 1, out)
                self.assertIn("refusing", out)
                self.assertEqual(self.read(path), text)
                self.assertEqual(glob.glob(path + ".bak.*"), [])


if __name__ == "__main__":
    unittest.main()
