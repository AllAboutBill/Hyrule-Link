#!/usr/bin/env bash
# =============================================================================
#  BombosSwap - deploy to the droplet.               bash deploy/deploy.sh
# =============================================================================
#  The service, its folder and its files kept the name from before the
#  rename (hyrulelink). /opt/hyrulelink is a git checkout of master and the
#  droplet pulls from GitHub, so push master first. ONE ssh session does:
#  refuse on local modifications -> fetch -> fast-forward to origin/master ->
#  pip (only if requirements.txt changed) -> unit (only if it changed;
#  Environment= lines the live unit has and the repo unit lacks are kept) ->
#  restart -> /api/health -> on any failure, roll code and unit back and
#  restart again.
#
#  NEVER touches .env, server/hyrulelink.db*, server/operator.key, nginx or
#  any other service: git leaves ignored files alone and nothing here names
#  them. The first-time reconcile of the hand-copied files is a manual step in
#  deploy/README.md, not part of this script.
#
#    bash deploy/deploy.sh              ship origin/master
#    bash deploy/deploy.sh --check      unit, health, HEAD vs origin/master,
#                                       working tree; changes nothing
#    bash deploy/deploy.sh --rollback   back to the commit before the last
#                                       deploy (.deploy-prev), unit included
#
#  HL_SSH_HOST picks the host (default: droplet). ssh is retried only when
#  ssh itself fails (rc 255): ufw rate-limits port 22 and this box times out
#  now and then. The droplet side is parsed whole before it runs, holds a
#  lock, and logs to /var/log/hyrulelink-deploy.log, so a dropped connection
#  does not stop it halfway; a rerun finishes one that did stop
#  (.deploy-pending).
# =============================================================================
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${HL_SSH_HOST:-droplet}"
MODE="${1:-ship}"

case "$MODE" in
  ship|--check|--rollback) ;;
  -h|--help) sed -n '3,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "usage: bash deploy/deploy.sh [--check | --rollback]" >&2; exit 2 ;;
esac

# ---- droplet side: shared by every mode (only definitions; nothing runs) ----
IFS= read -r -d '' COMMON <<'EOF'
APP=/opt/hyrulelink
SVC=hyrulelink
UNIT=/etc/systemd/system/hyrulelink.service
REPO_UNIT=deploy/hyrulelink.service
PORT=5019
LOG=/var/log/hyrulelink-deploy.log
export GIT_TERMINAL_PROMPT=0

say() { printf '%s\n' "$*"; }
setup() { cd "$APP" || { say "no $APP on this host"; exit 1; }; }

# One deploy at a time. A retried ssh waits here for the first attempt to end.
lock() {
  exec 9>/run/lock/hyrulelink-deploy.lock
  flock -w 300 9 || { say "another deploy is still running; nothing changed"; exit 1; }
}

# Output to ssh AND the log; tee -p keeps the log going if ssh drops.
log_to_file() { trap '' HUP; exec > >(tee -p -a "$LOG") 2>&1; }

dirty() { git --no-optional-locks status --porcelain --untracked-files=no; }

api_health() { curl -fsS -m 3 "http://127.0.0.1:$PORT/api/health"; }
# /api/health from this build on; /health also answers on older code.
any_health() {
  local out
  if out=$(api_health 2>/dev/null); then printf '%s' "$out"; return 0; fi
  if out=$(curl -fsS -m 3 "http://127.0.0.1:$PORT/health" 2>/dev/null); then
    printf '%s (only /health: older code)' "$out"; return 0
  fi
  return 1
}
# Poll for up to ~30 s: uvicorn on this 1-vCPU box takes a few seconds.
wait_health() {
  local i out
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    sleep 2
    if out=$("$@" 2>/dev/null); then printf '%s' "$out"; return 0; fi
  done
  "$@" >/dev/null   # once more, to show why
  return 1
}

# The repo unit plus the live unit's Environment= / EnvironmentFile= lines that
# the repo unit lacks (an Environment= line counts as present when the repo
# unit sets the same variable). Kept lines go right under [Service].
merge_unit() {
  if [ ! -s "$UNIT" ]; then cat "$REPO_UNIT"; return; fi
  awk '
    function envname(s) { sub(/^[ \t]*Environment=[ \t]*"?/, "", s); sub(/=.*/, "", s); return s }
    FNR == 1 { pass++ }
    pass == 1 {
      if ($0 ~ /^[ \t]*Environment=/) have[envname($0)] = 1
      else if ($0 ~ /^[ \t]*EnvironmentFile=/) have[$0] = 1
      next
    }
    pass == 2 {
      if ($0 ~ /^[ \t]*Environment=/) { if (!(envname($0) in have)) keep[++n] = $0 }
      else if ($0 ~ /^[ \t]*EnvironmentFile=/) { if (!($0 in have)) keep[++n] = $0 }
      next
    }
    { print; if ($0 ~ /^[ \t]*\[Service\]/) for (i = 1; i <= n; i++) print keep[i] }
  ' "$REPO_UNIT" "$UNIT" "$REPO_UNIT"
}

install_unit() {
  local merged live kept
  if [ ! -f "$REPO_UNIT" ]; then say "   no $REPO_UNIT in this commit; unit left alone"; return 0; fi
  merged=$(merge_unit) || return 1
  live=$(cat "$UNIT" 2>/dev/null)
  if [ "$merged" = "$live" ]; then say "   unit unchanged"; return 0; fi
  say "   unit changed; the live unit was:"
  printf '%s\n' "$live" | sed 's/^/     | /'
  kept=$(printf '%s\n' "$merged" | grep -vxF -f "$REPO_UNIT")
  if [ -n "$kept" ]; then say "   kept from the live unit:"; printf '%s\n' "$kept" | sed 's/^/     + /'; fi
  printf '%s\n' "$merged" > "$UNIT" || return 1
  systemctl daemon-reload || return 1
  systemctl is-enabled -q "$SVC" 2>/dev/null || systemctl enable -q "$SVC"
  say "   unit installed"
}

# Put commit $1 back: code, the unit saved by the last deploy, pip if
# requirements.txt differs, restart, health (either endpoint).
restore() {
  local to=$1 req out
  req=$(git diff --name-only "$to" HEAD -- requirements.txt)
  git reset -q --hard "$to" || { say "!! git reset --hard $to failed"; return 1; }
  if [ -n "$req" ]; then
    say "   requirements.txt differs - pip install"
    .venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt \
      || say "!! pip install failed"
  fi
  if [ -s .deploy-prev.service ] && ! cmp -s .deploy-prev.service "$UNIT"; then
    cp .deploy-prev.service "$UNIT" && systemctl daemon-reload && say "   unit restored"
  fi
  systemctl restart "$SVC"
  if out=$(wait_health any_health); then say "   health: $out"; return 0; fi
  say "!! $SVC is not answering after the rollback"
  journalctl -u "$SVC" -n 15 --no-pager | cut -c1-200
  return 1
}
EOF

# ---- this PC ----
retry_ssh() {  # stdin: the droplet-side entry point, appended to COMMON
  local script rc i
  script="$COMMON"$'\n'"$(cat)"
  for i in 1 2 3; do
    printf '%s\n' "$script" | ssh -o ConnectTimeout=20 -o BatchMode=yes \
      -o ServerAliveInterval=15 -o ServerAliveCountMax=4 "$HOST" 'bash -s'
    rc=$?
    [ $rc -ne 255 ] && return $rc
    echo "  -- ssh to $HOST failed (attempt $i of 3)" >&2
    [ $i -lt 3 ] && sleep 10
  done
  return 255
}

local_refs() {  # what this PC thinks it pushed; no network
  local m o
  m=$(git -C "$REPO" rev-parse --short master 2>/dev/null) || return 0
  o=$(git -C "$REPO" rev-parse --short origin/master 2>/dev/null) || o="?"
  if [ "$m" = "$o" ]; then
    echo "this PC:  master $m = origin/master (as last pushed or fetched here)"
  else
    echo "this PC:  master $m, origin/master $o (differ: push master if the change is yours)"
  fi
}

if [ "$MODE" = "--check" ]; then
  echo "BombosSwap on $HOST:/opt/hyrulelink (read-only)"
  local_refs
  retry_ssh <<'EOF'
check() {
  setup
  local head gh behind ahead d u mem
  mem=$(systemctl show $SVC -p MemoryCurrent --value 2>/dev/null)
  case "$mem" in ''|*[!0-9]*) mem="?" ;; *) mem="$((mem / 1048576)) MB" ;; esac
  say "unit:     $(systemctl is-active $SVC) ($(systemctl is-enabled $SVC 2>/dev/null)), since $(systemctl show $SVC -p ActiveEnterTimestamp --value), memory $mem"
  say "health:   $(any_health || echo 'NOT ANSWERING')"
  head=$(git rev-parse HEAD)
  say "HEAD:     $(git log -1 --format='%h %cs %s' HEAD) (branch $(git symbolic-ref -q --short HEAD || echo 'detached'))"
  gh=$(timeout 20 git ls-remote origin refs/heads/master 2>/dev/null | cut -f1)
  if [ -z "$gh" ]; then
    say "origin:   master unknown (git ls-remote failed)"
  elif [ "$gh" = "$head" ]; then
    say "origin:   master = HEAD (nothing to ship)"
  elif git cat-file -e "$gh^{commit}" 2>/dev/null; then
    behind=$(git rev-list --count "HEAD..$gh"); ahead=$(git rev-list --count "$gh..HEAD")
    say "origin:   master ${gh:0:7}; HEAD is $behind behind, $ahead ahead"
  else
    say "origin:   master ${gh:0:7}, not fetched here yet; HEAD is behind (a deploy fetches it)"
  fi
  d=$(dirty)
  if [ -z "$d" ]; then say "tree:     clean"
  else say "tree:     LOCAL MODIFICATIONS (a deploy refuses until they are gone; see deploy/README.md):"; printf '%s\n' "$d" | sed 's/^/          /'; fi
  u=$(git --no-optional-locks status --porcelain --untracked-files=normal | grep '^??')
  [ -n "$u" ] && { say "untracked (a deploy ignores these):"; printf '%s\n' "$u" | sed 's/^/          /'; }
  if [ -s .deploy-prev ]; then say "rollback: .deploy-prev = $(git log -1 --format='%h %s' "$(cat .deploy-prev)" 2>/dev/null || cat .deploy-prev)"
  else say "rollback: no .deploy-prev yet (no deploy.sh run here)"; fi
  [ -f .deploy-pending ] && say "PENDING:  the last deploy did not finish; the next run finishes it"
  if [ ! -f "$REPO_UNIT" ]; then say "unit file: $REPO_UNIT not in this checkout yet"
  elif [ "$(merge_unit)" = "$(cat "$UNIT" 2>/dev/null)" ]; then say "unit file: matches $REPO_UNIT"
  else say "unit file: differs from $REPO_UNIT (the next deploy installs it)"; fi
  if [ -s server/operator.key ]; then say "operator: server/operator.key present, mode $(stat -c %a server/operator.key)"
  else say "operator: no server/operator.key (operator routes answer 404)"; fi
  say "          /api/operator/rooms answers $(curl -s -o /dev/null -m 3 -w '%{http_code}' http://127.0.0.1:$PORT/api/operator/rooms) (404 off, 403 key set)"
  local conf=/etc/nginx/sites-available/billogna-sites.conf
  if grep -qF 'location ^~ /bombosswap/' "$conf" 2>/dev/null; then
    if grep -qF 'rewrite ^/hyrulelink/(.*)$ /bombosswap/$1 permanent;' "$conf"
    then say "nginx:    /bombosswap/ is in billogna-sites.conf; /hyrulelink/ redirects to it"
    else say "nginx:    /bombosswap/ is in billogna-sites.conf, but /hyrulelink/ does not redirect to it"; fi
  elif grep -qF 'location ^~ /hyrulelink/' "$conf" 2>/dev/null
  then say "nginx:    only the old /hyrulelink/ proxy; /bombosswap/ not installed yet (deploy/nginx_install.py)"
  else say "nginx:    /bombosswap/ not installed yet (deploy/nginx_install.py)"; fi
  say "state:    $(ls -l .env server/hyrulelink.db* 2>/dev/null | awk '{printf "%s %s  ", $NF, $5}')"
}
check </dev/null
EOF
  exit $?
fi

if [ "$MODE" = "--rollback" ]; then
  echo "BombosSwap on $HOST:/opt/hyrulelink: rolling back to .deploy-prev"
  retry_ssh <<'EOF'
rollback() {
  setup; lock; log_to_file
  say "---- $(date -u '+%F %T') rollback"
  local prev
  [ -s .deploy-prev ] || { say "no .deploy-prev; nothing to roll back to"; exit 1; }
  prev=$(cat .deploy-prev)
  git cat-file -e "$prev^{commit}" 2>/dev/null || { say ".deploy-prev ($prev) is not a commit here"; exit 1; }
  if [ "$(git rev-parse HEAD)" = "$prev" ]; then
    rm -f .deploy-pending
    say "already at $(git log -1 --format='%h %s' HEAD) (.deploy-prev); nothing to roll back"
    say "health: $(any_health || echo 'NOT ANSWERING')"
    exit 0
  fi
  if [ -n "$(dirty)" ]; then
    say "local modifications in $APP; refusing (git reset --hard would lose them):"; dirty; exit 1
  fi
  say "rolling back $(git log -1 --format='%h' HEAD) -> $(git log -1 --format='%h %s' "$prev")"
  if restore "$prev"; then
    rm -f .deploy-pending
    say "ROLLED BACK to ${prev:0:7}"
  else
    rm -f .deploy-pending
    say "!! rolled back to ${prev:0:7}, but the service is not answering"
    exit 1
  fi
}
rollback </dev/null
EOF
  exit $?
fi

echo "BombosSwap -> $HOST:/opt/hyrulelink (ships origin/master from GitHub)"
local_refs
retry_ssh <<'EOF'
fail_ship() {
  say "!! $1; rolling back to ${PREV:0:7}"
  journalctl -u "$SVC" -n 15 --no-pager | cut -c1-200
  if restore "$PREV"; then
    say "!! DEPLOY FAILED; rolled back to ${PREV:0:7}, which is answering"
  else
    say "!! DEPLOY FAILED; rolled back to ${PREV:0:7}, and that is not answering either"
  fi
  rm -f .deploy-pending
  exit 1
}
ship() {
  setup; lock; log_to_file
  say "---- $(date -u '+%F %T') deploy"
  local branch d target out
  branch=$(git symbolic-ref -q --short HEAD)
  [ "$branch" = master ] || { say "$APP is on '${branch:-a detached HEAD}', not master; refusing"; exit 1; }
  d=$(dirty)
  if [ -n "$d" ]; then
    say "local modifications in $APP; refusing (first deploy? see deploy/README.md, first-time reconcile):"
    printf '%s\n' "$d"; exit 1
  fi
  timeout 120 git fetch -q origin || { say "git fetch failed; nothing changed"; exit 1; }
  target=$(git rev-parse origin/master)
  if [ -f .deploy-pending ]; then
    [ -s .deploy-prev ] || { say ".deploy-pending without .deploy-prev; fix by hand"; exit 1; }
    PREV=$(cat .deploy-prev)
    say "the last deploy did not finish; finishing it (rollback target ${PREV:0:7})"
  elif [ "$(git rev-parse HEAD)" = "$target" ]; then
    say "already at $(git log -1 --format='%h %s' HEAD); nothing to ship"
    say "health: $(any_health || echo 'NOT ANSWERING')"
    exit 0
  else
    git merge-base --is-ancestor HEAD "$target" \
      || { say "origin/master is not a fast-forward of HEAD; refusing"; exit 1; }
    PREV=$(git rev-parse HEAD)
    printf '%s\n' "$PREV" > .deploy-prev
    if [ -f "$UNIT" ]; then cp -p "$UNIT" .deploy-prev.service; else rm -f .deploy-prev.service; fi
    touch .deploy-pending
  fi
  git merge -q --ff-only "$target" || fail_ship "git merge --ff-only failed"
  say "shipping ${PREV:0:7} -> $(git log -1 --format='%h %s' HEAD)"
  git log -20 --format='   %h %s' "$PREV..HEAD"
  if git diff --name-only "$PREV" HEAD | grep -qx 'requirements.txt'; then
    say "   requirements.txt changed - pip install"
    .venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt \
      || fail_ship "pip install failed"
  else
    say "   requirements.txt unchanged - skipping pip"
  fi
  install_unit || fail_ship "unit install failed"
  systemctl restart "$SVC" || fail_ship "systemctl restart failed"
  if out=$(wait_health api_health); then
    rm -f .deploy-pending
    say "=============================================="
    say " DEPLOY OK   $(git log -1 --format='%h' HEAD)   health: $out"
    say " rollback:   bash deploy/deploy.sh --rollback   (to ${PREV:0:7})"
    say "=============================================="
  else
    fail_ship "health check failed (/api/health)"
  fi
}
ship </dev/null
EOF
exit $?
