#!/usr/bin/env bash
# Puts Prism behind Caddy at https://jarvis.farviewequity.com/prism/ . Safe to re-run.
# Refuses to expose Prism unless Microsoft sign-in is on. Backs up the Caddy file and
# restores it automatically if Caddy rejects the change or the check afterwards fails.
set -euo pipefail
cd "$HOME/prism"
say(){ printf '\n== %s\n' "$1"; }
HOST=jarvis.farviewequity.com
CF=""; BAK=""
restore(){ if [ -n "$BAK" ]; then sudo cp -p "$BAK" "$CF"; sudo systemctl reload caddy; echo "Original Caddy config restored."; fi; }

say "1. Prism with Microsoft sign-in on"
grep -q '^PRISM_AUTH_MODE=entra' .env || { echo "PRISM_AUTH_MODE=entra isn't in .env. Run deploy/go_live.sh first."; exit 1; }
sudo systemctl restart prism-web prism-worker
sudo systemctl enable --now prism-mcp
sleep 4
CODE=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8040/prism/)
[ "$CODE" = "303" ] || { echo "Prism answered $CODE without asking for sign-in. Not exposing it."; exit 1; }
echo "sign-in required: yes"

say "2. Caddy config"
CF=${CADDYFILE:-}; [ -n "$CF" ] || CF=$(sudo grep -rlE --exclude="*.bak-*" "^[^#]*${HOST//./\\.}[^{]*\{" /etc/caddy 2>/dev/null | head -1 || true)
[ -n "$CF" ] || { echo "No Caddy site block for $HOST under /etc/caddy. Nothing changed."; exit 1; }
echo "site block in: $CF"
if sudo grep -q 'BEGIN prism' "$CF"; then
  echo "Prism routes already there."
else
  BAK="$CF.bak-prism-$(date +%Y%m%d%H%M%S)"; sudo cp -p "$CF" "$BAK"; echo "backup: $BAK"
  sudo python3 - "$CF" "$HOST" <<'PY'
import re, sys
path, host = sys.argv[1], sys.argv[2]
s = open(path).read()
block = """
    # BEGIN prism (deploy/caddy_go_live.sh)
    redir /prism /prism/ 308
    handle /prism/mcp/* {
        uri strip_prefix /prism
        reverse_proxy 127.0.0.1:8041 {
            flush_interval -1
        }
    }
    handle /prism/* {
        header {
            X-Content-Type-Options nosniff
            Referrer-Policy strict-origin-when-cross-origin
            Strict-Transport-Security "max-age=31536000"
            Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"
        }
        reverse_proxy 127.0.0.1:8040
    }
    # END prism
"""
m = re.search(r"^[^#\n]*" + re.escape(host) + r"[^{\n]*\{[ \t]*$", s, re.M)
if not m:
    sys.exit("site block opening line not found")
open(path, "w").write(s[:m.end()] + block + s[m.end():])
PY
  if ! sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/tmp/caddy_validate.log 2>&1; then
    tail -5 /tmp/caddy_validate.log; restore; exit 1
  fi
  echo "Caddy accepted the change."
fi

say "3. Reload Caddy and check"
sudo systemctl reload caddy
sleep 2
LOCAL=$(curl -s -o /dev/null -w '%{http_code}' --resolve "$HOST:443:127.0.0.1" "https://$HOST/prism/healthz" || true)
PUBLIC=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "https://$HOST/prism/healthz" || true)
echo "through Caddy on this VM: $LOCAL   through the public address: $PUBLIC"
if [ "$LOCAL" != "200" ]; then
  restore
  echo "Caddy still isn't routing /prism. Send Claude the site block below:"
  sudo grep -vEi 'password|secret|token|key ' "$CF" | head -150
  exit 1
fi
if [ "${PUBLIC%% *}" != "200" ]; then
  echo "This VM's Caddy serves /prism, but the public address doesn't reach it: $HOST may point at another"
  echo "machine or gateway. Routes left in place (harmless). Send Claude this output."
  exit 1
fi
echo "Done. Open https://$HOST/prism/ and sign in with Microsoft."
