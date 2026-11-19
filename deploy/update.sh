#!/usr/bin/env bash
# Prism: deploy a new version (git-first, never SCP). Run from azureuser:
#   sudo bash /var/www/prism/deploy/update.sh              production: latest main (merged by PR, green CI)
#   sudo bash /var/www/prism/deploy/update.sh --staging    staging: latest dev, at /prism-staging
#   sudo bash /var/www/prism/deploy/update.sh --rollback   production: back to the version before the last deploy
#   add --force to restart the worker even while a paid run is in progress (the run is cut short; answers kept)
#
# Same shape as Orbit's update.sh: refuses if the server copy has uncommitted changes, fast-forward only,
# notes the database restore point before creating tables, restarts, waits for /healthz/ready, logs one line to
# /var/log/prism-deploys.log and posts the result to Teams (production only).
# The code is owned by azureuser (who holds the GitHub key) with group prism read-only, so there is no
# chown dance: the app can read its code and can never change it.
set -euo pipefail
if [ "${PRISM_UPDATE_COPY:-}" != 1 ]; then   # run from a copy: the pull below can rewrite this very file
  cp "$0" /tmp/prism-update.sh; PRISM_UPDATE_COPY=1 exec bash /tmp/prism-update.sh "$@"
fi
MODE=production; FORCE=0
for a in "$@"; do case "$a" in --staging) MODE=staging ;; --rollback) MODE=rollback ;; --force) FORCE=1 ;;
  *) echo "unknown option $a"; exit 2 ;; esac; done
LOG=/var/log/prism-deploys.log
if [ "$MODE" = staging ]; then
  APP=/var/www/prism-staging; VENV=/opt/prism-staging-venv; BRANCH=dev; PORT=8042; URLP=prism-staging
  UNITS=(prism-staging-web prism-staging-worker); CLI=(/usr/local/bin/prism --staging)
  [ -d "$APP/.git" ] || { echo "STOPPED: staging isn't set up: sudo bash /var/www/prism/deploy/staging_setup.sh"; exit 1; }
else
  APP=/var/www/prism; VENV=/opt/prism-venv; BRANCH=main; PORT=8040; URLP=prism
  UNITS=(prism-web prism-worker prism-mcp); CLI=(/usr/local/bin/prism)
fi
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
[ -d "$APP/.git" ] || { echo "STOPPED: $APP isn't there: run deploy/install.sh first"; exit 1; }
G() { sudo -u azureuser git -C "$APP" "$@"; }
alert() {    # Teams; production only (staging posts nothing on purpose)
  [ "$MODE" = staging ] && return 0
  systemd-run --quiet --wait --pipe --collect -p User=prism -p EnvironmentFile=/etc/prism/prism.env -p StateDirectory=prism \
    -p IPAddressDeny=169.254.169.254/32 /opt/prism-venv/bin/python /var/www/prism/deploy/healthcheck.py deploy "$@" || true
}
stop() { echo "STOPPED: $1"; alert fail "${AFTER:-?}" "$1"; exit 1; }

# a paid run in progress: don't cut it short unless asked
ACTIVE=$("${CLI[@]}" active-runs 2>/dev/null | tr -d '\r' || true)
if [ -n "$ACTIVE" ] && [ "$FORCE" = 0 ]; then
  echo "STOPPED: run(s) $ACTIVE in progress. Deploy when they finish, or add --force (cuts them short; answers kept)."; exit 1
fi

if [ -n "$(G status --porcelain --untracked-files=no)" ]; then
  G status --short --untracked-files=no
  echo "STOPPED: the server copy has uncommitted changes (above). Don't discard them blindly: send them to Claude."; exit 1
fi
BEFORE=$(G rev-parse --short HEAD)
if [ "$MODE" = rollback ]; then
  LAST=$(grep -E "^[^ ]+ production " "$LOG" 2>/dev/null | tail -1 || true)
  [ -n "$LAST" ] || { echo "STOPPED: no production deploy in $LOG to roll back"; exit 1; }
  PREV=$(echo "$LAST" | awk '{print $3}')
  [ "$(echo "$LAST" | awk '{print $4}')" = "$BEFORE" ] || { echo "STOPPED: the code isn't at the last deploy's version (log: $LAST)"; exit 1; }
  G reset -q --hard "$PREV"
  echo "code rolled back: $BEFORE -> $PREV"
else
  G fetch -q origin "$BRANCH" || { echo "GitHub fetch failed, trying again in 15 s"; sleep 15
    G fetch -q origin "$BRANCH" || stop "couldn't fetch $BRANCH from GitHub (twice). Test: sudo -u azureuser ssh -T git@github-prism"; }
  G checkout -q "$BRANCH"
  G merge -q --ff-only "origin/$BRANCH" || stop "$BRANCH on the server can't fast-forward to GitHub's"
fi
AFTER=$(G rev-parse --short HEAD)
echo "code: $BEFORE -> $AFTER ($MODE)"
chown azureuser:prism "$APP"; chmod 750 "$APP"

PIP_CACHE_DIR=/tmp/pip-prism "$VENV/bin/pip" install -q --disable-pip-version-check -r "$APP/requirements.txt" \
  || stop "pip install failed; nothing restarted, the old version is still running"
chown -R root:root "$VENV"; chmod -R go-w,go+rX "$VENV"

if [ "$MODE" = staging ]; then
  install -m 644 "$APP"/deploy/prism-staging-web.service "$APP"/deploy/prism-staging-worker.service /etc/systemd/system/
else
  install -m 644 "$APP"/deploy/prism-web.service "$APP"/deploy/prism-worker.service "$APP"/deploy/prism-mcp.service \
    "$APP"/deploy/prism-health.service "$APP"/deploy/prism-health.timer /etc/systemd/system/
  install -m 755 "$APP/deploy/prism" /usr/local/bin/prism
fi
systemctl daemon-reload

# database: restore point first, then any new tables (Prism only ever adds tables; it never alters or drops)
RP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
"${CLI[@]}" init >/tmp/prism-init.log 2>&1 || { sleep 20; "${CLI[@]}" init >/tmp/prism-init.log 2>&1 || {
  cat /tmp/prism-init.log; stop "creating tables failed twice: nothing restarted. Azure SQL restore point: $RP"; }; }

systemctl restart "${UNITS[@]}"
[ "$MODE" = staging ] || systemctl enable --now prism-health.timer >/dev/null 2>&1 || true
for i in $(seq 1 45); do
  sleep 2
  for u in "${UNITS[@]}"; do systemctl is-active --quiet "$u" || { journalctl -u "$u" -n 40 --no-pager; stop "$u didn't start (restore point $RP)"; }; done
  curl -fsS -m 5 "http://127.0.0.1:$PORT/$URLP/healthz/ready" 2>/dev/null | grep -q '"db":true' && break
  [ "$i" = 45 ] && { journalctl -u "${UNITS[0]}" -n 40 --no-pager; stop "not healthy after 90 s (restore point $RP)"; }
done
"${CLI[@]}" selfcheck >/tmp/prism-selfcheck.log 2>&1 || { cat /tmp/prism-selfcheck.log; stop "selfcheck failed after deploy"; }
touch "$LOG"; chmod 644 "$LOG"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $MODE $BEFORE $AFTER $RP" >> "$LOG"
if [ "$MODE" = staging ]; then
  echo "Staging $AFTER is running: https://jarvis.farviewequity.com/prism-staging/"
else
  alert ok "$AFTER" "$([ "$MODE" = rollback ] && echo "Rolled back from $BEFORE." || echo "From $BEFORE.")"
  echo "Prism $AFTER is running: https://jarvis.farviewequity.com/prism/"
fi
