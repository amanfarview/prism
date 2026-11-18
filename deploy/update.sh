#!/usr/bin/env bash
# Pull, install, test, migrate, restart.
#   deploy/update.sh            production  (/home/azureuser/prism,          services prism-*)
#   deploy/update.sh --staging  staging     (/home/azureuser/prism-staging,  services prism-staging-*)
set -euo pipefail

if [[ "${1:-}" == "--staging" ]]; then
  APP=/home/azureuser/prism-staging; SVC=prism-staging
else
  APP=/home/azureuser/prism; SVC=prism
fi

cd "$APP"
git pull --ff-only
venv/bin/pip install -q -r requirements.txt
PRISM_DATABASE_URL=sqlite:///:memory: venv/bin/python -m pytest -q          # tests run on a throwaway DB
set -a; source .env; set +a
venv/bin/python -m prism.cli init                                          # creates any new tables, safe to re-run
sudo systemctl restart "$SVC-web" "$SVC-worker"
systemctl is-active "$SVC-mcp" >/dev/null 2>&1 && sudo systemctl restart "$SVC-mcp" || true
sleep 3
curl -fsS "http://127.0.0.1:$(grep -oP '(?<=--port )\d+' /etc/systemd/system/$SVC-web.service)/prism/healthz" && echo " $SVC is up"
