#!/usr/bin/env bash
# Step 2: put Prism on https://jarvis.farviewequity.com/prism behind Microsoft sign-in.
# Uses Orbit's "Farview Apps" Entra registration and Orbit's nginx server block.
# Backs up the nginx file first and restores it automatically if the config test fails.
set -euo pipefail
APP=${APP:-/home/azureuser/prism}
cd "$APP"
set -a; . ./.env; set +a
say(){ printf '\n== %s\n' "$*"; }

say "Entra details (reading Orbit's config)"
eval "$(venv/bin/python deploy/find_orbit_config.py)"
TENANT=${ORBIT_TENANT:-}; CLIENT=${ORBIT_CLIENT:-}; SECRET=${ORBIT_SECRET:-}
[ -n "${ORBIT_ENV_FILE:-}" ] && echo "found in $ORBIT_ENV_FILE"
[ -n "$TENANT" ] || read -rp "Entra tenant id: " TENANT
[ -n "$CLIENT" ] || read -rp "'Farview Apps' client id: " CLIENT
if [ -z "$SECRET" ]; then read -rsp "'Farview Apps' client secret (hidden): " SECRET; echo; fi
echo "tenant $TENANT, client $CLIENT, secret ${SECRET:+set}"

if [ "$PRISM_SECRET_BACKEND" = "keyvault" ]; then
  SECRET="$SECRET" venv/bin/python -c 'import os; from prism.secrets import set_secret; set_secret("prism-entra-client-secret", os.environ["SECRET"])'
  SECRET_LINE="PRISM_ENTRA_CLIENT_SECRET="
else
  SECRET_LINE="PRISM_ENTRA_CLIENT_SECRET=$SECRET"
fi
venv/bin/python deploy/envtool.py .env \
  "PRISM_AUTH_MODE=entra" \
  "PRISM_PUBLIC_URL=https://jarvis.farviewequity.com/prism" \
  "PRISM_ENTRA_TENANT_ID=$TENANT" \
  "PRISM_ENTRA_CLIENT_ID=$CLIENT" \
  "$SECRET_LINE"
set -a; . ./.env; set +a

say "nginx"
NGX=$(sudo grep -rlE 'location[^{]*/orbit' /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null | head -1 || true)
[ -n "$NGX" ] || { echo "Couldn't find the nginx file that serves /orbit. Stopping; nothing changed."; exit 1; }
NGX=$(readlink -f "$NGX"); echo "jarvis server block: $NGX"
sed -e "s#127.0.0.1:8040#127.0.0.1:$PRISM_PORT#" -e "s#127.0.0.1:8041#127.0.0.1:$PRISM_MCP_PORT#" \
    deploy/nginx-prism.conf > deploy/nginx-prism.live.conf
INC="include $APP/deploy/nginx-prism.live.conf;"
if ! sudo grep -qF "$INC" "$NGX"; then
  BAK="$NGX.bak-prism-$(date +%Y%m%d%H%M%S)"
  sudo cp -p "$NGX" "$BAK"; echo "backup: $BAK"
  sudo python3 - "$NGX" "$INC" <<'PY'
import re, sys
path, inc = sys.argv[1], sys.argv[2]
s = open(path).read()
hits = list(re.finditer(r"^([ \t]*)location[^{]*/orbit", s, re.M))
def in_https_block(m):
    start = s.rfind("server", 0, m.start())
    return start != -1 and re.search(r"listen[^;]*443|ssl_certificate", s[start:m.start()])
m = next((h for h in hits if in_https_block(h)), hits[0])
s = s[:m.start()] + m.group(1) + inc + "\n" + s[m.start():]
open(path, "w").write(s)
PY
  if ! sudo nginx -t; then sudo cp -p "$BAK" "$NGX"; echo "nginx test failed: original restored, nothing changed."; exit 1; fi
fi
sudo systemctl reload nginx

say "Restart Prism and start the MCP server"
sudo systemctl restart prism-web prism-worker
sudo systemctl enable --now prism-mcp
sleep 4
curl -fsS https://jarvis.farviewequity.com/prism/healthz && echo "  live"
echo
echo "Open https://jarvis.farviewequity.com/prism/ and sign in with your Farview account."
