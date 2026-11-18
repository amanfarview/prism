#!/usr/bin/env bash
# Step 1: install Prism on the Jarvis VM and run it PRIVATELY (localhost only).
# You reach it through an SSH tunnel; nothing is exposed and no AI calls are made.
# Safe to re-run. Never deletes or overwrites your data or .env.
set -euo pipefail
APP=${APP:-/home/azureuser/prism}
RUN_USER=${RUN_USER:-$(id -un)}
cd "$APP"
say(){ printf '\n== %s\n' "$*"; }
in_use(){ python3 -c "import socket,sys; s=socket.socket(); sys.exit(0 if s.connect_ex(('127.0.0.1',$1))==0 else 1)"; }

say "Python 3.10+"
python3 -c 'import sys; assert sys.version_info >= (3,10), sys.version; print(sys.version.split()[0])'
python3 -c 'import venv' 2>/dev/null || sudo apt-get install -y python3-venv

say "Virtualenv and packages"
[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt

say "Tests (on a throwaway database)"
venv/bin/python -m pytest -q -p no:warnings

mkdir -p data logs && chmod 700 data

if [ ! -f .env ]; then
  say "Key Vault (looking for the vault Orbit already uses)"
  VAULT=$(grep -rhoE 'https://[A-Za-z0-9-]+\.vault\.azure\.net' /etc/systemd/system/*.service \
          "$HOME"/*/.env "$HOME"/*/*/.env "$HOME"/*/*/*/.env 2>/dev/null | sort -u | head -1 || true)
  BACKEND=file
  if [ -n "$VAULT" ] && PRISM_KEY_VAULT_URL="$VAULT/" timeout 90 venv/bin/python - <<'PY' 2>/dev/null
import os
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
c = SecretClient(os.environ["PRISM_KEY_VAULT_URL"], DefaultAzureCredential())
next(iter(c.list_properties_of_secrets()), None)
PY
  then BACKEND=keyvault; echo "Keys will be stored in $VAULT"
  else echo "No reachable Key Vault for this VM's identity. Keys will be stored in $APP/data/secrets.json (chmod 600)."
  fi

  say "Ports"
  PORT=8040; while in_use $PORT; do PORT=$((PORT+1)); done
  MCP_PORT=$((PORT+1)); while in_use $MCP_PORT; do MCP_PORT=$((MCP_PORT+1)); done
  echo "web $PORT, mcp $MCP_PORT"

  say "Writing .env"
  venv/bin/python deploy/envtool.py .env \
    "PRISM_DATABASE_URL=sqlite:///$APP/data/prism.db" \
    "PRISM_BASE_PATH=/prism" \
    "PRISM_PUBLIC_URL=http://localhost:$PORT/prism" \
    "PRISM_SESSION_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')" \
    "PRISM_AUTH_MODE=dev" \
    "PRISM_ADMIN_EMAILS=aman@farviewequity.com" \
    "PRISM_SECRET_BACKEND=$BACKEND" \
    "PRISM_KEY_VAULT_URL=${VAULT:+$VAULT/}" \
    "PRISM_SECRETS_FILE=$APP/data/secrets.json" \
    "PRISM_PORT=$PORT" \
    "PRISM_MCP_PORT=$MCP_PORT"
fi
set -a; . ./.env; set +a

say "Database and the four portcos"
venv/bin/python -m prism.cli init
venv/bin/python -m prism.cli seed

say "Services"
for u in web worker mcp; do
  sed -e "s#/home/azureuser/prism#$APP#g" -e "s#User=azureuser#User=$RUN_USER#" -e "s#--port 8040#--port $PRISM_PORT#" \
      "deploy/prism-$u.service" | sudo tee "/etc/systemd/system/prism-$u.service" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now prism-web prism-worker
sudo systemctl restart prism-web prism-worker
sleep 4
curl -fsS "http://127.0.0.1:$PRISM_PORT/prism/healthz" && echo "  Prism is running (private)"

cat <<EOF

Next, on your laptop (PowerShell):
  ssh -N -L 8040:127.0.0.1:$PRISM_PORT $RUN_USER@jarvis.farviewequity.com
then open  http://localhost:8040/prism/
EOF
