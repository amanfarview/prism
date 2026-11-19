#!/usr/bin/env bash
# Prism STAGING, one-time set-up (Orbit's pattern). Run from azureuser:  sudo bash /var/www/prism/deploy/staging_setup.sh
# Then every time you want staging on the latest dev:  sudo bash /var/www/prism/deploy/update.sh --staging
#
#   settings  /etc/prism/prism-staging.env: only overrides on top of prism.env (staging environment, /prism-staging,
#             its own session secret, its own schema or SQLite file, its own secrets file)
#   secrets   /var/lib/prism-staging/secrets.json holds ONLY the Entra sign-in secret; no provider keys, no Teams.
#             The staging units can't see /var/lib/prism at all, and the code refuses paid calls in staging.
#   code      /var/www/prism-staging, dev branch; python /opt/prism-staging-venv (its own, so a new package on dev
#             never touches production)
#   services  prism-staging-web (127.0.0.1:8042), prism-staging-worker
#   Caddy     /prism-staging (deploy/caddy_go_live.sh --staging), after staging answers with sign-in
#   Entra     add https://jarvis.farviewequity.com/prism-staging/auth/callback as a Web redirect URI first
set -euo pipefail
PROD=/var/www/prism; APP=/var/www/prism-staging; VENV=/opt/prism-staging-venv
ENVF=/etc/prism/prism-staging.env; STATE=/var/lib/prism-staging
ok() { echo "  OK  $*"; }
die() { echo "  STOPPED: $*"; exit 1; }
[ "$(id -u)" = 0 ] || die "run with sudo"
[ -f /etc/prism/prism.env ] || die "/etc/prism/prism.env is missing: run deploy/install.sh first"

echo "1/5 Settings"
if [ -f "$ENVF" ]; then ok "$ENVF already there (left as it is)"; else
  if grep -qE '^PRISM_DATABASE_URL="?mssql' /etc/prism/prism.env; then DB_LINE='PRISM_DB_SCHEMA="prism_staging"'
  else DB_LINE='PRISM_DATABASE_URL="sqlite:////var/lib/prism-staging/prism.db"'; fi
  umask 077
  cat > "$ENVF" <<ENV
# Prism STAGING: read AFTER /etc/prism/prism.env, so every line here overrides production's.
PRISM_ENVIRONMENT="staging"
PRISM_BASE_PATH="/prism-staging"
PRISM_PUBLIC_URL="https://jarvis.farviewequity.com/prism-staging"
PRISM_SESSION_SECRET="$(openssl rand -hex 32)"
PRISM_SECRETS_FILE="/var/lib/prism-staging/secrets.json"
PRISM_PORT="8042"
PRISM_MCP_PORT="8043"
$DB_LINE
ENV
  chown root:root "$ENVF"; chmod 600 "$ENVF"; umask 022
  ok "wrote $ENVF"
fi

echo "2/5 Secrets (sign-in only)"
install -d -o prism -g prism -m 700 "$STATE"
python3 - /var/lib/prism/secrets.json "$STATE/secrets.json" <<'PY'
import json, os, sys
src, dst = sys.argv[1], sys.argv[2]
prod = json.load(open(src)) if os.path.exists(src) else {}
cur = json.load(open(dst)) if os.path.exists(dst) else {}
if "prism-entra-client-secret" in prod and "prism-entra-client-secret" not in cur:
    cur["prism-entra-client-secret"] = prod["prism-entra-client-secret"]
fd = os.open(dst + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(cur, f)
os.replace(dst + ".tmp", dst)
print(f"  OK  staging secrets file: {', '.join(sorted(cur)) or 'empty (set PRISM_ENTRA_CLIENT_SECRET or: sudo prism --staging set-key entra)'}")
PY
chown -R prism:prism "$STATE"; chmod 600 "$STATE/secrets.json"

echo "3/5 Code (dev branch)"
if [ -d "$APP/.git" ]; then ok "$APP already there"; else
  ORIGIN=$(sudo -u azureuser git -C "$PROD" config remote.origin.url)
  install -d -o azureuser -g prism -m 750 "$APP"
  if sudo -u azureuser git ls-remote --exit-code --heads "$ORIGIN" dev >/dev/null 2>&1; then
    sudo -u azureuser git clone -q --branch dev "$ORIGIN" "$APP"
  else
    die "there is no dev branch on GitHub yet. On your laptop: git checkout main; git pull; git checkout -b dev; git push -u origin dev"
  fi
  ok "cloned dev ($(sudo -u azureuser git -C "$APP" rev-parse --short HEAD))"
fi
chown azureuser:prism "$APP"; chmod 750 "$APP"

echo "4/5 Python"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
PIP_CACHE_DIR=/tmp/pip-prism "$VENV/bin/pip" install -q --disable-pip-version-check -r "$APP/requirements.txt"
chown -R root:root "$VENV"; chmod -R go-w,go+rX "$VENV"
ok "$VENV ready"

echo "5/5 Services"
install -m 644 "$APP"/deploy/prism-staging-web.service "$APP"/deploy/prism-staging-worker.service /etc/systemd/system/
systemctl daemon-reload
/usr/local/bin/prism --staging init >/dev/null && /usr/local/bin/prism --staging seed >/dev/null || die "staging init/seed failed"
systemctl enable --now prism-staging-web prism-staging-worker >/dev/null 2>&1
sleep 5
curl -fsS -m 5 http://127.0.0.1:8042/prism-staging/healthz/ready | grep -q '"db":true' || die "staging isn't healthy: journalctl -u prism-staging-web -n 50"
ok "staging running on 127.0.0.1:8042"
echo
echo "Next: sudo bash $PROD/deploy/caddy_go_live.sh --staging    (publishes /prism-staging behind sign-in)"
