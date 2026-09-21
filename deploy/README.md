# BombosSwap on the droplet

https://www.billogna.lol/bombosswap/ and https://hyrulelink.billogna.lol are one
service. The desktop app points at the subdomain. https://www.billogna.lol/hyrulelink/,
the address before the rename, answers with a 301 to the same path and query under
`/bombosswap/`, so invite links from before still work.

BombosSwap was called HyruleLink until 2026-09-21. Everything on the droplet kept
that name: the service, `/opt/hyrulelink`, its files, the log, the subdomain and
its nginx file. Only the www path changed.

| | |
|---|---|
| app | `/opt/hyrulelink`, a git checkout of `master`. `hyrulelink.service` (this folder), user `root`, `127.0.0.1:5019`, `/opt/hyrulelink/.venv` (Python 3.10), `MemoryMax=300M` |
| state | `.env`, `server/hyrulelink.db` (+ `-wal`, `-shm`), `server/operator.key`. All git-ignored and beside the code, so a fast-forward never touches them and the deploy never names them. |
| nginx, subdomain | `/etc/nginx/sites-available/hyrulelink.conf` proxies `/` to `:5019`. Not managed from here. Its `location /shared/` alias served the old aurora chrome and is unused now; remove it by hand some day. |
| nginx, www prefix | `location ^~ /bombosswap/` in the www.billogna.lol block of `/etc/nginx/sites-available/billogna-sites.conf`, put there by `nginx_install.py`, with `location ^~ /hyrulelink/` beside it as a 301 to `/bombosswap/`. The prefix is stripped, which is why no page uses a leading-slash URL. `^~` keeps the site's static-file regex locations off `/bombosswap/js` and friends (and off `/hyrulelink/js`, which gets the redirect). |
| deploy markers | `.deploy-prev` (the commit before the last deploy), `.deploy-prev.service` (the unit it replaced), `.deploy-pending` (a deploy that has not finished). Git-ignored, in `/opt/hyrulelink`. |
| deploy log | `/var/log/hyrulelink-deploy.log` on the droplet, one block per deploy or rollback |

## Commands

Run from the repo root in Git Bash. `HL_SSH_HOST` overrides the host (default `droplet`).

```bash
bash deploy/deploy.sh --check      # unit, health, HEAD vs origin/master, working tree; changes nothing
bash deploy/deploy.sh              # ship origin/master: push first
bash deploy/deploy.sh --rollback   # back to .deploy-prev, unit included

ssh droplet 'python3 - --dry-run' < deploy/nginx_install.py   # the diff the nginx step would apply
ssh droplet 'python3 -' < deploy/nginx_install.py             # once per nginx change; idempotent
ssh droplet 'systemctl stop hyrulelink'                       # the off switch
```

What a deploy does, in one ssh session on the droplet:

1. Refuses if `/opt/hyrulelink` is not on `master` or has local modifications to
   tracked files. Untracked files are ignored.
2. `git fetch`; if HEAD already is `origin/master`, says so and stops (no restart).
3. Writes `.deploy-prev` and `.deploy-prev.service`, fast-forwards to `origin/master`.
4. `pip install -r requirements.txt` only if `requirements.txt` changed.
5. Installs `deploy/hyrulelink.service` if it differs from the live unit, printing
   the old one first. Any `Environment=` or `EnvironmentFile=` line the live unit has
   and the repo unit lacks is kept (right under `[Service]`); when both set the same
   variable, the repo wins.
6. Restarts and polls `/api/health` for about 30 s. On any failure from step 3 on,
   it puts the old commit and unit back (and the old requirements, if they
   changed), restarts and checks again.

Every deploy restarts the server, so open pages and agents drop and reconnect within
a few seconds. A deploy with nothing new does not restart.

ssh is retried (three tries, 10 s apart) only when ssh itself fails. A refusal or a
failed deploy is not retried. The droplet side is parsed whole before it runs, holds
a lock, and writes through `tee -p` to the log, so a dropped connection does not stop
it halfway. If one did stop halfway, `--check` shows `PENDING` and the next deploy
finishes it.

`--rollback` goes back one deploy. To go further back, on the droplet:
`cd /opt/hyrulelink && git reset --hard <commit> && systemctl restart hyrulelink`.

`/root/deploy-hyrulelink.sh` (the old pull-and-restart helper) still works, but
this script supersedes it: it also installs the unit, checks health and rolls back.

## The rename (once)

Going from HyruleLink at `/hyrulelink/` to BombosSwap at `/bombosswap/`, in this
order. The code first, so that `/bombosswap/` serves the renamed pages the moment
nginx sends people there; the subdomain and the desktop app never notice.

```bash
git push origin master                                        # the droplet pulls from GitHub
bash deploy/deploy.sh --check                                 # tree: clean; nginx: only the old /hyrulelink/ proxy
bash deploy/deploy.sh                                         # new code; the unit is reinstalled (new Description)
ssh droplet 'python3 - --dry-run' < deploy/nginx_install.py   # read the diff: the old block out, the new one in
ssh droplet 'python3 -' < deploy/nginx_install.py             # nginx -t, then reload; prints the backup's name
bash deploy/deploy.sh --check                                 # nginx: /bombosswap/ ...; /hyrulelink/ redirects to it
```

Then the checks under Verify. Pages left open at `/hyrulelink/` keep working until
their socket drops; the reconnect gets the 301 and fails, and a reload lands on
`/bombosswap/`. Seats survive: both paths are on www.billogna.lol, so they share the
browser's storage. The undo is the nginx backup (see nginx below); the code needs no
undo, since it serves the subdomain and either path the same way.

## First deploy (once, done 2026-09-21)

Kept for the record. Before the first `deploy.sh`, `/opt/hyrulelink` was at
`b17491a` with six files copied in by hand from `4e5e7db` (`server/app.py`, `server/db.py`,
`server/ledger.py`, `shared/protocol.py`, `web/index.html`, `web/style.css`) and an
untracked `web.bak-pre-aurora/`. `deploy.sh` refuses to run over those, and on
purpose it does not fix them itself. Push `master` first (it contains `4e5e7db`).

Look (this changes nothing but a `git fetch`). Every file line must show two
equal hashes:

```bash
ssh droplet 'bash -s' <<'EOF'
cd /opt/hyrulelink && git fetch -q origin && git rev-parse --short HEAD && /opt/hyrulelink/.venv/bin/python --version
git cat-file -e '4e5e7db^{commit}' && echo "4e5e7db is here"
for f in server/app.py server/db.py server/ledger.py shared/protocol.py web/index.html web/style.css; do
  echo "$f $(git show 4e5e7db:$f | md5sum | cut -c1-32) $(md5sum < $f | cut -c1-32)"
done
git status --short
grep -rn "bak-pre-aurora" /etc/nginx/ || echo "web.bak-pre-aurora: not referenced by nginx"
EOF
```

Then reconcile: back up the database and `.env`, drop the hand copies, remove the
old web backup, and fast-forward to `4e5e7db`. Since the six files are identical to
`4e5e7db`, the running server's code does not change. The fast-forward means the
first deploy's `.deploy-prev` is the code that is live now, not the older
`b17491a`, so `--rollback` returns to it. It also means the first deploy sees no
`requirements.txt` change and skips pip.

```bash
ssh droplet 'bash -s' <<'EOF'
set -e
cd /opt/hyrulelink
git fetch -q origin
TS=$(date +%s)
python3 -c "import sqlite3; s=sqlite3.connect('server/hyrulelink.db'); d=sqlite3.connect('/root/hyrulelink.db.bak-$TS'); s.backup(d); d.close()"
cp -p .env /root/hyrulelink.env.bak-$TS
for f in server/app.py server/db.py server/ledger.py shared/protocol.py web/index.html web/style.css; do
  [ "$(git show 4e5e7db:$f | md5sum)" = "$(md5sum < $f)" ] || { echo "$f differs from 4e5e7db; stopping"; exit 1; }
done
if grep -rq "bak-pre-aurora" /etc/nginx/; then echo "nginx still references web.bak-pre-aurora; stopping"; exit 1; fi
git checkout -- server/app.py server/db.py server/ledger.py shared/protocol.py web/index.html web/style.css
rm -rf web.bak-pre-aurora
git merge -q --ff-only 4e5e7db
git log -1 --format='HEAD %h %s'; git status --short; ls -la /root/hyrulelink.*bak-$TS
EOF
```

`git status --short` must print nothing. Then `bash deploy/deploy.sh --check`
shows `tree: clean`, and `bash deploy/deploy.sh` ships. The first deploy replaces
the unit (new description, `MemoryMax=300M`); the old unit is printed in the output
and kept in `.deploy-prev.service`.

## The operator page

`operator.html` lists every room with who is in it and deletes any of them. Its API
answers 404 until `server/operator.key` holds a key. The deploy never touches that
file. To use the same key as QuakeCast's operator page:

```bash
ssh droplet 'python3 -c "import json;open(\"/opt/hyrulelink/server/operator.key\",\"w\").write(json.load(open(\"/opt/raceconnect/config.json\"))[\"operator_key\"])" && chmod 600 /opt/hyrulelink/server/operator.key && systemctl restart hyrulelink && sleep 3 && curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5019/api/operator/rooms'
```

The key is read once at start, hence the restart. `403` means the key is set, `404`
means it is not. `HYRULELINK_OPERATOR_KEY_FILE` (in `.env` or the unit) points
somewhere else if you ever want that.

## nginx

`nginx_install.py` runs on the droplet. Its block is the `/bombosswap/` proxy (the
directives the `/hyrulelink/` proxy had) and the two `/hyrulelink` redirects:

```nginx
location = /hyrulelink { return 301 /bombosswap/; }
location ^~ /hyrulelink/ { rewrite ^/hyrulelink/(.*)$ /bombosswap/$1 permanent; }
```

`rewrite` keeps the query string, so `/hyrulelink/room.html?room=CODE` lands on
`/bombosswap/room.html?room=CODE`. It starts from any of three states: nothing
installed (the block goes in above certbot's line), the HyruleLink block it used to
install (`# ---- HyruleLink co-op (...) ----` and the `/hyrulelink/` proxy; the block
takes its place), or the block already there (nothing changed). A `/hyrulelink` or
`/bombosswap` location in any other shape, edited by hand, makes it refuse.

It changes nothing unless the file defines the `$race_connection_upgrade` map and
certbot's `listen [::]:443 ssl ipv6only=on; # managed by Certbot` line is there
exactly once, inside the www.billogna.lol block. It backs the file up to
`billogna-sites.conf.bak.<epoch>`, writes it, runs `nginx -t`, restores the backup
if the test fails, and reloads (never restarts). Run the `--dry-run` form first: it
prints the diff. `tests/test_nginx_install.py` runs all three states and the
refusals against a stand-in file, with nginx and systemctl stubbed. To try it on a
copy of the live file (one ssh, read only):

```bash
ssh droplet cat /etc/nginx/sites-available/billogna-sites.conf > /tmp/billogna-sites.conf
.venv/Scripts/python.exe deploy/nginx_install.py --dry-run /tmp/billogna-sites.conf
```

Undo:

```bash
ssh droplet 'cp /etc/nginx/sites-available/billogna-sites.conf.bak.<epoch> /etc/nginx/sites-available/billogna-sites.conf && nginx -t && systemctl reload nginx'
```

## Verify

```bash
curl -s https://hyrulelink.billogna.lol/api/health
curl -s https://www.billogna.lol/bombosswap/api/health
curl -sI https://www.billogna.lol/bombosswap | head -1                      # 301 -> /bombosswap/
curl -sI https://www.billogna.lol/bombosswap/js/items.js | head -1          # 200
curl -s https://www.billogna.lol/bombosswap/ | grep -o '<title>[^<]*'      # BombosSwap - ...
curl -s -o /dev/null -w "%{http_code}\n" https://www.billogna.lol/bombosswap/api/operator/rooms   # 403
curl -sI "https://www.billogna.lol/hyrulelink/room.html?room=ABCDEFGH23" | grep -i -E '^(HTTP|location)'
#   301, location: https://www.billogna.lol/bombosswap/room.html?room=ABCDEFGH23
curl -sI https://www.billogna.lol/hyrulelink | grep -i -E '^(HTTP|location)'   # 301 -> /bombosswap/
curl -s https://www.billogna.lol/ethernet/api/health && curl -s https://www.billogna.lol/connect/health
ssh droplet 'journalctl -u hyrulelink -n 20 --no-pager'
```

## ServiceHub

`servicehub.toml` here is the block for `F:\ServiceHub\services.toml`. Paste it in
by hand.

## Things that will bite

- ufw rate-limits ssh: 6 new connections in 30 s and the next ones time out.
  `deploy.sh` uses one session per run. Do not loop it, and batch anything by hand
  into one `ssh droplet 'bash -s' <<'EOF' ... EOF`.
- The droplet pulls from GitHub (`git@github.com:AllAboutBill/Hyrule-Link.git`),
  not from this PC. An unpushed commit is not deployed; `--check` shows what
  GitHub has.
- Never edit files in `/opt/hyrulelink` by hand. The next deploy refuses until they
  are gone (`git checkout -- <file>`), and `--rollback` refuses too, because
  `git reset --hard` would lose them.
- `requirements.txt` also lists the desktop app's packages (pyz3r, Pillow,
  maseya-z3pr). When it changes, the droplet installs them too. That is harmless but
  slow on a 1-vCPU box.
- The server stays on Python 3.10 syntax: that is what the droplet's venv runs.
- The unit runs as root because the sqlite file and its `-wal`/`-shm` live in a
  root-owned checkout. Moving to `www-data` means
  `chown -R www-data:www-data /opt/hyrulelink/server` and a `safe.directory` entry
  for git, as a separate change.
- `nginx -t` before every reload, and reload, never restart. Do not touch EtherNet,
  QuakeCast (`/connect/`), the race hub or MediaMTX from here.
- `deploy.sh` must stay LF (`.gitattributes` sees to it); a CR breaks bash on the
  droplet.
