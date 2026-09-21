"""Put the /hyrulelink/ location into the www.billogna.lol server block.

    ssh droplet 'python3 - --dry-run' < deploy/nginx_install.py   # show what it would do
    ssh droplet 'python3 -' < deploy/nginx_install.py             # install; idempotent
    python deploy/nginx_install.py --dry-run <copy of the conf>    # try it on a copy

Runs ON the droplet as root. Does nothing if `location ^~ /hyrulelink/` is
already in the file. Changes nothing unless the file defines the
`$race_connection_upgrade` map the block uses and certbot's
`listen [::]:443 ssl ipv6only=on` line is there exactly once, inside the
www.billogna.lol server block. Otherwise it backs the file up to
<conf>.bak.<epoch>, inserts the block just above that line, runs `nginx -t`,
restores the backup if the test fails, and reloads nginx (never restarts).
`^~` keeps the site's static-file regex locations off /hyrulelink/js and
friends. hyrulelink.conf (the subdomain) is not touched.
"""
import argparse
import re
import shutil
import subprocess
import sys
import time

CONF = "/etc/nginx/sites-available/billogna-sites.conf"
ANCHOR = "    listen [::]:443 ssl ipv6only=on; # managed by Certbot"
MARKER = "location ^~ /hyrulelink/"
MAP_RE = re.compile(r"^\s*map\s+\S+\s+\$race_connection_upgrade\s*\{", re.M)
SERVER_NAME_RE = re.compile(r"^\s*server_name\s+([^;]*);", re.M)
BLOCK = """\
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

""".replace("\r", "")

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


def insertion(text):
    """(new text, line number of the anchor) or raise ValueError with the reason."""
    if not MAP_RE.search(text):
        raise ValueError("no `map ... $race_connection_upgrade` in the file; the block needs it")
    n = text.count(ANCHOR)
    if n != 1:
        raise ValueError("anchor line found %d times, expected 1: %r" % (n, ANCHOR.strip()))
    at = text.index(ANCHOR)
    if at and text[at - 1] != "\n":
        raise ValueError("anchor is not at the start of a line")
    names = SERVER_NAME_RE.findall(text, 0, at)
    if not names or "www.billogna.lol" not in names[-1].split():
        raise ValueError("anchor is not inside the www.billogna.lol server block")
    return text[:at] + BLOCK + text[at:], text.count("\n", 0, at) + 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Add location ^~ /hyrulelink/ to the www.billogna.lol block.")
    ap.add_argument("conf", nargs="?", default=CONF, help="nginx file (default %(default)s)")
    ap.add_argument("--dry-run", action="store_true", help="print what would change; write nothing")
    a = ap.parse_args(argv)

    with open(a.conf, encoding="utf-8", newline="") as f:
        text = f.read()
    if MARKER in text:
        print("already installed in %s; nothing changed" % a.conf)
        return 0
    try:
        new, line = insertion(text)
    except ValueError as e:
        print("refusing: %s; nothing changed" % e)
        return 1

    if a.dry_run:
        print("dry run: would insert %d lines above line %d of %s:" % (BLOCK.count("\n"), line, a.conf))
        for ln in BLOCK.splitlines():
            print("+" + ln)
        print(" " + ANCHOR)
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
    print("installed above line %d and reloaded; backup: %s" % (line, backup))
    return 0


if __name__ == "__main__":
    sys.exit(main())
