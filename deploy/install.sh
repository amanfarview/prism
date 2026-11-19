#!/usr/bin/env bash
# Prism: production install on the Jarvis VM (farview-mcp-server), per SERVER_OPERATIONS.md. Safe to re-run.
#
# Moves Prism off azureuser and out of /home/azureuser/prism into the Farview layout:
#   code      /var/www/prism            git checkout of main, owned by azureuser (who holds the GitHub key),
#                                       group prism, 750: the app can read its code but never change it
#   python    /opt/prism-venv           root-owned, read-only to the app
#   settings  /etc/prism/prism.env      root:root 600, loaded by systemd (the app can't even read the file)
#   state     /var/lib/prism            prism:prism 700: secrets.json (600), SQLite until the Azure SQL move
#   services  prism-web 8040, prism-worker, prism-mcp 8041 (all 127.0.0.1, user prism, sandboxed),
#             prism-health.timer; the `prism` command in /usr/local/bin
#
# Run from azureuser (it reads the new script straight from GitHub, without touching the old checkout):
#   cd ~/prism && git fetch -q origin && git show origin/main:deploy/install.sh > /tmp/prism-install.sh
#   sudo bash /tmp/prism-install.sh
#
# Nothing is deleted. The old folder /home/azureuser/prism (code, venv, .env, data/) is left exactly as it is;
# the old unit files are copied to /root/prism-old-units-<time>/. If the new services don't come up healthy,
# the old ones are put back and started again.
set -euo pipefail
OLD=/home/azureuser/prism; APP=/var/www/prism; VENV=/opt/prism-venv
ETC=/etc/prism; ENVF=$ETC/prism.env; STATE=/var/lib/prism; HOST=jarvis.farviewequity.com
STAMP=$(date +%Y%m%d%H%M%S)
ok()   { echo "  OK  $*"; }
note() { echo "  ..  $*"; }
die()  { echo "  STOPPED: $*"; exit 1; }
[ "$(id -u)" = 0 ] || die "run with sudo"
id azureuser >/dev/null 2>&1 || die "no azureuser on this machine"

echo "1/9 Checks before changing anything"
PY=$(command -v python3) || die "python3 missing"
"$PY" -c 'import sys; assert sys.version_info >= (3, 10)' || die "python 3.10+ needed"
"$PY" -c 'import venv, ensurepip' 2>/dev/null || { apt-get install -y -q python3-venv >/dev/null && ok "installed python3-venv"; }
command -v git >/dev/null || die "git missing"
ORIGIN=""
if [ -d "$OLD/.git" ]; then
  ORIGIN=$(sudo -u azureuser git -C "$OLD" config remote.origin.url)
  DIRTY=$(sudo -u azureuser git -C "$OLD" status --porcelain --untracked-files=no)
  [ -z "$DIRTY" ] || { echo "$DIRTY"; die "the old checkout $OLD has uncommitted changes (above): commit and push them first"; }
  sudo -u azureuser git -C "$OLD" fetch -q origin || die "can't reach GitHub as azureuser (test: sudo -u azureuser ssh -T git@github-prism)"
  AHEAD=$(sudo -u azureuser git -C "$OLD" rev-list --count origin/main..HEAD 2>/dev/null || echo 0)
  [ "$AHEAD" = 0 ] || die "the old checkout has $AHEAD commit(s) not on GitHub main: push them first"
  if [ -f "$OLD/.env" ]; then
    for k in PRISM_PORT PRISM_MCP_PORT; do
      v=$(grep -E "^$k=" "$OLD/.env" | tail -1 | cut -d= -f2- | tr -d '"' || true)
      want=8040; [ "$k" = PRISM_MCP_PORT ] && want=8041
      [ -z "$v" ] || [ "$v" = "$want" ] || die "old $k is $v; the units and Caddy block use $want. Tell Claude."
    done
  fi
  ok "old install found at $OLD (pushed, clean)"
elif [ -d "$APP/.git" ]; then
  ORIGIN=$(sudo -u azureuser git -C "$APP" config remote.origin.url)
  ok "no old install; $APP already there"
else
  ORIGIN=${PRISM_GIT_URL:-git@github-prism:amanfarview/prism.git}
  note "fresh install from $ORIGIN"
fi
for p in 8040 8041; do
  who=$(ss -Hltnp "sport = :$p" 2>/dev/null | grep -oE 'users:\(\("[^"]+"' | head -1 | cut -d'"' -f2 || true)
  [ -z "$who" ] || [[ "$who" =~ ^(uvicorn|python|python3)$ ]] || die "port $p is used by $who"
done
ok "ports 8040/8041 free or held by the current Prism"

echo "2/9 Service user"
if id prism >/dev/null 2>&1; then ok "prism exists"; else
  useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin prism
  ok "created system user prism (no home, no login)"
fi

echo "3/9 Code: $APP"
if [ -d "$APP/.git" ]; then ok "checkout already there ($(sudo -u azureuser git -C "$APP" rev-parse --short HEAD))"; else
  install -d -o azureuser -g prism -m 750 "$APP"
  sudo -u azureuser git clone -q --branch main "$ORIGIN" "$APP" || die "couldn't clone $ORIGIN as azureuser"
  ok "cloned main ($(sudo -u azureuser git -C "$APP" rev-parse --short HEAD))"
fi
chown azureuser:prism "$APP"; chmod 750 "$APP"
[ -f "$APP/deploy/prism-web.service" ] && grep -q '^User=prism' "$APP/deploy/prism-web.service" \
  || die "main on GitHub doesn't have the hardened units yet: merge the ops/production-hardening PR first"
ok "owned by azureuser, group prism, 750 (prism reads, can't write)"

echo "4/9 Python: $VENV"
[ -x "$VENV/bin/python" ] || { "$PY" -m venv "$VENV"; ok "created $VENV"; }
PIP_CACHE_DIR=/tmp/pip-prism "$VENV/bin/pip" install -q --disable-pip-version-check --upgrade pip
PIP_CACHE_DIR=/tmp/pip-prism "$VENV/bin/pip" install -q --disable-pip-version-check -r "$APP/requirements.txt"
chown -R root:root "$VENV"; chmod -R go-w,go+rX "$VENV"
ok "packages installed (pinned requirements.txt); root-owned, read-only to prism"

echo "5/9 Settings: $ENVF"
install -d -o root -g root -m 755 "$ETC"
if [ -f "$ENVF" ]; then ok "already there (left as it is)"; else
  umask 077
  "$PY" "$APP/deploy/envtool.py" import "$OLD/.env" "$ENVF"
  chown root:root "$ENVF"; chmod 600 "$ENVF"; umask 022
  ok "written from $( [ -f "$OLD/.env" ] && echo "$OLD/.env (paths moved, secrets moved to the secrets file)" || echo "a template")"
fi
grep -qE '^PRISM_AUTH_MODE="?dev' "$ENVF" && die "$ENVF says PRISM_AUTH_MODE=dev: production must sign in"

echo "6/9 State: $STATE"
install -d -o prism -g prism -m 700 "$STATE"
if [ -f "$OLD/data/prism.db" ] && [ ! -f "$STATE/prism.db" ]; then
  "$PY" - "$OLD/data/prism.db" "$STATE/prism.db" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True), sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
  ok "copied the SQLite database (online backup; the old file is untouched)"
fi
[ -f "$STATE/prism.db" ] && { chown prism:prism "$STATE/prism.db"; chmod 600 "$STATE/prism.db"; }
# secrets: old data/secrets.json, the old Key Vault, and a plaintext Entra secret in the old .env, merged into
# /var/lib/prism/secrets.json. Values travel through a pipe only: never printed, never in a temp file.
OLD_BACKEND=$(grep -E '^PRISM_SECRET_BACKEND=' "$OLD/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' || true)
if [ -f "$OLD/.env" ] && [ -x "$OLD/venv/bin/python" ]; then
  sudo -u azureuser bash -c 'cd "$1" && set -a && . ./.env && set +a && venv/bin/python "$2" export' _ "$OLD" "$APP/deploy/envtool.py" \
    | "$PY" "$APP/deploy/envtool.py" merge-secrets "$STATE/secrets.json"
  ok "secrets carried over from the old install (backend was ${OLD_BACKEND:-env})"
fi
[ -f "$STATE/secrets.json" ] && { chown prism:prism "$STATE/secrets.json"; chmod 600 "$STATE/secrets.json"; }
chown -R prism:prism "$STATE"

echo "7/9 Services"
BK=/root/prism-old-units-$STAMP; mkdir -p "$BK"; chmod 700 "$BK"
cp -p /etc/systemd/system/prism-*.service /etc/systemd/system/prism-*.timer "$BK"/ 2>/dev/null || true
OLD_ACTIVE=$(systemctl is-active prism-web 2>/dev/null || true)
rollback() {
  echo "  !!  putting the old services back"
  systemctl stop prism-web prism-worker prism-mcp prism-health.timer 2>/dev/null || true
  systemctl disable prism-health.timer 2>/dev/null || true
  rm -f /etc/systemd/system/prism-health.service /etc/systemd/system/prism-health.timer
  cp -p "$BK"/prism-*.service /etc/systemd/system/ 2>/dev/null || true
  systemctl daemon-reload
  [ "$OLD_ACTIVE" = active ] && systemctl start prism-web prism-worker prism-mcp || true
}
install -m 755 "$APP/deploy/prism" /usr/local/bin/prism
systemctl stop prism-web prism-worker prism-mcp 2>/dev/null || true
install -m 644 "$APP"/deploy/prism-web.service "$APP"/deploy/prism-worker.service "$APP"/deploy/prism-mcp.service \
  "$APP"/deploy/prism-health.service "$APP"/deploy/prism-health.timer /etc/systemd/system/
systemctl daemon-reload
if ! /usr/local/bin/prism init >/tmp/prism-init.log 2>&1; then cat /tmp/prism-init.log; rollback; die "prism init failed (above); old services restored"; fi
systemctl enable --now prism-web prism-worker prism-mcp prism-health.timer >/dev/null 2>&1
for i in $(seq 1 30); do
  sleep 2
  curl -fsS -m 5 http://127.0.0.1:8040/prism/healthz/ready 2>/dev/null | grep -q '"db":true' && break
  if [ "$i" = 30 ]; then journalctl -u prism-web -n 40 --no-pager; rollback; die "prism-web didn't get healthy in 60 s; old services restored"; fi
done
ok "prism-web, prism-worker, prism-mcp running as prism; health timer on; old units saved in $BK"

echo "8/9 Proof"
FAIL=0
for u in prism-web prism-worker prism-mcp; do
  [ "$(systemctl show "$u" -p User --value)" = prism ] && systemctl is-active --quiet "$u" || { echo "  FAIL $u not active as prism"; FAIL=1; }
  systemctl show "$u" -p IPAddressDeny --value | grep -q 169.254.169.254 || { echo "  FAIL $u has no metadata block"; FAIL=1; }
done
ss -Hltn | awk '{print $4}' | grep -E ':(8040|8041)$' | grep -vqE '^127\.0\.0\.1:' && { echo "  FAIL a Prism port listens beyond 127.0.0.1"; FAIL=1; }
if systemd-run --quiet --wait --pipe --collect -p User=prism -p IPAddressDeny=169.254.169.254/32 \
     /usr/bin/timeout 4 bash -c '</dev/tcp/169.254.169.254/80' 2>/dev/null; then
  echo "  FAIL IPAddressDeny isn't enforced on this kernel: tell Claude"; FAIL=1
fi
/usr/local/bin/prism selfcheck || FAIL=1
LOCAL=$(curl -s -o /dev/null -w '%{http_code}' --resolve "$HOST:443:127.0.0.1" "https://$HOST/prism/healthz" || true)
SIGNED_OUT=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --resolve "$HOST:443:127.0.0.1" "https://$HOST/prism/" || true)
[ "$LOCAL" = 200 ] && ok "through Caddy: healthz 200, signed-out $SIGNED_OUT" || { echo "  FAIL through Caddy: healthz $LOCAL"; FAIL=1; }
[ "$FAIL" = 0 ] || die "a check above failed. Prism is running on the new layout; send Claude this output."

echo "9/9 The old install"
note "$OLD is untouched (its .env still holds the old secrets). Once you're happy, archive it (nothing deleted):"
note "  sudo mv $OLD /root/prism-old-$STAMP && sudo chmod 700 /root/prism-old-$STAMP"
note "Next: sudo bash $APP/deploy/caddy_go_live.sh   (updated Caddy block: CSP without inline scripts, MCP path, 25 MB limit)"
echo "Done. Prism runs as prism from $APP. Day to day: sudo bash $APP/deploy/update.sh"
