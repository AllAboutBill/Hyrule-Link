"""Put BombosSwap's /bombosswap/ location into the www.billogna.lol server
block, and turn the old /hyrulelink/ one into a redirect to it.

    ssh droplet 'python3 - --dry-run' < deploy/nginx_install.py   # show the diff it would apply
    ssh droplet 'python3 -' < deploy/nginx_install.py             # install; idempotent
    python deploy/nginx_install.py --dry-run <copy of the conf>    # try it on a copy

Runs ON the droplet as root. It starts from one of three states:

- nothing installed: the block goes in just above certbot's
  `listen [::]:443 ssl ipv6only=on` line;
- the HyruleLink block this script used to install (the comment line
  `# ---- HyruleLink co-op (...) ----`, `location = /hyrulelink` and the
  `location ^~ /hyrulelink/` proxy): the block takes its place;
- the block already there: nothing changed.

The block proxies /bombosswap/ to 127.0.0.1:5019 with the directives the old
/hyrulelink/ proxy had, and answers /hyrulelink and /hyrulelink/<path>?<query>
with a 301 to the same path and query under /bombosswap/, so links from
before the rename keep working. hyrulelink.conf (hyrulelink.billogna.lol, the
subdomain the desktop app uses) is not touched.

Changes nothing unless the file defines the `$race_connection_upgrade` map
the block uses and the certbot line is there exactly once, inside the
www.billogna.lol server block (an old block it replaces must be in that same
server block). Otherwise it backs the file up to <conf>.bak.<epoch>, writes
it, runs `nginx -t`, restores the backup if the test fails, and reloads nginx
(never restarts). `^~` keeps the site's static-file regex locations off
/bombosswap/js and friends, and off /hyrulelink/ too, so old asset URLs get
the redirect rather than a 404.
"""
import argparse
import difflib
import re
import shutil
import subprocess
import sys
import time

CONF = "/etc/nginx/sites-available/billogna-sites.conf"
ANCHOR = "    listen [::]:443 ssl ipv6only=on; # managed by Certbot"
MARKER = "location ^~ /bombosswap/"
MAP_RE = re.compile(r"^\s*map\s+\S+\s+\$race_connection_upgrade\s*\{", re.M)
SERVER_NAME_RE = re.compile(r"^\s*server_name\s+([^;]*);", re.M)
SERVER_START_RE = re.compile(r"^\s*server\s*\{", re.M)
BLOCK = """\
    # ---- BombosSwap co-op, formerly HyruleLink (F:\\HyruleLink, hyrulelink.service, 127.0.0.1:5019; also hyrulelink.billogna.lol) ----
    location = /bombosswap { return 301 /bombosswap/; }
    location ^~ /bombosswap/ {
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
    # the address before the rename (2026-09-21): same path and query under /bombosswap/
    location = /hyrulelink { return 301 /bombosswap/; }
    location ^~ /hyrulelink/ { rewrite ^/hyrulelink/(.*)$ /bombosswap/$1 permanent; }

""".replace("\r", "")
# What this script installed before the rename: its comment line, the bare
# redirect, and the proxy (a block with no braces inside it), then the blank
# line it left above the anchor. Whitespace is loose, the shape is not.
OLD_RE = re.compile(
    r"^[ \t]*# ---- HyruleLink co-op \([^\n]*\) ----[ \t]*\n"
    r"[ \t]*location = /hyrulelink \{[ \t]*return 301 /hyrulelink/;[ \t]*\}[ \t]*\n"
    r"[ \t]*location \^~ /hyrulelink/ \{[ \t]*\n[^{}]*?\n[ \t]*\}[ \t]*\n"
    r"(?:[ \t]*\n)?",
    re.M)
# Any /hyrulelink or /bombosswap location at all (hand-made ones included).
ANY_OURS_RE = re.compile(r"^[ \t]*location\s+(?:=|\^~|~\*?)?\s*\^?/(?:hyrulelink|bombosswap)\b", re.M)

# Swappable so the install path can be exercised off the droplet.
NGINX_TEST = ["nginx", "-t"]
NGINX_RELOAD = ["systemctl", "reload", "nginx"]


def run(cmd):
    """(returncode, output); a missing program counts as a failure."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        return 127, "%s: %s" % (cmd[0], e)
    return p.returncode, (p.stdout + p.stderr).strip()


def line_of(text, at):
    return text.count("\n", 0, at) + 1


def in_www_block(text, at):
    """The last server_name before `at` is the www.billogna.lol block's."""
    names = SERVER_NAME_RE.findall(text, 0, at)
    return bool(names) and "www.billogna.lol" in names[-1].split()


def plan(text):
    """(new text, what it does in words); new text None means already there.
    Raises ValueError with the reason when it must not touch the file."""
    olds = list(OLD_RE.finditer(text))
    if MARKER in text:
        if olds:
            raise ValueError("the /bombosswap/ block and the old /hyrulelink/ proxy are both in the file; "
                             "take one out by hand")
        return None, "already installed"
    if not MAP_RE.search(text):
        raise ValueError("no `map ... $race_connection_upgrade` in the file; the block needs it")
    n = text.count(ANCHOR)
    if n != 1:
        raise ValueError("anchor line found %d times, expected 1: %r" % (n, ANCHOR.strip()))
    at = text.index(ANCHOR)
    if at and text[at - 1] != "\n":
        raise ValueError("anchor is not at the start of a line")
    if not in_www_block(text, at):
        raise ValueError("anchor is not inside the www.billogna.lol server block")

    if len(olds) > 1:
        raise ValueError("the old /hyrulelink/ block is in the file %d times" % len(olds))
    if olds:
        m = olds[0]
        if len(ANY_OURS_RE.findall(text)) != 2:
            raise ValueError("a /hyrulelink or /bombosswap location besides the old block; change it by hand")
        if not (m.end() <= at and in_www_block(text, m.start())
                and not SERVER_START_RE.search(text, m.end(), at)):
            raise ValueError("the old /hyrulelink/ block is not in the www.billogna.lol block above the anchor")
        first, last = line_of(text, m.start()), line_of(text, m.end()) - 1
        return (text[:m.start()] + BLOCK + text[m.end():],
                "replaced the old /hyrulelink/ proxy (lines %d-%d)" % (first, last))
    if ANY_OURS_RE.search(text):
        raise ValueError("a /hyrulelink or /bombosswap location that this script did not install; "
                         "change it by hand")
    return text[:at] + BLOCK + text[at:], "inserted above line %d" % line_of(text, at)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Install location ^~ /bombosswap/ in the www.billogna.lol block "
                                             "and redirect /hyrulelink/ to it.")
    ap.add_argument("conf", nargs="?", default=CONF, help="nginx file (default %(default)s)")
    ap.add_argument("--dry-run", action="store_true", help="print the diff it would apply; write nothing")
    a = ap.parse_args(argv)

    with open(a.conf, encoding="utf-8", newline="") as f:
        text = f.read()
    try:
        new, what = plan(text)
    except ValueError as e:
        print("refusing: %s; nothing changed" % e)
        return 1
    if new is None:
        print("already installed in %s; nothing changed" % a.conf)
        return 0

    if a.dry_run:
        print("dry run: would have %s of %s:" % (what, a.conf))
        sys.stdout.writelines(difflib.unified_diff(
            text.splitlines(True), new.splitlines(True), a.conf, a.conf + " (after)", n=2))
        print("then `%s` and `%s`; nothing written" % (" ".join(NGINX_TEST), " ".join(NGINX_RELOAD)))
        return 0

    backup = "%s.bak.%d" % (a.conf, int(time.time()))
    shutil.copy2(a.conf, backup)
    with open(a.conf, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    rc, out = run(NGINX_TEST)
    if rc != 0:
        shutil.copy2(backup, a.conf)
        print("nginx -t failed, restored the backup (%s):\n%s" % (backup, out[-600:]))
        return 1
    rc, out = run(NGINX_RELOAD)
    if rc != 0:
        shutil.copy2(backup, a.conf)
        print("reload failed, restored the backup (%s); nginx keeps its running config:\n%s"
              % (backup, out[-600:]))
        return 1
    print("%s and reloaded; backup: %s" % (what, backup))
    return 0


if __name__ == "__main__":
    sys.exit(main())
